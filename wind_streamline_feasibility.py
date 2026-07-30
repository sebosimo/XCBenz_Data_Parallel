"""Experimental precomputed Wind streamline bundle support.

This module is deliberately not connected to the production export pipeline.
It exists to measure whether backend-integrated, browser-rendered streamline
geometry is small and fast enough to justify a versioned vector-tile contract.
"""

from __future__ import annotations

import gzip
import json
import math
import struct
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np


MAGIC = b"XWS1"
VERSION = 1
HEADER = struct.Struct("<4sBBHII4d")
QUANTIZATION_MAX = 65_535
REFERENCE_BBOX = (5.5, 45.5, 11.0, 48.2)
MAX_MERCATOR_LATITUDE = 85.0511287798066


@dataclass(frozen=True)
class Presentation:
    name: str
    width: int
    height: int
    responsive_mode: str
    bbox: tuple[float, float, float, float] = REFERENCE_BBOX
    overscan: float = 0.25


PRESENTATIONS = {
    "desktop": Presentation("desktop", 1024, 640, "desktop"),
    "mobile": Presentation("mobile", 411, 520, "phone-portrait"),
    "desktop-view": Presentation("desktop-view", 1024, 640, "desktop", overscan=0.0),
    "mobile-view": Presentation("mobile-view", 411, 520, "phone-portrait", overscan=0.0),
}


@dataclass(frozen=True)
class Geometry:
    dx_px: float
    dy_px: float
    line_width: float
    max_len_px: float
    steps: int
    stroke_opacity: float
    trajectory_seconds: float
    uses_wide_spacing: bool


@dataclass(frozen=True)
class PathGeometry:
    seed_speed_ms: float
    points: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class TileProfile:
    name: str
    tile_zoom: int
    pixels_per_mercator_unit: float
    geometry: Geometry


TILE_PROFILES = {
    "compact-overview": TileProfile(
        "compact-overview",
        5,
        9_500.0,
        Geometry(10.56, 8.8704, 0.5, 186.0, 52, 0.72, 4_099.0625, False),
    ),
    "compact-regional": TileProfile(
        "compact-regional",
        6,
        27_000.0,
        Geometry(10.56, 8.8704, 0.5, 186.0, 52, 0.72, 4_099.0625, False),
    ),
    "wide-overview": TileProfile(
        "wide-overview",
        5,
        20_000.0,
        Geometry(17.6, 14.3, 0.62, 155.0, 32, 0.84, 2_100.0, True),
    ),
    "wide-regional": TileProfile(
        "wide-regional",
        7,
        60_000.0,
        Geometry(17.6, 14.3, 0.62, 155.0, 32, 0.84, 2_100.0, True),
    ),
}
DEFAULT_TILE_PROFILES = (
    TILE_PROFILES["compact-overview"],
    TILE_PROFILES["compact-regional"],
    TILE_PROFILES["wide-overview"],
    TILE_PROFILES["wide-regional"],
)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def longitude_to_mercator_x(lon: float) -> float:
    return (lon + 180.0) / 360.0


def latitude_to_mercator_y(lat: float) -> float:
    safe_lat = _clamp(lat, -MAX_MERCATOR_LATITUDE, MAX_MERCATOR_LATITUDE)
    radians = math.radians(safe_lat)
    return (1.0 - math.log(math.tan(radians) + 1.0 / math.cos(radians)) / math.pi) / 2.0


def lon_lat_to_mercator(lon: float, lat: float) -> tuple[float, float]:
    return longitude_to_mercator_x(lon), latitude_to_mercator_y(lat)


def mercator_to_lon_lat(x: float, y: float) -> tuple[float, float]:
    return x * 360.0 - 180.0, math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y))))


