# Wind streamline vector-tile feasibility — 2026-07-30

## Decision

Proceed with an opt-in beta2 static vector-tile pilot for the full ICON-CH1
domain at `800m_AGL`. Publish the unchanged compact and wide densities plus an
explicit compact-lite A/B profile. Do not yet enable all-level generation or
remove the current worker fallback.

The experiment proves that backend-integrated streamline geometry can remove
almost all browser trajectory work when it is clipped like a visible vector
tile and simplified below the display's pixel resolution. It does not yet
prove that generating and retaining every zoom band, level, model, and horizon
fits the production pipeline budget.

## Lean follow-up

The internet pilot exposed metadata and subpixel geometry as the two safe
payload reductions:

- raise Douglas-Peucker tolerance from 0.15 to 0.50 presentation pixels;
- replace the embedded 34-step manifest with
  `split-step-index-v1`, where `manifest.json` contains content-addressed step
  descriptors and `steps/Hxx.json` contains only one immutable step;
- retain `compact-default` and `wide-default`, and add `compact-lite` with 10%
  larger seed spacing in each direction for explicit visual A/B testing.

On run `20260730_1500` H00, 0.50-pixel simplification reduced the reference
compact transfer from 196,544 to 141,405 gzip bytes and wide transfer from
143,902 to 109,658 bytes while preserving terminal arrows and producing no
meaningful visual regression in side-by-side inspection. Compact-lite reduced
the same compact view to 116,887 bytes, with a deliberate visible density
change. The split two-step sample index is 2,527 gzip bytes and each
three-profile step manifest is about 4.7 KB; a complete index grows only with
small step descriptors.

The split package remains integrity-checked: the index declares the exact
SHA-256 and byte length of every step document, each step document declares
the exact SHA-256 of every tile, and XWS2 retains its payload CRC32 and strict
identity checks. Step documents and tiles are immutable; only the small root
index revalidates.

## Prototype

The backend prototype:

- reads the existing int8 interleaved U/V step;
- uses the frontend's global Mercator seed lattice, bilinear sampler, midpoint
  integration, desktop/mobile trajectory duration, and terminal arrow speed;
- clips polylines to the presentation viewport as a lower bound for visible
  vector-tile payload;
- preserves the terminal segment for exact arrow direction;
- applies Douglas-Peucker simplification in presentation pixels;
- quantizes the full ICON domain to uint16 coordinates;
- emits a small delta/zigzag-varint `XWS1` bundle.

The frontend prototype validates and decodes `XWS1`, draws its paths and open
arrowheads, and benchmarks that result against the production integrator on
the same real step and numeric projection snapshot.

The second-stage prototype generates real XYZ-partitioned full-domain packages,
loads every tile intersecting the viewport, joins continuation fragments, and
checks pixel error around tile boundaries. Neither prototype is connected to a
production manifest or exporter.

## Representative result

Input:

- ICON-CH1 run `20260729_1200`;
- `800m_AGL`, `H00`;
- current 391 × 191 U/V field;
- 0.15 CSS-pixel simplification tolerance;
- geometry clipped to the visible Switzerland view.

| Measure | Desktop 1024 × 640 | Mobile 411 × 520 |
| --- | ---: | ---: |
| Source U/V raw | 149,362 B | 149,362 B |
| Source U/V Brotli | 74,892 B | 74,892 B |
| XWS1 raw | 63,733 B | 73,639 B |
| XWS1 Brotli | 39,481 B | 48,081 B |
| Decoded paths | 3,255 | 3,136 |
| Decoded points | 14,847 | 17,475 |
| Current integrate + draw median | 61.0 ms | 149.6 ms |
| XWS1 cold decode + draw median | 6.7 ms | 6.8 ms |
| XWS1 cached draw median | 5.6 ms | 6.4 ms |
| XWS1 decode median | 2.0 ms | 2.2 ms |

The current production renderer uses an overscanned cache, so its accepted
physical-phone result remains the more relevant end-user baseline: 145.8 ms
worker rendering and approximately 196 ms to the next complete Wind frame.
This local experiment isolates geometry feasibility rather than claiming a new
physical-device result.

The 0.15-pixel candidate produced no observable difference in side-by-side
inspection. Pixel RMSE was 2.57 desktop and 3.06 mobile on a 0–255 RGBA scale.
The raw changed-pixel percentage is intentionally not used as an acceptance
metric because subpixel antialias changes mark many thin-line pixels despite
the visually matching result.

