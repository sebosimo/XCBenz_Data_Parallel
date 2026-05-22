#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[deploy-data] %s\n' "$*"
}

fail() {
  printf '[deploy-data] ERROR: %s\n' "$*" >&2
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
  if [[ -z "${!name:-}" ]]; then
    fail "missing required environment variable: $name"
  fi
}

require_env INFOMANIAK_HOST
require_env INFOMANIAK_USER
require_env INFOMANIAK_DATA_ROOT

INFOMANIAK_PORT="${INFOMANIAK_PORT:-22}"
DATA_HOST_BASE_URL="${DATA_HOST_BASE_URL:-https://data.xcbenz.com}"
WEB_EXPORT_DIR="${WEB_EXPORT_DIR:-web_exports}"
RELEASE_ID="${RELEASE_ID:-$(date -u +'%Y%m%dT%H%M%SZ')}"

[[ -f "$WEB_EXPORT_DIR/manifest.json" ]] || fail "missing $WEB_EXPORT_DIR/manifest.json"

nc_count="$(find "$WEB_EXPORT_DIR" -name '*.nc' -type f | wc -l | tr -d ' ')"
if [[ "$nc_count" != "0" ]]; then
  fail "$WEB_EXPORT_DIR contains $nc_count NetCDF file(s)"
fi

KEY_FILE=""
cleanup_key=false
if [[ -n "${INFOMANIAK_SSH_KEY_PATH:-}" ]]; then
  KEY_FILE="$INFOMANIAK_SSH_KEY_PATH"
elif [[ -n "${INFOMANIAK_SSH_KEY:-}" ]]; then
  KEY_FILE="${RUNNER_TEMP:-/tmp}/infomaniak_data_deploy_key"
  printf '%s\n' "$INFOMANIAK_SSH_KEY" > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
  cleanup_key=true
else
  fail "set INFOMANIAK_SSH_KEY or INFOMANIAK_SSH_KEY_PATH"
fi

if [[ ! -f "$KEY_FILE" ]]; then
  fail "SSH key file not found: $KEY_FILE"
fi

cleanup() {
  if [[ "$cleanup_key" == "true" ]]; then
    rm -f "$KEY_FILE"
  fi
}
trap cleanup EXIT

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

REMOTE_ROOT="${INFOMANIAK_DATA_ROOT%/}"
REMOTE_TMP="$REMOTE_ROOT/_upload_tmp_$RELEASE_ID"
REMOTE_CURRENT="$REMOTE_ROOT/web_exports"
REMOTE_PREVIOUS="$REMOTE_ROOT/_previous_web_exports"

log "Preparing remote upload directory $REMOTE_TMP"
retry "prepare remote upload directory" \
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "mkdir -p '$REMOTE_TMP/web_exports'"

if [[ -f "deploy/infomaniak-data.htaccess" ]]; then
  log "Uploading data host .htaccess"
  retry "upload data host .htaccess" \
    rsync -az -e "$RSYNC_SSH" "deploy/infomaniak-data.htaccess" "$SSH_TARGET:$REMOTE_ROOT/.htaccess"
fi

log "Uploading $WEB_EXPORT_DIR to $REMOTE_TMP/web_exports"
retry "upload $WEB_EXPORT_DIR" \
  rsync -az --delete -e "$RSYNC_SSH" "$WEB_EXPORT_DIR/" "$SSH_TARGET:$REMOTE_TMP/web_exports/"

log "Switching remote web_exports directory"
retry "switch remote web_exports directory" \
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
  set -e
  mkdir -p '$REMOTE_ROOT'
  rm -rf '$REMOTE_PREVIOUS'
  if [ -d '$REMOTE_CURRENT' ]; then
    mv '$REMOTE_CURRENT' '$REMOTE_PREVIOUS'
  fi
  mv '$REMOTE_TMP/web_exports' '$REMOTE_CURRENT'
  rmdir '$REMOTE_TMP' 2>/dev/null || true
  find '$REMOTE_CURRENT' -name '*.nc' -type f -print -quit | grep -q . && exit 20 || true
  find '$REMOTE_CURRENT' -maxdepth 2 -type f | wc -l
"

log "Published $WEB_EXPORT_DIR to $DATA_HOST_BASE_URL/web_exports/"
