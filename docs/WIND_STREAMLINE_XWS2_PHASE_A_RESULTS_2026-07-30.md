# Wind streamline XWS2 Phase-A results

- Date: 2026-07-30
- Scope: ICON-CH1 `20260729_1200`, `800m_AGL`, H00–H33
- Host: Hetzner Coding Server
- Status: optimized backend shadow generator implemented; production
  advertisement remains disabled

## Result

The backend-generation feasibility risk is resolved. The optimized four-worker
XWS2 shadow build completed all 34 steps in **127.6 seconds**, not 34 minutes.
It stayed within the preferred memory, size, disk, and object-count gates.

The 34-minute estimate described the first serial Python prototype. It is no
longer representative because the implementation now:

1. integrates all active seed trajectories in float64 NumPy batches;
2. projects each trajectory to normalized Web Mercator once;
3. simplifies each source trajectory once before tile splitting;
4. walks segments across XYZ boundaries in one pass;
5. generates independent forecast steps in a bounded process pool; and
6. limits BLAS/OpenMP/MKL to one native thread per worker.

The vectorized H00 output was byte-for-byte identical to the scalar XWS2
output. Complete packages were also byte-for-byte identical across four and
six workers.

## Implemented artifacts

- `wind_streamline_tiles.py`
  - stable lattice-derived numeric path IDs;
  - scalar reference integrator;
  - vectorized float64 midpoint integrator;
  - normalized Web Mercator trajectories;
  - pre-partition simplification;
  - exact XYZ boundary splitting and ordered fragments;
  - XWS2 encoding, CRC32, and strict decoding;
  - deterministic revision and manifest generation;
  - phase timings, counters, runtime identity, and per-worker RSS evidence;
  - bounded one-to-six-worker process pool;
  - owned temporary build cleanup and atomic shadow-directory promotion.
- `scripts/build_wind_streamline_shadow.py`
  - standalone, non-advertised shadow exporter.
- `scripts/run_coding_server_pipeline.py`
  - default-off `ENABLE_WIND_STREAMLINE_SHADOW` post-base hook;
  - runs only after the normal local validation and, in deploy mode, after the
    base deploy and remote validation;
  - writes evidence below the pipeline run directory;
  - never mutates the root capability or live forecast artifacts;
  - treats shadow failure as nonfatal after preserving the normal base result.
- `tests/fixtures/wind_streamline_tiles/canonical_xws2.json`
  - canonical XWS2 bytes, decoded contract, SHA-256, and revision fixture.

## Performance progression

Real H00, both compact z6 and wide z7 profiles:

| Implementation | Wall time | Main remaining cost |
| --- | ---: | --- |
| Initial XWS2 scalar pipeline | 53.0 s | integration and per-tile processing |
| Simplify once before splitting | 40.9 s | scalar integration |
| Vectorized integration, NumPy row fragments | 20.9 s | Python simplification over NumPy scalars |
| Vectorized integration, tuple path boundary | **14.5 s** | simplification and partition |

For the final vectorized implementation, H00+H24 took:

| Workers | Wall time | Aggregate worker peak RSS | Revision |
| ---: | ---: | ---: | --- |
| 1 | 28.37 s | 414,429,184 B | `e394cf36ae457816` |
| 2 | 15.55 s | 808,390,656 B | `e394cf36ae457816` |

The two packages had identical manifests and raw tiles.

Complete H00–H33:

| Workers | Wall time | Aggregate worker peak RSS | Maximum one-worker RSS |
| ---: | ---: | ---: | ---: |
| 4 | **127.60 s** | **1,694,605,312 B** | 428,302,336 B |
| 6 | 85.23 s | 2,532,151,296 B | 426,360,832 B |

Both complete packages had revision `dce27e998a69c9e6`, identical manifests,
and identical raw tiles. Four workers remains the recommended default: it is
42 seconds slower than six but stays below the preferred 2 GiB aggregate RSS
gate and leaves more headroom for the forecast service.

Four-worker per-step timing distribution:

