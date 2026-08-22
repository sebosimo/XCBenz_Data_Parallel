# Forecast transfer size audit

Date: 2026-08-22

Repositories reviewed:

- `sebosimo/XCBenz_Data_Parallel`
- `sebosimo/XCBenz_Web`

This audit covers forecast metadata, value tiles, browser rendering, prefetch,
and emagram bundles. It answers two questions:

1. Does the browser receive screen pixels or model data?
2. Does it receive only the information needed for the current display?

The browser receives model-grid values and renders screen pixels locally. That
part is sound. The transfer is still larger than necessary because fixed tile
boundaries include cells outside the viewport, manifests repeat metadata,
emagram bundles store derived values beside their inputs, and a few display
channels have more spatial or numeric detail than their rendering needs.

The recommended changes keep progressive prefetch. They make every foreground
and prefetched forecast cheaper.

## Implementation status

Phase 1 started on 2026-08-22. The backend now writes
`web_exports/manifest.compact.json` beside the existing manifest. The compact
contract shares location metadata and forecast schedules, then reconstructs
the version-1 object without losing fields. Local and remote publication checks
compare the expanded compact object with `manifest.json`.

The frontend work lives on the matching
`codex/forecast-transfer-manifests` branch in `XCBenz_Web`. It requests the
compact file first and falls back only when that resource returns HTTP 404.
The version-1 file remains published throughout the rollout.

## Executive summary

The largest findings are:

- The default Switzerland viewport uses about one third of the source cells in
  its selected tiles. The rest lie outside the viewport.
- A centered maximum-zoom view uses about 3% of the selected tile cells.
- On a representative forecast hour, an exact viewport crop would reduce
  compressed Wind data from 36.6 KB to 12.6 KB, Sun/Rain from 20.8 KB to
  9.2 KB, and Cloud plus Rain from 48.1 KB to 23.2 KB.
- The current public root manifest is 6,778,287 bytes of JSON and transfers as
  109,601 bytes with gzip. A normalized proof of concept was 45,020 bytes raw
  and 3,370 bytes with gzip level 9.
- Emagram bundles store five base variables and five values calculated from
  those variables. Removing the calculated variables cuts their raw profile
  payload in half without losing information.
- Low, middle, and high cloud layers use the full 0.02-degree grid even though
  the UI samples them only for symbols spaced 9 or 11 CSS pixels apart. A
  one-third-resolution simulation reduced compressed Cloud payload by 42.4%.
- Bundling current tiles gives little compression improvement for dense
  forecast fields. It helps request count more than body size.
- Empty-tile indexes can remove 30% of Rain requests and 26% of Sun/Rain
  requests in the measured CH2 run, with no rendering change.
- The existing prefetch scheduler already gives foreground work priority.
  Limiting prefetch would trade away perceived speed and is not part of this
  proposal.

## Scope and measurement method

Code inspection covered the current backend export code and the frontend code
that selects, loads, caches, stitches, interpolates, and displays forecast
values.

Raster payload measurements used the retained export:

```text
web_exports/value_tiles/v1/icon-ch2/20260729_1200/6ec9fe18a0b9
```

Unless a section says otherwise, Switzerland means the default frontend
bounding box `[5.5, 45.5, 11, 48.2]`. It selects tile columns 1 through 3 and
rows 1 through 2. Compressed estimates use Python gzip level 9 over the current
XVT data. Public manifest figures use observed HTTP response sizes from the
2026-08-22 public export.

The retained run is a concrete comparison set. Weather compressibility varies
by run and hour, so implementation work should repeat these measurements over
several runs before setting regression thresholds.

## What the browser transfers and renders

The frontend does not download a raster image for forecast maps. It downloads
quantized source-grid values:

- Wind uses a 0.04-degree grid.
- Sun/Rain, Cloud, and forecast Rain use a 0.02-degree grid.
- The browser interpolates source cells to canvas or GPU output pixels.
- The renderer caps the generated image at 1.1 million pixels.

Relevant code:

- Backend grids and tile sizes: `value_tiles.py`, lines 85 through 131
- Tile encoding and one-cell halo: `value_tiles.py`, lines 368 through 430
- Frontend render-size calculation:
  `XCBenz_Web/web/src/mapRendering/canvasCore.ts`, line 79
