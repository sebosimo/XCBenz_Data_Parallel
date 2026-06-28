import datetime
import json
import os
import math
import shutil
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np
import xarray as xr
from scipy.spatial import Delaunay, cKDTree


CACHE_DIR_WIND_PACKED = "cache_wind_packed"
CACHE_DIR_WIND_MAPS = "cache_wind_maps"
DEFAULT_CONFIG_PATH = "wind_maps_config.json"
NETCDF_ENGINE = "netcdf4"
WIND_SCHEMA_VERSION = 1
WIND_ENCODING_NAME = "int16_scale_0.1_ms"
WIND_COMPRESS_KW = {"zlib": True, "shuffle": True, "complevel": 4}
WIND_SCALE_FACTOR = 0.1
WIND_FILL_VALUE = np.int16(-32768)
WIND_WEB_DEFAULT_GRID_STRIDE = 2
WIND_WEB_SCALE_FACTOR = 0.25
WIND_WEB_FILL_VALUE = np.int8(-128)
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


def _default_log(msg, level="INFO"):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} [{level}] {msg}", flush=True)


def _env_bool(name, default=False, env=None):
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def is_wind_maps_enabled(model, env=None):
    if not _env_bool("ENABLE_WIND_MAPS", False, env=env):
        return False
    return _env_bool(f"ENABLE_WIND_MAPS_{model.upper()}", False, env=env)


def is_direct_wind_web_enabled(env=None):
    return _env_bool("XCBENZ_DIRECT_WIND_WEB", False, env=env)


def _env_float(name, default, env=None):
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name, default, env=None):
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _safe_level_name(value):
    clean = "".join(c for c in str(value) if c.isalnum() or c in ("-", "_"))
    return clean if clean else "unnamed"


def _json_safe(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, np.ndarray):
        return value.tolist()
    return value


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, allow_nan=False, default=_json_safe)
        f.write("\n")


def _clean_number(value, precision=None):
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


def _array_to_list(values, precision=None):
    return [_clean_number(item, precision) for item in np.asarray(values).tolist()]


@dataclass(frozen=True)
class WindMapLevel:
    name: str
    h: float
    type: str
    enabled: bool = True


@dataclass(frozen=True)
class WindMapConfig:
    levels: tuple[WindMapLevel, ...]
    crop: dict
    grid_spacing_deg: float
    source_padding_deg: float
    max_seconds: float
    horizon_stride: int = 1
    domain_id: str = "default"
    domain_label: str = "Default"

    @property
    def enabled_levels(self):
        return tuple(level for level in self.levels if level.enabled)


def load_config(path=DEFAULT_CONFIG_PATH, env=None, log: Callable[[str, str], None] | None = None):
    logger = log or _default_log
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} is required for wind-map generation")

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    raw_levels = raw.get("levels", raw if isinstance(raw, list) else [])
    levels = []
    only_levels = None
    source_env = os.environ if env is None else env
    if source_env.get("WIND_MAP_LEVELS"):
        only_levels = {
            _safe_level_name(item.strip())
            for item in source_env["WIND_MAP_LEVELS"].split(",")
            if item.strip()
        }

    for item in raw_levels:
        name = _safe_level_name(item["name"])
        enabled = bool(item.get("enabled", True))
        if only_levels is not None:
            enabled = name in only_levels
        levels.append(
            WindMapLevel(
                name=name,
                h=float(item["h"]),
                type=str(item["type"]).upper(),
                enabled=enabled,
            )
        )

    if not levels:
        raise ValueError("wind-map config must define at least one level")

    crop = raw.get("crop", {})
    required_crop_keys = {"lon_min", "lon_max", "lat_min", "lat_max"}
    if set(crop) & required_crop_keys != required_crop_keys:
        raise ValueError("wind-map config crop must define lon_min, lon_max, lat_min, lat_max")

    max_seconds = _env_float("WIND_MAP_MAX_SECONDS", float(raw.get("max_seconds", 300)), env=env)
    domain = raw.get("domain") or {}
    cfg = WindMapConfig(
        levels=tuple(levels),
        crop={key: float(crop[key]) for key in required_crop_keys},
        grid_spacing_deg=float(raw.get("grid_spacing_deg", 0.02)),
        source_padding_deg=float(raw.get("source_padding_deg", 0.2)),
        max_seconds=max_seconds,
        horizon_stride=max(1, int(raw.get("horizon_stride", 1))),
        domain_id=_safe_level_name(domain.get("id", "default")),
        domain_label=str(domain.get("label", "Default")),
    )
    enabled_names = ", ".join(level.name for level in cfg.enabled_levels) or "none"
    logger(f"Wind-map config loaded: {len(cfg.enabled_levels)} enabled level(s): {enabled_names}", "INFO")
    return cfg


