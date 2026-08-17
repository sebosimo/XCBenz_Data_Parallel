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
    else
      rc=$?
    fi
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
PYTHON_BIN="${PYTHON_BIN:-python3}"

command -v "$PYTHON_BIN" >/dev/null 2>&1 \
  || fail "Python executable not found: $PYTHON_BIN"
command -v timeout >/dev/null 2>&1 || fail "timeout executable not found"

[[ -f "$WEB_EXPORT_DIR/manifest.json" ]] || fail "missing $WEB_EXPORT_DIR/manifest.json"

nc_count="$(find "$WEB_EXPORT_DIR" -name '*.nc' -type f | wc -l | tr -d ' ')"
if [[ "$nc_count" != "0" ]]; then
  fail "$WEB_EXPORT_DIR contains $nc_count NetCDF file(s)"
fi

KEY_FILE=""
cleanup_key=false
lock_acquired=false
lease_heartbeat_pid=""
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
  -o ConnectTimeout=10
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=3
)
RSYNC_SSH="ssh -i $KEY_FILE -p $INFOMANIAK_PORT -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"

REMOTE_ROOT="${INFOMANIAK_DATA_ROOT%/}"
REMOTE_TMP="$REMOTE_ROOT/_upload_tmp_$RELEASE_ID"
REMOTE_CURRENT="$REMOTE_ROOT/web_exports"
REMOTE_PREVIOUS="$REMOTE_ROOT/_previous_web_exports"
REMOTE_LOCK="$REMOTE_ROOT/.xcbenz_web_exports_publish.lock"
REMOTE_LOCK_GUARD="$REMOTE_LOCK.guard"
CURRENT_MANIFEST_DIR="${RUNNER_TEMP:-/tmp}/xcbenz_current_manifests_${RELEASE_ID}"
LOCK_ID="${LOCK_ID:-forecast-$RELEASE_ID-$$}"
DEPLOY_LOCK_WAIT_SECONDS="${DEPLOY_LOCK_WAIT_SECONDS:-300}"
DEPLOY_LOCK_POLL_SECONDS="${DEPLOY_LOCK_POLL_SECONDS:-5}"
DEPLOY_LOCK_LEASE_SECONDS="${DEPLOY_LOCK_LEASE_SECONDS:-900}"
DEPLOY_LOCK_HEARTBEAT_SECONDS="${DEPLOY_LOCK_HEARTBEAT_SECONDS:-30}"
DEPLOY_LOCK_RECOVERY_GRACE_SECONDS="${DEPLOY_LOCK_RECOVERY_GRACE_SECONDS:-60}"
DEPLOY_LOCK_RELEASE_TIMEOUT_SECONDS="${DEPLOY_LOCK_RELEASE_TIMEOUT_SECONDS:-30}"
DEPLOY_LOCK_RECOVERY_ENABLED="${DEPLOY_LOCK_RECOVERY_ENABLED:-true}"
# Keep abandoned candidates for no more than four days in normal operation:
# six hours before quarantine plus 90 hours in quarantine. The initial grace
# exceeds the expected duration of an active fallback workflow.
DEPLOY_CANDIDATE_QUARANTINE_AFTER_SECONDS="${DEPLOY_CANDIDATE_QUARANTINE_AFTER_SECONDS:-21600}"
DEPLOY_CANDIDATE_DELETE_AFTER_SECONDS="${DEPLOY_CANDIDATE_DELETE_AFTER_SECONDS:-324000}"
DEPLOY_LOCK_PROTOCOL_VERSION=1
REMOTE_QUARANTINE_ROOT="$REMOTE_ROOT/.xcbenz_web_exports_publish.quarantine"
REMOTE_CANDIDATE_QUARANTINE_ROOT="$REMOTE_ROOT/.xcbenz_upload_candidate.quarantine"

for numeric_setting in \
  "$DEPLOY_LOCK_WAIT_SECONDS" \
  "$DEPLOY_LOCK_POLL_SECONDS" \
  "$DEPLOY_LOCK_LEASE_SECONDS" \
  "$DEPLOY_LOCK_HEARTBEAT_SECONDS" \
  "$DEPLOY_LOCK_RECOVERY_GRACE_SECONDS" \
  "$DEPLOY_LOCK_RELEASE_TIMEOUT_SECONDS" \
  "$DEPLOY_CANDIDATE_QUARANTINE_AFTER_SECONDS" \
  "$DEPLOY_CANDIDATE_DELETE_AFTER_SECONDS"; do
  [[ "$numeric_setting" =~ ^[0-9]+$ ]] || fail "publish lock timings must be integers"
  (( numeric_setting > 0 )) || fail "publish lock timings must be positive"
