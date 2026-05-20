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


def validate_map_encodings() -> tuple[int, int]:
    wind_metadata = list(Path("web_exports/wind_maps").glob("*/*/*/metadata.json"))
    sunshine_metadata = list(Path("web_exports/sunshine_maps").glob("*/*/*/metadata.json"))
    if not wind_metadata:
        raise ValueError("no wind map metadata found")
    if not sunshine_metadata:
        raise ValueError("no sunshine map metadata found")

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
    return len(wind_metadata), len(sunshine_metadata)


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
    for product in ("wind", "sunshine"):
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
        wind_metadata_count, sunshine_metadata_count = validate_map_encodings()
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
        f"wind_steps={counts.get('wind_map_steps')}, "
        f"sunshine_steps={counts.get('sunshine_map_steps')}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
