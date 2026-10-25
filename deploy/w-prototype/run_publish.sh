#!/bin/sh
# Credentials belong only to this lightweight host publisher, never W workers.
set -eu
code_dir="${W_CODE_DIR:-/root/xcbenz-w-prototype}"
cd "$code_dir"
result=0
for model in ch1 ch2; do
    if python3 -m w_forecast.publication publish \
        --feed "${W_FEED_DIR:-/data/xcbenz-w-prototype/feed}" --model "$model"; then
        :
    else
        result=1
    fi
done
exit "$result"