def wind_netcdf_encoding(ds):
    encoding = {}
    for name, data_array in ds.variables.items():
        if data_array.dtype.kind in ("U", "S", "O"):
            encoding[name] = {}
            continue
        enc = dict(WIND_COMPRESS_KW)
        if name in {"u", "v"}:
            enc.update({
                "dtype": "i2",
                "scale_factor": WIND_SCALE_FACTOR,
                "add_offset": 0.0,
                "_FillValue": WIND_FILL_VALUE,
            })
        elif name == "horizon":
            enc["dtype"] = "i2"
        elif name == "valid_time_epoch":
            enc["dtype"] = "i8"
        else:
            enc["dtype"] = "f4"
        encoding[name] = enc
    return encoding


def _regular_crop_grid(crop, spacing):
    lon = np.arange(crop["lon_min"], crop["lon_max"] + spacing * 0.5, spacing, dtype=np.float32)
    lat = np.arange(crop["lat_min"], crop["lat_max"] + spacing * 0.5, spacing, dtype=np.float32)
    lon2d, lat2d = np.meshgrid(lon, lat)
    return lat2d.astype(np.float32), lon2d.astype(np.float32)


def _lat_lon_coord(data):
    lat_name = "latitude" if "latitude" in data.coords else "lat"
    lon_name = "longitude" if "longitude" in data.coords else "lon"
    if lat_name not in data.coords or lon_name not in data.coords:
        raise ValueError("wind fields need latitude/longitude coordinates")
    lat = data[lat_name]
    lon = data[lon_name]
    if lat.ndim != 1 or lon.ndim != 1:
        raise ValueError("wind-map generation expects 1D native grid coordinates")
    if lat.dims[0] != lon.dims[0]:
        raise ValueError("latitude and longitude coordinates must share a spatial dimension")
    return lat.dims[0], lat.values.astype(np.float64), lon.values.astype(np.float64)


def _level_cell_values(data, spatial_dim, expected_levels=None):
    arr = data.squeeze()
    if spatial_dim not in arr.dims:
        raise ValueError(f"spatial dimension {spatial_dim!r} not found in {arr.dims}")
    dims = list(arr.dims)
    spatial_axis = dims.index(spatial_dim)
    other_dims = [d for d in dims if d != spatial_dim]
    if expected_levels is not None:
        level_dim = next((d for d in other_dims if arr.sizes[d] == expected_levels), None)
    else:
        level_dim = other_dims[0] if other_dims else None
    if level_dim is None:
        raise ValueError("could not identify vertical dimension in wind field")
    level_axis = dims.index(level_dim)
    values = np.moveaxis(arr.values, (level_axis, spatial_axis), (0, 1))
    if values.ndim != 2:
        values = values.reshape(values.shape[0], values.shape[1], -1)
        if values.shape[-1] != 1:
            raise ValueError("wind field has unsupported extra dimensions")
        values = values[:, :, 0]
    return values.astype(np.float32, copy=False)


