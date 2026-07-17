"""Strict, immutable startup configuration for the CH1 and CH2 fetchers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .planning import POLICIES, ProductSelection


class FetchConfigError(ValueError):
    """An owned fetcher environment value is malformed or conflicting."""


@dataclass(frozen=True)
class OutputRoots:
    wind: str
    sunshine: str
    rain: str
    sunrain: str
    cloud: str


@dataclass(frozen=True)
class FetchStartupConfig:
    model: str
    force_refresh: bool
    profile_mode: str
    horizon_start: int
    horizon_end: int
    profile_chunk_id: str
    pinned_run: datetime | None
    require_full_horizon_run: bool
    horizon_fetch_batch: bool
    prefetch_next_horizon: bool
    release_profile_only_fields: bool
    download_workers: int
    fetch_tmp_dir: Path | None
    products: ProductSelection
    output_roots: OutputRoots


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    if name not in env:
        return default
    value = str(env[name]).strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise FetchConfigError(f"{name} must be a boolean (true/false, 1/0, yes/no, on/off)")


def _int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    if name not in env:
        return default
    try:
        value = int(str(env[name]).strip())
    except ValueError as exc:
        raise FetchConfigError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise FetchConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def _choice(env: Mapping[str, str], name: str, default: str, choices: set[str]) -> str:
    if name not in env:
        return default
    value = str(env[name]).strip().lower()
    if value not in choices:
        expected = ", ".join(sorted(choices))
        raise FetchConfigError(f"{name} must be one of: {expected}")
    return value


def _run_tag(env: Mapping[str, str], name: str) -> datetime | None:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return None
    for fmt in ("%Y%m%d_%H%M", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    raise FetchConfigError(f"{name} must use YYYYMMDD_HHMM or ISO UTC format")


def _nonempty(env: Mapping[str, str], name: str, default: str) -> str:
    if name not in env:
        return default
    value = str(env[name]).strip()
    if not value:
        raise FetchConfigError(f"{name} must not be empty")
    return value


def _enabled(env: Mapping[str, str], product: str, model: str, default: bool) -> bool:
    global_name = f"ENABLE_{product}_MAPS"
    model_name = f"{global_name}_{model.upper()}"
    global_enabled = _bool(env, global_name, default)
    model_enabled = _bool(env, model_name, default)
    return global_enabled and model_enabled


def parse_startup_config(
    model: str,
    env: Mapping[str, str],
    *,
    default_output_roots: OutputRoots,
) -> FetchStartupConfig:
    """Parse only fetcher-owned keys; unrelated environment values are ignored."""
    try:
        policy = POLICIES[model]
    except KeyError as exc:
        raise FetchConfigError(f"unsupported model: {model}") from exc

    prefix = model.upper()
    profile_mode = _choice(env, f"{prefix}_PROFILE_MODE", "direct-chunk", {"direct-chunk", "none"})
    horizon_start = _int(env, f"{prefix}_HORIZON_START", 0, 0, policy.absolute_max_horizon)
    horizon_end = _int(
        env,
        f"{prefix}_HORIZON_END",
        policy.absolute_max_horizon,
        0,
        policy.absolute_max_horizon,
    )
    # Preserve the legacy CLI behavior for reversed but otherwise valid ranges.
    if horizon_end < horizon_start:
        horizon_start, horizon_end = horizon_end, horizon_start

    run_tag = _run_tag(env, f"{prefix}_RUN_TAG")
    reference_time = _run_tag(env, f"{prefix}_REFERENCE_TIME")
    if run_tag is not None and reference_time is not None and run_tag != reference_time:
        raise FetchConfigError(
            f"{prefix}_RUN_TAG conflicts with {prefix}_REFERENCE_TIME; provide one run"
        )

    chunk_default = policy.profile_chunk_id(horizon_start, horizon_end)
    tmp_raw = str(env.get("XCBENZ_FETCH_TMP_DIR", "")).strip()
    products = ProductSelection(
        wind=_enabled(env, "WIND", model, False),
        sunshine=_enabled(env, "SUNSHINE", model, True),
        rain=_enabled(env, "RAIN", model, True),
        sunrain=_enabled(env, "SUNRAIN", model, True),
        cloud=_enabled(env, "CLOUD", model, False),
    )
    roots = OutputRoots(
        wind=_nonempty(env, f"{prefix}_WIND_MAP_OUT_ROOT", default_output_roots.wind),
        sunshine=_nonempty(env, f"{prefix}_SUNSHINE_MAP_OUT_ROOT", default_output_roots.sunshine),
        rain=_nonempty(env, f"{prefix}_RAIN_MAP_OUT_ROOT", default_output_roots.rain),
        sunrain=_nonempty(env, f"{prefix}_SUNRAIN_MAP_OUT_ROOT", default_output_roots.sunrain),
        cloud=_nonempty(env, f"{prefix}_CLOUD_MAP_OUT_ROOT", default_output_roots.cloud),
    )
    return FetchStartupConfig(
        model=model,
        force_refresh=_bool(env, "FORCE_REFRESH", False),
        profile_mode=profile_mode,
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        profile_chunk_id=_nonempty(env, f"{prefix}_PROFILE_CHUNK_ID", chunk_default),
        pinned_run=run_tag or reference_time,
        require_full_horizon_run=_bool(
            env,
            f"{prefix}_REQUIRE_FULL_HORIZON_RUN",
            profile_mode == "direct-chunk",
        ),
        horizon_fetch_batch=_bool(env, "XCBENZ_FETCH_HORIZON_BATCH", False),
        prefetch_next_horizon=_bool(env, "XCBENZ_PREFETCH_NEXT_HORIZON", False),
        release_profile_only_fields=_bool(env, "XCBENZ_RELEASE_PROFILE_ONLY_FIELDS", False),
        download_workers=_int(env, "DOWNLOAD_WORKERS", 4, 1, 8),
        fetch_tmp_dir=Path(tmp_raw) if tmp_raw else None,
        products=products,
        output_roots=roots,
    )
