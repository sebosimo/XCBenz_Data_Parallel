# CH1 W feed prototype

This branch adds an independent static forecast feed for geometric vertical air
velocity. It does not change forecast scheduling, production publication, CH2,
the weather archive, or the website. There is no automatic polling service.

Run from the repository root using the existing forecast Python environment:

```sh
python -m w_forecast.runner \
  --run latest --start 0 --end 33 \
  --work-dir /data/xcbenz-w-prototype/work \
  --output /data/xcbenz-w-prototype/feed
```

Omit `--end` to use the complete model horizon. The existing CH1 policy allows
33 hours normally and 45 hours for 03Z. `--run YYYYMMDD_HHMM` pins a UTC cycle.
MeteoSwiss retains sources for about 24 hours after publication. An expired run
can still resume if all its requested frames already exist and static grids match.
Incomplete or failed runs never replace `latest.json`.

Defaults cover the existing common ICON-CH application domain, 0.8 to 16.4°E and
42.4 to 50°N, at 0.01° spacing. There are 1561 columns and 761 rows, without the
extra downsampling used by the existing wind web feed. Ten AMSL altitudes are
1000, 1500, 2000, 2500, 3000, 4000, 5000, 6000, 7000 and 8000 metres. Override
`--bbox WEST SOUTH EAST NORTH`, `--spacing` or `--altitudes` for smaller trials.

## Processing and storage

The worker downloads one control-member W file per forecast hour, about 186 MB.
It releases the GRIB file after exporting that hour. It reads one GRIB message
at a time and decodes values only for vertical levels required by the requested
altitudes. It never creates an 81-level W or HHL array.

Horizontal Delaunay weights and vertical HHL brackets are cached across jobs.
W uses the 81 half levels directly. It must not use the averaged 80 full-level
heights used for U/V. Interpolate vertically at native cells before horizontally
regridding. Below-terrain cells, out-of-domain cells and unavailable brackets are
missing. Horizontal interpolation does not extrapolate or bridge long triangles.
Each GRIB message must match the static grid UUIDs, control member, pinned
reference time, requested horizon, parameter and units.

Static files are refreshed by ETag and verified by SHA-256. A changed static-file
hash invalidates the prepared plan and frame reuse. Each step is deterministic,
gzip-compressed and content-addressed. Completed hours resume after checking
their object hashes, without downloading W again. The source-file hashes are in
the manifest. A private per-hour checkpoint also resumes an interrupted run,
without exposing partial coverage through discovery. Output is quantized to
0.01 m/s; that is encoding precision, not
physical model accuracy. Out-of-range values cause failure instead of clipping.

The worker checks the 10 GiB disk reserve before allocation and throughout
downloads. Files have a 512 MiB cap and downloads have a ten-minute deadline.
It holds exclusive work/output writer locks. Keep at least two source runs,
plus a six-hour grace period for browsers still reading old manifests. Cleanup
only removes this feed's unreachable, expired objects and manifests. Raw forecast
files are not archived. Keep the work directory private and outside the feed.

## Frontend contract

The root contains `latest.json`, `runs/RUN_TAG/HASH.json`, and
`objects/HASH.bin.gz`. Fetch `latest.json` without caching, then its immutable
manifest. All manifest and object paths are relative to the **feed root**.
Content hashes and byte counts apply to the compressed object bytes. The server
must serve `.bin.gz` as a gzip file without `Content-Encoding: gzip`, since the
client decompresses it explicitly. Mutable discovery must not receive immutable
cache headers. CORS must allow the frontend origin.

Both discovery and manifest use schema `xcbenz.w.ch1.v1`. The manifest describes:

| Field | Meaning |
| --- | --- |
| `reference_time` | Model initialization in UTC |
| `operational_mode` | `prototype_snapshot`; this feed is not automatically refreshed |
| `coverage.horizons_hours` | Exactly the available forecast hours |
| `coverage.full_model_run` | Whether every hour from zero through the model maximum is included |
| `grid` | EPSG:4326 grid points, origin, degree increments, width and height |
| `encoding` | Little-endian signed int16, scale 0.01, offset 0, fill -32768 |
| `frames[]` | Horizon, UTC valid time, altitude AMSL, valid-cell count, extrema and artifact |

