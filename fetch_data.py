import os, sys
import datetime, json, xarray as xr
import numpy as np
import warnings
import requests
import time
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Set GRIB definitions for COSMO/ICON
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
    CACHE_DIR_WIND_MAPS,
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

# Suppress warnings
warnings.filterwarnings("ignore")

# --- Configuration ---
VARS_TRACES = ["T", "U", "V", "P", "QV"]
VARS_MAPS = ["U", "V", "HHL"]
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
CACHE_DIR_MAPS_PACKED = CACHE_DIR_WIND_MAPS
PROFILE_CHUNK_DIR = "web_profile_chunks"
STATIC_DIR = "static_data"
HHL_FILENAME = "vertical_constants_icon-ch1-eps.grib2"
HGRID_FILENAME = "horizontal_constants_icon-ch1-eps.grib2"
STAC_BASE_URL = "https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-forecasting-icon-ch1"
STAC_ASSETS_URL = f"{STAC_BASE_URL}/assets"

WIND_LEVELS = []

os.makedirs(CACHE_DIR_MAPS_PACKED, exist_ok=True)
os.makedirs(CACHE_DIR_SUNSHINE_MAPS, exist_ok=True)
os.makedirs(CACHE_DIR_RAIN_MAPS, exist_ok=True)
os.makedirs(CACHE_DIR_CLOUD_MAPS, exist_ok=True)
os.makedirs(PROFILE_CHUNK_DIR, exist_ok=True)


def get_iso_horizon(total_hours):
    days = total_hours // 24
    hours = total_hours % 24
    return f"P{days}DT{hours}H"

def sanitize_name(name):
    n = name
    for src, dst in {"\u00fc": "ue", "\u00f6": "oe", "\u00e4": "ae", "\u00dc": "Ue", "\u00d6": "Oe", "\u00c4": "Ae", "\u00df": "ss"}.items():
        n = n.replace(src, dst)
    clean = "".join(c for c in n if c.isalnum() or c in ('-', '_'))
    return clean if clean else "unnamed"

def _step_number(step_label):
    return int(step_label.replace("H", ""))

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

def step_label(h):
    return f"H{h:02d}"

def log(msg, level="INFO"):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open("debug_log.txt", "a") as f:
        f.write(f"{timestamp} [{level}] {msg}\n")
    print(f"{timestamp} [{level}] {msg}", flush=True)

def download_file(url, target_path, max_retries=3):
    """Downloads a file with retries and exponential backoff."""
    backoff = 2
    for attempt in range(max_retries):
        try:
            log(f"Downloading {url} to {target_path}...")
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(target_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024*1024):
                        f.write(chunk)
            log(f"Download complete: {target_path}")
            return True
        except Exception as e:
            log(f"Download attempt {attempt+1} failed: {e}", "ERROR")
            if attempt < max_retries - 1:
                time.sleep(backoff ** attempt)
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
    """Return true when a CH1 run has the required pressure-level horizon."""
    try:
        req = ogd_api.Request(
            collection="ogd-forecasting-icon-ch1",
            variable="T",
            reference_datetime=reference_datetime,
            horizon=get_iso_horizon(horizon),
            perturbed=False,
        )
        return bool(ogd_api.get_asset_urls(req))
    except Exception as exc:
        label = reference_datetime.strftime("%Y%m%d_%H%M")
        log(f"CH1 profile horizon probe failed for {label} H+{horizon:03d}: {exc}", "WARNING")
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

