# Spatial Value Tiles v1 Backend Migration

Status: implementation branch only. Production rollout is not authorized.

The backend derives spatial value tiles from the existing whole-grid browser
files after those files have been generated. This keeps Wind, Sun/Rain, Rain,
and Cloud values byte-for-byte compatible and leaves every existing path in
place for frontend fallback.

## Feature gate

Generation is disabled by default. A no-deploy run can opt in with:

```text
ENABLE_VALUE_TILES=true
```

When enabled, `generate_web_exports.py` writes and fully validates
`web_exports/value_tiles/v1/` before adding
`capabilities.spatial_value_tiles` to `web_exports/manifest.json`. Any tile,
revision, metadata, or reassembly failure fails the candidate generation before
publication. When disabled, the root capability is absent and existing
whole-grid behavior is unchanged.

`scripts/apply_web_retention.py` merges old and staged value-tile indexes,
retains the same model runs as the whole-grid products, deletes unreferenced
revisions, validates every retained tile, and advertises the capability only
when the retained index is valid.

## Safe validation sequence

Run the narrow contract tests first:

```bash
python -m unittest tests.test_value_tiles -v
```

Then run the normal repository tests and output validation. A representative
pipeline invocation must keep deployment and data-branch pushing disabled:

```bash
ENABLE_VALUE_TILES=true uv run python scripts/run_coding_server_pipeline.py \
  --skip-deploy \
  --no-push-data-branch \
  --ch1-run-tag <pinned-complete-run> \
  --ch2-run-tag <pinned-complete-run>
```

Do not enable the production Coding Server timer with the feature flag until
the generated file-count, traversal, deletion, and validation measurements
have been reviewed.

## Local acceptance measurements

Measurements from 2026-07-16 on the local Windows workspace:

| Fixture | Result |
| --- | ---: |
| One model-run, one step for one Wind level, Sun/Rain, Rain, four individual Cloud layers, and `cloud4` | 96 `.xvt` files |
| XVT generation, excluding the separate validation pass | 0.322 seconds |
| Full hash, parse, reassembly, CRC, halo, and padding validation | 0.141 seconds |
| Raw XVT bytes | 1,450,560 bytes |
| Deterministic gzip level 9 bytes | 38,970 bytes |
| Modeled retained file fixture creation | 66,144 files in 51.349 seconds |
| Modeled retained file traversal | 66,144 files in 5.499 seconds |
| Modeled retained file deletion | 66,144 files in 23.943 seconds |

The retained-file fixture uses one-byte payloads and realistic directory depth,
so it measures filesystem operation cost rather than forecast compression or
network transfer. These results prove the fixture and deletion path locally.
They do not satisfy the remaining Coding Server generation, rsync traversal,
Infomaniak staging swap, or remote deletion acceptance checks.

Reproduce the local retained-file measurement with:

```bash
python scripts/benchmark_value_tile_filesystem.py --files 66144 --payload-bytes 1
```

## Host staging

`deploy/infomaniak-value-tiles-staging.htaccess` is a complete staging-only
Apache configuration with:

- an explicit binary MIME mapping for `.xvt`;
- gzip and Brotli filter inclusion for complete `.xvt` responses;
- one-year immutable caching below revision-scoped paths;
- no-cache behavior for both manifest files.

The production deployment script does not upload this file. Applying it to any
remote path requires separate approval. After an approved staging upload, run
`scripts/validate_remote_web_exports.py` against that staging base URL. When a
tile capability is present, the validator checks manifest caching, immutable
metadata and tile caching, MIME type, XVT parsing, and the selected tile hash.

## Frontend discovery and fallback

The frontend reads `capabilities.spatial_value_tiles` from the existing root
manifest. It uses tiles only for supported contract major version 1 and package
`immutable-chunks-cloud-dual-v1`. Absence or validation failure keeps the
existing whole-grid reader active. A failed tile frame must fall back as a
whole frame and must not mix tiled and whole-grid values.

The capability can remain disabled while backend artifacts are exercised in a
local or separately approved staging environment. Production capability
activation and frontend rollout are separate reviewed changes.
