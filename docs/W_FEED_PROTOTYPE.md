# CH1 and CH2 W backend

The optimized backend is on `codex/w-feed-efficient-backend`, based on the
original `codex/ch1-w-feed-prototype`. The original CH1 snapshot contract remains
readable. The default worker produces the model-independent v2 contract. W has
an independent HTTPS publisher. The frontend prototype remains a separate release.

## Current download policy

The existing weather forecast runtime uses four combined processing jobs,
eight concurrent field downloads per job, next-hour disk prefetch, early release
of profile-only fields, and CH2 chunks of 15 hours. It admits new jobs only with
at least 4096 MiB available RAM, CPU at or below 88 percent and one-minute load
at or below 110 percent of the host CPU count.

W contains one field per hour. Its worker therefore uses one decoder and one
background download of the next missing hour. Downloading overlaps decoding and
packaging. There are at most two raw W files on disk, about 372 MB for CH1 or
92 MB for CH2, plus transient download buffers. The 512 MiB per-file cap gives a
one-GiB worst-case raw-file allowance. File size and ten-minute download deadlines
are enforced. Raw files and partial downloads are removed on completion, error
or cancellation. Completed hours resume from verified objects or a private
checkpoint, including after an interrupted run.

Static grids and prepared interpolation plans are reused after hash validation.
Decode one native GRIB level at a time, and only levels needed by the selected
altitudes. W uses the 81 HHL half levels directly. Interpolate vertically at
native cells before horizontal barycentric interpolation. Validate the control
member, reference time, horizon, units and both grid UUIDs for each message.
Below-terrain cells and out-of-domain cells remain missing.

Weather workers share the existing heavy-job lock. An admission guard applies
the same 4 GiB RAM, CPU and load limits, and each container is capped at 3 GiB
and two CPUs. CH1 and CH2 run sequentially. Disk checks preserve at least 10 GiB
free before and throughout writes. Busy or overloaded hosts return exit 75.
There is no separate weather-server performance benchmark.

## Models and command

CH1 uses 0.01 degree output spacing, a 1561 by 761 grid and three-hour source
cycles. Its horizon is normally 33 hours, with the existing 45-hour policy for
03Z. CH2 uses 0.02 degree spacing, a 781 by 381 grid, six-hour cycles and 120-hour
horizons. Both cover 0.8 to 16.4 degrees east and 42.4 to 50 degrees north.
Each model has separate discovery, static files, caches and publication pointers.

Ten AMSL altitudes are 1000, 1500, 2000, 2500, 3000, 4000, 5000, 6000, 7000
and 8000 metres for maps. Cross-sections also use every 250 metres from 2000
through 4500 metres, giving 17 distinct output heights. Override `--bbox`,
`--spacing`, `--altitudes` or `--section-altitudes` for smaller trials. Passing
`--section-altitudes` without values disables the extra section heights.

```sh
python -m w_forecast.runner --model ch1 --run latest \
  --work-dir /data/xcbenz-w-prototype/work \
  --output /data/xcbenz-w-prototype/feed/ch1
python -m w_forecast.runner --model ch2 --run latest \
  --work-dir /data/xcbenz-w-prototype/work \
  --output /data/xcbenz-w-prototype/feed/ch2
```

Omit `--end` to process a complete run. `--run YYYYMMDD_HHMM` pins UTC initialization.
`--start` and `--end` select a bounded horizon range. `--no-prefetch` is available
for diagnosis. The old `--packaging whole-v1` remains available for CH1 only.
MeteoSwiss source availability is about 24 hours after publication. W does not
require permanent raw-data archiving.

## Lossless frontend package

The canonical data remains little-endian signed int16 at 0.01 m/s, with offset
zero and missing value -32768. Positive means rising air. Quantization has at
most 0.005 m/s rounding error; the new compression introduces no further loss,
spatial reduction or clipping of the detail grid. Zero remains distinct from
missing. These are model-resolved air velocities, not thermal or glider climb.

Each time and altitude has one immutable `.wvp` file. It contains a small JSON
directory, a quick overview and independently compressed 256 by 256 detail tiles.
Each detail tile includes a one-cell halo for continuous rendering across tile
edges. The overview samples every fourth grid point for immediate display;
full-resolution detail remains available for every source output cell.

