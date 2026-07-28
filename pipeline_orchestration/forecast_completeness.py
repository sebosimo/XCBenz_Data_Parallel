"""Shared completeness checks for source STAC items and published profiles."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import Any

from pipeline_orchestration.job_plan import expected_horizon_count


SEARCH_URL = "https://data.geo.admin.ch/api/stac/v1/search"
PROFILE_VARIABLES = ("T", "U", "V", "P", "QV")
_DURATION_RE = re.compile(
    r"^P"
    r"(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T"
    r"(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?"
    r")?$"
)


def parse_horizon_hours(value: object) -> int | None:
    """Return whole forecast hours for the ISO-8601 duration forms used by STAC."""
    if not isinstance(value, str):
        return None
    match = _DURATION_RE.fullmatch(value)
    if not match:
        return None
    total = (
        float(match.group("days") or 0) * 24
        + float(match.group("hours") or 0)
        + float(match.group("minutes") or 0) / 60
        + float(match.group("seconds") or 0) / 3600
    )
    rounded = round(total)
    return int(rounded) if abs(total - rounded) < 1e-9 else None


def _next_request(
    response: dict[str, Any],
    base_payload: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    for link in response.get("links") or []:
        if not isinstance(link, dict) or link.get("rel") != "next":
            continue
        href = link.get("href")
        if not isinstance(href, str) or not href:
            continue
        body = link.get("body")
        page_payload = dict(base_payload)
        if isinstance(body, dict):
            page_payload.update(body)
        return href, page_payload
    return None


def stac_variable_horizons(
    *,
    collection_id: str,
    reference_datetime: str,
    variable: str,
    post_json: Callable[[str, dict[str, Any], int], dict[str, Any]],
    timeout: int,
    page_limit: int = 100,
    max_pages: int = 10,
) -> set[int]:
    """Collect every matching horizon, following STAC POST pagination safely."""
    base_payload: dict[str, Any] = {
        "collections": [collection_id],
        "forecast:variable": variable,
        "forecast:reference_datetime": reference_datetime,
        "forecast:perturbed": False,
        "limit": page_limit,
    }
    url = SEARCH_URL
    payload = dict(base_payload)
    seen_requests: set[tuple[str, str]] = set()
    horizons: set[int] = set()

    for _page in range(max_pages):
        request_key = (url, json.dumps(payload, sort_keys=True, separators=(",", ":")))
        if request_key in seen_requests:
            raise ValueError(f"STAC pagination loop for {variable} at {reference_datetime}")
        seen_requests.add(request_key)

        response = post_json(url, payload, timeout)
        for feature in response.get("features") or []:
            if not isinstance(feature, dict):
                continue
            props = feature.get("properties") or {}
            if (
                props.get("forecast:reference_datetime") != reference_datetime
                or props.get("forecast:variable") != variable
                or props.get("forecast:perturbed") is not False
            ):
                continue
            horizon = parse_horizon_hours(props.get("forecast:horizon"))
            if horizon is not None:
                horizons.add(horizon)

        next_request = _next_request(response, base_payload)
        if next_request is None:
            return horizons
        url, payload = next_request

    raise ValueError(f"STAC pagination exceeded {max_pages} pages for {variable} at {reference_datetime}")


def missing_profile_horizons(
    *,
    collection_id: str,
    reference_datetime: str,
    expected_count: int,
    post_json: Callable[[str, dict[str, Any], int], dict[str, Any]],
    timeout: int,
    variables: Iterable[str] = PROFILE_VARIABLES,
) -> dict[str, tuple[int, ...]]:
    """Return missing required horizons per variable; harmless extras are allowed."""
    expected = set(range(expected_count))
    missing: dict[str, tuple[int, ...]] = {}
    for variable in variables:
        observed = stac_variable_horizons(
            collection_id=collection_id,
            reference_datetime=reference_datetime,
            variable=variable,
            post_json=post_json,
            timeout=timeout,
        )
        absent = tuple(sorted(expected.difference(observed)))
        if absent:
            missing[variable] = absent
    return missing


def expected_step_labels(model: str, run_tag: str) -> tuple[str, ...]:
    count = expected_horizon_count(model, run_tag)
    digits = 2 if model in {"ch1", "icon-ch1"} else 3
    return tuple(f"H{horizon:0{digits}d}" for horizon in range(count))


def step_labels(steps: object) -> tuple[str, ...]:
    if not isinstance(steps, list):
        return ()
    labels: list[str] = []
    for step in steps:
        label = step.get("step") if isinstance(step, dict) else step
        if isinstance(label, str):
            labels.append(label)
    return tuple(labels)


def profile_run_errors(model: str, run_tag: str, run_entry: object) -> list[str]:
    """Validate every location in a generated latest-run manifest exactly."""
    if not isinstance(run_entry, dict):
        return ["run entry is not an object"]
    locations = run_entry.get("locations")
    if not isinstance(locations, dict) or not locations:
        return ["run has no locations"]

    expected = expected_step_labels(model, run_tag)
    errors: list[str] = []
    for location_id, location_entry in locations.items():
        if not isinstance(location_entry, dict):
            errors.append(f"{location_id}: location entry is not an object")
            continue
        actual = step_labels(location_entry.get("steps"))
        if actual != expected:
            missing = sorted(set(expected).difference(actual))
            extra = sorted(set(actual).difference(expected))
            detail = f"{len(actual)}/{len(expected)} steps"
            if missing:
                detail += f", missing {','.join(missing[:5])}"
            if extra:
                detail += f", unexpected {','.join(extra[:5])}"
            errors.append(f"{location_id}: {detail}")
    return errors

