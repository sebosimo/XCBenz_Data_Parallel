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
import urllib.parse
import urllib.request


COLLECTIONS = {
    "ch1": {
        "url": "https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-forecasting-icon-ch1/items",
        "slot_hours": 3,
        "lookback_slots": 16,
        "manifest_key": "runs",
        "web_model_key": "icon-ch1",
    },
    "ch2": {
        "url": "https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-forecasting-icon-ch2/items",
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


def latest_run(model: str) -> str | None:
    cfg = COLLECTIONS[model]
    now = dt.datetime.now(dt.timezone.utc)
    slot = cfg["slot_hours"]
    hour = (now.hour // slot) * slot
    start = now.replace(hour=hour, minute=0, second=0, microsecond=0)

    for offset in range(cfg["lookback_slots"]):
        candidate = start - dt.timedelta(hours=offset * slot)
        ref = candidate.strftime("%Y-%m-%dT%H:%M:%SZ")
        query = urllib.parse.urlencode({"limit": 1, "forecast:reference_datetime": ref})
        url = f"{cfg['url']}?{query}"
        try:
            payload = get_json(url, timeout=10)
        except Exception as exc:  # noqa: BLE001 - preflight should be best effort.
            log(f"{model} probe failed for {ref}: {exc}")
            continue
        if payload.get("features"):
            tag = candidate.strftime("%Y%m%d_%H%M")
            log(f"{model} latest available run: {tag}")
            return tag
    return None


def load_existing_manifest() -> dict:
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


def expected_horizon_count(model: str, run_tag: str) -> int:
    if model == "ch2":
        return 121

    try:
        run_hour = int(run_tag.split("_", 1)[1][:2])
    except Exception:
        return 34
    return 46 if run_hour == 3 else 34


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
    if force_refresh:
        write_output("should_run", "true")
        write_output("should_run_ch1", "true")
        write_output("should_run_ch2", "true")
        write_output("reason", "force_refresh")
        return 0

    latest = {model: latest_run(model) for model in COLLECTIONS}
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