## Why clipping and simplification are required

The first exact whole-cache bundle was not viable:

| Candidate | Desktop Brotli | Mobile Brotli | Cold decode + draw |
| --- | ---: | ---: | ---: |
| Exact overscanned geometry | 297,466 B | 626,418 B | 90 / 176 ms |
| 0.35 px simplified cache | 113,535 B | 186,159 B | 20 / 25 ms |
| 0.15 px simplified visible tile-equivalent | 39,481 B | 48,081 B | 6.7 / 6.8 ms |

The exact bundle still submitted 381,323 desktop or 837,273 mobile points to
Canvas. Vector-tile-style clipping removes offscreen paths, and subpixel
simplification reduces the visible result to roughly 15,000–17,500 points.

## Horizon sampling

The 0.35-pixel clipped experiment also sampled CH1 `H00`, `H12`, `H24`, and
`H33`, plus CH2 `H060` and `H120`.

- Desktop gzip payloads ranged from 33.8 to 41.3 KB.
- Mobile gzip payloads ranged from 38.2 to 48.4 KB.
- Output path counts remained between 3,042 and 3,345.

The H00 result is therefore not unusually small or sparse.

## Full-domain tile result

The real tile package uses:

- `compact-default`: compact/mobile geometry partitioned at XYZ z6;
- `wide-default`: desktop geometry partitioned at XYZ z7;
- 512-pixel tile simplification coordinates;
- no overlap buffer, so geometry is stored exactly once apart from the shared
  boundary vertex;
- an experimental `0.1.0` package manifest with immutable tile paths and
  per-tile byte/count metadata.

Full-domain output:

| Step | Profile | Tiles | Raw total | Gzip total |
| --- | --- | ---: | ---: | ---: |
| H00 | compact z6 | 9 | 519,956 B | 387,326 B |
| H00 | wide z7 | 30 | 486,297 B | 347,967 B |
| H24 | compact z6 | 9 | 585,034 B | 447,018 B |
| H24 | wide z7 | 30 | 535,306 B | 394,557 B |

The default Switzerland view fetches two compact tiles or six wide tiles:

| Step | Profile | View Brotli | Current draw | Stitched cold decode + draw | Cached draw |
| --- | --- | ---: | ---: | ---: | ---: |
| H00 | compact z6 | 121,232 B | 145.9 ms | 29.7 ms | 13.4 ms |
| H00 | wide z7 | 90,242 B | 64.1 ms | 22.8 ms | 11.8 ms |
| H24 | compact z6 | 157,303 B | 142.3 ms | 30.9 ms | 13.2 ms |
| H24 | wide z7 | 114,026 B | 64.7 ms | 22.9 ms | 11.3 ms |

The endpoint-string stitcher is deliberately diagnostic, not the proposed
production implementation. It makes seam-strip RMSE lower than whole-image
RMSE in all four cases (for H00: 2.40 versus 3.19 compact and 1.97 versus 2.51
wide), but spends about 20–27 ms in decode/stitch. `XWS2` should carry a compact
stable path ID plus start/end continuation flags, allowing numeric stitching
of only boundary fragments.

The finer alternatives (compact z7 and wide z8) do not improve transfer for the
reference view. H00 Brotli increases to 126,190 B and 94,559 B, while package
object counts rise from 9/30 to 30/108. Keep z6/z7 for the pilot.

The tiled payload is larger than the perfectly viewport-clipped lower bound
because each request includes geometry outside the exact viewport. It is also
additional to the retained U/V field, which remains necessary for colours,
point inspection, unsupported zooms, and fallback. The result buys roughly a
3× desktop and 5× mobile cold CPU improvement after seam-safe stitching; cached
draw is roughly 5–11× faster.

## Backend and storage budget

The intentionally straightforward Python prototype takes approximately:

- 2.8–3.0 seconds for a desktop Switzerland surface;
- 7.1–7.4 seconds for a mobile Switzerland surface;
- 59–60 seconds for both full-domain LODs including clipping, simplification,
  encoding, and gzip on one process.

The sampled CH1 run contains 34 forecast steps. Serial prototype generation for
one level is therefore about 34 minutes, and the two LODs retain approximately
25–29 MB gzip per level based on H00/H24. That is acceptable for an isolated
beta artifact but not yet for eight levels by default.

