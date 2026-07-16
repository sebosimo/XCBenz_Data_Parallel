# Spatial Value Tiles: Packaging Decision and Contract v1

Status: approved for implementation; production rollout is not authorized

Contract name: `xcbenz-spatial-value-tiles`

Contract version: `1.0.0`

Physical package: `immutable-chunks-cloud-dual-v1`

This document decides the physical packaging for lossless forecast value tiles.
It does not authorize a production generator rollout or a production deploy.

## Decision

Use ordinary immutable spatial chunk files with a narrow channel-grouping
hybrid:

- Wind keeps its existing interleaved `u,v` pair in one tile and keeps every
  altitude level independent.
- Sun/Rain and Rain each keep one encoded channel per tile.
- Cloud publishes individual `total`, `low`, `mid`, and `high` tiles for a
  single-layer view.
- Cloud also publishes one `cloud4` tile containing `total`, `low`, `mid`, and
  `high` sections for stack mode and for the current total-mode behavior.
- Rain stays separate from Cloud because it is optional and independently
  useful. A Cloud/Rain view fetches the selected Cloud tile or `cloud4` tile
  plus the matching Rain tile.

Use a 160 by 112 cell core on the 0.02 degree grid and an aligned 80 by 56 cell
core on the 0.04 degree Wind grid. Both have the same 3.20 by 2.24 degree
geographic footprint and form a 4 by 3 tile matrix over the Alps domain. Every
tile carries a one-cell halo on all sides.

This package is preferred because it has normal browser and HTTP cache
semantics, deterministic URLs, simple failure isolation, no Range dependency,
and low request multiplication for Cloud stack. The duplicated `cloud4`
variant increases payload-file count by about 7.2 percent over plain
per-channel chunking, but reduces a Switzerland Cloud stack from 30 requests
to 12 and from 61,779 to 55,731 controlled gzip bytes.

Do not group Wind altitude levels. The active level is one of eight and level
grouping would normally overfetch seven unused levels. Do not group adjacent
spatial tiles. It would discard the view and pan reuse that motivated spatial
delivery.

The Range archive option is disqualified for v1. The live host does not yet
guarantee that HTTP content encoding is disabled for an archive path or
extension.

## Scope and ownership

`XCBenz_Data_Parallel` owns generation, validation, retention, manifest
generation, and forecast publication. The Coding Server is the primary
executor. GitHub Actions is the fallback. Infomaniak
`https://data.xcbenz.com/web_exports/` is the production source of truth.

The existing atomic directory swap remains the publication boundary. Forecast
publication must continue to preserve `live_stations`, `webcams`, `radar_maps`,
and `airspace`. This contract does not take ownership of those subtrees.

Current whole-grid Wind, Sun/Rain, Rain, and Cloud outputs remain published
during migration and rollback. Backend-colored raster tiles are out of scope.

## Measurement basis

The reusable analyzer is `scripts/analyze_value_tile_packaging.py`. The
controlled run used the production ICON-CH1 `20260716_0300` H02 payloads from
2026-07-16:

| Data | Grid | Existing encoding | Raw step bytes |
| --- | ---: | --- | ---: |
| Wind, one level | 313 by 146 | interleaved int8 `u,v`, scale 0.25, missing -128 | 91,396 |
| Sun/Rain | 626 by 291 | uint8 semantic code, missing 0 | 182,166 |
| Rain | 626 by 291 | uint8, scale 0.2 mm, missing 255 | 182,166 |
| Each Cloud layer | 626 by 291 | packed uint4, missing 15 | 91,083 |

The analyzer uses every selector in `XCBenz_Web/web/src/mapViews.ts`, extracts
the exact encoded cells, adds the specified halo and domain padding, constructs
the proposed binary container, and applies deterministic gzip level 9. Reported
bytes are compressed response-body bytes. HTTP headers and already-resolved
product metadata are not included. Overfetch compares fetched body bytes with
an exact view crop compressed in the same controlled run. Negative overfetch
can occur when a grouped file compresses better than the same channels in
separate files.