def bbox_mercator_bounds(
    bbox: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    west, north = lon_lat_to_mercator(bbox[0], bbox[3])
    east, south = lon_lat_to_mercator(bbox[2], bbox[1])
    return west, north, east, south


def _bbox_zoom_ratio(
    reference: tuple[float, float, float, float],
    bbox: tuple[float, float, float, float],
) -> float:
    reference_width = reference[2] - reference[0]
    reference_height = reference[3] - reference[1]
    width = max(np.finfo(float).eps, bbox[2] - bbox[0])
    height = max(np.finfo(float).eps, bbox[3] - bbox[1])
    return min(reference_width / width, reference_height / height)


def presentation_geometry(presentation: Presentation) -> Geometry:
    css_width = presentation.width
    uses_wide_spacing = css_width >= 760
    uses_compact_stroke = presentation.responsive_mode != "desktop"
    base_line_width = 0.5 if uses_compact_stroke else 0.62
    stroke_opacity = 0.72 if uses_compact_stroke else 0.84
    mobile_width_factor = _clamp((css_width - 320.0) / 160.0, 0.0, 1.0)
    mobile_target_columns = 38.0 + mobile_width_factor * 8.0
    mobile_dx_px = _clamp(presentation.width / mobile_target_columns, 7.8, 9.6)
    zoom_ratio = max(1.0, _bbox_zoom_ratio(REFERENCE_BBOX, presentation.bbox))
    zoom_fade = _clamp((zoom_ratio - 1.18) / 4.2, 0.0, 1.0)
    spacing_scale = 1.0 + zoom_fade * 1.45
    length_scale = 1.0 + zoom_fade * 1.25
    return Geometry(
        dx_px=(16.0 if uses_wide_spacing else mobile_dx_px) * spacing_scale,
        dy_px=(13.0 if uses_wide_spacing else mobile_dx_px * 0.84) * spacing_scale,
        line_width=base_line_width + zoom_fade * (0.06 if uses_wide_spacing else 0.05),
        max_len_px=(155.0 if uses_wide_spacing else 186.0) / length_scale,
        steps=32 if uses_wide_spacing else 52,
        stroke_opacity=stroke_opacity,
        trajectory_seconds=(
            2100.0 if uses_wide_spacing else 3900.0 + mobile_width_factor * 350.0
        )
        / length_scale,
        uses_wide_spacing=uses_wide_spacing,
    )


def presentation_snapshot(presentation: Presentation, grid: dict[str, Any]) -> dict[str, Any]:
    visible_bounds = bbox_mercator_bounds(presentation.bbox)
    west, north, east, south = visible_bounds
    width = east - west
    height = south - north
    overscan = max(0.0, presentation.overscan)
    draw_bounds = (
        west - width * overscan,
        north - height * overscan,
        east + width * overscan,
        south + height * overscan,
    )
    geometry = presentation_geometry(presentation)
    pixels_per_mercator_unit = (
        presentation.width / max(np.finfo(float).eps, width)
        + presentation.height / max(np.finfo(float).eps, height)
    ) / 2.0
    lon_end = float(grid["lon"]["start"]) + float(grid["lon"]["step"]) * (int(grid["width"]) - 1)
    lat_end = float(grid["lat"]["start"]) + float(grid["lat"]["step"]) * (int(grid["height"]) - 1)
    trajectory_bbox = (
        min(float(grid["lon"]["start"]), lon_end),
        min(float(grid["lat"]["start"]), lat_end),
        max(float(grid["lon"]["start"]), lon_end),
        max(float(grid["lat"]["start"]), lat_end),
    )
    return {
        "bbox": list(presentation.bbox),
        "css_width": presentation.width,
        "css_height": presentation.height,
        "draw_width": presentation.width * (1.0 + overscan * 2.0),
        "draw_height": presentation.height * (1.0 + overscan * 2.0),
        "draw_bounds": {
            "west_x": draw_bounds[0],
            "north_y": draw_bounds[1],
            "east_x": draw_bounds[2],
            "south_y": draw_bounds[3],
        },
        "distance_origin": list(
            lon_lat_to_mercator(float(grid["lon"]["start"]), float(grid["lat"]["start"]))
        ),
        "distance_pixels_per_mercator_unit": pixels_per_mercator_unit,
        "geometry": asdict(geometry),
        "responsive_mode": presentation.responsive_mode,
        "trajectory_bbox": list(trajectory_bbox),
    }


def grid_bbox(grid: dict[str, Any]) -> tuple[float, float, float, float]:
    lon_end = float(grid["lon"]["start"]) + float(grid["lon"]["step"]) * (
        int(grid["width"]) - 1
    )
    lat_end = float(grid["lat"]["start"]) + float(grid["lat"]["step"]) * (
        int(grid["height"]) - 1
    )
    return (
        min(float(grid["lon"]["start"]), lon_end),
        min(float(grid["lat"]["start"]), lat_end),
        max(float(grid["lon"]["start"]), lon_end),
        max(float(grid["lat"]["start"]), lat_end),
    )


def tile_profile_snapshot(profile: TileProfile, grid: dict[str, Any]) -> dict[str, Any]:
    trajectory_bbox = grid_bbox(grid)
    west, north, east, south = bbox_mercator_bounds(trajectory_bbox)
    return {
        "bbox": list(trajectory_bbox),
        "css_width": (east - west) * profile.pixels_per_mercator_unit,
        "css_height": (south - north) * profile.pixels_per_mercator_unit,
        "draw_width": (east - west) * profile.pixels_per_mercator_unit,
        "draw_height": (south - north) * profile.pixels_per_mercator_unit,
        "draw_bounds": {
            "west_x": west,
            "north_y": north,
            "east_x": east,
            "south_y": south,
        },
        "distance_origin": list(
            lon_lat_to_mercator(float(grid["lon"]["start"]), float(grid["lat"]["start"]))
        ),
        "distance_pixels_per_mercator_unit": profile.pixels_per_mercator_unit,
        "geometry": asdict(profile.geometry),
        "responsive_mode": (
            "desktop" if profile.geometry.uses_wide_spacing else "phone-portrait"
        ),
        "trajectory_bbox": list(trajectory_bbox),
    }


def xyz_tile_snapshot(x: int, y: int, zoom: int) -> dict[str, Any]:
    scale = 2**zoom
    return {
        "draw_width": 512.0,
        "draw_height": 512.0,
        "draw_bounds": {
            "west_x": x / scale,
            "north_y": y / scale,
            "east_x": (x + 1) / scale,
            "south_y": (y + 1) / scale,
        },
    }


class WindSampler:
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


def _enumerate_seeds(snapshot: dict[str, Any]) -> Iterable[tuple[float, float]]:
    bounds = snapshot["draw_bounds"]
    geometry = snapshot["geometry"]
    origin = snapshot["distance_origin"]
    pixels_per_unit = snapshot["distance_pixels_per_mercator_unit"]
    step_x = geometry["dx_px"] / pixels_per_unit
    step_y = geometry["dy_px"] / pixels_per_unit
    halo = (geometry["max_len_px"] + 12.0) / pixels_per_unit
    west = bounds["west_x"] - halo
    east = bounds["east_x"] + halo
    north = bounds["north_y"] - halo
    south = bounds["south_y"] + halo
    first_row = math.floor((north - origin[1]) / step_y) - 1
    last_row = math.ceil((south - origin[1]) / step_y) + 1
    for row in range(first_row, last_row + 1):
        mercator_y = origin[1] + row * step_y
        if mercator_y < north or mercator_y > south:
            continue
        row_offset = step_x * 0.5 if abs(row % 2) == 0 else 0.0
        first_column = math.floor((west - origin[0] - row_offset) / step_x) - 1
        last_column = math.ceil((east - origin[0] - row_offset) / step_x) + 1
        for column in range(first_column, last_column + 1):
            mercator_x = origin[0] + column * step_x + row_offset
            if west <= mercator_x <= east:
                yield mercator_to_lon_lat(mercator_x, mercator_y)


def integrate_paths(
    metadata: dict[str, Any],
    values: bytes,
    presentation: Presentation,
) -> tuple[list[PathGeometry], dict[str, Any]]:
    snapshot = presentation_snapshot(presentation, metadata["grid"])
    return integrate_paths_from_snapshot(metadata, values, snapshot)


def integrate_paths_from_snapshot(
    metadata: dict[str, Any],
    values: bytes,
    snapshot: dict[str, Any],
) -> tuple[list[PathGeometry], dict[str, Any]]:
    sampler = WindSampler(metadata, values)
    geometry = snapshot["geometry"]
    trajectory_bbox = snapshot["trajectory_bbox"]
    sample_margin = 0.35
    dt = geometry["trajectory_seconds"] / geometry["steps"]
    pixels_per_unit = snapshot["distance_pixels_per_mercator_unit"]
    paths: list[PathGeometry] = []
    tested_seeds = 0
    integration_steps = 0
    started = time.perf_counter()

    for seed_lon, seed_lat in _enumerate_seeds(snapshot):
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

        points = [(clamped_lon, clamped_lat)]
        previous_x, previous_y = lon_lat_to_mercator(clamped_lon, clamped_lat)
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
            next_x, next_y = lon_lat_to_mercator(clamped_next_lon, clamped_next_lat)
            delta_x = (next_x - previous_x) * pixels_per_unit
            delta_y = (next_y - previous_y) * pixels_per_unit
            step_len = math.hypot(delta_x, delta_y)
            if step_len < 0.01:
                break
            if travelled + step_len > geometry["max_len_px"]:
                fraction = max(0.0, (geometry["max_len_px"] - travelled) / step_len)
                head_x = previous_x + (next_x - previous_x) * fraction
                head_y = previous_y + (next_y - previous_y) * fraction
                points.append(mercator_to_lon_lat(head_x, head_y))
                break
            points.append((clamped_next_lon, clamped_next_lat))
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
            paths.append(PathGeometry(seed_speed, tuple(points)))

    return paths, {
        "generation_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "tested_seeds": tested_seeds,
        "accepted_paths": len(paths),
        "integration_steps": integration_steps,
        "source_points": sum(len(path.points) for path in paths),
        "snapshot": snapshot,
    }


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


def _clip_segment(
    start: tuple[float, float],
    end: tuple[float, float],
    width: float,
    height: float,
) -> tuple[tuple[float, float], tuple[float, float], float, float] | None:
    delta_x = end[0] - start[0]
    delta_y = end[1] - start[1]
    lower = 0.0
    upper = 1.0
    for direction, distance in (
        (-delta_x, start[0]),
        (delta_x, width - start[0]),
        (-delta_y, start[1]),
        (delta_y, height - start[1]),
    ):
        if abs(direction) <= np.finfo(float).eps:
            if distance < 0:
                return None
            continue
        ratio = distance / direction
        if direction < 0:
            if ratio > upper:
                return None
            lower = max(lower, ratio)
        else:
            if ratio < lower:
                return None
            upper = min(upper, ratio)
    if lower > upper:
        return None
    return (
        (start[0] + delta_x * lower, start[1] + delta_y * lower),
        (start[0] + delta_x * upper, start[1] + delta_y * upper),
        lower,
        upper,
    )


def clip_paths_to_draw_bounds(
    paths: Iterable[PathGeometry],
    snapshot: dict[str, Any],
) -> tuple[list[PathGeometry], dict[str, int | bool]]:
    bounds = snapshot["draw_bounds"]
    width = snapshot["draw_width"]
    height = snapshot["draw_height"]
    scale_x = width / (bounds["east_x"] - bounds["west_x"])
    scale_y = height / (bounds["south_y"] - bounds["north_y"])
    clipped_paths: list[PathGeometry] = []
    source_paths = 0
    source_points = 0

    for path in paths:
        source_paths += 1
        source_points += len(path.points)
        mercator_points = [lon_lat_to_mercator(lon, lat) for lon, lat in path.points]
        projected = [
            (
                (x - bounds["west_x"]) * scale_x,
                (y - bounds["north_y"]) * scale_y,
            )
            for x, y in mercator_points
        ]
        current: list[tuple[float, float]] = []
        current_has_original_end = False
        for index in range(len(projected) - 1):
            clipped = _clip_segment(projected[index], projected[index + 1], width, height)
            if clipped is None:
                if len(current) >= 2:
                    clipped_paths.append(
                        PathGeometry(
                            path.seed_speed_ms if current_has_original_end else 0.0,
                            tuple(current),
                        )
                    )
                current = []
                current_has_original_end = False
                continue
            clipped_start, clipped_end, start_fraction, end_fraction = clipped
            start_x = mercator_points[index][0] + (
                mercator_points[index + 1][0] - mercator_points[index][0]
            ) * start_fraction
            start_y = mercator_points[index][1] + (
                mercator_points[index + 1][1] - mercator_points[index][1]
            ) * start_fraction
            end_x = mercator_points[index][0] + (
                mercator_points[index + 1][0] - mercator_points[index][0]
            ) * end_fraction
            end_y = mercator_points[index][1] + (
                mercator_points[index + 1][1] - mercator_points[index][1]
            ) * end_fraction
            geographic_start = mercator_to_lon_lat(start_x, start_y)
            geographic_end = mercator_to_lon_lat(end_x, end_y)
            if not current:
                current = [geographic_start, geographic_end]
            else:
                previous_x, previous_y = current[-1]
                if (
                    abs(previous_x - geographic_start[0]) > 1e-10
                    or abs(previous_y - geographic_start[1]) > 1e-10
                ):
                    clipped_paths.append(PathGeometry(0.0, tuple(current)))
                    current = [geographic_start, geographic_end]
                else:
                    current.append(geographic_end)
            current_has_original_end = (
                index == len(projected) - 2 and abs(end_fraction - 1.0) <= 1e-12
            )
        if len(current) >= 2:
            clipped_paths.append(
                PathGeometry(
                    path.seed_speed_ms if current_has_original_end else 0.0,
                    tuple(current),
                )
            )

    return clipped_paths, {
        "clipped_to_draw_bounds": True,
        "clip_source_paths": source_paths,
        "clip_output_paths": len(clipped_paths),
        "clip_removed_points": source_points - sum(len(path.points) for path in clipped_paths),
    }


def _simplify_projected_points(
    geographic_points: tuple[tuple[float, float], ...],
    projected_points: list[tuple[float, float]],
    tolerance_px: float,
) -> tuple[tuple[float, float], ...]:
    if len(geographic_points) <= 2 or tolerance_px <= 0:
        return geographic_points
    keep = {0, len(geographic_points) - 1}
    stack = [(0, len(geographic_points) - 1)]
    while stack:
        start_index, end_index = stack.pop()
        largest_distance = tolerance_px
        largest_index = -1
        for index in range(start_index + 1, end_index):
            distance = _point_segment_distance(
                projected_points[index],
                projected_points[start_index],
                projected_points[end_index],
            )
            if distance > largest_distance:
                largest_distance = distance
                largest_index = index
        if largest_index >= 0:
            keep.add(largest_index)
            stack.append((start_index, largest_index))
            stack.append((largest_index, end_index))
    return tuple(geographic_points[index] for index in sorted(keep))


def simplify_paths(
    paths: Iterable[PathGeometry],
    snapshot: dict[str, Any],
    tolerance_px: float,
) -> tuple[list[PathGeometry], dict[str, int | float]]:
    safe_tolerance = max(0.0, float(tolerance_px))
    if safe_tolerance <= 0:
        unchanged = list(paths)
        point_count = sum(len(path.points) for path in unchanged)
        return unchanged, {
            "simplification_tolerance_px": 0.0,
            "simplified_points": point_count,
            "simplification_removed_points": 0,
        }
    bounds = snapshot["draw_bounds"]
    scale_x = snapshot["draw_width"] / (bounds["east_x"] - bounds["west_x"])
    scale_y = snapshot["draw_height"] / (bounds["south_y"] - bounds["north_y"])
    simplified: list[PathGeometry] = []
    source_points = 0
    simplified_points = 0
    for path in paths:
        source_points += len(path.points)
        projected = [
            (
                (longitude_to_mercator_x(lon) - bounds["west_x"]) * scale_x,
                (latitude_to_mercator_y(lat) - bounds["north_y"]) * scale_y,
            )
            for lon, lat in path.points
        ]
        if len(path.points) <= 2:
            points = path.points
        else:
            # Preserve the original final segment so terminal arrow direction
            # remains the same after simplifying the long, smooth trajectory.
            points = _simplify_projected_points(
                path.points[:-1],
                projected[:-1],
                safe_tolerance,
            ) + (path.points[-1],)
        simplified.append(PathGeometry(path.seed_speed_ms, points))
        simplified_points += len(points)
    return simplified, {
        "simplification_tolerance_px": safe_tolerance,
        "simplified_points": simplified_points,
        "simplification_removed_points": source_points - simplified_points,
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
    while offset < len(data) and shift <= 35:
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


def encode_bundle(
    paths: Iterable[PathGeometry],
    quantization_bbox: tuple[float, float, float, float],
) -> tuple[bytes, dict[str, int]]:
    west, south, east, north = quantization_bbox
    if east <= west or north <= south:
        raise ValueError("quantization bbox must have positive width and height")
    payload = bytearray()
    encoded_paths = 0
    encoded_points = 0
    source_points = 0

    for path in paths:
        quantized: list[tuple[int, int]] = []
        source_points += len(path.points)
        for lon, lat in path.points:
            qx = round(_clamp((lon - west) / (east - west), 0.0, 1.0) * QUANTIZATION_MAX)
            qy = round(_clamp((lat - south) / (north - south), 0.0, 1.0) * QUANTIZATION_MAX)
            point = (qx, qy)
            if not quantized or point != quantized[-1]:
                quantized.append(point)
        if len(quantized) < 2:
            continue
        _append_varint(payload, len(quantized))
        _append_varint(payload, round(max(0.0, path.seed_speed_ms) * 100.0))
        previous_x, previous_y = quantized[0]
        _append_varint(payload, previous_x)
        _append_varint(payload, previous_y)
        for qx, qy in quantized[1:]:
            _append_varint(payload, _zigzag(qx - previous_x))
            _append_varint(payload, _zigzag(qy - previous_y))
            previous_x = qx
            previous_y = qy
        encoded_paths += 1
        encoded_points += len(quantized)

    header = HEADER.pack(
        MAGIC,
        VERSION,
        0,
        HEADER.size,
        encoded_paths,
        encoded_points,
        west,
        south,
        east,
        north,
    )
    return header + payload, {
        "encoded_paths": encoded_paths,
        "encoded_points": encoded_points,
        "quantization_removed_points": source_points - encoded_points,
    }


def decode_bundle(data: bytes) -> tuple[list[PathGeometry], tuple[float, float, float, float]]:
    if len(data) < HEADER.size:
        raise ValueError("streamline bundle is shorter than its header")
    magic, version, flags, header_size, path_count, point_count, west, south, east, north = HEADER.unpack_from(
        data
    )
    if magic != MAGIC or version != VERSION or flags != 0 or header_size != HEADER.size:
        raise ValueError("unsupported streamline bundle header")
    offset = header_size
    paths: list[PathGeometry] = []
    decoded_points = 0
    for _ in range(path_count):
        count, offset = _read_varint(data, offset)
        speed_centi_ms, offset = _read_varint(data, offset)
        qx, offset = _read_varint(data, offset)
        qy, offset = _read_varint(data, offset)
        quantized = [(qx, qy)]
        for _ in range(count - 1):
            dx, offset = _read_varint(data, offset)
            dy, offset = _read_varint(data, offset)
            qx += _unzigzag(dx)
            qy += _unzigzag(dy)
            if not (0 <= qx <= QUANTIZATION_MAX and 0 <= qy <= QUANTIZATION_MAX):
                raise ValueError("streamline coordinate exceeds its quantization domain")
            quantized.append((qx, qy))
        points = tuple(
            (
                west + (qx / QUANTIZATION_MAX) * (east - west),
                south + (qy / QUANTIZATION_MAX) * (north - south),
            )
            for qx, qy in quantized
        )
        paths.append(PathGeometry(speed_centi_ms / 100.0, points))
        decoded_points += count
    if decoded_points != point_count or offset != len(data):
        raise ValueError("streamline bundle counts or trailing bytes do not match")
    return paths, (west, south, east, north)


def resolve_step_path(metadata_path: Path, metadata: dict[str, Any], step_label: str) -> Path:
    step = next((item for item in metadata.get("steps", []) if item.get("step") == step_label), None)
    if not step:
        raise ValueError(f"step {step_label!r} does not exist in {metadata_path}")
    declared = str(step.get("url") or step.get("path") or "")
    if not declared:
        raise ValueError(f"step {step_label!r} has no path")
    candidate = Path(declared)
    if candidate.is_absolute() and candidate.is_file():
        return candidate
    for ancestor in (metadata_path.parent, *metadata_path.parents):
        direct = ancestor / declared
        if direct.is_file():
            return direct
        if ancestor.name == "web_exports" and declared.startswith("web_exports/"):
            relative = ancestor.parent / declared
            if relative.is_file():
                return relative
    raise FileNotFoundError(f"cannot resolve {declared!r} from {metadata_path}")


def benchmark_bundle(
    metadata_path: Path,
    step_label: str,
    presentation: Presentation,
    output_path: Path,
    simplify_tolerance_px: float = 0.0,
    clip_to_view: bool = False,
) -> dict[str, Any]:
    metadata_path = Path(metadata_path).resolve()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    step_path = resolve_step_path(metadata_path, metadata, step_label)
    values = step_path.read_bytes()
    paths, stats = integrate_paths(metadata, values, presentation)
    if clip_to_view:
        paths, clip_stats = clip_paths_to_draw_bounds(paths, stats["snapshot"])
    else:
        clip_stats = {"clipped_to_draw_bounds": False}
    paths, simplification_stats = simplify_paths(
        paths,
        stats["snapshot"],
        simplify_tolerance_px,
    )
    trajectory_bbox = tuple(stats["snapshot"]["trajectory_bbox"])
    bundle, encoding_stats = encode_bundle(paths, trajectory_bbox)
    decoded, decoded_bbox = decode_bundle(bundle)
    if len(decoded) != encoding_stats["encoded_paths"] or decoded_bbox != trajectory_bbox:
        raise RuntimeError("streamline bundle round trip did not preserve its declared geometry")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(bundle)
    gzip_bytes = gzip.compress(bundle, compresslevel=9, mtime=0)
    gzip_path = output_path.with_suffix(output_path.suffix + ".gz")
    gzip_path.write_bytes(gzip_bytes)
    source_gzip_bytes = gzip.compress(values, compresslevel=9, mtime=0)
    result = {
        "prototype": "whole-viewport-precomputed-streamlines",
        "format": "XWS1 delta-varint geographic polylines",
        "metadata": str(metadata_path),
        "step": step_label,
        "step_path": str(step_path),
        "presentation": asdict(presentation),
        **stats,
        **clip_stats,
        **simplification_stats,
        **encoding_stats,
        "source_wind_bytes": len(values),
        "source_wind_gzip_bytes": len(source_gzip_bytes),
        "bundle_bytes": len(bundle),
        "bundle_gzip_bytes": len(gzip_bytes),
        "bundle_to_source_ratio": round(len(bundle) / max(1, len(values)), 4),
        "bundle_gzip_to_source_gzip_ratio": round(
            len(gzip_bytes) / max(1, len(source_gzip_bytes)), 4
        ),
        "output": str(output_path),
        "gzip_output": str(gzip_path),
    }
    sidecar_path = output_path.with_suffix(output_path.suffix + ".json")
    sidecar_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def _path_candidate_tiles(path: PathGeometry, zoom: int) -> Iterable[tuple[int, int]]:
    scale = 2**zoom
    points = [lon_lat_to_mercator(lon, lat) for lon, lat in path.points]
    minimum_x = min(point[0] for point in points)
    maximum_x = max(point[0] for point in points)
    minimum_y = min(point[1] for point in points)
    maximum_y = max(point[1] for point in points)
    first_x = max(0, min(scale - 1, math.floor(minimum_x * scale)))
    last_x = max(0, min(scale - 1, math.floor(maximum_x * scale)))
    first_y = max(0, min(scale - 1, math.floor(minimum_y * scale)))
    last_y = max(0, min(scale - 1, math.floor(maximum_y * scale)))
    for x in range(first_x, last_x + 1):
        for y in range(first_y, last_y + 1):
            yield x, y


def tile_paths(
    paths: Iterable[PathGeometry],
    zoom: int,
    simplify_tolerance_px: float,
) -> tuple[dict[tuple[int, int], list[PathGeometry]], dict[str, int | float]]:
    by_tile: dict[tuple[int, int], list[PathGeometry]] = defaultdict(list)
    source_paths = 0
    candidate_assignments = 0
    clipped_fragments = 0
    clipped_points = 0
    simplified_points = 0
    for path in paths:
        source_paths += 1
        for tile in _path_candidate_tiles(path, zoom):
            candidate_assignments += 1
            snapshot = xyz_tile_snapshot(tile[0], tile[1], zoom)
            fragments, _ = clip_paths_to_draw_bounds((path,), snapshot)
            if not fragments:
                continue
            clipped_fragments += len(fragments)
            clipped_points += sum(len(fragment.points) for fragment in fragments)
            fragments, _ = simplify_paths(fragments, snapshot, simplify_tolerance_px)
            by_tile[tile].extend(fragments)
            simplified_points += sum(len(fragment.points) for fragment in fragments)
    return dict(by_tile), {
        "tile_zoom": zoom,
        "source_paths": source_paths,
        "candidate_tile_assignments": candidate_assignments,
        "clipped_fragments": clipped_fragments,
        "clipped_points": clipped_points,
        "simplified_tile_points": simplified_points,
        "nonempty_tiles": len(by_tile),
        "simplification_tolerance_px": simplify_tolerance_px,
    }


def build_tile_package(
    metadata_path: Path,
    step_label: str,
    output_directory: Path,
    profiles: Iterable[TileProfile] = DEFAULT_TILE_PROFILES,
    simplify_tolerance_px: float = 0.15,
) -> dict[str, Any]:
    metadata_path = Path(metadata_path).resolve()
    output_directory = Path(output_directory).resolve()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    step_path = resolve_step_path(metadata_path, metadata, step_label)
    values = step_path.read_bytes()
    quantization_bbox = grid_bbox(metadata["grid"])
    package_profiles: list[dict[str, Any]] = []
    started = time.perf_counter()

    for profile in profiles:
        snapshot = tile_profile_snapshot(profile, metadata["grid"])
        benchmark_presentation = (
            PRESENTATIONS["desktop-view"]
            if profile.geometry.uses_wide_spacing
            else PRESENTATIONS["mobile-view"]
        )
        benchmark_snapshot = presentation_snapshot(benchmark_presentation, metadata["grid"])
        paths, integration_stats = integrate_paths_from_snapshot(metadata, values, snapshot)
        paths_by_tile, tiling_stats = tile_paths(
            paths,
            profile.tile_zoom,
            simplify_tolerance_px,
        )
        tile_entries: list[dict[str, Any]] = []
        for (x, y), fragments in sorted(paths_by_tile.items()):
            bundle, encoding_stats = encode_bundle(fragments, quantization_bbox)
            gzip_bundle = gzip.compress(bundle, compresslevel=9, mtime=0)
            relative_path = Path(profile.name) / f"z{profile.tile_zoom}" / f"{x}_{y}.xws"
            tile_path = output_directory / relative_path
            tile_path.parent.mkdir(parents=True, exist_ok=True)
            tile_path.write_bytes(bundle)
            tile_path.with_suffix(".xws.gz").write_bytes(gzip_bundle)
            tile_entries.append(
                {
                    "x": x,
                    "y": y,
                    "path": relative_path.as_posix(),
                    "bytes": len(bundle),
                    "gzip_bytes": len(gzip_bundle),
                    **encoding_stats,
                }
            )

        package_profiles.append(
            {
                "name": profile.name,
                "tile_zoom": profile.tile_zoom,
                "tile_size": 512,
                "pixels_per_mercator_unit": profile.pixels_per_mercator_unit,
                "geometry": asdict(profile.geometry),
                "snapshot": snapshot,
                "benchmark_presentation": asdict(benchmark_presentation),
                "benchmark_snapshot": benchmark_snapshot,
                "integration": integration_stats,
                "tiling": tiling_stats,
                "tiles": tile_entries,
                "total_bytes": sum(tile["bytes"] for tile in tile_entries),
                "total_gzip_bytes": sum(tile["gzip_bytes"] for tile in tile_entries),
            }
        )

    package = {
        "contract": "xcbenz-wind-streamline-tiles",
        "contract_version": "0.1.0-experimental",
        "format": "XWS1",
        "metadata": str(metadata_path),
        "step": step_label,
        "step_path": str(step_path),
        "grid_bbox": list(quantization_bbox),
        "simplification_tolerance_px": simplify_tolerance_px,
        "generation_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "profiles": package_profiles,
    }
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "metadata.json").write_text(
        json.dumps(package, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return package
