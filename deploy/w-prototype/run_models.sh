#!/bin/sh
# Independent scheduled feed. Reuses the existing forecast image without secrets.
set -eu
code_dir="${W_CODE_DIR:-/root/xcbenz-w-prototype}"
export W_FORECAST_IMAGE="${W_FORECAST_IMAGE:-xcbenz-forecast:local}"
export W_WORK_DIR="${W_WORK_DIR:-/data/xcbenz-w-prototype/work}"
export W_FEED_DIR="${W_FEED_DIR:-/data/xcbenz-w-prototype/feed}"
export W_HEAVY_LOCK_VOLUME="${W_HEAVY_LOCK_VOLUME:-hetzner-deploy_xcbenz-heavy-lock}"
export W_SOURCE_REVISION="$(cat "$code_dir/REVISION")"
result=0
for model in ch1 ch2; do
    export W_MODEL="$model"
    if docker compose -p xcbenz-w-prototype -f "$code_dir/deploy/w-prototype/compose.yaml" \
        run --rm --no-deps --name "xcbenz-w-feed-$model" w-prototype \
        --model "$model" --mode scheduled_forecast --packaging packed-v2 \
        --work-dir /w-work --output "/w-feed/$model"; then
        :
    else
        status=$?
        # Give production and host headroom priority. The timer will retry.
        if [ "$status" -eq 75 ]; then
            exit 75
        fi
        result=1
    fi
done
exit "$result"
