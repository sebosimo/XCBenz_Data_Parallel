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