def get_latest_available_runs(limit=1):
    """Discovers actual runs available on the server using Active Probing."""
    log("Discovering runs via Active Probing...")
    now = datetime.datetime.now(datetime.timezone.utc)
    hour = (now.hour // 3) * 3
    start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    
    found_runs = []
    for i in range(16): # Check last 48 hours
        cand = start - datetime.timedelta(hours=i*3)
        ref = cand.strftime("%Y-%m-%dT%H:%M:%SZ")
        
        params = {"limit": 1, "forecast:reference_datetime": ref}
        try:
            r = requests.get(f"{STAC_BASE_URL}/items", params=params, timeout=10)
            if r.status_code == 200 and r.json().get("features"):
                found_runs.append(cand)
                log(f"Found available run: {ref}")
                if len(found_runs) >= limit: break
        except: pass
    return found_runs

def download_static_files():
    os.makedirs(STATIC_DIR, exist_ok=True)
    for filename in [HHL_FILENAME, HGRID_FILENAME]:
        path = os.path.join(STATIC_DIR, filename)
        if not os.path.exists(path):
            log(f"Downloading static file {filename}...")
            try:
                resp = requests.get(STAC_ASSETS_URL, timeout=10)
                assets = resp.json()["assets"]
                url = next((a["href"] for a in assets if a.get("id") == filename), None)
                if url: download_file(url, path)
            except Exception as e: log(f"Failed to fetch static {filename}: {e}", "ERROR")

def load_static_hhl():
    path = os.path.join(STATIC_DIR, HHL_FILENAME)
    if not os.path.exists(path): return None
    try:
        ds = xr.open_dataset(path, engine='cfgrib', backend_kwargs={'indexpath': ''})
        var = next((v for v in ds.data_vars if v.lower() in ['h', 'hhl']), list(ds.data_vars)[0])
        hhl = ds[var].load()
        ds.close()
        return hhl
    except Exception as e: log(f"Error loading HHL: {e}", "ERROR"); return None

def load_static_grid():
    path = os.path.join(STATIC_DIR, HGRID_FILENAME)
    if not os.path.exists(path): return None
    try:
        ds = xr.open_dataset(path, engine='cfgrib', backend_kwargs={'indexpath': ''})
        grid = {}
        for key in ['lat', 'lon']:
            # Search both coordinates and data variables
            match_k = next((k for k in list(ds.coords) + list(ds.data_vars) if key in k.lower()), None)
            if match_k:
                grid[key] = ds[match_k].load()
            else:
                grid[key] = None
        ds.close()
        return grid if grid.get('lat') is not None else None
    except Exception as e: log(f"Error loading HGRID: {e}", "ERROR"); return None

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


def _point_profiles(data, lat_name, indices):
    names = list(indices.keys())
    if not names:
        return names, np.empty((0, 0), dtype=np.float32)
    spatial_dim = data[lat_name].dims[0]
    selected = data.squeeze().isel({spatial_dim: [indices[name] for name in names]}).compute()
    values = np.asarray(selected.values, dtype=np.float32)
    if spatial_dim in selected.dims:
        values = np.moveaxis(values, selected.get_axis_num(spatial_dim), 0)
    else:
        values = values.reshape((1, -1))
    return names, values.reshape((len(names), -1))


def _height_profile(fields, lat_name, idx, fallback_level_count):
    if "HHL" not in fields or fields["HHL"] is None:
        return np.arange(fallback_level_count, dtype=np.float32)
    hhl = fields["HHL"]
    spatial_dim = hhl[lat_name].dims[0]
    hhl_profile = hhl.squeeze().isel({spatial_dim: idx}).compute()
    h_vals = np.asarray(hhl_profile.values, dtype=np.float32).ravel()
    return ((h_vals[:-1] + h_vals[1:]) / 2.0).astype(np.float32)


def append_profile_chunk(
    buffers,
    fields,
    locations,
    tag,
    h,
    ref,
    loc_rad_map=None,
    location_indices=None,
    height_cache=None,
):
    sample = _sample_field(fields)
    if sample is None:
        return False
    lat_name, _lon_name = _field_lat_lon_names(sample)
    indices = location_indices if location_indices is not None else _location_indices(sample, locations)
    valid_time = ref + datetime.timedelta(hours=h)
    location_names = list(indices.keys())
    profiles_by_var = {}
    for var in VARS_TRACES:
        if var not in fields:
            profiles_by_var[var] = None
            continue
        names, profiles = _point_profiles(fields[var], lat_name, indices)
        if names != location_names:
            raise RuntimeError(f"CH1 direct profile location order changed for {var} H+{h:02d}")
        profiles_by_var[var] = profiles

    for loc_pos, (name, idx) in enumerate(indices.items()):
        raw_profiles = {
            var: None if profiles_by_var[var] is None else profiles_by_var[var][loc_pos]
            for var in VARS_TRACES
        }

        level_source = next((arr for arr in raw_profiles.values() if arr is not None), None)
        if level_source is None:
            continue
        level_count = int(level_source.shape[0])
        if height_cache is not None and name in height_cache:
            height = height_cache[name]
        else:
            height = _height_profile(fields, lat_name, idx, level_count)
            if height_cache is not None:
                height_cache[name] = height
        if height.shape[0] != level_count:
            log(
                f"CH1 direct profile height length mismatch for {name} H+{h:02d}: "
                f"{height.shape[0]} != {level_count}",
                "WARNING",
            )
            height = height[:level_count]

        buffer = buffers.setdefault(name, {"height": height, "steps": [], "values": []})
        if len(buffer["height"]) != len(height) or not np.allclose(buffer["height"], height, equal_nan=True):
            log(f"CH1 direct profile height changed within chunk for {name} H+{h:02d}", "WARNING")

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
            model_key="icon-ch1",
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
    log(f"CH1 direct profile chunk {chunk_id} wrote {written} location artifact(s)", "NOTICE")
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
        f"{seed_h:02d}",
        "temp_rad_seed",
    )
    for var, tmp in downloaded.items():
        try:
            ds_r = xr.open_dataset(tmp, engine="cfgrib", backend_kwargs={"indexpath": ""})
            previous[var] = ds_r[next(iter(ds_r.data_vars))].load().values.ravel()
            ds_r.close()
        except Exception as e:
            log(f"CH1 radiation seed decode failed for {var} H+{seed_h:02d}: {e}", "WARNING")
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
        f"{seed_h:02d}",
        "temp_rain_seed",
    )
    for var, tmp in downloaded.items():
        try:
            ds_r = xr.open_dataset(tmp, engine="cfgrib", backend_kwargs={"indexpath": ""})
            previous[var] = ds_r[next(iter(ds_r.data_vars))].load().values.ravel()
            ds_r.close()
        except Exception as e:
            log(f"CH1 rain seed decode failed for {var} H+{seed_h:02d}: {e}", "WARNING")
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return previous


