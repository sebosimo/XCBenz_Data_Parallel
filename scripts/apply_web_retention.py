"""Merge staged web exports, prune retained runs, and rebuild web manifests."""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
from pathlib import Path
from typing import Any


RUN_FORMAT = "%Y%m%d_%H%M"
WEB_DIR = Path(os.getenv("WEB_EXPORT_DIR", "web_exports"))
STAGING_DIR = Path(os.getenv("WEB_EXPORT_STAGING_DIR", "web_exports_staging"))
DEFAULT_DATA_ROOT = "https://raw.githubusercontent.com/sebosimo/XCBenz_Data/data"
MODEL_LABELS = {
    "icon-ch1": "ICON-CH1",
    "icon-ch2": "ICON-CH2",
}
ANCHOR_HOURS = {
    "icon-ch1": 3,
    "icon-ch2": 0,
}
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


def log(message: str) -> None:
    print(f"[web-retention] {message}", flush=True)


def parse_run_tag(run_tag: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(run_tag, RUN_FORMAT).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def kept_run_tags(run_tags: list[str], *, anchor_hour: int, now: dt.datetime) -> set[str]:
    keep_dates = {now.date(), (now - dt.timedelta(days=1)).date()}
    sorted_tags = sorted(run_tags, reverse=True)
    keep = set(sorted_tags[:2])

    for run_tag in sorted_tags:
        run_dt = parse_run_tag(run_tag)
        if run_dt is None:
            continue
        if run_dt.hour == anchor_hour and run_dt.minute == 0 and run_dt.date() in keep_dates:
            keep.add(run_tag)
    return keep


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any, *, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        if pretty:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        else:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def web_path(path: Path) -> str:
    return path.as_posix()


def resolve_web_url(url: str | None) -> Path | None:
    if not url:
        return None
    path = Path(url)
    if path.parts and path.parts[0] == WEB_DIR.name:
        return WEB_DIR.joinpath(*path.parts[1:])
    return path


def merge_staging() -> None:
    if not STAGING_DIR.exists():
        raise FileNotFoundError(f"Missing staged web exports: {STAGING_DIR}")
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    for item in STAGING_DIR.iterdir():
        destination = WEB_DIR / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)
    log(f"Merged {STAGING_DIR.as_posix()} into {WEB_DIR.as_posix()}")


def collect_model_runs(model_key: str) -> set[str]:
    runs: set[str] = set()
    for root in (
        WEB_DIR / "region_forecasts" / model_key,
        WEB_DIR / "emagrams" / model_key,
        WEB_DIR / "thermal_panels" / model_key,
        WEB_DIR / "wind_maps" / model_key,
        WEB_DIR / "sunshine_maps" / model_key,
        WEB_DIR / "rain_maps" / model_key,
        WEB_DIR / "sunrain_maps" / model_key,
        WEB_DIR / "cloud_maps" / model_key,
    ):
        if not root.exists():
            continue
        runs.update(path.name for path in root.iterdir() if path.is_dir() and parse_run_tag(path.name))
    return runs


def prune_model_runs(model_key: str, keep: set[str]) -> None:
    removed = 0
    for root in (
        WEB_DIR / "region_forecasts" / model_key,
        WEB_DIR / "emagrams" / model_key,
        WEB_DIR / "thermal_panels" / model_key,
        WEB_DIR / "wind_maps" / model_key,
        WEB_DIR / "sunshine_maps" / model_key,
        WEB_DIR / "rain_maps" / model_key,
        WEB_DIR / "sunrain_maps" / model_key,
        WEB_DIR / "cloud_maps" / model_key,
    ):
        if not root.exists():
            continue
        for run_dir in root.iterdir():
            if not run_dir.is_dir() or parse_run_tag(run_dir.name) is None:
                continue
            if run_dir.name in keep:
                continue
            shutil.rmtree(run_dir)
            removed += 1
            log(f"Removed {run_dir.as_posix()}")
    log(f"{model_key}: kept {len(keep)} run(s), removed {removed} product run directorie(s)")


