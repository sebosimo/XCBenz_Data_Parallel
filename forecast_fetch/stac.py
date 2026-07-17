"""Shared STAC discovery and forecast-asset download mechanics."""

from __future__ import annotations

import datetime
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

from .planning import ModelFetchPolicy


def iso_horizon(total_hours: int) -> str:
    days = total_hours // 24
    hours = total_hours % 24
    return f"P{days}DT{hours}H"


def download_file(
    url: str,
    target_path: str,
    *,
    policy: ModelFetchPolicy,
    log: Callable[..., None],
    max_retries: int | None = None,
    deadline_seconds: int | None = None,
) -> bool:
    """Download with the model's explicit retry, deadline, and partial-file policy."""

    retries = max_retries if max_retries is not None else policy.download_retry_limit
    for attempt in range(retries):
        try:
            log(f"Downloading {url} to {target_path}...")
            configured_deadline = (
                deadline_seconds
                if deadline_seconds is not None
                else policy.download_deadline_seconds
            )
            deadline = (
                time.time() + configured_deadline
                if configured_deadline is not None
                else None
            )
            with requests.get(
                url,
                stream=True,
                timeout=policy.request_timeout_seconds,
            ) as response:
                response.raise_for_status()
                with open(target_path, "wb") as target:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if deadline is not None and time.time() > deadline:
                            raise TimeoutError(
                                f"Download exceeded {configured_deadline}s - aborting"
                            )
                        target.write(chunk)
            log(f"Download complete: {target_path}")
            return True
        except Exception as exc:
            log(f"Download attempt {attempt + 1} failed: {exc}", "ERROR")
            if policy.remove_partial_downloads and os.path.exists(target_path):
                os.remove(target_path)
            if attempt < retries - 1:
                time.sleep(2**attempt)
    return False


def fetch_variable_file(
    *,
    api: Any,
    policy: ModelFetchPolicy,
    collection: str,
    variable: str,
    reference_datetime: datetime.datetime,
    horizon: str,
    target_path: str,
    log: Callable[..., None],
    downloader: Callable[[str, str], bool],
) -> tuple[str, str, bool]:
    try:
        request = api.Request(
            collection=collection,
            variable=variable,
            reference_datetime=reference_datetime,
            horizon=horizon,
            perturbed=False,
        )
        urls = api.get_asset_urls(request)
        if not urls:
            return variable, target_path, False
        return variable, target_path, downloader(urls[0], target_path)
    except Exception as exc:
        log(f"Fetch setup failed for {variable} {horizon}: {exc}", "WARNING")
        return variable, target_path, False


def fetch_variable_files(
    *,
    variables: Iterable[str],
    tag: str,
    horizon_label: str,
    prefix: str,
    workers: int,
    temporary_root: str | Path | None,
    fetch_one: Callable[[str, str], tuple[str, str, bool]],
) -> dict[str, str]:
    temp_dir = str(temporary_root) if temporary_root else None
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
        results = [fetch_one(variable, target_path) for variable, target_path in jobs]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as executor:
            futures = [
                executor.submit(fetch_one, variable, target_path)
                for variable, target_path in jobs
            ]
            for future in as_completed(futures):
                results.append(future.result())
    return {
        variable: target_path
        for variable, target_path, succeeded in results
        if succeeded and os.path.exists(target_path)
    }


def has_profile_horizon(
    reference_datetime: datetime.datetime,
    horizon: int,
    *,
    api: Any,
    policy: ModelFetchPolicy,
    log: Callable[..., None],
) -> bool:
    try:
        request = api.Request(
            collection=policy.collection,
            variable=policy.profile_variables[0],
            reference_datetime=reference_datetime,
            horizon=iso_horizon(horizon),
            perturbed=False,
        )
        return bool(api.get_asset_urls(request))
    except Exception as exc:
        label = reference_datetime.strftime("%Y%m%d_%H%M")
        log(
            f"{policy.model.upper()} profile horizon probe failed for {label} "
            f"H+{horizon:03d}: {exc}",
            "WARNING",
        )
        return False


def discover_runs(
    *,
    policy: ModelFetchPolicy,
    limit: int,
    log: Callable[..., None],
) -> list[datetime.datetime]:
    model_suffix = "" if policy.model == "ch1" else f" {policy.model.upper()}"
    log(f"Discovering{model_suffix} runs via Active Probing...")
    now = datetime.datetime.now(datetime.timezone.utc)
    hour = (now.hour // policy.run_interval_hours) * policy.run_interval_hours
    start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    found_runs = []
    for slot in range(policy.discovery_slots):
        candidate = start - datetime.timedelta(hours=slot * policy.run_interval_hours)
        reference = candidate.strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"limit": 1, "forecast:reference_datetime": reference}
        try:
            response = requests.get(
                f"{policy.stac_base_url}/items",
                params=params,
                timeout=policy.discovery_request_timeout_seconds,
            )
            if response.status_code == 200 and response.json().get("features"):
                found_runs.append(candidate)
                found_label = "" if policy.model == "ch1" else f" {policy.model.upper()}"
                log(f"Found available{found_label} run: {reference}")
                if len(found_runs) >= limit:
                    break
        except Exception:
            # Discovery is intentionally best-effort across candidate slots.
            pass
    return found_runs
