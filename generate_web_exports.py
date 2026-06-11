"""
generate_web_exports.py

Build browser-friendly JSON exports from the existing generated NetCDF files.
This is intentionally additive: Streamlit keeps reading cache_data*/ and the
root manifest.json, while the web app can read web_exports/.
"""

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
import xarray as xr

from web_profiles import (
    EMAGRAM_BUNDLE_VARIABLES,
    build_bundle_step_values,
    merge_profile_chunks,
    write_emagram_bundle,
)


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
        "cache_dir": Path("cache_data"),
        "packed_cache_dir": Path("cache_data_packed"),
        "profile_chunk_dir": Path("web_profile_chunks") / "icon-ch1",
        "horizon_digits": 2,
    },
    {
        "key": "icon-ch2",
        "label": "ICON-CH2",
        "cache_dir": Path("cache_data_ch2"),
        "packed_cache_dir": Path("cache_data_ch2_packed"),
        "profile_chunk_dir": Path("web_profile_chunks") / "icon-ch2",
        "horizon_digits": 3,
    },
)

PROFILE_VARIABLES = ("HEIGHT", "P", "T", "QV", "U", "V")
RADIATION_VARIABLES = ("ASWDIR_S", "ASWDIFD_S")
WIND_WEB_DEFAULT_LEVEL = "800m_AGL"
WIND_WEB_DEFAULT_GRID_STRIDE = 2
WIND_WEB_SCALE_FACTOR = 0.25
WIND_WEB_FILL_VALUE = -128
SUNSHINE_WEB_DIR = WEB_DIR / "sunshine_maps"
RAIN_WEB_DIR = WEB_DIR / "rain_maps"
SUNRAIN_WEB_DIR = WEB_DIR / "sunrain_maps"
CLOUD_WEB_DIR = WEB_DIR / "cloud_maps"
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


def scalar_value(ds: xr.Dataset, name: str, precision: int | None = None) -> Any:
    if name not in ds:
        return None
    values = np.asarray(ds[name].values)
    if values.ndim != 0:
        return None
    return clean_number(values.item(), precision)


def vector_values(ds: xr.Dataset, name: str, precision: int | None = None) -> list[Any] | None:
    if name not in ds:
        return None
    values = np.asarray(ds[name].values)
    if values.ndim == 0:
        return None
    return array_to_list(values, precision)


def pressure_to_hpa(pressure_pa: np.ndarray) -> np.ndarray:
    return pressure_pa.astype(float) / 100.0


def temperature_to_celsius(temperature_k: np.ndarray) -> np.ndarray:
    return temperature_k.astype(float) - 273.15


def dewpoint_from_specific_humidity(pressure_pa: np.ndarray, q_kgkg: np.ndarray) -> np.ndarray:
    q = np.maximum(q_kgkg.astype(float), 1e-9)
    vapor_pressure_hpa = (q * pressure_pa.astype(float) / (0.622 + 0.378 * q)) / 100.0
    vapor_pressure_hpa = np.maximum(vapor_pressure_hpa, 1e-6)
    gamma = np.log(vapor_pressure_hpa / 6.112)
    return (243.5 * gamma) / (17.67 - gamma)


def wind_speed(u_ms: np.ndarray, v_ms: np.ndarray) -> np.ndarray:
    return np.sqrt(np.square(u_ms.astype(float)) + np.square(v_ms.astype(float)))


def wind_direction_from(u_ms: np.ndarray, v_ms: np.ndarray) -> np.ndarray:
    return (270.0 - np.degrees(np.arctan2(v_ms.astype(float), u_ms.astype(float)))) % 360.0


def location_payload(location_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "location_id": location_id,
        "display_name": meta.get("display_name", location_id),
        "type": meta.get("type", "legacy"),
        "region_name": meta.get("region_name"),
        "lat": clean_number(meta.get("lat"), 6),
        "lon": clean_number(meta.get("lon"), 6),
    }