def apply_retention() -> None:
    now = dt.datetime.now(dt.timezone.utc)
    for model_key, anchor_hour in ANCHOR_HOURS.items():
        runs = sorted(collect_model_runs(model_key), reverse=True)
        keep = kept_run_tags(runs, anchor_hour=anchor_hour, now=now)
        prune_model_runs(model_key, keep)


def bundle_payloads() -> list[tuple[Path, dict[str, Any]]]:
    payloads: list[tuple[Path, dict[str, Any]]] = []
    for bundle_path in WEB_DIR.glob("emagrams/*/*/*/bundle.json"):
        payloads.append((bundle_path, load_json(bundle_path)))
    return payloads


def validate_emagram_bundles() -> int:
    count = 0
    for bundle_path, payload in bundle_payloads():
        encoding = payload.get("encoding") or {}
        variables = encoding.get("variables") or []
        if payload.get("product") != "emagram_bundle":
            raise ValueError(f"{bundle_path} is not an emagram bundle")
        if encoding.get("dtype") != "float32":
            raise ValueError(f"{bundle_path} has unexpected dtype {encoding.get('dtype')!r}")
        if encoding.get("format") != "float32-le-step-variable-level":
            raise ValueError(f"{bundle_path} has unexpected format {encoding.get('format')!r}")
        if tuple(variables) != EMAGRAM_BUNDLE_VARIABLES:
            raise ValueError(f"{bundle_path} has unexpected variables {variables!r}")

        step_count = int(encoding.get("step_count") or 0)
        level_count = int(encoding.get("level_count") or 0)
        expected = step_count * len(variables) * level_count * 4
        data_path = resolve_web_url(encoding.get("data"))
        if data_path is None or not data_path.exists():
            raise FileNotFoundError(f"{bundle_path} references missing profile data {encoding.get('data')!r}")
        actual = data_path.stat().st_size
        declared = int(encoding.get("byte_length") or -1)
        if actual != expected or declared != actual:
            raise ValueError(
                f"{bundle_path} byte length mismatch: expected={expected} declared={declared} actual={actual}"
            )
        count += 1
    if count == 0:
        raise ValueError("No emagram bundles found after web retention")
    log(f"Validated {count} emagram bundle(s)")
    return count


def rebuild_wind_manifest() -> dict[str, Any] | None:
    root = WEB_DIR / "wind_maps"
    if not root.exists():
        return None

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "product": "wind_maps",
        "default_level": "800m_AGL",
        "grid_stride": None,
        "level_filter": [],
        "models": {},
        "counts": {"runs": 0, "levels": 0, "steps": 0, "bytes": 0},
    }
    levels_seen: set[str] = set()

    for model_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        model_manifest = {"runs": {}}
        for run_dir in sorted((path for path in model_dir.iterdir() if path.is_dir()), reverse=True):
            run_manifest = {"layout": "split_binary_by_step", "levels": {}}
            for level_dir in sorted(path for path in run_dir.iterdir() if path.is_dir()):
                metadata_path = level_dir / "metadata.json"
                if not metadata_path.exists():
                    continue
                metadata = load_json(metadata_path)
                steps = metadata.get("steps") or []
                byte_count = sum(int(step.get("byte_length") or 0) for step in steps)
                grid = metadata.get("grid") or {}
                if manifest["grid_stride"] is None:
                    manifest["grid_stride"] = grid.get("source_stride")
                levels_seen.add(level_dir.name)
                run_manifest["levels"][level_dir.name] = {
                    "metadata": web_path(metadata_path),
                    "source": metadata.get("source"),
                    "level_type": (metadata.get("level") or {}).get("type"),
                    "level_h": (metadata.get("level") or {}).get("height_m"),
                    "grid": {
                        "width": grid.get("width"),
                        "height": grid.get("height"),
                        "source_stride": grid.get("source_stride"),
                    },
                    "steps": steps,
                    "step_count": len(steps),
                    "bytes": byte_count,
                }
                manifest["counts"]["levels"] += 1
                manifest["counts"]["steps"] += len(steps)
                manifest["counts"]["bytes"] += byte_count
            if run_manifest["levels"]:
                model_manifest["runs"][run_dir.name] = run_manifest
                manifest["counts"]["runs"] += 1
        if model_manifest["runs"]:
            manifest["models"][model_dir.name] = model_manifest

    if not manifest["models"]:
        return None
    manifest["grid_stride"] = manifest["grid_stride"] or 2
    manifest["level_filter"] = sorted(levels_seen)
    write_json(root / "manifest.json", manifest)
    return manifest


