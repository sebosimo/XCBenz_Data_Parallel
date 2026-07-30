# Wind streamline vector-tile implementation plan

- Status: reviewed proposal; Codex and Claude Opus accepted the plan
- Scope: ICON-CH1 `800m_AGL` pilot
- Repositories: `XCBenz_Data_Parallel` producer and `XCBenz_Web` consumer
- Fallback: existing U/V grid and browser streamline worker remain mandatory

## 1. Outcome

Move repeated streamline integration out of the browser by publishing immutable,
multi-resolution streamline tiles alongside the existing Wind U/V values.

The first production-shaped milestone is deliberately narrow:

- ICON-CH1 only;
- `800m_AGL` only;
- all forecast steps in one retained run;
- compact/mobile geometry partitioned at XYZ z6;
- wide/desktop geometry partitioned at XYZ z7;
- optional beta2 capability;
- complete-frame fallback to the current browser worker.

This milestone succeeds only if it improves physical-device step latency without
materially delaying forecast publication or creating visible tile seams.

## 2. Non-goals

The pilot does not:

- remove whole-grid Wind files or spatial Wind value tiles;
- expand to the other seven Wind levels or ICON-CH2;
- generate arbitrary camera zooms;
- interpolate geometry between forecast steps;
- change Wind sampling, map colours, point inspection, or value-tile transport;
- make the static frontend depend on a live application server;
- enable the capability in production before beta2 and rollback gates pass.

## 3. Evidence and baseline

Measured on the Hetzner Coding Server's 8-core AMD EPYC VM using
ICON-CH1 run `20260729_1200`, `800m_AGL`:

| Measure | Compact/mobile | Wide/desktop |
| --- | ---: | ---: |
| Tile partition | XYZ z6 | XYZ z7 |
| Full-domain tiles per step | 9 | 30 |
| H00 full-domain gzip | 387,326 B | 347,967 B |
| H24 full-domain gzip | 447,018 B | 394,557 B |
| Default-view tile requests | 2 | 6 |
| H00 default-view Brotli | 121,232 B | 90,242 B |
| H24 default-view Brotli | 157,303 B | 114,026 B |
| Current integrate/draw median | 142–146 ms | 64–65 ms |
| Prototype stitched cold median | 30–31 ms | 23 ms |
| Prototype cached draw median | 13 ms | 11–12 ms |

The current Python prototype takes 59–60 seconds per step for both profiles:

- approximately 29 seconds integrating trajectories;
- approximately 30 seconds projecting, assigning, clipping, simplifying,
  encoding, and gzip-compressing tiles.

Thirty-four steps therefore take about 34 minutes only when run serially.
That is a diagnostic upper bound, not an acceptable implementation target.
Four-worker parallelism alone has an ideal lower bound near 8.5 minutes before
contention and therefore addresses the 12-minute hard gate, not the five-minute
target. The five-minute target depends on measured projection/clipping and
possibly integration optimization.

The prototype's coordinate-string fragment stitch also spends approximately
20–27 ms in browser decode/stitch and must not become the production contract.

All payload, storage, and browser numbers in this section come from XWS1.
They are lower-bound planning evidence, not passed XWS2 gates. XWS2 adds path
identity, fragment order, flags, and CRC data; Phase A must regenerate every
size and browser number from actual XWS2 artifacts before any corresponding
gate can pass.

## 4. Target architecture

```text
existing Wind step U/V
        |
        +--> existing whole grid/value tiles --------------------------+
        |                                                              |
        +--> bounded process pool                                      |
              -> integrate deterministic global seed lattices          |
              -> assign stable seed/path IDs                            |
              -> project once to Mercator                               |
              -> clip/simplify directly into z6 and z7 tiles            |
              -> encode XWS2 + validate + immutable revision            |
                                   |                                    |
                                   v                                    |
                     optional root-manifest capability                  |
                                   |                                    |
                                   v                                    |
browser selects visible profile/tiles -> worker fetch/decode/join/draw |
                                   |                                    |
                                   +---- any unsupported/error state ---+
                                                existing worker fallback
```