- Product render caps:
  `XCBenz_Web/web/src/mapRendering/windCanvas.ts`, line 18;
  `sunRainCanvas.ts`, line 17; and `cloudRainOverlay.ts`, line 9

For a representative 390 by 844 CSS-pixel phone at device pixel ratio 3, the
device ratio is capped at 2. The forecast plot produces roughly 625,000 local
pixels. On the default Switzerland view, a 0.02-degree source cell spans about
1.4 CSS pixels horizontally. Wind cells span about 2.8 CSS pixels. The source
grids are not too detailed for the display. Further global downsampling would
make the fields blockier, especially on desktop.

## Spatial tile overfetch

The frontend waits 240 ms after a view change, then passes the settled
geographic bounding box to tile selection. The selector computes the required
source-cell bounds, then rounds outward to the fixed tile matrix. The stitcher
keeps the whole selected tile rectangle rather than cropping it to the
viewport.

Relevant frontend code:

- `XCBenz_Web/web/src/features/rasterMaps/products/controller.ts`, lines 20
  through 33
- `XCBenz_Web/web/src/valueTiles.ts`, lines 405 through 424
- `XCBenz_Web/web/src/valueTiles.ts`, lines 544 through 624

The current fine grid uses 160 by 112 core cells per tile. Wind uses 80 by 56
cells. Since Wind cells are twice as large in both directions, both tile types
cover about 3.2 by 2.24 degrees. Each tile also carries a one-cell halo for
interpolation.

### Preset efficiency

The table shows the share of selected fine-grid cells that the viewport needs.
The reciprocal is the source-cell overfetch ratio.

| View | Cells needed | Source-cell ratio |
| --- | ---: | ---: |
| Switzerland | about 34% | 2.9x |
| All Alps | 81.5% | 1.23x |
| Full ICON domain | 80.6% | 1.24x |
| French Alps North | 38.7% | 2.59x |
| French Alps South | 20.7% | 4.84x |
| Austrian Alps | 26.4% | 3.79x |
| Central Alps | 30.5% | 3.28x |
| Eastern Alps | 33.2% | 3.01x |

Wind has nearly the same ratios because its tiles cover the same geographic
area.

### Zoom efficiency

For a view centered over Switzerland:

| Relative zoom | Fine cells needed | Fine overfetch | Wind cells needed | Wind overfetch |
| --- | ---: | ---: | ---: | ---: |
| 1x | 34.1% | 2.93x | 34.1% | 2.93x |
| 2x | 13.3% | 7.54x | 13.4% | 7.44x |
| 4x | 6.8% | 14.66x | 7.2% | 13.91x |
| Maximum tested zoom | 3.0% | 33.46x | 3.3% | 30.49x |

Compression makes the byte ratios smaller because cells outside the view still
contain spatially correlated data. The remaining penalty is large:

| View | Wind | Sun/Rain | Cloud plus Rain |
| --- | ---: | ---: | ---: |
| Switzerland | 2.91x | 2.26x | 2.07x |
| 2x zoom | 7.33x | 3.33x | 3.54x |
| 4x zoom | 12.21x | 8.59x | 7.81x |
| Maximum tested zoom | 23.93x | 19.59x | 14.33x |

These ratios compare current compressed tiles with a compressed crop containing
the source cells needed for the viewport and interpolation edge.

### Tile-size experiments

A 192 by 128 candidate reduced request count for the default Switzerland view
from six tiles to four and reduced compressed data:

| Product | Current | Candidate | Change |
| --- | ---: | ---: | ---: |
| Wind | 36.6 KB, 6 requests | 33.7 KB, 4 requests | 7.9% fewer bytes |
| Sun/Rain | 20.8 KB, 6 requests | 16.9 KB, 4 requests | 18.8% fewer bytes |
| Cloud plus Rain | 48.1 KB, 12 requests | 40.5 KB, 8 requests | 15.8% fewer bytes |

Small 64 by 64 tiles reduced default-view compressed bytes by roughly 34% to
36%, but raised requests to 15 for a single-channel product and 30 for separate
Cloud and Rain. Small tiles are attractive at high zoom, where only a few are
needed, but they are a poor single global tile size.

The suitable design is a static tile pyramid at the same model resolution:

