"""Cache horizontal weights and HHL brackets; stream W through those brackets."""

import json
import os
from pathlib import Path
import time

import numpy as np
from eccodes import codes_get, codes_get_values
from scipy.spatial import Delaunay

from .grib import check_space, coordinates, identity, messages


PLAN_VERSION = 1


def horizon_seconds(handle):
    # Analysis files can expose endStep='0m'. Validate the encoded time unit,
    # rather than interpreting an ecCodes display string as a number of hours.
    units = {0: 60, 1: 3600, 2: 86400, 10: 10800, 11: 21600, 12: 43200, 13: 1}
    unit = int(codes_get(handle, "indicatorOfUnitOfTimeRange"))
    if unit not in units or str(codes_get(handle, "stepType")) != "instant":
        raise ValueError("unsupported W forecast time unit or aggregation")
    return int(codes_get(handle, "forecastTime")) * units[unit]


def horizontal_plan(lat, lon, config):
    """Barycentric interpolation inside short, native-grid triangles only."""
    west, south, east, north = config.bbox
    pad = 0.06
    mask = (np.isfinite(lat) & np.isfinite(lon)
            & (lon >= west - pad) & (lon <= east + pad)
            & (lat >= south - pad) & (lat <= north + pad))
    candidates = np.flatnonzero(mask)
    if candidates.size < 3:
        raise ValueError("not enough native cells inside the requested domain")
    points = np.column_stack((lon[candidates], lat[candidates]))
    tri = Delaunay(points)
    target_lon, target_lat = np.meshgrid(
        west + np.arange(config.width) * config.spacing,
        south + np.arange(config.height) * config.spacing,
    )
    target = np.column_stack((target_lon.ravel(), target_lat.ravel()))
    simplex = tri.find_simplex(target)
    target_indices = np.flatnonzero(simplex >= 0)
    simplices = simplex[target_indices]
    vertices = tri.simplices[simplices]
    # Never bridge model-domain gaps or extrapolate outside its convex hull.
    vertex_points = points[vertices]
    near = np.max(np.linalg.norm(vertex_points - target[target_indices, None, :], axis=2), axis=1) <= pad
    target_indices, simplices, vertices = target_indices[near], simplices[near], vertices[near]
    if not target_indices.size:
        raise ValueError("requested domain does not overlap valid native triangles")
    transform = tri.transform[simplices]
    delta = target[target_indices] - transform[:, 2]
    first = np.einsum("ijk,ik->ij", transform[:, :2], delta)
    weights = np.column_stack((first, 1 - first.sum(axis=1)))
    weights = np.clip(weights, 0, 1)
    weights /= weights.sum(axis=1, keepdims=True)
    needed, local_vertices = np.unique(vertices, return_inverse=True)
    return {
        "source_indices": candidates[needed].astype(np.int32),
        "target_indices": target_indices.astype(np.int32),
        "vertices": local_vertices.reshape(-1, 3).astype(np.int32),
        "weights": weights.astype(np.float32),
    }


def vertical_brackets(records, source_indices, altitudes, expected_identity):
    """Build ten 2D plans without ever retaining the complete HHL cube."""
    shape = (len(altitudes), len(source_indices))
    lower = np.zeros(shape, dtype=np.uint8)
    fraction = np.zeros(shape, dtype=np.float32)
    previous = None
    previous_level = 0
    targets = np.asarray(altitudes, dtype=np.float32)[:, None]
    for level, heights, grid_identity in records:
        if grid_identity != expected_identity:
            raise ValueError("HHL grid UUID does not match the interpolation plan")
        if level != previous_level + 1 or not 1 <= level <= 81:
            raise ValueError("HHL must contain ordered half levels 1 through 81")
        current = np.asarray(heights[source_indices], dtype=np.float32)
        if previous is not None:
            good = np.isfinite(previous) & np.isfinite(current) & (previous > current)
            match = good[None, :] & (targets >= current) & (targets <= previous)
            rows, cols = np.nonzero(match)
            lower[rows, cols] = level
            fraction[rows, cols] = (targets[rows, 0] - current[cols]) / (previous[cols] - current[cols])
        previous, previous_level = current, level
    if previous_level != 81:
        raise ValueError("HHL is truncated; expected 81 half levels")
    # No bracket means that the requested altitude is below terrain or outside HHL.
    return lower, fraction


def _height_records(path):
    with messages(path) as records:
        for h in records:
            if str(codes_get(h, "typeOfLevel")) != "generalVertical":
                raise ValueError("HHL must use model half levels")
            if str(codes_get(h, "units")) != "m":
                raise ValueError("HHL must contain geometric heights in metres")
            yield int(codes_get(h, "level")), codes_get_values(h), identity(h)


