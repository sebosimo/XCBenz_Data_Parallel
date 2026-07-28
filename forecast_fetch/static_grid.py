"""Compatibility loader for legacy and current MeteoSwiss HGRID layouts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import xarray as xr


_GRID_NAMES = {
    "lat": ("latitude", "lat", "clat", "tlat"),
    "lon": ("longitude", "lon", "clon", "tlon"),
}


def _grid_variable_name(dataset: Any, component: str) -> str | None:
    names = list(dataset.coords) + list(dataset.data_vars)
    by_lower = {str(name).lower(): str(name) for name in names}
    for candidate in _GRID_NAMES[component]:
        if candidate in by_lower:
            return by_lower[candidate]
    return next(
        (
            str(name)
            for name in names
            if component in str(name).lower()
        ),
        None,
    )


def load_horizontal_grid(path: str | Path) -> dict[str, Any]:
    """Load explicit cell coordinates from current or legacy static GRIB files."""
    attempts = (
        {"indexpath": "", "filter_by_keys": {"typeOfLevel": "surface"}},
        {"indexpath": ""},
    )
    errors: list[str] = []
    for backend_kwargs in attempts:
        dataset = None
        try:
            dataset = xr.open_dataset(
                path,
                engine="cfgrib",
                backend_kwargs=backend_kwargs,
            )
            latitude_name = _grid_variable_name(dataset, "lat")
            longitude_name = _grid_variable_name(dataset, "lon")
            if not latitude_name or not longitude_name:
                errors.append(
                    f"{backend_kwargs}: no explicit latitude/longitude fields"
                )
                continue
            latitude = dataset[latitude_name].load()
            longitude = dataset[longitude_name].load()
            if latitude.size <= 0 or latitude.size != longitude.size:
                errors.append(
                    f"{backend_kwargs}: coordinate sizes differ "
                    f"({latitude.size} != {longitude.size})"
                )
                continue
            return {"lat": latitude, "lon": longitude}
        except Exception as exc:  # noqa: BLE001 - the legacy fallback is intentional.
            errors.append(f"{backend_kwargs}: {exc}")
        finally:
            if dataset is not None:
                dataset.close()
    raise ValueError(
        f"could not load explicit HGRID coordinates from {path}: "
        + "; ".join(errors)
    )