The reversible transform differences in both dimensions, zigzags signed residuals,
separates the two byte planes, then applies gzip level 6. Compression preserves
every quantized int16 value, including missing values and extremes. Gzip can be
decoded with the browser's native `DecompressionStream`, without extra packages
or a WASM decoder.

A browser can get the directory and overview in one small byte-range request,
then fetch only tiles intersecting its viewport. This avoids both whole-domain
initial downloads and thousands of separately hosted tile files. Serve `.wvp`
unchanged as `application/octet-stream`, without an HTTP Content-Encoding.
Enable byte ranges and expose Content-Range, Content-Length and Accept-Ranges
through CORS. Gzip/Brotli HTTP compression is appropriate for JSON manifests.
The included private server implements ranges, compressed JSON and those headers.

## Contract and JavaScript reader

Each model root contains mutable `latest.json`, immutable
`runs/RUN_TAG/HASH.json`, and `objects/HASH.wvp`. All paths in the model manifest
are relative to that model root. Both discovery and manifest use `xcbenz.w.v2`.
Fetch discovery with revalidation or no-store; hash-addressed files are immutable.
The manifest records the model, source initialization, UTC valid times, available
hours and altitudes, grid, int16 encoding, statistics, source hashes and package
metadata. CH1 and CH2 pointers advance independently.

The map manifest retains its original map heights and frames. An optional
`cross_section` descriptor references an immutable manifest by `path`, `sha256`
and `bytes` within the same run directory. It includes all map and section
heights, sharing the existing map objects. Readers fetch it only when opening
a section and must verify its hash, model, grid, configuration, coverage and map
frames against the pinned map run. Older feeds without this descriptor remain
readable at their original resolution. Publication validates and uploads the
entire section closure before advancing `latest.json`; resume and both retention
paths include the section manifest and its objects.

`loadSectionForecast` opens this index. `openSectionFrame` reads only the package
directory, skipping the map overview. Its `loadPoints` method requests only tiles
owning section sample points, using tile halos for bilinear neighbours. Two
heights with two tile requests each remain the cross-section concurrency limit.
The scheduled worker enables the 250-metre section heights by default. Existing
publication guards still reject a configuration change within the same source
run. For an approved one-time rollout, `publication publish
--allow-section-extension` permits adding the first section index only when all
existing map metadata, source hashes and frame records are identical. It does
not permit changing map data or updating an existing section configuration.
Normal scheduled publication does not enable this option.

The grid uses EPSG:4326, row-major storage, south-to-north rows and west-to-east
columns. `index = y * width + x`; grid points have explicit origins and degree
increments. Reject unsupported orientation or encoding, and check valid times.
Render a diverging palette with a visible m/s legend and separate missing cells.

A `.wvp` starts with 16 bytes: eight-byte magic `XCBWVP2` plus NUL, uint32 version
2 and uint32 directory length, both little-endian. The directory follows. Block
offsets are relative to the body after the directory. The manifest contains the
directory SHA-256, length and initial request length, including the overview.
Each block records its own compressed SHA-256, offset, length, dimensions and
uncompressed length. The package filename is the SHA-256 of its complete bytes.

```js
import { loadForecast, openFrame } from './w-packed.mjs';
const forecast = await loadForecast('http://100.83.24.48:8766/ch1/');
const field = await openFrame(forecast, 1, 3000);
// Draw field.overview immediately, using its stride of four.
const tiles = await field.loadViewport([5.8, 45.7, 10.6, 47.9]);
// Tiles include core origin, dimensions, halo and decoded int16 values.
```

The reader validates package structure, range responses, dimensions, checksums
and decompression limits. It makes at most four parallel viewport requests and
caches at most 32 decoded blocks per open frame. A server without ranges falls
back to downloading the package once and reusing it. WebCrypto checksums run on
HTTPS or supported Node versions. Plain HTTP browsers may lack WebCrypto;
`checksumVerified` reports this. The reference code is not a completed map UI.

## Weather deployment

The deployment directory is `/root/xcbenz-w-prototype`. Runtime data is separate
under `/data/xcbenz-w-prototype/work` and `/data/xcbenz-w-prototype/feed`.
Compose reuses the existing `xcbenz-forecast:local` image, mounting this package,
the wrapper, the W directories and the existing heavy-lock volume. It mounts no
publication credentials and does not touch production forecast output volumes.
`REVISION` identifies the archived source commit.

