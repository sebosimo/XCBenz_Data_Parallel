"""Smoke-test a remote web_exports data host."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from value_tiles import (
    canonical_json_bytes,
    capability_declaration,
    parse_xvt,
    sha256_bytes,
)


BASE_URL = os.environ.get("DATA_BASE_URL", "https://data.xcbenz.com").rstrip("/") + "/"
TIMEOUT = float(os.environ.get("REMOTE_VALIDATE_TIMEOUT", "30"))
RETRIES = int(os.environ.get("REMOTE_VALIDATE_RETRIES", "3"))
RETRY_DELAY = float(os.environ.get("REMOTE_VALIDATE_RETRY_DELAY", "10"))
EXPECTED_VALUE_TILES_STATE = os.environ.get("EXPECTED_VALUE_TILES_STATE", "optional").strip().lower()


class ValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class FetchResult:
    url: str
    data: bytes
    headers: Any


def log(message: str) -> None:
    print(f"[remote-validate] {message}", flush=True)


def fail(message: str) -> int:
    print(f"[remote-validate] ERROR: {message}", file=sys.stderr, flush=True)
    return 1


def resolve_url(path: str, context_url: str | None = None) -> str:
    if path.startswith(("http://", "https://")):
        return path
    if path.startswith("web_exports/"):
        return urljoin(BASE_URL, path)
    if context_url:
        return urljoin(context_url, path)
    return urljoin(BASE_URL, path)


def fetch(url: str, *, max_bytes: int | None = None) -> FetchResult:
    headers = {"User-Agent": "xcbenz-remote-validator/1.0"}
    if max_bytes is not None:
        headers["Range"] = f"bytes=0-{max_bytes - 1}"
    request = Request(url, headers=headers)
    last_error: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urlopen(request, timeout=TIMEOUT) as response:
                data = response.read()
                return FetchResult(url=url, data=data, headers=response.headers)
        except HTTPError as exc:
            if 400 <= exc.code < 500:
                raise ValidationError(f"{url} returned HTTP {exc.code}") from exc
            last_error = exc
        except URLError as exc:
            last_error = exc

        if attempt < RETRIES:
            log(f"{url} failed on attempt {attempt}/{RETRIES}: {last_error}; retrying in {RETRY_DELAY:g}s")
            time.sleep(RETRY_DELAY)

    raise ValidationError(f"{url} failed after {RETRIES} attempt(s): {last_error}") from last_error


def fetch_json(path_or_url: str, *, context_url: str | None = None) -> tuple[dict[str, Any], str, Any]:
    url = resolve_url(path_or_url, context_url=context_url)
    result = fetch(url)
    try:
        return json.loads(result.data.decode("utf-8")), url, result.headers
    except json.JSONDecodeError as exc:
        raise ValidationError(f"{url} is not valid JSON: {exc}") from exc


def require_missing(path_or_url: str) -> None:
    url = resolve_url(path_or_url)
    request = Request(url, headers={"User-Agent": "xcbenz-remote-validator/1.0"})
    try:
        with urlopen(request, timeout=TIMEOUT):
            pass
    except HTTPError as exc:
        if exc.code == 404:
            return
        raise ValidationError(f"{url} returned HTTP {exc.code}; expected HTTP 404") from exc
    except URLError as exc:
        raise ValidationError(f"{url} could not be checked for absence: {exc}") from exc
    raise ValidationError(f"{url} is still published; expected HTTP 404")


def choose_first(mapping: dict[str, Any], label: str) -> tuple[str, Any]:
    if not mapping:
        raise ValidationError(f"no entries for {label}")
    key = sorted(mapping.keys(), reverse=True)[0]
    return key, mapping[key]


def validate_models(manifest: dict[str, Any]) -> tuple[str, str, str]:
    models = manifest.get("models") or {}
    selected_model = ""
    selected_run = ""
    selected_location = ""
    for model_key in ("icon-ch1", "icon-ch2"):
        model = models.get(model_key) or {}
        runs = model.get("runs") or {}
        if not runs:
            raise ValidationError(f"manifest has no runs for {model_key}")
        latest = model.get("latest_run")
        if latest and latest not in runs:
            raise ValidationError(f"{model_key} latest_run {latest!r} is missing from runs")
        run_key = latest or sorted(runs.keys(), reverse=True)[0]
        run_entry = runs[run_key]
        locations = run_entry.get("locations") or {}
        if not locations:
            raise ValidationError(f"{model_key} {run_key} has no locations")
        location_id, location_entry = choose_first(locations, f"{model_key} {run_key} locations")
        if not location_entry.get("emagram_bundle"):
            raise ValidationError(f"{model_key} {run_key} {location_id} has no emagram_bundle")
        selected_model = selected_model or model_key
        selected_run = selected_run or run_key
        selected_location = selected_location or location_id
    return selected_model, selected_run, selected_location


def validate_bundle(manifest: dict[str, Any], model_key: str, run_key: str, location_id: str) -> None:
    location_entry = manifest["models"][model_key]["runs"][run_key]["locations"][location_id]
    bundle_path = location_entry["emagram_bundle"]
    bundle, bundle_url, _headers = fetch_json(bundle_path)
    if bundle.get("product") != "emagram_bundle":
        raise ValidationError(f"{bundle_url} product is not emagram_bundle")
    encoding = bundle.get("encoding") or {}
    variables = encoding.get("variables") or []
    step_count = int(encoding.get("step_count") or len(bundle.get("steps") or []))
    level_count = int(encoding.get("level_count") or len(bundle.get("height") or []))
    expected = step_count * len(variables) * level_count * 4
    declared = int(encoding.get("byte_length") or -1)
    if expected <= 0 or declared != expected:
        raise ValidationError(f"{bundle_url} byte length mismatch: expected={expected} declared={declared}")
    data_url = resolve_url(str(encoding.get("data") or "profiles.bin"), context_url=bundle_url)
    profile = fetch(data_url)
    content_length = profile.headers.get("Content-Length")
    actual = int(content_length) if content_length else len(profile.data)
    if actual != expected:
        raise ValidationError(f"{data_url} byte length is {actual}, expected {expected}")
    log(f"bundle OK: {model_key}/{run_key}/{location_id} bytes={actual}")


def validate_map_product(manifest: dict[str, Any], product: str) -> None:
    map_path = ((manifest.get("products") or {}).get("maps") or {}).get(product)
    if not map_path:
        raise ValidationError(f"manifest has no {product} map product")
    map_manifest, map_manifest_url, _headers = fetch_json(map_path)
    models = map_manifest.get("models") or {}
    model_key, model_entry = choose_first(models, f"{product} models")
    run_key, run_entry = choose_first(model_entry.get("runs") or {}, f"{product} runs")

    if product == "wind":
        level_key, product_entry = choose_first(run_entry.get("levels") or {}, "wind levels")
    else:
        level_key, product_entry = choose_first(run_entry.get("products") or {}, "sunshine products")

    metadata_path = product_entry.get("metadata")
    if not metadata_path:
        raise ValidationError(f"{product} {model_key}/{run_key}/{level_key} has no metadata path")
    metadata, metadata_url, _headers = fetch_json(metadata_path)
    steps = metadata.get("steps") or []
    if not steps:
        raise ValidationError(f"{metadata_url} has no steps")
    step = steps[0]
    step_url = resolve_url(str(step.get("url") or ""), context_url=metadata_url)
    if not step.get("url"):
        raise ValidationError(f"{metadata_url} first step has no url")
    expected = int(step.get("byte_length") or 0)
    payload = fetch(step_url)
    content_length = payload.headers.get("Content-Length")
    actual = int(content_length) if content_length else len(payload.data)
    if expected and actual != expected:
        raise ValidationError(f"{step_url} byte length is {actual}, expected {expected}")
    log(f"{product} OK: {model_key}/{run_key}/{level_key}/{step.get('step')} bytes={actual}")


def _require_cache_header(headers: Any, token: str, url: str) -> None:
    cache_control = str(headers.get("Cache-Control") or "").lower()
    if token not in cache_control:
        raise ValidationError(f"{url} Cache-Control lacks {token!r}: {cache_control!r}")


def validate_expected_value_tile_state(manifest: dict[str, Any], expected_state: str) -> None:
    if expected_state not in {"optional", "enabled", "disabled"}:
        raise ValidationError("EXPECTED_VALUE_TILES_STATE must be optional, enabled, or disabled")
    capability = ((manifest.get("capabilities") or {}).get("spatial_value_tiles"))
    if expected_state == "enabled" and not capability:
        raise ValidationError("spatial value tiles were expected but the capability is absent")
    if expected_state == "disabled" and capability:
        raise ValidationError("spatial value tiles were expected to be disabled but the capability is present")


def validate_value_tiles(manifest: dict[str, Any]) -> None:
    capability = ((manifest.get("capabilities") or {}).get("spatial_value_tiles"))
    if not capability:
        return
    if capability != capability_declaration():
        raise ValidationError("spatial value-tile capability differs from contract v1")
    tile_manifest, tile_manifest_url, tile_manifest_headers = fetch_json(str(capability["manifest"]))
    _require_cache_header(tile_manifest_headers, "no-cache", tile_manifest_url)
    if (
        tile_manifest.get("contract") != capability["contract"]
        or tile_manifest.get("contract_version") != capability["contract_version"]
        or tile_manifest.get("package") != capability["package"]
    ):
        raise ValidationError(f"{tile_manifest_url} contract declaration is invalid")
    model_key, model_entry = choose_first(tile_manifest.get("models") or {}, "value-tile models")
    run_key, run_entry = choose_first(model_entry.get("runs") or {}, "value-tile runs")
    revision, revision_url, _revision_headers = fetch_json(str(run_entry.get("revision_record") or ""))
    record = revision.get("record") or {}
    digest = sha256_bytes(canonical_json_bytes(record))
    if (
        run_entry.get("revision") != digest[:12]
        or run_entry.get("revision_sha256") != digest
        or revision.get("revision") != digest[:12]
        or revision.get("revision_sha256") != digest
    ):
        raise ValidationError(f"{revision_url} revision digest is invalid")
    variant_key, variant_entry = choose_first(run_entry.get("variants") or {}, "value-tile variants")
    metadata, metadata_url, metadata_headers = fetch_json(str(variant_entry.get("metadata") or ""))
    _require_cache_header(metadata_headers, "immutable", metadata_url)
    steps = metadata.get("steps") or []
    if not steps:
        raise ValidationError(f"{metadata_url} has no steps")
    step = steps[0]
    tile_matrix = metadata.get("tile_matrix") or {}
    template = str(tile_matrix.get("url_template") or "")
    if not template:
        raise ValidationError(f"{metadata_url} has no tile URL template")
    relative_tile = template.format(step=step["step"], tile_y=0, tile_x=0)
    tile_url = resolve_url(relative_tile, context_url=metadata_url)
    tile_result = fetch(tile_url)
    _require_cache_header(tile_result.headers, "immutable", tile_url)
    content_type = str(tile_result.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type not in {"application/octet-stream", "application/vnd.xcbenz.xvt"}:
        raise ValidationError(f"{tile_url} has unexpected Content-Type {content_type!r}")
    parse_xvt(tile_result.data)
    logical_path = f"{variant_key}/{step['step']}/t0_0.xvt"
    tile_record = next(
        (item for item in (record.get("tiles") or []) if item.get("logical_path") == logical_path),
        None,
    )
    if not tile_record:
        raise ValidationError(f"{revision_url} does not record {logical_path}")
    if (
        int(tile_record.get("byte_length") or -1) != len(tile_result.data)
        or tile_record.get("sha256") != sha256_bytes(tile_result.data)
    ):
        raise ValidationError(f"{tile_url} differs from its revision record")
    log(f"value tiles OK: {model_key}/{run_key}/{variant_key}/{step['step']} bytes={len(tile_result.data)}")


def main() -> int:
    try:
        manifest, manifest_url, headers = fetch_json("web_exports/manifest.json")
        fetch_json("web_exports/locations.json")
        if "models" not in manifest or "products" not in manifest:
            raise ValidationError(f"{manifest_url} does not look like a web_exports manifest")
        validate_expected_value_tile_state(manifest, EXPECTED_VALUE_TILES_STATE)
        if EXPECTED_VALUE_TILES_STATE == "disabled":
            require_missing(capability_declaration()["manifest"])
        cors = headers.get("Access-Control-Allow-Origin")
        if cors not in ("*", None):
            raise ValidationError(f"unexpected CORS header on manifest: {cors!r}")
        model_key, run_key, location_id = validate_models(manifest)
        validate_bundle(manifest, model_key, run_key, location_id)
        validate_map_product(manifest, "wind")
        validate_map_product(manifest, "sunshine")
        validate_map_product(manifest, "rain")
        validate_map_product(manifest, "sunrain")
        validate_value_tiles(manifest)
    except Exception as exc:  # noqa: BLE001 - CI smoke guard.
        return fail(str(exc))

    log(f"OK: {BASE_URL}web_exports/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