The producer owns files, immutable identity, validation, retention, and the
capability declaration. The static frontend owns tile selection, bounded
caching, worker rendering, cancellation, and fallback.

## 5. XWS2 binary and package contract

### 5.1 Immutable paths

Use backend-owned generated paths:

```text
web_exports/wind_streamline_tiles/v2/manifest.json
web_exports/wind_streamline_tiles/v2/{model}/{run}/{revision}/revision.json
web_exports/wind_streamline_tiles/v2/{model}/{run}/{revision}/{level}/metadata.json
web_exports/wind_streamline_tiles/v2/{model}/{run}/{revision}/{level}/{profile}/{step}/z{z}/{x}_{y}.xws
```

`revision` is a SHA-256-derived identifier over canonical metadata and raw tile
records. Revision paths are immutable and receive long-lived immutable cache
headers. Mutable manifest aliases remain revalidated.

The root forecast manifest advertises the capability only after every declared
profile, step, tile record, checksum, and revision record has been generated
and locally validated.

### 5.2 Capability

The root capability should be additive:

```json
{
  "contract": "xcbenz-wind-streamline-tiles",
  "contract_version": "2.0.0-beta.1",
  "package": "immutable-xyz-xws2-v1",
  "status": "dual-render",
  "manifest": "web_exports/wind_streamline_tiles/v2/manifest.json",
  "fallback": "wind-streamer-worker-v1",
  "requires_range": false
}
```

The frontend accepts only the exact contract major, package family, fallback,
and supported renderer revision. Unknown or malformed declarations do not
silently enter tiled mode.

### 5.3 Profile metadata

Each profile declares:

- profile name and numeric profile ID;
- renderer revision;
- tile zoom and tile size;
- global quantization bbox;
- nominal pixels per Mercator unit and supported scale interval;
- lattice origin, row/column range, and spacing;
- integration steps, duration, and maximum length;
- simplification tolerance;
- line presentation defaults;
- ordered forecast steps;
- tile template and exact tile matrix coverage;
- expected raw byte, path, point, and checksum limits.

The initial profiles are:

| Profile | Geometry | Tile zoom | Intended use |
| --- | --- | ---: | --- |
| `compact-default` | current compact/mobile lattice and trajectories | 6 | phone portrait and compact layouts |
| `wide-default` | current wide/desktop lattice and trajectories | 7 | desktop and wide layouts |

If the current camera scale falls outside a profile's declared interval, the
frontend uses the existing renderer. The client must not stretch one LOD across
arbitrary zoom levels.

### 5.4 XWS2 tile header

Quantization is global per profile. Every tile in that profile uses the exact
same metadata-declared `(west, south, east, north)` domain. A boundary vertex is
therefore quantized to the same integer coordinate in every adjacent fragment.
Per-tile quantization is forbidden because it can decode a shared boundary to
different coordinates and create a seam.

Use a fixed 32-byte little-endian header:

| Field | Type |
| --- | --- |
| magic | 4 bytes, `XWS2` |
| version, flags, header bytes | `u8`, `u8`, `u16` |
| profile ID, tile z, reserved | `u8`, `u8`, `u16` |
| tile x, tile y | `u32`, `u32` |
| fragment count, point count | `u32`, `u32` |
| payload CRC32 | `u32` |

Decoding requires the validated profile metadata that supplies the global
quantization bbox and limits. Every fragment payload contains:

1. stable `path_id` delta from the previous sorted fragment as unsigned varint;
2. monotonic `fragment_order` as unsigned varint;
3. one-byte continuation/original-end flags;
4. point count as unsigned varint;
5. terminal speed as centi-metres/second varint only when the original end is
   present;
6. first quantized x/y and delta-zigzag-varint remaining points.

Fragments are sorted by `(path_id, fragment_order)` before encoding. The first
ID is absolute; later IDs are deltas, including zero for another fragment of
the same path. The ID is derived from the deterministic profile lattice, not
coordinates or Wind values, so it remains stable across tiles and forecast
steps. Fragment order makes loops or tile re-entry unambiguous.

Required flags distinguish:

