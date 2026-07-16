#!/usr/bin/env bash
set -euo pipefail

EXPECTED_REMOTE_ROOT="sites/data.xcbenz.com/value-tiles-staging"
EXPECTED_BASE_URL="https://data.xcbenz.com/value-tiles-staging"
PRODUCTION_WEB_EXPORTS="sites/data.xcbenz.com/web_exports"

log() {
  printf '[deploy-value-tiles-staging] %s\n' "$*"
}

fail() {
  printf '[deploy-value-tiles-staging] ERROR: %s\n' "$*" >&2
  exit 1
}

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    fail "missing required environment variable: $name"
  fi
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

REMOTE_ROOT="${INFOMANIAK_VALUE_TILE_STAGING_ROOT:-}"
DATA_HOST_BASE_URL="${DATA_HOST_BASE_URL:-}"
WEB_EXPORT_DIR="${WEB_EXPORT_DIR:-web_exports}"
EXPECTED_VALUE_TILES_STATE="${EXPECTED_VALUE_TILES_STATE:-enabled}"

[[ "$REMOTE_ROOT" == "$EXPECTED_REMOTE_ROOT" ]] || \
  fail "refusing remote root '$REMOTE_ROOT'; expected '$EXPECTED_REMOTE_ROOT'"
[[ "$DATA_HOST_BASE_URL" == "$EXPECTED_BASE_URL" ]] || \
  fail "refusing data URL '$DATA_HOST_BASE_URL'; expected '$EXPECTED_BASE_URL'"
[[ "$WEB_EXPORT_DIR" == "web_exports" ]] || \
  fail "refusing WEB_EXPORT_DIR '$WEB_EXPORT_DIR'; expected repository web_exports"
[[ "$EXPECTED_VALUE_TILES_STATE" == "enabled" || "$EXPECTED_VALUE_TILES_STATE" == "disabled" ]] || \
  fail "EXPECTED_VALUE_TILES_STATE must be enabled or disabled"

require_env INFOMANIAK_HOST
require_env INFOMANIAK_USER

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"
INFOMANIAK_PORT="${INFOMANIAK_PORT:-22}"
RELEASE_ID="${RELEASE_ID:-$(date -u +'%Y%m%dT%H%M%SZ')-$$}"
[[ "$RELEASE_ID" =~ ^[0-9A-Za-z._-]+$ ]] || fail "RELEASE_ID contains unsafe characters"

STAGING_HTACCESS="deploy/infomaniak-value-tiles-staging.htaccess"
[[ -f "$WEB_EXPORT_DIR/manifest.json" ]] || fail "missing $WEB_EXPORT_DIR/manifest.json"
[[ -f "$STAGING_HTACCESS" ]] || fail "missing $STAGING_HTACCESS"

log "Validating the complete local candidate"
EXPECTED_WEB_EXPORT_DATA_ROOT="$EXPECTED_BASE_URL" \
  "$PYTHON_BIN" scripts/validate_outputs.py

"$PYTHON_BIN" - "$WEB_EXPORT_DIR/manifest.json" "$EXPECTED_VALUE_TILES_STATE" "$WEB_EXPORT_DIR" <<'PY'
import json
import sys
from pathlib import Path

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    manifest = json.load(handle)
state = sys.argv[2]
web_export_dir = Path(sys.argv[3])
capability = (manifest.get("capabilities") or {}).get("spatial_value_tiles")
expected = {
    "contract": "xcbenz-spatial-value-tiles",
    "contract_version": "1.0.0",
    "package": "immutable-chunks-cloud-dual-v1",
    "status": "dual_publish",
    "manifest": "web_exports/value_tiles/v1/manifest.json",
    "fallback": "whole_grid_split_binary_v1",
    "requires_range": False,
}
if state == "enabled":
    if capability != expected:
        raise SystemExit("enabled candidate does not advertise the exact spatial value-tile capability")
    if not (web_export_dir / "value_tiles" / "v1" / "manifest.json").is_file():
        raise SystemExit("enabled candidate is missing value_tiles/v1/manifest.json")
