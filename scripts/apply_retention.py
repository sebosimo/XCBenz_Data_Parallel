"""Apply the production data-branch retention policy after artifact merge."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from forecast_retention import kept_run_tags, parse_run_tag


def log(message: str) -> None:
    print(f"[retention] {message}", flush=True)


def prune_run_dir(root: Path, *, anchor_hour: int) -> None:
    if not root.exists():
        return
    if not root.is_dir():
        log(f"Skipping non-directory root: {root}")
        return

    run_dirs = [path for path in root.iterdir() if path.is_dir()]
    keep = kept_run_tags([path.name for path in run_dirs], anchor_hour=anchor_hour)

    removed = 0
    for path in run_dirs:
        if path.name in keep:
            continue
        if parse_run_tag(path.name) is None:
            continue
        shutil.rmtree(path)
        removed += 1
        log(f"Removed {path.as_posix()}")

    log(f"{root.as_posix()}: kept {len(keep)} run(s), removed {removed} run(s)")


def main() -> None:
    # CH1 production policy: keep top-2 latest + 03Z anchor from today/yesterday.
    prune_run_dir(Path("cache_wind_maps/ch1"), anchor_hour=3)
    prune_run_dir(Path("cache_sunshine_maps/ch1"), anchor_hour=3)
    prune_run_dir(Path("cache_rain_maps/ch1"), anchor_hour=3)
    prune_run_dir(Path("cache_sunrain_maps/ch1"), anchor_hour=3)
    prune_run_dir(Path("cache_cloud_maps/ch1"), anchor_hour=3)

    # CH2 production policy: keep top-2 latest + 00Z anchor from today/yesterday.
    prune_run_dir(Path("cache_wind_maps/ch2"), anchor_hour=0)
    prune_run_dir(Path("cache_sunshine_maps/ch2"), anchor_hour=0)
    prune_run_dir(Path("cache_rain_maps/ch2"), anchor_hour=0)
    prune_run_dir(Path("cache_sunrain_maps/ch2"), anchor_hour=0)
    prune_run_dir(Path("cache_cloud_maps/ch2"), anchor_hour=0)


if __name__ == "__main__":
    main()
