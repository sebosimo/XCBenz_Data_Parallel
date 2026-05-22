"""Smoke-test a remote web_exports data host."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


BASE_URL = os.environ.get("DATA_BASE_URL", "https://data.xcbenz.com").rstrip("/") + "/"
TIMEOUT = float(os.environ.get("REMOTE_VALIDATE_TIMEOUT", "30"))
RETRIES = int(os.environ.get("REMOTE_VALIDATE_RETRIES", "3"))
RETRY_DELAY = float(os.environ.get("REMOTE_VALIDATE_RETRY_DELAY", "10"))


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


def main() -> int:
    try:
        manifest, manifest_url, headers = fetch_json("web_exports/manifest.json")
        fetch_json("web_exports/locations.json")
        if "models" not in manifest or "products" not in manifest:
            raise ValidationError(f"{manifest_url} does not look like a web_exports manifest")
        cors = headers.get("Access-Control-Allow-Origin")
        if cors not in ("*", None):
            raise ValidationError(f"unexpected CORS header on manifest: {cors!r}")
        model_key, run_key, location_id = validate_models(manifest)
        validate_bundle(manifest, model_key, run_key, location_id)
        validate_map_product(manifest, "wind")
        validate_map_product(manifest, "sunshine")
    except Exception as exc:  # noqa: BLE001 - CI smoke guard.
        return fail(str(exc))

    log(f"OK: {BASE_URL}web_exports/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