The selector copy is pinned to `XCBenz_Web` commit
`47d4ca530b03560208652d132039c20b7cdc4e89` and was verified on 2026-07-16.
The analyzer source repeats that provenance beside `SELECTORS`. A later
measurement must re-verify the copy against `mapViews.ts` and update the pin if
the frontend selector list or any bounding box changes.

### Package comparison at the chosen tile size

`Files/run` counts payload files for one model and one run using the observed
production sample shape: 8 Wind levels, 46 Wind steps, 45 Sun/Rain steps, 46
Rain steps, and 46 steps for each Cloud layer. `Retained` models the current
maximum policy of four retained runs for each of two models. Manifest and
metadata files are excluded from both counts. These are benchmark inputs, not
fixed contract cardinalities. An implementation acceptance report must record
the actual step and retained-run counts it observed.

| Package | Switzerland Wind | Switzerland Sun/Rain | Switzerland Cloud single + Rain | Switzerland Cloud stack + Rain | All Alps Cloud stack + Rain | Files/run | Retained | Result |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Ordinary per-channel chunks | 29,693 B / 6 req | 33,464 B / 6 req | 15,684 B / 12 req | 61,779 B / 30 req | 124,452 B / 60 req | 7,716 | 61,728 | Viable baseline |
| Per-channel Range archives | 29,954 B / 7 req | 33,730 B / 7 req | 16,195 B / 14 req | 63,075 B / 35 req | 125,748 B / 65 req | 1,286 | 10,288 | Disqualified by host gate |
| Immutable chunks plus dual Cloud access | 29,693 B / 6 req | 33,464 B / 6 req | 15,684 B / 12 req | 55,731 B / 12 req | 107,074 B / 24 req | 8,268 | 66,144 | Recommended |

For Switzerland, compressed overfetch versus an exact crop is 117.9 percent
for Wind, 87.7 percent for Sun/Rain, 156.7 percent for a single Cloud layer plus
Rain, and 123.6 percent for grouped Cloud stack plus Rain. The deliberately
coarse tiles exchange some first-view overfetch for 4 by 3 All Alps behavior,
bounded request count, and a materially smaller retained file set than finer
candidate grids. All Alps grouped Cloud stack is 6.6 percent smaller than the
separate-channel exact-crop reference because cross-channel gzip compression is
effective.

The 160 by 112 shape was selected over these main alternatives:

- 128 by 96 reduced Switzerland Wind to 25,842 B and Sun/Rain to 28,787 B, but
  produced 20 tiles per step and about 13,780 hybrid payload files per model-run.
- 192 by 128 also produced 12 tiles per step but increased Switzerland Wind to
  38,405 B and Sun/Rain to 35,990 B.
- 256 by 192 reduced the Alps matrix to 6 tiles but increased Switzerland Wind
  to 49,974 B and Sun/Rain to 48,623 B.

### Every selector footprint

These are the recommended hybrid results. A Cloud single-layer request uses an
individual Cloud tile plus Rain. Stack uses `cloud4` plus Rain.

| Selector | Wind | Sun/Rain | Cloud single + Rain | Cloud stack + Rain |
| --- | ---: | ---: | ---: | ---: |
| Switzerland | 29,693 B / 6 | 33,464 B / 6 | 15,684 B / 12 | 55,731 B / 12 |
| All Alps | 62,748 B / 12 | 59,800 B / 12 | 40,506 B / 24 | 107,074 B / 24 |
| French Alps North | 25,186 B / 4 | 22,657 B / 4 | 12,249 B / 8 | 25,278 B / 8 |
| French Alps South | 25,186 B / 4 | 7,245 B / 2 | 7,527 B / 4 | 10,043 B / 4 |
| Austrian Alps | 29,727 B / 6 | 33,014 B / 6 | 24,102 B / 12 | 69,122 B / 12 |
| Central Alps | 20,662 B / 4 | 26,659 B / 4 | 13,279 B / 8 | 43,164 B / 8 |
| Eastern Alps | 19,314 B / 4 | 18,707 B / 4 | 18,135 B / 8 | 50,580 B / 8 |

The number after `/` is request count. Wind values are for one selected level.
Fetching all eight Wind levels would multiply both bytes and requests by eight,
with no useful cross-level cache reuse, which is why levels remain separate.

