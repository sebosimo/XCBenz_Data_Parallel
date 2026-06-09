import datetime
import json
import os
import shutil

import numpy as np


CACHE_DIR_SUNRAIN_MAPS = "cache_sunrain_maps"
SUNRAIN_SCHEMA_VERSION = 1
SUNRAIN_FILL_VALUE = np.uint8(0)
SUNRAIN_RESERVED_VALUES = [251, 252, 253, 254, 255]
SUNRAIN_RAIN_VISIBLE_THRESHOLD_MM = 0.2
SUNRAIN_COMPONENTS = ["sunrain_code"]


def _default_log(msg, level="INFO"):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} [{level}] {msg}", flush=True)


def _env_bool(name, default=False, env=None):
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def is_sunrain_maps_enabled(model, env=None):
    if not _env_bool("ENABLE_SUNRAIN_MAPS", True, env=env):
        return False
    return _env_bool(f"ENABLE_SUNRAIN_MAPS_{model.upper()}", True, env=env)


def _json_safe(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
        return value if np.isfinite(value) else None
    if isinstance(value, float):
        return value if np.isfinite(value) else None
    return value


def is_sunrain_run_complete(model, run_tag, root=CACHE_DIR_SUNRAIN_MAPS):
    metadata_path = os.path.join(root, model, run_tag, "surface", "metadata.json")
    if not os.path.exists(metadata_path):
        return False
    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
        steps = metadata.get("steps") or []
        return bool(steps) and all(os.path.exists((step.get("path") or "").replace("/", os.sep)) for step in steps)
    except Exception:
        return False


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_safe)
        f.write("\n")


def encode_sunshine_percent_codes(fraction_pct):
    values = np.asarray(fraction_pct, dtype=np.float32)
    encoded = np.zeros(values.shape, dtype="u1")
    finite = np.isfinite(values)
    scaled = np.rint(np.clip(values[finite], 0.0, 100.0))
    encoded[finite] = np.clip(scaled, 1, 100).astype("u1")
    return encoded


def encode_rain_amount_codes(precipitation_mm):
    values = np.asarray(precipitation_mm, dtype=np.float32)
    encoded = np.zeros(values.shape, dtype="u1")
    finite = np.isfinite(values)
    if not np.any(finite):
        return encoded, 0

    mm = np.maximum(values[finite], 0.0)
    codes = np.zeros(mm.shape, dtype=np.int16)

    low = mm <= 5.0
    middle = (mm > 5.0) & (mm <= 30.0)
    high = mm > 30.0

    codes[low] = 101 + np.rint((mm[low] - 0.1) / 0.1).astype(np.int16)
    codes[middle] = 151 + np.rint((mm[middle] - 5.5) / 0.5).astype(np.int16)
    codes[high] = 201 + np.rint(mm[high] - 31.0).astype(np.int16)
    codes = np.clip(codes, 101, 250)

    clipped_count = int(np.count_nonzero(mm > 80.0))
    encoded[finite] = codes.astype("u1")
    return encoded, clipped_count