Arrays are row-major, **south to north**, then west to east within each row.
`index = y * width + x`. Each cell is `raw * 0.01` m/s, except raw -32768 means
missing. Zero is a valid value. Positive means rising air and negative means
sinking air. Check timestamps and coverage before offering a time or altitude.
The frontend should use a diverging palette with a visible m/s legend and treat
missing cells separately from zero. These values represent model-resolved air
motion, not predicted thermal climb or glider net climb.

The dependency-free ESM reader in `clients/w-feed.mjs` works in browsers and
recent Node. It validates metadata, dimensions, time/altitude coverage, byte
counts and gzip expansion. It verifies SHA-256 when WebCrypto is available,
including HTTPS browsers. Plain HTTP previews may lack WebCrypto; the returned
`checksumVerified` flag reports this accurately.

```js
import { loadForecast, loadFrame } from './w-feed.mjs';
const forecast = await loadForecast('http://100.83.24.48:8765/');
const field = await loadFrame(forecast, 1, 3000);
const verticalSpeed = field.valueAt(400, 250); // m/s, or null for missing
```

For a private development endpoint:

```sh
python -m w_forecast.serve --root /data/xcbenz-w-prototype/feed \
  --bind YOUR_TAILSCALE_IP --port 8765
```

The server is read-only, has no directory listing, and exposes only feed JSON
and gzip objects. Use an HTTP development frontend with this private HTTP feed.
An HTTPS website needs an HTTPS feed or a same-origin development proxy.
`benchmark.json` records stage timing, process peak RSS, source bytes, output
bytes and cache/resume use. Nothing is uploaded to Infomaniak by this prototype.

## Weather server

The standalone Compose file reuses the existing forecast image and dependencies.
It mounts only this package, the wrapper, separate W work/output directories and
the existing heavy-job lock volume. There are no publication credentials.

```sh
export W_FORECAST_IMAGE=xcbenz-forecast:local
export W_WORK_DIR=/data/xcbenz-w-prototype/work
export W_FEED_DIR=/data/xcbenz-w-prototype/feed
export W_HEAVY_LOCK_VOLUME=hetzner-deploy_xcbenz-heavy-lock
docker compose -p xcbenz-w-prototype -f deploy/w-prototype/compose.yaml \
  run --rm w-prototype
```

The prototype takes the same `xcbenz-heavy.lock` as production. Exit 75 means
production holds it; retry later. It does not queue itself ahead of production
or change the coordinator. The container is limited to 3 GiB RAM and two CPUs.
The weather host currently has no Tailscale client. To serve a weather-generated
feed privately, bind the feed server to weather-host loopback and forward that
port to the Coding Server's exact Tailscale address through SSH. Do not bind it
to the weather server's public interface.

Promotion would require scheduler integration, an isolated publication namespace,
the shared publication lease and validation, remote quota preflight, retention
integration, and paired frontend fixtures. This branch deliberately leaves those
production changes for a later release.

## Validation

A real-data trial on the Coding Server used the 2026-10-24 06Z run, all 34
hours from H+0 through H+33, ten altitudes and the default 1561 by 761 grid.
Its 340 frames totalled 355.85 MB compressed, compared with 807.79 MB decoded.
New hours averaged 6.32 seconds including download, decoding and compression.
The full export peaked at 290 MB RSS with cached geometry. Initial geometry
preparation took 20.06 seconds and the cold process peaked at 914 MB RSS.
These are Coding Server measurements; the weather-server benchmark must wait
for its production heavy-job lock. Weather Compose validation and the lock's
busy exit 75 were checked without changing production.

The Python suite includes analytic interpolation and terrain tests, zero-hour
time units, truncated downloads, checksum failures, publication rollback,
retention, and interruption/resume through private checkpoints. The JavaScript
tests cover gzip round trips, sign/zero/missing values, unsafe paths, timestamp
mismatches, corrupt objects and oversized decompression. Real first/last-hour
frames also passed the ESM reader over the private HTTP endpoint.

```sh
TEST_TMPDIR=/tmp python -m unittest discover -s tests -v
node clients/w-feed.test.mjs
```
