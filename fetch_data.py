import os, sys
import datetime, json, xarray as xr
import numpy as np
import warnings
import requests

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
)
from rain_maps import (
    CACHE_DIR_RAIN_MAPS,
    RainMapAccumulator,
    cleanup_old_rain_runs,
)
from sunshine_maps import (
    CACHE_DIR_SUNSHINE_MAPS,
    SunshineMapAccumulator,
    cleanup_old_sunshine_runs,
)
from sunrain_maps import (
    CACHE_DIR_SUNRAIN_MAPS,
    SunRainMapAccumulator,
    cleanup_old_sunrain_runs,
)
from wind_maps import (
    CACHE_DIR_WIND_MAPS,
    WindMapAccumulator,
    cleanup_old_wind_runs,
    load_config as load_wind_map_config,
)
from forecast_fetch.config import OutputRoots, parse_startup_config
from forecast_fetch.execution import FetchRuntime, execute_fetch
from forecast_fetch.planning import CH1_POLICY, ProductSelection
from forecast_fetch.profiles import (
    append_profile_chunk as append_shared_profile_chunk,
    finalize_profile_chunk as finalize_shared_profile_chunk,
    location_indices as _location_indices,
)
from forecast_fetch.static_grid import load_horizontal_grid
from forecast_fetch.stac import (
    discover_runs as discover_stac_runs,
    download_file as download_stac_file,
    fetch_variable_file as fetch_stac_variable_file,
    fetch_variable_files as fetch_stac_variable_files,
    has_profile_horizon as probe_stac_profile_horizon,
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
HHL_FILENAME, HGRID_FILENAME = CH1_POLICY.static_assets
STAC_BASE_URL = CH1_POLICY.stac_base_url
STAC_ASSETS_URL = CH1_POLICY.stac_assets_url

WIND_LEVELS = []

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
    return download_stac_file(
        url,
        target_path,
        policy=CH1_POLICY,
        log=log,
        max_retries=max_retries,
    )
def fetch_variable_file(collection, variable, reference_datetime, horizon, target_path):
    return fetch_stac_variable_file(
        api=ogd_api,
        policy=CH1_POLICY,
        collection=collection,
        variable=variable,
        reference_datetime=reference_datetime,
        horizon=horizon,
        target_path=target_path,
        log=log,
        downloader=download_file,
    )
def has_profile_horizon(reference_datetime, horizon):
    return probe_stac_profile_horizon(
        reference_datetime,
        horizon,
        api=ogd_api,
        policy=CH1_POLICY,
        log=log,
    )
def fetch_variable_files(collection, variables, reference_datetime, horizon, tag, horizon_label, prefix):
    return fetch_stac_variable_files(
        variables=variables,
        tag=tag,
        horizon_label=horizon_label,
        prefix=prefix,
        workers=env_int("DOWNLOAD_WORKERS", default=4, minimum=1, maximum=8),
        temporary_root=os.getenv("XCBENZ_FETCH_TMP_DIR"),
        fetch_one=lambda variable, target_path: fetch_variable_file(
            collection,
            variable,
            reference_datetime,
            horizon,
            target_path,
        ),
    )
def get_latest_available_runs(limit=1):
    return discover_stac_runs(policy=CH1_POLICY, limit=limit, log=log)
def download_static_files():
    os.makedirs(STATIC_DIR, exist_ok=True)
    for filename in [HHL_FILENAME, HGRID_FILENAME]:
        path = os.path.join(STATIC_DIR, filename)
        if not os.path.exists(path):
            log(f"Downloading static file {filename}...")
            try:
                resp = requests.get(STAC_ASSETS_URL, timeout=CH1_POLICY.static_request_timeout_seconds)
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
        return load_horizontal_grid(path)
    except Exception as e: log(f"Error loading HGRID: {e}", "ERROR"); return None

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
    return append_shared_profile_chunk(
        buffers,
        fields,
        locations,
        tag,
        h,
        ref,
        loc_rad_map,
        policy=CH1_POLICY,
        log=log,
        cached_location_indices=location_indices,
        height_cache=height_cache,
    )


def finalize_profile_chunk(buffers, locations, tag, chunk_id, ref):
    return finalize_shared_profile_chunk(
        buffers,
        locations,
        tag,
        chunk_id,
        ref,
        policy=CH1_POLICY,
        log=log,
        output_root=PROFILE_CHUNK_DIR,
    )

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
    startup = parse_startup_config(
        "ch1",
        os.environ,
        default_output_roots=OutputRoots(
            wind=CACHE_DIR_WIND_MAPS,
            sunshine=CACHE_DIR_SUNSHINE_MAPS,
            rain=CACHE_DIR_RAIN_MAPS,
            sunrain=CACHE_DIR_SUNRAIN_MAPS,
            cloud=CACHE_DIR_CLOUD_MAPS,
        ),
    )
    runtime = FetchRuntime(
        policy=CH1_POLICY,
        output_directories=(
            CACHE_DIR_MAPS_PACKED,
            CACHE_DIR_SUNSHINE_MAPS,
            CACHE_DIR_RAIN_MAPS,
            CACHE_DIR_CLOUD_MAPS,
            PROFILE_CHUNK_DIR,
        ),
        log=log,
        load_wind_map_config=load_wind_map_config,
        download_static_files=download_static_files,
        get_latest_available_runs=get_latest_available_runs,
        has_profile_horizon=has_profile_horizon,
        load_static_hhl=load_static_hhl,
        load_static_grid=load_static_grid,
        fetch_variable_files=fetch_variable_files,
        seed_previous_radiation=seed_previous_radiation,
        seed_previous_rain=seed_previous_rain,
        location_indices=_location_indices,
        append_profile_chunk=append_profile_chunk,
        finalize_profile_chunk=finalize_profile_chunk,
    )
    execute_fetch(runtime, startup)

if __name__ == "__main__":
    main()
