import datetime
import json
import os

import numpy as np

from wind_maps import _HorizontalWeights, _lat_lon_coord, _regular_crop_grid


CACHE_DIR_RAIN_MAPS = "cache_rain_maps"
RAIN_SCHEMA_VERSION = 1
RAIN_SCALE_FACTOR = 0.2
RAIN_FILL_VALUE = np.uint8(255)
RAIN_MAX_ENCODED_VALUE = 254
RAIN_MAX_MM = RAIN_MAX_ENCODED_VALUE * RAIN_SCALE_FACTOR
RAIN_COMPONENTS = ["precipitation_mm"]


def _default_log(msg, level="INFO"):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} [{level}] {msg}", flush=True)


def _env_bool(name, default=False, env=None):
    source = os.environ if env is None else env
    raw = source.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def is_rain_maps_enabled(model, env=None):
    if not _env_bool("ENABLE_RAIN_MAPS", True, env=env):
        return False
    return _env_bool(f"ENABLE_RAIN_MAPS_{model.upper()}", True, env=env)


def _json_safe(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
        return value if np.isfinite(value) else None
    if isinstance(value, float):
        return value if np.isfinite(value) else None
    return value


def is_rain_run_complete(model, run_tag, root=CACHE_DIR_RAIN_MAPS):
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


class RainMapAccumulator:
    def __init__(self, model, run_tag, ref_time, config, log=None, out_root=CACHE_DIR_RAIN_MAPS):
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
            raise ValueError("not enough source points inside rain-map crop")

        self.source_indices = source_indices
        self.target_lat, self.target_lon = _regular_crop_grid(crop, self.config.grid_spacing_deg)
        self.weights = _HorizontalWeights(lon[source_indices], lat[source_indices], self.target_lon, self.target_lat)
        self.prepared = True
        self.log(
            f"Rain maps {self.model}: crop grid {self.target_lat.shape[1]}x{self.target_lat.shape[0]}, "
            f"{source_indices.size} source point(s)",
            "INFO",
        )

    def _map_source(self, values):
        values = np.asarray(values, dtype=np.float32).ravel()
        return self.weights.apply(values[self.source_indices])

    def append(self, sample_field, values_by_source, horizon, ref_time):
        if "TOT_PREC" not in values_by_source:
            return False

        try:
            if not self.prepared:
                self._prepare(sample_field)

            current_raw = np.asarray(values_by_source["TOT_PREC"], dtype=np.float32).ravel()
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

            if horizon % self.config.horizon_stride != 0:
                return False

            precipitation = self._map_source(precipitation_source)
            step_label = f"H{int(horizon):03d}" if self.model == "ch2" else f"H{int(horizon):02d}"
            scaled = np.rint(precipitation / RAIN_SCALE_FACTOR)
            finite_scaled = np.isfinite(scaled)
            clipped_count = int(np.count_nonzero(finite_scaled & (scaled > RAIN_MAX_ENCODED_VALUE)))

            encoded = np.empty(precipitation.size * len(RAIN_COMPONENTS), dtype="u1")
            scaled[~finite_scaled] = RAIN_FILL_VALUE
            encoded[0::len(RAIN_COMPONENTS)] = np.clip(scaled, 0, RAIN_MAX_ENCODED_VALUE).astype("u1").ravel()
            encoded[~finite_scaled.ravel()] = RAIN_FILL_VALUE

            os.makedirs(self.steps_dir, exist_ok=True)
            step_path = os.path.join(self.steps_dir, f"{step_label}.bin")
            with open(step_path, "wb") as f:
                f.write(encoded.tobytes())

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
                    "max_precipitation_mm": float(np.nanmax(valid_precip)) if valid_precip.size else None,
                    "clipped_precipitation_cell_count": clipped_count,
                }
            )
            return True
        except Exception as exc:
            self.log(f"Rain maps {self.model}: H+{horizon:03d} failed: {exc}", "WARNING")
            return False

    def finalize(self):
        if not self.steps:
            self.log(f"Rain maps {self.model}: no horizons accumulated", "INFO")
            return {"files": 0, "bytes": 0}

        height, width = self.target_lat.shape
        metadata_path = os.path.join(self.output_dir, "metadata.json")
        payload = {
            "schema_version": RAIN_SCHEMA_VERSION,
            "product": "rain_map_surface",
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
            "source_variable": "TOT_PREC",
            "derived_component": "deaccumulated_step_precipitation",
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
                "format": "uint8-interleaved-components",
                "dtype": "uint8",
                "components": RAIN_COMPONENTS,
                "units": ["mm"],
                "scale_factor": RAIN_SCALE_FACTOR,
                "add_offset": 0.0,
                "missing_value": int(RAIN_FILL_VALUE),
            },
            "style": {
                "map_bbox": [
                    self.config.crop["lon_min"],
                    self.config.crop["lat_min"],
                    self.config.crop["lon_max"],
                    self.config.crop["lat_max"],
                ],
                "display_component": "precipitation_mm",
                "display_units": "mm",
                "bounds_mm": [0, 0.2, 0.5, 1, 2, 4, 8, 15, 25, 40, 50],
                "colors": [
                    "#f7fbff",
                    "#deebf7",
                    "#c6dbef",
                    "#9ecae1",
                    "#6baed6",
                    "#4292c6",
                    "#2171b5",
                    "#08519c",
                    "#08306b",
                    "#5b21b6",
                ],
            },
            "steps": self.steps,
        }
        _write_json(metadata_path, payload)
        total_bytes = sum(step["byte_length"] for step in self.steps) + os.path.getsize(metadata_path)
        self.log(
            f"Rain maps {self.model}: wrote {len(self.steps)} browser-ready step(s), {total_bytes} bytes",
            "NOTICE",
        )
        return {"files": len(self.steps) + 1, "bytes": total_bytes}


def cleanup_old_rain_runs(model, anchor_hour, log=None, root=CACHE_DIR_RAIN_MAPS):
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
            import shutil

            shutil.rmtree(path)
            logger(f"Rain maps {model}: removed old run {item}", "INFO")
        except Exception as exc:
            logger(f"Rain maps {model}: cleanup failed for {item}: {exc}", "WARNING")