- original path start;
- continuation from a previous tile;
- continuation into a later tile;
- original path end and terminal arrow.

The decoder rejects unknown flags, invalid varints, count/length mismatches,
coordinate overflow, CRC mismatch, wrong tile identity, unsupported profile,
duplicate fragment identity, and declared limits exceeded.

Backend and frontend tests must prove that the integer and decoded coordinate
of every shared boundary vertex are bit-identical across adjacent tiles.

### 5.5 Contract fixtures

The backend owns canonical XWS2 tiles, profile metadata, a revision record, and
their SHA-256 provenance. The frontend vendors those exact fixtures with the
backend source commit and hashes.

Any binary or metadata change requires:

1. an intentional contract-version decision;
2. regenerated backend fixtures;
3. updated frontend fixtures and strict decoder tests;
4. both contract suites;
5. frontend `npm run check:contract-fixtures`.

## 6. Backend implementation

### 6.1 Production module boundary

Create a production module such as `wind_streamline_tiles.py`. Keep
`wind_streamline_feasibility.py` and its benchmark scripts as experimental
measurement tools until the production implementation reaches parity.

The production module should own:

- profile definitions and renderer revision;
- source metadata validation;
- lattice enumeration and stable path IDs;
- batch Wind sampling and integration;
- projected trajectory representation;
- tile assignment, clipping, simplification, and XWS2 encoding;
- immutable revision generation;
- local and remote validation;
- feature flags and run/level selection.

### 6.2 Profile before optimization

Add phase timings and counters before changing algorithms:

- source read/decode;
- seed enumeration;
- compact integration;
- wide integration;
- Mercator projection;
- candidate tile assignment;
- clipping;
- simplification;
- encoding;
- compression;
- validation;
- total CPU and wall time;
- parent and worker peak RSS;
- paths, fragments, points, raw bytes, and compressed bytes.

Record machine identity, worker count, Python/NumPy versions, run, level, and
step selection in benchmark JSON. Performance comparisons must use H00, H24,
and the complete 34-step run, not only a synthetic field.

The benchmark has separate XWS1-baseline and XWS2-candidate modes. XWS2 mode
must measure the real encoder, CRC, numeric-ID join, and selected compression
delivery strategy. It must not reuse the prototype coordinate-string stitcher
or grandfather any XWS1 byte/timing result into an XWS2 pass.

### 6.3 Low-risk data-path optimization

First remove known duplicated work without changing integration:

1. Store each accepted trajectory as Mercator coordinates once. Retain
   geographic coordinates only while Wind sampling requires them.
2. Calculate its Mercator bounding box once.
3. Determine candidate tiles from that box once.
4. Clip directly in normalized Mercator/tile coordinates.
5. Simplify in tile-local coordinates without reprojecting each candidate.
6. Stream encoded fragments into per-tile buffers rather than constructing
   repeated geographic `PathGeometry` objects.
7. Sort by numeric path ID and fragment order before encoding.
8. Keep compression outside the integration timing and benchmark useful
   Brotli quality levels instead of assuming quality 11.

This should attack the approximately 30 seconds currently spent after
integration.

### 6.4 Integration optimization

Preserve midpoint integration semantics and float64 calculations initially.
Optimize in measured stages:

1. Replace per-sample Python object creation with array-backed seed state.
2. Add a vectorized bilinear sampler operating on active seed arrays.
3. Advance all active trajectories one integration iteration at a time with
   masks for invalid, calm, domain-exit, stagnation, and maximum-length states.
4. Store coordinates in preallocated dense arrays plus per-path lengths, then
   compact accepted output.
5. Compare path endpoints, terminal directions, lengths, and rendered images
   against the scalar implementation.
6. Only if vectorized NumPy misses the single-step budget, evaluate a pinned
   compiled implementation such as Numba or a small Rust extension.

Do not introduce a new compiled dependency until profiling shows that the
projection/clipping changes and process-level parallelism are insufficient.
If a compiled path is added, the scalar implementation remains the test oracle
and supported fallback for development platforms.