done
if (( DEPLOY_LOCK_HEARTBEAT_SECONDS * 2 >= DEPLOY_LOCK_LEASE_SECONDS )); then
  fail "publish lock lease must exceed two heartbeat intervals"
fi
if (( DEPLOY_LOCK_RELEASE_TIMEOUT_SECONDS >= DEPLOY_LOCK_LEASE_SECONDS )); then
  fail "publish lock release timeout must be shorter than the lease"
fi
if (( DEPLOY_CANDIDATE_DELETE_AFTER_SECONDS <= DEPLOY_CANDIDATE_QUARANTINE_AFTER_SECONDS )); then
  fail "candidate delete retention must exceed quarantine retention"
fi

acquire_remote_lock() {
  log "Acquiring remote publish lock $REMOTE_LOCK"
  retry "acquire remote publish lock" \
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
    set -eu
    command -v flock >/dev/null 2>&1 || { echo 'Remote host requires flock for publish fencing' >&2; exit 57; }
    mkdir -p '$REMOTE_ROOT'
    exec 9>'$REMOTE_LOCK_GUARD'
    acquired_here=0
    metadata_ready=0
    cleanup_partial_lease() {
      if [ \"\$acquired_here\" -eq 1 ] && [ \"\$metadata_ready\" -eq 0 ]; then
        partial_owner=\$(cat '$REMOTE_LOCK/owner' 2>/dev/null || true)
        if [ -z \"\$partial_owner\" ] || [ \"\$partial_owner\" = '$LOCK_ID' ]; then
          rm -rf '$REMOTE_LOCK'
        fi
      fi
    }
    trap cleanup_partial_lease 0
    trap 'cleanup_partial_lease; exit 51' HUP INT TERM
    deadline=\$((\$(date +%s) + $DEPLOY_LOCK_WAIT_SECONDS))
    while true; do
      now=\$(date +%s)
      flock -w '$DEPLOY_LOCK_RELEASE_TIMEOUT_SECONDS' 9 || { echo 'Timed out waiting for publish mutation guard' >&2; exit 52; }
      if mkdir '$REMOTE_LOCK' 2>/dev/null; then
        acquired_here=1
        break
      fi

      protocol=\$(cat '$REMOTE_LOCK/protocol_version' 2>/dev/null || true)
      observed_owner=\$(cat '$REMOTE_LOCK/owner' 2>/dev/null || true)
      heartbeat=\$(cat '$REMOTE_LOCK/heartbeat_at' 2>/dev/null || true)
      lease=\$(cat '$REMOTE_LOCK/lease_seconds' 2>/dev/null || true)
      expired=0
      if [ '$DEPLOY_LOCK_RECOVERY_ENABLED' = 'true' ] && [ \"\$protocol\" = '$DEPLOY_LOCK_PROTOCOL_VERSION' ]; then
        case \"\$heartbeat\" in ''|*[!0-9]*) ;; *)
          case \"\$lease\" in ''|*[!0-9]*) ;; *)
            if [ \"\$now\" -gt \$((heartbeat + lease + $DEPLOY_LOCK_RECOVERY_GRACE_SECONDS)) ]; then
              expired=1
            fi
          esac
        esac
      fi

      if [ \"\$expired\" -eq 1 ]; then
        previous_owner=\$(cat '$REMOTE_LOCK/owner' 2>/dev/null || true)
        current_protocol=\$(cat '$REMOTE_LOCK/protocol_version' 2>/dev/null || true)
        current_heartbeat=\$(cat '$REMOTE_LOCK/heartbeat_at' 2>/dev/null || true)
        current_lease=\$(cat '$REMOTE_LOCK/lease_seconds' 2>/dev/null || true)
        current_now=\$(date +%s)
        still_expired=0
        if [ \"\$current_protocol\" = '$DEPLOY_LOCK_PROTOCOL_VERSION' ]; then
          case \"\$current_heartbeat\" in ''|*[!0-9]*) ;; *)
            case \"\$current_lease\" in ''|*[!0-9]*) ;; *)
              if [ \"\$previous_owner\" = \"\$observed_owner\" ] && [ \"\$current_now\" -gt \$((current_heartbeat + current_lease + $DEPLOY_LOCK_RECOVERY_GRACE_SECONDS)) ]; then
                still_expired=1
              fi
            esac
          esac
        fi
        if [ \"\$still_expired\" -eq 1 ]; then
          mkdir -p '$REMOTE_QUARANTINE_ROOT'
          quarantine_path='$REMOTE_QUARANTINE_ROOT'/\$(date -u +%Y%m%dT%H%M%SZ)-\$\$
          if mv '$REMOTE_LOCK' \"\$quarantine_path\" 2>/dev/null; then
            echo \"Quarantined expired publish lease owner=\$previous_owner path=\$quarantine_path\" >&2
          fi
        fi
        flock -u 9
        continue
      fi

      if [ \"\$now\" -ge \"\$deadline\" ]; then
        flock -u 9
        echo 'Timed out waiting for $REMOTE_LOCK' >&2
        exit 42
      fi
      flock -u 9
      sleep '$DEPLOY_LOCK_POLL_SECONDS'
    done
    printf '%s\n' '$LOCK_ID' > '$REMOTE_LOCK/owner'
    printf '%s\n' '$DEPLOY_LOCK_PROTOCOL_VERSION' > '$REMOTE_LOCK/protocol_version'
    printf '%s\n' 'forecast' > '$REMOTE_LOCK/publisher'
    printf '%s\n' '$(hostname)' > '$REMOTE_LOCK/host'
    printf '%s\n' '$$' > '$REMOTE_LOCK/pid'
    printf '%s\n' '$DEPLOY_LOCK_LEASE_SECONDS' > '$REMOTE_LOCK/lease_seconds'
    acquired_at=\$(date +%s)
    printf '%s\n' \"\$acquired_at\" > '$REMOTE_LOCK/acquired_at_epoch'
    date -u +%Y-%m-%dT%H:%M:%SZ > '$REMOTE_LOCK/acquired_at'
    printf '%s\n' \"\$acquired_at\" > '$REMOTE_LOCK/heartbeat_at'
    touch '$REMOTE_LOCK'
    metadata_ready=1
    flock -u 9
    trap - 0 HUP INT TERM
  "
  lock_acquired=true
  start_lease_heartbeat
}

refresh_remote_lease() {
  timeout --signal=TERM "${DEPLOY_LOCK_RELEASE_TIMEOUT_SECONDS}s" \
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
      set -eu
      command -v flock >/dev/null 2>&1 || { echo 'Remote host requires flock for publish fencing' >&2; exit 57; }
      exec 9>'$REMOTE_LOCK_GUARD'
      flock -w '$DEPLOY_LOCK_RELEASE_TIMEOUT_SECONDS' 9 || { echo 'Timed out waiting for publish mutation guard' >&2; exit 52; }
      cd '$REMOTE_LOCK' 2>/dev/null || { echo 'Remote publish lease ownership lost' >&2; exit 49; }
      actual_owner=\$(cat owner 2>/dev/null || true)
      protocol=\$(cat protocol_version 2>/dev/null || true)
      if [ \"\$actual_owner\" != '$LOCK_ID' ] || [ \"\$protocol\" != '$DEPLOY_LOCK_PROTOCOL_VERSION' ]; then
        echo 'Remote publish lease ownership lost' >&2
        exit 49
      fi
      now=\$(date +%s)
      printf '%s\n' \"\$now\" > heartbeat_at.next
      mv heartbeat_at.next heartbeat_at
      touch .
      flock -u 9
    "
}

lease_heartbeat_loop() {
  while sleep "$DEPLOY_LOCK_HEARTBEAT_SECONDS"; do
    if ! refresh_remote_lease; then
      log "remote publish lease heartbeat failed"
      return 1
    fi
  done
}

start_lease_heartbeat() {
  lease_heartbeat_loop &
  lease_heartbeat_pid=$!
}

stop_lease_heartbeat() {
  if [[ -n "$lease_heartbeat_pid" ]]; then
    kill "$lease_heartbeat_pid" >/dev/null 2>&1 || true
    wait "$lease_heartbeat_pid" >/dev/null 2>&1 || true
    lease_heartbeat_pid=""
  fi
}

assert_remote_lease() {
  if [[ -z "$lease_heartbeat_pid" ]] || ! kill -0 "$lease_heartbeat_pid" >/dev/null 2>&1; then
    fail "remote publish lease heartbeat is not running"
  fi
  retry "verify remote publish lease" refresh_remote_lease
}

maintain_remote_candidates() {
  retry "maintain abandoned remote upload candidates" \
    timeout --signal=TERM "${DEPLOY_LOCK_RELEASE_TIMEOUT_SECONDS}s" \
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
      set -eu
      actual_owner=\$(cat '$REMOTE_LOCK/owner' 2>/dev/null || true)
      protocol=\$(cat '$REMOTE_LOCK/protocol_version' 2>/dev/null || true)
      if [ \"\$actual_owner\" != '$LOCK_ID' ] || [ \"\$protocol\" != '$DEPLOY_LOCK_PROTOCOL_VERSION' ]; then
        echo 'Remote publish lease ownership lost before candidate maintenance' >&2
        exit 49
      fi
      now=\$(date +%s)
      mkdir -p '$REMOTE_CANDIDATE_QUARANTINE_ROOT'
      for candidate in '$REMOTE_ROOT'/_upload_tmp_*; do
        [ -d \"\$candidate\" ] || continue
        [ \"\$candidate\" != '$REMOTE_TMP' ] || continue
        candidate_mtime=\$(stat -c %Y \"\$candidate\" 2>/dev/null || echo 0)
        candidate_age=\$((now - candidate_mtime))
        if [ \"\$candidate_mtime\" -gt 0 ] && [ \"\$candidate_age\" -ge '$DEPLOY_CANDIDATE_QUARANTINE_AFTER_SECONDS' ]; then
          candidate_name=\${candidate##*/}
          quarantine_path='$REMOTE_CANDIDATE_QUARANTINE_ROOT'/\${candidate_name}-\${now}-\$\$
          if mv \"\$candidate\" \"\$quarantine_path\" 2>/dev/null; then
            touch \"\$quarantine_path\"
            echo \"Quarantined abandoned upload candidate path=\$quarantine_path\" >&2
          fi
        fi
      done
      for quarantined in '$REMOTE_CANDIDATE_QUARANTINE_ROOT'/*; do
        [ -d \"\$quarantined\" ] || continue
        quarantine_mtime=\$(stat -c %Y \"\$quarantined\" 2>/dev/null || echo 0)
        quarantine_age=\$((now - quarantine_mtime))
        if [ \"\$quarantine_mtime\" -gt 0 ] && [ \"\$quarantine_age\" -ge '$DEPLOY_CANDIDATE_DELETE_AFTER_SECONDS' ]; then
          rm -rf \"\$quarantined\"
        fi
      done
    "
}

check_publish_freshness() {
  if ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "test -f '$REMOTE_CURRENT/manifest.json'"; then
    rm -rf "$CURRENT_MANIFEST_DIR"
    mkdir -p "$CURRENT_MANIFEST_DIR"
    retry "download current production manifests" \
      rsync -az --delete --prune-empty-dirs -e "$RSYNC_SSH" \
        --include='/manifest.json' \
        --include='/wind_maps/' --include='/wind_maps/manifest.json' \
        --include='/sunshine_maps/' --include='/sunshine_maps/manifest.json' \
        --include='/rain_maps/' --include='/rain_maps/manifest.json' \
        --include='/sunrain_maps/' --include='/sunrain_maps/manifest.json' \
        --include='/cloud_maps/' --include='/cloud_maps/manifest.json' \
        --include='/value_tiles/' --include='/value_tiles/v1/' \
        --include='/value_tiles/v1/manifest.json' \
        --exclude='*' \
        "$SSH_TARGET:$REMOTE_CURRENT/" "$CURRENT_MANIFEST_DIR/"
    guard_args=(
      --candidate-root "$WEB_EXPORT_DIR"
      --current-root "$CURRENT_MANIFEST_DIR"
      --require-products wind,sunshine,rain,sunrain,cloud
    )
    if [[ "${ENABLE_VALUE_TILES:-false}" == "true" ]]; then
      guard_args+=(--require-value-tiles)
    fi
    "$PYTHON_BIN" scripts/guard_publish_freshness.py "${guard_args[@]}"
  else
    log "No current production manifest found; freshness guard has nothing to compare"
  fi
}

release_remote_lock() {
  if [[ "$lock_acquired" != "true" ]]; then
    return
  fi

  stop_lease_heartbeat
  retry "release remote publish lease" \
    timeout --signal=TERM "${DEPLOY_LOCK_RELEASE_TIMEOUT_SECONDS}s" \
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
      set -eu
      command -v flock >/dev/null 2>&1 || { echo 'Remote host requires flock for publish fencing' >&2; exit 57; }
      exec 9>'$REMOTE_LOCK_GUARD'
      flock -w '$DEPLOY_LOCK_RELEASE_TIMEOUT_SECONDS' 9 || { echo 'Timed out waiting for publish mutation guard' >&2; exit 52; }
      actual_owner=\$(cat '$REMOTE_LOCK/owner' 2>/dev/null || true)
      if [ -d '$REMOTE_LOCK' ] && [ \"\$actual_owner\" = '$LOCK_ID' ]; then
        rm -rf '$REMOTE_LOCK'
      fi
      flock -u 9
    "
  lock_acquired=false
}

cleanup_remote_upload_candidate() {
  retry "remove failed remote upload candidate" \
    timeout --signal=TERM "${DEPLOY_LOCK_RELEASE_TIMEOUT_SECONDS}s" \
    ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
      set -eu
      case '$REMOTE_TMP' in
        '$REMOTE_ROOT'/_upload_tmp_*) ;;
        *) echo 'Refusing to remove unexpected remote upload path' >&2; exit 58 ;;
      esac
      rm -rf -- '$REMOTE_TMP'
    "
}

cleanup() {
  local exit_code=$?
  stop_lease_heartbeat
  if [[ "$lock_acquired" == "true" ]]; then
    release_remote_lock || log "remote publish lease release failed during cleanup"
  fi
  if (( exit_code != 0 )); then
    cleanup_remote_upload_candidate \
      || log "failed remote upload candidate cleanup will be handled by retention maintenance"
  fi
  rm -rf "$CURRENT_MANIFEST_DIR"
  if [[ "$cleanup_key" == "true" ]]; then
    rm -f "$KEY_FILE"
  fi
  return "$exit_code"
}
trap cleanup EXIT

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

acquire_remote_lock
assert_remote_lease
maintain_remote_candidates
assert_remote_lease
check_publish_freshness

log "Preserving live-owned folders in forecast upload"
retry "preserve live-owned folders" \
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
  set -e
  current='$REMOTE_CURRENT'
  target='$REMOTE_TMP/web_exports'
  mkdir -p \"\$target\"
  for subtree in live_stations webcams radar_maps airspace fai_records satellite_cloud_maps; do
    if [ -d \"\$current/\$subtree\" ]; then
      rm -rf \"\$target/\$subtree\"
      cp -a \"\$current/\$subtree\" \"\$target/\$subtree\"
    fi
  done
"

log "Switching remote web_exports directory"
assert_remote_lease
retry "switch remote web_exports directory" \
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "
    set -e
    command -v flock >/dev/null 2>&1 || { echo 'Remote host requires flock for publish fencing' >&2; exit 57; }
    exec 9>'$REMOTE_LOCK_GUARD'
    flock -w '$DEPLOY_LOCK_RELEASE_TIMEOUT_SECONDS' 9 || { echo 'Timed out waiting for publish mutation guard' >&2; exit 52; }
    (
      cd '$REMOTE_LOCK' 2>/dev/null || { echo 'Remote publish lease ownership lost before commit' >&2; exit 49; }
      actual_owner=\$(cat owner 2>/dev/null || true)
      protocol=\$(cat protocol_version 2>/dev/null || true)
      if [ \"\$actual_owner\" != '$LOCK_ID' ] || [ \"\$protocol\" != '$DEPLOY_LOCK_PROTOCOL_VERSION' ]; then
        echo 'Remote publish lease ownership lost before commit' >&2
        exit 49
      fi
      now=\$(date +%s)
      printf '%s\n' \"\$now\" > heartbeat_at.next
      mv heartbeat_at.next heartbeat_at
      touch .
    )
    flock -u 9
    mkdir -p '$REMOTE_ROOT'
    rm -rf '$REMOTE_PREVIOUS'
    if [ -d '$REMOTE_CURRENT' ]; then
      mv '$REMOTE_CURRENT' '$REMOTE_PREVIOUS'
    fi
    if ! mv '$REMOTE_TMP/web_exports' '$REMOTE_CURRENT'; then
      # Rollback is atomic to the last complete public tree.
      if [ -d '$REMOTE_PREVIOUS' ]; then
        mv '$REMOTE_PREVIOUS' '$REMOTE_CURRENT'
      fi
      exit 44
    fi
  rmdir '$REMOTE_TMP' 2>/dev/null || true
  find '$REMOTE_CURRENT' -name '*.nc' -type f -print -quit | grep -q . && exit 20 || true
    find '$REMOTE_CURRENT' -maxdepth 2 -type f | wc -l
"
assert_remote_lease
release_remote_lock

log "Published $WEB_EXPORT_DIR to $DATA_HOST_BASE_URL/web_exports/"
