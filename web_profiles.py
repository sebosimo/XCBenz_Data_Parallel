"""Helpers for browser-ready emagram profile bundles.

The public contract is intentionally stable:
float32 little-endian values laid out as step -> variable -> level.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
from pathlib import Path
from typing import Any, Callable

import numpy as np


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
EMAGRAM_BUNDLE_UNITS = {
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
}
SURFACE_RADIATION_KEYS = {
    "ASWDIR_S": "aswdir_s_wm2",
    "ASWDIFD_S": "aswdifd_s_wm2",
}


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
    return [clean_number(v, precision) for v in arr.ravel().tolist()]


def horizon_sort_key(label: str) -> tuple[int, str]:
    match = re.search(r"(\d+)", str(label))
    if not match:
        return (10_000, str(label))
    return (int(match.group(1)), str(label))


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


def build_bundle_step_values(
    *,
    p: np.ndarray | None,
    t: np.ndarray | None,
    qv: np.ndarray | None,
    u: np.ndarray | None,
    v: np.ndarray | None,
    level_count: int,
) -> np.ndarray:
    values = np.full((len(EMAGRAM_BUNDLE_VARIABLES), level_count), np.nan, dtype="<f4")
    arrays = {
        "p": p,
        "t": t,
        "qv": qv,
        "u": u,
        "v": v,
        "temperature_c": temperature_to_celsius(t) if t is not None else None,
        "pressure_hpa": pressure_to_hpa(p) if p is not None else None,
        "dewpoint_c": dewpoint_from_specific_humidity(p, qv) if p is not None and qv is not None else None,
        "wind_speed_ms": wind_speed(u, v) if u is not None and v is not None else None,
        "wind_dir_deg": wind_direction_from(u, v) if u is not None and v is not None else None,
    }
    for variable_index, variable_name in enumerate(EMAGRAM_BUNDLE_VARIABLES):
        variable_values = arrays.get(variable_name)
        if variable_values is None:
            continue
        variable_values = np.asarray(variable_values, dtype=np.float32).ravel()
        if variable_values.shape[0] != level_count:
            continue
        values[variable_index, :] = variable_values
    return values


def expected_byte_length(step_count: int, level_count: int) -> int:
    return int(step_count) * len(EMAGRAM_BUNDLE_VARIABLES) * int(level_count) * 4


def _write_json(path: Path, payload: Any, *, pretty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        if pretty:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        else:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def write_profile_chunk(
    *,
    output_root: Path,
    model_key: str,
    run_tag: str,
    chunk_id: str,
    location_id: str,
    location_meta: dict[str, Any],
    ref_time: str,
    height_values: np.ndarray,
    steps: list[dict[str, Any]],
    values: np.ndarray,
) -> Path:
    output_dir = output_root / model_key / run_tag / chunk_id / location_id
    output_dir.mkdir(parents=True, exist_ok=True)
    data_path = output_dir / "profiles.bin"
    values = np.asarray(values, dtype="<f4")
    data_path.write_bytes(values.tobytes())
    metadata_path = output_dir / "chunk.json"
    payload = {
        "schema_version": 1,
        "product": "emagram_profile_chunk",
        "model": model_key,
        "run": run_tag,
        "chunk_id": chunk_id,
        "location": location_payload(location_id, location_meta),
        "ref_time": ref_time,
        "source": "direct-grib",
        "units": EMAGRAM_BUNDLE_UNITS,
        "encoding": {
            "format": "float32-le-step-variable-level",
            "dtype": "float32",
            "variables": list(EMAGRAM_BUNDLE_VARIABLES),
            "step_count": int(values.shape[0]),
            "level_count": int(values.shape[2]),
            "data": data_path.relative_to(output_root).as_posix(),
            "byte_length": int(data_path.stat().st_size),
            "missing_value": "NaN",
        },
        "height": array_to_list(height_values, 3),
        "steps": steps,
    }
    expected = expected_byte_length(values.shape[0], values.shape[2])
    if payload["encoding"]["byte_length"] != expected:
        raise ValueError(f"chunk byte length mismatch for {metadata_path}: expected {expected}")
    _write_json(metadata_path, payload)
    return metadata_path


def read_profile_chunk(path: Path) -> tuple[dict[str, Any], np.ndarray]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    encoding = payload.get("encoding") or {}
    if payload.get("product") != "emagram_profile_chunk":
        raise ValueError(f"{path} is not an emagram profile chunk")
    if tuple(encoding.get("variables") or []) != EMAGRAM_BUNDLE_VARIABLES:
        raise ValueError(f"{path} uses unexpected variables")
    step_count = int(encoding.get("step_count") or 0)
    level_count = int(encoding.get("level_count") or 0)
    expected = expected_byte_length(step_count, level_count)
    data_path = path.parent / Path(str(encoding.get("data", "profiles.bin"))).name
    actual = data_path.stat().st_size
    if actual != expected or int(encoding.get("byte_length") or -1) != actual:
        raise ValueError(f"{path} byte length mismatch: expected={expected} actual={actual}")
    values = np.fromfile(data_path, dtype="<f4").reshape(
        (step_count, len(EMAGRAM_BUNDLE_VARIABLES), level_count)
    )
    return payload, values


def write_emagram_bundle(
    *,
    output_dir: Path,
    model_key: str,
    run_tag: str,
    location_id: str,
    location_meta: dict[str, Any],
    ref_time: str | None,
    source: str,
    height_values: np.ndarray,
    steps: list[dict[str, Any]],
    values: np.ndarray,
    url_for: Callable[[Path], str],
    write_json: Callable[[Path, Any], None],
) -> list[dict[str, Any]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    values = np.asarray(values, dtype="<f4")
    data_path = output_dir / "profiles.bin"
    data_path.write_bytes(values.tobytes())
    bundle_path = output_dir / "bundle.json"

    exports: list[dict[str, Any]] = []
    for step_index, step in enumerate(steps):
        item = dict(step)
        item["bundle_step_index"] = step_index
        exports.append(item)

    payload = {
        "schema_version": 1,
        "product": "emagram_bundle",
        "model": model_key,
        "run": run_tag,
        "location": location_payload(location_id, location_meta),
        "ref_time": ref_time,
        "source": source,
        "units": EMAGRAM_BUNDLE_UNITS,
        "encoding": {
            "format": "float32-le-step-variable-level",
            "dtype": "float32",
            "variables": list(EMAGRAM_BUNDLE_VARIABLES),
            "step_count": int(values.shape[0]),
            "level_count": int(values.shape[2]),
            "data": url_for(data_path),
            "byte_length": int(data_path.stat().st_size),
            "missing_value": "NaN",
        },
        "height": array_to_list(height_values, 3),
        "steps": exports,
    }
    expected = expected_byte_length(values.shape[0], values.shape[2])
    if payload["encoding"]["byte_length"] != expected:
        raise ValueError(f"bundle byte length mismatch for {bundle_path}: expected {expected}")
    write_json(bundle_path, payload)
    for item in exports:
        item["bundle_url"] = url_for(bundle_path)
    return exports


def merge_profile_chunks(
    chunk_paths: list[Path],
    *,
    output_dir: Path,
    model_key: str,
    run_tag: str,
    location_id: str,
    location_meta: dict[str, Any],
    url_for: Callable[[Path], str],
    write_json: Callable[[Path, Any], None],
) -> list[dict[str, Any]]:
    rows: list[tuple[tuple[int, str], dict[str, Any], np.ndarray, dict[str, Any]]] = []
    height_values: np.ndarray | None = None
    ref_time: str | None = None
    seen_steps: set[str] = set()

    for chunk_path in sorted(chunk_paths):
        payload, values = read_profile_chunk(chunk_path)
        chunk_height = np.asarray(payload.get("height") or [], dtype=np.float32)
        if height_values is None:
            height_values = chunk_height
        elif height_values.shape != chunk_height.shape or not np.allclose(height_values, chunk_height, equal_nan=True):
            raise ValueError(f"{chunk_path} height coordinate differs from other chunks")
        if ref_time is None:
            ref_time = payload.get("ref_time")
        for step_index, step in enumerate(payload.get("steps") or []):
            step_label = str(step.get("step"))
            if step_label in seen_steps:
                raise ValueError(f"duplicate profile step {step_label} while merging {location_id}")
            seen_steps.add(step_label)
            rows.append((horizon_sort_key(step_label), step, values[step_index, :, :], payload))

    if not rows or height_values is None:
        return []

    rows.sort(key=lambda item: item[0])
    steps = [dict(row[1]) for row in rows]
    merged_values = np.stack([row[2] for row in rows]).astype("<f4")
    source = ",".join(sorted({str(row[3].get("chunk_id")) for row in rows if row[3].get("chunk_id")}))
    return write_emagram_bundle(
        output_dir=output_dir,
        model_key=model_key,
        run_tag=run_tag,
        location_id=location_id,
        location_meta=location_meta,
        ref_time=ref_time,
        source=f"direct-grib-chunks:{source}",
        height_values=height_values,
        steps=steps,
        values=merged_values,
        url_for=url_for,
        write_json=write_json,
    )
