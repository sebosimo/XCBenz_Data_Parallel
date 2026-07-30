#!/usr/bin/env bash
set -euo pipefail

EXPECTED_DATA_ROOT="sites/data.xcbenz.com"
EXPECTED_REMOTE_ROOT="$EXPECTED_DATA_ROOT/wind-streamline-beta2"
EXPECTED_BASE_URL="https://data.xcbenz.com/wind-streamline-beta2"

log() {
  printf '[deploy-wind-streamline-beta2] %s\n' "$*"
}

fail() {
  printf '[deploy-wind-streamline-beta2] ERROR: %s\n' "$*" >&2
  exit 1
}

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    fail "missing required environment variable: $name"
  fi
}

if [[ $# -ne 1 ]]; then
  fail "usage: $0 PACKAGE_DIRECTORY"
fi

PACKAGE_DIRECTORY="$(realpath "$1")"
[[ -d "$PACKAGE_DIRECTORY" ]] || fail "package directory does not exist"
[[ -f "$PACKAGE_DIRECTORY/manifest.json" ]] || fail "package manifest is missing"

require_env INFOMANIAK_HOST
require_env INFOMANIAK_USER
require_env INFOMANIAK_DATA_ROOT

[[ "${INFOMANIAK_DATA_ROOT%/}" == "$EXPECTED_DATA_ROOT" ]] || \
  fail "refusing unexpected INFOMANIAK_DATA_ROOT"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INFOMANIAK_PORT="${INFOMANIAK_PORT:-22}"
HTACCESS="$REPO_ROOT/deploy/infomaniak-wind-streamline-beta2.htaccess"
[[ -f "$HTACCESS" ]] || fail "beta2 XWS2 htaccess is missing"

log "Strictly validating the complete local package"
"$PYTHON_BIN" "$REPO_ROOT/scripts/validate_wind_streamline_package.py" \
  "$PACKAGE_DIRECTORY" \
  --require-complete-pilot

MODEL="$("$PYTHON_BIN" -c \
  'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["source"]["model"])' \
  "$PACKAGE_DIRECTORY/manifest.json")"
RUN="$("$PYTHON_BIN" -c \
  'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["source"]["run"])' \
  "$PACKAGE_DIRECTORY/manifest.json")"
LEVEL="$("$PYTHON_BIN" -c \
  'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["source"]["level"]["name"])' \
  "$PACKAGE_DIRECTORY/manifest.json")"
REVISION="$("$PYTHON_BIN" -c \
  'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["revision"])' \
  "$PACKAGE_DIRECTORY/manifest.json")"

[[ "$MODEL" == "icon-ch1" ]] || fail "refusing model '$MODEL'"
[[ "$RUN" =~ ^[0-9]{8}_[0-9]{4}$ ]] || fail "unsafe run identity"
[[ "$LEVEL" == "800m_AGL" ]] || fail "refusing level '$LEVEL'"
[[ "$REVISION" =~ ^[0-9a-f]{16}$ ]] || fail "unsafe revision identity"

KEY_FILE=""
cleanup_key=false
if [[ -n "${INFOMANIAK_SSH_KEY_PATH:-}" ]]; then
  KEY_FILE="$INFOMANIAK_SSH_KEY_PATH"
elif [[ -n "${INFOMANIAK_SSH_KEY:-}" ]]; then
  KEY_FILE="${RUNNER_TEMP:-/tmp}/infomaniak_wind_streamline_beta2_key"
  printf '%s\n' "$INFOMANIAK_SSH_KEY" > "$KEY_FILE"
  chmod 600 "$KEY_FILE"
  cleanup_key=true
else
  fail "set INFOMANIAK_SSH_KEY or INFOMANIAK_SSH_KEY_PATH"
fi
[[ -f "$KEY_FILE" ]] || fail "SSH key file not found"

SSH_TARGET="${INFOMANIAK_USER}@${INFOMANIAK_HOST}"
SSH_OPTS=(
  -i "$KEY_FILE"
  -p "$INFOMANIAK_PORT"
  -o BatchMode=yes
  -o StrictHostKeyChecking=accept-new
)
RSYNC_SSH="ssh -i $KEY_FILE -p $INFOMANIAK_PORT -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
RELEASE_ID="$(date -u +'%Y%m%dT%H%M%SZ')-$$"
REMOTE_TARGET="$EXPECTED_REMOTE_ROOT/$MODEL/$RUN/$REVISION/$LEVEL"
REMOTE_TMP="$EXPECTED_REMOTE_ROOT/.upload-$RELEASE_ID"
REMOTE_LOCK="$EXPECTED_REMOTE_ROOT/.publish.lock"
LOCK_ID="wind-streamline-beta2-$RELEASE_ID"
MANIFEST_SHA256="$(sha256sum "$PACKAGE_DIRECTORY/manifest.json" | awk '{print $1}')"
lock_acquired=false
published=false

release_lock() {
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

cleanup() {
  if [[ "$published" != "true" ]]; then
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "rm -rf '$REMOTE_TMP'" >/dev/null 2>&1 || true
  fi
  release_lock
  if [[ "$cleanup_key" == "true" ]]; then
    rm -f "$KEY_FILE"
  fi
}
trap cleanup EXIT

log "Preparing the isolated beta2 artifact root"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
  set -e
  mkdir -p '$EXPECTED_REMOTE_ROOT'
  test ! -L '$EXPECTED_REMOTE_ROOT'
  if ! mkdir '$REMOTE_LOCK' 2>/dev/null; then
    echo 'beta2 XWS2 publish lock is already held' >&2
    exit 42
  fi
  printf '%s\n' '$LOCK_ID' > '$REMOTE_LOCK/owner'
"
lock_acquired=true

if ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "test -d '$REMOTE_TARGET'"; then
  remote_sha256="$(ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
    "sha256sum '$REMOTE_TARGET/manifest.json' | awk '{print \$1}'")"
  if [[ "$remote_sha256" != "$MANIFEST_SHA256" ]]; then
    fail "immutable target collision at $REMOTE_TARGET"
  fi
  log "The identical immutable package is already present"
else
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
    set -e
    rm -rf '$REMOTE_TMP'
    mkdir -p '$REMOTE_TMP'
  "
  log "Uploading 34-step package to the isolated temporary path"
  rsync -az --stats -e "$RSYNC_SSH" \
    "$PACKAGE_DIRECTORY/" "$SSH_TARGET:$REMOTE_TMP/"
  remote_sha256="$(ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
    "sha256sum '$REMOTE_TMP/manifest.json' | awk '{print \$1}'")"
  [[ "$remote_sha256" == "$MANIFEST_SHA256" ]] || \
    fail "uploaded manifest SHA-256 does not match"
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
    set -e
    mkdir -p '$(dirname "$REMOTE_TARGET")'
    test ! -e '$REMOTE_TARGET'
    mv '$REMOTE_TMP' '$REMOTE_TARGET'
  "
fi

log "Setting public read and traversal permissions on the immutable package"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
  set -e
  find '$REMOTE_TARGET' -type d -exec chmod 755 {} +
  find '$REMOTE_TARGET' -type f -exec chmod 644 {} +
"

log "Installing isolated delivery headers"
rsync -az -e "$RSYNC_SSH" \
  "$HTACCESS" "$SSH_TARGET:$EXPECTED_REMOTE_ROOT/.htaccess"

PUBLIC_MANIFEST="$EXPECTED_BASE_URL/$MODEL/$RUN/$REVISION/$LEVEL/manifest.json"
published_manifest="$(mktemp)"
published_tile="$(mktemp)"
published_headers="$(mktemp)"
cleanup_public_files() {
  rm -f "$published_manifest" "$published_tile" "$published_headers"
}
trap 'cleanup_public_files; cleanup' EXIT

log "Validating public manifest and a representative immutable tile"
curl -fsS "$PUBLIC_MANIFEST" -o "$published_manifest"
[[ "$(sha256sum "$published_manifest" | awk '{print $1}')" == "$MANIFEST_SHA256" ]] || \
  fail "public manifest SHA-256 does not match"
readarray -t tile_identity < <(
  "$PYTHON_BIN" -c '
import json,sys
manifest=json.load(open(sys.argv[1], encoding="utf-8"))
record=manifest["steps"][0]["profiles"]["wide-default"]["tiles"][0]
print(record["path"])
print(record["sha256"])
' "$PACKAGE_DIRECTORY/manifest.json"
)
curl -fsS -D "$published_headers" \
  "${PUBLIC_MANIFEST%manifest.json}${tile_identity[0]}" \
  -o "$published_tile"
[[ "$(sha256sum "$published_tile" | awk '{print $1}')" == "${tile_identity[1]}" ]] || \
  fail "public representative tile SHA-256 does not match"
grep -qi '^access-control-allow-origin: \*' "$published_headers" || \
  fail "public tile is missing the required CORS header"
grep -qi '^content-type: application/octet-stream' "$published_headers" || \
  fail "public tile has the wrong MIME type"
grep -qi '^cache-control: .*immutable' "$published_headers" || \
  fail "public tile is missing immutable caching"

published=true
release_lock
log "Published $PUBLIC_MANIFEST"
