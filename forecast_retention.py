"""Shared forecast-run retention and manifest inspection policy."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Iterable


RUN_FORMAT = "%Y%m%d_%H%M"
RUN_TAG = re.compile(r"^\d{8}_\d{4}$")
MODEL_ANCHOR_HOURS = {
    "icon-ch1": 3,
    "icon-ch2": 0,
}
LEGACY_MODEL_RUN_KEYS = {
    "icon-ch1": "runs",
    "icon-ch2": "runs_ch2",
}
FORECAST_PRODUCT_MANIFESTS = {
    "wind": Path("wind_maps/manifest.json"),
    "sunshine": Path("sunshine_maps/manifest.json"),
    "rain": Path("rain_maps/manifest.json"),
    "sunrain": Path("sunrain_maps/manifest.json"),
    "cloud": Path("cloud_maps/manifest.json"),
    "value_tiles": Path("value_tiles/v1/manifest.json"),
}
FORECAST_OWNED_PATHS = (
    "manifest.json",
    "locations.json",
    "region_forecasts",
    "emagrams",
    "thermal_panels",
    "wind_maps",
    "sunshine_maps",
    "rain_maps",
    "sunrain_maps",
    "cloud_maps",
    "value_tiles",
)


def parse_run_tag(run_tag: str) -> dt.datetime | None:
    if not RUN_TAG.fullmatch(run_tag):
        return None
    try:
        return dt.datetime.strptime(run_tag, RUN_FORMAT).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def valid_run_tags(run_tags: Iterable[str]) -> list[str]:
    return sorted({tag for tag in run_tags if parse_run_tag(tag) is not None}, reverse=True)


def kept_run_tags(
    run_tags: Iterable[str],
    *,
    anchor_hour: int,
    reference_run: str | None = None,
) -> set[str]:
    """Keep the newest two runs and two daily anchors relative to the newest run.

    Using the model's newest run rather than wall-clock time makes pruning and
    publication checks deterministic during delayed or recovery executions.
    """

    sorted_tags = valid_run_tags(run_tags)
    if not sorted_tags:
        return set()

    reference = parse_run_tag(reference_run) if reference_run else parse_run_tag(sorted_tags[0])
    if reference is None:
        raise ValueError(f"Invalid retention reference run: {reference_run!r}")

    keep = set(sorted_tags[:2])
    keep_dates = {reference.date(), (reference - dt.timedelta(days=1)).date()}
    for run_tag in sorted_tags:
        run_dt = parse_run_tag(run_tag)
        assert run_dt is not None
        if run_dt.hour == anchor_hour and run_dt.minute == 0 and run_dt.date() in keep_dates:
            keep.add(run_tag)
    return keep


def kept_model_run_tags(
    model_key: str,
    run_tags: Iterable[str],
    *,
    reference_run: str | None = None,
) -> set[str]:
    return kept_run_tags(
        run_tags,
        anchor_hour=MODEL_ANCHOR_HOURS[model_key],
        reference_run=reference_run,
    )


def load_json_object(path: Path, *, missing_ok: bool = False) -> dict[str, Any]:
    if missing_ok and not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Manifest is not a JSON object: {path}")
    return payload


def manifest_run_tags(manifest: dict[str, Any], model_key: str) -> set[str]:
    models = manifest.get("models")
    if isinstance(models, dict):
        model = models.get(model_key)
        if isinstance(model, dict):
            runs = model.get("runs")
            if isinstance(runs, dict):
                return set(valid_run_tags(str(tag) for tag in runs))

    legacy_runs = manifest.get(LEGACY_MODEL_RUN_KEYS[model_key])
    if isinstance(legacy_runs, dict):
        return set(valid_run_tags(str(tag) for tag in legacy_runs))
    return set()


def product_manifest_path(web_root: Path, product: str) -> Path:
    return web_root / FORECAST_PRODUCT_MANIFESTS[product]


def product_run_tags(web_root: Path, product: str, model_key: str) -> set[str]:
    manifest = load_json_object(product_manifest_path(web_root, product), missing_ok=True)
    return manifest_run_tags(manifest, model_key)


def required_retained_runs(
    current_runs: Iterable[str],
    candidate_runs: Iterable[str],
    *,
    model_key: str,
) -> set[str]:
    union = set(current_runs) | set(candidate_runs)
    candidate_valid = valid_run_tags(candidate_runs)
    reference_run = candidate_valid[0] if candidate_valid else None
    return kept_model_run_tags(model_key, union, reference_run=reference_run)
