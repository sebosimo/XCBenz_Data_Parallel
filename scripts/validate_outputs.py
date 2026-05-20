"""Validate publish-ready data outputs before pushing the data branch."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


WEB_MANIFEST = Path("web_exports/manifest.json")
ROOT_MANIFEST = Path("manifest.json")


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing required file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def fail(message: str) -> int:
    print(f"[validate] ERROR: {message}", flush=True)
    return 1


def main() -> int:
    try:
        root = load_json(ROOT_MANIFEST)
        web = load_json(WEB_MANIFEST)
    except Exception as exc:  # noqa: BLE001 - this is a CI guard.
        return fail(str(exc))

    expected_root = os.getenv("EXPECTED_WEB_EXPORT_DATA_ROOT")
    actual_root = (web.get("source") or {}).get("data_root")
    if expected_root and actual_root != expected_root:
        return fail(f"web export data_root is {actual_root!r}, expected {expected_root!r}")

    counts = web.get("counts") or {}
    if int(counts.get("profiles") or 0) <= 0:
        return fail("web manifest has no profiles")
    if int(counts.get("region_forecasts") or 0) <= 0:
        return fail("web manifest has no region forecasts")

    models = web.get("models") or {}
    for model_key in ("icon-ch1", "icon-ch2"):
        model = models.get(model_key) or {}
        runs = model.get("runs") or {}
        if not runs:
            return fail(f"web manifest has no runs for {model_key}")
        if len(runs) > 4:
            return fail(f"web manifest has {len(runs)} runs for {model_key}; retention should keep at most 4")
        latest = model.get("latest_run")
        if latest and latest not in runs:
            return fail(f"{model_key} latest_run {latest!r} is absent from runs")

    map_products = ((web.get("products") or {}).get("maps") or {})
    for product in ("wind", "sunshine"):
        path = map_products.get(product)
        if not path:
            return fail(f"web manifest has no {product} map product")
        if not Path(path).exists():
            return fail(f"{product} map manifest path does not exist: {path}")

    root_runs = (root.get("runs") or {}, root.get("runs_ch2") or {})
    if not any(root_runs):
        return fail("root manifest has neither CH1 nor CH2 runs")

    print(
        "[validate] OK: "
        f"profiles={counts.get('profiles')}, "
        f"region_forecasts={counts.get('region_forecasts')}, "
        f"wind_steps={counts.get('wind_map_steps')}, "
        f"sunshine_steps={counts.get('sunshine_map_steps')}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
