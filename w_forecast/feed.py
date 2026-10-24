"""Immutable compressed frames, atomic discovery, validated resume and bounded GC."""

import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import time

import numpy as np

from .grib import atomic_json, check_space, sha256_file


SCHEMA = "xcbenz.w.ch1.v1"
SCALE = 0.01
FILL = -32768
OBJECT_RE = re.compile(r"^[0-9a-f]{64}\.bin\.gz$")
RUN_RE = re.compile(r"^\d{8}_\d{4}$")


def utc(value):
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def pack(values):
    finite = np.isfinite(values)
    quantized = np.rint(np.where(finite, values, 0) / SCALE)
    if np.any(finite & ((quantized < -32767) | (quantized > 32767))):
        raise ValueError("W exceeds the signed int16 encoding range; refusing to clip it")
    result = np.full(values.shape, FILL, dtype="<i2")
    result[finite] = quantized[finite].astype("<i2")
    return result


class Feed:
    def __init__(self, root, config):
        self.root, self.config = Path(root), config
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "objects").mkdir(exist_ok=True)
        (self.root / "runs").mkdir(exist_ok=True)

    def verified_frames(self, frames, reference_time):
        results = {}
        for frame in frames:
            asset = frame["artifact"]
            if (not re.fullmatch(r"[a-f0-9]{64}", asset["sha256"])
                    or asset["path"] != "objects/" + asset["sha256"] + ".bin.gz"):
                raise ValueError("unsafe cached frame path")
            if (asset["decoded_bytes"] != self.config.width * self.config.height * 2
                    or frame["altitude_m"] not in self.config.altitudes
                    or frame["valid_time"] != utc(reference_time + dt.timedelta(hours=frame["horizon_hours"]))):
                raise ValueError("cached frame metadata differs from the contract")
            obj = self.root / asset["path"]
            if (obj.is_file() and obj.stat().st_size == asset["bytes"]
                    and sha256_file(obj) == asset["sha256"]):
                results[(frame["horizon_hours"], frame["altitude_m"])] = frame
        return results

    def existing(self, reference_time, static_hashes=None):
        """Resume only a checksum-verified manifest for this exact run and config."""
        tag = reference_time.strftime("%Y%m%d_%H%M")
        results = {}
        for path in sorted((self.root / "runs" / tag).glob("*.json")):
            manifest = json.loads(path.read_text())
            if (manifest.get("schema") != SCHEMA or manifest.get("config_id") != self.config.key
                    or manifest.get("reference_time") != utc(reference_time)):
                continue
            if static_hashes is not None and manifest["source"].get("static_sha256") != static_hashes:
                continue
            results.update(self.verified_frames(manifest["frames"], reference_time))
        return results

    def frame(self, values, reference_time, horizon, altitude):
        if np.shape(values) != (self.config.height, self.config.width):
            raise ValueError("W frame shape does not match the declared grid")
        if altitude not in self.config.altitudes:
            raise ValueError("W frame altitude is absent from the output contract")
        encoded = pack(values)
        payload = gzip.compress(encoded.tobytes(), compresslevel=3, mtime=0)
        digest = hashlib.sha256(payload).hexdigest()
        path = self.root / "objects" / (digest + ".bin.gz")
        check_space(self.root, len(payload))
        if path.exists():
            if sha256_file(path) != digest:
                raise ValueError("existing immutable W object is corrupt")
        else:
            temporary = path.with_suffix(".part")
            try:
                with temporary.open("wb") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        valid = encoded != FILL
        valid_values = encoded[valid].astype(np.float32) * SCALE
        if valid_values.size == 0:
            raise ValueError("W frame contains no valid cells")
        return {
            "horizon_hours": horizon, "valid_time": utc(reference_time + dt.timedelta(hours=horizon)),
            "altitude_m": altitude, "altitude_reference": "AMSL",
            "valid_cells": int(valid.sum()), "min_m_s": round(float(valid_values.min()), 2),
            "max_m_s": round(float(valid_values.max()), 2),
            "artifact": {"path": "objects/" + path.name, "sha256": digest,
                         "bytes": len(payload), "decoded_bytes": encoded.nbytes, "compression": "gzip"},
        }

    def publish(self, frames, reference_time, horizons, maximum_horizon, source, provenance):
        expected = {(h, a) for h in horizons for a in self.config.altitudes}
        actual = [(f["horizon_hours"], f["altitude_m"]) for f in frames]
        if set(actual) != expected or len(actual) != len(expected):
            raise ValueError("refusing to publish incomplete or duplicate W frames")
        # Validate all bytes before advancing discovery, including resumed objects.
        for frame in frames:
            asset = frame["artifact"]
            if (asset["path"] != "objects/" + asset["sha256"] + ".bin.gz"
                    or asset["decoded_bytes"] != self.config.width * self.config.height * 2
                    or frame["valid_time"] != utc(reference_time + dt.timedelta(hours=frame["horizon_hours"]))):
                raise ValueError("W frame metadata validation failed before publication")
            path = self.root / asset["path"]
            if path.stat().st_size != asset["bytes"] or sha256_file(path) != asset["sha256"]:
                raise ValueError("W frame validation failed before publication")
        pointer_path = self.root / "latest.json"
        previous = json.loads(pointer_path.read_text()) if pointer_path.exists() else None
        if previous and previous["reference_time"] > utc(reference_time):
            raise ValueError("refusing to replace a newer forecast with an older run")
        if previous and previous["reference_time"] == utc(reference_time):
            previous_manifest = json.loads((self.root / previous["manifest"]).read_text())
            if previous_manifest["config_id"] == self.config.key:
                previous_hours = set(previous_manifest["coverage"]["horizons_hours"])
                if not previous_hours.issubset(horizons):
                    raise ValueError("refusing to reduce coverage of the current W run")
        manifest = {
            "schema": SCHEMA, "model": "ICON-CH1-EPS", "member": "control",
            "parameter": "W", "quantity": "geometric_vertical_velocity", "units": "m/s",
            "positive_direction": "up", "altitude_reference": "AMSL",
            "operational_mode": "prototype_snapshot", "config_id": self.config.key,
            "reference_time": utc(reference_time), "grid": self.config.grid(),
            "encoding": {"dtype": "int16", "byte_order": "little", "scale_factor": SCALE,
                         "add_offset": 0, "fill_value": FILL},
            "altitudes_m": list(self.config.altitudes),
            "coverage": {"horizons_hours": list(horizons), "model_maximum_horizon_hours": maximum_horizon,
                         "full_model_run": list(horizons) == list(range(maximum_horizon + 1))},
            "source": source, "provenance": provenance,
            "interpretation": "Model-resolved vertical air motion. Not thermal climb rate or glider net climb.",
            "interpolation": "Linear on native HHL half levels, then barycentric horizontally; no extrapolation.",
            "frames": sorted(frames, key=lambda f: (f["horizon_hours"], f["altitude_m"])),
        }
        payload = (json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        digest = hashlib.sha256(payload).hexdigest()
        tag = reference_time.strftime("%Y%m%d_%H%M")
        directory = self.root / "runs" / tag
        directory.mkdir(exist_ok=True)
        destination = directory / (digest + ".json")
        if not destination.exists():
            atomic_json(destination, manifest)
        pointer = {"schema": SCHEMA, "reference_time": utc(reference_time),
                   "published_at": utc(dt.datetime.now(dt.timezone.utc)),
                   "operational_mode": "prototype_snapshot",
                   "manifest": f"runs/{tag}/{digest}.json", "sha256": digest, "bytes": len(payload)}
        atomic_json(pointer_path, pointer)
        return pointer

    def prune(self, keep_runs=2, grace_seconds=21600):
        """Keep two newest source runs plus six hours for in-flight browser reads."""
        if keep_runs < 2 or grace_seconds < 21600:
            raise ValueError("retention must keep at least two runs and six hours of grace")
        pointer = json.loads((self.root / "latest.json").read_text())
        current = self.root / pointer["manifest"]
        directories = sorted((p for p in (self.root / "runs").iterdir() if p.is_dir() and RUN_RE.fullmatch(p.name)), reverse=True)
        keep = set(directories[:keep_runs])
        cutoff = time.time() - grace_seconds
        manifests = []
        for directory in directories:
            paths = list(directory.glob("*.json"))
            for path in paths:
                if directory in keep or path == current or path.stat().st_mtime >= cutoff:
                    manifests.append(path)
                else:
                    path.unlink()
            if not list(directory.iterdir()):
                directory.rmdir()
        reachable = set()
        # Parse everything before removing any object. Malformed manifests fail closed.
        for path in manifests:
            manifest = json.loads(path.read_text())
            reachable.update(f["artifact"]["path"] for f in manifest["frames"])
        for path in (self.root / "objects").iterdir():
            if (OBJECT_RE.fullmatch(path.name) and "objects/" + path.name not in reachable
                    and path.stat().st_mtime < cutoff):
                path.unlink()
