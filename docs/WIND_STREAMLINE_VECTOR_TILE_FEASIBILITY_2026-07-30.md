# Wind streamline vector-tile feasibility — 2026-07-30

## Decision

Proceed with a narrow static vector-tile pilot for the default Switzerland
view and `800m_AGL`. Do not yet enable full-domain, all-level generation.

The experiment proves that backend-integrated streamline geometry can remove
almost all browser trajectory work when it is clipped like a visible vector
tile and simplified below the display's pixel resolution. It does not yet
prove that generating and retaining every zoom band, level, model, and horizon
fits the production pipeline budget.

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

Neither prototype is connected to a production manifest or exporter.

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

## Backend budget

The intentionally straightforward Python prototype takes approximately:

- 2.8–3.0 seconds for a desktop Switzerland surface;
- 7.1–7.4 seconds for a mobile Switzerland surface.

Generating profiles and zoom bands independently across the full ICON domain
would be too expensive. A production generator must integrate the densest,
longest required lattice once, derive lower-density/shorter LODs from stable
seed IDs, parallelize by step/level within a bounded worker pool, and measure
the complete pipeline wall time before expanding beyond the pilot.

## Pilot requirements

1. Publish an optional immutable capability for one model/run revision,
   `800m_AGL`, and the default zoom band.
2. Use stable global seed IDs and tile clipping with a buffer so panning has no
   seams or duplicated arrowheads.
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