def rebuild_sunshine_manifest() -> dict[str, Any] | None:
    root = WEB_DIR / "sunshine_maps"
    if not root.exists():
        return None

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "product": "sunshine_maps",
        "default_product": "surface",
        "models": {},
        "counts": {"runs": 0, "products": 0, "steps": 0, "bytes": 0},
    }

    for model_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        model_manifest = {"runs": {}}
        for run_dir in sorted((path for path in model_dir.iterdir() if path.is_dir()), reverse=True):
            run_manifest = {"layout": "split_binary_by_step", "products": {}}
            for product_dir in sorted(path for path in run_dir.iterdir() if path.is_dir()):
                metadata_path = product_dir / "metadata.json"
                if not metadata_path.exists():
                    continue
                metadata = load_json(metadata_path)
                steps = metadata.get("steps") or []
                byte_count = sum(int(step.get("byte_length") or 0) for step in steps)
                grid = metadata.get("grid") or {}
                run_manifest["products"][product_dir.name] = {
                    "metadata": web_path(metadata_path),
                    "source": metadata.get("source"),
                    "components": (metadata.get("encoding") or {}).get("components", []),
                    "grid": {
                        "width": grid.get("width"),
                        "height": grid.get("height"),
                    },
                    "steps": steps,
                    "step_count": len(steps),
                    "bytes": byte_count,
                }
                manifest["counts"]["products"] += 1
                manifest["counts"]["steps"] += len(steps)
                manifest["counts"]["bytes"] += byte_count
            if run_manifest["products"]:
                model_manifest["runs"][run_dir.name] = run_manifest
                manifest["counts"]["runs"] += 1
        if model_manifest["runs"]:
            manifest["models"][model_dir.name] = model_manifest

    if not manifest["models"]:
        return None
    write_json(root / "manifest.json", manifest)
    return manifest


def rebuild_rain_manifest() -> dict[str, Any] | None:
    root = WEB_DIR / "rain_maps"
    if not root.exists():
        return None

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "product": "rain_maps",
        "default_product": "surface",
        "models": {},
        "counts": {"runs": 0, "products": 0, "steps": 0, "bytes": 0},
    }

    for model_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        model_manifest = {"runs": {}}
        for run_dir in sorted((path for path in model_dir.iterdir() if path.is_dir()), reverse=True):
            run_manifest = {"layout": "split_binary_by_step", "products": {}}
            for product_dir in sorted(path for path in run_dir.iterdir() if path.is_dir()):
                metadata_path = product_dir / "metadata.json"
                if not metadata_path.exists():
                    continue
                metadata = load_json(metadata_path)
                steps = metadata.get("steps") or []
                byte_count = sum(int(step.get("byte_length") or 0) for step in steps)
                grid = metadata.get("grid") or {}
                run_manifest["products"][product_dir.name] = {
                    "metadata": web_path(metadata_path),
                    "source": metadata.get("source"),
                    "components": (metadata.get("encoding") or {}).get("components", []),
                    "grid": {
                        "width": grid.get("width"),
                        "height": grid.get("height"),
                    },
                    "steps": steps,
                    "step_count": len(steps),
                    "bytes": byte_count,
                }
                manifest["counts"]["products"] += 1
                manifest["counts"]["steps"] += len(steps)
                manifest["counts"]["bytes"] += byte_count
            if run_manifest["products"]:
                model_manifest["runs"][run_dir.name] = run_manifest
                manifest["counts"]["runs"] += 1
        if model_manifest["runs"]:
            manifest["models"][model_dir.name] = model_manifest

    if not manifest["models"]:
        return None
    write_json(root / "manifest.json", manifest)
    return manifest


