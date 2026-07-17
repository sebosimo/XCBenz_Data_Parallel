"""Shared location sampling and direct profile-chunk assembly."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

from web_profiles import (
    SURFACE_RADIATION_KEYS,
    build_bundle_step_values,
    clean_number,
    write_profile_chunk,
)

from .planning import ModelFetchPolicy


def _sample_field(fields: dict[str, Any]) -> Any:
    return next(
        (value for value in fields.values() if value is not None and hasattr(value, "dims")),
        None,
    )


def _field_lat_lon_names(sample: Any) -> tuple[str, str]:
    return (
        "latitude" if "latitude" in sample.coords else "lat",
        "longitude" if "longitude" in sample.coords else "lon",
    )


def location_indices(sample: Any, locations: dict[str, dict[str, Any]]) -> dict[str, int]:
    latitude_name, longitude_name = _field_lat_lon_names(sample)
    latitudes = sample[latitude_name].values
    longitudes = sample[longitude_name].values
    return {
        name: int(
            np.argmin(
                (latitudes - coordinates["lat"]) ** 2
                + (longitudes - coordinates["lon"]) ** 2
            )
        )
        for name, coordinates in locations.items()
    }


def _point_profiles(
    data: Any,
    latitude_name: str,
    indices: dict[str, int],
) -> tuple[list[str], np.ndarray]:
    names = list(indices)
    if not names:
        return names, np.empty((0, 0), dtype=np.float32)
    spatial_dimension = data[latitude_name].dims[0]
    selected = data.squeeze().isel(
        {spatial_dimension: [indices[name] for name in names]}
    ).compute()
    values = np.asarray(selected.values, dtype=np.float32)
    if spatial_dimension in selected.dims:
        values = np.moveaxis(values, selected.get_axis_num(spatial_dimension), 0)
    else:
        values = values.reshape((1, -1))
    return names, values.reshape((len(names), -1))


def _height_profile(
    fields: dict[str, Any],
    latitude_name: str,
    index: int,
    fallback_level_count: int,
) -> np.ndarray:
    if "HHL" not in fields or fields["HHL"] is None:
        return np.arange(fallback_level_count, dtype=np.float32)
    hhl = fields["HHL"]
    spatial_dimension = hhl[latitude_name].dims[0]
    profile = hhl.squeeze().isel({spatial_dimension: index}).compute()
    values = np.asarray(profile.values, dtype=np.float32).ravel()
    return ((values[:-1] + values[1:]) / 2.0).astype(np.float32)


def append_profile_chunk(
    buffers: dict[str, Any],
    fields: dict[str, Any],
    locations: dict[str, dict[str, Any]],
    tag: str,
    horizon: int,
    reference_time: datetime.datetime,
    location_radiation: dict[str, dict[str, float]] | None = None,
    *,
    policy: ModelFetchPolicy,
    log: Callable[..., None],
    cached_location_indices: dict[str, int] | None = None,
    height_cache: dict[str, np.ndarray] | None = None,
) -> bool:
    sample = _sample_field(fields)
    if sample is None:
        return False
    latitude_name, _longitude_name = _field_lat_lon_names(sample)
    indices = (
        cached_location_indices
        if cached_location_indices is not None
        else location_indices(sample, locations)
    )
    valid_time = reference_time + datetime.timedelta(hours=horizon)
    location_names = list(indices)
    profiles_by_variable: dict[str, np.ndarray | None] = {}
    for variable in policy.profile_variables:
        if variable not in fields:
            profiles_by_variable[variable] = None
            continue
        names, profiles = _point_profiles(fields[variable], latitude_name, indices)
        if names != location_names:
            raise RuntimeError(
                f"{policy.model.upper()} direct profile location order changed "
                f"for {variable} H+{horizon:0{policy.step_digits}d}"
            )
        profiles_by_variable[variable] = profiles

    for location_position, (name, index) in enumerate(indices.items()):
        raw_profiles = {
            variable: (
                None
                if profiles_by_variable[variable] is None
                else profiles_by_variable[variable][location_position]
            )
            for variable in policy.profile_variables
        }
        level_source = next(
            (profile for profile in raw_profiles.values() if profile is not None),
            None,
        )
        if level_source is None:
            continue
        level_count = int(level_source.shape[0])
        if height_cache is not None and name in height_cache:
            height = height_cache[name]
        else:
            height = _height_profile(fields, latitude_name, index, level_count)
            if height_cache is not None:
                height_cache[name] = height
        if height.shape[0] != level_count:
            log(
                f"{policy.model.upper()} direct profile height length mismatch "
                f"for {name} H+{horizon:0{policy.step_digits}d}: "
                f"{height.shape[0]} != {level_count}",
                "WARNING",
            )
            height = height[:level_count]

        buffer = buffers.setdefault(name, {"height": height, "steps": [], "values": []})
        if len(buffer["height"]) != len(height) or not np.allclose(
            buffer["height"],
            height,
            equal_nan=True,
        ):
            log(
                f"{policy.model.upper()} direct profile height changed within chunk "
                f"for {name} H+{horizon:0{policy.step_digits}d}",
                "WARNING",
            )

        surface = {}
        for source_key, output_key in SURFACE_RADIATION_KEYS.items():
            raw_value = (location_radiation or {}).get(name, {}).get(source_key)
            value = clean_number(raw_value, 2)
            if value is not None:
                surface[output_key] = value

        buffer["steps"].append(
            {
                "step": policy.step_label(horizon),
                "valid_time": valid_time.isoformat(),
                "horizon": horizon,
                "surface": surface or None,
            }
        )
        buffer["values"].append(
            build_bundle_step_values(
                p=raw_profiles.get("P"),
                t=raw_profiles.get("T"),
                qv=raw_profiles.get("QV"),
                u=raw_profiles.get("U"),
                v=raw_profiles.get("V"),
                level_count=level_count,
            )
        )
    return True


def finalize_profile_chunk(
    buffers: dict[str, Any],
    locations: dict[str, dict[str, Any]],
    tag: str,
    chunk_id: str,
    reference_time: datetime.datetime,
    *,
    policy: ModelFetchPolicy,
    log: Callable[..., None],
    output_root: str | Path,
) -> int:
    written = 0
    for name, buffer in buffers.items():
        if not buffer["steps"]:
            continue
        values = np.stack(buffer["values"]).astype("<f4")
        write_profile_chunk(
            output_root=Path(output_root),
            model_key=policy.model_key,
            run_tag=tag,
            chunk_id=chunk_id,
            location_id=name,
            location_meta=locations[name],
            ref_time=reference_time.isoformat(),
            height_values=np.asarray(buffer["height"], dtype=np.float32),
            steps=buffer["steps"],
            values=values,
        )
        written += 1
    log(
        f"{policy.model.upper()} direct profile chunk {chunk_id} wrote "
        f"{written} location artifact(s)",
        "NOTICE",
    )
    return written