### Zoom-out and nearby-view reuse

The grouped Cloud stack transition matrix assumes the source view remains in a
bounded decoded-block cache:

| Transition | New bytes | New requests | Target bytes already cached |
| --- | ---: | ---: | ---: |
| Switzerland to Central Alps | 0 | 0 | 100.0% |
| Central Alps to Austrian Alps | 25,958 | 4 | 62.4% |
| Austrian Alps to Eastern Alps | 0 | 0 | 100.0% |
| Switzerland to All Alps | 51,343 | 12 | 52.0% |

Ordinary and hybrid files have standard immutable full-response cache entries.
A nearby pan can reuse them in the browser cache and in a frontend decoded-tile
LRU. Range responses require an application block cache for equally predictable
reuse because browsers and intermediaries need not merge partial entries.

## Host-capability result

The live host was probed on 2026-07-16 using the current Wind H02 file.

- A real Chrome 150 `fetch` with `Range: bytes=0-1023` returned `206`, CORS
  response type, and exactly 1,024 bytes. A full control fetch returned `200`
  and 91,396 decoded bytes.
- `Accept-Encoding: identity` plus the same Range returned `206` with
  `Content-Range: bytes 0-1023/91396`.
- `Accept-Encoding: gzip` plus Range returned `200`, `Content-Encoding: gzip`,
  and no `Content-Range`.
- `Accept-Encoding: br` plus Range returned `206` without content encoding on
  this response.

The checked-in Infomaniak configuration applies HTTP DEFLATE to
`application/octet-stream` and has no archive-path exception. A real browser
can currently obtain a partial `.bin`, but the required path-level guarantee is
absent and an explicit gzip client still receives the complete representation.
Therefore Range archives fail the acceptance gate.

The option can be reconsidered only after a non-production archive extension or
path is configured with both gzip and Brotli disabled, every archive block is
compressed independently inside the file, and another real browser fetch proves
`206`. That host change is not part of contract v1.

## Versioned contract

### Capability declaration

The existing root `web_exports/manifest.json` gains this optional declaration:

```json
{
  "capabilities": {
    "spatial_value_tiles": {
      "contract": "xcbenz-spatial-value-tiles",
      "contract_version": "1.0.0",
      "package": "immutable-chunks-cloud-dual-v1",
      "status": "dual_publish",
      "manifest": "web_exports/value_tiles/v1/manifest.json",
      "fallback": "whole_grid_split_binary_v1",
      "requires_range": false
    }
  }
}
```

Absence of this object means the capability is unavailable. A frontend must
compare the full supported major version, not merely test that `manifest` is
present. Minor additions must be backward compatible. A major version change
uses a new path and a new reader.

`whole_grid_split_binary_v1` is an informational name assigned by this contract
to the existing Wind, Sun/Rain, Rain, and Cloud `metadata.json` plus
`steps/{step}.bin` outputs documented in the repository pipeline contract. It
is not a separately declared capability and the frontend must not try to
resolve or version-check that string. Fallback uses the existing root product
manifest URLs and current whole-grid readers. A future machine-validated
fallback contract requires its own capability declaration and identifier.

### Immutable path structure

```text
web_exports/value_tiles/v1/manifest.json
web_exports/value_tiles/v1/{model}/{run}/{revision}/revision.json
web_exports/value_tiles/v1/{model}/{run}/{revision}/{product}/{variant}/metadata.json
web_exports/value_tiles/v1/{model}/{run}/{revision}/{product}/{variant}/{step}/t{tile_y}_{tile_x}.xvt
```

Examples:

```text
web_exports/value_tiles/v1/icon-ch1/20260716_0300/7f4c2a91d0e3/wind/800m_AGL/metadata.json
web_exports/value_tiles/v1/icon-ch1/20260716_0300/7f4c2a91d0e3/wind/800m_AGL/H02/t1_0.xvt
web_exports/value_tiles/v1/icon-ch1/20260716_0300/7f4c2a91d0e3/cloud/cloud4/H02/t1_0.xvt
web_exports/value_tiles/v1/icon-ch1/20260716_0300/7f4c2a91d0e3/cloud/low/H02/t1_0.xvt
web_exports/value_tiles/v1/icon-ch1/20260716_0300/7f4c2a91d0e3/rain/surface/H02/t1_0.xvt
```

