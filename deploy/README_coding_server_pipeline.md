# Coding Server Forecast Pipeline Prototype

This prototype moves forecast execution onto the Hetzner coding server while
keeping the public browser contract at `https://data.xcbenz.com/web_exports/`.

It does not remove the GitHub Actions workflow. Keep Actions available as the
fallback until the coding-server run has been benchmarked and observed.

## What Runs Locally

`scripts/run_coding_server_pipeline.py` runs the same broad job families as
`.github/workflows/daily_plot.yml`:

- preflight against the latest MeteoSwiss CH1 and CH2 runs
- CH1 map chunks, now parallelized locally
- CH1 direct profile chunks
- CH2 map chunks
- CH2 direct profile chunks
- map chunk merge
- retention, manifest generation, `web_exports` generation, and validation
- optional data-host deploy
- optional `data-web` backup branch push

The runner monitors `/proc/stat`, load average, and `/proc/meminfo`. It logs CPU,
load, available RAM, and active fetch workers, and delays starting new workers
when configured limits are exceeded.

## Local Smoke Plan

From the data repo checkout on the coding server:

```bash
uv run python scripts/run_coding_server_pipeline.py \
  --plan-only \
  --skip-deploy \
  --no-push-data-branch \
  --ch1-run-tag 20260624_0300 \
  --ch2-run-tag 20260624_0000
```

For a full local benchmark without publishing:

```bash
XCBENZ_RUN_MODE=force-refresh \
uv run python scripts/run_coding_server_pipeline.py \
  --skip-deploy \
  --no-push-data-branch
```

Logs are written under `.local_pipeline/runs/<timestamp>/logs/`.

`--job-layout combined` is an experimental speed mode for the local runner. It
uses one worker per horizon chunk to write both map outputs and direct profile
chunks from the same downloaded fields. For the pinned non-03Z test run below,
that reduces top-level fetch jobs from 12 to 6 while preserving the same
browser-facing output validation contract. Keep the default split layout
available until the combined layout has more production-like runs behind it.

## Benchmark Notes

### 2026-06-24 Max-Jobs 7 Dry Run

Command shape:

```bash
cd /home/sebas/projects/XCBenz_Data_Parallel
.venv/bin/python scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd .venv/bin/python \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260624T2040Z
```

Result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `2c18f89`
- server: `codex-cx43-nbg1`, 8 CPU, 15 GiB RAM
- run window: `2026-06-24T20:40:19Z` to `2026-06-24T20:49:32Z`
- total runtime: about 9m13s
- fetch phase runtime: about 6m32s, from first fetch start to last fetch done
- selected runs: CH1 `20260624_1800`, CH2 `20260624_1200`
- top-level fetch jobs planned: 12
- top-level fetch jobs active at peak: 7
- deploy: skipped
- `data-web` push: skipped
- validation: passed

Validation summary:

```text
profiles=90160
bundles=1120
region_forecasts=1120
wind_steps=3864
sunshine_steps=636
rain_steps=644
sunrain_steps=636
cloud_steps=2576
```

Output sizes:

```text
web_exports:        1.2G
map_chunks:         332M
web_profile_chunks: 78M
run logs:           2.3M
```

Resource observations:

- sampled CPU peaked around 84%
- sampled load1 peaked at 7.71
- sampled available RAM stayed above the configured 4096 MB floor
- lowest sampled available RAM was about 4931 MB
- public SSH timed out once during peak load, then recovered

Interpretation:

`XCBENZ_LOCAL_MAX_JOBS=7` is viable for throughput, but it is aggressive for an
interactive coding server. Use `4` or `5` as the safer starting point if Codex
or SSH responsiveness matters while the pipeline is running.

### 2026-06-24 Max-Jobs 4 Dry Run

Command shape:

```bash
cd /home/sebas/projects/XCBenz_Data_Parallel_max4_pinned_20260624T2104Z
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --max-jobs 4 \
  --ch1-run-tag 20260624_1800 \
  --ch2-run-tag 20260624_1200 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260624T2104Z-max4-pinned
```

Result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `2c18f89`
- server: `codex-cx43-nbg1`, 8 CPU, 15 GiB RAM
- run window: `2026-06-24T21:04:27Z` to `2026-06-24T21:14:28Z`
- total runtime: about 10m01s
- fetch phase runtime: about 8m20s, from first fetch start to last fetch done
- selected runs: CH1 `20260624_1800`, CH2 `20260624_1200`
- top-level fetch jobs planned: 12
- top-level fetch jobs active at peak: 4
- deploy: skipped
- `data-web` push: skipped
- validation: passed

Validation summary matched the max-jobs 7 dry run:

```text
profiles=90160
bundles=1120
region_forecasts=1120
wind_steps=3864
sunshine_steps=636
rain_steps=644
sunrain_steps=636
cloud_steps=2576
```

Output sizes also matched the max-jobs 7 dry run:

```text
web_exports:        1.2G
map_chunks:         332M
web_profile_chunks: 78M
run logs:           2.5M
```

Resource observations:

- sampled CPU peaked around 56%
- sampled load1 peaked at 3.79
- sampled available RAM stayed above the configured 4096 MB floor
- lowest sampled available RAM was about 5909 MB
- no SSH timeout was observed during this run

Interpretation:

`XCBENZ_LOCAL_MAX_JOBS=4` keeps the coding server much more responsive while
still running the fetch phase in parallel. Compared with max-jobs 7, the fetch
phase took about 1m48s longer, but sampled peak CPU, load, and RAM pressure were
all materially lower. Total runtime was only about 48s longer in this pair of
tests, but fetch phase runtime is the cleaner comparison because post-fetch
network/cache timing differed between runs.

### 2026-06-25 Combined Layout Dry Runs

These tests used the same pinned source runs as the 2026-06-24 comparison:
CH1 `20260624_1800` and CH2 `20260624_1200`. Deploy and `data-web` push were
skipped. Because the restored `data-web` branch had advanced by 2026-06-25, the
final retained `web_exports` counts were lower than the 2026-06-24 dry runs; use
fetch phase timing and resource samples for scheduler comparison, not retained
snapshot counts across dates.

Max-jobs 4 command shape:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --job-layout combined \
  --max-jobs 4 \
  --ch1-run-tag 20260624_1800 \
  --ch2-run-tag 20260624_1200 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260625T0000Z-combined-max4
```

Max-jobs 4 result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `7608647`
- fetch jobs planned: 6
- fetch jobs active at peak: 4
- run window: `2026-06-25T06:17:52Z` to `2026-06-25T06:26:01Z`
- total runtime: about 8m09s
- fetch phase runtime: about 6m22s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 59%
- sampled load1 peaked at 4.20
- lowest sampled available RAM was about 6640 MB
- no SSH timeout was observed

Max-jobs 5 changed only `--max-jobs` and used run dir
`.local_pipeline/runs/manual-coding-server-test-20260625T0635Z-combined-max5`.

Max-jobs 5 result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `7608647`
- fetch jobs planned: 6
- fetch jobs active at peak: 5
- run window: `2026-06-25T06:27:26Z` to `2026-06-25T06:34:47Z`
- total runtime: about 7m21s
- fetch phase runtime: about 5m58s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 68%
- sampled load1 peaked at 4.98
- lowest sampled available RAM was about 6063 MB
- no SSH timeout was observed

Validation summary for both combined runs:

```text
profiles=68460
bundles=840
region_forecasts=840
wind_steps=2934
sunshine_steps=483
rain_steps=489
sunrain_steps=483
cloud_steps=1956
```

Output sizes for both combined runs:

```text
web_exports:        843M
map_chunks:         332M
web_profile_chunks: 78M
run logs:           1.8M
```

Interpretation:

Combined max-jobs 4 is the best measured balance so far: it is about 1m58s
faster in fetch phase than split max-jobs 4 and slightly faster than split
max-jobs 7, while using much less CPU/load than split max-jobs 7. Combined
max-jobs 5 is faster again, but only by about 24s of fetch time and with a
sampled CPU peak near 68%, so it is less aligned with an interactive coding
server target. Neither combined run reached the sub-5-minute fetch target; the
next optimization target is reducing per-horizon CH1 processing and decode work
inside each combined worker.

## Server Setup

Assuming the repo is checked out at `/opt/xcbenz/XCBenz_Data_Parallel` and the
service user is `xcbenz`:

```bash
sudo install -d -m 0750 -o root -g xcbenz /etc/xcbenz
sudo install -m 0600 -o root -g xcbenz deploy/coding-server-pipeline.env.example /etc/xcbenz/coding-server-forecast.env
sudo editor /etc/xcbenz/coding-server-forecast.env

sudo install -m 0644 deploy/systemd/xcbenz-coding-server-forecast.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/xcbenz-coding-server-forecast.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Run a manual service test before enabling the timer:

```bash
sudo systemctl start xcbenz-coding-server-forecast.service
sudo journalctl -u xcbenz-coding-server-forecast.service -n 100 --no-pager
```

Enable the timer only after the manual run is acceptable:

```bash
sudo systemctl enable --now xcbenz-coding-server-forecast.timer
```

When this timer becomes the primary publisher, disable the old GitHub-dispatch
timer so both schedulers do not compete:

```bash
sudo systemctl disable --now xcbenz-github-actions-trigger.timer
```
