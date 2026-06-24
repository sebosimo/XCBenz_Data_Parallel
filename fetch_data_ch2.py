import os, sys
import datetime, json, xarray as xr
import numpy as np
import warnings
import requests
import time
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Set GRIB definitions for COSMO/ICON (same as fetch_data.py)
COSMO_DEFS = r"C:\Users\sebas\.conda\envs\weather_final\share\eccodes-cosmo-resources\definitions"
STANDARD_DEFS = os.path.join(sys.prefix, "Library", "share", "eccodes", "definitions")

defs_to_use = []
if os.path.exists(COSMO_DEFS):
    defs_to_use.append(COSMO_DEFS)
if os.path.exists(STANDARD_DEFS):
    defs_to_use.append(STANDARD_DEFS)

if defs_to_use:
    final_def_path = ":".join(defs_to_use)
    os.environ["GRIB_DEFINITION_PATH"] = final_def_path
    os.environ["ECCODES_DEFINITION_PATH"] = final_def_path

from meteodatalab import ogd_api
from cloud_maps import (
    CACHE_DIR_CLOUD_MAPS,
    CloudMapAccumulator,
    cleanup_old_cloud_runs,
    is_cloud_maps_enabled,
    is_cloud_run_complete,
)
from rain_maps import (
    CACHE_DIR_RAIN_MAPS,
    RainMapAccumulator,
    cleanup_old_rain_runs,
    is_rain_maps_enabled,
    is_rain_run_complete,
)
from sunshine_maps import (
    CACHE_DIR_SUNSHINE_MAPS,
    SunshineMapAccumulator,
    cleanup_old_sunshine_runs,
    is_sunshine_maps_enabled,
    is_sunshine_run_complete,
)
from sunrain_maps import (
    CACHE_DIR_SUNRAIN_MAPS,
    SunRainMapAccumulator,
    cleanup_old_sunrain_runs,
    is_sunrain_maps_enabled,
    is_sunrain_run_complete,
)
from wind_maps import (
    CACHE_DIR_WIND_PACKED,
    WindMapAccumulator,
    cleanup_old_wind_runs,
    is_wind_maps_enabled,
    load_config as load_wind_map_config,
)
from web_profiles import (
    SURFACE_RADIATION_KEYS,
    build_bundle_step_values,
    clean_number,
    write_profile_chunk,
)

warnings.filterwarnings("ignore")

# --- Configuration ---
COLLECTION_CH2   = "ogd-forecasting-icon-ch2"
HHL_FILENAME     = "vertical_constants_icon-ch2-eps.grib2"
HGRID_FILENAME   = "horizontal_constants_icon-ch2-eps.grib2"
CACHE_DIR        = "cache_data_ch2"
CACHE_DIR_PACKED = "cache_data_ch2_packed"
CACHE_DIR_MAPS_PACKED = CACHE_DIR_WIND_PACKED
PROFILE_CHUNK_DIR = "web_profile_chunks"
STATIC_DIR       = "static_data"
MAX_HORIZON      = 120   # H000–H120, full 5-day forecast
VARS             = ["T", "U", "V", "P", "QV"]
VARS_NATIVE_10M_WIND = ["U_10M", "V_10M"]
VARS_RADIATION_AVERAGE = ["ASWDIR_S", "ASWDIFD_S"]   # surface SW radiation (running means from ref time)
VARS_SUNSHINE_ACCUM = ["DURSUN", "DURSUN_M"]          # sunshine duration / possible max (running sums)
VARS_SUNSHINE_MAPS = [*VARS_RADIATION_AVERAGE, *VARS_SUNSHINE_ACCUM]
VARS_RAIN_ACCUM = ["TOT_PREC"]
VARS_CLOUD_MAPS = ["CLCT", "CLCL", "CLCM", "CLCH"]
VARS_RADIATION = VARS_RADIATION_AVERAGE
SURFACE_SCALAR_UNITS = {
    "ASWDIR_S": "W m-2",
    "ASWDIFD_S": "W m-2",
}
NETCDF_ENGINE    = "netcdf4"
NETCDF_COMPRESS_KW = {"zlib": True, "shuffle": True, "complevel": 4}

STAC_BASE_URL  = "https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-forecasting-icon-ch2"
STAC_ASSETS_URL = f"{STAC_BASE_URL}/assets"

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(CACHE_DIR_PACKED, exist_ok=True)
os.makedirs(CACHE_DIR_MAPS_PACKED, exist_ok=True)
os.makedirs(CACHE_DIR_SUNSHINE_MAPS, exist_ok=True)
os.makedirs(CACHE_DIR_RAIN_MAPS, exist_ok=True)
os.makedirs(CACHE_DIR_CLOUD_MAPS, exist_ok=True)
os.makedirs(PROFILE_CHUNK_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)


def compressed_encoding(ds):
    return {name: dict(NETCDF_COMPRESS_KW) for name in ds.data_vars}

def packed_encoding(ds):
    encoding = {}
    for name, data_array in ds.data_vars.items():
        if data_array.dtype.kind in ("U", "S", "O"):
            encoding[name] = {}
        else:
            enc = dict(NETCDF_COMPRESS_KW)
            if name == "horizon":
                enc["dtype"] = "i2"
            elif name == "valid_time_epoch":
                enc["dtype"] = "i8"
            elif np.issubdtype(data_array.dtype, np.floating):
                enc["dtype"] = "f4"
            encoding[name] = enc
    return encoding


def get_iso_horizon(total_hours):
    days = total_hours // 24
    hours = total_hours % 24
    return f"P{days}DT{hours}H"


def sanitize_name(name):
    n = name.replace("ü", "ue").replace("ö", "oe").replace("ä", "ae") \
            .replace("Ü", "Ue").replace("Ö", "Oe").replace("Ä", "Ae").replace("ß", "ss")
    clean = "".join(c for c in n if c.isalnum() or c in ('-', '_'))
    return clean if clean else "unnamed"