- Use larger tiles for wide views and known regional presets.
- Use smaller tiles for zoomed views.
- Let the frontend select a tier from viewport size and source-cell coverage.
- Keep immutable revision URLs so HTTP and CDN caches remain effective.

A dynamic crop endpoint could approach the ideal byte figures, but arbitrary
bounding boxes weaken shared caching and would add a live backend dependency.
The static frontend must continue to work from normal object or static hosting.

## Metadata transfer

### Root manifest

Observed public root manifest on 2026-08-22:

| Representation | Raw bytes | Gzip bytes |
| --- | ---: | ---: |
| Current | 6,778,287 | 109,601 |
| Normalized proof of concept | 45,020 | 3,370 |

The current JSON repeats model, run, location, product, and URL structure. Gzip
handles repeated strings well, but the browser still transfers 109.6 KB and
parses a 6.78 MB document before loading forecast data.

The normalized experiment used shared tables and identifiers rather than full
nested records. It did not remove forecast choices or locations.

### Product manifests

Observed compressed sizes:

| Manifest | Transfer size |
| --- | ---: |
| Wind | 88,753 bytes |
| Sun/Rain | 23,860 bytes |
| Rain | 17,369 bytes |
| Cloud | 66,365 bytes |
| Value-tile run index | 1,511 bytes |

Equivalent lean product records compressed to roughly 3.3 KB through 3.7 KB
per product in the experiment. A normalized root contract and lean product
manifests are the safest first implementation because they change no model
values or rendered output.

## Per-channel audit

### Wind

Backend encoding:

- Two signed byte components per source cell
- `u` and `v`
- 0.25 m/s scale
- 0.04-degree web grid

Source: `wind_maps.py`, lines 583 through 615.

Both components are used. The renderer needs speed for color and direction for
streamlines and vector direction. A polar representation or coarser numeric
scale may save a modest amount, but spatial overfetch is the larger issue.

Verdict: keep the current base resolution and vector components.

### Sun/Rain

Backend encoding:

- Code 0 for neutral or missing sunshine
- Codes 1 through 100 for sunshine percentage
- Codes 101 through 250 for piecewise precipitation amounts
- One byte per cell

Source: `sunrain_maps.py`, lines 69 through 99.

The UI displays ten sunshine colors and ten rain colors. It interpolates the
decoded numeric values before assigning a color. This means the numeric detail
affects transition edges even when many values end in the same display color.

Measured over the selected Switzerland tiles for the retained CH2 run:

| Encoding experiment | Compressed payload | Change |
| --- | ---: | ---: |
| Current semantic codes | 2,095,147 bytes | baseline |
| One-byte display classes | 862,072 bytes | 58.9% smaller |
| Packed five-bit display classes | 1,077,956 bytes | 48.5% smaller |

The byte-aligned class representation compressed better than five-bit packing
because gzip could exploit repeated byte values. Either class representation
would alter some interpolated boundaries. Treat this as a visual optimization,
not a lossless contract cleanup.

Verdict: test class quantization with image comparisons after lossless work.

### Forecast Rain

Backend encoding uses one unsigned byte per cell at 0.2 mm precision. Source:
`rain_maps.py`, lines 10 through 16.

Mapping values to the ten displayed rain classes reduced compressed payload by
37.5% in the retained run, from 246,261 bytes to 153,941 bytes when headers were
excluded consistently from both sides. It changes interpolation and threshold
edges. Forecast Rain is already small compared with Cloud.

Verdict: low priority for numeric quantization. Empty-tile omission has less
risk and removes many requests.

### Cloud

Backend encoding uses 0 through 10 for 0% through 100% cover, code 15 for
missing values, and four bits per cell. The format cannot shrink losslessly at
the same resolution. Source: `cloud_maps.py`, lines 11 through 23, and
`value_tiles.py`, lines 327 through 356.

The Total Cloud view selects the grouped `cloud4` variant. It transfers total,
low, middle, and high cover. Total drives the raster shading. The other three
drive layer symbols, so they are used. Frontend sources:

- `XCBenz_Web/web/src/features/rasterMaps/cloudRainLoadPolicy.ts`, lines 3
  through 15
- `XCBenz_Web/web/src/mapRendering/cloudLayerSymbols.ts`, lines 108 through
  180

