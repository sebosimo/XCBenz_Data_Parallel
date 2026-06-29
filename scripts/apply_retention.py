"""Apply the production data-branch retention policy after artifact merge."""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path


RUN_FORMAT = "%Y%m%d_%H%M"


def log(message: str) -> None:
    print(f"[retention] {message}", flush=True)


def parse_run_tag(run_tag: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(run_tag, RUN_FORMAT).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def kept_run_tags(run_tags: list[str], *, anchor_hour: int, now: dt.datetime) -> set[str]:
    keep_dates = {now.date(), (now - dt.timedelta(days=1)).date()}
    sorted_tags = sorted(run_tags, reverse=True)
    keep = set(sorted_tags[:2])

    for run_tag in sorted_tags:
        run_dt = parse_run_tag(run_tag)
        if run_dt is None:
            continue
        if run_dt.hour == anchor_hour and run_dt.minute == 0 and run_dt.date() in keep_dates:
            keep.add(run_tag)
    return keep


def prune_run_dir(root: Path, *, anchor_hour: int) -> None:
    if not root.exists():
        return
    if not root.is_dir():
        log(f"Skipping non-directory root: {root}")
        return

    now = dt.datetime.now(dt.timezone.utc)
    run_dirs = [path for path in root.iterdir() if path.is_dir()]
    keep = kept_run_tags([path.name for path in run_dirs], anchor_hour=anchor_hour, now=now)

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