`revision` is the first 12 lowercase hexadecimal characters of SHA-256 over a
canonical revision record. The record is UTF-8 JSON serialized with sorted
object keys, no insignificant whitespace, integer numeric fields, and arrays
sorted by `logical_path`. It contains `contract`, `contract_version`, `model`,
`run`, the complete grid and encoding declarations, and two arrays:

- `tiles`: one entry for every `.xvt` file as
  `{"logical_path": string, "byte_length": integer, "sha256": lowercase hex}`.
  `byte_length` and `sha256` refer to the exact published, decoded HTTP entity
  bytes beginning with `XVT1`.
- `metadata`: one entry per variant as
  `{"logical_path": string, "content": object}`. `content` is the complete
  canonical metadata object before adding the derived `revision` and
  `revision_sha256` fields. It includes step labels, horizons, valid times, grid,
  encodings, matrix shape, URL templates, and full-grid CRC values.

Logical paths start below the revision directory and therefore do not contain
the revision itself. Metadata files are deliberately not represented by a
published-byte hash or byte length, which removes the digest circularity. After
the revision digest is computed, the publisher adds `revision` and
`revision_sha256` to each metadata file without changing its canonical content
entry.

The immutable `revision.json` file is a wrapper with exactly
`{"revision": string, "revision_sha256": string, "record": object}`. The
wrapper itself is not part of `record`. A validator hashes canonical `record`,
checks both derived fields, then strips `revision` and `revision_sha256` from
each published metadata file and compares the re-canonicalized object with its
`metadata[].content`. It can validate selected tile files against the matching
`tiles` entries remotely; local publication validation must validate all tile
entries. If any tile value, header, metadata field, grid, encoding, step, or
file set changes, the revision changes. Retained runs keep their existing
revision paths, so a new publication does not rename old runs.

The v1 manifest is an index of models, runs, revisions, and variant metadata
URLs. A variant `metadata.json` is the step index. It declares the complete
rectangular tile matrix and a deterministic URL template. V1 intentionally has
no per-tile JSON index and no discovery requests. Every declared step must have
every tile. Missing output makes the variant invalid and prevents it from being
advertised.

Minimum v1 manifest run entry:

```json
{
  "run": "20260716_0300",
  "revision": "7f4c2a91d0e3",
  "revision_sha256": "7f4c2a91d0e3...64 hex characters total...",
  "revision_record": "web_exports/value_tiles/v1/icon-ch1/20260716_0300/7f4c2a91d0e3/revision.json",
  "variants": {
    "wind/800m_AGL": {
      "metadata": "web_exports/value_tiles/v1/icon-ch1/20260716_0300/7f4c2a91d0e3/wind/800m_AGL/metadata.json"
    },
    "cloud/cloud4": {
      "metadata": "web_exports/value_tiles/v1/icon-ch1/20260716_0300/7f4c2a91d0e3/cloud/cloud4/metadata.json"
    }
  }
}
```

Minimum variant metadata:

```json
{
  "contract": "xcbenz-spatial-value-tiles",
  "contract_version": "1.0.0",
  "package": "immutable-chunks-cloud-dual-v1",
  "model": "icon-ch1",
  "run": "20260716_0300",
  "revision": "7f4c2a91d0e3",
  "product": "cloud",
  "variant": "cloud4",
  "grid_id": "alps_002deg_v1",
  "tile_matrix": {
    "core_width": 160,
    "core_height": 112,
    "halo": 1,
    "tiles_x": 4,
    "tiles_y": 3,
    "tile_order": "y_then_x",
    "url_template": "{step}/t{tile_y}_{tile_x}.xvt"
  },
  "channels": ["cloud_total", "cloud_low", "cloud_mid", "cloud_high"],
  "steps": [
    {
      "step": "H02",
      "horizon": 2,
      "valid_time": "2026-07-16T05:00:00Z",
      "tile_count": 12,
      "full_grid_crc32": {
        "cloud_total": "00000000",
        "cloud_low": "00000000",
        "cloud_mid": "00000000",
        "cloud_high": "00000000"
      }
    }
  ]
}
```

