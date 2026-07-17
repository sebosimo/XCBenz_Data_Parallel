# Coding Server Direct Pipeline

The Coding Server pipeline writes browser-ready artifacts directly and does not
generate or publish NetCDF intermediates.

## Current Production Status

- The Coding Server runs the `main` checkout. This document deliberately does
  not pin a deployed commit because that value becomes stale after every
  update. Verify the live revision on the server when needed.
- `xcbenz-coding-server-forecast.timer` is enabled and active.
- The staging timer is disabled.
- A manual production publish passed local and remote validation on 2026-07-13.
- The Coding Server is the primary publisher to
  `https://data.xcbenz.com/web_exports/`.
- The weather server watchdog checks production twice per hour and dispatches
  GitHub Actions only after two consecutive stale observations.
- GitHub Actions retains its independent six-hour schedule as a last resort.

Coding Server checkout and live revision check:

```bash
git -C /home/sebas/projects/XCBenz_Data_Parallel status --short --branch
git -C /home/sebas/projects/XCBenz_Data_Parallel rev-parse --short HEAD
```

Installed user systemd units:

```text
~/.config/systemd/user/xcbenz-coding-server-forecast.service
~/.config/systemd/user/xcbenz-coding-server-forecast.timer
```

The timer polls every 5 minutes. It publishes atomically to
`sites/data.xcbenz.com/web_exports` on Infomaniak and does not push `data-web`.

Runtime files on the Coding Server:

```text
/home/sebas/projects/XCBenz_Data_Parallel/.local_pipeline/coding-server-production.env
/home/sebas/projects/XCBenz_Data_Parallel/.local_pipeline/production_poller_state.json
/home/sebas/projects/XCBenz_Data_Parallel/.local_pipeline/runs/<timestamp>/logs/
```

Never reuse `staging_poller_state.json` for production because state paths are
part of the publisher isolation boundary.

## Server Polling

Production scheduling on the coding server should call `scripts/poll_coding_server_pipeline.py`, not the heavy runner directly. The poller is cheap enough for a 5 minute timer:

1. Probe MeteoSwiss STAC for the latest complete CH1 and CH2 run pair.
2. Compare the pair with `.local_pipeline/poller_state.json`.
3. Exit immediately if the pair already succeeded, is incomplete, or is inside retry backoff after a failed attempt.
4. Start `scripts/run_coding_server_pipeline.py` with pinned run tags only when a new complete pair is available.
5. Mark `last_success` only after the direct pipeline and validation exit cleanly.

The systemd unit in `deploy/systemd/xcbenz-coding-server-forecast.service` runs the poller. The timer in `deploy/systemd/xcbenz-coding-server-forecast.timer` polls every 5 minutes. The poller and the heavy runner use separate nonblocking lock files so duplicate starts exit without overlap.

Manual dry-run poll:

```bash
uv run python scripts/poll_coding_server_pipeline.py \
  --plan-only \
  --skip-deploy \
  --no-push-data-branch
```

## Output Contract

The pipeline produces these local cache roots before export:

- `web_profile_chunks/{model}/{run}/{chunk}/{location}/chunk.json`
- `web_profile_chunks/{model}/{run}/{chunk}/{location}/profiles.bin`
- `cache_wind_maps/{model}/{run}/{level}/metadata.json`
- `cache_wind_maps/{model}/{run}/{level}/steps/*.bin`
- `cache_sunshine_maps/{model}/{run}/surface/metadata.json`
- `cache_sunshine_maps/{model}/{run}/surface/steps/*.bin`
- `cache_rain_maps/{model}/{run}/surface/metadata.json`
- `cache_rain_maps/{model}/{run}/surface/steps/*.bin`
- `cache_sunrain_maps/{model}/{run}/surface/metadata.json`
- `cache_sunrain_maps/{model}/{run}/surface/steps/*.bin`
- `cache_cloud_maps/{model}/{run}/{product}/metadata.json`
- `cache_cloud_maps/{model}/{run}/{product}/steps/*.bin`

`generate_combined_manifest.py` scans only these direct caches. `generate_web_exports.py` then copies or merges them into `web_exports/` while preserving the browser-facing data contract:

- emagrams: `bundle.json` plus `profiles.bin`
- wind maps: `metadata.json` plus `steps/*.bin`
- rain, Sun+Rain, cloud, and sunshine maps: existing split binary metadata plus step binaries

The GitHub fallback uses `cache_wind_packed` only as the isolated per-chunk
CH2 Wind staging directory under `map_chunk_outputs/`. The map-merge step
explicitly consumes that staging name and writes the canonical
`cache_wind_maps/` tree above. CH1 fallback restore, generation, inspection,
and upload use `cache_wind_maps/` directly. `cache_wind_packed/` is not a final
generation, manifest, retention, or publication root.