def _interpolate_vertical(heights, values, target_h):
    z = heights
    vals = values
    if np.nanmedian(z[0] - z[-1]) > 0:
        z = z[::-1]
        vals = vals[::-1]

    target = np.float32(target_h)
    z_min = z[0]
    z_max = z[-1]
    valid = np.isfinite(z_min) & np.isfinite(z_max) & (target >= z_min) & (target <= z_max)
    idx1 = np.sum(z <= target, axis=0)
    idx1 = np.clip(idx1, 1, z.shape[0] - 1)
    idx0 = idx1 - 1
    cols = np.arange(z.shape[1])
    z0 = z[idx0, cols]
    z1 = z[idx1, cols]
    v0 = vals[idx0, cols]
    v1 = vals[idx1, cols]
    denom = z1 - z0
    frac = np.divide(target - z0, denom, out=np.zeros_like(z0, dtype=np.float32), where=denom != 0)
    out = v0 + frac * (v1 - v0)
    out = out.astype(np.float32, copy=False)
    out[~valid] = np.nan
    return out


def _single_level_values(data, spatial_dim):
    arr = data.squeeze()
    if spatial_dim not in arr.dims:
        raise ValueError(f"spatial dimension {spatial_dim!r} not found in {arr.dims}")

    dims = list(arr.dims)
    spatial_axis = dims.index(spatial_dim)
    values = np.moveaxis(arr.values, spatial_axis, 0)
    if values.ndim != 1:
        values = values.reshape(values.shape[0], -1)
        if values.shape[-1] != 1:
            raise ValueError("single-level wind field has unsupported extra dimensions")
        values = values[:, 0]
    return values.astype(np.float32, copy=False)


class _HorizontalWeights:
    def __init__(self, source_lon, source_lat, target_lon, target_lat, max_nearest_distance_deg=0.1):
        points = np.column_stack([source_lon, source_lat])
        target_points = np.column_stack([target_lon.ravel(), target_lat.ravel()])
        self.output_shape = target_lon.shape

        tri = Delaunay(points)
        simplices = tri.find_simplex(target_points)
        self.inside_mask = simplices >= 0
        self.inside_target = np.flatnonzero(self.inside_mask)
        self.vertices = np.empty((0, 3), dtype=np.int32)
        self.weights = np.empty((0, 3), dtype=np.float32)
        if self.inside_target.size:
            inside_simplices = simplices[self.inside_mask]
            transforms = tri.transform[inside_simplices]
            delta = target_points[self.inside_mask] - transforms[:, 2]
            bary = np.einsum("ijk,ik->ij", transforms[:, :2], delta)
            self.weights = np.column_stack([bary, 1.0 - bary.sum(axis=1)]).astype(np.float32)
            self.vertices = tri.simplices[inside_simplices].astype(np.int32)

        outside_target = np.flatnonzero(~self.inside_mask)
        self.outside_target = np.empty(0, dtype=np.int64)
        self.outside_source = np.empty(0, dtype=np.int32)
        if outside_target.size:
            distances, nearest = cKDTree(points).query(target_points[outside_target], k=1)
            near = np.isfinite(distances) & (distances <= float(max_nearest_distance_deg))
            self.outside_target = outside_target[near]
            self.outside_source = nearest[near].astype(np.int32)

    def apply(self, source_values):
        flat = np.full(self.output_shape[0] * self.output_shape[1], np.nan, dtype=np.float32)
        if self.inside_target.size:
            vals = source_values[self.vertices]
            flat[self.inside_target] = np.sum(vals * self.weights, axis=1, dtype=np.float32)
        if self.outside_target.size:
            flat[self.outside_target] = source_values[self.outside_source]
        return flat.reshape(self.output_shape).astype(np.float32, copy=False)