The independent `xcbenz-w-feed.timer` polls every fifteen minutes. Its one-shot
service runs CH1 then CH2 under the shared lock and resource guard. Successful
hours are reused. Exit 75 defers work to a later poll. Other failures retain the
last verified pointer, and the other model can still update. A thirty-minute
service timeout stops only the named W containers. Each successful run writes
`run-stats.json`. Keep two source runs plus a six-hour grace period, then delete
only expired unreachable W objects. Incomplete runs never replace discovery.

```sh
systemctl status xcbenz-w-feed.timer xcbenz-w-feed.service
journalctl -u xcbenz-w-feed.service -n 50
systemctl start xcbenz-w-feed.service
# Stop only automatic W work for rollback:
systemctl disable --now xcbenz-w-feed.timer
systemctl stop xcbenz-w-feed.service
```

The weather host has no Tailscale client. For private development, run the static
server on weather-host loopback and SSH-forward that port onto the Coding Server's
exact Tailscale address. The intended URLs are
`http://100.83.24.48:8766/ch1/latest.json` and
`http://100.83.24.48:8766/ch2/latest.json`. They need the private server and SSH
forward to be running. The Coding Server also retains verified local snapshots
under `http://100.83.24.48:8765/v2/ch1/` and `/v2/ch2/`.

## HTTPS publication

`w_forecast.publication` publishes complete scheduled v2 runs independently at
`https://data.xcbenz.com/web_exports/w_forecast/ch1/latest.json` and the matching
`ch2/latest.json`. The frontend's default W base URL is their parent directory.
This data release does not deploy the frontend prototype.

Install `xcbenz-w-publish.service` and `.timer` from `deploy/w-prototype/` on the
weather host. The publisher runs every fifteen minutes, independently of model
generation, so a failed upload can retry without fetching W again. Its private
`/root/xcbenz-forecast-secrets/w-publish.env` supplies `INFOMANIAK_HOST`,
`INFOMANIAK_USER`, `INFOMANIAK_DATA_ROOT`, `INFOMANIAK_SSH_KEY_PATH` and
`INFOMANIAK_KNOWN_HOSTS_PATH`. Reuse the existing forecast key and pinned host
file by path. Never mount these credentials into the W generation containers.
The default account quota is 250 GB with a 5 GiB reserve. Account scans use
`du -sxk` to stay on the active filesystem. Infomaniak mounts read-only NFS
backup snapshots below the home directory; traversing them recounts historical
data and can stall publication. `DATA_HOST_BASE_URL`
and `DEPLOY_ACCOUNT_QUOTA_BYTES` can override the public origin and ceiling.

The publisher takes a verified hardlink snapshot under the model writer lock,
checks remote account usage before uploading, and stages only the current
pointer, immutable manifest and referenced packages outside `web_exports`.
Promotion uses the existing version-1 publication lease. Its mutation guard
stays held throughout the short promotion, preventing a forecast swap or lease
recovery from interrupting it. Existing leases fail closed and retry next poll.
Immutable bytes arrive first and `latest.json` is renamed last. Older runs and
configuration changes within the same run are rejected. Remote W retention keeps
the current run, two newest source runs and six hours for active readers. Each
cleanup is bounded to 5000 files and 2 GiB, touching only W-owned names.

The ordinary forecast publisher must include `w_forecast` in both its preserved
subtrees and capacity accounting before this timer is enabled. This applies to
the weather runtime and GitHub Actions fallback. The W-local Apache policy is
preserved with that subtree. It enables CORS and exposed range headers, keeps
hashed assets immutable, forces `latest.json` to revalidate, and disables HTTP
gzip/Brotli for `.wvp` packages. JSON retains the inherited compression policy.

After each promotion, HTTPS checks verify discovery and manifest hashes plus
HTTP 206 range, CORS and compression behavior on the first and last frames.
Unchanged pointers skip upload and repeat the HTTP checks.

```sh
systemctl status xcbenz-w-publish.timer xcbenz-w-publish.service
journalctl -u xcbenz-w-publish.service -n 50
systemctl start xcbenz-w-publish.service
# Stop W publication only; existing HTTPS data and generation remain available:
systemctl disable --now xcbenz-w-publish.timer
```

