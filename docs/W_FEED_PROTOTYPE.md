# CH1 and CH2 W backend

The optimized backend is on `codex/w-feed-efficient-backend`, based on the
original `codex/ch1-w-feed-prototype`. The original CH1 snapshot contract remains
readable. The default worker now produces the model-independent v2 contract.
Existing forecast publication and the website are unchanged.

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
and 8000 metres. Override `--bbox`, `--spacing` or `--altitudes` for smaller trials.

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
last verified pointer, and the other model can still update. A fifteen-minute
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

An HTTPS production website still needs HTTPS publication through the shared
publication lease, remote quota checks and namespace-preservation rules. The
independent weather backend and private development feed do not change the
production website or existing forecast publisher.

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
