# Weather Server Forecast Runtime

This image lets the weather server's durable backend coordinator probe source
readiness and run one pinned forecast publication under the shared heavy-work
lock.

The image contains no credentials. Production configuration is provided by the
weather server through a mode-0600 env file and a read-only deploy-key mount.
The direct publisher must keep `XCBENZ_PUSH_DATA_BRANCH=false`; GitHub Actions
remains the only writer of `data-web`.

The example environment retains the measured four-job configuration: combined
workers, interleaved CH1/CH2 ordering, CH2 chunks of 15 horizons, next-horizon
prefetch, early release of profile-only fields, and eight download workers.
The wrapper pins both Python command variables to the container's `python`
executable so host-only interpreter paths cannot leak into deployment checks.

Probe source readiness without state changes:

```bash
python scripts/poll_coding_server_pipeline.py --probe-only-json
```

Run a pinned skip-deploy canary while taking the global heavy lock:

```bash
/app/deploy/weather-server/run_forecast.sh \
  --skip-deploy \
  --no-push-data-branch \
  --no-restore-web-exports \
  --ch1-run-tag 20260728_1500 \
  --ch2-run-tag 20260728_1200
```

The coordinator sets `XCBENZ_HEAVY_LOCK_HELD=1` because it already holds the
same lock for the full managed-container lifetime. Manual invocations omit that
variable and the wrapper acquires `/run/lock/xcbenz-heavy.lock` itself.