A production generator should parallelize independent steps in a bounded
worker pool, avoid repeated projection during tile clipping, and emit Brotli
alongside gzip. Deriving both LODs from one dense integration is possible only
if stable seed identities preserve the current profile-specific lattice and
trajectory semantics; it must be validated rather than assumed.

## Proposed optional contract

Add one optional `streamline_tiles` capability to the level metadata:

```json
{
  "contract": "xcbenz-wind-streamline-tiles",
  "contract_version": "0.2.0-beta",
  "format": "XWS2",
  "profiles": {
    "compact-default": {
      "tile_zoom": 6,
      "template": "streamlines/{step}/compact-default/z6/{x}_{y}.xws"
    },
    "wide-default": {
      "tile_zoom": 7,
      "template": "streamlines/{step}/wide-default/z7/{x}_{y}.xws"
    }
  }
}
```

`XWS2` must add stable path identity and continuation flags before production
wiring. The capability is all-or-nothing per step/profile: clients must not
combine geometry revisions or silently use partial tile sets. Missing,
malformed, or unsupported capability data falls back to the current U/V worker.

## Rollout and acceptance gates

1. **Contract implementation:** add XWS2 IDs/flags, deterministic backend
   output, decoder limits, corruption tests, and tile-selection tests. Do not
   publish a capability yet.
2. **Shadow export:** generate all 34 CH1 `800m_AGL` steps for one run without
   exposing them to clients. Require at most 1,400 XWS objects, 35 MB compressed
   geometry, and 12 minutes wall time with a bounded four-worker export.
3. **Beta2 opt-in:** publish the optional capability behind a client flag.
   Require reference-view Brotli at or below 125 KB wide and 175 KB compact,
   local cold decode/stitch/draw below 35 ms wide and 45 ms compact, and cached
   median draw below 16.7 ms.
4. **Visual and device gate:** require seam-strip RMSE no worse than whole-image
   RMSE, no visible discontinuity during scripted pans, and a physical-phone
   next-complete-step latency below 100 ms after tile bytes are cached.
5. **Default for one level:** enable only `800m_AGL` after two consecutive runs
   pass all gates. Keep the worker as a silent per-step fallback and record
   capability/fallback telemetry.
6. **Expansion:** evaluate other levels and CH2 separately. Do not infer their
   storage, generation, or mobile results from CH1.

## Pilot requirements

1. Implement `XWS2` stable path IDs and continuation flags, with numeric
   boundary-fragment stitching and a strict decoder.
2. Publish an optional immutable capability for one model/run revision and
   `800m_AGL`, limited to compact z6 and wide z7.
3. Keep the U/V value field for colour rendering, inspection, and fallback.
4. Commit a Wind step only when value and matching streamline identities are
   both ready.
5. Fall back to the existing worker for absent, malformed, unsupported-view,
   unsupported-level, or unsupported-zoom geometry.
6. Measure cold transfer, decode, draw/upload, pan seams, visual equivalence,
   and physical-phone step latency on beta2.
7. Expand zoom bands, domains, and levels only after generator time, retained
   bytes, artifact count, and phone measurements pass explicit budgets.

## Reproduction

Backend:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/benchmark_wind_streamline_bundle.py \
  /home/sebas/projects/XCBenz_Data_Parallel/web_exports/wind_maps/icon-ch1/20260729_1200/800m_AGL/metadata.json \
  --step H00 \
  --profiles desktop-view,mobile-view \
  --simplify-px 0.15 \
  --clip-to-view \
  --output-dir /tmp/xcbenz-wind-streamline-feasibility
```

Frontend:

```bash
npm run perf:streamline-feasibility -- \
  --bundle /tmp/xcbenz-wind-streamline-feasibility/h00-mobile-view-s015-clip.xws
```

Full-domain tiles:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/benchmark_wind_streamline_tiles.py \
  /home/sebas/projects/XCBenz_Data_Parallel/web_exports/wind_maps/icon-ch1/20260729_1200/800m_AGL/metadata.json \
  --step H00 \
  --output-dir /tmp/xcbenz-wind-streamline-tiles/h00

npm run perf:streamline-tiles -- \
  --package /tmp/xcbenz-wind-streamline-tiles/h00/metadata.json \
  --profile wide-default
```