Fetcher policy differences are explicit until the shared fetch-engine work:

- Both CH1 and CH2 honor `XCBENZ_FETCH_TMP_DIR`. CH2 previously ignored it;
  that was a defect because isolated Coding Server jobs leaked temporary files
  into the checkout.
- CH2 retains its 90-second whole-download deadline and deletes a partial file
  after a failed attempt. This bounds larger CH2 asset downloads and is an
  intentional model policy for the current baseline.
- CH1 retains its request timeout without a separate whole-download deadline
  and its legacy retry cleanup behavior. WP1 characterization must lock both
  policies before WP2A decides whether they can safely converge.

## Retired Paths

The pipeline no longer supports hourly profile NetCDF files, packed profile
NetCDF files, packed wind NetCDF files, or any NetCDF-derived web export
fallback. GitHub Actions uses the same direct web-export contract when fallback
publication is required.

Do not reintroduce these cache roots as final generation or publication inputs:

- `cache_data/`
- `cache_data_ch2/`
- `cache_data_packed/`
- `cache_data_ch2_packed/`
- `cache_wind_packed/`

## Local Benchmark Command

Run the direct pipeline without deploy or data-branch push:

```bash
uv run python scripts/run_coding_server_pipeline.py \
  --skip-deploy \
  --no-push-data-branch \
  --ch1-run-tag 20260628_0300 \
  --ch2-run-tag 20260628_0000
```

Historical split-workflow trials recorded CH1 at about 16 minutes and CH2 at
about 23 minutes. A later successful full workflow with six download workers
completed in about 16 minutes end to end. These measurements are historical
context, not an operating budget: source availability, selected runs, and host
load can change the result. Current worker defaults and run-completeness rules
live in the pipeline code and configuration, while the production poller pins
complete CH1 and CH2 run tags before starting the heavy runner.

The publish stage runs:

1. `scripts/merge_map_chunks.py`
2. `scripts/apply_retention.py`
3. `generate_combined_manifest.py`
4. `generate_web_exports.py`
5. `scripts/apply_web_retention.py`
6. `scripts/validate_outputs.py`

Validation must pass and `web_exports/` must contain no `*.nc` files.

## Production Operations

Check timer and service status:

```bash
systemctl --user list-timers --all xcbenz-coding-server-forecast.timer
systemctl --user status xcbenz-coding-server-forecast.service --no-pager --lines=40
```

Follow the production service log:

```bash
journalctl --user -u xcbenz-coding-server-forecast.service -f
```

Inspect the latest poller state:

```bash
cat /home/sebas/projects/XCBenz_Data_Parallel/.local_pipeline/production_poller_state.json
```

Pause or resume production polling:

```bash
systemctl --user stop xcbenz-coding-server-forecast.timer
systemctl --user start xcbenz-coding-server-forecast.timer
```

## Production Publisher and Fallback

The production design has three independent layers:

1. The Coding Server polls MeteoSwiss every 5 minutes and is the primary
   publisher to `https://data.xcbenz.com/web_exports/`.
2. The weather server checks the live production manifest at minute `00` and
   `30`. It dispatches GitHub Actions only after the same profile-complete
   source cycle is stale twice consecutively, with a 90-minute same-cycle
   cooldown.
3. GitHub Actions retains its six-hour native schedule as a last resort and
   rechecks the live manifest before starting heavy jobs.

All publishers use the same remote lock and atomic directory swap. The deploy
script removes locks older than 30 minutes and, while holding the lock, compares
the candidate manifest with the live manifest. A candidate older in either
model is rejected before the swap.

Keep `XCBENZ_PUSH_DATA_BRANCH=false` on the Coding Server. GitHub remains the
only writer of `data-web`; Infomaniak is the production source of truth.

## Verified Cutover State

The production cutover is complete:

- GitHub live-manifest preflight and downgrade protection are merged.
- The Coding Server production timer is enabled and staging is disabled.
- The first manual production publish passed local and remote validation.
- The weather-server watchdog is deployed and reports current production
  without dispatching.
- The GitHub six-hour native schedule remains enabled.

## Rollback

1. Stop the Coding Server production timer.
2. Re-enable the prior staging timer if staging observation is still useful.
3. Manually dispatch GitHub Actions with
   `run_mode=standard-deploy-data-host`; its live preflight will publish only
   if production is stale.
4. If the active directory itself is damaged, restore
   `_previous_web_exports` on Infomaniak under the same remote lock, then run
   remote validation before reopening automated publishing.
