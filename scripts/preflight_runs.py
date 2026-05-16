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
    },
    "ch2": {
        "url": "https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-forecasting-icon-ch2/items",
        "slot_hours": 6,
        "lookback_slots": 20,
        "manifest_key": "runs_ch2",
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
    url = f"https://raw.githubusercontent.com/{repository}/{branch}/manifest.json"
    try:
        return get_json(url, timeout=10)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            log(f"No existing manifest on {branch}; running full update.")
            return {}
        raise
    except Exception as exc:  # noqa: BLE001 - absence should not block updates.
        log(f"Could not read existing manifest: {exc}")
        return {}


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
        write_output("reason", "force_refresh")
        return 0

    latest = {model: latest_run(model) for model in COLLECTIONS}
    manifest = load_existing_manifest()
    missing = []
    for model, cfg in COLLECTIONS.items():
        tag = latest.get(model)
        if not tag:
            missing.append(f"{model}:no_available_run")
            continue
        runs = manifest.get(cfg["manifest_key"]) or {}
        if tag not in runs:
            missing.append(f"{model}:{tag}")

    should_run = bool(missing)
    write_output("should_run", "true" if should_run else "false")
    write_output("reason", ",".join(missing) if missing else "latest_runs_already_published")
    for model, tag in latest.items():
        write_output(f"latest_{model}", tag or "")
    log(f"should_run={should_run}; reason={','.join(missing) if missing else 'latest_runs_already_published'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