The CRC values above are placeholders showing shape, not contract fixtures.
The implementation must write measured values.

### Grid definitions

Coordinates are EPSG:4326 cell centers. Authoritative coordinate math uses
integer units of `1e-5` degree to prevent float-derived phantom tiles.

Fine grid `alps_002deg_v1`:

```json
{
  "projection": "EPSG:4326",
  "coordinate_scale": 100000,
  "width": 626,
  "height": 291,
  "lon": {"origin": 400000, "step": 2000, "direction": "east"},
  "lat": {"origin": 4300000, "step": 2000, "direction": "north"},
  "storage_order": "row_major_y_then_x",
  "cell_reference": "center"
}
```

Wind grid `alps_004deg_v1`:

```json
{
  "projection": "EPSG:4326",
  "coordinate_scale": 100000,
  "width": 313,
  "height": 146,
  "lon": {"origin": 400000, "step": 4000, "direction": "east"},
  "lat": {"origin": 4300000, "step": 4000, "direction": "north"},
  "storage_order": "row_major_y_then_x",
  "cell_reference": "center"
}
```

For cell `(x, y)`:

```text
lon = (lon.origin + x * lon.step) / coordinate_scale
lat = (lat.origin + y * lat.step) / coordinate_scale
```

Longitude increases west to east and is the fastest-varying dimension.
Latitude increases south to north. Tile `(0, 0)` is the southwest tile. Within
each encoded channel section, values remain row-major from south to north and
west to east. A shader or sampler must not reverse latitude during upload; any
screen-space reversal belongs in texture coordinates.

### Tile matrix and halo

| Grid | Core cells | Stored payload cells | Matrix | Geographic core span |
| --- | ---: | ---: | ---: | ---: |
| `alps_002deg_v1` | 160 by 112 | 162 by 114 | 4 by 3 | 3.20 by 2.24 degrees |
| `alps_004deg_v1` | 80 by 56 | 82 by 58 | 4 by 3 | 3.20 by 2.24 degrees |

Core start for tile `(tx, ty)` is `(tx * core_width, ty * core_height)`. Stored
payload cell `(1, 1)` is that core start. Payload row 0, final row, column 0,
and final column are the one-cell interpolation halo. Interior halos copy the
exact neighboring global-grid values. Cells outside the global grid, including
the unused portion of an edge core, use the channel's existing missing value.
Payload dimensions never vary, which keeps parsing and GPU upload simple.

The final valid core sizes are:

- Fine grid east edge: 146 cells; north edge: 67 cells.
- Wind grid east edge: 73 cells; north edge: 34 cells.

The reader derives a view's inclusive cell range from the authoritative integer
grid, clamps it to the global dimensions, and fetches the core tiles containing
that range. The tile halo supplies interpolation neighbors, so the reader does
not fetch an extra ring of tiles solely for bilinear interpolation.

### Binary `.xvt` container

All integers are unsigned little-endian unless the channel encoding says
otherwise. The browser receives the decoded HTTP response beginning with this
container. The base header is 48 bytes.

| Offset | Size | Field |
| ---: | ---: | --- |
| 0 | 4 | ASCII magic `XVT1` |
| 4 | 1 | major version, `1` |
| 5 | 1 | minor version, `0` |
| 6 | 2 | total header bytes, including section directory |
| 8 | 2 | flags: bit 0 outside-domain padding present, bit 1 grouped sections |
| 10 | 2 | tile x |
| 12 | 2 | tile y |
| 14 | 1 | halo cells, `1` |
| 15 | 1 | section count |
| 16 | 2 | configured core width |
| 18 | 2 | configured core height |
| 20 | 2 | valid core width inside global grid |
| 22 | 2 | valid core height inside global grid |
| 24 | 2 | stored payload width |
| 26 | 2 | stored payload height |
| 28 | 4 | global grid width |
| 32 | 4 | global grid height |
| 36 | 4 | concatenated payload byte length |
| 40 | 4 | IEEE CRC-32 of concatenated payload bytes |
| 44 | 4 | reserved, zero in v1 |