def rebuild_sunrain_manifest() -> dict[str, Any] | None:
    root = WEB_DIR / "sunrain_maps"
    if not root.exists():
        return None

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "product": "sunrain_maps",
        "default_product": "surface",
        "models": {},
        "counts": {"runs": 0, "products": 0, "steps": 0, "bytes": 0},
    }

    for model_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        model_manifest = {"runs": {}}
        for run_dir in sorted((path for path in model_dir.iterdir() if path.is_dir()), reverse=True):
            run_manifest = {"layout": "split_binary_by_step", "products": {}}
            for product_dir in sorted(path for path in run_dir.iterdir() if path.is_dir()):
                metadata_path = product_dir / "metadata.json"
                if not metadata_path.exists():
                    continue
                metadata = load_json(metadata_path)
                steps = metadata.get("steps") or []
                byte_count = sum(int(step.get("byte_length") or 0) for step in steps)
                grid = metadata.get("grid") or {}
                run_manifest["products"][product_dir.name] = {
                    "metadata": web_path(metadata_path),
                    "source": metadata.get("source"),
                    "components": (metadata.get("encoding") or {}).get("components", []),
                    "grid": {
                        "width": grid.get("width"),
                        "height": grid.get("height"),
                    },
                    "steps": steps,
                    "step_count": len(steps),
                    "bytes": byte_count,
                }
                manifest["counts"]["products"] += 1
                manifest["counts"]["steps"] += len(steps)
                manifest["counts"]["bytes"] += byte_count
            if run_manifest["products"]:
                model_manifest["runs"][run_dir.name] = run_manifest
                manifest["counts"]["runs"] += 1
        if model_manifest["runs"]:
            manifest["models"][model_dir.name] = model_manifest

    if not manifest["models"]:
        return None
    write_json(root / "manifest.json", manifest)
    return manifest


def rebuild_cloud_manifest() -> dict[str, Any] | None:
    root = WEB_DIR / "cloud_maps"
    if not root.exists():
        return None

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "product": "cloud_maps",
        "default_product": "total",
        "models": {},
        "counts": {"runs": 0, "products": 0, "steps": 0, "bytes": 0},
    }

    for model_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        model_manifest = {"runs": {}}
        for run_dir in sorted((path for path in model_dir.iterdir() if path.is_dir()), reverse=True):
            run_manifest = {"layout": "split_binary_by_step", "products": {}}
            for product_dir in sorted(path for path in run_dir.iterdir() if path.is_dir()):
                metadata_path = product_dir / "metadata.json"
                if not metadata_path.exists():
                    continue
                metadata = load_json(metadata_path)
                steps = metadata.get("steps") or []
                byte_count = sum(int(step.get("byte_length") or 0) for step in steps)
                grid = metadata.get("grid") or {}
                run_manifest["products"][product_dir.name] = {
                    "metadata": web_path(metadata_path),
                    "source": metadata.get("source"),
                    "components": (metadata.get("encoding") or {}).get("components", []),
                    "grid": {
                        "width": grid.get("width"),
                        "height": grid.get("height"),
                    },
                    "steps": steps,
                    "step_count": len(steps),
                    "bytes": byte_count,
                }
                manifest["counts"]["products"] += 1
                manifest["counts"]["steps"] += len(steps)
                manifest["counts"]["bytes"] += byte_count
            if run_manifest["products"]:
                model_manifest["runs"][run_dir.name] = run_manifest
                manifest["counts"]["runs"] += 1
        if model_manifest["runs"]:
            manifest["models"][model_dir.name] = model_manifest

    if not manifest["models"]:
        return None
    write_json(root / "manifest.json", manifest)
    return manifest


