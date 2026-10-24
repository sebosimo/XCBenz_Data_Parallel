#!/bin/sh
# Run inside the existing forecast image with separate W work/output mounts.
# Manual prototype work yields immediately to any production heavy job.
set -eu
lock_path="${XCBENZ_HEAVY_LOCK_PATH:-/run/lock/xcbenz-heavy.lock}"
mkdir -p "$(dirname "$lock_path")"
exec flock -n -E 75 -x "$lock_path" python -u -m w_forecast.runner "$@"