The header is followed by one 16-byte directory entry per section:

| Entry offset | Size | Field |
| ---: | ---: | --- |
| 0 | 2 | channel id |
| 2 | 1 | encoding id |
| 3 | 1 | component count |
| 4 | 4 | section offset relative to payload start |
| 8 | 4 | section byte length |
| 12 | 4 | decoded cell count |

Payload sections begin at `header_bytes`. Section ranges must be contiguous,
non-overlapping, and exactly cover `payload_bytes`. The reader validates magic,
supported major version, header length, tile coordinates, grid dimensions,
section declarations, byte lengths, and CRC before exposing values.

Flag bit 0 is set if and only if at least one stored payload coordinate maps
outside `[0, grid_width)` or `[0, grid_height)` and was therefore filled with
the channel missing code. This includes west, south, east, or north halo cells
outside the domain and unused cells in a partial east or north core. A stored
missing code originating from valid source data does not by itself set bit 0.

### Encodings and missing values

| Channel id | Channel | Encoding id | Exact v1 representation |
| ---: | --- | ---: | --- |
| 1 | `wind_uv` | 1 | per-cell signed int8 `u,v`, scale 0.25 m/s, offset 0, missing -128 |
| 2 | `sunrain_code` | 2 | existing uint8 semantic codes, missing 0, reserved 251-255 |
| 3 | `rain` | 3 | uint8 precipitation, scale 0.2 mm, offset 0, missing 255 |
| 4 | `cloud_total` | 4 | packed uint4 codes 0-10, missing 15 |
| 5 | `cloud_low` | 4 | packed uint4 codes 0-10, missing 15 |
| 6 | `cloud_mid` | 4 | packed uint4 codes 0-10, missing 15 |
| 7 | `cloud_high` | 4 | packed uint4 codes 0-10, missing 15 |

Cloud sections preserve the current nibble rule independently within each
tile section: even flat cell index is the low nibble, odd flat cell index is
the high nibble, and an unused final high nibble is 15. `cloud4` section order
is total, low, mid, high. Wind section component count is 2; every other
section has component count 1.

No precision, scale, category, or missing-value change is permitted in v1.
Backend colors and palettes are not stored.

### Validation fields

Validation is layered:

1. Revision metadata has a full SHA-256 and an immutable URL prefix.
2. Every `.xvt` header has exact dimensions, section lengths, and payload CRC-32.
3. Every metadata step has per-channel `full_grid_crc32` values computed over
   the existing whole-grid encoded bytes. Reassembling tile cores must reproduce
   these values exactly. Cloud validation must decode each tile-local nibble
   stream to cell codes, place core cells by global `(x, y)`, and repack the
   complete grid with the whole-grid even-low, odd-high rule before computing
   CRC. It must not splice packed tile bytes because halo offsets change local
   nibble parity.
4. Generator validation must compare every reassembled cell, including missing
   values, and must check adjacent core and halo equality on every seam.
5. Publication validation must reject a capability manifest that references a
   missing or malformed tile.

### Cache policy and content encoding

- `web_exports/manifest.json` and
  `web_exports/value_tiles/v1/manifest.json`: `Cache-Control: no-cache,
  must-revalidate`.
- Revision-scoped metadata and `.xvt` files: `Cache-Control: public,
  max-age=31536000, immutable`.
- `.xvt` must have an explicit `application/octet-stream` or dedicated binary
  media type mapping and must be included in the host's gzip or Brotli
  compression filter. It is a whole-file resource and the browser parses the
  decoded response.
- `.xva` is reserved for a possible future Range archive and is not part of v1.
  If introduced, outer HTTP gzip and Brotli must both be disabled.

The current host's `manifest.json` rule already gives the new manifest the
required no-cache behavior. Its one-hour file rule and MIME mappings do not
match `.xvt`, so they are insufficient for tiles. The implementation task must
add and test the `.xvt` MIME mapping, compression inclusion, and immutable path
rule in a non-production environment before rollout.

### Atomic publication and retention

