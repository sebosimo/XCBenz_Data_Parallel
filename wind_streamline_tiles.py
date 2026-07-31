"""Production-shaped XWS2 Wind streamline tile generation.

The module is opt-in and is not connected to the publication manifest yet.
It implements the Phase-A shadow artifact: deterministic global seed IDs,
normalized Web Mercator trajectories, one-pass XYZ partitioning, strict XWS2
encoding/validation, phase metrics, and bounded step-level multiprocessing.
"""

from __future__ import annotations

import concurrent.futures
import gzip
import hashlib
import json
import math
import os
import platform
import resource
import shutil
import struct
import tempfile
import time
import zlib
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Sequence

import numpy as np

from pipeline_orchestration.job_plan import expected_horizon_count
from wind_streamline_feasibility import (
    Geometry,
    TILE_PROFILES,
    TileProfile,
    grid_bbox,
    latitude_to_mercator_y,
    longitude_to_mercator_x,
    mercator_to_lon_lat,
    resolve_step_path,
    tile_profile_snapshot,
)


CONTRACT = "xcbenz-wind-streamline-tiles"
CONTRACT_VERSION = "2.3.0-shadow.1"
PACKAGE = "immutable-xyz-xws2-v1"
GENERATOR_REVISION = "xws2-fixed-four-stable-lod-mercator-v2"
MANIFEST_LAYOUT = "split-step-index-v1"
LOD_SELECTION_ALGORITHM = "fixed-camera-scale-bands-v1"
MAGIC = b"XWS2"
VERSION = 2
HEADER = struct.Struct("<4sBBHBBHIIIII")
HEADER_FLAGS = 0
QUANTIZATION_MAX = 65_535
TILE_SIZE = 512.0
ORIGINAL_START = 1 << 0
CONTINUES_BEFORE = 1 << 1
CONTINUES_AFTER = 1 << 2
ORIGINAL_END = 1 << 3
KNOWN_FRAGMENT_FLAGS = ORIGINAL_START | CONTINUES_BEFORE | CONTINUES_AFTER | ORIGINAL_END
MAX_TILE_BYTES = 32 * 1024 * 1024
MAX_TILE_FRAGMENTS = 100_000
MAX_TILE_POINTS = 5_000_000


@dataclass(frozen=True)
class ProductionProfile:
    id: int
    name: str
    tile_zoom: int
    pixels_per_mercator_unit: float
    geometry: Geometry

    @classmethod
    def from_feasibility(cls, profile_id: int, profile: TileProfile) -> "ProductionProfile":
        return cls(
            id=profile_id,
            name=profile.name,
            tile_zoom=profile.tile_zoom,
            pixels_per_mercator_unit=profile.pixels_per_mercator_unit,
            geometry=profile.geometry,
        )


_COMPACT_GEOMETRY = TILE_PROFILES["compact-regional"].geometry
_CONTROL_ZOOM_FACTOR = 1.65
_REGIONAL_SELECTION_SCALE = 27_000.0
_LOD_SELECTION_SCALES = {
    "lod-overview": _REGIONAL_SELECTION_SCALE / _CONTROL_ZOOM_FACTOR,
    "lod-regional": _REGIONAL_SELECTION_SCALE,
    "lod-local": _REGIONAL_SELECTION_SCALE * _CONTROL_ZOOM_FACTOR,
    "lod-detail": _REGIONAL_SELECTION_SCALE * _CONTROL_ZOOM_FACTOR**2,
}
_LOCAL_GEOMETRY = Geometry(
    14.0,
    11.76,
    _COMPACT_GEOMETRY.line_width,
    _COMPACT_GEOMETRY.max_len_px,
    _COMPACT_GEOMETRY.steps,
    _COMPACT_GEOMETRY.stroke_opacity,
    _COMPACT_GEOMETRY.trajectory_seconds,
    False,
)
_DETAIL_GEOMETRY = Geometry(
    _LOCAL_GEOMETRY.dx_px,
    _LOCAL_GEOMETRY.dy_px,
    _LOCAL_GEOMETRY.line_width,
    _LOCAL_GEOMETRY.max_len_px,
    _LOCAL_GEOMETRY.steps,
    _LOCAL_GEOMETRY.stroke_opacity,
    _LOCAL_GEOMETRY.trajectory_seconds,
    False,
)

PROFILES = {
    "lod-overview": ProductionProfile(
        1,
        "lod-overview",
        5,
        9_500.0,
        _COMPACT_GEOMETRY,
    ),
    "lod-regional": ProductionProfile(
        2,
        "lod-regional",
        6,
        _REGIONAL_SELECTION_SCALE,
        _COMPACT_GEOMETRY,
    ),
    "lod-local": ProductionProfile(
        3,
        "lod-local",
        7,
        _REGIONAL_SELECTION_SCALE * _CONTROL_ZOOM_FACTOR,
        _LOCAL_GEOMETRY,
    ),
    "lod-detail": ProductionProfile(
        4,
        "lod-detail",
        8,
        72_000.0,
        _DETAIL_GEOMETRY,
    ),
}
DEFAULT_PROFILE_NAMES = (
    "lod-overview",
    "lod-regional",
    "lod-local",
    "lod-detail",
)


@dataclass(frozen=True)
class ProjectedPath:
    path_id: int
    seed_speed_ms: float
    points: Sequence[tuple[float, float]] | np.ndarray


@dataclass(frozen=True)
class TileFragment:
    path_id: int
    fragment_order: int
    flags: int
    terminal_speed_ms: float
    points: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class DecodedFragment:
    path_id: int
    fragment_order: int
    flags: int
    terminal_speed_ms: float
    points: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class DecodedTile:
    profile_id: int
    zoom: int
    x: int
    y: int
    fragments: tuple[DecodedFragment, ...]
    point_count: int


@dataclass(frozen=True)
class SeedLattice:
    row_min: int
    row_max: int
    column_min: int
    column_max: int
    column_count: int
    step_x: float
    step_y: float
    halo: float

    def path_id(self, row: int, column: int) -> int:
        return (row - self.row_min) * self.column_count + (column - self.column_min)


class PhaseTimer:
    def __init__(self) -> None:
        self.values: dict[str, float] = {}

    def measure(self, name: str):
        return _TimedPhase(self, name)


class _TimedPhase:
    def __init__(self, timer: PhaseTimer, name: str) -> None:
        self.timer = timer
        self.name = name
        self.started = 0.0

    def __enter__(self):
        self.started = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        elapsed = (time.perf_counter() - self.started) * 1000.0
        self.timer.values[self.name] = round(elapsed, 3)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _json_artifact_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    multiplier = 1 if platform.system() == "Darwin" else 1024
    return int(value * multiplier)


def _profile_payload(profile: ProductionProfile, lattice: SeedLattice) -> dict[str, Any]:
    return {
        "id": profile.id,
        "name": profile.name,
        "renderer_revision": GENERATOR_REVISION,
        "tile_zoom": profile.tile_zoom,
        "tile_size": round(TILE_SIZE),
        "pixels_per_mercator_unit": profile.pixels_per_mercator_unit,
        "geometry": asdict(profile.geometry),
        "lod_control": {
            "algorithm": LOD_SELECTION_ALGORITHM,
            "responsive_modes": ["compact", "wide"],
            "selection_scale": _LOD_SELECTION_SCALES[profile.name],
        },
        "lattice": asdict(lattice),
    }