def _step_number(step_label):
    return int(step_label.replace("H", ""))


def step_label(h):
    return f"H{h:03d}"


def env_flag(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name, default=1, minimum=1, maximum=16):
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def env_choice(name, default, choices):
    raw = os.getenv(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    return value if value in choices else default


def env_run_tag(name):
    raw = os.getenv(name)
    if not raw:
        return None
    value = str(raw).strip()
    for fmt in ("%Y%m%d_%H%M", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.datetime.strptime(value, fmt).replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            continue
    log(f"Ignoring invalid {name}={value!r}; expected YYYYMMDD_HHMM or ISO UTC.", "WARNING")
    return None


def log(msg, level="INFO"):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open("debug_log_ch2.txt", "a") as f:
        f.write(f"{timestamp} [{level}] {msg}\n")
    print(f"{timestamp} [{level}] {msg}", flush=True)


def download_file(url, target_path, max_retries=3, max_seconds=90):
    """Download url to target_path. Aborts if total download exceeds max_seconds."""
    for attempt in range(max_retries):
        try:
            log(f"Downloading {url} to {target_path}...")
            deadline = time.time() + max_seconds
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(target_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if time.time() > deadline:
                            raise TimeoutError(f"Download exceeded {max_seconds}s — aborting")
                        f.write(chunk)
            log(f"Download complete: {target_path}")
            return True
        except Exception as e:
            log(f"Download attempt {attempt + 1} failed: {e}", "ERROR")
            if os.path.exists(target_path):
                os.remove(target_path)  # clean up partial file before retry
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    return False


def fetch_variable_file(collection, variable, reference_datetime, horizon, target_path):
    try:
        req = ogd_api.Request(
            collection=collection,
            variable=variable,
            reference_datetime=reference_datetime,
            horizon=horizon,
            perturbed=False,
        )
        urls = ogd_api.get_asset_urls(req)
        if not urls:
            return variable, target_path, False
        return variable, target_path, download_file(urls[0], target_path)
    except Exception as exc:
        log(f"Fetch setup failed for {variable} {horizon}: {exc}", "WARNING")
        return variable, target_path, False


def has_profile_horizon(reference_datetime, horizon):
    """Return true when a CH2 run has the required pressure-level horizon."""
    try:
        req = ogd_api.Request(
            collection=COLLECTION_CH2,
            variable="T",
            reference_datetime=reference_datetime,
            horizon=get_iso_horizon(horizon),
            perturbed=False,
        )
        return bool(ogd_api.get_asset_urls(req))
    except Exception as exc:
        label = reference_datetime.strftime("%Y%m%d_%H%M")
        log(f"CH2 profile horizon probe failed for {label} H+{horizon:03d}: {exc}", "WARNING")
        return False


def fetch_variable_files(collection, variables, reference_datetime, horizon, tag, horizon_label, prefix):
    workers = env_int("DOWNLOAD_WORKERS", default=4, minimum=1, maximum=8)
    temp_dir = os.getenv("XCBENZ_FETCH_TMP_DIR")
    if temp_dir:
        os.makedirs(temp_dir, exist_ok=True)
    jobs = [
        (
            variable,
            os.path.join(temp_dir, f"{prefix}_{variable}_{tag}_{horizon_label}.grib2")
            if temp_dir
            else f"{prefix}_{variable}_{tag}_{horizon_label}.grib2",
        )
        for variable in variables
    ]
    if workers <= 1 or len(jobs) <= 1:
        results = [
            fetch_variable_file(collection, variable, reference_datetime, horizon, target_path)
            for variable, target_path in jobs
        ]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as executor:
            futures = [
                executor.submit(
                    fetch_variable_file,
                    collection,
                    variable,
                    reference_datetime,
                    horizon,
                    target_path,
                )
                for variable, target_path in jobs
            ]
            for future in as_completed(futures):
                results.append(future.result())

    return {
        variable: target_path
        for variable, target_path, ok in results
        if ok and os.path.exists(target_path)
    }


def download_static_files():
    """Download CH2 HHL and HGRID static files once per CI runner."""
    for filename in [HHL_FILENAME, HGRID_FILENAME]:
        path = os.path.join(STATIC_DIR, filename)
        if os.path.exists(path):
            log(f"Static file already present: {filename}")
            continue
        log(f"Downloading CH2 static file {filename}...")
        try:
            resp = requests.get(STAC_ASSETS_URL, timeout=15)
            resp.raise_for_status()
            assets = resp.json().get("assets", {})
            # Assets may be a dict keyed by ID or a list
            if isinstance(assets, dict):
                url = assets.get(filename, {}).get("href")
            else:
                url = next((a["href"] for a in assets if a.get("id") == filename), None)
            if url:
                download_file(url, path)
            else:
                log(f"Asset URL not found for {filename}", "ERROR")
        except Exception as e:
            log(f"Failed to fetch static {filename}: {e}", "ERROR")


def load_static_hhl():
    path = os.path.join(STATIC_DIR, HHL_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        ds = xr.open_dataset(path, engine='cfgrib', backend_kwargs={'indexpath': ''})
        var = next((v for v in ds.data_vars if v.lower() in ['h', 'hhl']), list(ds.data_vars)[0])
        hhl = ds[var].load()
        ds.close()
        return hhl
    except Exception as e:
        log(f"Error loading CH2 HHL: {e}", "ERROR")
        return None


def load_static_grid():
    path = os.path.join(STATIC_DIR, HGRID_FILENAME)
    if not os.path.exists(path):
        return None
    try:
        ds = xr.open_dataset(path, engine='cfgrib', backend_kwargs={'indexpath': ''})
        grid = {}
        for key in ['lat', 'lon']:
            match_k = next((k for k in list(ds.coords) + list(ds.data_vars) if key in k.lower()), None)
            if match_k:
                grid[key] = ds[match_k].load()
            else:
                grid[key] = None
        ds.close()
        return grid if grid.get('lat') is not None else None
    except Exception as e:
        log(f"Error loading CH2 HGRID: {e}", "ERROR")
        return None


def get_latest_available_runs(limit=2):
    """Discover available CH2 runs via active probing (6-hourly: 00Z/06Z/12Z/18Z)."""
    log("Discovering CH2 runs via Active Probing...")
    now = datetime.datetime.now(datetime.timezone.utc)
    # Round down to nearest 6-hour slot
    hour = (now.hour // 6) * 6
    start = now.replace(hour=hour, minute=0, second=0, microsecond=0)

    found_runs = []
    for i in range(20):  # Check last 120 hours (20 × 6h slots) to account for 3.5h pub delay
        cand = start - datetime.timedelta(hours=i * 6)
        ref = cand.strftime("%Y-%m-%dT%H:%M:%SZ")

        params = {"limit": 1, "forecast:reference_datetime": ref}
        try:
            r = requests.get(f"{STAC_BASE_URL}/items", params=params, timeout=15)
            if r.status_code == 200 and r.json().get("features"):
                found_runs.append(cand)
                log(f"Found available CH2 run: {ref}")
                if len(found_runs) >= limit:
                    break
        except Exception:
            pass
    return found_runs


def is_run_complete_locally(time_tag, locations, max_h):
    """Check if the last expected file exists — implies full run was downloaded."""
    last_loc = sanitize_name(list(locations.keys())[-1])
    trace_path = os.path.join(CACHE_DIR, time_tag, last_loc, f"H{max_h:03d}.nc")
    return os.path.exists(trace_path)


def is_horizon_complete_locally(time_tag, locations, h):
    """Check if all location .nc files for a given horizon already exist."""
    for name in locations:
        path = os.path.join(CACHE_DIR, time_tag, sanitize_name(name), f"H{h:03d}.nc")
        if not os.path.exists(path):
            return False
    return True

def is_packed_run_complete_locally(time_tag, locations):
    return all(
        os.path.exists(os.path.join(CACHE_DIR_PACKED, time_tag, f"{sanitize_name(name)}.nc"))
        for name in locations
    )

def build_packed_dataset_from_hourlies(tag, safe_name, location_name, location_meta, step_labels):
    level_height = None
    profile_columns = {var: [] for var in VARS}
    radiation_columns = {var: [] for var in VARS_RADIATION}
    valid_time_epochs = []
    available_steps = []
    ref_time = None

    for step_label in step_labels:
        step_path = os.path.join(CACHE_DIR, tag, safe_name, f"{step_label}.nc")
        if not os.path.exists(step_path):
            continue

        with xr.open_dataset(step_path, engine=NETCDF_ENGINE) as ds_in:
            ds_loaded = ds_in.load()

        if ref_time is None:
            ref_time = datetime.datetime.fromisoformat(str(ds_loaded.attrs["ref_time"]))

        height = ds_loaded["HEIGHT"].values.astype(np.float32)
        if level_height is None:
            level_height = height

        for var in VARS:
            profile_columns[var].append(ds_loaded[var].values.astype(np.float32))

        for var in VARS_RADIATION:
            if var in ds_loaded:
                radiation_columns[var].append(np.float32(ds_loaded[var].values))
            else:
                radiation_columns[var].append(np.float32(np.nan))

        valid_attr = ds_loaded.attrs.get("valid_time")
        if valid_attr:
            valid_time = datetime.datetime.fromisoformat(str(valid_attr))
        else:
            valid_time = ref_time + datetime.timedelta(hours=_step_number(step_label))
        if valid_time.tzinfo is None:
            valid_time = valid_time.replace(tzinfo=datetime.timezone.utc)
        valid_time_epochs.append(int(valid_time.timestamp()))
        available_steps.append(step_label)

    if not available_steps or level_height is None or ref_time is None:
        return None

    packed_vars = {
        "horizon": xr.DataArray(
            np.asarray([_step_number(step) for step in available_steps], dtype=np.int16),
            dims=["time"],
        ),
        "valid_time_epoch": xr.DataArray(
            np.asarray(valid_time_epochs, dtype=np.int64),
            dims=["time"],
        ),
        "step_label": xr.DataArray(np.asarray(available_steps), dims=["time"]),
        "height": xr.DataArray(level_height, dims=["level"]),
    }

    for var, columns in profile_columns.items():
        if columns:
            packed_vars[var] = xr.DataArray(np.stack(columns).astype(np.float32), dims=["time", "level"])

    for var, values in radiation_columns.items():
        if values and not np.all(np.isnan(values)):
            packed_vars[var] = xr.DataArray(np.asarray(values, dtype=np.float32), dims=["time"])

    packed_ds = xr.Dataset(packed_vars)
    packed_ds.attrs = {
        "location": location_meta.get("display_name", location_name),
        "location_id": location_name,
        "display_name": location_meta.get("display_name", location_name),
        "point_type": location_meta.get("type", "legacy"),
        "region_name": location_meta.get("region_name") or "",
        "latitude": float(location_meta["lat"]),
        "longitude": float(location_meta["lon"]),
        "ref_time": ref_time.isoformat(),
        "model": "icon-ch2",
        "step_label_pad": 3,
        "schema_version": 1,
    }
    return packed_ds

def write_packed_run_files(tag, locations):
    run_dir = os.path.join(CACHE_DIR, tag)
    if not os.path.isdir(run_dir):
        return

    packed_run_dir = os.path.join(CACHE_DIR_PACKED, tag)
    os.makedirs(packed_run_dir, exist_ok=True)

    for location_name, location_meta in locations.items():
        safe_name = sanitize_name(location_name)
        step_dir = os.path.join(run_dir, safe_name)
        if not os.path.isdir(step_dir):
            continue
        step_labels = sorted(
            f.replace(".nc", "")
            for f in os.listdir(step_dir)
            if f.endswith(".nc") and f.startswith("H")
        )
        packed_ds = build_packed_dataset_from_hourlies(tag, safe_name, location_name, location_meta, step_labels)
        if packed_ds is None:
            continue

        packed_path = os.path.join(packed_run_dir, f"{safe_name}.nc")
        tmp_path = packed_path + ".tmp"
        packed_ds.to_netcdf(
            tmp_path,
            engine=NETCDF_ENGINE,
            format="NETCDF4",
            encoding=packed_encoding(packed_ds),
        )
        os.replace(tmp_path, packed_path)
        log(f"Packed CH2 location written: {tag}/{safe_name}.nc")


def process_traces(fields, locations, tag, h, ref, rad_scalars=None):
    """Extract point profiles for all locations and save as .nc files (3-digit horizon)."""
    sample = list(fields.values())[0]
    lat_n = 'latitude' if 'latitude' in sample.coords else 'lat'
    lon_n = 'longitude' if 'longitude' in sample.coords else 'lon'
    lats, lons = sample[lat_n].values, sample[lon_n].values
    indices = {n: int(np.argmin((lats - c['lat']) ** 2 + (lons - c['lon']) ** 2))
               for n, c in locations.items()}

    for name, idx in indices.items():
        safe_name = sanitize_name(name)
        loc_dir = os.path.join(CACHE_DIR, tag, safe_name)
        os.makedirs(loc_dir, exist_ok=True)
        filename = f"H{h:03d}.nc"   # 3-digit padding for H000–H120
        path = os.path.join(loc_dir, filename)

        if os.path.exists(path):
            continue

        loc_vars = {}

        # Compute full-level heights from HHL half-levels if available
        if "HHL" in fields:
            ds_hhl = fields["HHL"]
            s_dim = ds_hhl[lat_n].dims[0]
            hhl_profile = ds_hhl.squeeze().isel({s_dim: idx}).compute()
            h_vals = hhl_profile.values
            height_centers = (h_vals[:-1] + h_vals[1:]) / 2.0
            loc_vars["HEIGHT"] = xr.DataArray(height_centers, dims=["level"])

        for var, ds in fields.items():
            if var == "HHL":
                continue
            s_dim = ds[lat_n].dims[0]
            profile = ds.squeeze().isel({s_dim: idx}).compute()
            if profile.dims:
                profile = profile.rename({profile.dims[0]: 'level'})
            loc_vars[var] = profile.drop_vars([c for c in profile.coords if c not in profile.dims])

        # Store de-accumulated radiation scalars (if provided)
        if rad_scalars:
            for var, val in rad_scalars.items():
                loc_vars[var] = xr.DataArray(float(val), attrs={"units": SURFACE_SCALAR_UNITS.get(var, "")})

        ds_out = xr.Dataset(loc_vars)
        valid_time = ref + datetime.timedelta(hours=h)
        coords = locations[name]
        ds_out.attrs = {
            "location": coords.get("display_name", name),
            "location_id": name,
            "display_name": coords.get("display_name", name),
            "point_type": coords.get("type", "legacy"),
            "region_name": coords.get("region_name") or "",
            "latitude": float(coords["lat"]),
            "longitude": float(coords["lon"]),
            "ref_time": ref.isoformat(),
            "horizon": h,
            "valid_time": valid_time.isoformat(),
            "model": "icon-ch2",
        }
        ds_out.to_netcdf(
            path,
            engine=NETCDF_ENGINE,
            format="NETCDF4",
            encoding=compressed_encoding(ds_out),
        )


def _sample_field(fields):
    return next((v for v in fields.values() if v is not None and hasattr(v, "dims")), None)


def _field_lat_lon_names(sample):
    return "latitude" if "latitude" in sample.coords else "lat", "longitude" if "longitude" in sample.coords else "lon"


def _location_indices(sample, locations):
    lat_n, lon_n = _field_lat_lon_names(sample)
    lats, lons = sample[lat_n].values, sample[lon_n].values
    return {
        name: int(np.argmin((lats - coords["lat"]) ** 2 + (lons - coords["lon"]) ** 2))
        for name, coords in locations.items()
    }


def _point_profile(data, lat_name, idx):
    spatial_dim = data[lat_name].dims[0]
    profile = data.squeeze().isel({spatial_dim: idx}).compute()
    return np.asarray(profile.values, dtype=np.float32).ravel()


def _height_profile(fields, lat_name, idx, fallback_level_count):
    if "HHL" not in fields or fields["HHL"] is None:
        return np.arange(fallback_level_count, dtype=np.float32)
    hhl = fields["HHL"]
    spatial_dim = hhl[lat_name].dims[0]
    hhl_profile = hhl.squeeze().isel({spatial_dim: idx}).compute()
    h_vals = np.asarray(hhl_profile.values, dtype=np.float32).ravel()
    return ((h_vals[:-1] + h_vals[1:]) / 2.0).astype(np.float32)


def append_profile_chunk(buffers, fields, locations, tag, h, ref, loc_rad_map=None):
    sample = _sample_field(fields)
    if sample is None:
        return False
    lat_name, _lon_name = _field_lat_lon_names(sample)
    indices = _location_indices(sample, locations)
    valid_time = ref + datetime.timedelta(hours=h)

    for name, idx in indices.items():
        raw_profiles = {}
        for var in VARS:
            if var not in fields:
                raw_profiles[var] = None
                continue
            raw_profiles[var] = _point_profile(fields[var], lat_name, idx)

        level_source = next((arr for arr in raw_profiles.values() if arr is not None), None)
        if level_source is None:
            continue
        level_count = int(level_source.shape[0])
        height = _height_profile(fields, lat_name, idx, level_count)
        if height.shape[0] != level_count:
            log(
                f"CH2 direct profile height length mismatch for {name} H+{h:03d}: "
                f"{height.shape[0]} != {level_count}",
                "WARNING",
            )
            height = height[:level_count]

        buffer = buffers.setdefault(name, {"height": height, "steps": [], "values": []})
        if len(buffer["height"]) != len(height) or not np.allclose(buffer["height"], height, equal_nan=True):
            log(f"CH2 direct profile height changed within chunk for {name} H+{h:03d}", "WARNING")

        surface = {}
        for source_key, output_key in SURFACE_RADIATION_KEYS.items():
            raw_value = (loc_rad_map or {}).get(name, {}).get(source_key)
            value = clean_number(raw_value, 2)
            if value is not None:
                surface[output_key] = value

        buffer["steps"].append(
            {
                "step": step_label(h),
                "valid_time": valid_time.isoformat(),
                "horizon": h,
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


def finalize_profile_chunk(buffers, locations, tag, chunk_id, ref):
    written = 0
    for name, buffer in buffers.items():
        if not buffer["steps"]:
            continue
        values = np.stack(buffer["values"]).astype("<f4")
        write_profile_chunk(
            output_root=Path(PROFILE_CHUNK_DIR),
            model_key="icon-ch2",
            run_tag=tag,
            chunk_id=chunk_id,
            location_id=name,
            location_meta=locations[name],
            ref_time=ref.isoformat(),
            height_values=np.asarray(buffer["height"], dtype=np.float32),
            steps=buffer["steps"],
            values=values,
        )
        written += 1
    log(f"CH2 direct profile chunk {chunk_id} wrote {written} location artifact(s)", "NOTICE")
    return written


def seed_previous_radiation(collection, ref_time, tag, start_h):
    if start_h <= 1:
        return {var: None for var in VARS_SUNSHINE_MAPS}

    seed_h = start_h - 1
    iso_h = get_iso_horizon(seed_h)
    previous = {var: None for var in VARS_SUNSHINE_MAPS}
    downloaded = fetch_variable_files(
        collection,
        VARS_SUNSHINE_MAPS,
        ref_time,
        iso_h,
        tag,
        f"{seed_h:03d}",
        "temp_ch2_rad_seed",
    )
    for var, tmp in downloaded.items():
        try:
            ds_r = xr.open_dataset(tmp, engine="cfgrib", backend_kwargs={"indexpath": ""})
            previous[var] = ds_r[next(iter(ds_r.data_vars))].load().values.ravel()
            ds_r.close()
        except Exception as e:
            log(f"CH2 radiation seed decode failed for {var} H+{seed_h:03d}: {e}", "WARNING")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return previous


def seed_previous_rain(collection, ref_time, tag, start_h):
    if start_h <= 1:
        return {var: None for var in VARS_RAIN_ACCUM}

    seed_h = start_h - 1
    iso_h = get_iso_horizon(seed_h)
    previous = {var: None for var in VARS_RAIN_ACCUM}
    downloaded = fetch_variable_files(
        collection,
        VARS_RAIN_ACCUM,
        ref_time,
        iso_h,
        tag,
        f"{seed_h:03d}",
        "temp_ch2_rain_seed",
    )
    for var, tmp in downloaded.items():
        try:
            ds_r = xr.open_dataset(tmp, engine="cfgrib", backend_kwargs={"indexpath": ""})
            previous[var] = ds_r[next(iter(ds_r.data_vars))].load().values.ravel()
            ds_r.close()
        except Exception as e:
            log(f"CH2 rain seed decode failed for {var} H+{seed_h:03d}: {e}", "WARNING")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return previous


def cleanup_old_runs():
    """Keep top-2 most recent CH2 runs + the 00Z anchor run from today/yesterday."""
    now = datetime.datetime.now(datetime.timezone.utc)
    keep_dates = {now.date(), (now - datetime.timedelta(days=1)).date()}

    for cache_dir in [CACHE_DIR, CACHE_DIR_PACKED]:
        if not os.path.exists(cache_dir):
            continue

        all_runs = sorted(
            [item for item in os.listdir(cache_dir) if os.path.isdir(os.path.join(cache_dir, item))],
            reverse=True
        )
        keep_recent = set(all_runs[:2])

        for item in all_runs:
            if item in keep_recent:
                continue
            path = os.path.join(cache_dir, item)
            try:
                dt = datetime.datetime.strptime(item, "%Y%m%d_%H%M").replace(
                    tzinfo=datetime.timezone.utc)
            except ValueError:
                continue
            if dt.hour == 0 and dt.minute == 0 and dt.date() in keep_dates:
                continue
            try:
                shutil.rmtree(path)
                log(f"CH2 cleanup: removed {cache_dir}/{item}")
            except Exception as e:
                log(f"CH2 cleanup failed {cache_dir}/{item}: {e}", "ERROR")


def main():
    log("=== CH2 Data Fetcher Start ===")
    force_refresh = env_flag("FORCE_REFRESH", default=False)
    profile_mode = env_choice("CH2_PROFILE_MODE", "netcdf", {"netcdf", "direct-chunk", "none"})
    horizon_start = env_int("CH2_HORIZON_START", default=0, minimum=0, maximum=MAX_HORIZON)
    horizon_end = env_int("CH2_HORIZON_END", default=MAX_HORIZON, minimum=0, maximum=MAX_HORIZON)
    if horizon_end < horizon_start:
        horizon_start, horizon_end = horizon_end, horizon_start
    chunk_id = os.getenv("CH2_PROFILE_CHUNK_ID") or f"H{horizon_start:03d}_H{horizon_end:03d}"
    pinned_run = env_run_tag("CH2_RUN_TAG") or env_run_tag("CH2_REFERENCE_TIME")
    if force_refresh:
        log("FORCE_REFRESH enabled: existing CH2 run/horizon-complete checks will be ignored.", "NOTICE")
    log(
        f"CH2 profile mode: {profile_mode}; horizon range H{horizon_start:03d}-H{horizon_end:03d}; "
        f"chunk_id={chunk_id}",
        "NOTICE",
    )
    if pinned_run is not None:
        log(f"CH2 pinned run: {pinned_run.strftime('%Y%m%d_%H%M')}", "NOTICE")

    wind_config = None
    wind_map_out_root = os.getenv("CH2_WIND_MAP_OUT_ROOT", CACHE_DIR_WIND_PACKED)
    sunshine_map_out_root = os.getenv("CH2_SUNSHINE_MAP_OUT_ROOT", CACHE_DIR_SUNSHINE_MAPS)
    rain_map_out_root = os.getenv("CH2_RAIN_MAP_OUT_ROOT", CACHE_DIR_RAIN_MAPS)
    sunrain_map_out_root = os.getenv("CH2_SUNRAIN_MAP_OUT_ROOT", CACHE_DIR_SUNRAIN_MAPS)
    cloud_map_out_root = os.getenv("CH2_CLOUD_MAP_OUT_ROOT", CACHE_DIR_CLOUD_MAPS)
    require_full_horizon_run = env_flag("CH2_REQUIRE_FULL_HORIZON_RUN", profile_mode == "direct-chunk")
    wind_enabled = is_wind_maps_enabled("ch2")
    sunshine_enabled = is_sunshine_maps_enabled("ch2")
    rain_enabled = is_rain_maps_enabled("ch2")
    sunrain_enabled = is_sunrain_maps_enabled("ch2")
    cloud_enabled = is_cloud_maps_enabled("ch2")
    if wind_enabled or sunshine_enabled or rain_enabled or sunrain_enabled or cloud_enabled:
        try:
            wind_config = load_wind_map_config(log=log)
            if wind_enabled:
                log("CH2 wind-map generation enabled for this run.", "NOTICE")
            if sunshine_enabled:
                log("CH2 sunshine-map generation enabled for this run.", "NOTICE")
            if rain_enabled:
                log("CH2 rain-map generation enabled for this run.", "NOTICE")
            if sunrain_enabled:
                log("CH2 Sun+Rain map generation enabled for this run.", "NOTICE")
            if cloud_enabled:
                log("CH2 cloud-map generation enabled for this run.", "NOTICE")
        except Exception as e:
            wind_enabled = False
            sunshine_enabled = False
            rain_enabled = False
            sunrain_enabled = False
            cloud_enabled = False
            log(f"CH2 map generation disabled: {e}", "WARNING")
    else:
        log("CH2 wind/sunshine/rain/sunrain/cloud map generation disabled by flags.")

    download_static_files()

    if not os.path.exists("locations.json"):
        log("locations.json not found.", "ERROR")
        return
    with open("locations.json", "r", encoding="utf-8") as f:
        locations = json.load(f)

    runs = [pinned_run] if pinned_run is not None else get_latest_available_runs(limit=2)
    if not runs:
        log("No CH2 runs found.")
        return

    hhl = load_static_hhl()
    grid = load_static_grid()

    # Inject lat/lon coordinates into HHL so process_traces can locate grid points
    if hhl is not None and grid is not None:
        n_grid = grid['lat'].size
        match_dim = next((d for d in hhl.dims if hhl.sizes[d] == n_grid), None)
        if match_dim:
            hhl = hhl.assign_coords({
                "latitude": (match_dim, grid['lat'].values),
                "longitude": (match_dim, grid['lon'].values)
            })

    for ref_time in runs:
        tag = ref_time.strftime('%Y%m%d_%H%M')
        sunshine_missing = sunshine_enabled and not is_sunshine_run_complete("ch2", tag, root=sunshine_map_out_root)
        rain_missing = rain_enabled and not is_rain_run_complete("ch2", tag, root=rain_map_out_root)
        sunrain_missing = sunrain_enabled and not is_sunrain_run_complete("ch2", tag, root=sunrain_map_out_root)
        cloud_missing = cloud_enabled and not is_cloud_run_complete("ch2", tag, root=cloud_map_out_root)
        if require_full_horizon_run and not has_profile_horizon(ref_time, MAX_HORIZON):
            log(f"CH2 run {tag} does not expose H{MAX_HORIZON:03d} yet; trying next available run.")
            continue
        if (
            profile_mode == "netcdf"
            and not force_refresh
            and is_run_complete_locally(tag, locations, MAX_HORIZON)
            and not sunshine_missing
            and not rain_missing
            and not sunrain_missing
            and not cloud_missing
        ):
            if not is_packed_run_complete_locally(tag, locations):
                write_packed_run_files(tag, locations)
            log(f"CH2 run {tag} already complete locally — skipping.")
            break

        log(f"Processing CH2 run: {tag} (H{horizon_start:03d}-H{horizon_end:03d})")
        any_success = False
        profile_buffers = {} if profile_mode == "direct-chunk" else None
        wind_accumulator = (
            WindMapAccumulator("ch2", tag, ref_time, wind_config, log=log, out_root=wind_map_out_root)
            if wind_enabled and wind_config is not None
            else None
        )
        sunshine_accumulator = (
            SunshineMapAccumulator("ch2", tag, ref_time, wind_config, log=log, out_root=sunshine_map_out_root)
            if sunshine_enabled and wind_config is not None
            else None
        )
        rain_accumulator = (
            RainMapAccumulator("ch2", tag, ref_time, wind_config, log=log, out_root=rain_map_out_root)
            if rain_enabled and wind_config is not None
            else None
        )
        sunrain_accumulator = (
            SunRainMapAccumulator("ch2", tag, ref_time, wind_config, log=log, out_root=sunrain_map_out_root)
            if sunrain_enabled and wind_config is not None
            else None
        )
        cloud_accumulator = (
            CloudMapAccumulator("ch2", tag, ref_time, wind_config, log=log, out_root=cloud_map_out_root)
            if cloud_enabled and wind_config is not None
            else None
        )
        # Cache previous raw radiation values for de-accumulation (running mean → hourly mean)
        prev_rad_raw = seed_previous_radiation(COLLECTION_CH2, ref_time, tag, horizon_start)
        if rain_accumulator is not None or sunrain_accumulator is not None:
            rain_seed = seed_previous_rain(COLLECTION_CH2, ref_time, tag, horizon_start)
            if rain_accumulator is not None:
                rain_accumulator.seed_previous_raw(rain_seed.get("TOT_PREC"))
            if sunrain_accumulator is not None:
                sunrain_accumulator.seed_previous_raw(rain_seed.get("TOT_PREC"))

        for h in range(horizon_start, horizon_end + 1):
            # Skip horizons where all location .nc files are already on disk
            if (
                profile_mode == "netcdf"
                and not force_refresh
                and not sunshine_missing
                and not rain_missing
                and not sunrain_missing
                and not cloud_missing
                and is_horizon_complete_locally(tag, locations, h)
            ):
                any_success = True   # count existing horizons toward run completion
                continue

            iso_h = get_iso_horizon(h)

            fields = {"HHL": hhl} if hhl is not None else {}
            has_new_data = False
            profile_variables = VARS if profile_mode in {"netcdf", "direct-chunk"} else []
            map_variables = ["U", "V", *VARS_NATIVE_10M_WIND] if (wind_enabled or sunshine_enabled) else []
            variables_to_fetch = list(dict.fromkeys([*profile_variables, *map_variables]))

            downloaded_fields = fetch_variable_files(
                COLLECTION_CH2,
                variables_to_fetch,
                ref_time,
                iso_h,
                tag,
                f"{h:03d}",
                "temp_ch2",
            )
            for var, tmp in downloaded_fields.items():
                try:
                    ds = xr.open_dataset(tmp, engine='cfgrib',
                                         backend_kwargs={'indexpath': ''})
                    data = ds[next(iter(ds.data_vars))].load()
                    if grid:
                        m_dim = next(d for d in data.dims
                                     if data.sizes[d] == grid['lat'].size)
                        data = data.assign_coords({
                            "latitude": (m_dim, grid['lat'].values),
                            "longitude": (m_dim, grid['lon'].values)
                        })
                    fields[var] = data
                    ds.close()
                    has_new_data = True
                except Exception as e:
                    log(f"CH2 decode failed for {var} H+{h:03d}: {e}", "WARNING")
                finally:
                    if os.path.exists(tmp):
                        os.remove(tmp)

            # --- Radiation fetch and de-accumulation (same logic as fetch_data.py) ---
            rad_scalars = {}
            sample_field = next((v for v in fields.values()
                                  if v is not None and hasattr(v, 'dims')), None)
            rain_scalars = {}
            rain_sample_field = None
            if rain_accumulator is not None or sunrain_accumulator is not None:
                downloaded_rain = fetch_variable_files(
                    COLLECTION_CH2,
                    VARS_RAIN_ACCUM,
                    ref_time,
                    iso_h,
                    tag,
                    f"{h:03d}",
                    "temp_ch2_rain",
                )
                for var, tmp in downloaded_rain.items():
                    try:
                        ds_rain = xr.open_dataset(tmp, engine='cfgrib', backend_kwargs={'indexpath': ''})
                        rain_data = ds_rain[next(iter(ds_rain.data_vars))].load()
                        if grid:
                            m_dim = next(d for d in rain_data.dims if rain_data.sizes[d] == grid['lat'].size)
                            rain_data = rain_data.assign_coords({
                                "latitude": (m_dim, grid['lat'].values),
                                "longitude": (m_dim, grid['lon'].values),
                            })
                        rain_scalars[var] = rain_data.values.ravel()
                        rain_sample_field = rain_data
                        if sample_field is None:
                            sample_field = rain_sample_field
                        ds_rain.close()
                        has_new_data = True
                    except Exception as e:
                        log(f"CH2 rain decode failed for {var} H+{h:03d}: {e}", "WARNING")
                    finally:
                        if os.path.exists(tmp):
                            os.remove(tmp)

            cloud_scalars = {}
            cloud_sample_field = None
            if cloud_accumulator is not None:
                downloaded_cloud = fetch_variable_files(
                    COLLECTION_CH2,
                    VARS_CLOUD_MAPS,
                    ref_time,
                    iso_h,
                    tag,
                    f"{h:03d}",
                    "temp_ch2_cloud",
                )
                for var, tmp in downloaded_cloud.items():
                    try:
                        ds_cloud = xr.open_dataset(tmp, engine='cfgrib',
                                                   backend_kwargs={'indexpath': ''})
                        cloud_data = ds_cloud[next(iter(ds_cloud.data_vars))].load()
                        if grid:
                            m_dim = next(d for d in cloud_data.dims
                                         if cloud_data.sizes[d] == grid['lat'].size)
                            cloud_data = cloud_data.assign_coords({
                                "latitude": (m_dim, grid['lat'].values),
                                "longitude": (m_dim, grid['lon'].values),
                            })
                        cloud_scalars[var] = cloud_data.values.ravel()
                        cloud_sample_field = cloud_data
                        if sample_field is None:
                            sample_field = cloud_sample_field
                        ds_cloud.close()
                        has_new_data = True
                    except Exception as e:
                        log(f"CH2 cloud decode failed for {var} H+{h:03d}: {e}", "WARNING")
                    finally:
                        if os.path.exists(tmp):
                            os.remove(tmp)

            radiation_needed = (
                profile_mode in {"netcdf", "direct-chunk"}
                or sunshine_accumulator is not None
                or sunrain_accumulator is not None
            )
            if h > 0 and radiation_needed and sample_field is not None:
                lat_n = 'latitude' if 'latitude' in sample_field.coords else 'lat'
                lon_n = 'longitude' if 'longitude' in sample_field.coords else 'lon'
                lats_f = sample_field[lat_n].values
                lons_f = sample_field[lon_n].values
                downloaded_radiation = fetch_variable_files(
                    COLLECTION_CH2,
                    VARS_SUNSHINE_MAPS,
                    ref_time,
                    iso_h,
                    tag,
                    f"{h:03d}",
                    "temp_ch2_rad",
                )
                for var, tmp in downloaded_radiation.items():
                    try:
                        ds_r = xr.open_dataset(tmp, engine='cfgrib',
                                               backend_kwargs={'indexpath': ''})
                        raw_data = ds_r[next(iter(ds_r.data_vars))].load().values.ravel()
                        ds_r.close()
                        prev_raw_arr = prev_rad_raw[var]
                        if prev_raw_arr is None:
                            prev_raw_arr = np.zeros_like(raw_data)
                        if var in VARS_RADIATION_AVERAGE:
                            # Convert running mean from reference time to hourly mean.
                            deacc_arr = h * raw_data - (h - 1) * prev_raw_arr
                        else:
                            # Convert running sum from reference time to interval seconds.
                            deacc_arr = raw_data - prev_raw_arr
                        deacc_arr = np.maximum(deacc_arr, 0.0)
                        prev_rad_raw[var] = raw_data
                        rad_scalars[var] = deacc_arr
                    except Exception as e:
                        log(f"CH2 radiation decode failed for {var} H+{h:03d}: {e}", "WARNING")
                    finally:
                        if os.path.exists(tmp):
                            os.remove(tmp)

            if has_new_data:
                loc_rad_map = {}
                if rad_scalars and sample_field is not None:
                    # Build per-location radiation scalars
                    for name, coords in locations.items():
                        idx_loc = int(np.argmin(
                            (lats_f - coords['lat'])**2 + (lons_f - coords['lon'])**2
                        ))
                        loc_rad_map[name] = {
                            v: float(arr.ravel()[idx_loc])
                            for v, arr in rad_scalars.items()
                            if v in VARS_RADIATION
                        }
                    # Locations without radiation (fallback — none expected)
                if profile_mode == "netcdf" and loc_rad_map:
                    for name, coords in locations.items():
                        process_traces(fields, {name: coords}, tag, h, ref_time, rad_scalars=loc_rad_map.get(name))
                elif profile_mode == "netcdf":
                    process_traces(fields, locations, tag, h, ref_time)
                elif profile_mode == "direct-chunk" and profile_buffers is not None:
                    append_profile_chunk(profile_buffers, fields, locations, tag, h, ref_time, loc_rad_map)
                if sunshine_accumulator is not None and rad_scalars and sample_field is not None:
                    sunshine_accumulator.append(sample_field, rad_scalars, h, ref_time)
                if rain_accumulator is not None and rain_scalars and rain_sample_field is not None:
                    rain_accumulator.append(rain_sample_field, rain_scalars, h, ref_time)
                if (
                    sunrain_accumulator is not None
                    and rad_scalars
                    and rain_scalars
                    and sample_field is not None
                ):
                    sunrain_accumulator.append(sample_field, rad_scalars, rain_scalars, h, ref_time)
                if cloud_accumulator is not None and cloud_scalars and cloud_sample_field is not None:
                    cloud_accumulator.append(cloud_sample_field, cloud_scalars, h, ref_time)
                if wind_accumulator is not None:
                    wind_accumulator.append(fields, h, ref_time)
                log(f"CH2 H+{h:03d} done")
                any_success = True

        if any_success:
            log(f"CH2 run {tag} complete.", "NOTICE")
            if wind_accumulator is not None:
                wind_accumulator.finalize()
            if sunshine_accumulator is not None:
                sunshine_accumulator.finalize()
            if rain_accumulator is not None:
                rain_accumulator.finalize()
            if sunrain_accumulator is not None:
                sunrain_accumulator.finalize()
            if cloud_accumulator is not None:
                cloud_accumulator.finalize()
            if profile_mode == "direct-chunk" and profile_buffers is not None:
                finalize_profile_chunk(profile_buffers, locations, tag, chunk_id, ref_time)
            if profile_mode == "netcdf":
                write_packed_run_files(tag, locations)
            if profile_mode != "netcdf":
                break
            # Compute thermal forecasts for the newly fetched CH2 run
            try:
                import compute_thermals
                log(f"Computing CH2 thermals for {tag}…")
                compute_thermals.process_run(tag, CACHE_DIR)
                log(f"CH2 thermals complete for {tag}.", "NOTICE")
            except Exception as e:
                log(f"CH2 thermal computation failed for {tag}: {e}", "WARNING")
            break
        else:
            log(f"CH2 run {tag} yielded no data, trying next available run...")
            p = os.path.join(CACHE_DIR, tag)
            if os.path.exists(p) and not os.listdir(p):
                try:
                    shutil.rmtree(p)
                except Exception:
                    pass

    cleanup_old_runs()
    cleanup_old_wind_runs("ch2", anchor_hour=0, log=log)
    cleanup_old_sunshine_runs("ch2", anchor_hour=0, log=log)
    cleanup_old_rain_runs("ch2", anchor_hour=0, log=log)
    cleanup_old_sunrain_runs("ch2", anchor_hour=0, log=log)
    cleanup_old_cloud_runs("ch2", anchor_hour=0, log=log)
    log("=== CH2 Data Fetcher Complete ===", "NOTICE")


if __name__ == "__main__":
    main()
