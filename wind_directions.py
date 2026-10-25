"""Small 0.08-degree U/V sidecar, generated from the forecast's decoded fields."""

from dataclasses import replace
import datetime as dt
import json
import os
from pathlib import Path

import numpy as np

from wind_maps import WindMapAccumulator, WindMapLevel


ALTITUDES_M = (1000, 1500, 2000, 2500, 3000, 4000, 5000, 6000, 7000, 8000)
SPACING_DEG = 0.08
SCALE = 0.25
MISSING = -32768


class _SubsetWeights:
    """Reuse the fine grid's triangles, retaining only coarse targets and sources."""

    def __init__(self, parent, stride):
        flat_targets = np.arange(np.prod(parent.output_shape)).reshape(parent.output_shape)[::stride, ::stride]
        self.output_shape = flat_targets.shape
        lookup = np.full(np.prod(parent.output_shape), -1, dtype=np.int32)
        lookup[flat_targets.ravel()] = np.arange(flat_targets.size)
        inside = lookup[parent.inside_target] >= 0
        outside = lookup[parent.outside_target] >= 0
        vertices = parent.vertices[inside]
        nearest = parent.outside_source[outside]
        self.source_indices = np.unique(np.concatenate((vertices.ravel(), nearest)))
        self.inside_target = lookup[parent.inside_target[inside]]
        self.outside_target = lookup[parent.outside_target[outside]]
        self.vertices = np.searchsorted(self.source_indices, vertices)
        self.outside_source = np.searchsorted(self.source_indices, nearest)
        self.weights = parent.weights[inside]

    def apply(self, values):
        flat = np.full(np.prod(self.output_shape), np.nan, dtype=np.float32)
        if self.inside_target.size:
            samples = values[self.vertices]
            valid = np.all(np.isfinite(samples) | (np.abs(self.weights) < 1e-6), axis=1)
            weighted = np.sum(np.where(np.isfinite(samples), samples, 0) * self.weights, axis=1)
            flat[self.inside_target[valid]] = weighted[valid]
        flat[self.outside_target] = values[self.outside_source]
        return flat.reshape(self.output_shape)


class WindDirectionAccumulator(WindMapAccumulator):
    def __init__(self, parent):
        self.parent = parent
        config = replace(
            parent.config,
            levels=tuple(WindMapLevel(f"{h}m_AMSL", h, "AMSL") for h in ALTITUDES_M),
            grid_spacing_deg=SPACING_DEG,
            max_seconds=0,
            horizon_stride=1,
        )
        super().__init__(parent.model, parent.run_tag, parent.ref_time, config,
                         log=parent.log, out_root=parent.out_root, web_grid_stride=1)

    def _prepare(self, fields):
        parent = self.parent
        if not parent.prepared:
            raise ValueError("wind directions require the prepared forecast geometry")
        ratio = SPACING_DEG / parent.config.grid_spacing_deg
        stride = round(ratio)
        if stride < 1 or not np.isclose(stride, ratio):
            raise ValueError("0.08-degree directions must align with the forecast grid")
        self.weights = _SubsetWeights(parent.weights, stride)
        indices = self.weights.source_indices
        self.source_indices = parent.source_indices[indices]
        self.heights_full = parent.heights_full[:, indices]
        self.surface_height = parent.surface_height[indices]
        self.target_lat = parent.target_lat[::stride, ::stride]
        self.target_lon = parent.target_lon[::stride, ::stride]
        self.prepared = True

    def _level_output_dir(self, level):
        return os.path.join(self.out_root, self.model, self.run_tag, "directions", level.name)

    def _encode_web_component(self, values):
        scaled = np.rint(np.asarray(values, dtype=np.float64) / SCALE)
        valid = np.isfinite(scaled)
        if np.any(np.abs(scaled[valid]) > 32767):
            raise ValueError("wind component exceeds int16 range; refusing to clip direction")
        return np.where(valid, scaled, MISSING).astype("<i2")

    def _wind_step_summary(self, u_raw, v_raw):
        valid = (u_raw != MISSING) & (v_raw != MISSING)
        if not np.any(valid):
            return {"min_speed_ms": None, "max_speed_ms": None}
        speed = np.hypot(u_raw[valid].astype(float), v_raw[valid].astype(float)) * SCALE
        return {"min_speed_ms": round(float(speed.min()), 2), "max_speed_ms": round(float(speed.max()), 2)}

    def _direct_metadata_payload(self, level, steps):
        result = super()._direct_metadata_payload(level, steps)
        result["purpose"] = "vertical_air_arrows"
        result["encoding"].update(format="int16-interleaved-u-v", dtype="int16",
                                  scale_factor=SCALE, missing_value=MISSING)
        return result


def validate_publication(web_root, latest_runs, *, required=True):
    """Require complete current runs and check every referenced slice before upload."""
    from pipeline_orchestration.job_plan import expected_horizon_count

    web_root = Path(web_root)
    manifest_path = web_root / "wind_maps" / "directions.json"
    if not manifest_path.exists():
        if required:
            raise ValueError("missing coarse wind direction manifest")
        return
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != 1 or manifest.get("purpose") != "vertical_air_arrows":
        raise ValueError("unsupported wind direction manifest")
    for model, run in latest_runs.items():
        levels = manifest.get("models", {}).get(model, {}).get("runs", {}).get(run, {}).get("levels", {})
        expected_levels = {f"{h}m_AMSL" for h in ALTITUDES_M}
        if set(levels) != expected_levels:
            raise ValueError(f"wind directions {model}/{run}: incomplete altitude coverage")
        expected_hours = set(range(expected_horizon_count(model, run)))
        reference = dt.datetime.strptime(run, "%Y%m%d_%H%M").replace(tzinfo=dt.timezone.utc)
        for name, entry in levels.items():
            path = Path(entry["metadata"])
            if not path.is_file() or not path.resolve().is_relative_to(web_root.resolve()):
                raise ValueError(f"unsafe or missing direction metadata: {path}")
            metadata = json.loads(path.read_text())
            encoding = metadata["encoding"]
            if (metadata["model"] != model or metadata["run"] != run
                    or metadata["level"]["name"] != name or metadata["level"]["type"] != "AMSL"
                    or encoding["dtype"] != "int16" or encoding["scale_factor"] != SCALE
                    or encoding["missing_value"] != MISSING or encoding["components"] != ["u", "v"]):
                raise ValueError(f"invalid direction metadata: {path}")
            steps = metadata["steps"]
            if len(steps) != len(expected_hours) or {s["horizon"] for s in steps} != expected_hours:
                raise ValueError(f"incomplete direction hours: {path}")
            grid = metadata["grid"]
            if not all(abs(grid[axis]["step"] - SPACING_DEG) < 2e-5 for axis in ("lat", "lon")):
                raise ValueError(f"unexpected direction grid spacing: {path}")
            for step in steps:
                expected_time = reference + dt.timedelta(hours=step["horizon"])
                artifact = Path(step["url"])
                expected_bytes = grid["width"] * grid["height"] * 4
                if (dt.datetime.fromisoformat(step["valid_time"].replace("Z", "+00:00")) != expected_time
                        or not artifact.resolve().is_relative_to(web_root.resolve())
                        or step["byte_length"] != expected_bytes or artifact.stat().st_size != expected_bytes):
                    raise ValueError(f"invalid direction slice: {artifact}")