Generation writes a complete revision under staging. Local validation must pass
before `value_tiles/v1/manifest.json` advertises it. The existing guarded
publisher then uploads the complete `web_exports` candidate, preserves the four
live-owned subtrees, and atomically swaps the top-level directory under the
shared remote lock.

The manifest and tiles therefore become visible in one publication. A reader
that has an old manifest continues using old immutable URLs. A reader with the
new manifest sees only the complete new revision. `_previous_web_exports`
remains the operational rollback copy.

Retention keeps only revisions referenced by retained model-runs plus any
revision still required by the candidate's rollback policy. Deleting 66,144
tile payloads at the assumed maximum retention is a material filesystem cost.
The implementation benchmark must time generation, rsync file-list traversal,
remote swap, and old-revision deletion on the Coding Server and Infomaniak.
This operational measurement is an acceptance check, not permission to choose
a Range archive that fails the host gate.

### Frontend fallback

The frontend uses value tiles only when all of these are true:

1. The root capability exists and has a supported contract major version and
   package id.
2. The value-tile manifest and selected variant metadata validate.
3. The selected step declares the complete expected tile matrix.
4. Every fetched tile validates before the frame is committed.

If capability discovery or metadata validation fails, the frontend uses the
existing whole-grid metadata and step file. If any tile in a step fails, abort
that tiled frame and retry the same step through the whole-grid reader. Do not
mix validated tiles with a partial or invalid tile set in one rendered frame.
Keep the previous valid frame visible during fallback.

Whole-grid fallback remains until the reviewed rollback gate has passed at
least 48 hours and four successful production publication cycles, production
and mobile checks pass, and removal receives explicit approval.

## Frontend parsing complexity

The recommended reader needs:

- capability and semver selection;
- deterministic tile selection from the integer grid;
- concurrent full-file fetches with cancellation and a bounded decoded-tile
  LRU;
- one small binary-header parser and CRC implementation;
- packed Cloud nibble upload and product-specific shader mapping;
- selection between individual Cloud and `cloud4` variants;
- whole-grid fallback at the step boundary.

This is more work than plain per-channel chunks only in the Cloud variant
selection. It is materially simpler than an archive reader, which would also
need offset indexes, Range construction, partial-response checks, internal block
decompression, application-owned partial caching, and special host behavior.

## Open decisions

1. Decide whether the new frontend's total mode should continue loading
   `cloud4` for instant stack switching or use individual `total` plus Rain for
   the smallest initial response. The contract supports both.
2. Set the frontend request-concurrency and decoded-tile LRU byte limits after
   desktop and mobile measurement. The contract does not prescribe cache size.
3. Benchmark the modeled 66,144-file maximum retained set on the Coding Server and
   Infomaniak staging path, including deletion and rsync traversal. If the host
   has a practical inode or operation limit, revisit the 160 by 112 size before
   production rollout, not the lossless contract or selector independence. The
   acceptance report must also state the actual step and retained-run counts.
4. Decide whether CRC validation runs on every production tile fetch or only on
   first cache insertion. Generator and publication validation remain required
   in either case.

## Backend implementation task prompt

