"""Bounded downloads and one-message-at-a-time GRIB decoding.

No cfgrib index or full 81-level array is created. Signed URLs stay out of logs.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

import numpy as np
import requests
from eccodes import codes_get, codes_get_values, codes_grib_new_from_file, codes_release


RESERVE_BYTES = 10 * 1024**3
MAX_FILE_BYTES = 512 * 1024**2


def check_space(directory, additional=0):
    if shutil.disk_usage(directory).free - additional < RESERVE_BYTES:
        raise RuntimeError("W prototype stopped to preserve the 10 GiB free-space reserve")


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    payload = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    check_space(path.parent, len(payload))
    try:
        with temporary.open("wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return payload


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url, path, *, session=None, reuse=False, cancelled=None):
    """Refresh static files by ETag, or download a forecast with a fixed size cap."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = path.with_suffix(path.suffix + ".json")
    metadata = json.loads(metadata_path.read_text()) if reuse and metadata_path.exists() else {}
    client = session or requests.Session()
    part = path.with_suffix(path.suffix + ".part")
    started = time.monotonic()
    check_space(path.parent, MAX_FILE_BYTES)
    try:
        if cancelled is not None and cancelled.is_set():
            raise RuntimeError("W lookahead download cancelled")
        with client.get(url, stream=True, timeout=(15, 60), headers={"Accept-Encoding": "identity"}) as response:
            # Never include a pre-signed URL in an exception or saved report.
            if response.status_code != 200:
                raise RuntimeError(f"source download returned HTTP {response.status_code}")
            etag = response.headers.get("ETag")
            if (reuse and etag and etag == metadata.get("etag") and path.is_file()
                    and path.stat().st_size == metadata.get("bytes")
                    and sha256_file(path) == metadata.get("sha256")):
                return {**metadata, "reused": True, "download_seconds": 0.0}
            expected = int(response.headers.get("Content-Length", "0"))
            if expected > MAX_FILE_BYTES:
                raise RuntimeError("source file exceeds the 512 MiB download cap")
            digest = hashlib.sha256()
            count = 0
            with part.open("wb") as f:
                for block in response.iter_content(1024 * 1024):
                    if cancelled is not None and cancelled.is_set():
                        raise RuntimeError("W lookahead download cancelled")
                    count += len(block)
                    if count > MAX_FILE_BYTES or time.monotonic() - started > 600:
                        raise RuntimeError("source download exceeded its size or time budget")
                    check_space(path.parent, len(block))
                    f.write(block)
                    digest.update(block)
                f.flush()
                os.fsync(f.fileno())
            if count == 0 or (expected and count != expected):
                raise RuntimeError("source download is truncated")
            sha256 = digest.hexdigest()
            provider_sha = response.headers.get("x-amz-meta-sha256")
            if provider_sha and provider_sha.lower() != sha256:
                raise RuntimeError("source SHA-256 does not match the provider checksum")
            os.replace(part, path)
            result = {"bytes": count, "sha256": sha256, "etag": etag}
            if reuse:
                atomic_json(metadata_path, result)
            return {**result, "reused": False, "download_seconds": time.monotonic() - started}
    finally:
        part.unlink(missing_ok=True)
        if session is None:
            client.close()


@contextmanager
def messages(path):
    """The iterator releases each ecCodes handle before advancing."""
    def iterate(f):
        while True:
            handle = codes_grib_new_from_file(f)
            if handle is None:
                break
            try:
                yield handle
            finally:
                codes_release(handle)
    with Path(path).open("rb") as f:
        iterator = iterate(f)
        try:
            yield iterator
        finally:
            iterator.close()


def identity(handle):
    return {key: str(codes_get(handle, key)) for key in ("uuidOfHGrid", "uuidOfVGrid")}


def coordinates(path):
    result = {}
    horizontal_uuid = None
    with messages(path) as records:
        for handle in records:
            name = str(codes_get(handle, "shortName")).lower()
            if name not in ("clat", "clon", "tlat", "tlon"):
                continue
            key = "lat" if name.endswith("lat") else "lon"
            values = codes_get_values(handle)
            unit = str(codes_get(handle, "units")).lower()
            if "rad" in unit:
                values = np.rad2deg(values)
            elif "degree" not in unit:
                raise ValueError(f"unexpected horizontal coordinate unit {unit}")
            result[key] = values
            uuid = str(codes_get(handle, "uuidOfHGrid"))
            if horizontal_uuid is not None and uuid != horizontal_uuid:
                raise ValueError("horizontal coordinate grid UUIDs differ")
            horizontal_uuid = uuid
    if set(result) != {"lat", "lon"} or result["lat"].shape != result["lon"].shape:
        raise ValueError("static file must contain matching cell latitude and longitude")
    result["uuid"] = horizontal_uuid
    return result
