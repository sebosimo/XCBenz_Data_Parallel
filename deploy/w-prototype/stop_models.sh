#!/bin/sh
# Stop only this service's named containers, including after a timeout.
set -eu
for name in xcbenz-w-feed-ch1 xcbenz-w-feed-ch2; do
    owner="$(docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "$name" 2>/dev/null || true)"
    if [ "$owner" = "xcbenz-w-prototype" ]; then
        docker stop --time 20 "$name"
    fi
done