```text
Work in the XCBenz_Data_Parallel repository from a clean worktree and a feature
branch based on current origin/main. Read AGENTS.md,
deploy/README_coding_server_pipeline.md, this contract at
docs/VALUE_TILE_CONTRACT_V1.md, and the relevant generator, manifest, retention,
validation, and deploy code before editing. Do not modify or deploy production.

Objective:
Implement dual generation and publication of xcbenz-spatial-value-tiles
contract 1.0.0 using package immutable-chunks-cloud-dual-v1, while preserving
all current whole-grid outputs and readers.

Architecture and ownership:
- XCBenz_Data_Parallel owns generator and publisher changes.
- The Coding Server is the primary executor and GitHub Actions remains fallback.
- Infomaniak web_exports is the production source of truth, but this task may
  publish only to an explicitly approved staging path.
- Forecast publication must continue preserving live_stations, webcams,
  radar_maps, and airspace.
- Backend-colored tiles are out of scope.

Required implementation:
1. Add a reusable value-tile encoder for the exact XVT1 little-endian container
   in docs/VALUE_TILE_CONTRACT_V1.md. Preserve existing encodings exactly:
   Wind interleaved int8 u/v at scale 0.25 and missing -128, Sun/Rain semantic
   uint8 and missing 0, Rain uint8 at scale 0.2 and missing 255, and packed
   uint4 Cloud with missing 15 and the existing nibble order.
2. Implement the exact integer grid definitions. Fine products use 626 by 291,
   origin (4.0, 43.0), step 0.02, core 160 by 112, payload 162 by 114. Wind
   uses 313 by 146, the same origin, step 0.04, core 80 by 56, payload 82 by
   58. Longitude and latitude both increase; storage is row-major y then x.
3. Copy a one-cell halo from neighboring global cells. Fill only outside-domain
   and unused edge-core cells with each channel's existing missing code. Keep
   payload dimensions fixed. Set flag bit 0 exactly when any payload coordinate
   lies outside the global grid, including a border halo. Validate every
   interior seam in both directions.
4. Publish individual variants for Wind level, Sun/Rain surface, Rain surface,
   and Cloud total/low/mid/high. Also publish Cloud cloud4 with sections ordered
   total, low, mid, high. Do not group Wind levels or adjacent spatial tiles.
5. Use revision-scoped immutable paths and the non-circular `revision.json`
   record exactly as specified. Add a canonical record fixture, derive full
   SHA-256 and 12-character path revision, strip derived metadata fields during
   validation, and prove that any payload, metadata, grid, encoding, step, or
   file-set change changes the revision.
6. Generate value_tiles/v1/manifest.json and per-variant metadata with complete
   rectangular tile matrices, deterministic templates, step metadata, and
   measured full-grid CRC values. Do not advertise partial variants or steps.
7. Extend generate_web_exports.py, generate_combined_manifest.py,
   scripts/apply_web_retention.py, scripts/validate_outputs.py, and
   scripts/validate_remote_web_exports.py as appropriate. Keep the existing
   output contract unchanged and add the root capability only when the complete
   tiled contract validates.
8. Add a non-production host configuration with an explicit `.xvt` binary MIME
   mapping, gzip or Brotli filter inclusion, and revision-scoped immutable cache
   headers. XVT files are whole-file resources. Do not implement a Range
   archive or rely on Range behavior.
9. Preserve the current atomic staging, freshness guard, shared remote lock,
   whole-directory swap, _previous_web_exports rollback, and live-subtree copy.
10. Add focused unit and integration tests for header parsing, exact bytes,
    missing values, edge padding, Cloud nibble parity, grouped section offsets,
    CRC failure, deterministic revision, path generation, manifest capability,
    retention, incomplete-step rejection, and whole-grid coexistence.

Required validation:
- Reassemble every tile core for representative Wind, Sun/Rain, Rain, and all
  four Cloud layers and assert byte-for-byte equality with existing whole-grid
  steps. For Cloud, decode tile nibbles to cell codes and repack by global flat
  index before comparing bytes or CRC; never splice tile-packed byte ranges.
- Assert neighboring halos equal the adjacent core values and cover domain
  edges and odd Cloud nibble counts.
- Run the narrow tests first, then the proportionate generator, manifest,
  retention, output-validation, and remote-validation test targets.
- Run a no-deploy pipeline sample with pinned runs if practical. Do not publish
  production.
- Measure generation time, compressed bytes, payload-file count, rsync/file-list
  traversal, retention deletion, and validation time. Specifically test the
  modeled 66,144-payload maximum retained shape or a faithful filesystem
  fixture, and record the actual per-product step and retained-run counts used.
- If staging publication is separately approved, prove cache headers, atomic
  new-revision visibility, fallback coexistence, and preserved live-owned
  subtrees. Do not switch the production capability status.

Deliverables:
- Implementation and tests on the feature branch.
- Any contract fixture needed to lock canonical revision hashing and XVT1 bytes.
- Before/after validation results and operational file-count timings.
- A migration note showing how the frontend discovers the optional capability
  and falls back to current whole-grid files.
- Explicit unresolved risks. Stop before production deploy and request review.
```