Run the vectorized candidate repeatedly and assert bit-identical raw XWS2 output
on the pinned production NumPy/BLAS versions. Record the generator version and
pinned numeric-runtime versions in revision provenance. A dependency upgrade
requires determinism and parity requalification.

### 6.5 Step-level parallelism

Use a bounded `ProcessPoolExecutor` because the scalar work is Python/GIL-bound
and forecast steps are independent.

- Default to four workers on the 8-core, 15 GiB Coding Server.
- Make worker count explicit and bounded by selected steps and CPU count.
- Read a step file inside its worker instead of serializing large arrays over
  IPC.
- Write each step/profile into its own temporary build directory.
- Return only metadata and metrics to the parent.
- Have the parent sort records, validate completeness, compute revision
  identity, and atomically promote the finished revision.
- On any worker failure, cancel pending work, remove only the owned temporary
  build, and publish no capability.
- Ensure NumPy/BLAS native thread counts are one per worker to prevent hidden
  oversubscription (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`,
  `MKL_NUM_THREADS`, or an equivalent controlled mechanism).
- Run streamline generation after other parallel fetch/decode jobs, or account
  for it explicitly in the pipeline resource scheduler.

Benchmark worker counts 1, 2, 4, and 6. Select the fastest count that leaves
safe CPU and memory headroom; do not assume eight workers are best.

### 6.6 Export and publication integration

Follow the existing spatial value-tile publication pattern:

- `ENABLE_WIND_STREAMLINE_TILES=false` by default;
- explicit model/run and level selection;
- separate `shadow` and `advertise` modes;
- generated revision validated before capability mutation;
- immutable revision collision detection;
- safe containment checks for every removal or promotion;
- retention that preserves advertised revisions and removes unreferenced owned
  revisions;
- root manifest written only after capability generation returns;
- atomic data-host swap, shared publish lock, and downgrade guard unchanged;
- local validation before deploy and representative remote validation after.

Streamline generation must not sit before the normal forecast commit point:

1. build, validate, and publish the normal forecast exactly as today;
2. record the base-manifest live timestamp and release the base publication
   lock;
3. run the bounded streamline enrichment stage against that exact run;
4. in shadow mode, retain only benchmark evidence and publish no capability;
5. in advertise mode, build a second complete candidate containing the same
   base forecast plus the validated immutable streamline revision;
6. reacquire the shared lock and verify that the live model/run fingerprints
   still equal the enrichment source;
7. abandon the enrichment if a newer forecast has won the race;
8. otherwise promote through the existing atomic swap and validation path.

This two-commit model makes the base forecast available before optional
geometry. It must not introduce a bespoke unlocked partial-manifest mutation.
Shadow mode writes benchmark results and validates a complete local revision
but does not add the root capability. Advertise mode is unavailable unless the
frontend contract fixtures for the exact XWS2 revision have landed.

Measure the base publication timestamp with streamline generation enabled, not
only when disabled. The enrichment stage must also release the production
pipeline service within its bounded post-commit deadline so later polls are not
silently starved.

### 6.7 Compression delivery decision

Resolve compression before the complete Phase-A benchmark:

1. deploy representative XWS2 tiles to the existing staging data host;
2. measure response `Content-Encoding`, transferred bytes, MIME type, cache
   headers, and decode behavior with real browser fetches;
3. if host/CDN compression is effective, keep raw XWS2 as the logical artifact;
4. if static precompression is required, benchmark Brotli qualities 5–9 and
   gzip, configure correct content negotiation, and include compression CPU,
   sidecar bytes, and sidecar object count in every backend gate;
5. do not use Brotli quality 11 by default or quote locally compressed bytes as
   network transfer evidence.

The revision digest covers canonical raw XWS2. Compression sidecars, if used,
must be deterministically derived and validated but cannot change logical
geometry identity.

Remote validation checks:

- capability and package manifest identity;
- revision digest;
- H00 and terminal-step tiles for both profiles;
- HTTP status, MIME type, immutable caching, content length/encoding;
- raw tile header, tile identity, CRC, counts, and coordinate bounds;
- a declared missing capability only through HTTP 404, never malformed JSON.

## 7. Frontend implementation

### 7.1 Contract and loader

Add the capability type to `types/manifest.ts` and strict runtime decoders in
the existing data-contract boundary. Create a focused client module, modeled on
the spatial value-tile client but independent from value transport.

The client:

- validates capability, package manifest, revision, level, profile, and step
  identity;
- chooses compact or wide using the same responsive presentation decision as
  the current renderer;
- checks the renderer revision and supported camera-scale interval;
- computes exact visible XYZ tiles;
- loads an optional one-tile pan-prefetch ring only after the visible set;
- treats a frame as ready only when all required visible tiles validate;
- coalesces identical requests and supports `AbortSignal`;
- quarantines a failed immutable tile/revision to avoid retrying every repaint;
- never mixes fragments from different revisions, steps, or profiles.

Wind U/V values continue loading normally for colour rendering, value
inspection, unsupported views, and fallback.

### 7.2 Worker ownership

Extend the existing Wind render-worker boundary or introduce one focused
streamline-tile worker after measuring bundle cost. The chosen design must keep
network decode, fragment joining, Canvas drawing, and `ImageBitmap` creation off
the main thread.

The worker should own:

- immutable tile byte and decoded-fragment LRU caches;
- XWS2 strict decoding;
- numeric grouping by `(path_id, fragment_order)`;
- joining only continuation fragments, not indexing every coordinate string;
- OffscreenCanvas draw and `ImageBitmap` transfer;
- request cancellation and stale-frame suppression;
- phase-level performance counters.

The main thread supplies revision/profile/step/tile identities and the current
render snapshot. It commits only a complete bitmap whose frame identity still
matches the active forecast frame.

If tiled rendering is absent, unsupported, cancelled by a newer frame, or
fails validation/fetch/render, invoke the existing worker integrator for that
complete frame. Never display a partial tiled frame and never combine tiled and
locally integrated streamlines in one frame.

### 7.3 Cache and prefetch policy

Use byte-accounted, bounded caches rather than entry counts.

- Key by revision, level, profile, step, z, x, and y.
- Keep the active step and immediate timeline neighbours warm after the active
  frame succeeds.
- Prefer visible tiles over pan-ring or timeline prefetch.
- Limit tile fetch concurrency and abort stale foreground/prefetch work.
- Start with an 8 MiB mobile and 16 MiB desktop combined raw/decoded budget,
  then tune from measured peak memory and eviction telemetry.
- Rely on immutable HTTP caching as the second-level cache.
- Clear or namespace worker caches when revision or renderer identity changes.

Prefetch must remain opportunistic: it cannot delay the active frame or turn a
prefetch failure into a user-visible product error.

### 7.4 Dynamic-layer integration

Integrate at the existing `forecastDynamicLayer` Wind worker decision:

1. construct the normal Wind render snapshot;
2. request a tiled bitmap when a supported tile plan is ready;
3. retain the current frame while the replacement is pending;
4. commit the tiled `ImageBitmap` through the existing frame-identity path;
5. use the current Wind worker for the complete-frame fallback;
6. retain the current main-thread fallback if OffscreenCanvas itself is
   unsupported.

Do not change MapLibre scene ownership, texture-update ordering, or the Wind
value layer.

### 7.5 Frontend observability

Record separate performance events for:

- manifest/profile resolution;
- visible tile count and compressed/content bytes;
- HTTP-cache and worker-cache hits;
- fetch wait;
- decode;
- numeric join;
- Canvas draw;
- bitmap transfer and main-thread composite;
- total request-to-commit;
- cancellation;
- fallback reason;
- cache bytes and eviction.

Network time and CPU time must remain separate. Report p50 and p95 by
responsive mode, device class, profile, step, and cache state.

## 8. Verification

### 8.1 Backend

Add tests for:

- deterministic stable path IDs across H00/H24 and worker counts;
- scalar versus optimized integration parity;
- exact tile boundary clipping and fragment ordering;
- exact profile-global quantization metadata and absence of per-tile domains;
- bit-identical integer and decoded shared vertices across adjacent tiles;
- no duplicate terminal arrow;
- deterministic XWS2 bytes and revision digest;
- CRC and every malformed-header/count/varint case;
- source metadata/profile mismatch;
- generation completeness and immutable collision;
- worker failure cleanup;
- enabled enrichment preserving the normal base-publication commit point;
- stale enrichment abandonment when a newer live run appears;
- deterministic compression sidecars when the chosen delivery strategy uses
  them;
- capability absent by default and added only after validation;
- retention and staging merge;
- selective generation by run/level;
- local and remote publication validation.

The full backend suite and `compileall` remain required.

### 8.2 Frontend

Add tests for:

- canonical XWS2 fixture decoding and provenance;
- decoder limits and corrupt input;
- numeric fragment joining, including re-entry and missing adjacent tiles;
- real XWS2 numeric-join benchmark input, with no coordinate-string stitcher
  available in the measured code path;
- profile and camera-scale selection;
- viewport tile coverage;
- complete-set readiness and no partial frames;
- cancellation and stale response suppression;
- bounded LRU accounting and request coalescing;
- neighbour/pan prefetch priority;
- immutable failure quarantine;
- worker cache hit and bitmap transfer;
- fallback for 404, malformed, fetch, decode, render, and unsupported states;
- dynamic-layer frame identity and resource cleanup.

Required checks:

```text
npm test
npm run typecheck
npm run check:imports
npm run check:type-contracts
npm run check:contract-fixtures
npm run build
```

### 8.3 Visual and device validation

Automate current-versus-XWS2 screenshots for H00 and H24:

- desktop 1024 × 640;
- phone portrait 411 × 520;
- at least two panned views crossing vertical and horizontal tile boundaries;
- one supported scale near each LOD interval boundary.

Measure whole-image and two-pixel seam-strip RMSE. Review blink/slider images
manually. Run the accepted build on a physical phone through beta2 and record
cold, warm, cached-next-step, pan, and rapid timeline-scrub traces.

## 9. Performance and resource gates

All backend timings use the named 8-core Coding Server with no competing heavy
pipeline job. Browser CPU gates use the existing headless benchmark machine;
physical-phone latency is a separate release gate.

| Area | Target | Hard beta gate |
| --- | ---: | ---: |
| Complete 34-step backend wall time | ≤ 5 min | ≤ 12 min |
| Base-manifest live-time regression with enrichment enabled | ≤ 30 s | ≤ 60 s |
| Post-base enrichment plus second deploy/validation | ≤ 8 min | ≤ 15 min |
| Total pipeline invocation | measured base p95 + 8 min | measured base p95 + 15 min |
| Worker count | 4 preferred after sweep | ≤ 6 |
| Generator aggregate peak RSS | ≤ 2 GiB | ≤ 4 GiB |
| One-level preferred-encoding transfer corpus | ≤ 30 MB | ≤ 35 MB |
| One-level retained disk including compression sidecars | ≤ 75 MB | ≤ 100 MB |
| One-level logical XWS objects | ≤ 1,350 | ≤ 1,400 |
| One-level deployed objects with two sidecars | ≤ 4,050 | ≤ 4,200 |
| Default-view Brotli, wide | ≤ 110 KB | ≤ 125 KB |
| Default-view Brotli, compact | ≤ 150 KB | ≤ 175 KB |
| XWS2 decode + numeric join p50 | ≤ 8 ms | ≤ 15 ms |
| Wide cold decode/join/draw p50 | ≤ 20 ms | ≤ 35 ms |
| Compact cold decode/join/draw p50 | ≤ 25 ms | ≤ 45 ms |
| Cached draw p50 | ≤ 12 ms | ≤ 16.7 ms |
| Physical-phone cached next-step commit | ≤ 75 ms | ≤ 100 ms |
| Seam-strip RMSE | ≤ whole-image RMSE | ≤ whole-image RMSE + 0.25 |
| Fallback on injected tile failures | 100% complete frame | 100% |

Also require:

- no statistically meaningful regression to base forecast publication while
  shadow/enrichment mode is enabled; generation begins only after the base
  manifest is live;
- the production service releases within 15 minutes after the base commit, so
  timer polls cannot be starved without bound;
- no capability publication from incomplete output;
- deterministic raw tiles and revision identity across worker counts;
- no visible seam, doubled arrow, partial frame, or stale-frame flash;
- no unbounded browser or worker memory growth during a 34-step scrub;
- transfer budgets measured from actual data-host content encoding, not local
  compression estimates.

The size and browser rows are acceptance thresholds, not current passes. Phase
A must replace the XWS1 baseline with real XWS2 results, including identity
overhead, numeric joining, CRC, and the chosen data-host compression behavior.

If the optimized implementation misses a target but passes the hard gate, keep
it beta-only and profile the remaining phase. Missing a hard gate blocks
advertisement.

## 10. Rollout

### Phase A — XWS2 contract and optimized shadow generator

- Implement XWS2 and canonical fixtures.
- Add timings, low-risk projection/clipping changes, and bounded step workers.
- Add vectorized integration only if the measured integration phase remains
  material.
- Generate H00/H24 and compare against the scalar oracle.
- Run the complete 34-step `800m_AGL` shadow export.
- Regenerate all storage, transfer, decode/join, draw, and seam measurements
  from XWS2; do not reuse XWS1 pass/fail status.
- Run the full pipeline with enrichment enabled and measure both the normal
  base-manifest live timestamp and bounded post-base service completion.

Exit: backend hard gates, base-publication isolation, determinism, cleanup,
real XWS2 byte budgets, and visual parity pass.

### Phase B — frontend behind an explicit local flag

- Vendor fixtures and implement strict decoder/client/worker caches.
- Integrate complete-frame fallback.
- Run unit, contract, build, visual, pan, and rapid-scrub benchmarks.

Exit: browser hard gates and injected-failure tests pass.

### Phase C — beta2 opt-in

- Advertise the immutable capability publicly; production frontend ignores it.
- Enable tiled rendering only on beta2 through an explicit flag.
- Validate real CDN/data-host headers and transfer sizes.
- Collect physical-phone cold/warm/cache/pan measurements over two consecutive
  forecast runs.

Exit: both runs pass every hard gate with no fallback or publication anomaly.

### Phase D — default beta2, then production `800m_AGL`

- Make tiled mode the beta2 default while preserving fallback telemetry.
- Promote the already-tested frontend contract to production.
- Keep a client kill switch and backend capability switch.
- Observe at least two production runs before considering broader scope.

Rollback:

- frontend kill switch immediately forces the existing worker;
- removing the optional root capability prevents new tiled selection;
- immutable tile artifacts may remain harmlessly until normal retention;
- never delete U/V fallback files as part of this rollout.

### Phase E — separate expansion decisions

Benchmark each additional level and ICON-CH2 independently. Expansion needs a
new storage, artifact, generation, transfer, and physical-phone decision; it is
not implied by the CH1 `800m_AGL` pilot.

## 11. Implementation sequence across repositories

1. Backend: XWS2 format, scalar oracle, validator, canonical fixtures.
2. Backend: projection/clipping optimization and phase benchmarks.
3. Backend: bounded process pool and complete 34-step shadow result.
4. Frontend: vendor exact fixtures, strict decoder, numeric fragment join.
5. Frontend: tile client, worker cache/render path, and complete-frame fallback.
6. Both: contract checks, visual tests, and failure injection.
7. Backend: disabled-by-default capability generation, retention, and remote
   validation.
8. Frontend: beta2-only opt-in and physical-device measurement.
9. Both: review measured gates before any default enablement.

Each contract-bearing backend change and its frontend consumer must be reviewed
and landed as a paired revision. Generated `web_exports` remain backend-owned.

## 12. Decisions that remain evidence-driven

The following are intentionally not fixed before profiling:

- four versus six generator workers;
- vectorized NumPy versus a compiled integrator;
- extending the existing raster worker versus a focused tile worker;
- exact cache sizes and prefetch ring;
- compression quality and whether precompressed sidecars are worthwhile;
- supported nominal-scale intervals for the two LODs.

Each decision must cite phase timings, memory, transfer, and device evidence.
The optional capability and existing fallback allow the project to stop safely
after any phase.
