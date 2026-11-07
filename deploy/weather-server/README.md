# Weather Server Forecast Runtime

Verified 2026-11-07: this runtime is the primary forecast publisher, executed
by `Hetzner_Server/backend-coordinator`. The coding-server forecast timer is
inactive. GitHub Actions and the weather watchdog remain independent recovery
layers. Historical `coding_server` script names do not describe current host
ownership.

## Validated model reuse

The runner reuses retained model exports when the requested run and generation
identity match a private receipt and every model-owned output passes SHA-256
verification. Missing, changed, extra, or symlinked files cause regeneration.
The identity covers generator source, locations, Python dependencies, static
GRIB inputs, product environment settings, and job configuration.

Receipts are written only after combined-output validation. There are at most
two receipts, each limited to 32 MiB, outside `web_exports`; forecast data is
not copied into a second cache. The weather Compose service uses the existing
state volume at `/var/lib/xcbenz-forecast/model-reuse`. No receipt means a cold
run. Hydrated remote history alone is not trusted as a reusable computation.

Only changed models run fetch jobs and tile generation. Retention merges the
partial output with retained exports, rebuilds browser manifests, and validates
both models. Publication locks, remote validation, and downgrade guards are
unchanged. Receipts are checked again before publication to catch concurrent
changes to reused files.

Set `XCBENZ_MODEL_REUSE=false`, pass `--no-model-reuse`, or use force-refresh
mode to regenerate. Reuse is disabled with GitHub `data-web` restoration,
because that restoration occurs after fetching and could replace verified
files. Production already uses `--no-restore-web-exports`.

An upstream correction that retains the same model run tag requires a forced
refresh. No production speedup is claimed until a bounded canary measures cold
and reused runs. Deploy the reviewed forecast image with the matching weather
Compose state-path change; retain the previous image for rollback.

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

The forecast worker also produces a 0.08-degree wind-arrow sidecar at the ten
Vertical air AMSL altitudes, from 1,000 through 8,000 metres. It reuses decoded
U/V and HHL fields and a subset of the existing interpolation geometry. Signed
little-endian int16 U/V at 0.25 m/s avoids the ordinary int8 wind map's clipping
of strong upper-level components. Missing values remain -32768.

The sidecar is indexed at `web_exports/wind_maps/directions.json`, with metadata
and slices below each retained `wind_maps/MODEL/RUN/directions/ALTITUDE/` tree.
The version-1 wind manifest and metadata contracts are reused, with purpose
`vertical_air_arrows`; manifest indexes omit repeated steps, which remain in
per-altitude metadata. HTTP gzip reduces transfer size. Cache chunk merging,
web export, existing forecast leases, atomic promotion and run retention include
the sidecar. It adds no raw downloads and no independent publisher. Set
`REQUIRE_WIND_DIRECTIONS=true` in the weather runtime to reject incomplete current
model runs before upload and verify all ten levels' first/last slices over HTTPS
after promotion. GitHub's fallback enables the same guard.