class WindGrid:
    def __init__(self, metadata: dict[str, Any], values: bytes):
        grid = metadata["grid"]
        encoding = metadata["encoding"]
        self.width = int(grid["width"])
        self.height = int(grid["height"])
        expected = self.width * self.height * 2
        raw = np.frombuffer(values, dtype=np.int8)
        if raw.size != expected:
            raise ValueError(f"Wind step has {raw.size} bytes, expected {expected}")
        decoded = raw.astype(np.float64).reshape(self.height, self.width, 2)
        missing = int(encoding["missing_value"])
        invalid = (decoded[:, :, 0] == missing) | (decoded[:, :, 1] == missing)
        decoded *= float(encoding["scale_factor"])
        decoded[invalid] = np.nan
        self.values = decoded
        self.lon_start = float(grid["lon"]["start"])
        self.lon_step = float(grid["lon"]["step"])
        self.lat_start = float(grid["lat"]["start"])
        self.lat_step = float(grid["lat"]["step"])

    def sample(self, lon: float, lat: float) -> tuple[float, float] | None:
        x_float = (lon - self.lon_start) / self.lon_step
        y_float = (lat - self.lat_start) / self.lat_step
        x0 = math.floor(x_float)
        y0 = math.floor(y_float)
        if x0 < 0 or y0 < 0 or x0 >= self.width - 1 or y0 >= self.height - 1:
            return None
        x_ratio = x_float - x0
        y_ratio = y_float - y0
        weights = (
            (1.0 - x_ratio) * (1.0 - y_ratio),
            x_ratio * (1.0 - y_ratio),
            (1.0 - x_ratio) * y_ratio,
            x_ratio * y_ratio,
        )
        samples = (
            self.values[y0, x0],
            self.values[y0, x0 + 1],
            self.values[y0 + 1, x0],
            self.values[y0 + 1, x0 + 1],
        )
        u = 0.0
        v = 0.0
        valid_weight = 0.0
        for weight, sample in zip(weights, samples):
            if weight > 0.0 and not math.isnan(float(sample[0])):
                u += float(sample[0]) * weight
                v += float(sample[1]) * weight
                valid_weight += weight
        return (u / valid_weight, v / valid_weight) if valid_weight > 0.0 else None

    def sample_batch(
        self,
        lon: np.ndarray,
        lat: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        x_float = (lon - self.lon_start) / self.lon_step
        y_float = (lat - self.lat_start) / self.lat_step
        x0 = np.floor(x_float).astype(np.int64)
        y0 = np.floor(y_float).astype(np.int64)
        in_grid = (
            (x0 >= 0)
            & (y0 >= 0)
            & (x0 < self.width - 1)
            & (y0 < self.height - 1)
        )
        safe_x = np.clip(x0, 0, self.width - 2)
        safe_y = np.clip(y0, 0, self.height - 2)
        x_ratio = x_float - x0
        y_ratio = y_float - y0
        weights = (
            (1.0 - x_ratio) * (1.0 - y_ratio),
            x_ratio * (1.0 - y_ratio),
            (1.0 - x_ratio) * y_ratio,
            x_ratio * y_ratio,
        )
        samples = (
            self.values[safe_y, safe_x],
            self.values[safe_y, safe_x + 1],
            self.values[safe_y + 1, safe_x],
            self.values[safe_y + 1, safe_x + 1],
        )
        u = np.zeros(lon.shape, dtype=np.float64)
        v = np.zeros(lon.shape, dtype=np.float64)
        valid_weight = np.zeros(lon.shape, dtype=np.float64)
        for weight, sample in zip(weights, samples):
            valid = in_grid & (weight > 0.0) & ~np.isnan(sample[:, 0])
            u += np.where(valid, sample[:, 0] * weight, 0.0)
            v += np.where(valid, sample[:, 1] * weight, 0.0)
            valid_weight += np.where(valid, weight, 0.0)
        valid = valid_weight > 0.0
        result_u = np.zeros(lon.shape, dtype=np.float64)
        result_v = np.zeros(lon.shape, dtype=np.float64)
        np.divide(u, valid_weight, out=result_u, where=valid)
        np.divide(v, valid_weight, out=result_v, where=valid)
        return result_u, result_v, valid


def seed_lattice(profile: ProductionProfile, snapshot: dict[str, Any]) -> SeedLattice:
    bounds = snapshot["draw_bounds"]
    geometry = snapshot["geometry"]
    origin_x, origin_y = snapshot["distance_origin"]
    pixels_per_unit = snapshot["distance_pixels_per_mercator_unit"]
    step_x = geometry["dx_px"] / pixels_per_unit
    step_y = geometry["dy_px"] / pixels_per_unit
    halo = (geometry["max_len_px"] + 12.0) / pixels_per_unit
    west = bounds["west_x"] - halo
    east = bounds["east_x"] + halo
    north = bounds["north_y"] - halo
    south = bounds["south_y"] + halo
    row_min = math.floor((north - origin_y) / step_y) - 1
    row_max = math.ceil((south - origin_y) / step_y) + 1
    column_ranges: list[tuple[int, int]] = []
    for row in range(row_min, row_max + 1):
        row_offset = step_x * 0.5 if abs(row % 2) == 0 else 0.0
        first = math.floor((west - origin_x - row_offset) / step_x) - 1
        last = math.ceil((east - origin_x - row_offset) / step_x) + 1
        column_ranges.append((first, last))
    column_min = min(item[0] for item in column_ranges)
    column_max = max(item[1] for item in column_ranges)
    return SeedLattice(
        row_min=row_min,
        row_max=row_max,
        column_min=column_min,
        column_max=column_max,
        column_count=column_max - column_min + 1,
        step_x=step_x,
        step_y=step_y,
        halo=halo,
    )


def enumerate_seeds(
    snapshot: dict[str, Any],
    lattice: SeedLattice,
) -> Iterator[tuple[int, float, float]]:
    bounds = snapshot["draw_bounds"]
    origin_x, origin_y = snapshot["distance_origin"]
    west = bounds["west_x"] - lattice.halo
    east = bounds["east_x"] + lattice.halo
    north = bounds["north_y"] - lattice.halo
    south = bounds["south_y"] + lattice.halo
    for row in range(lattice.row_min, lattice.row_max + 1):
        mercator_y = origin_y + row * lattice.step_y
        if mercator_y < north or mercator_y > south:
            continue
        row_offset = lattice.step_x * 0.5 if abs(row % 2) == 0 else 0.0
        first_column = math.floor(
            (west - origin_x - row_offset) / lattice.step_x
        ) - 1
        last_column = math.ceil(
            (east - origin_x - row_offset) / lattice.step_x
        ) + 1
        for column in range(first_column, last_column + 1):
            mercator_x = origin_x + column * lattice.step_x + row_offset
            if west <= mercator_x <= east:
                lon, lat = mercator_to_lon_lat(mercator_x, mercator_y)
                yield lattice.path_id(row, column), lon, lat


def integrate_projected_paths(
    metadata: dict[str, Any],
    values: bytes,
    profile: ProductionProfile,
) -> tuple[list[ProjectedPath], dict[str, Any], SeedLattice]:
    feasibility_profile = TileProfile(
        profile.name,
        profile.tile_zoom,
        profile.pixels_per_mercator_unit,
        profile.geometry,
    )
    snapshot = tile_profile_snapshot(feasibility_profile, metadata["grid"])
    lattice = seed_lattice(profile, snapshot)
    sampler = WindGrid(metadata, values)
    geometry = snapshot["geometry"]
    trajectory_bbox = snapshot["trajectory_bbox"]
    sample_margin = 0.35
    dt = geometry["trajectory_seconds"] / geometry["steps"]
    pixels_per_unit = snapshot["distance_pixels_per_mercator_unit"]
    paths: list[ProjectedPath] = []
    tested_seeds = 0
    integration_steps = 0
    started = time.perf_counter()

    for path_id, seed_lon, seed_lat in enumerate_seeds(snapshot, lattice):
        tested_seeds += 1
        lon = seed_lon
        lat = seed_lat
        if (
            lon < trajectory_bbox[0] - sample_margin
            or lon > trajectory_bbox[2] + sample_margin
            or lat < trajectory_bbox[1] - sample_margin
            or lat > trajectory_bbox[3] + sample_margin
        ):
            continue
        clamped_lon = _clamp(lon, trajectory_bbox[0], trajectory_bbox[2])
        clamped_lat = _clamp(lat, trajectory_bbox[1], trajectory_bbox[3])
        seed = sampler.sample(clamped_lon, clamped_lat)
        if seed is None:
            continue
        seed_speed = math.hypot(seed[0], seed[1])
        if seed_speed < 0.25:
            continue

        previous_x = longitude_to_mercator_x(clamped_lon)
        previous_y = latitude_to_mercator_y(clamped_lat)
        points = [(previous_x, previous_y)]
        travelled = 0.0
        for _ in range(int(geometry["steps"])):
            integration_steps += 1
            first = sampler.sample(
                _clamp(lon, trajectory_bbox[0], trajectory_bbox[2]),
                _clamp(lat, trajectory_bbox[1], trajectory_bbox[3]),
            )
            if first is None:
                break
            lon_degree_metres = 111_320.0 * math.cos(math.radians(lat))
            mid_lon = lon + (first[0] * (dt * 0.5)) / lon_degree_metres
            mid_lat = lat + (first[1] * (dt * 0.5)) / 111_320.0
            second = sampler.sample(
                _clamp(mid_lon, trajectory_bbox[0], trajectory_bbox[2]),
                _clamp(mid_lat, trajectory_bbox[1], trajectory_bbox[3]),
            )
            if second is None:
                break
            next_lon = lon + (second[0] * dt) / lon_degree_metres
            next_lat = lat + (second[1] * dt) / 111_320.0
            clamped_next_lon = _clamp(next_lon, trajectory_bbox[0], trajectory_bbox[2])
            clamped_next_lat = _clamp(next_lat, trajectory_bbox[1], trajectory_bbox[3])
            next_x = longitude_to_mercator_x(clamped_next_lon)
            next_y = latitude_to_mercator_y(clamped_next_lat)
            delta_x = (next_x - previous_x) * pixels_per_unit
            delta_y = (next_y - previous_y) * pixels_per_unit
            step_len = math.hypot(delta_x, delta_y)
            if step_len < 0.01:
                break
            if travelled + step_len > geometry["max_len_px"]:
                fraction = max(0.0, (geometry["max_len_px"] - travelled) / step_len)
                points.append(
                    (
                        previous_x + (next_x - previous_x) * fraction,
                        previous_y + (next_y - previous_y) * fraction,
                    )
                )
                break
            points.append((next_x, next_y))
            travelled += step_len
            lon = next_lon
            lat = next_lat
            previous_x = next_x
            previous_y = next_y
            if (
                lon < trajectory_bbox[0] - sample_margin
                or lon > trajectory_bbox[2] + sample_margin
                or lat < trajectory_bbox[1] - sample_margin
                or lat > trajectory_bbox[3] + sample_margin
            ):
                break

        if len(points) >= 2:
            paths.append(ProjectedPath(path_id, seed_speed, tuple(points)))

    return (
        paths,
        {
            "generation_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "tested_seeds": tested_seeds,
            "accepted_paths": len(paths),
            "integration_steps": integration_steps,
            "source_points": sum(len(path.points) for path in paths),
        },
        lattice,
    )


def integrate_projected_paths_vectorized(
    metadata: dict[str, Any],
    values: bytes,
    profile: ProductionProfile,
) -> tuple[list[ProjectedPath], dict[str, Any], SeedLattice]:
    feasibility_profile = TileProfile(
        profile.name,
        profile.tile_zoom,
        profile.pixels_per_mercator_unit,
        profile.geometry,
    )
    snapshot = tile_profile_snapshot(feasibility_profile, metadata["grid"])
    lattice = seed_lattice(profile, snapshot)
    sampler = WindGrid(metadata, values)
    geometry = snapshot["geometry"]
    trajectory_bbox = np.asarray(snapshot["trajectory_bbox"], dtype=np.float64)
    sample_margin = 0.35
    dt = geometry["trajectory_seconds"] / geometry["steps"]
    pixels_per_unit = snapshot["distance_pixels_per_mercator_unit"]
    started = time.perf_counter()

    seeds = list(enumerate_seeds(snapshot, lattice))
    tested_seeds = len(seeds)
    path_ids = np.fromiter((seed[0] for seed in seeds), dtype=np.int64)
    lon = np.fromiter((seed[1] for seed in seeds), dtype=np.float64)
    lat = np.fromiter((seed[2] for seed in seeds), dtype=np.float64)
    within_margin = (
        (lon >= trajectory_bbox[0] - sample_margin)
        & (lon <= trajectory_bbox[2] + sample_margin)
        & (lat >= trajectory_bbox[1] - sample_margin)
        & (lat <= trajectory_bbox[3] + sample_margin)
    )
    path_ids = path_ids[within_margin]
    lon = lon[within_margin]
    lat = lat[within_margin]
    clamped_lon = np.clip(lon, trajectory_bbox[0], trajectory_bbox[2])
    clamped_lat = np.clip(lat, trajectory_bbox[1], trajectory_bbox[3])
    seed_u, seed_v, seed_valid = sampler.sample_batch(clamped_lon, clamped_lat)
    seed_speed = np.hypot(seed_u, seed_v)
    accepted = seed_valid & (seed_speed >= 0.25)
    path_ids = path_ids[accepted]
    lon = lon[accepted]
    lat = lat[accepted]
    clamped_lon = clamped_lon[accepted]
    clamped_lat = clamped_lat[accepted]
    seed_speed = seed_speed[accepted]

    path_count = len(path_ids)
    maximum_points = int(geometry["steps"]) + 1
    coordinates = np.full(
        (path_count, maximum_points, 2),
        np.nan,
        dtype=np.float64,
    )
    previous_x = (clamped_lon + 180.0) / 360.0
    latitude_radians = np.radians(
        np.clip(clamped_lat, -85.0511287798066, 85.0511287798066)
    )
    previous_y = (
        1.0
        - np.log(
            np.tan(latitude_radians)
            + 1.0 / np.cos(latitude_radians)
        )
        / np.pi
    ) / 2.0
    coordinates[:, 0, 0] = previous_x
    coordinates[:, 0, 1] = previous_y
    lengths = np.ones(path_count, dtype=np.int32)
    travelled = np.zeros(path_count, dtype=np.float64)
    active = np.ones(path_count, dtype=bool)
    integration_steps = 0

    def append_points(
        indices: np.ndarray,
        x_values: np.ndarray,
        y_values: np.ndarray,
    ) -> None:
        if len(indices) == 0:
            return
        positions = lengths[indices]
        coordinates[indices, positions, 0] = x_values
        coordinates[indices, positions, 1] = y_values
        lengths[indices] += 1

    for _ in range(int(geometry["steps"])):
        current = np.flatnonzero(active)
        if len(current) == 0:
            break
        integration_steps += len(current)
        first_u, first_v, first_valid = sampler.sample_batch(
            np.clip(lon[current], trajectory_bbox[0], trajectory_bbox[2]),
            np.clip(lat[current], trajectory_bbox[1], trajectory_bbox[3]),
        )
        active[current[~first_valid]] = False
        current = current[first_valid]
        first_u = first_u[first_valid]
        first_v = first_v[first_valid]
        if len(current) == 0:
            continue
        lon_degree_metres = 111_320.0 * np.cos(np.radians(lat[current]))
        mid_lon = lon[current] + (first_u * (dt * 0.5)) / lon_degree_metres
        mid_lat = lat[current] + (first_v * (dt * 0.5)) / 111_320.0
        second_u, second_v, second_valid = sampler.sample_batch(
            np.clip(mid_lon, trajectory_bbox[0], trajectory_bbox[2]),
            np.clip(mid_lat, trajectory_bbox[1], trajectory_bbox[3]),
        )
        active[current[~second_valid]] = False
        current = current[second_valid]
        second_u = second_u[second_valid]
        second_v = second_v[second_valid]
        lon_degree_metres = lon_degree_metres[second_valid]
        if len(current) == 0:
            continue
        next_lon = lon[current] + (second_u * dt) / lon_degree_metres
        next_lat = lat[current] + (second_v * dt) / 111_320.0
        clamped_next_lon = np.clip(
            next_lon, trajectory_bbox[0], trajectory_bbox[2]
        )
        clamped_next_lat = np.clip(
            next_lat, trajectory_bbox[1], trajectory_bbox[3]
        )
        next_x = (clamped_next_lon + 180.0) / 360.0
        next_latitude_radians = np.radians(
            np.clip(clamped_next_lat, -85.0511287798066, 85.0511287798066)
        )
        next_y = (
            1.0
            - np.log(
                np.tan(next_latitude_radians)
                + 1.0 / np.cos(next_latitude_radians)
            )
            / np.pi
        ) / 2.0
        delta_x = (next_x - previous_x[current]) * pixels_per_unit
        delta_y = (next_y - previous_y[current]) * pixels_per_unit
        step_length = np.hypot(delta_x, delta_y)
        stagnant = step_length < 0.01
        active[current[stagnant]] = False
        moving = ~stagnant
        moving_indices = current[moving]
        moving_step_length = step_length[moving]
        moving_next_x = next_x[moving]
        moving_next_y = next_y[moving]
        if len(moving_indices) == 0:
            continue
        exceeds = (
            travelled[moving_indices] + moving_step_length
            > geometry["max_len_px"]
        )
        exceeded_indices = moving_indices[exceeds]
        if len(exceeded_indices):
            fraction = (
                geometry["max_len_px"] - travelled[exceeded_indices]
            ) / moving_step_length[exceeds]
            head_x = previous_x[exceeded_indices] + (
                moving_next_x[exceeds] - previous_x[exceeded_indices]
            ) * fraction
            head_y = previous_y[exceeded_indices] + (
                moving_next_y[exceeds] - previous_y[exceeded_indices]
            ) * fraction
            append_points(exceeded_indices, head_x, head_y)
            active[exceeded_indices] = False

        regular_indices = moving_indices[~exceeds]
        if len(regular_indices):
            regular_x = moving_next_x[~exceeds]
            regular_y = moving_next_y[~exceeds]
            append_points(regular_indices, regular_x, regular_y)
            travelled[regular_indices] += moving_step_length[~exceeds]
            lon[regular_indices] = next_lon[moving][~exceeds]
            lat[regular_indices] = next_lat[moving][~exceeds]
            previous_x[regular_indices] = regular_x
            previous_y[regular_indices] = regular_y
            outside = (
                (lon[regular_indices] < trajectory_bbox[0] - sample_margin)
                | (lon[regular_indices] > trajectory_bbox[2] + sample_margin)
                | (lat[regular_indices] < trajectory_bbox[1] - sample_margin)
                | (lat[regular_indices] > trajectory_bbox[3] + sample_margin)
            )
            active[regular_indices[outside]] = False

    retained = np.flatnonzero(lengths >= 2)
    paths = [
        ProjectedPath(
            path_id=int(path_ids[index]),
            seed_speed_ms=float(seed_speed[index]),
            points=tuple(
                (float(point[0]), float(point[1]))
                for point in coordinates[index, : lengths[index]]
            ),
        )
        for index in retained
    ]
    return (
        paths,
        {
            "generation_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "tested_seeds": tested_seeds,
            "accepted_paths": len(paths),
            "integration_steps": int(integration_steps),
            "source_points": int(sum(len(path.points) for path in paths)),
        },
        lattice,
    )


def _strict_split_fraction(value: float) -> bool:
    return 1e-14 < value < 1.0 - 1e-14


def _segment_tile_splits(
    start: tuple[float, float],
    end: tuple[float, float],
    zoom: int,
) -> list[float]:
    scale = 2**zoom
    start_x = start[0] * scale
    start_y = start[1] * scale
    end_x = end[0] * scale
    end_y = end[1] * scale
    fractions = [0.0, 1.0]
    delta_x = end_x - start_x
    delta_y = end_y - start_y
    if abs(delta_x) > np.finfo(float).eps:
        first = math.floor(min(start_x, end_x)) + 1
        last = math.ceil(max(start_x, end_x)) - 1
        for boundary in range(first, last + 1):
            fraction = (boundary - start_x) / delta_x
            if _strict_split_fraction(fraction):
                fractions.append(fraction)
    if abs(delta_y) > np.finfo(float).eps:
        first = math.floor(min(start_y, end_y)) + 1
        last = math.ceil(max(start_y, end_y)) - 1
        for boundary in range(first, last + 1):
            fraction = (boundary - start_y) / delta_y
            if _strict_split_fraction(fraction):
                fractions.append(fraction)
    return sorted(set(fractions))


def _tile_for_midpoint(
    start: tuple[float, float],
    end: tuple[float, float],
    zoom: int,
) -> tuple[int, int]:
    scale = 2**zoom
    midpoint_x = (start[0] + end[0]) * 0.5
    midpoint_y = (start[1] + end[1]) * 0.5
    x = max(0, min(scale - 1, math.floor(midpoint_x * scale)))
    y = max(0, min(scale - 1, math.floor(midpoint_y * scale)))
    return x, y


def _append_distinct(
    target: list[tuple[float, float]],
    point: tuple[float, float],
) -> None:
    if (
        not target
        or abs(target[-1][0] - point[0]) > 1e-15
        or abs(target[-1][1] - point[1]) > 1e-15
    ):
        target.append(point)


def split_path_into_tiles(
    path: ProjectedPath,
    zoom: int,
) -> list[tuple[tuple[int, int], list[tuple[float, float]]]]:
    pieces: list[tuple[tuple[int, int], list[tuple[float, float]]]] = []
    for index in range(len(path.points) - 1):
        start = path.points[index]
        end = path.points[index + 1]
        delta_x = end[0] - start[0]
        delta_y = end[1] - start[1]
        fractions = _segment_tile_splits(start, end, zoom)
        for split_index in range(len(fractions) - 1):
            lower = fractions[split_index]
            upper = fractions[split_index + 1]
            clipped_start = (start[0] + delta_x * lower, start[1] + delta_y * lower)
            clipped_end = (start[0] + delta_x * upper, start[1] + delta_y * upper)
            tile = _tile_for_midpoint(clipped_start, clipped_end, zoom)
            if pieces and pieces[-1][0] == tile:
                _append_distinct(pieces[-1][1], clipped_start)
                _append_distinct(pieces[-1][1], clipped_end)
            else:
                pieces.append((tile, [clipped_start, clipped_end]))
    return pieces


def _point_segment_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    delta_x = end[0] - start[0]
    delta_y = end[1] - start[1]
    length_squared = delta_x * delta_x + delta_y * delta_y
    if length_squared <= np.finfo(float).eps:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    fraction = _clamp(
        ((point[0] - start[0]) * delta_x + (point[1] - start[1]) * delta_y)
        / length_squared,
        0.0,
        1.0,
    )
    projected_x = start[0] + delta_x * fraction
    projected_y = start[1] + delta_y * fraction
    return math.hypot(point[0] - projected_x, point[1] - projected_y)


def _simplify_points(
    points: Sequence[tuple[float, float]],
    zoom: int,
    tolerance_px: float,
    preserve_terminal_segment: bool,
) -> tuple[tuple[float, float], ...]:
    if len(points) <= 2 or tolerance_px <= 0:
        return tuple(points)
    scale = (2**zoom) * TILE_SIZE
    projected = [
        (point[0] * scale, point[1] * scale)
        for point in points
    ]
    simplify_count = len(points) - 1 if preserve_terminal_segment and len(points) > 2 else len(points)
    keep = {0, simplify_count - 1}
    stack = [(0, simplify_count - 1)]
    while stack:
        start_index, end_index = stack.pop()
        largest_distance = tolerance_px
        largest_index = -1
        for index in range(start_index + 1, end_index):
            distance = _point_segment_distance(
                projected[index],
                projected[start_index],
                projected[end_index],
            )
            if distance > largest_distance:
                largest_distance = distance
                largest_index = index
        if largest_index >= 0:
            keep.add(largest_index)
            stack.append((start_index, largest_index))
            stack.append((largest_index, end_index))
    simplified = [points[index] for index in sorted(keep)]
    if preserve_terminal_segment:
        simplified.append(points[-1])
    return tuple(simplified)


def partition_paths(
    paths: Iterable[ProjectedPath],
    profile: ProductionProfile,
    simplify_tolerance_px: float,
) -> tuple[dict[tuple[int, int], list[TileFragment]], dict[str, Any]]:
    by_tile: dict[tuple[int, int], list[TileFragment]] = defaultdict(list)
    source_paths = 0
    source_points = 0
    fragment_points = 0
    prepartition_simplified_points = 0
    simplified_points = 0
    fragment_count = 0
    for path in paths:
        source_paths += 1
        source_points += len(path.points)
        simplified_path = ProjectedPath(
            path_id=path.path_id,
            seed_speed_ms=path.seed_speed_ms,
            points=_simplify_points(
                path.points,
                profile.tile_zoom,
                simplify_tolerance_px,
                preserve_terminal_segment=True,
            ),
        )
        prepartition_simplified_points += len(simplified_path.points)
        pieces = split_path_into_tiles(simplified_path, profile.tile_zoom)
        for fragment_order, (tile, points) in enumerate(pieces):
            flags = 0
            if fragment_order == 0:
                flags |= ORIGINAL_START
            else:
                flags |= CONTINUES_BEFORE
            if fragment_order == len(pieces) - 1:
                flags |= ORIGINAL_END
            else:
                flags |= CONTINUES_AFTER
            fragment_points += len(points)
            by_tile[tile].append(
                TileFragment(
                    path_id=path.path_id,
                    fragment_order=fragment_order,
                    flags=flags,
                    terminal_speed_ms=path.seed_speed_ms if flags & ORIGINAL_END else 0.0,
                    points=tuple(points),
                )
            )
            fragment_count += 1
            simplified_points += len(points)
    return dict(by_tile), {
        "source_paths": source_paths,
        "source_points": source_points,
        "prepartition_simplified_points": prepartition_simplified_points,
        "fragment_count": fragment_count,
        "fragment_points": fragment_points,
        "simplified_points": simplified_points,
        "simplification_removed_points": source_points
        - prepartition_simplified_points,
        "nonempty_tiles": len(by_tile),
    }


def _append_varint(target: bytearray, value: int) -> None:
    if value < 0:
        raise ValueError("varints cannot encode negative values")
    while value >= 0x80:
        target.append((value & 0x7F) | 0x80)
        value >>= 7
    target.append(value)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while offset < len(data) and shift <= 63:
        value = data[offset]
        offset += 1
        result |= (value & 0x7F) << shift
        if value < 0x80:
            return result, offset
        shift += 7
    raise ValueError("invalid or truncated varint")


def _zigzag(value: int) -> int:
    return (value << 1) ^ (value >> 31)


def _unzigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def _quantize_point(
    point: tuple[float, float],
    quantization_bounds: tuple[float, float, float, float],
) -> tuple[int, int]:
    west_x, north_y, east_x, south_y = quantization_bounds
    qx = round(
        _clamp((point[0] - west_x) / (east_x - west_x), 0.0, 1.0)
        * QUANTIZATION_MAX
    )
    qy = round(
        _clamp((point[1] - north_y) / (south_y - north_y), 0.0, 1.0)
        * QUANTIZATION_MAX
    )
    return qx, qy


def encode_tile(
    fragments: Iterable[TileFragment],
    profile: ProductionProfile,
    tile: tuple[int, int],
    quantization_bounds: tuple[float, float, float, float],
) -> tuple[bytes, dict[str, int]]:
    ordered = sorted(fragments, key=lambda item: (item.path_id, item.fragment_order))
    payload = bytearray()
    point_count = 0
    previous_path_id = 0
    identities: set[tuple[int, int]] = set()
    for index, fragment in enumerate(ordered):
        identity = (fragment.path_id, fragment.fragment_order)
        if identity in identities:
            raise ValueError(f"duplicate fragment identity {identity}")
        identities.add(identity)
        if fragment.flags & ~KNOWN_FRAGMENT_FLAGS:
            raise ValueError("fragment uses unknown flags")
        if bool(fragment.flags & ORIGINAL_START) == bool(
            fragment.flags & CONTINUES_BEFORE
        ):
            raise ValueError("fragment start flags are inconsistent")
        if bool(fragment.flags & ORIGINAL_END) == bool(fragment.flags & CONTINUES_AFTER):
            raise ValueError("fragment end flags are inconsistent")
        if len(fragment.points) < 2:
            raise ValueError("fragment must contain at least two points")
        path_delta = fragment.path_id if index == 0 else fragment.path_id - previous_path_id
        _append_varint(payload, path_delta)
        _append_varint(payload, fragment.fragment_order)
        payload.append(fragment.flags)
        _append_varint(payload, len(fragment.points))
        if fragment.flags & ORIGINAL_END:
            _append_varint(payload, round(max(0.0, fragment.terminal_speed_ms) * 100.0))
        quantized = [
            _quantize_point(point, quantization_bounds) for point in fragment.points
        ]
        previous_x, previous_y = quantized[0]
        _append_varint(payload, previous_x)
        _append_varint(payload, previous_y)
        for qx, qy in quantized[1:]:
            _append_varint(payload, _zigzag(qx - previous_x))
            _append_varint(payload, _zigzag(qy - previous_y))
            previous_x = qx
            previous_y = qy
        previous_path_id = fragment.path_id
        point_count += len(quantized)
    crc = zlib.crc32(payload) & 0xFFFFFFFF
    header = HEADER.pack(
        MAGIC,
        VERSION,
        HEADER_FLAGS,
        HEADER.size,
        profile.id,
        profile.tile_zoom,
        0,
        tile[0],
        tile[1],
        len(ordered),
        point_count,
        crc,
    )
    return header + payload, {
        "fragment_count": len(ordered),
        "point_count": point_count,
        "payload_crc32": crc,
    }


def decode_tile(
    data: bytes,
    profile: ProductionProfile,
    expected_tile: tuple[int, int],
) -> DecodedTile:
    if len(data) < HEADER.size:
        raise ValueError("XWS2 tile is shorter than its header")
    if len(data) > MAX_TILE_BYTES:
        raise ValueError("XWS2 tile exceeds its byte limit")
    (
        magic,
        version,
        header_flags,
        header_size,
        profile_id,
        zoom,
        reserved,
        x,
        y,
        fragment_count,
        point_count,
        payload_crc32,
    ) = HEADER.unpack_from(data)
    if (
        magic != MAGIC
        or version != VERSION
        or header_flags != HEADER_FLAGS
        or header_size != HEADER.size
        or reserved != 0
    ):
        raise ValueError("unsupported XWS2 tile header")
    if profile_id != profile.id or zoom != profile.tile_zoom:
        raise ValueError("XWS2 tile profile identity does not match")
    if (x, y) != expected_tile:
        raise ValueError("XWS2 tile coordinate identity does not match")
    if fragment_count > MAX_TILE_FRAGMENTS or point_count > MAX_TILE_POINTS:
        raise ValueError("XWS2 tile declared counts exceed limits")
    payload = data[header_size:]
    if zlib.crc32(payload) & 0xFFFFFFFF != payload_crc32:
        raise ValueError("XWS2 payload CRC32 does not match")
    offset = 0
    previous_path_id = 0
    decoded_points = 0
    fragments: list[DecodedFragment] = []
    identities: set[tuple[int, int]] = set()
    for index in range(fragment_count):
        path_delta, offset = _read_varint(payload, offset)
        path_id = path_delta if index == 0 else previous_path_id + path_delta
        fragment_order, offset = _read_varint(payload, offset)
        if offset >= len(payload):
            raise ValueError("XWS2 fragment flags are truncated")
        flags = payload[offset]
        offset += 1
        if flags & ~KNOWN_FRAGMENT_FLAGS:
            raise ValueError("XWS2 fragment uses unknown flags")
        if bool(flags & ORIGINAL_START) == bool(flags & CONTINUES_BEFORE):
            raise ValueError("XWS2 fragment start flags are inconsistent")
        if bool(flags & ORIGINAL_END) == bool(flags & CONTINUES_AFTER):
            raise ValueError("XWS2 fragment end flags are inconsistent")
        count, offset = _read_varint(payload, offset)
        if count < 2 or decoded_points + count > point_count:
            raise ValueError("XWS2 fragment point count is inconsistent")
        terminal_speed = 0.0
        if flags & ORIGINAL_END:
            speed_centi_ms, offset = _read_varint(payload, offset)
            terminal_speed = speed_centi_ms / 100.0
        qx, offset = _read_varint(payload, offset)
        qy, offset = _read_varint(payload, offset)
        if qx > QUANTIZATION_MAX or qy > QUANTIZATION_MAX:
            raise ValueError("XWS2 coordinate exceeds its quantization domain")
        points = [(qx, qy)]
        for _ in range(count - 1):
            dx, offset = _read_varint(payload, offset)
            dy, offset = _read_varint(payload, offset)
            qx += _unzigzag(dx)
            qy += _unzigzag(dy)
            if not (0 <= qx <= QUANTIZATION_MAX and 0 <= qy <= QUANTIZATION_MAX):
                raise ValueError("XWS2 coordinate exceeds its quantization domain")
            points.append((qx, qy))
        identity = (path_id, fragment_order)
        if identity in identities:
            raise ValueError("XWS2 tile contains a duplicate fragment identity")
        identities.add(identity)
        fragments.append(
            DecodedFragment(
                path_id=path_id,
                fragment_order=fragment_order,
                flags=flags,
                terminal_speed_ms=terminal_speed,
                points=tuple(points),
            )
        )
        previous_path_id = path_id
        decoded_points += count
    if decoded_points != point_count or offset != len(payload):
        raise ValueError("XWS2 counts or trailing bytes do not match")
    return DecodedTile(
        profile_id=profile_id,
        zoom=zoom,
        x=x,
        y=y,
        fragments=tuple(fragments),
        point_count=decoded_points,
    )


def _quantization_bounds(metadata: dict[str, Any]) -> tuple[float, float, float, float]:
    west, south, east, north = grid_bbox(metadata["grid"])
    return (
        longitude_to_mercator_x(west),
        latitude_to_mercator_y(north),
        longitude_to_mercator_x(east),
        latitude_to_mercator_y(south),
    )


def _step_record(
    metadata_path: Path,
    step_label: str,
    output_root: Path,
    profile_names: Sequence[str],
    simplify_tolerance_px: float,
    integration_mode: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    timer = PhaseTimer()
    total_started = time.perf_counter()
    metadata_path = Path(metadata_path).resolve()
    with timer.measure("source_read_ms"):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        step_path = resolve_step_path(metadata_path, metadata, step_label)
        values = step_path.read_bytes()
    quantization_bounds = _quantization_bounds(metadata)
    profile_records: dict[str, Any] = {}
    profile_metrics: dict[str, Any] = {}

    for profile_name in profile_names:
        profile = PROFILES[profile_name]
        with timer.measure(f"{profile_name}_integrate_ms"):
            integrator = (
                integrate_projected_paths_vectorized
                if integration_mode == "vectorized"
                else integrate_projected_paths
            )
            paths, integration, lattice = integrator(metadata, values, profile)
        with timer.measure(f"{profile_name}_partition_ms"):
            by_tile, partition = partition_paths(
                paths,
                profile,
                simplify_tolerance_px,
            )
        tile_records: list[dict[str, Any]] = []
        encode_started = time.perf_counter()
        gzip_ms = 0.0
        for tile, fragments in sorted(by_tile.items()):
            bundle, encoding = encode_tile(
                fragments,
                profile,
                tile,
                quantization_bounds,
            )
            decoded = decode_tile(bundle, profile, tile)
            if (
                len(decoded.fragments) != encoding["fragment_count"]
                or decoded.point_count != encoding["point_count"]
            ):
                raise RuntimeError("XWS2 tile round trip changed declared counts")
            relative_path = (
                Path("profiles")
                / profile.name
                / step_label
                / f"z{profile.tile_zoom}"
                / f"{tile[0]}_{tile[1]}.xws"
            )
            tile_path = output_root / relative_path
            tile_path.parent.mkdir(parents=True, exist_ok=True)
            tile_path.write_bytes(bundle)
            gzip_started = time.perf_counter()
            gzip_bytes = len(gzip.compress(bundle, compresslevel=9, mtime=0))
            gzip_ms += (time.perf_counter() - gzip_started) * 1000.0
            tile_records.append(
                {
                    "x": tile[0],
                    "y": tile[1],
                    "path": relative_path.as_posix(),
                    "bytes": len(bundle),
                    "gzip_bytes": gzip_bytes,
                    "sha256": _sha256_bytes(bundle),
                    **encoding,
                }
            )
        timer.values[f"{profile_name}_encode_write_ms"] = round(
            (time.perf_counter() - encode_started) * 1000.0 - gzip_ms,
            3,
        )
        timer.values[f"{profile_name}_gzip_measure_ms"] = round(gzip_ms, 3)
        profile_records[profile.name] = {
            "profile": _profile_payload(profile, lattice),
            "quantization": {
                "coordinate_system": "normalized_web_mercator",
                "maximum": QUANTIZATION_MAX,
                "bounds": list(quantization_bounds),
            },
            "tiles": tile_records,
            "total_bytes": sum(tile["bytes"] for tile in tile_records),
            "total_gzip_bytes": sum(tile["gzip_bytes"] for tile in tile_records),
        }
        profile_metrics[profile.name] = {
            "integration": integration,
            "partition": partition,
        }

    record = {
        "step": step_label,
        "source_bytes": len(values),
        "source_sha256": _sha256_bytes(values),
        "integration_mode": integration_mode,
        "profiles": profile_records,
    }
    metrics = {
        "step": step_label,
        "worker_pid": os.getpid(),
        "phase_ms": timer.values,
        "profiles": profile_metrics,
        "peak_rss_bytes": _peak_rss_bytes(),
        "total_ms": round((time.perf_counter() - total_started) * 1000.0, 3),
    }
    return record, metrics


def generate_step_package(
    metadata_path: Path,
    step_label: str,
    output_root: Path,
    *,
    profile_names: Sequence[str] = DEFAULT_PROFILE_NAMES,
    simplify_tolerance_px: float = 0.50,
    integration_mode: str = "vectorized",
) -> tuple[dict[str, Any], dict[str, Any]]:
    unknown = sorted(set(profile_names) - set(PROFILES))
    if unknown:
        raise ValueError(f"unknown XWS2 profile(s): {', '.join(unknown)}")
    if integration_mode not in {"scalar", "vectorized"}:
        raise ValueError("XWS2 integration mode must be scalar or vectorized")
    return _step_record(
        Path(metadata_path),
        step_label,
        Path(output_root),
        tuple(profile_names),
        simplify_tolerance_px,
        integration_mode,
    )


def _worker_generate_step(
    arguments: tuple[str, str, str, tuple[str, ...], float, str],
):
    (
        metadata_path,
        step_label,
        output_root,
        profile_names,
        tolerance,
        integration_mode,
    ) = arguments
    return generate_step_package(
        Path(metadata_path),
        step_label,
        Path(output_root),
        profile_names=profile_names,
        simplify_tolerance_px=tolerance,
        integration_mode=integration_mode,
    )


def _selected_steps(metadata: dict[str, Any], requested: Sequence[str] | None) -> list[str]:
    available = [str(step["step"]) for step in metadata.get("steps", [])]
    if requested is None:
        return available
    unknown = sorted(set(requested) - set(available))
    if unknown:
        raise ValueError(f"unknown Wind step(s): {', '.join(unknown)}")
    selected = set(requested)
    return [step for step in available if step in selected]


def _assert_owned_build_path(path: Path, parent: Path) -> None:
    resolved = path.resolve()
    resolved_parent = parent.resolve()
    if resolved.parent != resolved_parent or not resolved.name.startswith(
        ".wind-streamline-shadow-"
    ):
        raise ValueError(f"refusing to remove unowned shadow build path {resolved}")


def expected_complete_pilot_steps(model: str, run: str) -> list[str]:
    """Return the complete published timeline for a supported pilot run."""
    count = expected_horizon_count(model, run)
    digits = 2 if model in {"ch1", "icon-ch1"} else 3
    return [f"H{index:0{digits}d}" for index in range(count)]


def validate_shadow_package(
    package_directory: Path,
    *,
    require_complete_pilot: bool = False,
) -> dict[str, Any]:
    package_directory = Path(package_directory).resolve()
    manifest_path = package_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("contract") != CONTRACT
        or manifest.get("contract_version") != CONTRACT_VERSION
        or manifest.get("package") != PACKAGE
        or manifest.get("generator_revision") != GENERATOR_REVISION
        or manifest.get("manifest_layout") != MANIFEST_LAYOUT
        or manifest.get("integration_mode") not in {"scalar", "vectorized"}
    ):
        raise ValueError("XWS2 package identity is unsupported")
    source = manifest.get("source") or {}
    source_level = source.get("level") or {}
    run = str(source.get("run") or "")
    if (
        source.get("model") != "icon-ch1"
        or source_level.get("name") != "800m_AGL"
        or len(run) != 13
        or run[8] != "_"
        or not (run[:8] + run[9:]).isdigit()
    ):
        raise ValueError("XWS2 package source is outside the beta2 pilot")
    profile_names = manifest.get("profile_names")
    if profile_names != list(DEFAULT_PROFILE_NAMES):
        raise ValueError("XWS2 package profile set is unsupported")
    step_descriptors = manifest.get("steps")
    if not isinstance(step_descriptors, list) or not step_descriptors:
        raise ValueError("XWS2 package contains no steps")
    step_labels = [
        descriptor.get("step")
        for descriptor in step_descriptors
        if isinstance(descriptor, dict)
    ]
    if (
        len(step_labels) != len(step_descriptors)
        or any(not isinstance(step, str) or not step for step in step_labels)
        or len(set(step_labels)) != len(step_labels)
    ):
        raise ValueError("XWS2 package steps are invalid or duplicated")
    if require_complete_pilot:
        expected_steps = expected_complete_pilot_steps(str(source["model"]), run)
        if step_labels != expected_steps:
            raise ValueError(
                "XWS2 beta2 pilot requires the complete "
                f"{expected_steps[0]}-{expected_steps[-1]} timeline"
            )

    revision = manifest.get("revision")
    revision_sha256 = manifest.get("revision_sha256")
    if (
        not isinstance(revision, str)
        or len(revision) != 16
        or not isinstance(revision_sha256, str)
        or len(revision_sha256) != 64
        or not revision_sha256.startswith(revision)
    ):
        raise ValueError("XWS2 package revision identity is malformed")

    declared_step_paths: set[str] = set()
    steps: list[dict[str, Any]] = []
    for descriptor, step_label in zip(step_descriptors, step_labels):
        relative = PurePosixPath(str(descriptor.get("path") or ""))
        expected_path = PurePosixPath("steps", f"{step_label}.json")
        if (
            relative != expected_path
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            raise ValueError("XWS2 step manifest path does not match its identity")
        relative_text = relative.as_posix()
        if relative_text in declared_step_paths:
            raise ValueError("XWS2 package contains a duplicate step manifest path")
        declared_step_paths.add(relative_text)
        step_path = package_directory.joinpath(*relative.parts)
        try:
            step_bytes = step_path.read_bytes()
        except FileNotFoundError as error:
            raise ValueError(
                f"XWS2 package is missing step manifest {relative_text}"
            ) from error
        if (
            len(step_bytes) != descriptor.get("bytes")
            or _sha256_bytes(step_bytes) != descriptor.get("sha256")
            or len(gzip.compress(step_bytes, compresslevel=9, mtime=0))
            != descriptor.get("gzip_bytes")
        ):
            raise ValueError(f"XWS2 step manifest bytes do not match {relative_text}")
        try:
            step_document = json.loads(step_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"XWS2 step manifest is invalid {relative_text}") from error
        if (
            step_document.get("contract") != CONTRACT
            or step_document.get("contract_version") != CONTRACT_VERSION
            or step_document.get("package") != PACKAGE
            or step_document.get("generator_revision") != GENERATOR_REVISION
            or step_document.get("revision") != revision
        ):
            raise ValueError(f"XWS2 step manifest identity is invalid {relative_text}")
        step_record = step_document.get("step")
        if not isinstance(step_record, dict) or step_record.get("step") != step_label:
            raise ValueError(f"XWS2 step manifest record is invalid {relative_text}")
        steps.append(step_record)

    actual_step_paths = {
        path.relative_to(package_directory).as_posix()
        for path in package_directory.glob("steps/*.json")
    }
    if actual_step_paths != declared_step_paths:
        raise ValueError("XWS2 package contains missing or undeclared step manifests")

    content = {
        "contract": manifest.get("contract"),
        "contract_version": manifest.get("contract_version"),
        "package": manifest.get("package"),
        "generator_revision": manifest.get("generator_revision"),
        "manifest_layout": manifest.get("manifest_layout"),
        "source": manifest.get("source"),
        "profile_names": manifest.get("profile_names"),
        "simplification_tolerance_px": manifest.get(
            "simplification_tolerance_px"
        ),
        "integration_mode": manifest.get("integration_mode"),
        "steps": steps,
    }
    revision_sha256 = _sha256_bytes(_canonical_json_bytes(content))
    if (
        manifest.get("revision_sha256") != revision_sha256
        or manifest.get("revision") != revision_sha256[:16]
    ):
        raise ValueError("XWS2 package revision identity does not match its content")

    actual_counts = {
        "steps": len(steps),
        "profiles": len(profile_names),
        "tiles": 0,
        "bytes": 0,
        "gzip_bytes": 0,
    }
    declared_paths: set[str] = set()
    for step in steps:
        step_label = step["step"]
        profiles = step.get("profiles")
        if (
            not isinstance(profiles, dict)
            or len(profiles) != len(profile_names)
            or set(profiles) != set(profile_names)
        ):
            raise ValueError(f"XWS2 {step_label} profile set is invalid")
        for profile_name in profile_names:
            profile = PROFILES[profile_name]
            profile_record = profiles[profile_name]
            profile_identity = profile_record.get("profile") or {}
            if (
                profile_identity.get("id") != profile.id
                or profile_identity.get("name") != profile.name
                or profile_identity.get("tile_zoom") != profile.tile_zoom
                or profile_identity.get("renderer_revision") != GENERATOR_REVISION
            ):
                raise ValueError(
                    f"XWS2 {step_label}/{profile_name} profile identity is invalid"
                )
            tiles = profile_record.get("tiles")
            if not isinstance(tiles, list):
                raise ValueError(f"XWS2 {step_label}/{profile_name} tiles are invalid")
            profile_bytes = 0
            profile_gzip_bytes = 0
            for record in tiles:
                relative = PurePosixPath(str(record.get("path") or ""))
                if (
                    relative.is_absolute()
                    or not relative.parts
                    or any(part in {"", ".", ".."} for part in relative.parts)
                ):
                    raise ValueError("XWS2 package contains an unsafe tile path")
                expected_prefix = (
                    "profiles",
                    profile_name,
                    step_label,
                    f"z{profile.tile_zoom}",
                )
                if relative.parts[:4] != expected_prefix or relative.suffix != ".xws":
                    raise ValueError("XWS2 tile path does not match its identity")
                relative_text = relative.as_posix()
                if relative_text in declared_paths:
                    raise ValueError("XWS2 package contains a duplicate tile path")
                declared_paths.add(relative_text)
                tile_path = package_directory.joinpath(*relative.parts)
                try:
                    payload = tile_path.read_bytes()
                except FileNotFoundError as error:
                    raise ValueError(
                        f"XWS2 package is missing declared tile {relative_text}"
                    ) from error
                if (
                    len(payload) != record.get("bytes")
                    or _sha256_bytes(payload) != record.get("sha256")
                ):
                    raise ValueError(f"XWS2 tile bytes do not match {relative_text}")
                decoded = decode_tile(
                    payload,
                    profile,
                    (record.get("x"), record.get("y")),
                )
                header = HEADER.unpack_from(payload)
                if (
                    len(decoded.fragments) != record.get("fragment_count")
                    or decoded.point_count != record.get("point_count")
                    or header[-1] != record.get("payload_crc32")
                ):
                    raise ValueError(f"XWS2 tile counts do not match {relative_text}")
                gzip_bytes = len(gzip.compress(payload, compresslevel=9, mtime=0))
                if gzip_bytes != record.get("gzip_bytes"):
                    raise ValueError(f"XWS2 gzip measurement does not match {relative_text}")
                profile_bytes += len(payload)
                profile_gzip_bytes += gzip_bytes
            if (
                profile_bytes != profile_record.get("total_bytes")
                or profile_gzip_bytes != profile_record.get("total_gzip_bytes")
            ):
                raise ValueError(
                    f"XWS2 {step_label}/{profile_name} aggregate bytes do not match"
                )
            actual_counts["tiles"] += len(tiles)
            actual_counts["bytes"] += profile_bytes
            actual_counts["gzip_bytes"] += profile_gzip_bytes

    actual_tile_paths = {
        path.relative_to(package_directory).as_posix()
        for path in package_directory.glob("profiles/**/*.xws")
    }
    if actual_tile_paths != declared_paths:
        raise ValueError("XWS2 package contains missing or undeclared tile files")
    if manifest.get("counts") != actual_counts:
        raise ValueError("XWS2 package aggregate counts do not match")
    return {
        "counts": actual_counts,
        "level": source_level["name"],
        "model": source["model"],
        "revision": manifest["revision"],
        "revision_sha256": revision_sha256,
        "run": run,
    }


def build_shadow_package(
    metadata_path: Path,
    output_directory: Path,
    *,
    steps: Sequence[str] | None = None,
    profile_names: Sequence[str] = DEFAULT_PROFILE_NAMES,
    simplify_tolerance_px: float = 0.50,
    workers: int = 1,
    integration_mode: str = "vectorized",
) -> dict[str, Any]:
    metadata_path = Path(metadata_path).resolve()
    output_directory = Path(output_directory).resolve()
    if output_directory.exists():
        raise FileExistsError(f"shadow output already exists: {output_directory}")
    if workers < 1 or workers > 6:
        raise ValueError("XWS2 shadow workers must be between 1 and 6")
    if not profile_names:
        raise ValueError("XWS2 shadow selection contains no profiles")
    if integration_mode not in {"scalar", "vectorized"}:
        raise ValueError("XWS2 integration mode must be scalar or vectorized")
    unknown_profiles = sorted(set(profile_names) - set(PROFILES))
    if unknown_profiles:
        raise ValueError(f"unknown XWS2 profile(s): {', '.join(unknown_profiles)}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    selected = _selected_steps(metadata, steps)
    if not selected:
        raise ValueError("XWS2 shadow selection contains no forecast steps")
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    build_root = Path(
        tempfile.mkdtemp(
            prefix=".wind-streamline-shadow-",
            dir=output_directory.parent,
        )
    )
    started = time.perf_counter()
    try:
        arguments = [
            (
                str(metadata_path),
                step,
                str(build_root),
                tuple(profile_names),
                simplify_tolerance_px,
                integration_mode,
            )
            for step in selected
        ]
        results: list[tuple[dict[str, Any], dict[str, Any]]]
        if workers == 1:
            results = [_worker_generate_step(argument) for argument in arguments]
        else:
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=min(workers, len(arguments))
            ) as executor:
                futures = [executor.submit(_worker_generate_step, argument) for argument in arguments]
                try:
                    results = [future.result() for future in futures]
                except BaseException:
                    for future in futures:
                        future.cancel()
                    raise
        by_step = {record["step"]: (record, metrics) for record, metrics in results}
        ordered_records = [by_step[step][0] for step in selected]
        ordered_metrics = [by_step[step][1] for step in selected]
        content = {
            "contract": CONTRACT,
            "contract_version": CONTRACT_VERSION,
            "package": PACKAGE,
            "generator_revision": GENERATOR_REVISION,
            "manifest_layout": MANIFEST_LAYOUT,
            "source": {
                "model": metadata.get("model"),
                "run": metadata.get("run"),
                "level": metadata.get("level"),
                "grid": metadata.get("grid"),
                "encoding": metadata.get("encoding"),
            },
            "profile_names": list(profile_names),
            "simplification_tolerance_px": simplify_tolerance_px,
            "integration_mode": integration_mode,
            "steps": ordered_records,
        }
        revision_sha256 = _sha256_bytes(_canonical_json_bytes(content))
        revision = revision_sha256[:16]
        step_descriptors = []
        step_manifest_directory = build_root / "steps"
        step_manifest_directory.mkdir(parents=True, exist_ok=True)
        for record in ordered_records:
            step_label = record["step"]
            step_document = {
                "contract": CONTRACT,
                "contract_version": CONTRACT_VERSION,
                "generator_revision": GENERATOR_REVISION,
                "package": PACKAGE,
                "revision": revision,
                "step": record,
            }
            step_bytes = _json_artifact_bytes(step_document)
            step_path = f"steps/{step_label}.json"
            (build_root / step_path).write_bytes(step_bytes)
            step_descriptors.append(
                {
                    "bytes": len(step_bytes),
                    "gzip_bytes": len(
                        gzip.compress(step_bytes, compresslevel=9, mtime=0)
                    ),
                    "path": step_path,
                    "sha256": _sha256_bytes(step_bytes),
                    "step": step_label,
                }
            )
        manifest = {
            **{key: value for key, value in content.items() if key != "steps"},
            "steps": step_descriptors,
            "revision": revision,
            "revision_sha256": revision_sha256,
            "counts": {
                "steps": len(ordered_records),
                "profiles": len(profile_names),
                "tiles": sum(
                    len(profile["tiles"])
                    for step in ordered_records
                    for profile in step["profiles"].values()
                ),
                "bytes": sum(
                    profile["total_bytes"]
                    for step in ordered_records
                    for profile in step["profiles"].values()
                ),
                "gzip_bytes": sum(
                    profile["total_gzip_bytes"]
                    for step in ordered_records
                    for profile in step["profiles"].values()
                ),
            },
        }
        peak_rss_by_worker: dict[int, int] = {}
        for metric in ordered_metrics:
            worker_pid = int(metric["worker_pid"])
            peak_rss_by_worker[worker_pid] = max(
                peak_rss_by_worker.get(worker_pid, 0),
                int(metric["peak_rss_bytes"]),
            )
        benchmark = {
            "contract": CONTRACT,
            "contract_version": CONTRACT_VERSION,
            "revision": manifest["revision"],
            "workers": workers,
            "machine": {
                "cpu_count": os.cpu_count(),
                "platform": platform.platform(),
                "python": platform.python_version(),
                "numpy": np.__version__,
            },
            "native_thread_limits": {
                name: os.environ.get(name)
                for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")
            },
            "steps": ordered_metrics,
            "worker_peak_rss_bytes": {
                str(pid): peak_rss_by_worker[pid] for pid in sorted(peak_rss_by_worker)
            },
            "aggregate_worker_peak_rss_bytes": sum(peak_rss_by_worker.values()),
            "maximum_worker_peak_rss_bytes": max(peak_rss_by_worker.values()),
            "wall_ms": round((time.perf_counter() - started) * 1000.0, 3),
        }
        (build_root / "manifest.json").write_bytes(_json_artifact_bytes(manifest))
        (build_root / "benchmark.json").write_bytes(_json_artifact_bytes(benchmark))
        os.replace(build_root, output_directory)
        return {"manifest": manifest, "benchmark": benchmark}
    finally:
        if build_root.exists():
            _assert_owned_build_path(build_root, output_directory.parent)
            shutil.rmtree(build_root)
