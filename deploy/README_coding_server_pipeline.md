# Coding Server Direct Pipeline

The coding-server pipeline branch writes browser-ready artifacts directly and does not generate or publish NetCDF intermediates. During the current rollout, the production GitHub Actions workflow remains unchanged.

## Current Staging Status

As of 2026-06-29, Hetzner is running the direct pipeline as a staging publisher only. Production GitHub Actions and production `https://data.xcbenz.com/web_exports/` remain unchanged.

Staging target:

```text
https://data.xcbenz.com/server-staging/web_exports/
```

Hetzner checkout:

```text
/home/sebas/projects/XCBenz_Data_Parallel
branch: codex/coding-server-pipeline-prototype
validated commit: 3e622002
```

Installed user systemd units:

```text
~/.config/systemd/user/xcbenz-coding-server-staging.service
~/.config/systemd/user/xcbenz-coding-server-staging.timer
```

The timer is enabled and polls every 5 minutes. It publishes only to `sites/data.xcbenz.com/server-staging` on Infomaniak, does not push `data-web`, and does not overwrite production `sites/data.xcbenz.com/web_exports`.

Runtime files on Hetzner:

```text
/home/sebas/projects/XCBenz_Data_Parallel/.local_pipeline/coding-server-staging.env
/home/sebas/projects/XCBenz_Data_Parallel/.local_pipeline/staging_poller_state.json
/home/sebas/logs/xcbenz-coding-server-staging.log
/home/sebas/projects/XCBenz_Data_Parallel/.local_pipeline/runs/<timestamp>/logs/
```

First successful staging publish:

```text
completed_at: 2026-06-29T18:26:57Z
CH1: 20260629_1500
CH2: 20260629_1200
local validation: passed
remote validation: passed
web_exports .nc count: 0
```

A same-run follow-up poll exited cheaply with `latest pair already succeeded`, confirming the idle path does not start the heavy pipeline.

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

## Retired Paths

The pipeline no longer supports hourly profile NetCDF files, packed profile NetCDF files, packed wind NetCDF files, or any NetCDF-derived web export fallback. The production `daily_plot.yml` workflow is intentionally left untouched during this server-runner rollout.

Do not reintroduce these cache roots as generation or publication inputs:

- `cache_data/`
- `cache_data_ch2/`
- `cache_data_packed/`
- `cache_data_ch2_packed/`
- `cache_wind_packed/`

## Local Benchmark Command

Run the direct pipeline without deploy or data-branch push:

```bash
uv run python scripts/run_coding_server_pipeline.py   --skip-deploy   --no-push-data-branch   --ch1-run-tag 20260628_0300   --ch2-run-tag 20260628_0000
```

The publish stage runs:

1. `scripts/merge_map_chunks.py`
2. `scripts/apply_retention.py`
3. `generate_combined_manifest.py`
4. `generate_web_exports.py`
5. `scripts/apply_web_retention.py`
6. `scripts/validate_outputs.py`

Validation must pass and `web_exports/` must contain no `*.nc` files.

## Staging Operations

Check timer and service status:

```bash
systemctl --user list-timers --all xcbenz-coding-server-staging.timer
systemctl --user status xcbenz-coding-server-staging.service --no-pager --lines=40
```

Follow the main staging log:

```bash
tail -f /home/sebas/logs/xcbenz-coding-server-staging.log
```

Inspect the latest poller state:

```bash
cat /home/sebas/projects/XCBenz_Data_Parallel/.local_pipeline/staging_poller_state.json
```

Compare production and staging manifests:

```bash
python - <<'PY'
import json, urllib.request
for name, url in {
    "production": "https://data.xcbenz.com/web_exports/manifest.json",
    "staging": "https://data.xcbenz.com/server-staging/web_exports/manifest.json",
}.items():
    with urllib.request.urlopen(url, timeout=30) as response:
        manifest = json.loads(response.read().decode("utf-8"))
    print(name, (manifest.get("source") or {}).get("data_root"))
    print(name, manifest.get("counts"))
    for model_key, model in sorted((manifest.get("models") or {}).items()):
        print(name, model_key, model.get("latest_run"), sorted((model.get("runs") or {}).keys()))
PY
```

Pause or resume staging polling:

```bash
systemctl --user stop xcbenz-coding-server-staging.timer
systemctl --user start xcbenz-coding-server-staging.timer
```

## Production Cutover Checklist

1. Let staging run through several complete forecast cycles.
2. Confirm repeated idle polls exit cheaply when the latest pair already succeeded.
3. Confirm local and remote validation pass for each staging publish.
4. Compare latest production and staging CH1/CH2 run tags plus high-level manifest counts.
5. Decide whether GitHub Actions remains manual fallback or still scheduled during cutover.
6. Change the production publisher root from `sites/data.xcbenz.com/server-staging` to `sites/data.xcbenz.com` only after the staging cycle is stable.
7. Disable or retire the GitHub Actions scheduled production writer once Hetzner is the primary writer.
