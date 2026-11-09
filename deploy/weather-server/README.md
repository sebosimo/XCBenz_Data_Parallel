# Weather Server Forecast Runtime

Verified 2026-11-07: this runtime is the primary forecast publisher, executed
by `Hetzner_Server/backend-coordinator`. The coding-server forecast timer is
inactive. GitHub Actions and the weather watchdog remain independent recovery
layers. Historical `coding_server` script names do not describe current host
ownership.

## Finding the code for a change

The weather coordinator decides when to run a forecast. This repository owns
the forecast calculation, retained browser exports, validation, and upload.

| Change | Entry point |
| --- | --- |
| Source readiness and complete run selection | `scripts/poll_coding_server_pipeline.py`, `pipeline_orchestration/forecast_completeness.py` |
| Model horizons and work partitioning | `pipeline_orchestration/job_plan.py` |
| Local concurrency, resources, and execution order | `scripts/run_coding_server_pipeline.py` |
| Validated model reuse | `pipeline_orchestration/model_reuse.py` |
| Container execution and the global heavy-work lock | `deploy/weather-server/run_forecast.sh` |
| Scheduling, durable queue state, archive ordering | `Hetzner_Server/backend-coordinator/`, in the separate deployment repository |

In the runner, `build_jobs` creates the fetch plan, `run_pipeline` applies
reuse and schedules work, and `serial_publish_steps` exports, retains, and
validates the combined result. `deploy_outputs` runs only after those checks.
Keep changes to scientific calculations separate from orchestration changes.
An unchanged run tag alone is insufficient evidence for reuse; the receipt
must also match generation inputs and the complete retained output inventory.

## Validated model reuse

The runner logs `model reuse miss` with a model, requested run, and reason.
`different_model_run` is expected when fresh source data arrives.
`different_generation_inputs` means source, dependencies, static GRIB data,
settings, or scientific environment changed. Missing, invalid, or changed
output inventories require regeneration. Logs omit receipt contents, private
paths, and environment values. A code update invalidates existing receipts,
including an update that only adds diagnostics.

The runner reuses retained model exports when the requested run and generation
identity match a private receipt and every model-owned output passes SHA-256
verification. Missing, changed, extra, or symlinked files cause regeneration.
Each model has its own identity. Generator source, locations, Python dependencies,
product environment settings, and job configuration remain shared inputs. The
four default horizontal/vertical `static_data/*_constants_icon-{ch1,ch2}-eps.grib2`
files belong only to their respective model. Unknown GRIB files and alternate
static directories remain shared inputs. Reference files are hashed in full,
including their run metadata. A CH1 reference refresh therefore preserves CH2
eligibility without weakening checks on CH2's own inputs.

The runner logs `model reuse identity` with component digests for source, runtime,
policy, settings and static inputs. Compare those hashes across runs to explain
`different_generation_inputs`; private values are never logged. Fingerprint
version 2 invalidates the former combined fingerprint, so the first successful
run after this update seeds fresh receipts. Receipt files keep their existing
schema and size limits.

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
refresh. The 2026-11-07 canary took 22.0 minutes cold and 6.8 minutes with both
models reused, excluding publication. All 148819 model files remained identical.
A separate partial-export replay regenerated CH1 from captured inputs, retained
CH2 unchanged, and passed combined-output validation. That replay measures
correctness; production timing for a newly fetched model still needs observation.

The server audit document records deployed image pins, test results and measured
production behavior. The model-specific fingerprint update can be built with
`deploy/weather-server/Dockerfile.reuse` on the preceding verified production
image, preserving its dependencies. Push the source revision before deployment.
Change only `XCBENZ_FORECAST_IMAGE` in the weather Compose environment, verify the
effective configuration differs only in that image, and preserve both execution
gates. The coordinator reads the pin for each new job and needs no restart.
For rollback, restore the preceding image pin, `xcbenz-forecast:audit-2e9e042288`.
Its combined fingerprints safely reject version-2 identities and regenerate.

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