def main():
    log("Main start...")
    force_refresh = env_flag("FORCE_REFRESH", default=False)
    profile_mode = env_choice("CH1_PROFILE_MODE", "direct-chunk", {"direct-chunk", "none"})
    horizon_start = env_int("CH1_HORIZON_START", default=0, minimum=0, maximum=45)
    horizon_end = env_int("CH1_HORIZON_END", default=45, minimum=0, maximum=45)
    if horizon_end < horizon_start:
        horizon_start, horizon_end = horizon_end, horizon_start
    chunk_id = os.getenv("CH1_PROFILE_CHUNK_ID") or f"H{horizon_start:03d}_H{horizon_end:03d}"
    pinned_run = env_run_tag("CH1_RUN_TAG") or env_run_tag("CH1_REFERENCE_TIME")
    require_full_horizon_run = env_flag("CH1_REQUIRE_FULL_HORIZON_RUN", profile_mode == "direct-chunk")
    horizon_fetch_batch = env_flag("XCBENZ_FETCH_HORIZON_BATCH", default=False)
    if force_refresh:
        log("FORCE_REFRESH enabled: existing CH1 run-complete checks will be ignored.", "NOTICE")
    log(
        f"CH1 profile mode: {profile_mode}; horizon range H{horizon_start:03d}-H{horizon_end:03d}; "
        f"chunk_id={chunk_id}",
        "NOTICE",
    )
    if pinned_run is not None:
        log(f"CH1 pinned run: {pinned_run.strftime('%Y%m%d_%H%M')}", "NOTICE")

    wind_config = None
    wind_map_out_root = os.getenv("CH1_WIND_MAP_OUT_ROOT", CACHE_DIR_WIND_MAPS)
    sunshine_map_out_root = os.getenv("CH1_SUNSHINE_MAP_OUT_ROOT", CACHE_DIR_SUNSHINE_MAPS)
    rain_map_out_root = os.getenv("CH1_RAIN_MAP_OUT_ROOT", CACHE_DIR_RAIN_MAPS)
    sunrain_map_out_root = os.getenv("CH1_SUNRAIN_MAP_OUT_ROOT", CACHE_DIR_SUNRAIN_MAPS)
    cloud_map_out_root = os.getenv("CH1_CLOUD_MAP_OUT_ROOT", CACHE_DIR_CLOUD_MAPS)
    wind_enabled = is_wind_maps_enabled("ch1")
    sunshine_enabled = is_sunshine_maps_enabled("ch1")
    rain_enabled = is_rain_maps_enabled("ch1")
    sunrain_enabled = is_sunrain_maps_enabled("ch1")
    cloud_enabled = is_cloud_maps_enabled("ch1")
    if wind_enabled or sunshine_enabled or rain_enabled or sunrain_enabled or cloud_enabled:
        try:
            wind_config = load_wind_map_config(log=log)
            if wind_enabled:
                log("CH1 wind-map generation enabled for this run.", "NOTICE")
            if sunshine_enabled:
                log("CH1 sunshine-map generation enabled for this run.", "NOTICE")
            if rain_enabled:
                log("CH1 rain-map generation enabled for this run.", "NOTICE")
            if sunrain_enabled:
                log("CH1 Sun+Rain map generation enabled for this run.", "NOTICE")
            if cloud_enabled:
                log("CH1 cloud-map generation enabled for this run.", "NOTICE")
        except Exception as e:
            wind_enabled = False
            sunshine_enabled = False
            rain_enabled = False
            sunrain_enabled = False
            cloud_enabled = False
            log(f"CH1 map generation disabled: {e}", "WARNING")
    else:
        log("CH1 wind/sunshine/rain/sunrain/cloud map generation disabled by flags.")

    download_static_files()
    if not os.path.exists("locations.json"): return
    with open("locations.json", "r", encoding="utf-8") as f: locations = json.load(f)

    runs = [pinned_run] if pinned_run is not None else get_latest_available_runs(limit=3)
    if not runs: log("No runs found."); return

    hhl = load_static_hhl()
    grid = load_static_grid()

    if hhl is not None and grid is not None:
        # Inject coords into HHL so it can serve as a sample for direct profile chunks
        n_grid = grid['lat'].size
        match_dim = next((d for d in hhl.dims if hhl.sizes[d] == n_grid), None)
        if match_dim:
            hhl = hhl.assign_coords({
                "latitude": (match_dim, grid['lat'].values),
                "longitude": (match_dim, grid['lon'].values)
            })

    for ref_time in runs:
        tag = ref_time.strftime('%Y%m%d_%H%M')
        max_h = 45 if ref_time.hour == 3 else 33
        sunshine_missing = sunshine_enabled and not is_sunshine_run_complete("ch1", tag, root=sunshine_map_out_root)
        rain_missing = rain_enabled and not is_rain_run_complete("ch1", tag, root=rain_map_out_root)
        sunrain_missing = sunrain_enabled and not is_sunrain_run_complete("ch1", tag, root=sunrain_map_out_root)
        cloud_missing = cloud_enabled and not is_cloud_run_complete("ch1", tag, root=cloud_map_out_root)

        run_horizon_end = min(horizon_end, max_h)
        if run_horizon_end < horizon_start:
            log(
                f"Run {tag} has max horizon H{max_h:02d}, outside requested "
                f"H{horizon_start:03d}-H{horizon_end:03d}; trying next run.",
                "WARNING",
            )
            continue
        if require_full_horizon_run and not has_profile_horizon(ref_time, run_horizon_end):
            log(f"CH1 run {tag} does not expose H{run_horizon_end:03d} yet; trying next available run.")
            continue

        log(f"Processing run: {tag} (H{horizon_start:03d}-H{run_horizon_end:03d})")
        any_success = False
        profile_buffers = {} if profile_mode == "direct-chunk" else None
        location_indices_cache = None
        height_cache = {} if profile_mode == "direct-chunk" else None
        release_profile_only_fields = env_flag("XCBENZ_RELEASE_PROFILE_ONLY_FIELDS", default=False)
        wind_accumulator = (
            WindMapAccumulator(
                "ch1",
                tag,
                ref_time,
                wind_config,
                log=log,
                out_root=wind_map_out_root,
            )
            if wind_enabled and wind_config is not None
            else None
        )
        sunshine_accumulator = (
            SunshineMapAccumulator("ch1", tag, ref_time, wind_config, log=log, out_root=sunshine_map_out_root)
            if sunshine_enabled and wind_config is not None
            else None
        )
        rain_accumulator = (
            RainMapAccumulator("ch1", tag, ref_time, wind_config, log=log, out_root=rain_map_out_root)
            if rain_enabled and wind_config is not None
            else None
        )
        sunrain_accumulator = (
            SunRainMapAccumulator("ch1", tag, ref_time, wind_config, log=log, out_root=sunrain_map_out_root)
            if sunrain_enabled and wind_config is not None
            else None
        )
        cloud_accumulator = (
            CloudMapAccumulator("ch1", tag, ref_time, wind_config, log=log, out_root=cloud_map_out_root)
            if cloud_enabled and wind_config is not None
            else None
        )
        # Cache previous raw radiation values for de-accumulation (running mean to hourly mean)
        # Formula: hourly_mean[n to n+1h] = (n+1)*raw[n+1h] - n*raw[nh]
        prev_rad_raw = seed_previous_radiation(
            "ogd-forecasting-icon-ch1",
            ref_time,
            tag,
            horizon_start,
        )
        prefetch_next_horizon = horizon_fetch_batch and env_flag("XCBENZ_PREFETCH_NEXT_HORIZON", default=False)
        prefetch_executor = ThreadPoolExecutor(max_workers=1) if prefetch_next_horizon else None
        prefetch_future = None
        prefetch_horizon = None

        def fetch_horizon_batch(h_value):
            profile_variables = VARS_TRACES if profile_mode == "direct-chunk" else []
            map_variables = ["U", "V", *VARS_NATIVE_10M_WIND] if (wind_enabled or sunshine_enabled) else []
            variables_to_fetch = list(dict.fromkeys([*profile_variables, *map_variables]))
            rain_needed = rain_accumulator is not None or sunrain_accumulator is not None
            cloud_needed = cloud_accumulator is not None
            radiation_needed = (
                profile_mode == "direct-chunk"
                or sunshine_accumulator is not None
                or sunrain_accumulator is not None
            )
            batch_variables = list(dict.fromkeys([
                *variables_to_fetch,
                *(VARS_RAIN_ACCUM if rain_needed else []),
                *(VARS_CLOUD_MAPS if cloud_needed else []),
                *(VARS_SUNSHINE_MAPS if h_value > 0 and radiation_needed else []),
            ]))
            return fetch_variable_files(
                "ogd-forecasting-icon-ch1",
                batch_variables,
                ref_time,
                get_iso_horizon(h_value),
                tag,
                f"{h_value:02d}",
                "temp_batch",
            )

        if prefetch_executor is not None:
            prefetch_horizon = horizon_start
            prefetch_future = prefetch_executor.submit(fetch_horizon_batch, prefetch_horizon)

        if rain_accumulator is not None or sunrain_accumulator is not None:
            rain_seed = seed_previous_rain("ogd-forecasting-icon-ch1", ref_time, tag, horizon_start)
            if rain_accumulator is not None:
                rain_accumulator.seed_previous_raw(rain_seed.get("TOT_PREC"))
            if sunrain_accumulator is not None:
                sunrain_accumulator.seed_previous_raw(rain_seed.get("TOT_PREC"))

        for h in range(horizon_start, run_horizon_end + 1):
            iso_h = get_iso_horizon(h)
            valid_time_str = (ref_time + datetime.timedelta(hours=h)).strftime('%Y-%m-%dT%H:%M:%SZ')
            # Only log detailed info if we actually have chance of finding data

            fields = {"HHL": hhl} if hhl is not None else {}
            has_new_data = False
            profile_variables = VARS_TRACES if profile_mode == "direct-chunk" else []
            map_variables = ["U", "V", *VARS_NATIVE_10M_WIND] if (wind_enabled or sunshine_enabled) else []
            variables_to_fetch = list(dict.fromkeys([*profile_variables, *map_variables]))
            rain_needed = rain_accumulator is not None or sunrain_accumulator is not None
            cloud_needed = cloud_accumulator is not None
            radiation_needed = (
                profile_mode == "direct-chunk"
                or sunshine_accumulator is not None
                or sunrain_accumulator is not None
            )
            downloaded_all = {}
            if horizon_fetch_batch:
                if prefetch_future is not None and prefetch_horizon == h:
                    downloaded_all = prefetch_future.result()
                    if h < run_horizon_end:
                        prefetch_horizon = h + 1
                        prefetch_future = prefetch_executor.submit(fetch_horizon_batch, prefetch_horizon)
                    else:
                        prefetch_horizon = None
                        prefetch_future = None
                else:
                    downloaded_all = fetch_horizon_batch(h)
                downloaded_fields = {var: downloaded_all[var] for var in variables_to_fetch if var in downloaded_all}
            else:
                downloaded_fields = fetch_variable_files(
                    "ogd-forecasting-icon-ch1",
                    variables_to_fetch,
                    ref_time,
                    iso_h,
                    tag,
                    f"{h:02d}",
                    "temp",
                )
            for var, tmp in downloaded_fields.items():
                try:
                    ds = xr.open_dataset(tmp, engine='cfgrib', backend_kwargs={'indexpath': ''})
                    data = ds[next(iter(ds.data_vars))].load()
                    if grid:
                        m_dim = next(d for d in data.dims if data.sizes[d] == grid['lat'].size)
                        data = data.assign_coords({"latitude": (m_dim, grid['lat'].values), "longitude": (m_dim, grid['lon'].values)})
                    fields[var] = data
                    ds.close()
                    has_new_data = True
                except Exception as e:
                    log(f"Decode failed for {var} H+{h:02d}: {e}", "WARNING")
                finally:
                    if os.path.exists(tmp):
                        os.remove(tmp)

            sample_field = next((v for v in fields.values() if v is not None and hasattr(v, 'dims')), None)
            rain_scalars = {}
            rain_sample_field = None
            if rain_needed:
                if horizon_fetch_batch:
                    downloaded_rain = {var: downloaded_all[var] for var in VARS_RAIN_ACCUM if var in downloaded_all}
                else:
                    downloaded_rain = fetch_variable_files(
                        "ogd-forecasting-icon-ch1",
                        VARS_RAIN_ACCUM,
                        ref_time,
                        iso_h,
                        tag,
                        f"{h:02d}",
                        "temp_rain",
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
                        log(f"Rain decode failed for {var} H+{h:02d}: {e}", "WARNING")
                    finally:
                        if os.path.exists(tmp):
                            os.remove(tmp)

            cloud_scalars = {}
            cloud_sample_field = None
            if cloud_needed:
                if horizon_fetch_batch:
                    downloaded_cloud = {var: downloaded_all[var] for var in VARS_CLOUD_MAPS if var in downloaded_all}
                else:
                    downloaded_cloud = fetch_variable_files(
                        "ogd-forecasting-icon-ch1",
                        VARS_CLOUD_MAPS,
                        ref_time,
                        iso_h,
                        tag,
                        f"{h:02d}",
                        "temp_cloud",
                    )
                for var, tmp in downloaded_cloud.items():
                    try:
                        ds_cloud = xr.open_dataset(tmp, engine='cfgrib', backend_kwargs={'indexpath': ''})
                        cloud_data = ds_cloud[next(iter(ds_cloud.data_vars))].load()
                        if grid:
                            m_dim = next(d for d in cloud_data.dims if cloud_data.sizes[d] == grid['lat'].size)
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
                        log(f"Cloud decode failed for {var} H+{h:02d}: {e}", "WARNING")
                    finally:
                        if os.path.exists(tmp):
                            os.remove(tmp)

            # --- Radiation fetch and de-accumulation ---
            # ICON stores ASWDIR_S / ASWDIFD_S as running means from run start.
            # H00 is defined as 0 (model init).  For h >= 1:
            #   hourly_mean = h * raw[h] - (h-1) * raw[h-1]
            # We fetch raw[h] here and use the cached raw[h-1] from prev_rad_raw.
            rad_scalars = {}
            if h > 0 and radiation_needed:
                # Extract nearest-grid-point index from any existing field
                if sample_field is not None:
                    lat_n = 'latitude' if 'latitude' in sample_field.coords else 'lat'
                    lon_n = 'longitude' if 'longitude' in sample_field.coords else 'lon'
                    lats_r = sample_field[lat_n].values
                    lons_r = sample_field[lon_n].values
                    if horizon_fetch_batch:
                        downloaded_radiation = {var: downloaded_all[var] for var in VARS_SUNSHINE_MAPS if var in downloaded_all}
                    else:
                        downloaded_radiation = fetch_variable_files(
                            "ogd-forecasting-icon-ch1",
                            VARS_SUNSHINE_MAPS,
                            ref_time,
                            iso_h,
                            tag,
                            f"{h:02d}",
                            "temp_rad",
                        )
                    for var, tmp in downloaded_radiation.items():
                        try:
                            ds_r = xr.open_dataset(tmp, engine='cfgrib', backend_kwargs={'indexpath': ''})
                            raw_data = ds_r[next(iter(ds_r.data_vars))].load().values.ravel()
                            ds_r.close()
                            # De-accumulate: hourly mean = h*raw_current - (h-1)*raw_prev
                            # We need per-grid-point to later extract at location idx.
                            # Store as a 1D flat array keyed by var, compute per location in loop below.
                            prev_raw_arr = prev_rad_raw[var]
                            if prev_raw_arr is None:
                                prev_raw_arr = np.zeros_like(raw_data)
                            if var in VARS_RADIATION_AVERAGE:
                                # Convert running mean from reference time to hourly mean.
                                deacc_arr = h * raw_data - (h - 1) * prev_raw_arr
                            else:
                                # Convert running sum from reference time to interval seconds.
                                deacc_arr = raw_data - prev_raw_arr
                            deacc_arr = np.maximum(deacc_arr, 0.0)   # clip negatives (float16 artifacts)
                            prev_rad_raw[var] = raw_data   # store for next iteration
                            # Store as {var: flat_array} - indexed per location below
                            rad_scalars[var] = deacc_arr
                        except Exception as e:
                            log(f"Radiation decode failed for {var} H+{h:02d}: {e}", "WARNING")
                        finally:
                            if os.path.exists(tmp):
                                os.remove(tmp)

            if has_new_data:
                if sample_field is not None and location_indices_cache is None:
                    location_indices_cache = _location_indices(sample_field, locations)
                loc_rad_map = {}
                if rad_scalars and sample_field is not None:
                    indices_for_radiation = location_indices_cache or _location_indices(sample_field, locations)
                    for name, idx_loc in indices_for_radiation.items():
                        loc_rad_map[name] = {
                            var: float(arr.ravel()[idx_loc])
                            for var, arr in rad_scalars.items()
                            if var in VARS_RADIATION
                        }
                if profile_mode == "direct-chunk" and profile_buffers is not None:
                    append_profile_chunk(
                        profile_buffers,
                        fields,
                        locations,
                        tag,
                        h,
                        ref_time,
                        loc_rad_map,
                        location_indices=location_indices_cache,
                        height_cache=height_cache,
                    )
                    if release_profile_only_fields:
                        for profile_only_var in ("P", "T", "QV"):
                            fields.pop(profile_only_var, None)
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
                log(f"H+{h:02d} done")
                any_success = True
        if prefetch_executor is not None:
            prefetch_executor.shutdown(wait=True)

        if any_success:
            log(f"Run {tag} processing complete.", "NOTICE")
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
            break
        else:
            log(f"Run {tag} yield no data, trying next available run...")
    cleanup_old_wind_runs("ch1", anchor_hour=3, log=log, root=wind_map_out_root)
    cleanup_old_sunshine_runs("ch1", anchor_hour=3, log=log)
    cleanup_old_rain_runs("ch1", anchor_hour=3, log=log)
    cleanup_old_sunrain_runs("ch1", anchor_hour=3, log=log)
    cleanup_old_cloud_runs("ch1", anchor_hour=3, log=log)
    # Manifest is now written by generate_combined_manifest.py (CI step after CH2 fetch)
    log("--- Data Fetcher Complete ---", "NOTICE")


if __name__ == "__main__":
    main()