The layer symbols use a screen lattice every 9 CSS pixels on mobile and 11 CSS
pixels on desktop. The backend still sends those layer values at every
0.02-degree grid cell. At a wide view, that is more spatial detail than the
symbols sample.

A simulation kept total cloud at full resolution and sampled low, middle, and
high layers every third source cell in each direction:

| Cloud payload | Raw bytes | Gzip bytes |
| --- | ---: | ---: |
| Current four full-resolution channels | 26,815,536 | 4,241,455 |
| Full total plus three coarse layers | 8,938,512 | 2,444,743 |
| Reduction | 66.7% | 42.4% |

At high zoom the symbol lattice may need the full layer grid. The same
wide-view and zoom-tier idea used for spatial tiles can choose the appropriate
layer resolution.

Verdict: promising visual-equivalent optimization. Validate symbols at mobile
and desktop sizes before adopting it.

## Empty tiles and sparse indexes

The backend currently writes every tile for every step. Step metadata records
tile count and full-grid checksums but no tile occupancy information. Source:
`value_tiles.py`, lines 887 through 967.

Selected Switzerland tiles across the retained CH2 run:

| Variant | Tile files | Entirely zero tiles | Requests removable | Compressed bytes removable |
| --- | ---: | ---: | ---: | ---: |
| Rain | 726 | 219 | 30.2% | 8.2% |
| Sun/Rain | 720 | 184 | 25.6% | 0.9% |
| Cloud4, all four channels empty | 726 | 6 | 0.8% | less than 0.1% |

Individual empty Cloud channel counts were:

- Total: 6 of 726
- Low: 32 of 726
- Middle: 12 of 726
- High: 116 of 726

A per-step sparse index can tell the frontend that a tile has its neutral
value. The frontend can synthesize that tile locally and avoid the request.
This is lossless when the index covers the complete payload, including the
halo and padding, and distinguishes neutral, missing, and absent states where
required.

The index is small. A matrix of 20 tiles needs 20 bits per step and variant
before normal JSON or binary framing.

Implementation status, 2026-08-22: the backend now publishes additive sorted
`neutral_tile_indexes` lists and explicit channel `neutral_values`, and fully
validates them against retained immutable tile files. The frontend synthesizes
declared neutral payloads and excludes their URLs from foreground and warm
requests. Legacy files remain present for old-client compatibility, and the
prefetch scheduler remains unchanged for all non-neutral tiles.

The next implementation slice added a same-resolution detail tier with 128 by
64 fine-grid cores and 64 by 32 Wind cores. The original matrix and URLs remain
available. The frontend selects the detail tier only when it stays within ten
requests and reduces payload cells by at least 20% when it adds requests. For a
Switzerland-centered view, the measured geometry is:

| Relative zoom | Base payload cells | Detail payload cells | Selected reduction |
| --- | ---: | ---: | ---: |
| 1x | 110,808 in 6 requests | 77,220 in 9 requests | 30.3% |
| 2x | 73,872 in 4 requests | 51,480 in 6 requests | 30.3% |
| 4x | 36,936 in 2 requests | 17,160 in 2 requests | 53.5% |
| 16x | 36,936 in 2 requests | 8,580 in 1 request | 76.8% |

These are exact encoded payload cell counts before compression. Compression
changes the byte ratio by product, but the model values and grid spacing do not
change.

The representative one-step, eight-variant backend fixture grows from 160 base
files and 2,417,600 bytes to 496 files and 4,791,776 bytes after adding the
detail tier. That is 3.1 times as many tile files and 98.2% more immutable tile
storage. This is a deliberate server-side cache tradeoff for smaller viewport
responses. A staging publication must measure generation time, retained-tree
traversal, upload, and superseded-tree deletion before production rollout.

## Bundling and compression

Current separate XVT files each start a new gzip stream. The audit compared
that baseline with concatenating selected tiles into one gzip stream.

Across the retained CH2 run:

| Product | Separate tile gzip | One bundle per hour | Saving | Four-hour bundles | Saving |
| --- | ---: | ---: | ---: | ---: | ---: |
| Wind | 3,003,527 | 2,977,028 | 0.9% | 2,950,657 | 1.8% |
| Sun/Rain | 2,139,864 | 2,114,633 | 1.2% | 2,111,214 | 1.3% |
| Rain | 288,756 | 252,448 | 12.6% | 243,610 | 15.6% |
| Cloud4 | 4,305,177 | 4,295,913 | 0.2% | 4,290,400 | 0.3% |
| Total Cloud | 1,965,181 | 1,922,781 | 2.2% | 1,903,992 | 3.1% |

