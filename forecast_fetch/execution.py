"""Shared execution engine for the ICON CH1 and CH2 fetcher wrappers."""

from __future__ import annotations

import datetime
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import xarray as xr

from cloud_maps import CloudMapAccumulator, cleanup_old_cloud_runs
from rain_maps import RainMapAccumulator, cleanup_old_rain_runs
from sunshine_maps import SunshineMapAccumulator, cleanup_old_sunshine_runs
from sunrain_maps import SunRainMapAccumulator, cleanup_old_sunrain_runs
from wind_maps import WindMapAccumulator, cleanup_old_wind_runs

from .config import FetchStartupConfig
from .planning import ModelFetchPolicy, ProductSelection


LogFunction = Callable[[str, str], None]


@dataclass(frozen=True)
class FetchRuntime:
    """Model-specific I/O seams used by the shared execution algorithm."""

    policy: ModelFetchPolicy
    output_directories: tuple[str, ...]
    log: LogFunction
    load_wind_map_config: Callable[..., Any]
    download_static_files: Callable[[], None]
    get_latest_available_runs: Callable[..., list[datetime.datetime]]
    has_profile_horizon: Callable[[datetime.datetime, int], bool]
    load_static_hhl: Callable[[], Any]
    load_static_grid: Callable[[], Any]
    fetch_variable_files: Callable[..., dict[str, str]]
    seed_previous_radiation: Callable[..., dict[str, Any]]
    seed_previous_rain: Callable[..., dict[str, Any]]
    location_indices: Callable[..., dict[str, int]]
    append_profile_chunk: Callable[..., None]
    finalize_profile_chunk: Callable[..., None]


def _iso_horizon(total_hours: int) -> str:
    days = total_hours // 24
    hours = total_hours % 24
    return f"P{days}DT{hours}H"


def _assign_grid_coordinates(data: Any, grid: Any) -> Any:
    if not grid:
        return data
    match_dim = next(dimension for dimension in data.dims if data.sizes[dimension] == grid["lat"].size)
    return data.assign_coords(
        {
            "latitude": (match_dim, grid["lat"].values),
            "longitude": (match_dim, grid["lon"].values),
        }
    )


def _decode_fields(
    downloaded: dict[str, str],
    *,
    grid: Any,
    horizon: int,
    policy: ModelFetchPolicy,
    log: LogFunction,
    owner: str,
) -> tuple[dict[str, Any], bool]:
    decoded: dict[str, Any] = {}
    any_success = False
    for variable, temporary_path in downloaded.items():
        try:
            dataset = xr.open_dataset(
                temporary_path,
                engine="cfgrib",
                backend_kwargs={"indexpath": ""},
            )
            data = dataset[next(iter(dataset.data_vars))].load()
            data = _assign_grid_coordinates(data, grid)
            decoded[variable] = data
            dataset.close()
            any_success = True
        except Exception as exc:
            owner_label = "" if owner == "primary" else f"{owner.title()} "
            model_label = "CH2 " if policy.model == "ch2" else ""
            log(
                f"{model_label}{owner_label}decode failed for {variable} "
                f"H+{horizon:0{policy.step_digits}d}: {exc}",
                "WARNING",
            )
        finally:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
    return decoded, any_success


def _fetch_group(
    runtime: FetchRuntime,
    *,
    variables: tuple[str, ...],
    reference_time: datetime.datetime,
    horizon: int,
    tag: str,
    owner: str,
) -> dict[str, str]:
    return runtime.fetch_variable_files(
        runtime.policy.collection,
        variables,
        reference_time,
        _iso_horizon(horizon),
        tag,
        f"{horizon:0{runtime.policy.step_digits}d}",
        runtime.policy.temporary_prefix(owner),
    )


def _start_message(model: str) -> str:
    return "Main start..." if model == "ch1" else "=== CH2 Data Fetcher Start ==="


def _complete_message(model: str) -> str:
    return "--- Data Fetcher Complete ---" if model == "ch1" else "=== CH2 Data Fetcher Complete ==="