def rebuild_main_manifest(
    bundle_count: int,
    wind_manifest: dict[str, Any] | None,
    sunshine_manifest: dict[str, Any] | None,
    rain_manifest: dict[str, Any] | None,
    sunrain_manifest: dict[str, Any] | None,
    cloud_manifest: dict[str, Any] | None,
) -> None:
    locations_path = WEB_DIR / "locations.json"
    if not locations_path.exists():
        source_locations = Path("locations.json")
        if not source_locations.exists():
            raise FileNotFoundError("Missing locations.json for web manifest rebuild")
        shutil.copy2(source_locations, locations_path)
    locations = load_json(locations_path)
    source_manifest = load_json(Path("manifest.json")) if Path("manifest.json").exists() else {}

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "source": {
            "source_manifest": "manifest.json",
            "source_manifest_generated_at": source_manifest.get("generated_at"),
            "data_root": os.getenv("WEB_EXPORT_DATA_ROOT", DEFAULT_DATA_ROOT),
        },
        "urls": {
            "locations": "web_exports/locations.json",
            "regions": None,
        },
        "products": {
            "region_forecasts": "web_exports/region_forecasts/{model}/{run}/{location_id}.json",
            "emagrams": None,
            "emagram_bundles": "web_exports/emagrams/{model}/{run}/{location_id}/bundle.json",
            "thermal_panels": "web_exports/thermal_panels/{model}/{run}/{location_id}.json",
            "maps": {
                "wind": "web_exports/wind_maps/manifest.json" if wind_manifest else None,
                "sunshine": "web_exports/sunshine_maps/manifest.json" if sunshine_manifest else None,
                "rain": "web_exports/rain_maps/manifest.json" if rain_manifest else None,
                "sunrain": "web_exports/sunrain_maps/manifest.json" if sunrain_manifest else None,
                "cloud": "web_exports/cloud_maps/manifest.json" if cloud_manifest else None,
            },
        },
        "models": {},
        "counts": {
            "locations": len(locations),
            "region_locations": sum(1 for item in locations.values() if item.get("type") == "region"),
            "legacy_locations": sum(1 for item in locations.values() if item.get("type") == "legacy"),
            "profiles": 0,
            "emagram_bundles": bundle_count,
            "thermal_panels": 0,
            "region_forecasts": 0,
            "wind_map_levels": (wind_manifest or {}).get("counts", {}).get("levels", 0),
            "wind_map_steps": (wind_manifest or {}).get("counts", {}).get("steps", 0),
            "sunshine_map_products": (sunshine_manifest or {}).get("counts", {}).get("products", 0),
            "sunshine_map_steps": (sunshine_manifest or {}).get("counts", {}).get("steps", 0),
            "rain_map_products": (rain_manifest or {}).get("counts", {}).get("products", 0),
            "rain_map_steps": (rain_manifest or {}).get("counts", {}).get("steps", 0),
            "sunrain_map_products": (sunrain_manifest or {}).get("counts", {}).get("products", 0),
            "sunrain_map_steps": (sunrain_manifest or {}).get("counts", {}).get("steps", 0),
            "cloud_map_products": (cloud_manifest or {}).get("counts", {}).get("products", 0),
            "cloud_map_steps": (cloud_manifest or {}).get("counts", {}).get("steps", 0),
        },
        "notes": [
            "Generated from retained browser-ready web_exports.",
            "Emagram profiles are bundled per location/run as bundle.json plus float32 little-endian profiles.bin.",
            "Wind map exports are split into browser-readable metadata JSON plus lazy-loaded int8 binary u/v slices.",
            "Sunshine map exports are browser-readable metadata JSON plus lazy-loaded uint8 binary sunshine-fraction slices.",
            "Rain map exports are browser-readable metadata JSON plus lazy-loaded uint8 binary precipitation slices.",
            "Sun+Rain map exports are browser-readable metadata JSON plus lazy-loaded uint8 semantic sunshine/rain slices.",
            "Cloud map exports are browser-readable metadata JSON plus lazy-loaded packed uint4 cloud-cover slices.",
        ],
    }

    for model_key, label in MODEL_LABELS.items():
        model_manifest: dict[str, Any] = {
            "label": label,
            "profile_source": "web_exports",
            "latest_run": None,
            "runs": {},
            "counts": {
                "runs": 0,
                "locations": 0,
                "profiles": 0,
                "thermal_panels": 0,
                "region_forecasts": 0,
                "emagram_bundles": 0,
            },
        }
        seen_locations: set[str] = set()
        model_root = WEB_DIR / "region_forecasts" / model_key
        if model_root.exists():
            for run_dir in sorted((path for path in model_root.iterdir() if path.is_dir()), reverse=True):
                run_manifest = {"locations": {}}
                for forecast_path in sorted(run_dir.glob("*.json")):
                    forecast = load_json(forecast_path)
                    location_id = forecast_path.stem
                    location = forecast.get("location") or locations.get(location_id) or {}
                    products = forecast.get("products") or {}
                    steps = forecast.get("steps") or []
                    bundle_url = products.get("emagram_bundle")
                    thermal_url = products.get("thermal_panel")
                    run_manifest["locations"][location_id] = {
                        "type": location.get("type", locations.get(location_id, {}).get("type", "legacy")),
                        "display_name": location.get(
                            "display_name",
                            locations.get(location_id, {}).get("display_name", location_id),
                        ),
                        "steps": steps,
                        "valid_times": forecast.get("valid_times") or [],
                        "region_forecast": web_path(forecast_path),
                        "thermal_panel": thermal_url,
                        "emagram_template": (
                            None
                            if bundle_url
                            else f"web_exports/emagrams/{model_key}/{run_dir.name}/{location_id}/{{step}}.json"
                        ),
                        "emagram_bundle": bundle_url,
                    }
                    seen_locations.add(location_id)
                    model_manifest["counts"]["profiles"] += len(steps)
                    model_manifest["counts"]["region_forecasts"] += 1
                    if thermal_url:
                        model_manifest["counts"]["thermal_panels"] += 1
                    if bundle_url:
                        model_manifest["counts"]["emagram_bundles"] += 1
                if run_manifest["locations"]:
                    model_manifest["runs"][run_dir.name] = run_manifest

        model_manifest["latest_run"] = max(model_manifest["runs"].keys()) if model_manifest["runs"] else None
        model_manifest["counts"]["runs"] = len(model_manifest["runs"])
        model_manifest["counts"]["locations"] = len(seen_locations)
        manifest["models"][model_key] = model_manifest
        manifest["counts"]["profiles"] += model_manifest["counts"]["profiles"]
        manifest["counts"]["thermal_panels"] += model_manifest["counts"]["thermal_panels"]
        manifest["counts"]["region_forecasts"] += model_manifest["counts"]["region_forecasts"]

    write_json(WEB_DIR / "manifest.json", manifest)
    log(
        "Rebuilt web manifest: "
        f"profiles={manifest['counts']['profiles']}, "
        f"bundles={manifest['counts']['emagram_bundles']}, "
        f"region_forecasts={manifest['counts']['region_forecasts']}, "
        f"wind_steps={manifest['counts']['wind_map_steps']}, "
        f"sunshine_steps={manifest['counts']['sunshine_map_steps']}, "
        f"rain_steps={manifest['counts']['rain_map_steps']}, "
        f"sunrain_steps={manifest['counts']['sunrain_map_steps']}, "
        f"cloud_steps={manifest['counts']['cloud_map_steps']}"
    )


def main() -> None:
    merge_staging()
    apply_retention()
    bundle_count = validate_emagram_bundles()
    wind_manifest = rebuild_wind_manifest()
    sunshine_manifest = rebuild_sunshine_manifest()
    rain_manifest = rebuild_rain_manifest()
    sunrain_manifest = rebuild_sunrain_manifest()
    cloud_manifest = rebuild_cloud_manifest()
    rebuild_main_manifest(bundle_count, wind_manifest, sunshine_manifest, rain_manifest, sunrain_manifest, cloud_manifest)


if __name__ == "__main__":
    main()
