"""Validate publish-ready data outputs before pushing the data branch."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


WEB_MANIFEST = Path("web_exports/manifest.json")
ROOT_MANIFEST = Path("manifest.json")
EMAGRAM_BUNDLE_VARIABLES = (
    "p",
    "t",
    "qv",
    "u",
    "v",
    "temperature_c",
    "pressure_hpa",
    "dewpoint_c",
    "wind_speed_ms",
    "wind_dir_deg",
)


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing required file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def fail(message: str) -> int:
    print(f"[validate] ERROR: {message}", flush=True)
    return 1


def resolve_web_url(url: str | None) -> Path | None:
    if not url:
        return None
    path = Path(url)
    if path.parts and path.parts[0] == "web_exports":
        return path
    return path


def validate_bundles() -> tuple[int, int]:
    bundles = list(Path("web_exports/emagrams").glob("*/*/*/bundle.json"))
    if not bundles:
        raise ValueError("no emagram bundles found")

    total_bytes = 0
    for bundle_path in bundles:
        bundle = load_json(bundle_path)
        encoding = bundle.get("encoding") or {}
        variables = encoding.get("variables") or []
        if bundle.get("product") != "emagram_bundle":
            raise ValueError(f"{bundle_path} product is not emagram_bundle")
        if encoding.get("dtype") != "float32":
            raise ValueError(f"{bundle_path} dtype is {encoding.get('dtype')!r}")
        if encoding.get("format") != "float32-le-step-variable-level":
            raise ValueError(f"{bundle_path} format is {encoding.get('format')!r}")
        if tuple(variables) != EMAGRAM_BUNDLE_VARIABLES:
            raise ValueError(f"{bundle_path} variables are unexpected")
        expected = int(encoding.get("step_count") or 0) * len(variables) * int(encoding.get("level_count") or 0) * 4
        data_path = resolve_web_url(encoding.get("data"))
        if data_path is None or not data_path.exists():
            raise FileNotFoundError(f"{bundle_path} profile binary is missing: {encoding.get('data')!r}")
        actual = data_path.stat().st_size
        declared = int(encoding.get("byte_length") or -1)
        if actual != expected or declared != actual:
            raise ValueError(
                f"{bundle_path} byte length mismatch: expected={expected} declared={declared} actual={actual}"
            )
        total_bytes += actual
    return len(bundles), total_bytes


def validate_map_encodings() -> tuple[int, int, int, int, int]:
    wind_metadata = list(Path("web_exports/wind_maps").glob("*/*/*/metadata.json"))
    sunshine_metadata = list(Path("web_exports/sunshine_maps").glob("*/*/*/metadata.json"))
    rain_metadata = list(Path("web_exports/rain_maps").glob("*/*/*/metadata.json"))
    sunrain_metadata = list(Path("web_exports/sunrain_maps").glob("*/*/*/metadata.json"))
    cloud_metadata = list(Path("web_exports/cloud_maps").glob("*/*/*/metadata.json"))
    if not wind_metadata:
        raise ValueError("no wind map metadata found")
    if not sunshine_metadata:
        raise ValueError("no sunshine map metadata found")
    if not rain_metadata:
        raise ValueError("no rain map metadata found")
    if not sunrain_metadata:
        raise ValueError("no Sun+Rain map metadata found")
    if not cloud_metadata:
        raise ValueError("no cloud map metadata found")

    for metadata_path in wind_metadata:
        encoding = load_json(metadata_path).get("encoding") or {}
        if encoding.get("dtype") != "int8" or encoding.get("format") != "int8-interleaved-u-v":
            raise ValueError(f"{metadata_path} has unexpected wind encoding: {encoding}")
        if encoding.get("missing_value") != -128:
            raise ValueError(f"{metadata_path} has unexpected wind missing value")
    for metadata_path in sunshine_metadata:
        encoding = load_json(metadata_path).get("encoding") or {}
        if encoding.get("dtype") != "uint8" or encoding.get("format") != "uint8-interleaved-components":
            raise ValueError(f"{metadata_path} has unexpected sunshine encoding: {encoding}")
        if encoding.get("components") != ["sunshine_fraction_pct"]:
            raise ValueError(f"{metadata_path} has unexpected sunshine components")
        if encoding.get("missing_value") != 255:
            raise ValueError(f"{metadata_path} has unexpected sunshine missing value")
    for metadata_path in rain_metadata:
        payload = load_json(metadata_path)
        encoding = payload.get("encoding") or {}
        if encoding.get("dtype") != "uint8" or encoding.get("format") != "uint8-interleaved-components":
            raise ValueError(f"{metadata_path} has unexpected rain encoding: {encoding}")
        if encoding.get("components") != ["precipitation_mm"]:
            raise ValueError(f"{metadata_path} has unexpected rain components")
        if encoding.get("units") != ["mm"]:
            raise ValueError(f"{metadata_path} has unexpected rain units")
        if encoding.get("missing_value") != 255:
            raise ValueError(f"{metadata_path} has unexpected rain missing value")
        for step in payload.get("steps") or []:
            step_url = step.get("url")
            if not step_url or not Path(step_url).exists():
                raise FileNotFoundError(f"{metadata_path} references missing rain step {step_url!r}")
            actual = Path(step_url).stat().st_size
            declared = int(step.get("byte_length") or -1)
            if actual != declared:
                raise ValueError(f"{metadata_path} rain step byte length mismatch: declared={declared} actual={actual}")
    for metadata_path in sunrain_metadata:
        payload = load_json(metadata_path)
        encoding = payload.get("encoding") or {}
        if encoding.get("dtype") != "uint8" or encoding.get("format") != "uint8-semantic-sunrain-code":
            raise ValueError(f"{metadata_path} has unexpected Sun+Rain encoding: {encoding}")
        if encoding.get("components") != ["sunrain_code"]:
            raise ValueError(f"{metadata_path} has unexpected Sun+Rain components")
        if encoding.get("units") != ["code"]:
            raise ValueError(f"{metadata_path} has unexpected Sun+Rain units")
        if encoding.get("missing_value") != 0:
            raise ValueError(f"{metadata_path} has unexpected Sun+Rain missing value")
        if encoding.get("reserved_values") != [251, 252, 253, 254, 255]:
            raise ValueError(f"{metadata_path} has unexpected Sun+Rain reserved values")
        grid = payload.get("grid") or {}
        expected_length = int(grid.get("width") or 0) * int(grid.get("height") or 0)
        if expected_length <= 0:
            raise ValueError(f"{metadata_path} has invalid Sun+Rain grid dimensions")
        for step in payload.get("steps") or []:
            step_url = step.get("url")
            if not step_url or not Path(step_url).exists():
                raise FileNotFoundError(f"{metadata_path} references missing Sun+Rain step {step_url!r}")
            step_path = Path(step_url)
            data = step_path.read_bytes()
            declared = int(step.get("byte_length") or -1)
            actual = len(data)
            if actual != declared or actual != expected_length:
                raise ValueError(
                    f"{metadata_path} Sun+Rain step byte length mismatch: "
                    f"expected={expected_length} declared={declared} actual={actual}"
                )
            if any(byte >= 251 for byte in data):
                raise ValueError(f"{metadata_path} Sun+Rain step uses reserved values")
    for metadata_path in cloud_metadata:
        payload = load_json(metadata_path)
        encoding = payload.get("encoding") or {}
        if encoding.get("dtype") != "uint8" or encoding.get("format") != "packed-uint4-cloud-cover":
            raise ValueError(f"{metadata_path} has unexpected cloud encoding: {encoding}")
        if encoding.get("components") != ["cloud_cover_pct"]:
            raise ValueError(f"{metadata_path} has unexpected cloud components")
        if encoding.get("units") != ["%"]:
            raise ValueError(f"{metadata_path} has unexpected cloud units")
        if encoding.get("bits_per_value") != 4:
            raise ValueError(f"{metadata_path} has unexpected cloud bits_per_value")
        if encoding.get("quantization_step_pct") != 10:
            raise ValueError(f"{metadata_path} has unexpected cloud quantization step")
        if encoding.get("missing_code") != 15:
            raise ValueError(f"{metadata_path} has unexpected cloud missing code")
        grid = payload.get("grid") or {}
        cell_count = int(grid.get("width") or 0) * int(grid.get("height") or 0)
        if cell_count <= 0:
            raise ValueError(f"{metadata_path} has invalid cloud grid dimensions")
        expected_length = (cell_count + 1) // 2
        for step in payload.get("steps") or []:
            step_url = step.get("url")
            if not step_url or not Path(step_url).exists():
                raise FileNotFoundError(f"{metadata_path} references missing cloud step {step_url!r}")
            step_path = Path(step_url)
            data = step_path.read_bytes()
            declared = int(step.get("byte_length") or -1)
            actual = len(data)
            if actual != declared or actual != expected_length:
                raise ValueError(
                    f"{metadata_path} cloud step byte length mismatch: "
                    f"expected={expected_length} declared={declared} actual={actual}"
                )
            low = [byte & 0x0F for byte in data]
            high = [byte >> 4 for byte in data]
            codes = []
            for idx in range(cell_count):
                codes.append(low[idx // 2] if idx % 2 == 0 else high[idx // 2])
            invalid = [code for code in codes if not (0 <= code <= 10 or code == 15)]
            if invalid:
                raise ValueError(f"{metadata_path} cloud step uses reserved code {invalid[0]}")
            if cell_count % 2 == 1 and high[-1] != 15:
                raise ValueError(f"{metadata_path} cloud odd-cell padding nibble is not missing code 15")
    return len(wind_metadata), len(sunshine_metadata), len(rain_metadata), len(sunrain_metadata), len(cloud_metadata)


def main() -> int:
    try:
        root = load_json(ROOT_MANIFEST)
        web = load_json(WEB_MANIFEST)
    except Exception as exc:  # noqa: BLE001 - this is a CI guard.
        return fail(str(exc))

    expected_root = os.getenv("EXPECTED_WEB_EXPORT_DATA_ROOT")
    actual_root = (web.get("source") or {}).get("data_root")
    if expected_root and actual_root != expected_root:
        return fail(f"web export data_root is {actual_root!r}, expected {expected_root!r}")

    counts = web.get("counts") or {}
    if int(counts.get("profiles") or 0) <= 0:
        return fail("web manifest has no profiles")
    if int(counts.get("emagram_bundles") or 0) <= 0:
        return fail("web manifest has no emagram bundles")
    if int(counts.get("region_forecasts") or 0) <= 0:
        return fail("web manifest has no region forecasts")

    models = web.get("models") or {}
    for model_key in ("icon-ch1", "icon-ch2"):
        model = models.get(model_key) or {}
        runs = model.get("runs") or {}
        if not runs:
            return fail(f"web manifest has no runs for {model_key}")
        if len(runs) > 4:
            return fail(f"web manifest has {len(runs)} runs for {model_key}; retention should keep at most 4")
        latest = model.get("latest_run")
        if latest and latest not in runs:
            return fail(f"{model_key} latest_run {latest!r} is absent from runs")
        for run_tag, run_entry in runs.items():
            for location_id, location_entry in (run_entry.get("locations") or {}).items():
                bundle_url = location_entry.get("emagram_bundle")
                template = location_entry.get("emagram_template")
                if not bundle_url and not template:
                    return fail(f"{model_key} {run_tag} {location_id} has neither emagram_bundle nor emagram_template")
                if bundle_url and not Path(bundle_url).exists():
                    return fail(f"{model_key} {run_tag} {location_id} bundle path missing: {bundle_url}")

    map_products = ((web.get("products") or {}).get("maps") or {})
    for product in ("wind", "sunshine", "rain", "sunrain", "cloud"):
        path = map_products.get(product)
        if not path:
            return fail(f"web manifest has no {product} map product")
        if not Path(path).exists():
            return fail(f"{product} map manifest path does not exist: {path}")

    root_runs = (root.get("runs") or {}, root.get("runs_ch2") or {})
    modern_web_runs = tuple(
        (models.get(model_key) or {}).get("runs") or {}
        for model_key in ("icon-ch1", "icon-ch2")
    )
    if not any(root_runs) and not all(modern_web_runs):
        return fail("neither root manifest nor web manifest exposes CH1 and CH2 runs")

    try:
        bundle_count, bundle_bytes = validate_bundles()
        (
            wind_metadata_count,
            sunshine_metadata_count,
            rain_metadata_count,
            sunrain_metadata_count,
            cloud_metadata_count,
        ) = validate_map_encodings()
    except Exception as exc:  # noqa: BLE001 - this is a CI guard.
        return fail(str(exc))

    nc_files = list(Path("web_exports").rglob("*.nc"))
    if nc_files:
        return fail(f"web_exports contains NetCDF files; first is {nc_files[0]}")

    print(
        "[validate] OK: "
        f"profiles={counts.get('profiles')}, "
        f"bundles={bundle_count}, "
        f"bundle_bytes={bundle_bytes}, "
        f"region_forecasts={counts.get('region_forecasts')}, "
        f"wind_metadata={wind_metadata_count}, "
        f"sunshine_metadata={sunshine_metadata_count}, "
        f"rain_metadata={rain_metadata_count}, "
        f"sunrain_metadata={sunrain_metadata_count}, "
        f"cloud_metadata={cloud_metadata_count}, "
        f"wind_steps={counts.get('wind_map_steps')}, "
        f"sunshine_steps={counts.get('sunshine_map_steps')}, "
        f"rain_steps={counts.get('rain_map_steps')}, "
        f"sunrain_steps={counts.get('sunrain_map_steps')}, "
        f"cloud_steps={counts.get('cloud_map_steps')}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