else:
    if capability is not None:
        raise SystemExit("disabled candidate still advertises the spatial value-tile capability")
    if (web_export_dir / "value_tiles").exists():
        raise SystemExit("disabled candidate still contains a value_tiles tree")
PY

KEY_FILE=""
cleanup_key=false
if [[ -n "${INFOMANIAK_SSH_KEY_PATH:-}" ]]; then
  KEY_FILE="$INFOMANIAK_SSH_KEY_PATH"
elif [[ -n "${INFOMANIAK_SSH_KEY:-}" ]]; then
  KEY_FILE="${RUNNER_TEMP:-/tmp}/infomaniak_value_tile_staging_key"
  printf '%s\n' "$INFOMANIAK_SSH_KEY" > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
  cleanup_key=true
else
  fail "set INFOMANIAK_SSH_KEY or INFOMANIAK_SSH_KEY_PATH"
fi
[[ -f "$KEY_FILE" ]] || fail "SSH key file not found: $KEY_FILE"

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"
if command -v ssh-keyscan >/dev/null 2>&1; then
  ssh-keyscan -p "$INFOMANIAK_PORT" "$INFOMANIAK_HOST" >> "$HOME/.ssh/known_hosts" 2>/dev/null || true
  chmod 600 "$HOME/.ssh/known_hosts" || true
fi

SSH_TARGET="${INFOMANIAK_USER}@${INFOMANIAK_HOST}"
SSH_OPTS=(
  -i "$KEY_FILE"
  -p "$INFOMANIAK_PORT"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
)
RSYNC_SSH="ssh -i $KEY_FILE -p $INFOMANIAK_PORT -o BatchMode=yes -o StrictHostKeyChecking=accept-new"

REMOTE_TMP="$REMOTE_ROOT/_upload_tmp_$RELEASE_ID"
REMOTE_CURRENT="$REMOTE_ROOT/web_exports"
REMOTE_PREVIOUS="$REMOTE_ROOT/_previous_web_exports"
REMOTE_FAILED="$REMOTE_ROOT/_failed_web_exports_$RELEASE_ID"
REMOTE_LOCK="$REMOTE_ROOT/.xcbenz_value_tile_staging_publish.lock"
LOCK_ID="value-tiles-staging-$RELEASE_ID"
lock_acquired=false
published=false
switched=false
retain_lock_on_failure=false

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

rollback_staging() {
  if [[ "$switched" != "true" ]]; then
    return
  fi
  log "Restoring the prior staging release"
  if ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
    set -e
    rm -rf '$REMOTE_FAILED'
    if [ -d '$REMOTE_CURRENT' ]; then
      mv '$REMOTE_CURRENT' '$REMOTE_FAILED'
    fi
    if [ -d '$REMOTE_PREVIOUS' ]; then
      mv '$REMOTE_PREVIOUS' '$REMOTE_CURRENT'
    fi
  "; then
    switched=false
    retain_lock_on_failure=false
  else
    retain_lock_on_failure=true
    log "Automatic staging rollback failed; inspect $REMOTE_ROOT while the staging lock is retained"
    return 1
  fi
}

cleanup() {
  if [[ "$published" != "true" && "$switched" == "true" ]]; then
    rollback_staging || true
  fi
  if [[ "$retain_lock_on_failure" != "true" ]]; then
    release_remote_lock
  fi
  if [[ "$published" != "true" ]]; then
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "rm -rf '$REMOTE_TMP'" >/dev/null 2>&1 || true
  fi
  if [[ "$cleanup_key" == "true" ]]; then
    rm -f "$KEY_FILE"
  fi
}
trap cleanup EXIT

log "Checking isolated remote root and production source"
production_manifest_before="$(ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
  set -e
  if [ -L '$REMOTE_ROOT' ]; then
    echo 'staging root must not be a symlink' >&2
    exit 41
  fi
  test -f '$PRODUCTION_WEB_EXPORTS/manifest.json'
  sha256sum '$PRODUCTION_WEB_EXPORTS/manifest.json' | awk '{print \$1}'
")"