def scan_profiles(cache_dir: Path, locations: dict[str, Any]) -> dict[str, dict[str, list[Path]]]:
    runs: dict[str, dict[str, list[Path]]] = {}
    if not cache_dir.exists():
        return runs

    for run_dir in sorted((p for p in cache_dir.iterdir() if p.is_dir()), reverse=True):
        run_locations: dict[str, list[Path]] = {}
        for location_id in sorted(locations):
            candidates = [run_dir / location_id]
            sanitized = sanitize_name(location_id)
            if sanitized != location_id:
                candidates.append(run_dir / sanitized)
            loc_dir = next((p for p in candidates if p.is_dir()), None)
            if not loc_dir:
                continue
            step_files = sorted(
                (p for p in loc_dir.glob("H*.nc") if p.is_file()),
                key=lambda p: horizon_sort_key(p.stem),
            )
            if step_files:
                run_locations[location_id] = step_files
        if run_locations:
            runs[run_dir.name] = run_locations
    return runs


def scan_packed_profiles(cache_dir: Path, locations: dict[str, Any]) -> dict[str, dict[str, Path]]:
    runs: dict[str, dict[str, Path]] = {}
    if not cache_dir.exists():
        return runs

    for run_dir in sorted((p for p in cache_dir.iterdir() if p.is_dir()), reverse=True):
        run_locations: dict[str, Path] = {}
        for location_id in sorted(locations):
            candidates = [run_dir / f"{location_id}.nc"]
            sanitized = sanitize_name(location_id)
            if sanitized != location_id:
                candidates.append(run_dir / f"{sanitized}.nc")
            packed_path = next((p for p in candidates if p.is_file()), None)
            if packed_path:
                run_locations[location_id] = packed_path
        if run_locations:
            runs[run_dir.name] = run_locations
    return runs


def scan_profile_chunks(root: Path, locations: dict[str, Any]) -> dict[str, dict[str, list[Path]]]:
    runs: dict[str, dict[str, list[Path]]] = {}
    if not root.exists():
        return runs
    expected_chunks = set()
    if root.name == "icon-ch1":
        expected_chunks = {"H000_H016", "H017_H045"}
    elif root.name == "icon-ch2":
        expected_chunks = {"H000_H030", "H031_H060", "H061_H090", "H091_H120"}
    saw_chunks = False

    for run_dir in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
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