### Enabled deployment, 2026-10-25

The weather-host uploader is enabled, using publication source `ce056aad2a`.
The first CH1 and CH2 uploads completed at 14:57 and 14:58 UTC. A second cycle
verified both feeds and skipped unchanged uploads. The canonical JavaScript
reader decoded first/last-hour overviews and Switzerland viewport tiles over
HTTPS for both models, with verified checksums and HTTP 206 range responses.

The forecast source checkout and GitHub fallback contain main revision
`a27253a887`, including W preservation and the filesystem-bounded quota scan.
The weather runtime uses `xcbenz-forecast:w-publication-a27253a887`, also tagged
`xcbenz-forecast:local`, image
`sha256:5d060328b2b228eb0b709f9ade3209210ed7e74f1e2b37856acdd734f51567e4`.
The prior image is retained as `xcbenz-forecast:pre-w-publication-20261025`.
An old forecast one-shot stuck in backup traversal was stopped, and the active
coordinator retried with the corrected image. Other services stayed running.
Account usage before W publication was about 81.0 GB against the 250 GB quota.
Do not restore a forecast publisher without W preservation while this feed is
enabled. Stop only `xcbenz-w-publish.timer` to pause future W uploads.

### Enabled 250-metre section heights, 2026-10-26

Generator and publisher source `4312143f25` is deployed on the weather host.
The W service uses the thirty-minute timeout from `64604b8481`, retaining its
two-CPU, 3 GiB container limit, shared heavy-job lock and disk reserve. Previous
code is retained at `/root/xcbenz-w-prototype.pre-section-4312143f25`.

The first complete publications are CH1 2026-10-26 15Z with 34 hours and 578
section frames, and CH2 12Z with 121 hours and 2057 section frames. Both map
indexes retain the original ten heights. The one-time `--allow-section-extension`
rollout verified all existing map metadata and frame records were identical.
The regular publisher then verified both public feeds without that option at
19:54:59 UTC. Both permanent fifteen-minute timers remain enabled and active.

Public JavaScript checks validated both manifest hashes, all 17 heights and
range-readable tiles at 2250 and 4500 metres for the first and last forecast
hours. Additional compressed data is 149,908,126 bytes for CH1 and 119,257,645
bytes for CH2, 269,165,771 bytes together. Full generation took 551 seconds for
CH1 and 479 seconds for CH2; process peak RSS was 912 MB and 307 MB. These are
weather-server measurements for this run, not phone latency measurements.

Release evidence is retained on the Coding Server under
`/home/sebas/reviews/xcbenz-w-section-production-20261026/`. The updated private
prototype reads the public feeds. This backend rollout did not deploy a website.

## Measurements and checks

All comparative measurements were made on the Coding Server. Repacking the
2026-10-24 06Z CH1 run, 34 hours and ten altitudes, reduced 355,851,857 bytes to
247,833,515 bytes, a 30.4 percent reduction including overviews and tile halos.
All 340 detail grids were reconstructed and compared exactly with the originals.
For H+6 at 3000 m, discovery of a frame plus its overview transferred 75,833 bytes;
a Switzerland viewport including preview transferred 324,645 bytes across six
detail tiles, compared with the old whole-grid file's 1,120,506 bytes.
These payload measurements do not measure phone rendering latency.

CH2's complete 121-hour, ten-altitude run produced about 204 MB. Next-hour
prefetch reduced its average foreground network wait to about seven milliseconds,
while its actual download averaged 581 milliseconds per hour. Its process peak
RSS was about 308 MB. Initial CH1 geometry preparation remains under one GB RSS;
prepared plans and bounded processing keep warm runs much smaller. Container
memory also includes reclaimable file cache, which is why the 3 GiB cap is kept.

Tests cover analytic interpolation, terrain and missing values, zero-hour GRIB
time units, model independence, checksum failures, interruption/resume, bounded
prefetch and cancellation, all int16 values, tile-edge halos, cross-language
Python/JavaScript decoding, range failures and full-file fallback. Real CH1 and
CH2 first/last-hour fields pass the range reader over private HTTP.

```sh
TEST_TMPDIR=/tmp python -m unittest discover -s tests -v
node clients/w-feed.test.mjs
node clients/w-packed.test.mjs
```