Dense weather fields already compress well inside each tile. Combining them
does not expose much repeated data. Rain benefits because headers and long zero
runs dominate its small payload.

Grouping Cloud4 and Rain for one tested hour changed 96,787 compressed bytes
across 12 files into approximately 97,053 compressed bytes across 6 grouped
files. Request count fell by half, but body size did not improve.

Verdict: bundle to reduce request and filesystem overhead. Do not expect
bundling alone to solve mobile transfer size. A static range-addressable archive
could retain random tile access while avoiding thousands of small files.

## Temporal delta experiments

Step-to-step deltas were generally no smaller than independently compressed
steps. Spatial gzip compression already captures much of the local structure,
while forecast fields move and change between hours.

The one useful result was modular Wind delta coding, which reduced a complete
sequential traversal by about 14%. It creates two problems:

- An arbitrary foreground hour depends on a previous step or keyframe.
- Cache and retry behavior becomes more complicated.

Verdict: do not prioritize temporal deltas. Revisit them only after tile and
metadata changes, with regular keyframes and direct foreground access.

## Emagram and point-forecast bundles

Each profile bundle stores ten float32 arrays at every model level:

```text
p, t, qv, u, v,
temperature_c, pressure_hpa, dewpoint_c, wind_speed_ms, wind_dir_deg
```

The backend calculates the second row directly from the first row. Source:
`web_profiles.py`, lines 19 through 30 and 96 through 149.

Lossless compact contract:

- Keep `p`, `t`, `qv`, `u`, and `v`.
- Calculate Celsius temperature, hPa pressure, dew point, wind speed, and wind
  direction in the browser.
- Keep the shared height array once in bundle metadata.
- Consider reducing levels above 7 km because the current chart and thermal
  views stop there. The measured profiles use about 52 of 80 levels below that
  height. This second change needs a contract decision because another future
  consumer may need the upper levels.

Current frontend behavior loads all `profiles.bin` bytes when any hour from the
bundle is selected. Source:
`XCBenz_Web/web/src/dataEmagrams.ts`, lines 27 through 47.

Observed compressed bundle responses for representative locations were about
102.7 KB for CH1 and 364.9 KB for CH2. The public server returned exact HTTP
206 responses for byte-range requests, but the frontend does not request a
range.

Recommended loading order:

1. Fetch bundle JSON.
2. Fetch the selected step range from the compact five-variable binary.
3. Render the selected hour.
4. Fetch remaining ranges or the remainder of the bundle during idle time.

This preserves complete progressive prefetch and reduces foreground latency.

## Prefetch and cache audit

The progressive forecast prefetch design is useful and should remain.

Current behavior:

- One background forecast job runs at a time.
- Near steps schedule after the next paint.
- Distant warm steps use `requestIdleCallback` when available.
- Starting foreground work pauses the queue and aborts unrelated active warm
  work synchronously.
- A matching warm request remains active and foreground loading joins it.
- Foreground tile loading uses six concurrent tile requests. Warm loading uses
  two.

Relevant frontend code:

- `XCBenz_Web/web/src/features/rasterMaps/rasterPrefetchScheduler.ts`, lines 30
  through 45, 162 through 190, and 219 through 271
- `XCBenz_Web/web/src/valueTiles.ts`, lines 13 through 15 and line 948
- `XCBenz_Web/web/src/valueTiles.test.ts`, tests around concurrent warm and
  foreground consumers

The decoded tile cache is capped at 16 MB. The stitched step cache retains six
steps. A complete decoded Cloud plus Rain timeline is much larger, so the
browser will evict old decoded steps during deep prefetch. HTTP immutable cache
entries may still prevent a second network transfer, depending on browser
cache pressure.

A static binary archive with range requests could support a persistent raw-byte
cache more efficiently than thousands of separate decoded objects, but it is
not necessary for the first implementation phase.

## Recommended implementation order

### Phase 1: lossless metadata cleanup

1. Define a normalized root manifest contract with shared model, run, location,
   and product tables.