log "Preparing isolated upload directory $REMOTE_TMP"
retry "prepare staging upload directory" \
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
    set -e
    mkdir -p '$REMOTE_ROOT'
    rm -rf '$REMOTE_TMP'
    mkdir -p '$REMOTE_TMP/web_exports'
  "

log "Uploading staging-only Apache configuration"
retry "upload staging Apache configuration" \
  rsync -az -e "$RSYNC_SSH" "$STAGING_HTACCESS" "$SSH_TARGET:$REMOTE_ROOT/.htaccess"

upload_started="$(date +%s)"
log "Uploading complete candidate to the isolated staging root"
retry "upload staging candidate" \
  rsync -az --delete --stats -e "$RSYNC_SSH" \
    "$WEB_EXPORT_DIR/" "$SSH_TARGET:$REMOTE_TMP/web_exports/"
upload_seconds="$(( $(date +%s) - upload_started ))"
log "Candidate upload completed in ${upload_seconds}s"

log "Acquiring staging-only publish lock"
retry "acquire staging publish lock" \
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
    set -e
    mkdir -p '$REMOTE_ROOT'
    if mkdir '$REMOTE_LOCK' 2>/dev/null; then
      printf '%s\n' '$LOCK_ID' > '$REMOTE_LOCK/owner'
    elif [ \"\$(cat '$REMOTE_LOCK/owner' 2>/dev/null || true)\" != '$LOCK_ID' ]; then
      exit 43
    fi
  "
lock_acquired=true

log "Copying live-owned subtrees read-only from production into staging"
retry "copy live-owned staging snapshots" \
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
    set -e
    source='$PRODUCTION_WEB_EXPORTS'
    target='$REMOTE_TMP/web_exports'
    for subtree in live_stations webcams radar_maps airspace; do
      if [ -d \"\$source/\$subtree\" ]; then
        rm -rf \"\$target/\$subtree\"
        cp -a \"\$source/\$subtree\" \"\$target/\$subtree\"
      fi
    done
    test -f \"\$target/manifest.json\"
    if find \"\$target\" -name '*.nc' -type f -print -quit | grep -q .; then
      exit 42
    fi
  "

log "Switching the isolated staging web_exports directory"
switch_metrics="$(ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
  set -e
  delete_started=\$(date +%s)
  rm -rf '$REMOTE_PREVIOUS'
  delete_seconds=\$((\$(date +%s) - delete_started))
  if [ -d '$REMOTE_CURRENT' ]; then
    mv '$REMOTE_CURRENT' '$REMOTE_PREVIOUS'
  fi
  if ! mv '$REMOTE_TMP/web_exports' '$REMOTE_CURRENT'; then
    if [ -d '$REMOTE_PREVIOUS' ]; then
      mv '$REMOTE_PREVIOUS' '$REMOTE_CURRENT'
    fi
    exit 44
  fi
  rmdir '$REMOTE_TMP' 2>/dev/null || true
  file_count=\$(find '$REMOTE_CURRENT' -type f | wc -l | tr -d ' ')
  printf 'delete_seconds=%s file_count=%s\n' \"\$delete_seconds\" \"\$file_count\"")"
switched=true
log "Staging switch complete: $switch_metrics"

log "Running remote validation against $DATA_HOST_BASE_URL with tiles $EXPECTED_VALUE_TILES_STATE"
if ! EXPECTED_VALUE_TILES_STATE="$EXPECTED_VALUE_TILES_STATE" \
  DATA_BASE_URL="$DATA_HOST_BASE_URL" \
  "$PYTHON_BIN" scripts/validate_remote_web_exports.py; then
  rollback_staging
  fail "remote validation failed and the prior staging release was restored"
fi

production_manifest_after="$(ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
  "sha256sum '$PRODUCTION_WEB_EXPORTS/manifest.json' | awk '{print \$1}'")"
if [[ "$production_manifest_before" != "$production_manifest_after" ]]; then
  rollback_staging
  fail "production manifest changed during staging publication"
fi

published=true
switched=false
release_remote_lock
log "Published and validated $DATA_HOST_BASE_URL/web_exports/ without changing production"
