#!/bin/sh
set -eu

lock_path="${XCBENZ_HEAVY_LOCK_PATH:-/run/lock/xcbenz-heavy.lock}"
export XCBENZ_PYTHON_CMD=python
export PYTHON_BIN=python

run_forecast() {
    exec python /app/scripts/run_coding_server_pipeline.py "$@"
}

if [ "${XCBENZ_HEAVY_LOCK_HELD:-0}" = "1" ]; then
    run_forecast "$@"
fi

mkdir -p "$(dirname "$lock_path")"
exec flock -x "$lock_path" \
    python /app/scripts/run_coding_server_pipeline.py "$@"