2. Produce lean map product manifests.
3. Add frontend decoders for the new version while retaining the old version
   during migration.
4. Add raw, compressed, and parsed-size regression checks.

Expected result: more than 90% less startup forecast metadata, with no map or
forecast-value change.

### Phase 2: lossless spatial transfer

1. Define at least two static tile tiers at unchanged model resolution. Implemented on 2026-08-22.
2. Generate immutable metadata and tile paths for each tier. Implemented on 2026-08-22.
3. Select a tier by viewport source-cell dimensions and expected overfetch. Implemented on 2026-08-22.
4. Add per-step sparse occupancy indexes and synthesize neutral tiles in the frontend. Implemented on 2026-08-22.
5. Preserve the current foreground and progressive prefetch scheduler. Implemented on 2026-08-22.

Expected result: roughly half to two thirds less tile data for the default
Switzerland view, with larger relative gains at zoom.

### Phase 3: lossless emagram compaction

1. Publish only five base float32 variables.
2. Derive display variables in TypeScript with cross-language fixture tests.
3. Fetch the selected step by byte range before warming the rest.
4. Keep a whole-bundle fallback for hosts or intermediaries that ignore ranges.

Expected result: 50% less raw profile binary and a much smaller selected-hour
foreground request.

### Phase 4: request packaging

1. Evaluate a static indexed archive or grouped step files.
2. Keep direct random access for foreground hours.
3. Measure header bytes, HTTP protocol behavior, cache hits, and filesystem
   pressure. Body gzip alone does not justify this phase.

### Phase 5: visual-equivalent reductions

1. Generate a coarse low, middle, and high Cloud tier for wide views.
2. Compare current and candidate renders on phone and desktop viewports.
3. Test Sun/Rain display-class quantization.
4. Test Rain display-class quantization only if Rain remains material after
   sparse-tile omission.

These changes require image-difference thresholds and human review because they
can change interpolation edges.

## Acceptance criteria

Every implementation phase should record:

- Bytes before content encoding
- Actual transfer bytes with production content encoding
- Request count for first foreground frame
- Time to first displayed forecast frame on a throttled mobile profile
- Background prefetch throughput
- Number of foreground requests delayed by warm work
- Browser parse and decode time
- Decoded cache peak
- Pixel comparison against the current renderer for visual-equivalent changes

Suggested functional cases:

- Switzerland, All Alps, and every smaller regional preset
- Default view, 2x zoom, 4x zoom, and maximum zoom
- Phone portrait, phone landscape, and desktop
- Dry, convective, mixed Sun/Rain, clear Cloud, and multilayer Cloud hours
- Direct navigation to a distant forecast hour before prefetch reaches it
- Switching product or viewport while a warm job is active
- Server honors Range, ignores Range, and returns a failed range
- Old and new manifest versions during rollout

## Decisions from this audit

- Keep progressive prefetch.
- Keep foreground priority and warm-request coalescing.
- Keep current base model-grid resolution.
- Fix fixed-tile spatial overfetch with static tiers.
- Normalize metadata before experimenting with lossy encodings.
- Treat bundling as a request-count and filesystem optimization.
- Do not prioritize temporal deltas.
- Require visual tests before changing Sun/Rain precision or Cloud layer
  resolution.

## Source files reviewed

Backend:

- `value_tiles.py`
- `wind_maps.py`
- `sunrain_maps.py`
- `rain_maps.py`
- `cloud_maps.py`
- `web_profiles.py`
- `generate_web_exports.py`
- `scripts/apply_web_retention.py`
- `scripts/analyze_value_tile_packaging.py`

Frontend:

- `web/src/valueTiles.ts`
- `web/src/mapViews.ts`
- `web/src/mapRendering/canvasCore.ts`
- `web/src/mapRendering/colorScales.ts`
- `web/src/mapRendering/forecastValueLayer.ts`
- `web/src/mapRendering/cloudLayerSymbols.ts`
- `web/src/features/rasterMaps/products/controller.ts`
- `web/src/features/rasterMaps/products/cloudRain.ts`
- `web/src/features/rasterMaps/cloudRainLoadPolicy.ts`
- `web/src/features/rasterMaps/rasterPrefetchScheduler.ts`
- `web/src/features/rasterMaps/rasterStepLoader.ts`
- `web/src/dataEmagrams.ts`