def execute_fetch(runtime: FetchRuntime, startup: FetchStartupConfig) -> None:
    """Execute one fetcher with model behavior supplied by immutable policy and I/O seams."""

    policy = runtime.policy
    model = policy.model
    model_label = model.upper()
    log = runtime.log

    if startup.model != model:
        raise ValueError(f"startup model {startup.model!r} does not match runtime model {model!r}")

    for directory in runtime.output_directories:
        os.makedirs(directory, exist_ok=True)

    log(_start_message(model))
    if startup.force_refresh:
        log(
            f"FORCE_REFRESH was selected by orchestration; regenerating the requested {model_label} range.",
            "NOTICE",
        )
    log(
        f"{model_label} profile mode: {startup.profile_mode}; "
        f"horizon range H{startup.horizon_start:03d}-H{startup.horizon_end:03d}; "
        f"chunk_id={startup.profile_chunk_id}",
        "NOTICE",
    )
    if startup.pinned_run is not None:
        log(f"{model_label} pinned run: {startup.pinned_run.strftime('%Y%m%d_%H%M')}", "NOTICE")

    roots = startup.output_roots
    products = startup.products
    wind_config = None
    enabled = {
        "wind": products.wind,
        "sunshine": products.sunshine,
        "rain": products.rain,
        "sunrain": products.sunrain,
        "cloud": products.cloud,
    }
    if any(enabled.values()):
        try:
            wind_config = runtime.load_wind_map_config(log=log)
            for product, is_enabled in enabled.items():
                if is_enabled:
                    display = "Sun+Rain" if product == "sunrain" else product
                    log(f"{model_label} {display}-map generation enabled for this run.", "NOTICE")
        except Exception as exc:
            enabled = {product: False for product in enabled}
            log(f"{model_label} map generation disabled: {exc}", "WARNING")
    else:
        log(f"{model_label} wind/sunshine/rain/sunrain/cloud map generation disabled by flags.")

    runtime_products = ProductSelection(**enabled)
    runtime.download_static_files()
    if not os.path.exists("locations.json"):
        if model == "ch2":
            log("locations.json not found.", "ERROR")
        return
    with open("locations.json", "r", encoding="utf-8") as locations_file:
        locations = json.load(locations_file)

    runs = (
        [startup.pinned_run]
        if startup.pinned_run is not None
        else runtime.get_latest_available_runs(limit=policy.processing_candidate_limit)
    )
    if not runs:
        log("No runs found." if model == "ch1" else "No CH2 runs found.")
        return

    hhl = runtime.load_static_hhl()
    grid = runtime.load_static_grid()
    if hhl is not None and grid is not None:
        grid_size = grid["lat"].size
        match_dim = next((dimension for dimension in hhl.dims if hhl.sizes[dimension] == grid_size), None)
        if match_dim:
            hhl = hhl.assign_coords(
                {
                    "latitude": (match_dim, grid["lat"].values),
                    "longitude": (match_dim, grid["lon"].values),
                }
            )

    for reference_time in runs:
        tag = reference_time.strftime("%Y%m%d_%H%M")
        maximum_horizon = policy.maximum_horizon(reference_time)
        run_horizon_end = min(startup.horizon_end, maximum_horizon)
        if run_horizon_end < startup.horizon_start:
            log(
                f"Run {tag} has max horizon H{maximum_horizon:02d}, outside requested "
                f"H{startup.horizon_start:03d}-H{startup.horizon_end:03d}; trying next run.",
                "WARNING",
            )
            continue
        probe_horizon = policy.required_probe_horizon(run_horizon_end)
        if startup.require_full_horizon_run and not runtime.has_profile_horizon(reference_time, probe_horizon):
            log(
                f"{model_label} run {tag} does not expose H{probe_horizon:03d} yet; "
                "trying next available run."
            )
            continue

        prefix = "" if model == "ch1" else "CH2 "
        log(
            f"Processing {prefix}run: {tag} "
            f"(H{startup.horizon_start:03d}-H{run_horizon_end:03d})"
        )
        any_success = False
        profile_buffers = {} if startup.profile_mode == "direct-chunk" else None
        location_indices_cache = None
        height_cache = {} if startup.profile_mode == "direct-chunk" else None

        wind_accumulator = (
            WindMapAccumulator(model, tag, reference_time, wind_config, log=log, out_root=roots.wind)
            if enabled["wind"] and wind_config is not None
            else None
        )
        sunshine_accumulator = (
            SunshineMapAccumulator(
                model,
                tag,
                reference_time,
                wind_config,
                log=log,
                out_root=roots.sunshine,
            )
            if enabled["sunshine"] and wind_config is not None
            else None
        )
        rain_accumulator = (
            RainMapAccumulator(model, tag, reference_time, wind_config, log=log, out_root=roots.rain)
            if enabled["rain"] and wind_config is not None
            else None
        )
        sunrain_accumulator = (
            SunRainMapAccumulator(
                model,
                tag,
                reference_time,
                wind_config,
                log=log,
                out_root=roots.sunrain,
            )
            if enabled["sunrain"] and wind_config is not None
            else None
        )
        cloud_accumulator = (
            CloudMapAccumulator(model, tag, reference_time, wind_config, log=log, out_root=roots.cloud)
            if enabled["cloud"] and wind_config is not None
            else None
        )

        previous_radiation = runtime.seed_previous_radiation(
            policy.collection,
            reference_time,
            tag,
            startup.horizon_start,
        )
        prefetch_enabled = startup.horizon_fetch_batch and startup.prefetch_next_horizon
        prefetch_executor = ThreadPoolExecutor(max_workers=1) if prefetch_enabled else None
        prefetch_future = None
        prefetch_horizon = None

        def fetch_horizon_batch(horizon: int) -> dict[str, str]:
            plan = policy.horizon_plan(
                horizon,
                profile_mode=startup.profile_mode,
                products=runtime_products,
            )
            return _fetch_group(
                runtime,
                variables=plan.batch,
                reference_time=reference_time,
                horizon=horizon,
                tag=tag,
                owner="batch",
            )

        if prefetch_executor is not None:
            prefetch_horizon = startup.horizon_start
            prefetch_future = prefetch_executor.submit(fetch_horizon_batch, prefetch_horizon)

        if rain_accumulator is not None or sunrain_accumulator is not None:
            rain_seed = runtime.seed_previous_rain(
                policy.collection,
                reference_time,
                tag,
                startup.horizon_start,
            )
            if rain_accumulator is not None:
                rain_accumulator.seed_previous_raw(rain_seed.get("TOT_PREC"))
            if sunrain_accumulator is not None:
                sunrain_accumulator.seed_previous_raw(rain_seed.get("TOT_PREC"))

        for horizon in range(startup.horizon_start, run_horizon_end + 1):
            fields = {"HHL": hhl} if hhl is not None else {}
            has_new_data = False
            plan = policy.horizon_plan(
                horizon,
                profile_mode=startup.profile_mode,
                products=runtime_products,
            )
            downloaded_all: dict[str, str] = {}
            if startup.horizon_fetch_batch:
                if prefetch_future is not None and prefetch_horizon == horizon:
                    downloaded_all = prefetch_future.result()
                    if horizon < run_horizon_end:
                        prefetch_horizon = horizon + 1
                        prefetch_future = prefetch_executor.submit(fetch_horizon_batch, prefetch_horizon)
                    else:
                        prefetch_horizon = None
                        prefetch_future = None
                else:
                    downloaded_all = fetch_horizon_batch(horizon)
                downloaded_fields = {
                    variable: downloaded_all[variable]
                    for variable in plan.primary
                    if variable in downloaded_all
                }
            else:
                downloaded_fields = _fetch_group(
                    runtime,
                    variables=plan.primary,
                    reference_time=reference_time,
                    horizon=horizon,
                    tag=tag,
                    owner="primary",
                )

            decoded_fields, decoded_success = _decode_fields(
                downloaded_fields,
                grid=grid,
                horizon=horizon,
                policy=policy,
                log=log,
                owner="primary",
            )
            fields.update(decoded_fields)
            has_new_data = has_new_data or decoded_success
            sample_field = next(
                (value for value in fields.values() if value is not None and hasattr(value, "dims")),
                None,
            )

            rain_scalars: dict[str, Any] = {}
            rain_fields: dict[str, Any] = {}
            downloaded_rain: dict[str, str] = {}
            rain_sample_field = None
            if plan.rain:
                downloaded_rain = (
                    {
                        variable: downloaded_all[variable]
                        for variable in plan.rain
                        if variable in downloaded_all
                    }
                    if startup.horizon_fetch_batch
                    else _fetch_group(
                        runtime,
                        variables=plan.rain,
                        reference_time=reference_time,
                        horizon=horizon,
                        tag=tag,
                        owner="rain",
                    )
                )
                rain_fields, rain_success = _decode_fields(
                    downloaded_rain,
                    grid=grid,
                    horizon=horizon,
                    policy=policy,
                    log=log,
                    owner="rain",
                )
                has_new_data = has_new_data or rain_success
                for variable, rain_data in rain_fields.items():
                    rain_scalars[variable] = rain_data.values.ravel()
                    rain_sample_field = rain_data
                if sample_field is None:
                    sample_field = rain_sample_field

            cloud_scalars: dict[str, Any] = {}
            cloud_fields: dict[str, Any] = {}
            downloaded_cloud: dict[str, str] = {}
            cloud_sample_field = None
            if plan.cloud:
                downloaded_cloud = (
                    {
                        variable: downloaded_all[variable]
                        for variable in plan.cloud
                        if variable in downloaded_all
                    }
                    if startup.horizon_fetch_batch
                    else _fetch_group(
                        runtime,
                        variables=plan.cloud,
                        reference_time=reference_time,
                        horizon=horizon,
                        tag=tag,
                        owner="cloud",
                    )
                )
                cloud_fields, cloud_success = _decode_fields(
                    downloaded_cloud,
                    grid=grid,
                    horizon=horizon,
                    policy=policy,
                    log=log,
                    owner="cloud",
                )
                has_new_data = has_new_data or cloud_success
                for variable, cloud_data in cloud_fields.items():
                    cloud_scalars[variable] = cloud_data.values.ravel()
                    cloud_sample_field = cloud_data
                if sample_field is None:
                    sample_field = cloud_sample_field

            radiation_scalars: dict[str, Any] = {}
            downloaded_radiation: dict[str, str] = {}
            raw_data = None
            previous_raw = None
            deaccumulated = None
            if horizon > 0 and plan.radiation and sample_field is not None:
                downloaded_radiation = (
                    {
                        variable: downloaded_all[variable]
                        for variable in plan.radiation
                        if variable in downloaded_all
                    }
                    if startup.horizon_fetch_batch
                    else _fetch_group(
                        runtime,
                        variables=plan.radiation,
                        reference_time=reference_time,
                        horizon=horizon,
                        tag=tag,
                        owner="rad",
                    )
                )
                for variable, temporary_path in downloaded_radiation.items():
                    try:
                        dataset = xr.open_dataset(
                            temporary_path,
                            engine="cfgrib",
                            backend_kwargs={"indexpath": ""},
                        )
                        raw_data = dataset[next(iter(dataset.data_vars))].load().values.ravel()
                        dataset.close()
                        previous_raw = previous_radiation[variable]
                        if previous_raw is None:
                            previous_raw = np.zeros_like(raw_data)
                        if variable in policy.radiation_average_variables:
                            deaccumulated = horizon * raw_data - (horizon - 1) * previous_raw
                        else:
                            deaccumulated = raw_data - previous_raw
                        radiation_scalars[variable] = np.maximum(deaccumulated, 0.0)
                        previous_radiation[variable] = raw_data
                    except Exception as exc:
                        model_prefix = "CH2 " if model == "ch2" else ""
                        log(
                            f"{model_prefix}Radiation decode failed for {variable} "
                            f"H+{horizon:0{policy.step_digits}d}: {exc}",
                            "WARNING",
                        )
                    finally:
                        if os.path.exists(temporary_path):
                            os.remove(temporary_path)

            if has_new_data:
                if sample_field is not None and location_indices_cache is None:
                    location_indices_cache = runtime.location_indices(sample_field, locations)
                location_radiation: dict[str, dict[str, float]] = {}
                if radiation_scalars and sample_field is not None:
                    indices = location_indices_cache or runtime.location_indices(sample_field, locations)
                    for name, location_index in indices.items():
                        location_radiation[name] = {
                            variable: float(values.ravel()[location_index])
                            for variable, values in radiation_scalars.items()
                            if variable in policy.radiation_average_variables
                        }
                if startup.profile_mode == "direct-chunk" and profile_buffers is not None:
                    runtime.append_profile_chunk(
                        profile_buffers,
                        fields,
                        locations,
                        tag,
                        horizon,
                        reference_time,
                        location_radiation,
                        location_indices=location_indices_cache,
                        height_cache=height_cache,
                    )
                    if startup.release_profile_only_fields:
                        for variable in ("P", "T", "QV"):
                            fields.pop(variable, None)
                if sunshine_accumulator is not None and radiation_scalars and sample_field is not None:
                    sunshine_accumulator.append(
                        sample_field,
                        radiation_scalars,
                        horizon,
                        reference_time,
                    )
                if rain_accumulator is not None and rain_scalars and rain_sample_field is not None:
                    rain_accumulator.append(rain_sample_field, rain_scalars, horizon, reference_time)
                if (
                    sunrain_accumulator is not None
                    and radiation_scalars
                    and rain_scalars
                    and sample_field is not None
                ):
                    sunrain_accumulator.append(
                        sample_field,
                        radiation_scalars,
                        rain_scalars,
                        horizon,
                        reference_time,
                    )
                if cloud_accumulator is not None and cloud_scalars and cloud_sample_field is not None:
                    cloud_accumulator.append(
                        cloud_sample_field,
                        cloud_scalars,
                        horizon,
                        reference_time,
                    )
                if wind_accumulator is not None:
                    wind_accumulator.append(fields, horizon, reference_time)
                horizon_prefix = "CH2 " if model == "ch2" else ""
                log(f"{horizon_prefix}H+{horizon:0{policy.step_digits}d} done")
                any_success = True

            # Keep only the accumulator-owned/profile-buffer copies between
            # horizons. Without explicit release, loop locals retain the prior
            # horizon's complete decoded fields while the next batch downloads.
            fields.clear()
            decoded_fields.clear()
            rain_fields.clear()
            cloud_fields.clear()
            rain_scalars.clear()
            cloud_scalars.clear()
            radiation_scalars.clear()
            downloaded_fields.clear()
            downloaded_rain.clear()
            downloaded_cloud.clear()
            downloaded_radiation.clear()
            downloaded_all.clear()
            sample_field = None
            rain_sample_field = None
            cloud_sample_field = None
            raw_data = None
            previous_raw = None
            deaccumulated = None

        if prefetch_executor is not None:
            prefetch_executor.shutdown(wait=True)

        if any_success:
            completion_prefix = "CH2 run" if model == "ch2" else "Run"
            log(f"{completion_prefix} {tag} processing complete.", "NOTICE")
            for accumulator in (
                wind_accumulator,
                sunshine_accumulator,
                rain_accumulator,
                sunrain_accumulator,
                cloud_accumulator,
            ):
                if accumulator is not None:
                    accumulator.finalize()
            if startup.profile_mode == "direct-chunk" and profile_buffers is not None:
                runtime.finalize_profile_chunk(
                    profile_buffers,
                    locations,
                    tag,
                    startup.profile_chunk_id,
                    reference_time,
                )
            break
        yielded = "yielded" if model == "ch2" else "yield"
        log(f"{model_label + ' ' if model == 'ch2' else ''}run {tag} {yielded} no data, trying next available run...")

    cleanup_old_wind_runs(model, anchor_hour=policy.cleanup_anchor_hour, log=log, root=roots.wind)
    cleanup_old_sunshine_runs(model, anchor_hour=policy.cleanup_anchor_hour, log=log)
    cleanup_old_rain_runs(model, anchor_hour=policy.cleanup_anchor_hour, log=log)
    cleanup_old_sunrain_runs(model, anchor_hour=policy.cleanup_anchor_hour, log=log)
    cleanup_old_cloud_runs(model, anchor_hour=policy.cleanup_anchor_hour, log=log)
    log(_complete_message(model), "NOTICE")