class Plan:
    def __init__(self, config, metadata, arrays):
        self.config, self.metadata = config, metadata
        for key, value in arrays.items():
            setattr(self, key, value)

    def regrid(self, source_values):
        result = np.full(self.config.width * self.config.height, np.nan, dtype=np.float32)
        sampled = source_values[self.vertices]
        # Zero-weight vertices must not mask an exact neighbouring cell.
        valid = np.all(np.isfinite(sampled) | (self.weights < 1e-6), axis=1)
        values = np.sum(np.where(np.isfinite(sampled), sampled, 0) * self.weights, axis=1)
        result[self.target_indices[valid]] = values[valid]
        return result.reshape(self.config.height, self.config.width)


def prepare(config, horizontal_path, vertical_path, cache_path, static_hashes):
    started = time.monotonic()
    signature = {"version": PLAN_VERSION, "config": config.key, "static_sha256": static_hashes}
    cache_path = Path(cache_path)
    if cache_path.exists():
        try:
            with np.load(cache_path, allow_pickle=False) as stored:
                metadata = json.loads(str(stored["metadata"]))
                if metadata["signature"] == signature:
                    arrays = {k: stored[k] for k in ("source_indices", "target_indices", "vertices", "weights", "lower", "fraction")}
                    return Plan(config, metadata, arrays), {"reused": True, "seconds": time.monotonic() - started}
        except (ValueError, KeyError, OSError):
            pass
    grid = coordinates(horizontal_path)
    with messages(vertical_path) as records:
        first = next(records)
        grid_identity = identity(first)
        cell_count = int(codes_get(first, "numberOfDataPoints"))
    if grid_identity["uuidOfHGrid"] != grid["uuid"] or cell_count != grid["lat"].size:
        raise ValueError("horizontal and vertical static grids differ")
    arrays = horizontal_plan(grid["lat"], grid["lon"], config)
    del grid
    arrays["lower"], arrays["fraction"] = vertical_brackets(
        _height_records(vertical_path), arrays["source_indices"], config.altitudes, grid_identity,
    )
    metadata = {"signature": signature, "grid_identity": grid_identity, "native_cells": cell_count}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    part = cache_path.with_suffix(".part")
    check_space(cache_path.parent, sum(v.nbytes for v in arrays.values()) + 1024**2)
    try:
        with part.open("wb") as f:
            np.savez(f, metadata=json.dumps(metadata, sort_keys=True), **arrays)
            f.flush()
            os.fsync(f.fileno())
        os.replace(part, cache_path)
    finally:
        part.unlink(missing_ok=True)
    return Plan(config, metadata, arrays), {"reused": False, "seconds": time.monotonic() - started}


def decode_w(path, plan, reference_time, horizon):
    """Only one native GRIB level plus the requested altitude fields live in RAM."""
    native = np.zeros(plan.lower.shape, dtype=np.float32)
    needed = set(np.unique(plan.lower[plan.lower > 0]).tolist())
    needed |= {v - 1 for v in needed}
    seen = set()
    decoded = 0
    with messages(path) as records:
        for h in records:
            level = int(codes_get(h, "level"))
            if level in seen or not 1 <= level <= 81:
                raise ValueError("duplicate or invalid W half level")
            seen.add(level)
            if (identity(h) != plan.metadata["grid_identity"]
                    or int(codes_get(h, "numberOfDataPoints")) != plan.metadata["native_cells"]):
                raise ValueError("W grid does not match cached HHL")
            if (str(codes_get(h, "name")) != "Geometric vertical velocity"
                    or str(codes_get(h, "units")) != "m s**-1"
                    or str(codes_get(h, "typeOfLevel")) != "generalVertical"):
                raise ValueError("expected geometric W in m/s on half levels")
            if (int(codes_get(h, "dataDate")) != int(reference_time.strftime("%Y%m%d"))
                    or int(codes_get(h, "dataTime")) != reference_time.hour * 100
                    or horizon_seconds(h) != horizon * 3600
                    or int(codes_get(h, "perturbationNumber")) != 0):
                raise ValueError("W reference time, horizon or control member differs")
            if level not in needed:
                continue
            values = codes_get_values(h)[plan.source_indices].astype(np.float32)
            missing = float(codes_get(h, "missingValue"))
            values[values == missing] = np.nan
            for row in range(len(plan.config.altitudes)):
                low = plan.lower[row] == level
                high = (plan.lower[row] > 0) & (plan.lower[row] - 1 == level)
                native[row, low] += values[low] * (1 - plan.fraction[row, low])
                native[row, high] += values[high] * plan.fraction[row, high]
            decoded += 1
    if seen != set(range(1, 82)):
        raise ValueError("W file is truncated; expected all 81 half levels")
    native[plan.lower == 0] = np.nan
    return native, {"messages": len(seen), "decoded_levels": decoded}
