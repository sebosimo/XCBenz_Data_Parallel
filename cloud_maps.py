import datetime
import json
import os
import shutil

import numpy as np

from wind_maps import _HorizontalWeights, _lat_lon_coord, _regular_crop_grid


CACHE_DIR_CLOUD_MAPS = "cache_cloud_maps"
CLOUD_SCHEMA_VERSION = 1
CLOUD_BITS_PER_VALUE = 4
CLOUD_QUANTIZATION_STEP_PCT = 10
CLOUD_MISSING_CODE = 15
CLOUD_RESERVED_CODES = [11, 12, 13, 14]
CLOUD_COMPONENTS = ["cloud_cover_pct"]
CLOUD_LAYERS = {
    "total": {"param": "CLCT", "label": "Total cloud cover"},
    "low": {"param": "CLCL", "label": "Low cloud cover"},
    "mid": {"param": "CLCM", "label": "Medium cloud cover"},
    "high": {"param": "CLCH", "label": "High cloud cover"},
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


def is_cloud_maps_enabled(model, env=None):
    if not _env_bool("ENABLE_CLOUD_MAPS", False, env=env):
        return False
    return _env_bool(f"ENABLE_CLOUD_MAPS_{model.upper()}", False, env=env)


def _json_safe(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
        return value if np.isfinite(value) else None
    if isinstance(value, float):
        return value if np.isfinite(value) else None
    return value


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_safe)
        f.write("\n")


def is_cloud_run_complete(model, run_tag, root=CACHE_DIR_CLOUD_MAPS):
    try:
        for layer in CLOUD_LAYERS:
            metadata_path = os.path.join(root, model, run_tag, layer, "metadata.json")
            if not os.path.exists(metadata_path):
                return False
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            steps = metadata.get("steps") or []
            if not steps:
                return False
            for step in steps:
                path = (step.get("path") or "").replace("/", os.sep)
                if not os.path.exists(path):
                    return False
        return True
    except Exception:
        return False


def quantize_cloud_cover_codes(values):
    arr = np.asarray(values, dtype=np.float32)
    encoded = np.full(arr.shape, CLOUD_MISSING_CODE, dtype="u1")
    finite = np.isfinite(arr)
    if np.any(finite):
        codes = np.floor(np.clip(arr[finite], 0.0, 100.0) / CLOUD_QUANTIZATION_STEP_PCT + 0.5)
        encoded[finite] = np.clip(codes, 0, 10).astype("u1")
    return encoded


def pack_cloud_codes(codes):
    flat = np.asarray(codes, dtype="u1").ravel()
    invalid = ~(((flat >= 0) & (flat <= 10)) | (flat == CLOUD_MISSING_CODE))
    if np.any(invalid):
        raise ValueError("cloud codes must be 0..10 or missing code 15")

    byte_count = (flat.size + 1) // 2
    packed = np.empty(byte_count, dtype="u1")
    even = flat[0::2]
    odd = flat[1::2]
    packed[: odd.size] = even[: odd.size] | (odd << 4)
    if even.size > odd.size:
        packed[-1] = even[-1] | (CLOUD_MISSING_CODE << 4)
    return packed


def unpack_cloud_codes(packed, cell_count):
    data = np.asarray(packed, dtype="u1").ravel()
    out = np.empty(data.size * 2, dtype="u1")
    out[0::2] = data & 0x0F
    out[1::2] = data >> 4
    return out[:cell_count]


class CloudMapAccumulator:
    def __init__(self, model, run_tag, ref_time, config, log=None, out_root=CACHE_DIR_CLOUD_MAPS):
        self.model = model
        self.model_key = "icon-ch1" if model == "ch1" else "icon-ch2"
        self.run_tag = run_tag
        self.ref_time = ref_time
        self.config = config
        self.log = log or _default_log
        self.out_root = out_root
        self.prepared = False
        self.target_lat = None
        self.target_lon = None
        self.source_indices = None
        self.weights = None
        self.steps = {layer: [] for layer in CLOUD_LAYERS}

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
            raise ValueError("not enough source points inside cloud-map crop")

        self.source_indices = source_indices
        self.target_lat, self.target_lon = _regular_crop_grid(crop, self.config.grid_spacing_deg)
        self.weights = _HorizontalWeights(lon[source_indices], lat[source_indices], self.target_lon, self.target_lat)
        self.prepared = True
        self.log(
            f"Cloud maps {self.model}: crop grid {self.target_lat.shape[1]}x{self.target_lat.shape[0]}, "
            f"{source_indices.size} source point(s)",
            "INFO",
        )

    def _map_source(self, values):
        values = np.asarray(values, dtype=np.float32).ravel()
        return self.weights.apply(values[self.source_indices])

    def append(self, sample_field, values_by_source, horizon, ref_time):
        if horizon % self.config.horizon_stride != 0:
            return False

        wrote_any = False
        try:
            if not self.prepared:
                self._prepare(sample_field)

            for layer, layer_config in CLOUD_LAYERS.items():
                source_name = layer_config["param"]
                if source_name not in values_by_source:
                    continue

                cloud_cover = np.clip(self._map_source(values_by_source[source_name]), 0.0, 100.0)
                codes = quantize_cloud_cover_codes(cloud_cover)
                packed = pack_cloud_codes(codes)
                step_label = f"H{int(horizon):03d}" if self.model == "ch2" else f"H{int(horizon):02d}"
                output_dir = os.path.join(self.out_root, self.model, self.run_tag, layer)
                steps_dir = os.path.join(output_dir, "steps")
                os.makedirs(steps_dir, exist_ok=True)
                step_path = os.path.join(steps_dir, f"{step_label}.bin")
                with open(step_path, "wb") as f:
                    f.write(packed.tobytes())

                valid_time = ref_time + datetime.timedelta(hours=int(horizon))
                if valid_time.tzinfo is None:
                    valid_time = valid_time.replace(tzinfo=datetime.timezone.utc)

                valid = cloud_cover[np.isfinite(cloud_cover)]
                self.steps[layer].append(
                    {
                        "step": step_label,
                        "horizon": int(horizon),
                        "valid_time": valid_time.isoformat(),
                        "path": step_path.replace(os.sep, "/"),
                        "byte_length": int(os.path.getsize(step_path)),
                        "min_cloud_cover_pct": float(np.nanmin(valid)) if valid.size else None,
                        "max_cloud_cover_pct": float(np.nanmax(valid)) if valid.size else None,
                        "mean_cloud_cover_pct": float(np.nanmean(valid)) if valid.size else None,
                        "missing_cell_count": int(np.count_nonzero(codes == CLOUD_MISSING_CODE)),
                    }
                )
                wrote_any = True
            return wrote_any
        except Exception as exc:
            self.log(f"Cloud maps {self.model}: H+{horizon:03d} failed: {exc}", "WARNING")
            return False

    def _metadata_payload(self, layer, steps):
        height, width = self.target_lat.shape
        layer_config = CLOUD_LAYERS[layer]
        return {
            "schema_version": CLOUD_SCHEMA_VERSION,
            "product": "cloud_map_layer",
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
            "product_name": layer,
            "source_variable": layer_config["param"],
            "source_label": layer_config["label"],
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
                "format": "packed-uint4-cloud-cover",
                "dtype": "uint8",
                "components": CLOUD_COMPONENTS,
                "units": ["%"],
                "bits_per_value": CLOUD_BITS_PER_VALUE,
                "quantization_step_pct": CLOUD_QUANTIZATION_STEP_PCT,
                "add_offset": 0.0,
                "missing_code": CLOUD_MISSING_CODE,
                "reserved_codes": CLOUD_RESERVED_CODES,
                "nibble_order": "even_cell_low_nibble_odd_cell_high_nibble",
                "pad_nibble_code": CLOUD_MISSING_CODE,
            },
            "style": {
                "map_bbox": [
                    self.config.crop["lon_min"],
                    self.config.crop["lat_min"],
                    self.config.crop["lon_max"],
                    self.config.crop["lat_max"],
                ],
                "display_component": "cloud_cover_pct",
                "display_units": "%",
                "bounds_pct": [0, 10, 25, 50, 75, 90, 100],
            },
            "steps": steps,
        }

    def finalize(self):
        if not any(self.steps.values()):
            self.log(f"Cloud maps {self.model}: no horizons accumulated", "INFO")
            return {"files": 0, "bytes": 0}

        file_count = 0
        byte_count = 0
        for layer, steps in self.steps.items():
            if not steps:
                continue
            output_dir = os.path.join(self.out_root, self.model, self.run_tag, layer)
            metadata_path = os.path.join(output_dir, "metadata.json")
            _write_json(metadata_path, self._metadata_payload(layer, steps))
            file_count += len(steps) + 1
            byte_count += sum(step["byte_length"] for step in steps) + os.path.getsize(metadata_path)

        self.log(
            f"Cloud maps {self.model}: wrote {file_count} browser-ready file(s), {byte_count} bytes",
            "NOTICE",
        )
        return {"files": file_count, "bytes": byte_count}


def cleanup_old_cloud_runs(model, anchor_hour, log=None, root=CACHE_DIR_CLOUD_MAPS):
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
            logger(f"Cloud maps {model}: cleanup removed {item}", "INFO")
        except Exception as exc:
            logger(f"Cloud maps {model}: cleanup failed {item}: {exc}", "WARNING")
