"""Fast preflight for scheduled data updates.

This intentionally uses only the Python standard library so a scheduled run can
decide whether to continue before installing the heavier forecast stack.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request


COLLECTIONS = {
    "ch1": {
        "url": "https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-forecasting-icon-ch1/items",
        "collection_id": "ch.meteoschweiz.ogd-forecasting-icon-ch1",
        "slot_hours": 3,
        "lookback_slots": 16,
        "manifest_key": "runs",
        "web_model_key": "icon-ch1",
    },
    "ch2": {
        "url": "https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-forecasting-icon-ch2/items",
        "collection_id": "ch.meteoschweiz.ogd-forecasting-icon-ch2",
        "slot_hours": 6,
        "lookback_slots": 20,
        "manifest_key": "runs_ch2",
        "web_model_key": "icon-ch2",
    },
}


def log(message: str) -> None:
    print(f"[preflight] {message}", flush=True)


def get_json(url: str, timeout: int = 15) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "XCBenz_Data_Parallel/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url: str, payload: dict, timeout: int = 15) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "XCBenz_Data_Parallel/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def iso_horizon(hours: int) -> str:
    days = int(hours) // 24
    remainder = int(hours) % 24
    return f"P{days}DT{remainder}H"


def expected_horizon_count(model: str, run_tag: str) -> int:
    if model == "ch2":
        return 121

    try:
        run_hour = int(run_tag.split("_", 1)[1][:2])
    except Exception:
        return 34
    return 46 if run_hour == 3 else 34


def ch1_profile_matrix(run_tag: str) -> dict[str, list[dict[str, str]]]:
    chunks = [(0, 16), (17, 33)]
    try:
        run_hour = int(run_tag.split("_", 1)[1][:2])
    except (IndexError, ValueError):
        run_hour = -1
    if run_hour == 3:
        chunks.append((34, 45))
    return {
        "chunk": [
            {
                "id": f"H{start:03d}_H{end:03d}",
                "start": str(start),
                "end": str(end),
            }
            for start, end in chunks
        ]
    }


def has_profile_horizon(model: str, reference_datetime: dt.datetime, horizon: int) -> bool:
    cfg = COLLECTIONS[model]
    ref = reference_datetime.strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "collections": [cfg["collection_id"]],
        "forecast:variable": "T",
        "forecast:reference_datetime": ref,
        "forecast:perturbed": False,
        "forecast:horizon": iso_horizon(horizon),
        "limit": 1,
    }
    try:
        response = post_json("https://data.geo.admin.ch/api/stac/v1/search", payload, timeout=15)
    except Exception as exc:  # noqa: BLE001 - preflight should be best effort.
        log(f"{model} horizon probe failed for {ref} H+{horizon:03d}: {exc}")
        return False

    for feature in response.get("features") or []:
        props = feature.get("properties") or {}
        if (
            props.get("forecast:reference_datetime") == ref
            and props.get("forecast:variable") == "T"
            and props.get("forecast:perturbed") is False
        ):
            log(f"{model} horizon probe ok: {ref} H+{horizon:03d}")
            return True
    log(f"{model} horizon probe missing: {ref} H+{horizon:03d}")
    return False


def latest_run(model: str) -> str | None:
    cfg = COLLECTIONS[model]
    now = dt.datetime.now(dt.timezone.utc)
    slot = cfg["slot_hours"]
    hour = (now.hour // slot) * slot
    start = now.replace(hour=hour, minute=0, second=0, microsecond=0)

    for offset in range(cfg["lookback_slots"]):
        candidate = start - dt.timedelta(hours=offset * slot)
        ref = candidate.strftime("%Y-%m-%dT%H:%M:%SZ")
        tag = candidate.strftime("%Y%m%d_%H%M")
        required_horizon = expected_horizon_count(model, tag) - 1
        if has_profile_horizon(model, candidate, required_horizon):
            log(f"{model} latest profile-complete run: {tag}")
            return tag
    return None


def load_existing_manifest() -> dict:
    manifest_url = os.environ.get("XCBENZ_PREFLIGHT_MANIFEST_URL", "").strip()
    if manifest_url:
        manifest = get_json(manifest_url, timeout=15)
        if not isinstance(manifest, dict):
            raise ValueError(f"Live manifest is not a JSON object: {manifest_url}")
        log(f"Loaded existing manifest from {manifest_url}")
        return manifest

    repository = os.environ.get("GITHUB_REPOSITORY", "sebosimo/XCBenz_Data")
    branch = os.environ.get("DATA_BRANCH", "data-test")
    base_url = f"https://raw.githubusercontent.com/{repository}/{branch}"
    for path in ("web_exports/manifest.json", "manifest.json"):
        url = f"{base_url}/{path}"
        try:
            manifest = get_json(url, timeout=10)
            log(f"Loaded existing manifest from {path}")
            return manifest
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        except Exception as exc:  # noqa: BLE001 - absence should not block updates.
            log(f"Could not read existing {path}: {exc}")
            continue

    log(f"No existing manifest on {branch}; running full update.")
    return {}


def published_step_count(run_payload: dict) -> int:
    if not isinstance(run_payload, dict) or not run_payload:
        return 0

    counts = []
    locations = run_payload.get("locations")
    if isinstance(locations, dict):
        for location_payload in locations.values():
            if isinstance(location_payload, dict):
                steps = location_payload.get("steps")
                if isinstance(steps, list):
                    counts.append(len(steps))
        return min(counts) if counts else 0

    for steps in run_payload.values():
        if isinstance(steps, list):
            counts.append(len(steps))
    return min(counts) if counts else 0


def published_runs(manifest: dict, model: str) -> dict:
    cfg = COLLECTIONS[model]
    models = manifest.get("models")
    if isinstance(models, dict):
        web_model = models.get(cfg["web_model_key"])
        if isinstance(web_model, dict):
            runs = web_model.get("runs")
            if isinstance(runs, dict):
                return runs

    runs = manifest.get(cfg["manifest_key"])
    return runs if isinstance(runs, dict) else {}


def write_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def main() -> int:
    force_refresh = os.environ.get("FORCE_REFRESH", "").strip().lower() in {"1", "true", "yes", "on"}
    latest = {model: latest_run(model) for model in COLLECTIONS}
    write_output(
        "ch1_profile_matrix",
        json.dumps(ch1_profile_matrix(latest.get("ch1") or ""), separators=(",", ":")),
    )
    if force_refresh:
        write_output("should_run", "true")
        write_output("should_run_ch1", "true")
        write_output("should_run_ch2", "true")
        write_output("reason", "force_refresh")
        for model, tag in latest.items():
            write_output(f"latest_{model}", tag or "")
        return 0

    manifest = load_existing_manifest()
    missing = []
    model_should_run = {model: False for model in COLLECTIONS}
    for model, cfg in COLLECTIONS.items():
        tag = latest.get(model)
        if not tag:
            missing.append(f"{model}:no_available_run")
            model_should_run[model] = True
            continue
        runs = published_runs(manifest, model)
        if tag not in runs:
            missing.append(f"{model}:{tag}")
            model_should_run[model] = True
            continue

        expected = expected_horizon_count(model, tag)
        published = published_step_count(runs.get(tag) or {})
        if published < expected:
            missing.append(f"{model}:{tag}_incomplete:{published}/{expected}")
            model_should_run[model] = True

    should_run = bool(missing)
    write_output("should_run", "true" if should_run else "false")
    for model, value in model_should_run.items():
        write_output(f"should_run_{model}", "true" if value else "false")
    write_output("reason", ",".join(missing) if missing else "latest_runs_already_published")
    for model, tag in latest.items():
        write_output(f"latest_{model}", tag or "")
    log(f"should_run={should_run}; reason={','.join(missing) if missing else 'latest_runs_already_published'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