class WindMapAccumulator:
    def __init__(
        self,
        model,
        run_tag,
        ref_time,
        config,
        log=None,
        out_root=CACHE_DIR_WIND_PACKED,
        direct_web=False,
        web_grid_stride=None,
    ):
        self.model = model
        self.model_key = "icon-ch1" if model == "ch1" else "icon-ch2"
        self.run_tag = run_tag
        self.ref_time = ref_time
        self.config = config
        self.log = log or _default_log
        self.out_root = out_root
        self.direct_web = bool(direct_web)
        self.web_grid_stride = max(
            1,
            int(
                web_grid_stride
                if web_grid_stride is not None
                else _env_int("WIND_WEB_GRID_STRIDE", WIND_WEB_DEFAULT_GRID_STRIDE)
            ),
        )
        self.started_at = time.monotonic()
        self.wind_elapsed_seconds = 0.0
        self.max_wind_seconds = float(config.max_seconds)
        self.prepared = False
        self.budget_exceeded = False
        self.failed = False
        self.target_lat = None
        self.target_lon = None
        self.source_indices = None
        self.heights_full = None
        self.surface_height = None
        self.weights = None
        self.records = {
            level.name: {"u": [], "v": [], "horizon": [], "valid_time_epoch": [], "step_label": []}
            for level in config.enabled_levels
        }
        self.direct_steps = {level.name: [] for level in config.enabled_levels}
        self.direct_byte_count = 0

    def _over_budget(self):
        if self.max_wind_seconds > 0 and self.wind_elapsed_seconds > self.max_wind_seconds:
            self.budget_exceeded = True
            return True
        return False

    def _prepare(self, fields):
        sample = fields["U"].squeeze()
        spatial_dim, lat, lon = _lat_lon_coord(sample)
        crop = self.config.crop
        pad = self.config.source_padding_deg
        mask = (
            (lon >= crop["lon_min"] - pad)
            & (lon <= crop["lon_max"] + pad)
            & (lat >= crop["lat_min"] - pad)
            & (lat <= crop["lat_max"] + pad)
        )
        source_indices = np.flatnonzero(mask)
        if source_indices.size < 3:
            raise ValueError("not enough source points inside wind-map crop")

        hhl = fields["HHL"].squeeze()
        hhl_values = _level_cell_values(hhl, spatial_dim)
        hhl_values = hhl_values[:, source_indices]
        self.heights_full = ((hhl_values[:-1] + hhl_values[1:]) * 0.5).astype(np.float32)
        self.surface_height = hhl_values[-1].astype(np.float32)
        self.source_indices = source_indices
        self.target_lat, self.target_lon = _regular_crop_grid(crop, self.config.grid_spacing_deg)
        self.weights = _HorizontalWeights(
            lon[source_indices],
            lat[source_indices],
            self.target_lon,
            self.target_lat,
        )
        self.prepared = True
        self.log(
            f"Wind maps {self.model}: crop grid {self.target_lat.shape[1]}x{self.target_lat.shape[0]}, "
            f"{source_indices.size} source point(s)",
            "INFO",
        )

    def append(self, fields, horizon, ref_time):
        wind_start = time.monotonic()
        if horizon % self.config.horizon_stride != 0:
            return False
        if self._over_budget():
            self.log(
                f"Wind maps {self.model}: wind budget exceeded before H+{horizon:03d} "
                f"({self.wind_elapsed_seconds:.1f}s active wind time); skipping remaining horizons",
                "WARNING",
            )
            return False
        missing = [name for name in ("U", "V", "HHL") if name not in fields or fields[name] is None]
        if missing:
            self.log(f"Wind maps {self.model}: missing fields for H+{horizon:03d}: {missing}", "WARNING")
            return False

        try:
            if not self.prepared:
                self._prepare(fields)
            spatial_dim, _, _ = _lat_lon_coord(fields["U"].squeeze())
            u_all = _level_cell_values(fields["U"], spatial_dim, expected_levels=self.heights_full.shape[0])
            v_all = _level_cell_values(fields["V"], spatial_dim, expected_levels=self.heights_full.shape[0])
            u_source = u_all[:, self.source_indices]
            v_source = v_all[:, self.source_indices]
            u10_source = v10_source = None
            if "U_10M" in fields and "V_10M" in fields:
                u10_source = _single_level_values(fields["U_10M"], spatial_dim)[self.source_indices]
                v10_source = _single_level_values(fields["V_10M"], spatial_dim)[self.source_indices]
            valid_time = ref_time + datetime.timedelta(hours=int(horizon))
            if valid_time.tzinfo is None:
                valid_time = valid_time.replace(tzinfo=datetime.timezone.utc)

            for level in self.config.enabled_levels:
                if level.name == "10m_AGL" and u10_source is not None and v10_source is not None:
                    u_target = self.weights.apply(u10_source)
                    v_target = self.weights.apply(v10_source)
                else:
                    heights = self.heights_full - self.surface_height if level.type == "AGL" else self.heights_full
                    u_target = self.weights.apply(_interpolate_vertical(heights, u_source, level.h))
                    v_target = self.weights.apply(_interpolate_vertical(heights, v_source, level.h))
                if self.direct_web:
                    self._write_direct_step(level, horizon, valid_time, u_target, v_target)
                else:
                    rec = self.records[level.name]
                    rec["u"].append(u_target)
                    rec["v"].append(v_target)
                    rec["horizon"].append(int(horizon))
                    rec["valid_time_epoch"].append(int(valid_time.timestamp()))
                    rec["step_label"].append(self._step_label(horizon))
            return True
        except Exception as exc:
            self.failed = True
            self.log(f"Wind maps {self.model}: H+{horizon:03d} failed: {exc}", "WARNING")
            return False
        finally:
            self.wind_elapsed_seconds += time.monotonic() - wind_start

    def _step_label(self, horizon):
        return f"H{int(horizon):03d}" if self.model == "ch2" else f"H{int(horizon):02d}"

    def _level_attrs(self, level):
        return {
            "schema_version": WIND_SCHEMA_VERSION,
            "source": "MeteoSwiss ICON OGD",
            "model": f"icon-{self.model}",
            "layout": "packed_by_level",
            "level_name": level.name,
            "level_type": level.type,
            "level_h": float(level.h),
            "ref_time": self.ref_time.isoformat(),
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "crop_lon_min": self.config.crop["lon_min"],
            "crop_lon_max": self.config.crop["lon_max"],
            "crop_lat_min": self.config.crop["lat_min"],
            "crop_lat_max": self.config.crop["lat_max"],
            "grid_spacing_deg": self.config.grid_spacing_deg,
            "domain_id": self.config.domain_id,
            "domain_label": self.config.domain_label,
            "encoding": WIND_ENCODING_NAME,
        }

    def _dataset_for_level(self, level, rec):
        u_stack = np.stack(rec["u"]).astype(np.float32)
        v_stack = np.stack(rec["v"]).astype(np.float32)
        ds = xr.Dataset(
            {
                "u": xr.DataArray(u_stack, dims=("step", "y", "x"), attrs={"units": "m s-1"}),
                "v": xr.DataArray(v_stack, dims=("step", "y", "x"), attrs={"units": "m s-1"}),
            },
            coords={
                "horizon": xr.DataArray(np.asarray(rec["horizon"], dtype=np.int16), dims=("step",)),
                "valid_time_epoch": xr.DataArray(np.asarray(rec["valid_time_epoch"], dtype=np.int64), dims=("step",)),
                "step_label": xr.DataArray(np.asarray(rec["step_label"]), dims=("step",)),
                "latitude": xr.DataArray(self.target_lat, dims=("y", "x"), attrs={"units": "degrees_north"}),
                "longitude": xr.DataArray(self.target_lon, dims=("y", "x"), attrs={"units": "degrees_east"}),
            },
            attrs=self._level_attrs(level),
        )
        return ds

    def _wind_axis_payload(self, values, precision=5):
        axis = np.asarray(values, dtype=float)
        step = float(axis[1] - axis[0]) if len(axis) > 1 else 0.0
        return {
            "start": _clean_number(axis[0], precision) if len(axis) else None,
            "end": _clean_number(axis[-1], precision) if len(axis) else None,
            "step": _clean_number(step, precision),
            "count": int(len(axis)),
            "values": _array_to_list(axis, precision),
        }

    def _wind_map_bbox(self, lat, lon, attrs):
        keys = ("crop_lon_min", "crop_lat_min", "crop_lon_max", "crop_lat_max")
        if all(key in attrs and attrs[key] is not None for key in keys):
            return [_clean_number(attrs[key], 5) for key in keys]
        return [
            _clean_number(np.nanmin(lon), 5),
            _clean_number(np.nanmin(lat), 5),
            _clean_number(np.nanmax(lon), 5),
            _clean_number(np.nanmax(lat), 5),
        ]

    def _wind_style_payload(self, lat, lon, attrs):
        style = dict(WIND_WEB_STYLE)
        style["map_bbox"] = self._wind_map_bbox(lat, lon, attrs)
        if attrs.get("domain_id") or attrs.get("domain_label"):
            style["domain"] = {
                "id": attrs.get("domain_id") or "default",
                "label": attrs.get("domain_label") or attrs.get("domain_id") or "Default",
                "bbox": style["map_bbox"],
            }
        return style

    def _packed_wind_values(self, values):
        values = np.asarray(values, dtype=np.float32)
        packed = np.full(values.shape, WIND_FILL_VALUE, dtype=np.int16)
        valid = np.isfinite(values)
        if np.any(valid):
            scaled = np.rint(values[valid] / np.float32(WIND_SCALE_FACTOR))
            scaled = np.clip(scaled, np.iinfo(np.int16).min + 1, np.iinfo(np.int16).max)
            packed[valid] = scaled.astype(np.int16)
        decoded = packed.astype(np.float32) * WIND_SCALE_FACTOR
        decoded[~valid] = np.nan
        return decoded

    def _encode_web_component(self, values):
        values = np.asarray(values[:: self.web_grid_stride, :: self.web_grid_stride], dtype=float)
        values = self._packed_wind_values(values)
        scaled = np.rint(values / WIND_WEB_SCALE_FACTOR)
        missing = ~np.isfinite(scaled)
        scaled = np.clip(scaled, -127, 127)
        scaled[missing] = WIND_WEB_FILL_VALUE
        return scaled.astype("i1")

    def _wind_step_summary(self, u_raw, v_raw):
        valid = (u_raw != WIND_WEB_FILL_VALUE) & (v_raw != WIND_WEB_FILL_VALUE)
        if not np.any(valid):
            return {"min_speed_ms": None, "max_speed_ms": None}

        u_ms = u_raw[valid].astype(float) * WIND_WEB_SCALE_FACTOR
        v_ms = v_raw[valid].astype(float) * WIND_WEB_SCALE_FACTOR
        speed = np.hypot(u_ms, v_ms)
        return {
            "min_speed_ms": _clean_number(np.nanmin(speed), 2),
            "max_speed_ms": _clean_number(np.nanmax(speed), 2),
        }

    def _write_direct_step(self, level, horizon, valid_time, u_values, v_values):
        step_label = self._step_label(horizon)
        output_dir = os.path.join(self.out_root, self.model, self.run_tag, level.name)
        steps_dir = os.path.join(output_dir, "steps")
        os.makedirs(steps_dir, exist_ok=True)

        u_raw = self._encode_web_component(u_values)
        v_raw = self._encode_web_component(v_values)
        interleaved = np.empty(u_raw.size * 2, dtype="i1")
        interleaved[0::2] = u_raw.ravel()
        interleaved[1::2] = v_raw.ravel()

        step_path = os.path.join(steps_dir, f"{step_label}.bin")
        tmp_path = step_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(interleaved.tobytes())
        os.replace(tmp_path, step_path)

        step = {
            "step": step_label,
            "horizon": int(horizon),
            "valid_time": valid_time.isoformat(),
            "path": step_path.replace(os.sep, "/"),
            "byte_length": int(os.path.getsize(step_path)),
            **self._wind_step_summary(u_raw, v_raw),
        }
        self.direct_steps[level.name].append(step)
        self.direct_byte_count += step["byte_length"]

    def _direct_metadata_payload(self, level, steps):
        attrs = self._level_attrs(level)
        lat = np.asarray(self.target_lat[:: self.web_grid_stride, :: self.web_grid_stride], dtype=float)
        lon = np.asarray(self.target_lon[:: self.web_grid_stride, :: self.web_grid_stride], dtype=float)
        height, width = lat.shape
        return {
            "schema_version": WIND_SCHEMA_VERSION,
            "product": "wind_map_level",
            "model": self.model_key,
            "run": self.run_tag,
            "level": {
                "name": level.name,
                "type": attrs.get("level_type"),
                "height_m": _clean_number(attrs.get("level_h"), 1),
            },
            "ref_time": attrs.get("ref_time"),
            "source": "direct_wind_web",
            "grid": {
                "projection": "EPSG:4326",
                "width": int(width),
                "height": int(height),
                "source_stride": self.web_grid_stride,
                "lon": self._wind_axis_payload(lon[0, :]),
                "lat": self._wind_axis_payload(lat[:, 0]),
            },
            "encoding": {
                "format": "int8-interleaved-u-v",
                "dtype": "int8",
                "components": ["u", "v"],
                "units": "m s-1",
                "scale_factor": WIND_WEB_SCALE_FACTOR,
                "add_offset": 0.0,
                "missing_value": int(WIND_WEB_FILL_VALUE),
            },
            "style": self._wind_style_payload(lat, lon, attrs),
            "steps": sorted(steps, key=lambda item: int(item.get("horizon", 0))),
        }

    def _finalize_direct_web(self):
        elapsed = time.monotonic() - self.started_at
        if not any(self.direct_steps.values()):
            self.log(
                f"Wind maps {self.model}: no horizons accumulated "
                f"(wind={self.wind_elapsed_seconds:.1f}s, wall={elapsed:.1f}s)",
                "INFO",
            )
            return {"files": 0, "bytes": 0, "elapsed_seconds": elapsed, "wind_elapsed_seconds": self.wind_elapsed_seconds}
        if self.budget_exceeded:
            out_dir = os.path.join(self.out_root, self.model, self.run_tag)
            shutil.rmtree(out_dir, ignore_errors=True)
            self.log(
                f"Wind maps {self.model}: removed partial direct web files after wind budget exceed "
                f"(wind={self.wind_elapsed_seconds:.1f}s, wall={elapsed:.1f}s)",
                "WARNING",
            )
            return {
                "files": 0,
                "bytes": 0,
                "elapsed_seconds": elapsed,
                "wind_elapsed_seconds": self.wind_elapsed_seconds,
                "budget_exceeded": True,
            }

        file_count = 0
        byte_count = self.direct_byte_count
        for level in self.config.enabled_levels:
            steps = self.direct_steps[level.name]
            if not steps:
                continue
            output_dir = os.path.join(self.out_root, self.model, self.run_tag, level.name)
            metadata_path = os.path.join(output_dir, "metadata.json")
            _write_json(metadata_path, self._direct_metadata_payload(level, steps))
            file_count += len(steps) + 1
            byte_count += os.path.getsize(metadata_path)
            self.log(
                f"Wind maps {self.model}: wrote direct web {metadata_path} "
                f"({len(steps)} horizon(s))",
                "INFO",
            )

        elapsed = time.monotonic() - self.started_at
        self.log(
            f"Wind maps {self.model}: complete in {self.wind_elapsed_seconds:.1f}s active wind time "
            f"({elapsed:.1f}s wall), files={file_count}, bytes={byte_count}",
            "NOTICE",
        )
        return {
            "files": file_count,
            "bytes": byte_count,
            "elapsed_seconds": elapsed,
            "wind_elapsed_seconds": self.wind_elapsed_seconds,
        }

    def finalize(self):
        if self.direct_web:
            return self._finalize_direct_web()

        elapsed = time.monotonic() - self.started_at
        if not any(rec["horizon"] for rec in self.records.values()):
            self.log(
                f"Wind maps {self.model}: no horizons accumulated "
                f"(wind={self.wind_elapsed_seconds:.1f}s, wall={elapsed:.1f}s)",
                "INFO",
            )
            return {"files": 0, "bytes": 0, "elapsed_seconds": elapsed, "wind_elapsed_seconds": self.wind_elapsed_seconds}
        if self.budget_exceeded:
            self.log(
                f"Wind maps {self.model}: not writing partial files after wind budget exceed "
                f"(wind={self.wind_elapsed_seconds:.1f}s, wall={elapsed:.1f}s)",
                "WARNING",
            )
            return {
                "files": 0,
                "bytes": 0,
                "elapsed_seconds": elapsed,
                "wind_elapsed_seconds": self.wind_elapsed_seconds,
                "budget_exceeded": True,
            }

        out_dir = os.path.join(self.out_root, self.model, self.run_tag)
        os.makedirs(out_dir, exist_ok=True)
        file_count = 0
        byte_count = 0
        for level in self.config.enabled_levels:
            rec = self.records[level.name]
            if not rec["horizon"]:
                continue
            ds = self._dataset_for_level(level, rec)
            path = os.path.join(out_dir, f"Wind_{level.type}_{level.name}.nc")
            tmp_path = path + ".tmp"
            ds.to_netcdf(
                tmp_path,
                engine=NETCDF_ENGINE,
                format="NETCDF4",
                encoding=wind_netcdf_encoding(ds),
            )
            os.replace(tmp_path, path)
            size = os.path.getsize(path)
            file_count += 1
            byte_count += size
            self.log(
                f"Wind maps {self.model}: wrote {path} ({len(rec['horizon'])} horizon(s), {size} bytes)",
                "INFO",
            )

        elapsed = time.monotonic() - self.started_at
        self.log(
            f"Wind maps {self.model}: complete in {self.wind_elapsed_seconds:.1f}s active wind time "
            f"({elapsed:.1f}s wall), files={file_count}, bytes={byte_count}",
            "NOTICE",
        )
        return {
            "files": file_count,
            "bytes": byte_count,
            "elapsed_seconds": elapsed,
            "wind_elapsed_seconds": self.wind_elapsed_seconds,
        }


def cleanup_old_wind_runs(model, anchor_hour, log=None, root=CACHE_DIR_WIND_PACKED):
    logger = log or _default_log
    model_dir = os.path.join(root, model)
    if not os.path.exists(model_dir):
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    keep_dates = {now.date(), (now - datetime.timedelta(days=1)).date()}
    all_runs = sorted(
        [item for item in os.listdir(model_dir) if os.path.isdir(os.path.join(model_dir, item))],
        reverse=True,
    )
    keep_recent = set(all_runs[:2])
    for item in all_runs:
        if item in keep_recent:
            continue
        try:
            dt = datetime.datetime.strptime(item, "%Y%m%d_%H%M").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        if dt.hour == anchor_hour and dt.minute == 0 and dt.date() in keep_dates:
            continue
        path = os.path.join(model_dir, item)
        try:
            import shutil

            shutil.rmtree(path)
            logger(f"Wind maps {model}: cleanup removed {item}", "INFO")
        except Exception as exc:
            logger(f"Wind maps {model}: cleanup failed {item}: {exc}", "WARNING")