def export_profile(
    model_key: str,
    run_tag: str,
    location_id: str,
    location_meta: dict[str, Any],
    source_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    with xr.open_dataset(source_path) as ds:
        ds.load()

        attrs = dict(ds.attrs)
        profile: dict[str, Any] = {}
        for var in PROFILE_VARIABLES:
            values = vector_values(ds, var, precision=3)
            if values is not None:
                profile[var.lower()] = values

        derived: dict[str, Any] = {}
        if "T" in ds:
            derived["temperature_c"] = array_to_list(temperature_to_celsius(ds["T"].values), 2)
        if "P" in ds:
            derived["pressure_hpa"] = array_to_list(pressure_to_hpa(ds["P"].values), 1)
        if "P" in ds and "QV" in ds:
            derived["dewpoint_c"] = array_to_list(
                dewpoint_from_specific_humidity(ds["P"].values, ds["QV"].values),
                2,
            )
        if "U" in ds and "V" in ds:
            derived["wind_speed_ms"] = array_to_list(wind_speed(ds["U"].values, ds["V"].values), 2)
            derived["wind_dir_deg"] = array_to_list(wind_direction_from(ds["U"].values, ds["V"].values), 0)

        surface = {
            "aswdir_s_wm2": scalar_value(ds, "ASWDIR_S", precision=2),
            "aswdifd_s_wm2": scalar_value(ds, "ASWDIFD_S", precision=2),
        }
        surface = {k: v for k, v in surface.items() if v is not None}

    payload = {
        "schema_version": SCHEMA_VERSION,
        "product": "emagram_profile",
        "model": model_key,
        "run": run_tag,
        "step": source_path.stem,
        "location": location_payload(location_id, location_meta),
        "ref_time": attrs.get("ref_time"),
        "valid_time": attrs.get("valid_time"),
        "horizon": clean_number(attrs.get("horizon")),
        "source": rel(source_path),
        "units": {
            "height": "m",
            "p": "Pa",
            "t": "K",
            "qv": "kg kg-1",
            "u": "m s-1",
            "v": "m s-1",
            "temperature_c": "degC",
            "pressure_hpa": "hPa",
            "dewpoint_c": "degC",
            "wind_speed_ms": "m s-1",
            "wind_dir_deg": "degrees_from",
            "aswdir_s_wm2": "W m-2",
            "aswdifd_s_wm2": "W m-2",
        },
        "profile": profile,
        "derived": derived,
    }
    if surface:
        payload["surface"] = surface

    write_json(output_path, payload)
    return {
        "step": source_path.stem,
        "url": rel(output_path),
        "valid_time": attrs.get("valid_time"),
        "horizon": clean_number(attrs.get("horizon")),
    }


def export_profiles_from_packed(
    model_key: str,
    run_tag: str,
    location_id: str,
    location_meta: dict[str, Any],
    source_path: Path,
) -> list[dict[str, Any]]:
    output_dir = WEB_DIR / "emagrams" / model_key / run_tag / location_id

    with xr.open_dataset(source_path) as ds:
        ds.load()
        attrs = dict(ds.attrs)
        step_labels = [normalize_step_label(item) for item in np.asarray(ds["step_label"].values).tolist()]
        horizons = np.asarray(ds["horizon"].values).tolist() if "horizon" in ds else [None] * len(step_labels)
        valid_epochs = (
            np.asarray(ds["valid_time_epoch"].values, dtype=np.int64).tolist()
            if "valid_time_epoch" in ds
            else [None] * len(step_labels)
        )
        height_values = np.asarray(ds["height"].values) if "height" in ds else None
        if height_values is None:
            raise ValueError(f"packed profile has no height coordinate: {source_path}")

        step_count = len(step_labels)
        level_count = int(height_values.size)
        values = np.full(
            (step_count, len(EMAGRAM_BUNDLE_VARIABLES), level_count),
            np.nan,
            dtype="<f4",
        )
        exports: list[dict[str, Any]] = []
        for step_index, step_label in enumerate(step_labels):
            t_values = np.asarray(ds["T"].isel(time=step_index).values) if "T" in ds else None
            p_values = np.asarray(ds["P"].isel(time=step_index).values) if "P" in ds else None
            qv_values = np.asarray(ds["QV"].isel(time=step_index).values) if "QV" in ds else None
            u_values = np.asarray(ds["U"].isel(time=step_index).values) if "U" in ds else None
            v_values = np.asarray(ds["V"].isel(time=step_index).values) if "V" in ds else None
            values[step_index, :, :] = build_bundle_step_values(
                p=p_values,
                t=t_values,
                qv=qv_values,
                u=u_values,
                v=v_values,
                level_count=level_count,
            )

            surface = {}
            for var, key in (("ASWDIR_S", "aswdir_s_wm2"), ("ASWDIFD_S", "aswdifd_s_wm2")):
                if var in ds:
                    surface[key] = clean_number(np.asarray(ds[var].isel(time=step_index).values).item(), 2)
            surface = {k: v for k, v in surface.items() if v is not None}

            valid_time = epoch_to_iso(valid_epochs[step_index]) if valid_epochs[step_index] is not None else None
            horizon = clean_number(horizons[step_index]) if horizons[step_index] is not None else None
            exports.append(
                {
                    "step": step_label,
                    "valid_time": valid_time,
                    "horizon": horizon,
                    "surface": surface or None,
                }
            )

    return write_emagram_bundle(
        output_dir=output_dir,
        model_key=model_key,
        run_tag=run_tag,
        location_id=location_id,
        location_meta=location_meta,
        ref_time=attrs.get("ref_time"),
        source=rel(source_path),
        height_values=height_values,
        steps=exports,
        values=values,
        url_for=rel,
        write_json=write_json,
    )


def export_thermal_panel(
    model_key: str,
    run_tag: str,
    location_id: str,
    location_meta: dict[str, Any],
    source_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    with xr.open_dataset(source_path) as ds:
        ds.load()
        heights = array_to_list(ds["height"].values, 0) if "height" in ds.coords else []
        horizons = array_to_list(ds["horizon_label"].values) if "horizon_label" in ds.coords else []
        valid_times = array_to_list(ds["valid_time"].values) if "valid_time" in ds.coords else []
        w_values = array_to_list(ds["w"].values, 2) if "w" in ds else []
        z_lcl = array_to_list(ds["z_lcl"].values, 0) if "z_lcl" in ds else []
        z_top = array_to_list(ds["z_top"].values, 0) if "z_top" in ds else []
        q_h = array_to_list(ds["Q_H"].values, 1) if "Q_H" in ds else []
        active = array_to_list(ds["active"].values) if "active" in ds else []

        if "w" in ds:
            w_arr = np.asarray(ds["w"].values, dtype=float)
            max_w_by_horizon = np.nanmax(w_arr, axis=1)
            max_w = float(np.nanmax(w_arr)) if np.isfinite(w_arr).any() else None
        else:
            max_w_by_horizon = np.asarray([])
            max_w = None

    summary = {
        "max_w_ms": clean_number(max_w, 2) if max_w is not None else None,
        "max_w_by_horizon_ms": array_to_list(max_w_by_horizon, 2),
        "active_count": sum(1 for item in active if item is True),
    }

    payload = {
        "schema_version": SCHEMA_VERSION,
        "product": "thermal_panel",
        "model": model_key,
        "run": run_tag,
        "location": location_payload(location_id, location_meta),
        "source": rel(source_path),
        "units": {
            "height_m": "m",
            "w_ms": "m s-1",
            "z_lcl_m": "m",
            "z_top_m": "m",
            "sensible_heat_flux_wm2": "W m-2",
        },
        "horizons": horizons,
        "valid_times": valid_times,
        "height_m": heights,
        "w_ms": w_values,
        "z_lcl_m": z_lcl,
        "z_top_m": z_top,
        "sensible_heat_flux_wm2": q_h,
        "active": active,
        "summary": summary,
    }

    write_json(output_path, payload)
    return {
        "url": rel(output_path),
        "source": rel(source_path),
        "horizons": horizons,
        "valid_times": valid_times,
        "summary": summary,
    }


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


def raw_wind_component(ds: xr.Dataset, name: str, step_index: int, grid_stride: int) -> np.ndarray:
    values = np.asarray(ds[name].values[step_index, ::grid_stride, ::grid_stride])
    scaled = np.rint(values.astype(float) / WIND_WEB_SCALE_FACTOR)
    missing = ~np.isfinite(scaled)
    scaled = np.clip(scaled, -127, 127)
    scaled[missing] = WIND_WEB_FILL_VALUE
    return scaled.astype("i1")


def wind_step_summary(u_raw: np.ndarray, v_raw: np.ndarray) -> dict[str, Any]:
    valid = (u_raw != WIND_WEB_FILL_VALUE) & (v_raw != WIND_WEB_FILL_VALUE)
    if not np.any(valid):
        return {"min_speed_ms": None, "max_speed_ms": None}

    u_ms = u_raw[valid].astype(float) * WIND_WEB_SCALE_FACTOR
    v_ms = v_raw[valid].astype(float) * WIND_WEB_SCALE_FACTOR
    speed = np.hypot(u_ms, v_ms)
    return {
        "min_speed_ms": clean_number(np.nanmin(speed), 2),
        "max_speed_ms": clean_number(np.nanmax(speed), 2),
    }


def wind_map_bbox(lat: np.ndarray, lon: np.ndarray, attrs: dict[str, Any]) -> list[float]:
    keys = ("crop_lon_min", "crop_lat_min", "crop_lon_max", "crop_lat_max")
    if all(key in attrs and attrs[key] is not None for key in keys):
        return [clean_number(attrs[key], 5) for key in keys]
    return [
        clean_number(np.nanmin(lon), 5),
        clean_number(np.nanmin(lat), 5),
        clean_number(np.nanmax(lon), 5),
        clean_number(np.nanmax(lat), 5),
    ]


def wind_style_payload(lat: np.ndarray, lon: np.ndarray, attrs: dict[str, Any]) -> dict[str, Any]:
    style = dict(WIND_WEB_STYLE)
    style["map_bbox"] = wind_map_bbox(lat, lon, attrs)
    if attrs.get("domain_id") or attrs.get("domain_label"):
        style["domain"] = {
            "id": attrs.get("domain_id") or "default",
            "label": attrs.get("domain_label") or attrs.get("domain_id") or "Default",
            "bbox": style["map_bbox"],
        }
    return style


def export_wind_level(
    model_key: str,
    run_tag: str,
    level_name: str,
    source_path: Path,
    grid_stride: int,
) -> dict[str, Any]:
    output_dir = WEB_DIR / "wind_maps" / model_key / run_tag / level_name
    steps_dir = output_dir / "steps"

    with xr.open_dataset(source_path) as ds:
        ds.load()
        attrs = dict(ds.attrs)
        lat = np.asarray(ds["latitude"].values[::grid_stride, ::grid_stride], dtype=float)
        lon = np.asarray(ds["longitude"].values[::grid_stride, ::grid_stride], dtype=float)
        step_labels = [normalize_step_label(item) for item in np.asarray(ds["step_label"].values).tolist()]
        horizons = np.asarray(ds["horizon"].values, dtype=int)
        valid_epochs = np.asarray(ds["valid_time_epoch"].values, dtype=np.int64)

        height, width = lat.shape
        step_exports: list[dict[str, Any]] = []

        for step_index, step_label in enumerate(step_labels):
            u_raw = raw_wind_component(ds, "u", step_index, grid_stride)
            v_raw = raw_wind_component(ds, "v", step_index, grid_stride)
            interleaved = np.empty(u_raw.size * 2, dtype="i1")
            interleaved[0::2] = u_raw.ravel()
            interleaved[1::2] = v_raw.ravel()

            step_path = steps_dir / f"{step_label}.bin"
            step_path.parent.mkdir(parents=True, exist_ok=True)
            step_path.write_bytes(interleaved.tobytes())

            step_exports.append(
                {
                    "step": step_label,
                    "horizon": int(horizons[step_index]),
                    "valid_time": epoch_to_iso(valid_epochs[step_index]),
                    "url": rel(step_path),
                    "byte_length": int(step_path.stat().st_size),
                    **wind_step_summary(u_raw, v_raw),
                }
            )

    metadata_path = output_dir / "metadata.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "product": "wind_map_level",
        "model": model_key,
        "run": run_tag,
        "level": {
            "name": level_name,
            "type": attrs.get("level_type"),
            "height_m": clean_number(attrs.get("level_h"), 1),
        },
        "ref_time": attrs.get("ref_time"),
        "source": rel(source_path),
        "grid": {
            "projection": "EPSG:4326",
            "width": width,
            "height": height,
            "source_stride": grid_stride,
            "lon": wind_axis_payload(lon[0, :]),
            "lat": wind_axis_payload(lat[:, 0]),
        },
        "encoding": {
            "format": "int8-interleaved-u-v",
            "dtype": "int8",
            "components": ["u", "v"],
            "units": "m s-1",
            "scale_factor": WIND_WEB_SCALE_FACTOR,
            "add_offset": 0.0,
            "missing_value": WIND_WEB_FILL_VALUE,
        },
        "style": wind_style_payload(lat, lon, attrs),
        "steps": step_exports,
    }
    write_json(metadata_path, payload, pretty=True)

    return {
        "metadata": rel(metadata_path),
        "source": rel(source_path),
        "level_type": payload["level"]["type"],
        "level_h": payload["level"]["height_m"],
        "grid": {
            "width": width,
            "height": height,
            "source_stride": grid_stride,
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
    grid_stride = env_int("WIND_WEB_GRID_STRIDE", WIND_WEB_DEFAULT_GRID_STRIDE)
    wind_manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "product": "wind_maps",
        "default_level": WIND_WEB_DEFAULT_LEVEL,
        "grid_stride": grid_stride,
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

                source_path = Path(level_entry.get("path", ""))
                if not source_path.exists():
                    log(f"WARN wind source missing for {model_key} {run_tag} {level_name}: {source_path}")
                    continue

                try:
                    exported_level = export_wind_level(model_key, run_tag, level_name, source_path, grid_stride)
                except Exception as exc:
                    log(f"WARN wind export failed for {source_path}: {exc}")
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


def export_model(model: dict[str, Any], locations: dict[str, Any]) -> dict[str, Any]:
    model_key = model["key"]
    cache_dir = model["cache_dir"]
    packed_cache_dir = model.get("packed_cache_dir")
    profile_chunk_dir = model.get("profile_chunk_dir")
    scanned_chunk_runs = (
        scan_profile_chunks_with_fallback(profile_chunk_dir, locations)
        if isinstance(profile_chunk_dir, Path)
        else {}
    )
    scanned_packed_runs = (
        scan_packed_profiles(packed_cache_dir, locations)
        if isinstance(packed_cache_dir, Path)
        else {}
    )
    scanned_runs = scanned_chunk_runs or scanned_packed_runs or scan_profiles(cache_dir, locations)
    profile_source = "direct_chunks" if scanned_chunk_runs else ("packed" if scanned_packed_runs else "hourly")
    model_manifest: dict[str, Any] = {
        "label": model["label"],
        "cache_dir": str(cache_dir),
        "packed_cache_dir": str(packed_cache_dir) if packed_cache_dir else None,
        "profile_chunk_dir": str(profile_chunk_dir) if profile_chunk_dir else None,
        "profile_source": profile_source,
        "latest_run": max(scanned_runs.keys()) if scanned_runs else None,
        "runs": {},
        "counts": {
            "runs": len(scanned_runs),
            "locations": 0,
            "profiles": 0,
            "thermal_panels": 0,
            "region_forecasts": 0,
        },
    }

    seen_locations: set[str] = set()

    for run_tag, run_locations in scanned_runs.items():
        run_manifest = {"locations": {}}
        for location_id, source_entry in run_locations.items():
            location_meta = locations[location_id]
            if scanned_chunk_runs:
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
            elif scanned_packed_runs:
                try:
                    profile_exports = export_profiles_from_packed(
                        model_key,
                        run_tag,
                        location_id,
                        location_meta,
                        source_entry,
                    )
                except Exception as exc:
                    log(f"WARN packed profile export failed for {source_entry}: {exc}")
                    profile_exports = []
            else:
                profile_exports = []
                for profile_path in source_entry:
                    out_path = (
                        WEB_DIR
                        / "emagrams"
                        / model_key
                        / run_tag
                        / location_id
                        / f"{profile_path.stem}.json"
                    )
                    try:
                        profile_exports.append(
                            export_profile(model_key, run_tag, location_id, location_meta, profile_path, out_path)
                        )
                    except Exception as exc:
                        log(f"WARN profile export failed for {profile_path}: {exc}")

            thermal_export = None
            thermal_path = cache_dir / run_tag / "thermals" / f"{sanitize_name(location_id)}.nc"
            if not thermal_path.exists():
                thermal_path = cache_dir / run_tag / "thermals" / f"{location_id}.nc"
            if thermal_path.exists():
                out_path = WEB_DIR / "thermal_panels" / model_key / run_tag / f"{location_id}.json"
                try:
                    thermal_export = export_thermal_panel(
                        model_key,
                        run_tag,
                        location_id,
                        location_meta,
                        thermal_path,
                        out_path,
                    )
                    model_manifest["counts"]["thermal_panels"] += 1
                except Exception as exc:
                    log(f"WARN thermal export failed for {thermal_path}: {exc}")

            region_forecast = write_region_forecast(
                model_key,
                run_tag,
                location_id,
                location_meta,
                profile_exports,
                thermal_export,
            )

            seen_locations.add(location_id)
            model_manifest["counts"]["profiles"] += len(profile_exports)
            model_manifest["counts"]["region_forecasts"] += 1
            bundle_url = next((item["bundle_url"] for item in profile_exports if item.get("bundle_url")), None)
            run_manifest["locations"][location_id] = {
                "type": location_meta.get("type", "legacy"),
                "display_name": location_meta.get("display_name", location_id),
                "steps": region_forecast["steps"],
                "valid_times": region_forecast["valid_times"],
                "region_forecast": region_forecast["url"],
                "thermal_panel": thermal_export["url"] if thermal_export else None,
                "emagram_template": (
                    None
                    if bundle_url
                    else rel(WEB_DIR / "emagrams" / model_key / run_tag / location_id / "{step}.json")
                ),
                "emagram_bundle": bundle_url,
            }

        model_manifest["runs"][run_tag] = run_manifest

    model_manifest["counts"]["locations"] = len(seen_locations)
    return model_manifest


def validate_manifest(manifest: dict[str, Any]) -> None:
    profile_count = manifest["counts"]["profiles"]
    source_nc_count = sum(1 for model in MODELS for _ in model["cache_dir"].glob("*/*/H*.nc"))
    if source_nc_count and profile_count == 0:
        raise RuntimeError("NetCDF profile files exist, but no web profile JSON files were written")

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
            "netcdf_manifest": "manifest.json",
            "netcdf_manifest_generated_at": source_manifest.get("generated_at"),
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
            },
        },
        "models": {},
        "counts": {
            "locations": len(locations),
            "region_locations": sum(1 for item in locations.values() if item.get("type") == "region"),
            "legacy_locations": sum(1 for item in locations.values() if item.get("type") == "legacy"),
            "profiles": 0,
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
        },
        "notes": [
            "Generated from existing NetCDF files; no additional MeteoSwiss downloads are performed.",
            "Emagram profiles are bundled per location/run as bundle.json plus float32 little-endian profiles.bin.",
            "Wind map exports are split into browser-readable metadata JSON plus lazy-loaded int8 binary u/v slices.",
            "Sunshine map exports are browser-readable metadata JSON plus lazy-loaded uint8 binary sunshine-fraction slices.",
            "Rain map exports are browser-readable metadata JSON plus lazy-loaded uint8 binary precipitation slices.",
            "Sun+Rain map exports are browser-readable metadata JSON plus lazy-loaded uint8 semantic sunshine/rain slices.",
            "Cloud map exports are browser-readable metadata JSON plus lazy-loaded packed uint4 cloud-cover slices.",
        ],
    }

    for model in MODELS:
        model_manifest = export_model(model, locations)
        manifest["models"][model["key"]] = model_manifest
        manifest["counts"]["profiles"] += model_manifest["counts"]["profiles"]
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
        f"{manifest['counts']['cloud_map_steps']} cloud map steps"
    )


if __name__ == "__main__":
    main()