| Measure | Minimum | Median | p95 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| Total step | 12.29 s | 14.06 s | 16.31 s | 16.53 s |

Median phase costs:

| Profile | Integrate | Partition/simplify | Encode/write |
| --- | ---: | ---: | ---: |
| Compact z6 | 1.72 s | 5.76 s | 0.82 s |
| Wide z7 | 1.29 s | 3.45 s | 0.83 s |

Partition/simplification is now the largest phase. It is no longer a release
blocker, so a compiled dependency is not justified for the pilot.

## Package measurements

The four-worker complete shadow package contains:

| Measure | Result |
| --- | ---: |
| Forecast steps | 34 |
| Profiles | 2 |
| XWS2 tiles | 1,326 |
| Raw XWS2 bytes | 40,756,536 B |
| Local gzip-9 estimate | 27,560,861 B |
| Filesystem package size, including JSON | 41,479,976 B |
| Per-step gzip range | 719,233–944,729 B |
| Largest gzip tile | 109,605 B |

The gzip number is a deterministic local sizing measurement, not proof of
network transfer size. CDN/data-host `Content-Encoding`, cache headers, MIME
type, and transferred bytes still require the Phase-C staging check.

## Gate status

| Backend gate | Target | Result | Status |
| --- | ---: | ---: | --- |
| 34-step wall time | ≤ 5 min | 2 min 7.6 s | Pass |
| Aggregate peak RSS | ≤ 2 GiB | 1.58 GiB | Pass |
| One-level compressed corpus | ≤ 30 MB | 27.56 MB local gzip | Provisional |
| One-level retained disk | ≤ 75 MB | 41.48 MB | Pass |
| Logical XWS objects | ≤ 1,350 | 1,326 | Pass |
| Deterministic raw output | Required | scalar/vector and worker-count parity | Pass |
| Capability absent by default | Required | no capability implementation or mutation | Pass |
| Base publication regression | ≤ 30 s | post-base hook implemented, live run not measured | Open |

The backend CPU/RSS/storage feasibility is a pass. Phase A is not a production
rollout approval: live base-publication timing, real data-host compression, the
frontend numeric join/draw path, visual seam checks, injected failure fallback,
and physical-phone measurements remain open.

## Verification

- Canonical XWS2 fixture locks exact bytes, SHA-256, decode result, and revision.
- Scalar projected integration matches the existing geographic integrator on a
  uniform-field oracle fixture.
- Vectorized integration matches scalar path counts, point counts, and
  endpoints in the oracle test.
- Real H00 vectorized raw tiles match the real scalar XWS2 tiles byte-for-byte.
- Shared tile-boundary vertices quantize to identical integers.
- Tile re-entry produces unambiguous monotonic fragment order.
- Header, profile/tile identity, flags, limits, duplicate identities, trailing
  bytes, coordinate domain, and CRC are strictly validated.
- Worker-count determinism is covered by a generated test package and the real
  four-versus-six-worker complete package comparison.
- The full backend suite passes: **146 tests**.
- `compileall` and `git diff --check` pass.

## Reproduction

```bash
OMP_NUM_THREADS=1 \
OPENBLAS_NUM_THREADS=1 \
MKL_NUM_THREADS=1 \
uv run python scripts/build_wind_streamline_shadow.py \
  web_exports/wind_maps/icon-ch1/20260729_1200/800m_AGL/metadata.json \
  --output-dir /tmp/xcbenz-xws2-shadow-full-w4 \
  --workers 4
```

Pipeline shadow mode remains explicit:

```bash
ENABLE_WIND_STREAMLINE_SHADOW=true \
WIND_STREAMLINE_SHADOW_WORKERS=4 \
scripts/run_coding_server_pipeline.py ...
```

The pipeline evidence is written to:

```text
{run_dir}/wind-streamline-shadow.json
```

The generated package stays below:

```text
{run_dir}/wind_streamline_shadow/icon-ch1/{run}/800m_AGL/
```

It is not copied into `web_exports`, deployed, or advertised.