class SunRainMapAccumulator:
    def __init__(self, model, run_tag, ref_time, config, log=None, out_root=CACHE_DIR_SUNRAIN_MAPS):
        self.model = model
        self.model_key = "icon-ch1" if model == "ch1" else "icon-ch2"
        self.run_tag = run_tag
        self.ref_time = ref_time
        self.config = config
        self.log = log or _default_log
        self.out_root = out_root
        self.output_dir = os.path.join(out_root, model, run_tag, "surface")
        self.steps_dir = os.path.join(self.output_dir, "steps")
        self.prepared = False
        self.target_lat = None
        self.target_lon = None
        self.source_indices = None
        self.weights = None
        self.previous_raw = None
        self.steps = []

    def seed_previous_raw(self, values):
        if values is None:
            self.previous_raw = None
        else:
            self.previous_raw = np.asarray(values, dtype=np.float32).ravel()

    def _prepare(self, sample_field):
        from wind_maps import _HorizontalWeights, _lat_lon_coord, _regular_crop_grid

        sample = sample_field.squeeze()
        _spatial_dim, lat, lon = _lat_lon_coord(sample)
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
            raise ValueError("not enough source points inside sunrain-map crop")

        self.source_indices = source_indices
        self.target_lat, self.target_lon = _regular_crop_grid(crop, self.config.grid_spacing_deg)
        self.weights = _HorizontalWeights(lon[source_indices], lat[source_indices], self.target_lon, self.target_lat)
        self.prepared = True
        self.log(
            f"Sun+Rain maps {self.model}: crop grid {self.target_lat.shape[1]}x{self.target_lat.shape[0]}, "
            f"{source_indices.size} source point(s)",
            "INFO",
        )

    def _map_source(self, values):
        values = np.asarray(values, dtype=np.float32).ravel()
        return self.weights.apply(values[self.source_indices])

    def _map_optional_source(self, values_by_source, source_name):
        if source_name not in values_by_source:
            return np.full(self.target_lat.shape, np.nan, dtype=np.float32)
        return self._map_source(values_by_source[source_name])

    def append(self, sample_field, radiation_values_by_source, rain_values_by_source, horizon, ref_time):
        if horizon % self.config.horizon_stride != 0:
            return False
        if "TOT_PREC" not in rain_values_by_source:
            return False
        if "DURSUN" not in radiation_values_by_source or "DURSUN_M" not in radiation_values_by_source:
            return False

        try:
            if not self.prepared:
                self._prepare(sample_field)

            current_raw = np.asarray(rain_values_by_source["TOT_PREC"], dtype=np.float32).ravel()
            if self.previous_raw is None:
                previous_raw = np.zeros_like(current_raw, dtype=np.float32)
            else:
                previous_raw = self.previous_raw

            precipitation_source = current_raw - previous_raw
            invalid = ~np.isfinite(current_raw) | ~np.isfinite(previous_raw)
            precipitation_source = precipitation_source.astype(np.float32, copy=False)
            precipitation_source[invalid] = np.nan
            precipitation_source = np.maximum(precipitation_source, 0.0)
            self.previous_raw = current_raw

            precipitation = self._map_source(precipitation_source)
            duration = self._map_optional_source(radiation_values_by_source, "DURSUN")
            possible = self._map_optional_source(radiation_values_by_source, "DURSUN_M")
            sunshine_fraction = np.divide(
                duration * 100.0,
                possible,
                out=np.full_like(duration, np.nan, dtype=np.float32),
                where=np.isfinite(duration) & np.isfinite(possible) & (possible > 0),
            )
            sunshine_fraction = np.clip(sunshine_fraction, 0.0, 100.0)

            encoded = np.full(precipitation.shape, SUNRAIN_FILL_VALUE, dtype="u1")
            rain_mask = np.isfinite(precipitation) & (precipitation >= SUNRAIN_RAIN_VISIBLE_THRESHOLD_MM)
            sunshine_mask = ~rain_mask & np.isfinite(sunshine_fraction)

            rain_codes, clipped_count = encode_rain_amount_codes(precipitation)
            sunshine_codes = encode_sunshine_percent_codes(sunshine_fraction)
            encoded[rain_mask] = rain_codes[rain_mask]
            encoded[sunshine_mask] = sunshine_codes[sunshine_mask]

            step_label = f"H{int(horizon):03d}" if self.model == "ch2" else f"H{int(horizon):02d}"
            os.makedirs(self.steps_dir, exist_ok=True)
            step_path = os.path.join(self.steps_dir, f"{step_label}.bin")
            with open(step_path, "wb") as f:
                f.write(encoded.ravel().tobytes())

            valid_time = ref_time + datetime.timedelta(hours=int(horizon))
            if valid_time.tzinfo is None:
                valid_time = valid_time.replace(tzinfo=datetime.timezone.utc)

            valid_precip = precipitation[np.isfinite(precipitation)]
            self.steps.append(
                {
                    "step": step_label,
                    "horizon": int(horizon),
                    "valid_time": valid_time.isoformat(),
                    "path": step_path.replace(os.sep, "/"),
                    "byte_length": int(os.path.getsize(step_path)),
                    "rain_cell_count": int(np.count_nonzero(rain_mask)),
                    "sunshine_cell_count": int(np.count_nonzero(sunshine_mask)),
                    "missing_cell_count": int(np.count_nonzero(encoded == SUNRAIN_FILL_VALUE)),
                    "max_precipitation_mm": float(np.nanmax(valid_precip)) if valid_precip.size else None,
                    "clipped_precipitation_cell_count": clipped_count,
                }
            )
            return True
        except Exception as exc:
            self.log(f"Sun+Rain maps {self.model}: H+{horizon:03d} failed: {exc}", "WARNING")
            return False

    def finalize(self):
        if not self.steps:
            self.log(f"Sun+Rain maps {self.model}: no horizons accumulated", "INFO")
            return {"files": 0, "bytes": 0}

        height, width = self.target_lat.shape
        metadata_path = os.path.join(self.output_dir, "metadata.json")
        payload = {
            "schema_version": SUNRAIN_SCHEMA_VERSION,
            "product": "sunrain_map_surface",
            "model": self.model_key,
            "run": self.run_tag,
            "domain": {
                "id": getattr(self.config, "domain_id", "default"),
                "label": getattr(self.config, "domain_label", "Default"),
                "bbox": [
                    self.config.crop["lon_min"],
                    self.config.crop["lat_min"],
                    self.config.crop["lon_max"],
                    self.config.crop["lat_max"],
                ],
            },
            "product_name": "surface",
            "source_variables": ["DURSUN", "DURSUN_M", "TOT_PREC"],
            "derived_component": "sunshine_fraction_or_deaccumulated_step_precipitation",
            "ref_time": self.ref_time.isoformat(),
            "grid": {
                "projection": "EPSG:4326",
                "width": int(width),
                "height": int(height),
                "lon": {
                    "start": float(self.target_lon[0, 0]),
                    "end": float(self.target_lon[0, -1]),
                    "step": float(self.target_lon[0, 1] - self.target_lon[0, 0]) if width > 1 else 0.0,
                    "count": int(width),
                },
                "lat": {
                    "start": float(self.target_lat[0, 0]),
                    "end": float(self.target_lat[-1, 0]),
                    "step": float(self.target_lat[1, 0] - self.target_lat[0, 0]) if height > 1 else 0.0,
                    "count": int(height),
                },
            },
            "encoding": {
                "format": "uint8-semantic-sunrain-code",
                "dtype": "uint8",
                "components": SUNRAIN_COMPONENTS,
                "units": ["code"],
                "missing_value": int(SUNRAIN_FILL_VALUE),
                "reserved_values": SUNRAIN_RESERVED_VALUES,
            },
            "sunrain_encoding": {
                "sunshine_code_min": 1,
                "sunshine_code_max": 100,
                "sunshine_code_units": "%",
                "sunshine_code_1_represents": "0-1% sunshine fraction",
                "rain_code_min": 101,
                "rain_code_max": 250,
                "rain_visible_threshold_mm": SUNRAIN_RAIN_VISIBLE_THRESHOLD_MM,
                "rain_bins": [
                    {"code_min": 101, "code_max": 150, "start_mm": 0.1, "step_mm": 0.1},
                    {"code_min": 151, "code_max": 200, "start_mm": 5.5, "step_mm": 0.5},
                    {"code_min": 201, "code_max": 250, "start_mm": 31.0, "step_mm": 1.0},
                ],
            },
            "style": {
                "map_bbox": [
                    self.config.crop["lon_min"],
                    self.config.crop["lat_min"],
                    self.config.crop["lon_max"],
                    self.config.crop["lat_max"],
                ],
                "display_component": "sunrain_code",
                "display_units": "code",
            },
            "steps": self.steps,
        }
        _write_json(metadata_path, payload)
        total_bytes = sum(step["byte_length"] for step in self.steps) + os.path.getsize(metadata_path)
        self.log(
            f"Sun+Rain maps {self.model}: wrote {len(self.steps)} browser-ready step(s), {total_bytes} bytes",
            "NOTICE",
        )
        return {"files": len(self.steps) + 1, "bytes": total_bytes}


def cleanup_old_sunrain_runs(model, anchor_hour, log=None, root=CACHE_DIR_SUNRAIN_MAPS):
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
            run_dt = datetime.datetime.strptime(item, "%Y%m%d_%H%M").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
        if run_dt.hour == anchor_hour and run_dt.minute == 0 and run_dt.date() in keep_dates:
            continue
        path = os.path.join(model_dir, item)
        try:
            shutil.rmtree(path)
            logger(f"Sun+Rain maps {model}: cleanup removed {item}", "INFO")
        except Exception as exc:
            logger(f"Sun+Rain maps {model}: cleanup failed {item}: {exc}", "WARNING")
