"""Build browser-ready web_exports from direct profile chunks and split-binary map caches."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import shutil
import stat
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np
from web_profiles import (
    EMAGRAM_BUNDLE_VARIABLES,
    merge_profile_chunks,
)
from value_tiles import capability_declaration, generate_value_tiles, value_tiles_enabled


SCHEMA_VERSION = 1
WEB_DIR = Path(os.getenv("WEB_EXPORT_DIR", "web_exports"))
WEB_URL_PREFIX = os.getenv("WEB_EXPORT_URL_PREFIX", "web_exports").strip("/")
LOCATIONS_FILE = Path("locations.json")
SOURCE_MANIFEST_FILE = Path("manifest.json")
DEFAULT_DATA_ROOT = "https://raw.githubusercontent.com/sebosimo/XCBenz_Data/data"

MODELS = (
    {
        "key": "icon-ch1",
        "label": "ICON-CH1",
        "profile_chunk_dir": Path("web_profile_chunks") / "icon-ch1",
    },
    {
        "key": "icon-ch2",
        "label": "ICON-CH2",
        "profile_chunk_dir": Path("web_profile_chunks") / "icon-ch2",
    },
)

WIND_WEB_DEFAULT_LEVEL = "800m_AGL"
WIND_WEB_DEFAULT_GRID_STRIDE = 2
WIND_WEB_SCALE_FACTOR = 0.25
WIND_WEB_FILL_VALUE = -128
SUNSHINE_WEB_DIR = WEB_DIR / "sunshine_maps"
RAIN_WEB_DIR = WEB_DIR / "rain_maps"
SUNRAIN_WEB_DIR = WEB_DIR / "sunrain_maps"
CLOUD_WEB_DIR = WEB_DIR / "cloud_maps"
RADAR_MAP_PRODUCT = "radar"
WIND_WEB_STYLE = {
    "source": "XCBenz wind-map style v1",
    "map_bbox": [4.0, 43.0, 16.5, 48.8],
    "speed_units": "km/h",
    "source_speed_units": "kt",
    "bounds_kt": [0, 4, 6, 10, 14, 18, 22, 26, 30, 35, 40, 45, 50, 60, 70, 80, 90, 100],
    "display_bounds_kt": [0, 4, 6, 10, 14, 18, 22, 26, 30, 35, 40, 45, 50, 60, 70, 80, 90, 100, 120],
    "colors": [
        "#FFFFFF",
        "#F3F9E9",
        "#E4F1D1",
        "#C6E4A0",
        "#A8D770",
        "#FDEB1E",
        "#F6CD4C",
        "#F1B24B",
        "#EB954A",
        "#E6743A",
        "#E1002A",
        "#C8347D",
        "#A1438E",
        "#7A4C9F",
        "#5556AD",
        "#4669B9",
        "#7FA0E6",
        "#BFD0FF",
    ],
}


def log(message: str) -> None:
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} [web-export] {message}", flush=True)


def sanitize_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    clean = "".join(c for c in normalized if c.isalnum() or c in ("-", "_"))
    return clean if clean else "unnamed"


def horizon_sort_key(label: str) -> tuple[int, str]:
    match = re.search(r"(\d+)", label)
    if not match:
        return (10_000, label)
    return (int(match.group(1)), label)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def rel(path: Path) -> str:
    try:
        local_path = path.relative_to(WEB_DIR)
    except ValueError:
        return path.as_posix()
    if WEB_URL_PREFIX:
        return (Path(WEB_URL_PREFIX) / local_path).as_posix()
    return local_path.as_posix()


def ensure_clean_web_dir() -> None:
    if WEB_DIR.exists():
        if WEB_DIR.resolve().name not in {"web_exports", "web_exports_staging"}:
            raise RuntimeError(f"Refusing to delete unexpected export dir: {WEB_DIR}")
        shutil.rmtree(WEB_DIR, onerror=remove_readonly)
    WEB_DIR.mkdir(parents=True, exist_ok=True)


def remove_readonly(func: Any, path: str, _exc_info: Any) -> None:
    Path(path).chmod(stat.S_IWRITE)
    func(path)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def radar_map_manifest_path() -> Path:
    return WEB_DIR / "radar_maps" / "manifest.json"


def radar_map_manifest_url() -> str:
    return rel(radar_map_manifest_path())


def radar_map_layer_count() -> int:
    manifest = load_json(radar_map_manifest_path())
    layers = manifest.get("layers")
    return len(layers) if isinstance(layers, dict) else 0


def write_json(path: Path, payload: Any, *, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        if pretty:
            json.dump(payload, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write("\n")
        else:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def clean_number(value: Any, precision: int | None = None) -> float | int | bool | None:
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        if precision is not None:
            return round(number, precision)
        return number
    return None


def array_to_list(values: Any, precision: int | None = None) -> list[Any]:
    arr = np.asarray(values)
    if arr.ndim == 0:
        return [clean_number(arr.item(), precision)]
    return [clean_value(v, precision) for v in arr.tolist()]


def clean_value(value: Any, precision: int | None = None) -> Any:
    if isinstance(value, list):
        return [clean_value(item, precision) for item in value]
    if isinstance(value, tuple):
        return [clean_value(item, precision) for item in value]
    number = clean_number(value, precision)
    if number is not None or isinstance(value, (np.floating, float)):
        return number
    if isinstance(value, (str, type(None))):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (np.datetime64,)):
        return str(value)
    return str(value)


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def selected_wind_web_levels() -> set[str] | None:
    raw = os.getenv("WIND_WEB_LEVELS")
    if not raw:
        return {WIND_WEB_DEFAULT_LEVEL}
    if raw.strip().lower() == "all":
        return None
    levels = {item.strip() for item in raw.split(",") if item.strip()}
    return levels or {WIND_WEB_DEFAULT_LEVEL}


def normalize_step_label(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore").strip("\x00")
    return str(value)


def epoch_to_iso(value: Any) -> str:
    return dt.datetime.fromtimestamp(int(value), tz=dt.timezone.utc).isoformat()


def location_payload(location_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "location_id": location_id,
        "display_name": meta.get("display_name", location_id),
        "type": meta.get("type", "legacy"),
        "region_name": meta.get("region_name"),
        "lat": clean_number(meta.get("lat"), 6),
        "lon": clean_number(meta.get("lon"), 6),
    }


def expected_profile_chunks(model_key: str, run_tag: str) -> set[str]:
    if model_key == "icon-ch1":
        try:
            run_hour = int(run_tag.split("_", 1)[1][:2])
        except (IndexError, ValueError):
            run_hour = 3
        if run_hour == 3:
            return {"H000_H016", "H017_H033", "H034_H045"}
        return {"H000_H016", "H017_H033"}
    if model_key == "icon-ch2":
        return {"H000_H030", "H031_H060", "H061_H090", "H091_H120"}
    return set()


def scan_profile_chunks(root: Path, locations: dict[str, Any]) -> dict[str, dict[str, list[Path]]]:
    runs: dict[str, dict[str, list[Path]]] = {}
    if not root.exists():
        return runs
    saw_chunks = False

    for run_dir in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        expected_chunks = expected_profile_chunks(root.name, run_dir.name)
        found: dict[str, dict[str, Path]] = {}
        chunk_names: set[str] = set()
        for chunk_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
            for location_id in sorted(locations):
                chunk_path = chunk_dir / location_id / "chunk.json"
                if chunk_path.is_file():
                    saw_chunks = True
                    chunk_names.add(chunk_dir.name)
                    found.setdefault(location_id, {})[chunk_dir.name] = chunk_path
        run_locations: dict[str, list[Path]] = {}
        incomplete_count = 0
        missing_summaries: set[str] = set()
        for location_id, chunk_paths in found.items():
            missing_chunks = expected_chunks.difference(chunk_paths)
            if missing_chunks:
                incomplete_count += 1
                missing_summaries.add(",".join(sorted(missing_chunks)))
                continue
            run_locations[location_id] = [chunk_paths[name] for name in sorted(chunk_paths)]
        if found:
            log(
                f"direct profile chunk coverage for {root.name} {run_dir.name}: "
                f"chunks={sorted(chunk_names)} complete_locations={len(run_locations)} "
                f"incomplete_locations={incomplete_count}"
            )
            if missing_summaries:
                log(
                    f"WARN direct profile chunks incomplete for {root.name} {run_dir.name}: "
                    f"missing chunk sets={sorted(missing_summaries)}"
                )
        if run_locations:
            runs[run_dir.name] = run_locations
    if saw_chunks and expected_chunks and not runs:
        log(f"WARN found {root.name} direct profile chunks, but no run had all expected chunks; falling back")
    return runs


def scan_profile_chunks_with_fallback(root: Path, locations: dict[str, Any]) -> dict[str, dict[str, list[Path]]]:
    runs = scan_profile_chunks(root, locations)
    if runs or root.exists():
        return runs

    # GitHub's artifact download layout can vary depending on the upload root.
    # Accept a flattened ``icon-ch2/...`` tree as a defensive fallback.
    fallback_root = Path(root.name)
    if fallback_root == root or not fallback_root.exists():
        return runs

    log(f"WARN scanning flattened direct profile chunk layout at {fallback_root}")
    return scan_profile_chunks(fallback_root, locations)


def write_region_forecast(
    model_key: str,
    run_tag: str,
    location_id: str,
    location_meta: dict[str, Any],
    profile_exports: list[dict[str, Any]],
    thermal_export: dict[str, Any] | None,
) -> dict[str, Any]:
    path = WEB_DIR / "region_forecasts" / model_key / run_tag / f"{location_id}.json"
    steps = [item["step"] for item in profile_exports]
    valid_times = [item.get("valid_time") for item in profile_exports]
    payload = {
        "schema_version": SCHEMA_VERSION,
        "product": "region_forecast",
        "model": model_key,
        "run": run_tag,
        "location": location_payload(location_id, location_meta),
        "steps": steps,
        "valid_times": valid_times,
        "products": {
            "emagrams": {
                item["step"]: item["url"]
                for item in profile_exports
                if item.get("url")
            },
            "emagram_bundle": next((item["bundle_url"] for item in profile_exports if item.get("bundle_url")), None),
            "thermal_panel": thermal_export["url"] if thermal_export else None,
        },
        "summary": {
            "thermal": thermal_export.get("summary") if thermal_export else None,
        },
    }
    write_json(path, payload)
    return {"url": rel(path), "steps": steps, "valid_times": valid_times}


def wind_model_key(source_key: str) -> str:
    return "icon-ch1" if source_key == "ch1" else "icon-ch2"


def wind_axis_payload(values: np.ndarray, precision: int = 5) -> dict[str, Any]:
    axis = np.asarray(values, dtype=float)
    step = float(axis[1] - axis[0]) if len(axis) > 1 else 0.0
    return {
        "start": clean_number(axis[0], precision) if len(axis) else None,
        "end": clean_number(axis[-1], precision) if len(axis) else None,
        "step": clean_number(step, precision),
        "count": int(len(axis)),
        "values": array_to_list(axis, precision),
    }


def export_direct_wind_level(
    model_key: str,
    run_tag: str,
    level_name: str,
    source_metadata_path: Path,
) -> dict[str, Any]:
    source_metadata = load_json(source_metadata_path)
    if not source_metadata:
        raise FileNotFoundError(f"wind metadata missing: {source_metadata_path}")

    output_dir = WEB_DIR / "wind_maps" / model_key / run_tag / level_name
    steps_dir = output_dir / "steps"
    step_exports: list[dict[str, Any]] = []

    for source_step in source_metadata.get("steps") or []:
        step_label = str(source_step.get("step") or "")
        if not step_label:
            continue
        source_step_path = Path(str(source_step.get("path") or ""))
        if not source_step_path.exists():
            source_step_path = source_metadata_path.parent / "steps" / f"{step_label}.bin"
        if not source_step_path.exists():
            raise FileNotFoundError(f"wind step missing: {source_step_path}")

        step_path = steps_dir / f"{step_label}.bin"
        step_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_step_path, step_path)

        output_step = dict(source_step)
        output_step.pop("path", None)
        output_step.pop("url", None)
        output_step["url"] = rel(step_path)
        output_step["byte_length"] = int(step_path.stat().st_size)
        step_exports.append(output_step)

    metadata_path = output_dir / "metadata.json"
    payload = dict(source_metadata)
    payload["schema_version"] = SCHEMA_VERSION
    payload["product"] = "wind_map_level"
    payload["model"] = model_key
    payload["run"] = run_tag
    payload["source"] = rel(source_metadata_path)
    payload["steps"] = step_exports
    write_json(metadata_path, payload, pretty=True)

    level = payload.get("level") or {}
    grid = payload.get("grid") or {}
    return {
        "metadata": rel(metadata_path),
        "source": payload["source"],
        "level_type": level.get("type"),
        "level_h": level.get("height_m"),
        "grid": {
            "width": grid.get("width"),
            "height": grid.get("height"),
            "source_stride": grid.get("source_stride"),
        },
        "steps": step_exports,
        "step_count": len(step_exports),
        "bytes": sum(step["byte_length"] for step in step_exports),
    }


def export_wind_maps(source_manifest: dict[str, Any]) -> dict[str, Any] | None:
    source_wind = source_manifest.get("wind_maps") or {}
    if not source_wind:
        return None

    selected_levels = selected_wind_web_levels()
    wind_manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "product": "wind_maps",
        "default_level": WIND_WEB_DEFAULT_LEVEL,
        "level_filter": "all" if selected_levels is None else sorted(selected_levels),
        "models": {},
        "counts": {
            "runs": 0,
            "levels": 0,
            "steps": 0,
            "bytes": 0,
        },
    }

    for source_key, source_runs in source_wind.items():
        model_key = wind_model_key(source_key)
        model_manifest = {"runs": {}}

        for run_tag, run_entry in source_runs.items():
            run_manifest = {"layout": "split_binary_by_step", "levels": {}}
            for level_name, level_entry in (run_entry.get("levels") or {}).items():
                if selected_levels is not None and level_name not in selected_levels:
                    continue

                source_path = Path(level_entry.get("metadata", ""))
                if not source_path.exists():
                    log(f"WARN direct wind metadata missing for {model_key} {run_tag} {level_name}: {source_path}")
                    continue
                try:
                    exported_level = export_direct_wind_level(model_key, run_tag, level_name, source_path)
                except Exception as exc:
                    log(f"WARN direct wind export failed for {source_path}: {exc}")
                    continue

                run_manifest["levels"][level_name] = exported_level
                wind_manifest["counts"]["levels"] += 1
                wind_manifest["counts"]["steps"] += exported_level["step_count"]
                wind_manifest["counts"]["bytes"] += exported_level["bytes"]

            if run_manifest["levels"]:
                model_manifest["runs"][run_tag] = run_manifest
                wind_manifest["counts"]["runs"] += 1

        if model_manifest["runs"]:
            wind_manifest["models"][model_key] = model_manifest

    if not wind_manifest["models"]:
        return None

    manifest_path = WEB_DIR / "wind_maps" / "manifest.json"
    write_json(manifest_path, wind_manifest, pretty=True)
    wind_manifest["url"] = rel(manifest_path)
    return wind_manifest


def sunshine_model_key(source_key: str) -> str:
    return "icon-ch1" if source_key == "ch1" else "icon-ch2"


def export_sunshine_surface(
    model_key: str,
    run_tag: str,
    source_metadata_path: Path,
) -> dict[str, Any]:
    with source_metadata_path.open("r", encoding="utf-8") as f:
        source_metadata = json.load(f)

    output_dir = SUNSHINE_WEB_DIR / model_key / run_tag / "surface"
    steps_dir = output_dir / "steps"
    output_steps: list[dict[str, Any]] = []

    for source_step in source_metadata.get("steps") or []:
        source_path = Path(str(source_step.get("path", "")))
        if not source_path.exists():
            raise FileNotFoundError(f"sunshine step missing: {source_path}")
        step_label = str(source_step["step"])
        step_path = steps_dir / f"{step_label}.bin"
        step_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, step_path)
        output_step = dict(source_step)
        output_step.pop("path", None)
        output_step["url"] = rel(step_path)
        output_step["byte_length"] = int(step_path.stat().st_size)
        output_steps.append(output_step)

    metadata_path = output_dir / "metadata.json"
    metadata = dict(source_metadata)
    metadata["model"] = model_key
    metadata["run"] = run_tag
    metadata["source"] = rel(source_metadata_path)
    metadata["steps"] = output_steps
    write_json(metadata_path, metadata, pretty=True)

    return {
        "metadata": rel(metadata_path),
        "source": rel(source_metadata_path),
        "components": metadata.get("encoding", {}).get("components", []),
        "grid": {
            "width": metadata.get("grid", {}).get("width"),
            "height": metadata.get("grid", {}).get("height"),
        },
        "steps": output_steps,
        "step_count": len(output_steps),
        "bytes": sum(step["byte_length"] for step in output_steps),
    }


def export_sunshine_maps(source_manifest: dict[str, Any]) -> dict[str, Any] | None:
    source_sunshine = source_manifest.get("sunshine_maps") or {}
    if not source_sunshine:
        return None

    sunshine_manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "product": "sunshine_maps",
        "default_product": "surface",
        "models": {},
        "counts": {
            "runs": 0,
            "products": 0,
            "steps": 0,
            "bytes": 0,
        },
    }

    for source_key, source_runs in source_sunshine.items():
        model_key = sunshine_model_key(source_key)
        model_manifest = {"runs": {}}
        for run_tag, run_entry in source_runs.items():
            run_manifest = {"layout": "split_binary_by_step", "products": {}}
            for product_name, product_entry in (run_entry.get("products") or {}).items():
                source_metadata_path = Path(product_entry.get("metadata", ""))
                if not source_metadata_path.exists():
                    log(f"WARN sunshine source metadata missing for {model_key} {run_tag}: {source_metadata_path}")
                    continue
                try:
                    exported_product = export_sunshine_surface(model_key, run_tag, source_metadata_path)
                except Exception as exc:
                    log(f"WARN sunshine export failed for {source_metadata_path}: {exc}")
                    continue
                run_manifest["products"][product_name] = exported_product
                sunshine_manifest["counts"]["products"] += 1
                sunshine_manifest["counts"]["steps"] += exported_product["step_count"]
                sunshine_manifest["counts"]["bytes"] += exported_product["bytes"]

            if run_manifest["products"]:
                model_manifest["runs"][run_tag] = run_manifest
                sunshine_manifest["counts"]["runs"] += 1

        if model_manifest["runs"]:
            sunshine_manifest["models"][model_key] = model_manifest

    if not sunshine_manifest["models"]:
        return None

    manifest_path = SUNSHINE_WEB_DIR / "manifest.json"
    write_json(manifest_path, sunshine_manifest, pretty=True)
    sunshine_manifest["url"] = rel(manifest_path)
    return sunshine_manifest


def rain_model_key(source_key: str) -> str:
    return "icon-ch1" if source_key == "ch1" else "icon-ch2"


def export_rain_surface(
    model_key: str,
    run_tag: str,
    source_metadata_path: Path,
) -> dict[str, Any]:
    with source_metadata_path.open("r", encoding="utf-8") as f:
        source_metadata = json.load(f)

    output_dir = RAIN_WEB_DIR / model_key / run_tag / "surface"
    steps_dir = output_dir / "steps"
    output_steps: list[dict[str, Any]] = []

    for source_step in source_metadata.get("steps") or []:
        source_path = Path(str(source_step.get("path", "")))
        if not source_path.exists():
            raise FileNotFoundError(f"rain step missing: {source_path}")
        step_label = str(source_step["step"])
        step_path = steps_dir / f"{step_label}.bin"
        step_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, step_path)
        output_step = dict(source_step)
        output_step.pop("path", None)
        output_step["url"] = rel(step_path)
        output_step["byte_length"] = int(step_path.stat().st_size)
        output_steps.append(output_step)

    metadata_path = output_dir / "metadata.json"
    metadata = dict(source_metadata)
    metadata["model"] = model_key
    metadata["run"] = run_tag
    metadata["source"] = rel(source_metadata_path)
    metadata["steps"] = output_steps
    write_json(metadata_path, metadata, pretty=True)

    return {
        "metadata": rel(metadata_path),
        "source": rel(source_metadata_path),
        "components": metadata.get("encoding", {}).get("components", []),
        "grid": {
            "width": metadata.get("grid", {}).get("width"),
            "height": metadata.get("grid", {}).get("height"),
        },
        "steps": output_steps,
        "step_count": len(output_steps),
        "bytes": sum(step["byte_length"] for step in output_steps),
    }


def export_rain_maps(source_manifest: dict[str, Any]) -> dict[str, Any] | None:
    source_rain = source_manifest.get("rain_maps") or {}
    if not source_rain:
        return None

    rain_manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "product": "rain_maps",
        "default_product": "surface",
        "models": {},
        "counts": {
            "runs": 0,
            "products": 0,
            "steps": 0,
            "bytes": 0,
        },
    }

    for source_key, source_runs in source_rain.items():
        model_key = rain_model_key(source_key)
        model_manifest = {"runs": {}}
        for run_tag, run_entry in source_runs.items():
            run_manifest = {"layout": "split_binary_by_step", "products": {}}
            for product_name, product_entry in (run_entry.get("products") or {}).items():
                source_metadata_path = Path(product_entry.get("metadata", ""))
                if not source_metadata_path.exists():
                    log(f"WARN rain source metadata missing for {model_key} {run_tag}: {source_metadata_path}")
                    continue
                try:
                    exported_product = export_rain_surface(model_key, run_tag, source_metadata_path)
                except Exception as exc:
                    log(f"WARN rain export failed for {source_metadata_path}: {exc}")
                    continue
                run_manifest["products"][product_name] = exported_product
                rain_manifest["counts"]["products"] += 1
                rain_manifest["counts"]["steps"] += exported_product["step_count"]
                rain_manifest["counts"]["bytes"] += exported_product["bytes"]

            if run_manifest["products"]:
                model_manifest["runs"][run_tag] = run_manifest
                rain_manifest["counts"]["runs"] += 1

        if model_manifest["runs"]:
            rain_manifest["models"][model_key] = model_manifest

    if not rain_manifest["models"]:
        return None

    manifest_path = RAIN_WEB_DIR / "manifest.json"
    write_json(manifest_path, rain_manifest, pretty=True)
    rain_manifest["url"] = rel(manifest_path)
    return rain_manifest


def sunrain_model_key(source_key: str) -> str:
    return "icon-ch1" if source_key == "ch1" else "icon-ch2"


def export_sunrain_surface(
    model_key: str,
    run_tag: str,
    source_metadata_path: Path,
) -> dict[str, Any]:
    with source_metadata_path.open("r", encoding="utf-8") as f:
        source_metadata = json.load(f)

    output_dir = SUNRAIN_WEB_DIR / model_key / run_tag / "surface"
    steps_dir = output_dir / "steps"
    output_steps: list[dict[str, Any]] = []

    for source_step in source_metadata.get("steps") or []:
        source_path = Path(str(source_step.get("path", "")))
        if not source_path.exists():
            raise FileNotFoundError(f"Sun+Rain step missing: {source_path}")
        step_label = str(source_step["step"])
        step_path = steps_dir / f"{step_label}.bin"
        step_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, step_path)
        output_step = dict(source_step)
        output_step.pop("path", None)
        output_step["url"] = rel(step_path)
        output_step["byte_length"] = int(step_path.stat().st_size)
        output_steps.append(output_step)

    metadata_path = output_dir / "metadata.json"
    metadata = dict(source_metadata)
    metadata["model"] = model_key
    metadata["run"] = run_tag
    metadata["source"] = rel(source_metadata_path)
    metadata["steps"] = output_steps
    write_json(metadata_path, metadata, pretty=True)

    return {
        "metadata": rel(metadata_path),
        "source": rel(source_metadata_path),
        "components": metadata.get("encoding", {}).get("components", []),
        "grid": {
            "width": metadata.get("grid", {}).get("width"),
            "height": metadata.get("grid", {}).get("height"),
        },
        "steps": output_steps,
        "step_count": len(output_steps),
        "bytes": sum(step["byte_length"] for step in output_steps),
    }


def export_sunrain_maps(source_manifest: dict[str, Any]) -> dict[str, Any] | None:
    source_sunrain = source_manifest.get("sunrain_maps") or {}
    if not source_sunrain:
        return None

    sunrain_manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "product": "sunrain_maps",
        "default_product": "surface",
        "models": {},
        "counts": {
            "runs": 0,
            "products": 0,
            "steps": 0,
            "bytes": 0,
        },
    }

    for source_key, source_runs in source_sunrain.items():
        model_key = sunrain_model_key(source_key)
        model_manifest = {"runs": {}}
        for run_tag, run_entry in source_runs.items():
            run_manifest = {"layout": "split_binary_by_step", "products": {}}
            for product_name, product_entry in (run_entry.get("products") or {}).items():
                source_metadata_path = Path(product_entry.get("metadata", ""))
                if not source_metadata_path.exists():
                    log(f"WARN Sun+Rain source metadata missing for {model_key} {run_tag}: {source_metadata_path}")
                    continue
                try:
                    exported_product = export_sunrain_surface(model_key, run_tag, source_metadata_path)
                except Exception as exc:
                    log(f"WARN Sun+Rain export failed for {source_metadata_path}: {exc}")
                    continue
                run_manifest["products"][product_name] = exported_product
                sunrain_manifest["counts"]["products"] += 1
                sunrain_manifest["counts"]["steps"] += exported_product["step_count"]
                sunrain_manifest["counts"]["bytes"] += exported_product["bytes"]

            if run_manifest["products"]:
                model_manifest["runs"][run_tag] = run_manifest
                sunrain_manifest["counts"]["runs"] += 1

        if model_manifest["runs"]:
            sunrain_manifest["models"][model_key] = model_manifest

    if not sunrain_manifest["models"]:
        return None

    manifest_path = SUNRAIN_WEB_DIR / "manifest.json"
    write_json(manifest_path, sunrain_manifest, pretty=True)
    sunrain_manifest["url"] = rel(manifest_path)
    return sunrain_manifest


def cloud_model_key(source_key: str) -> str:
    return "icon-ch1" if source_key == "ch1" else "icon-ch2"


def export_cloud_layer(
    model_key: str,
    run_tag: str,
    product_name: str,
    source_metadata_path: Path,
) -> dict[str, Any]:
    with source_metadata_path.open("r", encoding="utf-8") as f:
        source_metadata = json.load(f)

    output_dir = CLOUD_WEB_DIR / model_key / run_tag / product_name
    steps_dir = output_dir / "steps"
    output_steps: list[dict[str, Any]] = []

    for source_step in source_metadata.get("steps") or []:
        source_path = Path(str(source_step.get("path", "")))
        if not source_path.exists():
            raise FileNotFoundError(f"cloud step missing: {source_path}")
        step_label = str(source_step["step"])
        step_path = steps_dir / f"{step_label}.bin"
        step_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, step_path)
        output_step = dict(source_step)
        output_step.pop("path", None)
        output_step["url"] = rel(step_path)
        output_step["byte_length"] = int(step_path.stat().st_size)
        output_steps.append(output_step)

    metadata_path = output_dir / "metadata.json"
    metadata = dict(source_metadata)
    metadata["model"] = model_key
    metadata["run"] = run_tag
    metadata["source"] = rel(source_metadata_path)
    metadata["steps"] = output_steps
    write_json(metadata_path, metadata, pretty=True)

    return {
        "metadata": rel(metadata_path),
        "source": rel(source_metadata_path),
        "components": metadata.get("encoding", {}).get("components", []),
        "grid": {
            "width": metadata.get("grid", {}).get("width"),
            "height": metadata.get("grid", {}).get("height"),
        },
        "steps": output_steps,
        "step_count": len(output_steps),
        "bytes": sum(step["byte_length"] for step in output_steps),
    }


def export_cloud_maps(source_manifest: dict[str, Any]) -> dict[str, Any] | None:
    source_cloud = source_manifest.get("cloud_maps") or {}
    if not source_cloud:
        return None

    cloud_manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "product": "cloud_maps",
        "default_product": "total",
        "models": {},
        "counts": {
            "runs": 0,
            "products": 0,
            "steps": 0,
            "bytes": 0,
        },
    }

    for source_key, source_runs in source_cloud.items():
        model_key = cloud_model_key(source_key)
        model_manifest = {"runs": {}}
        for run_tag, run_entry in source_runs.items():
            run_manifest = {"layout": "split_binary_by_step", "products": {}}
            for product_name, product_entry in (run_entry.get("products") or {}).items():
                source_metadata_path = Path(product_entry.get("metadata", ""))
                if not source_metadata_path.exists():
                    log(f"WARN cloud source metadata missing for {model_key} {run_tag}: {source_metadata_path}")
                    continue
                try:
                    exported_product = export_cloud_layer(
                        model_key,
                        run_tag,
                        product_name,
                        source_metadata_path,
                    )
                except Exception as exc:
                    log(f"WARN cloud export failed for {source_metadata_path}: {exc}")
                    continue
                run_manifest["products"][product_name] = exported_product
                cloud_manifest["counts"]["products"] += 1
                cloud_manifest["counts"]["steps"] += exported_product["step_count"]
                cloud_manifest["counts"]["bytes"] += exported_product["bytes"]

            if run_manifest["products"]:
                model_manifest["runs"][run_tag] = run_manifest
                cloud_manifest["counts"]["runs"] += 1

        if model_manifest["runs"]:
            cloud_manifest["models"][model_key] = model_manifest

    if not cloud_manifest["models"]:
        return None

    manifest_path = CLOUD_WEB_DIR / "manifest.json"
    write_json(manifest_path, cloud_manifest, pretty=True)
    cloud_manifest["url"] = rel(manifest_path)
    return cloud_manifest


def export_value_tiles_capability(manifest: dict[str, Any]) -> dict[str, Any] | None:
    if not value_tiles_enabled():
        return None
    tile_manifest = generate_value_tiles(WEB_DIR)
    manifest.setdefault("capabilities", {})["spatial_value_tiles"] = capability_declaration()
    log(
        "Generated validated spatial value tiles: "
        f"{tile_manifest['counts']['runs']} run(s), "
        f"{tile_manifest['counts']['variants']} variant(s), "
        f"{tile_manifest['counts']['tiles']} tile(s)"
    )
    return tile_manifest


def export_model(model: dict[str, Any], locations: dict[str, Any]) -> dict[str, Any]:
    model_key = model["key"]
    profile_chunk_dir = model.get("profile_chunk_dir")
    scanned_runs = (
        scan_profile_chunks_with_fallback(profile_chunk_dir, locations)
        if isinstance(profile_chunk_dir, Path)
        else {}
    )
    model_manifest: dict[str, Any] = {
        "label": model["label"],
        "profile_chunk_dir": str(profile_chunk_dir) if profile_chunk_dir else None,
        "profile_source": "direct_chunks",
        "latest_run": max(scanned_runs.keys()) if scanned_runs else None,
        "runs": {},
        "counts": {
            "runs": len(scanned_runs),
            "locations": 0,
            "profiles": 0,
            "emagram_bundles": 0,
            "thermal_panels": 0,
            "region_forecasts": 0,
        },
    }

    seen_locations: set[str] = set()

    for run_tag, run_locations in scanned_runs.items():
        run_manifest = {"locations": {}}
        for location_id, source_entry in run_locations.items():
            location_meta = locations[location_id]
            try:
                profile_exports = merge_profile_chunks(
                    source_entry,
                    output_dir=WEB_DIR / "emagrams" / model_key / run_tag / location_id,
                    model_key=model_key,
                    run_tag=run_tag,
                    location_id=location_id,
                    location_meta=location_meta,
                    url_for=rel,
                    write_json=write_json,
                )
            except Exception as exc:
                log(f"WARN profile chunk merge failed for {model_key} {run_tag} {location_id}: {exc}")
                profile_exports = []

            region_forecast = write_region_forecast(
                model_key,
                run_tag,
                location_id,
                location_meta,
                profile_exports,
                None,
            )

            seen_locations.add(location_id)
            model_manifest["counts"]["profiles"] += len(profile_exports)
            model_manifest["counts"]["region_forecasts"] += 1
            bundle_url = next((item["bundle_url"] for item in profile_exports if item.get("bundle_url")), None)
            if bundle_url:
                model_manifest["counts"]["emagram_bundles"] += 1
            run_manifest["locations"][location_id] = {
                "type": location_meta.get("type", "legacy"),
                "display_name": location_meta.get("display_name", location_id),
                "steps": region_forecast["steps"],
                "valid_times": region_forecast["valid_times"],
                "region_forecast": region_forecast["url"],
                "thermal_panel": None,
                "emagram_template": None if bundle_url else rel(WEB_DIR / "emagrams" / model_key / run_tag / location_id / "{step}.json"),
                "emagram_bundle": bundle_url,
            }

        model_manifest["runs"][run_tag] = run_manifest

    model_manifest["counts"]["locations"] = len(seen_locations)
    return model_manifest


def validate_manifest(manifest: dict[str, Any]) -> None:
    if not (WEB_DIR / "manifest.json").exists():
        raise RuntimeError("web_exports/manifest.json was not written")
    if not (WEB_DIR / "locations.json").exists():
        raise RuntimeError("web_exports/locations.json was not written")


def main() -> None:
    if not LOCATIONS_FILE.exists():
        raise FileNotFoundError("locations.json is required for web export generation")

    locations = load_json(LOCATIONS_FILE)
    source_manifest = load_json(SOURCE_MANIFEST_FILE)
    ensure_clean_web_dir()
    write_json(WEB_DIR / "locations.json", locations, pretty=True)

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now_iso(),
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
                "wind": None,
                "sunshine": None,
                "rain": None,
                "sunrain": None,
                "cloud": None,
                RADAR_MAP_PRODUCT: radar_map_manifest_url(),
            },
        },
        "models": {},
        "counts": {
            "locations": len(locations),
            "region_locations": sum(1 for item in locations.values() if item.get("type") == "region"),
            "legacy_locations": sum(1 for item in locations.values() if item.get("type") == "legacy"),
            "profiles": 0,
            "emagram_bundles": 0,
            "thermal_panels": 0,
            "region_forecasts": 0,
            "wind_map_levels": 0,
            "wind_map_steps": 0,
            "sunshine_map_products": 0,
            "sunshine_map_steps": 0,
            "rain_map_products": 0,
            "rain_map_steps": 0,
            "sunrain_map_products": 0,
            "sunrain_map_steps": 0,
            "cloud_map_products": 0,
            "cloud_map_steps": 0,
            "radar_map_layers": radar_map_layer_count(),
        },
        "notes": [
            "Generated from direct browser-ready pipeline outputs.",
            "Emagram profiles are bundled per location/run as bundle.json plus float32 little-endian profiles.bin.",
            "Wind map exports are split into browser-readable metadata JSON plus lazy-loaded int8 binary u/v slices.",
            "Sunshine map exports are browser-readable metadata JSON plus lazy-loaded uint8 binary sunshine-fraction slices.",
            "Rain map exports are browser-readable metadata JSON plus lazy-loaded uint8 binary precipitation slices.",
            "Sun+Rain map exports are browser-readable metadata JSON plus lazy-loaded uint8 semantic sunshine/rain slices.",
            "Cloud map exports are browser-readable metadata JSON plus lazy-loaded packed uint4 cloud-cover slices.",
            "Radar map exports are live-owned browser-readable metadata JSON plus lazy-loaded uint8 rain-rate slices.",
        ],
    }

    for model in MODELS:
        model_manifest = export_model(model, locations)
        manifest["models"][model["key"]] = model_manifest
        manifest["counts"]["profiles"] += model_manifest["counts"]["profiles"]
        manifest["counts"]["emagram_bundles"] += model_manifest["counts"]["emagram_bundles"]
        manifest["counts"]["thermal_panels"] += model_manifest["counts"]["thermal_panels"]
        manifest["counts"]["region_forecasts"] += model_manifest["counts"]["region_forecasts"]

    wind_manifest = export_wind_maps(source_manifest)
    if wind_manifest:
        manifest["products"]["maps"]["wind"] = wind_manifest["url"]
        manifest["counts"]["wind_map_levels"] = wind_manifest["counts"]["levels"]
        manifest["counts"]["wind_map_steps"] = wind_manifest["counts"]["steps"]
    else:
        manifest["products"]["maps"]["wind"] = None

    sunshine_manifest = export_sunshine_maps(source_manifest)
    if sunshine_manifest:
        manifest["products"]["maps"]["sunshine"] = sunshine_manifest["url"]
        manifest["counts"]["sunshine_map_products"] = sunshine_manifest["counts"]["products"]
        manifest["counts"]["sunshine_map_steps"] = sunshine_manifest["counts"]["steps"]
    else:
        manifest["products"]["maps"]["sunshine"] = None

    rain_manifest = export_rain_maps(source_manifest)
    if rain_manifest:
        manifest["products"]["maps"]["rain"] = rain_manifest["url"]
        manifest["counts"]["rain_map_products"] = rain_manifest["counts"]["products"]
        manifest["counts"]["rain_map_steps"] = rain_manifest["counts"]["steps"]
    else:
        manifest["products"]["maps"]["rain"] = None

    sunrain_manifest = export_sunrain_maps(source_manifest)
    if sunrain_manifest:
        manifest["products"]["maps"]["sunrain"] = sunrain_manifest["url"]
        manifest["counts"]["sunrain_map_products"] = sunrain_manifest["counts"]["products"]
        manifest["counts"]["sunrain_map_steps"] = sunrain_manifest["counts"]["steps"]
    else:
        manifest["products"]["maps"]["sunrain"] = None

    cloud_manifest = export_cloud_maps(source_manifest)
    if cloud_manifest:
        manifest["products"]["maps"]["cloud"] = cloud_manifest["url"]
        manifest["counts"]["cloud_map_products"] = cloud_manifest["counts"]["products"]
        manifest["counts"]["cloud_map_steps"] = cloud_manifest["counts"]["steps"]
    else:
        manifest["products"]["maps"]["cloud"] = None

    export_value_tiles_capability(manifest)

    write_json(WEB_DIR / "manifest.json", manifest, pretty=True)
    validate_manifest(manifest)
    log(
        "Wrote web_exports: "
        f"{manifest['counts']['profiles']} profiles, "
        f"{manifest['counts']['thermal_panels']} thermal panels, "
        f"{manifest['counts']['region_forecasts']} region forecasts, "
        f"{manifest['counts']['wind_map_steps']} wind map steps, "
        f"{manifest['counts']['sunshine_map_steps']} sunshine map steps, "
        f"{manifest['counts']['rain_map_steps']} rain map steps, "
        f"{manifest['counts']['sunrain_map_steps']} Sun+Rain map steps, "
        f"{manifest['counts']['cloud_map_steps']} cloud map steps, "
        f"{manifest['counts']['radar_map_layers']} radar map layers"
    )


if __name__ == "__main__":
    main()
