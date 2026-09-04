#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[history-hydrate] %s\n' "$*"
}

fail() {
  printf '[history-hydrate] ERROR: %s\n' "$*" >&2
  exit 1
}

retry() {
  local label="$1"
  shift
  local max_attempts="${DEPLOY_RETRIES:-3}"
  local delay_seconds="${DEPLOY_RETRY_DELAY_SECONDS:-20}"
  local attempt=1
  local rc=0
  while true; do
    if "$@"; then
      return 0
    fi
    rc=$?
    if (( attempt >= max_attempts )); then
      log "$label failed after $attempt attempt(s)"
      return "$rc"
    fi
    log "$label failed on attempt $attempt/$max_attempts with exit $rc; retrying in ${delay_seconds}s"
    sleep "$delay_seconds"
    attempt=$((attempt + 1))
  done
}

require_env() {
  local name="$1"
  [[ -n "${!name:-}" ]] || fail "missing required environment variable: $name"
}

if [[ $# -ne 2 ]]; then
  fail "usage: $0 CH1_RUN_TAG CH2_RUN_TAG"
fi

CH1_RUN_TAG="$1"
CH2_RUN_TAG="$2"
WEB_EXPORT_DIR="${WEB_EXPORT_DIR:-web_exports}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INFOMANIAK_PORT="${INFOMANIAK_PORT:-22}"
RELEASE_ID="${RELEASE_ID:-$(date -u +'%Y%m%dT%H%M%SZ')}-$$"

require_env INFOMANIAK_HOST
require_env INFOMANIAK_USER
require_env INFOMANIAK_DATA_ROOT
command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "Python executable not found: $PYTHON_BIN"

KEY_FILE=""
cleanup_key=false
KNOWN_HOSTS_FILE=""
cleanup_known_hosts=false
if [[ -n "${INFOMANIAK_SSH_KEY_PATH:-}" ]]; then
  KEY_FILE="$INFOMANIAK_SSH_KEY_PATH"
elif [[ -n "${INFOMANIAK_SSH_KEY:-}" ]]; then
  KEY_FILE="${RUNNER_TEMP:-/tmp}/infomaniak_history_key_$RELEASE_ID"
  printf '%s\n' "$INFOMANIAK_SSH_KEY" > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
  cleanup_key=true
else
  fail "set INFOMANIAK_SSH_KEY or INFOMANIAK_SSH_KEY_PATH"
fi
[[ -f "$KEY_FILE" ]] || fail "SSH key file not found: $KEY_FILE"

if [[ -n "${INFOMANIAK_KNOWN_HOSTS_PATH:-}" ]]; then
  KNOWN_HOSTS_FILE="$INFOMANIAK_KNOWN_HOSTS_PATH"
elif [[ -n "${INFOMANIAK_KNOWN_HOSTS:-}" ]]; then
  KNOWN_HOSTS_FILE="${RUNNER_TEMP:-/tmp}/infomaniak_history_known_hosts_$RELEASE_ID"
  printf '%s\n' "$INFOMANIAK_KNOWN_HOSTS" > "$KNOWN_HOSTS_FILE"
  chmod 600 "$KNOWN_HOSTS_FILE"
  cleanup_known_hosts=true
else
  fail "set INFOMANIAK_KNOWN_HOSTS or INFOMANIAK_KNOWN_HOSTS_PATH"
fi
[[ -s "$KNOWN_HOSTS_FILE" ]] || fail "pinned SSH known-hosts file not found or empty: $KNOWN_HOSTS_FILE"

SSH_TARGET="${INFOMANIAK_USER}@${INFOMANIAK_HOST}"
SSH_OPTS=(
  -i "$KEY_FILE"
  -p "$INFOMANIAK_PORT"
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o "UserKnownHostsFile=$KNOWN_HOSTS_FILE"
)
RSYNC_SSH="ssh -i $KEY_FILE -p $INFOMANIAK_PORT -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS_FILE"

REMOTE_ROOT="${INFOMANIAK_DATA_ROOT%/}"
REMOTE_CURRENT="$REMOTE_ROOT/web_exports"
REMOTE_LOCK="$REMOTE_ROOT/.xcbenz_web_exports_publish.lock"
REMOTE_BREAK_LOCK="$REMOTE_LOCK.break"
REMOTE_SNAPSHOT="$REMOTE_ROOT/_history_snapshot_$RELEASE_ID"
LOCK_ID="history-hydrate-$RELEASE_ID"
DEPLOY_LOCK_WAIT_SECONDS="${DEPLOY_LOCK_WAIT_SECONDS:-300}"
DEPLOY_LOCK_POLL_SECONDS="${DEPLOY_LOCK_POLL_SECONDS:-5}"
DEPLOY_LOCK_STALE_SECONDS="${DEPLOY_LOCK_STALE_SECONDS:-1800}"
DEPLOY_LOCK_BREAK_STALE_SECONDS="${DEPLOY_LOCK_BREAK_STALE_SECONDS:-120}"
lock_acquired=false

LOCAL_TMP="$(mktemp -d "${RUNNER_TEMP:-/tmp}/xcbenz-history-$RELEASE_ID.XXXXXX")"
MANIFEST_ROOT="$LOCAL_TMP/current-manifests"
STAGE_ROOT="$LOCAL_TMP/web_exports"
FULL_MANIFEST_ROOT="$LOCAL_TMP/full-manifests"

acquire_remote_lock() {
  log "Acquiring remote publish lock"
  retry "acquire remote publish lock" \
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
    set -e
    mkdir -p '$REMOTE_ROOT'
    deadline=\$((\$(date +%s) + $DEPLOY_LOCK_WAIT_SECONDS))
    break_lock='$REMOTE_BREAK_LOCK'
    while true; do
      now=\$(date +%s)
      if [ -d \"\$break_lock\" ]; then
        break_mtime=\$(stat -c %Y \"\$break_lock\" 2>/dev/null || echo 0)
        break_age=\$((now - break_mtime))
        if [ \"\$break_mtime\" -gt 0 ] && [ \"\$break_age\" -ge '$DEPLOY_LOCK_BREAK_STALE_SECONDS' ]; then
          rm -rf \"\$break_lock\"
        else
          sleep '$DEPLOY_LOCK_POLL_SECONDS'
          continue
        fi
      fi
      if mkdir '$REMOTE_LOCK' 2>/dev/null; then
        break
      fi
      lock_mtime=\$(stat -c %Y '$REMOTE_LOCK' 2>/dev/null || echo 0)
      lock_age=\$((now - lock_mtime))
      if [ \"\$lock_mtime\" -gt 0 ] && [ \"\$lock_age\" -ge '$DEPLOY_LOCK_STALE_SECONDS' ] && mkdir \"\$break_lock\" 2>/dev/null; then
        current_mtime=\$(stat -c %Y '$REMOTE_LOCK' 2>/dev/null || echo 0)
        current_age=\$((\$(date +%s) - current_mtime))
        if [ \"\$current_mtime\" -gt 0 ] && [ \"\$current_age\" -ge '$DEPLOY_LOCK_STALE_SECONDS' ]; then
          rm -rf '$REMOTE_LOCK'
        fi
        rmdir \"\$break_lock\" 2>/dev/null || true
        continue
      fi
      if [ \"\$now\" -ge \"\$deadline\" ]; then
        exit 42
      fi
      sleep '$DEPLOY_LOCK_POLL_SECONDS'
    done
    printf '%s\n' '$LOCK_ID' > '$REMOTE_LOCK/owner'
    date -u +%Y-%m-%dT%H:%M:%SZ > '$REMOTE_LOCK/created_at'
  "
  lock_acquired=true
}

release_remote_lock() {
  if [[ "$lock_acquired" != "true" ]]; then
    return
  fi
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
    if [ -d '$REMOTE_LOCK' ] && [ \"\$(cat '$REMOTE_LOCK/owner' 2>/dev/null || true)\" = '$LOCK_ID' ]; then
      rm -rf '$REMOTE_LOCK'
    fi
  " >/dev/null 2>&1 || true
  lock_acquired=false
}

remove_remote_snapshot() {
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "rm -rf '$REMOTE_SNAPSHOT'" >/dev/null 2>&1 || true
}

cleanup() {
  release_remote_lock
  remove_remote_snapshot
  rm -rf "$LOCAL_TMP"
  if [[ "$cleanup_key" == "true" ]]; then
    rm -f "$KEY_FILE"
  fi
  if [[ "$cleanup_known_hosts" == "true" ]]; then
    rm -f "$KNOWN_HOSTS_FILE"
  fi
}
trap cleanup EXIT

snapshot_manifest_set() {
  acquire_remote_lock
  retry "snapshot production manifests" \
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
      set -e
      rm -rf '$REMOTE_SNAPSHOT'
      target='$REMOTE_SNAPSHOT/web_exports'
      current='$REMOTE_CURRENT'
      mkdir -p \"\$target\"
      for item in \
        manifest.json \
        wind_maps/manifest.json \
        sunshine_maps/manifest.json \
        rain_maps/manifest.json \
        sunrain_maps/manifest.json \
        cloud_maps/manifest.json \
        value_tiles/v1/manifest.json; do
        if [ -f \"\$current/\$item\" ]; then
          mkdir -p \"\$target/\$(dirname \"\$item\")\"
          cp -a \"\$current/\$item\" \"\$target/\$item\"
        fi
      done
    "
  release_remote_lock
  mkdir -p "$MANIFEST_ROOT"
  retry "download production manifests" \
    rsync -az --delete -e "$RSYNC_SSH" \
      "$SSH_TARGET:$REMOTE_SNAPSHOT/web_exports/" "$MANIFEST_ROOT/"
  remove_remote_snapshot
}

checker_args=(
  --ch1-run-tag "$CH1_RUN_TAG"
  --ch2-run-tag "$CH2_RUN_TAG"
)
if [[ "${ENABLE_VALUE_TILES:-false}" == "true" ]]; then
  checker_args+=(--include-value-tiles)
fi

snapshot_manifest_set
if [[ ! -f "$MANIFEST_ROOT/manifest.json" ]]; then
  log "Production has no forecast manifest; no history is available to hydrate"
  exit 0
fi

set +e
"$PYTHON_BIN" scripts/check_forecast_history.py \
  --local-root "$WEB_EXPORT_DIR" \
  --current-root "$MANIFEST_ROOT" \
  "${checker_args[@]}"
history_rc=$?
set -e
if [[ "$history_rc" == "0" ]]; then
  log "Persistent local forecast history is complete"
  exit 0
fi
if [[ "$history_rc" != "10" ]]; then
  fail "history comparison failed with exit $history_rc"
fi

log "Local history is incomplete; creating an immutable production snapshot"
acquire_remote_lock
retry "snapshot production forecast history" \
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
    set -e
    rm -rf '$REMOTE_SNAPSHOT'
    target='$REMOTE_SNAPSHOT/web_exports'
    current='$REMOTE_CURRENT'
    mkdir -p \"\$target\"
    for item in \
      manifest.json locations.json region_forecasts emagrams thermal_panels \
      wind_maps sunshine_maps rain_maps sunrain_maps cloud_maps value_tiles; do
      if [ -e \"\$current/\$item\" ]; then
        cp -al \"\$current/\$item\" \"\$target/\$item\"
      fi
    done
  "
release_remote_lock

mkdir -p "$STAGE_ROOT"
retry "download production forecast history" \
  rsync -az --delete -e "$RSYNC_SSH" \
    "$SSH_TARGET:$REMOTE_SNAPSHOT/web_exports/" "$STAGE_ROOT/"
remove_remote_snapshot

mkdir -p "$FULL_MANIFEST_ROOT"
for item in \
  manifest.json \
  wind_maps/manifest.json \
  sunshine_maps/manifest.json \
  rain_maps/manifest.json \
  sunrain_maps/manifest.json \
  cloud_maps/manifest.json \
  value_tiles/v1/manifest.json; do
  if [[ -f "$STAGE_ROOT/$item" ]]; then
    mkdir -p "$FULL_MANIFEST_ROOT/$(dirname "$item")"
    cp "$STAGE_ROOT/$item" "$FULL_MANIFEST_ROOT/$item"
  fi
done

"$PYTHON_BIN" scripts/check_forecast_history.py \
  --local-root "$STAGE_ROOT" \
  --current-root "$FULL_MANIFEST_ROOT" \
  "${checker_args[@]}"

mkdir -p "$WEB_EXPORT_DIR"
for item in \
  manifest.json locations.json region_forecasts emagrams thermal_panels \
  wind_maps sunshine_maps rain_maps sunrain_maps cloud_maps value_tiles; do
  rm -rf "$WEB_EXPORT_DIR/$item"
  if [[ -e "$STAGE_ROOT/$item" ]]; then
    mv "$STAGE_ROOT/$item" "$WEB_EXPORT_DIR/$item"
  fi
done

"$PYTHON_BIN" scripts/check_forecast_history.py \
  --local-root "$WEB_EXPORT_DIR" \
  --current-root "$FULL_MANIFEST_ROOT" \
  "${checker_args[@]}"
log "Hydrated and validated authoritative production forecast history"
