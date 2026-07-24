# Spatial Value Tiles v1 Backend Migration

Status: production dual publication active since 2026-07-16. Whole-grid files
remain the supported fallback and must not be removed before the observation
gate passes and removal is separately approved.

The implemented package contract is defined by `value_tiles.py` and verified by
`tests/test_value_tiles.py`. This document records the durable migration,
rollout, and acceptance context rather than duplicating the implementation.

The backend derives spatial value tiles from the existing whole-grid browser
files after those files have been generated. This keeps Wind, Sun/Rain, Rain,
and Cloud values byte-for-byte compatible and leaves every existing path in
place for frontend fallback.

## Processing model and historical runs

Value-tile generation is an additional packaging pass, not a new forecast
calculation. The normal pipeline remains:

1. Download and decode the selected MeteoSwiss run.
2. Produce the existing whole-grid Wind, Sun/Rain, Rain, and Cloud browser
   files with their existing precision and encodings.
3. Read those completed whole-grid files and split them into haloed `.xvt`
   files.
4. Validate tile hashes, headers, encodings, complete-grid reconstruction,
   halos, and padding.
5. Add the optional capability only after validation succeeds.

The tile pass does not download or decode the model a second time and does not
change the upstream map accumulators. It adds filesystem reads, tile encoding,
validation, and publication work after the current browser files exist.

Each newly processed forecast run receives tiles during its normal pipeline
run when `ENABLE_VALUE_TILES=true`. Retention merges the new immutable revision
with still-retained earlier revisions. Historical backfill is not required for
rollout: when a selected run has no tile entry, the frontend uses its existing
whole-grid files. If historical tiles are later wanted, package only complete
retained whole-grid runs in an isolated candidate tree. Do not redownload or
recalculate model data merely to backfill tiles.

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

Disabled mode is authoritative during retention. It does not copy or merge
staged tile artifacts, removes any previously retained `value_tiles` tree, and
rebuilds the root manifest without `capabilities.spatial_value_tiles`. This
makes the next candidate an immediate rollback to whole-grid-only publication.

## WP4 execution record

The reviewed WP4 sequence is complete through production activation:

1. The contract, encoder, dual-generation path, validation, retention, guarded
   publisher, production MIME/cache rules, and disk high-water telemetry are on
   `main`.
2. Staging proved a second revision, enabled publication, disabled rollback,
   enabled restoration, atomic swaps, deletion, and whole-grid coexistence.
3. The frontend reader passed beta2 desktop and mobile acceptance with strict
   capability/version checks and complete-frame whole-grid fallback.
4. The capability-aware frontend was deployed to `https://xcbenz.com` at commit
   `ffd2904` on 2026-07-16.
5. `ENABLE_VALUE_TILES=true` was set in both the Coding Server protected
   production environment and the GitHub Actions repository variable.
6. The first production dual-publication run completed for CH1 and CH2
   `20260716_1800`, followed by local validation, guarded Infomaniak publication,
   remote validation, and real-browser rendering.

Live stations, webcams, radar maps, airspace, and FAI records remain independently owned.
Keep the legacy reader and whole-grid outputs for at least 48 hours and four
successful production publication cycles. Their removal requires fresh review
and explicit approval.

## Exact staged acceptance plan

This plan has four gates. A gate must pass before the next gate starts. No step
publishes to or writes production forecast data. The temporary production timer
pause in Gate 1 is a separately approved operational change.

### Gate 0: finish staging safety controls

Before connecting to Infomaniak:

1. Verify the authoritative disabled behavior described above.
2. Adjust the staging Apache rules so immutable revision matching works below
   `/value-tiles-staging/web_exports/`, not only `/web_exports/`.
3. Add `scripts/deploy_value_tiles_staging_infomaniak.sh` as a dedicated
   staging publisher. It must refuse every remote root except
   `sites/data.xcbenz.com/value-tiles-staging`, use a staging-specific lock and
   rollback directory, upload the staging `.htaccess`, and never call the
   production publisher as a shortcut.
4. The staging publisher must upload a complete `web_exports` candidate so the
   existing whole-grid fallback remains usable. For later beta2 testing, it may
   copy the independently owned `live_stations`, `webcams`, `radar_maps`,
   `airspace`, and `fai_records` trees from production into the staging candidate
   without changing their production sources.
5. Run the narrow and complete repository tests. Do not proceed with an
   uncommitted candidate.

Run the narrow contract tests first:

```bash
python -m unittest discover -s tests -p 'test_value_tiles.py' -v
```

Then run:

```bash
python -m unittest discover -s tests -v
```

### Gate 1: isolated Coding Server run

Commit Gate 0, then push the clean feature branch directly. A pull request is
not required for this staging exercise:

```bash
git push -u origin codex/value-tiles-backend-v1
```

On the Coding Server, fetch it and create a detached test worktree so the
production `main` checkout remains untouched:

```bash
git -C /home/sebas/projects/XCBenz_Data_Parallel fetch origin
git -C /home/sebas/projects/XCBenz_Data_Parallel worktree add --detach \
  /home/sebas/projects/XCBenz_Data_Parallel-value-tiles \
  origin/codex/value-tiles-backend-v1
cd /home/sebas/projects/XCBenz_Data_Parallel-value-tiles
```

Use the poller only to identify the latest complete pair and print the pinned
runner command. Give this test its own poller state and poller lock:

```bash
ENABLE_VALUE_TILES=true \
WEB_EXPORT_DATA_ROOT=https://data.xcbenz.com/value-tiles-staging \
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/poll_coding_server_pipeline.py \
  --plan-only \
  --force-run \
  --state-file .local_pipeline/value-tile-staging-poller-state.json \
  --lock-file /run/lock/xcbenz-value-tile-staging-poller.lock \
  --skip-deploy \
  --no-push-data-branch
```

The heavy runner uses the same default lock as production. Running a second
heavy job concurrently risks resource contention, while a lock-skipped runner
can be recorded by the poller as successful. Therefore obtain separate
operational approval for a short timer pause immediately before the test. First
verify that the production service is inactive. Treat `active`, `activating`,
and `deactivating` as busy states. Wait for every busy state to finish and do
not stop the service:

```bash
systemctl --user is-active xcbenz-coding-server-production.service
systemctl --user is-active xcbenz-coding-server-production.timer
```

Record the complete CH1 and CH2 tags printed by the plan-only command. Keep
restore enabled so retention is exercised against a realistic prior
`web_exports` tree. Run the timer pause and candidate in one shell with a trap
that restarts the timer after success, failure, or interruption:

```bash
set -e
timer=xcbenz-coding-server-production.timer
service=xcbenz-coding-server-production.service
restart_timer() {
  systemctl --user start "$timer"
}
wait_for_service() {
  state="$(systemctl --user is-active "$service" || true)"
  while [ "$state" != inactive ] && [ "$state" != failed ]; do
    sleep 15
    state="$(systemctl --user is-active "$service" || true)"
  done
}
trap restart_timer EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

wait_for_service
systemctl --user stop "$timer"
wait_for_service

ENABLE_VALUE_TILES=true \
WEB_EXPORT_DATA_ROOT=https://data.xcbenz.com/value-tiles-staging \
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --skip-deploy \
  --no-push-data-branch \
  --ch1-run-tag <pinned-complete-ch1-run> \
  --ch2-run-tag <pinned-complete-ch2-run>

restart_timer
trap - EXIT HUP INT TERM
systemctl --user is-active "$timer"
```

This pause changes production scheduling and therefore always requires explicit
approval at execution time. It does not enable the tile flag in the production
service environment and does not deploy the candidate.

Gate 1 passes only if:

- `scripts/validate_outputs.py` passes;
- no deployment or data-branch push occurs;
- the root and tile manifests declare the expected capability and counts;
- all expected whole-grid files remain present;
- there are no `.nc` files in `web_exports`;
- file count, tile bytes, generation time, validation time, peak disk use, and
  retained-tree traversal time are recorded; and
- restarting the production timer is verified.

### Gate 2: isolated Infomaniak data staging

The proposed staging namespace is:

```text
Remote root: sites/data.xcbenz.com/value-tiles-staging
Public base: https://data.xcbenz.com/value-tiles-staging
Candidate:   https://data.xcbenz.com/value-tiles-staging/web_exports/
```

After separate staging-publication approval, run only the dedicated guarded
staging publisher created in Gate 0:

```bash
INFOMANIAK_VALUE_TILE_STAGING_ROOT=sites/data.xcbenz.com/value-tiles-staging \
DATA_HOST_BASE_URL=https://data.xcbenz.com/value-tiles-staging \
WEB_EXPORT_DIR=web_exports \
bash scripts/deploy_value_tiles_staging_infomaniak.sh
```

The host, user, port, and SSH key continue to come from the Coding Server's
protected environment. The publisher must perform an atomic candidate swap
inside that staging root and leave `sites/data.xcbenz.com/web_exports`
untouched. Record rsync traversal time, uploaded bytes, swap time, and retained
file count.

Validate the staged candidate from the Coding Server:

```bash
DATA_BASE_URL=https://data.xcbenz.com/value-tiles-staging \
uv run python scripts/validate_remote_web_exports.py
```

Also inspect representative responses from a real browser. Ordinary `.xvt`
chunks are complete objects, so the successful response is `200`, not `206`.
They may use HTTP gzip or Brotli. Gate 2 requires:

- both manifest files are `no-cache`;
- revision-scoped metadata and `.xvt` files are immutable for one year;
- `.xvt` has an approved binary MIME type;
- browser fetch and XVT parsing succeed with normal content encoding;
- the fetched bytes match the revision SHA-256 record;
- the production `web_exports` manifest and a representative production file
  are unchanged; and
- an atomic second staging publication never exposes a mixed revision.

On Infomaniak, keep the immutable revision-path override inside the same
`FilesMatch` section as the generic one-hour cache rule, after that generic
rule. Apache processes file sections after directory-level header directives,
so an override outside the section is replaced by the generic rule even when
it appears later in the `.htaccess` source.

Atomic replacement and deletion cost require a second candidate with a
different value-tile revision. Repeat Gate 1 for the next complete pinned run,
publish it to the same staging namespace, and record swap time plus deletion
time for the superseded staging revision. Republishing identical files does not
satisfy this check.

### Gate 3: beta2 end-to-end frontend test

The website is not needed for Gates 1 and 2. Those gates prove generation,
retention, publication, host behavior, and remote parsing. Beta2 becomes useful
only after the frontend tile reader exists.

Build and deploy the beta2 frontend against the staging data base:

```powershell
.\scripts\deploy-beta2.ps1 `
  -DataBaseUrl "https://data.xcbenz.com/value-tiles-staging"
```

The beta2 candidate must retain whole-grid fallback. Measure in a real browser:

- compressed bytes and request count for each initial selector view;
- nearby-pan cache reuse;
- incremental zoom-out and All Alps;
- Wind-level switching;
- individual Cloud layers, `cloud4`, and Cloud plus Rain;
- behavior after one tile is unavailable or corrupt; and
- confirmation that a failed frame uses one complete whole-grid fallback and
  never mixes tile and whole-grid values.

### Promotion and rollback decision

Review all measurements before production activation. Promotion is two
separate decisions:

1. Merge the backend, explicitly enable tiles in the production Coding Server
   environment, and publish the capability while the current frontend continues
   using whole-grid files.
2. Promote the tested frontend reader after backend observation is satisfactory.

Rollback must be proven in staging before either decision. Turning the tile
flag off and publishing the next candidate must remove the root capability,
leave all whole-grid products working, and make beta2 use its whole-grid reader.
No production activation is part of this plan.

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

## Measured Coding Server and Infomaniak staging acceptance

Measurements from 2026-07-16 use pinned CH1 `20260716_1500` and CH2
`20260716_1200` runs. The no-deploy Coding Server candidate completed in 9
minutes 29 seconds at commit `2bde5f6` after all 12 fetch jobs, including the
run-dependent CH1 `H017_H033` profile chunk, completed successfully.

| Coding Server result | Measurement |
| --- | ---: |
| Retained value-tile runs / variants / XVT files | 2 / 30 / 27,876 |
| Complete value-tile tree, including manifests and indexes | 27,909 files, 355,485,245 bytes |
| Complete retained `web_exports` tree | 989,506,464 bytes |
| Value-tile tree traversal | 0.03 seconds |
| NetCDF files below `web_exports` | 0 |
| Final isolated worktree footprint | 2,496,302,832 bytes |

The final worktree footprint is an observed final value, not an instrumented
peak. A future runner telemetry change is still needed if an exact peak disk
high-water mark is required.

The first staging publication correctly rolled back after the real remote
validator found that Apache section ordering caused revision metadata to retain
the generic one-hour cache policy. Commit `aa3d350` moves the immutable override
inside the same `FilesMatch` section after the generic rule. The retry then
passed local validation, atomic publication, remote XVT parsing and SHA-256
validation, and the production-manifest before/after checksum guard.

| Infomaniak staging result | Measurement |
| --- | ---: |
| Logical candidate bytes | 989,506,464 |
| Rsync bytes sent / received | 423,850,998 / 671,902 |
| Candidate upload | 11 seconds |
| Atomic staging switch | less than 1 second |
| Published staging file count, including copied live-owned snapshots | 35,175 |
| Superseded staging deletion during this publication | less than 1 second |

Observed public response behavior:

| Object/request | Status | Bytes | Relevant headers |
| --- | ---: | ---: | --- |
| Root manifest with gzip | 200 | 55,842 | `no-cache`, JSON, CORS `*` |
| Tile manifest with gzip | 200 | 701 | `no-cache`, JSON, CORS `*` |
| Revision metadata with gzip | 200 | 2,753 | one-year immutable, JSON, CORS `*` |
| XVT with identity | 200 | 9,576 | one-year immutable, octet-stream, CORS `*` |
| XVT with gzip | 200 | 7,463 | one-year immutable, octet-stream, CORS `*` |
| XVT identity Range `0-63` | 206 | 64 | `Content-Range: bytes 0-63/9576` |
| XVT gzip Range `0-63` | 206 | 64 | ranges the 7,463-byte HTTP-gzip representation |

The gzip Range result does not qualify a Range archive. Browsers control
`Accept-Encoding`, and a partial HTTP-gzip representation is not a stable byte
range into the archive. A future archive path would still have to disable HTTP
content encoding, use independently compressed internal blocks, and pass a real
browser `206` test. Ordinary XVT chunks use complete `200` responses and are not
affected.

The available controlled Chrome and in-app browser surfaces both returned
client-side `ERR_BLOCKED_BY_CLIENT` for direct navigation to the public staging
JSON URL. No server request failed, and command-line plus Python HTTPS
validation passed. Browser integration remains a beta2 frontend gate. No
production data or production configuration was changed.

### Second revision and rollback acceptance

The second isolated run used CH1 `20260716_1800` and CH2 `20260716_1200` at
commit `da9df46`. It completed in 9 minutes 31 seconds with no deploy and no
data-branch push. CH1 revision `197c50018692` was genuinely different from the
retained CH1 `20260716_1500` revision `d4f31ce1c6ca`. CH2 retained revision
`7d2a80a5046d`.

| Second Coding Server result | Measurement |
| --- | ---: |
| Retained value-tile runs / variants / XVT files | 3 / 45 / 33,984 |
| Complete value-tile tree, including manifests and indexes | 34,033 files, 433,365,462 bytes |
| Complete candidate tree | 40,178 files, 1,073,666,672 bytes |
| Existing whole-grid step files retained | 4,400 |
| NetCDF files below `web_exports` | 0 |
| Filesystem used baseline / peak / delta | 86,580 / 89,351 / 2,770 MB |

The same validated candidate then passed the complete enabled, disabled, and
enabled staging sequence:

| Staging state | Upload | Bytes sent / received | Prior-tree deletion | Published files | Remote result |
| --- | ---: | ---: | ---: | ---: | --- |
| Enabled second revision | 12 s | 453,531,780 / 794,136 | 0 s | 41,373 | New run, XVT hash, parse, MIME, and cache passed |
| Disabled rollback | 7 s | 315,331,572 / 122,533 | 3 s | 7,336 | Capability absent, tile manifest HTTP 404, whole grids passed |
| Enabled restore | 12 s | 453,531,108 / 794,376 | 2 s | 41,373 | Same three immutable revisions passed |

After the final restore, the staging tile manifest returned `200` with
`no-cache`. A representative gzip-encoded XVT returned `200` with
`application/octet-stream` and `max-age=31536000, immutable`. The production
timer was active, the production service was inactive, and the production root
manifest did not advertise spatial value tiles.

## Production activation evidence

Production activation was explicitly approved and completed on 2026-07-16.
The Coding Server used the official `main` checkout at commit `2d81026`; the
recurring timer was restored before the forced publication completed. The run
started at `21:23:11Z` and succeeded at `21:36:37Z` for CH1 and CH2
`20260716_1800`.

| Production result | Measurement |
| --- | ---: |
| Retained value-tile runs / variants / XVT files | 8 / 120 / 115,824 |
| Existing retained whole-grid steps | Wind 5,152; Sunshine 636; Rain 644; Sun/Rain 636; Cloud 2,576 |
| Filesystem used baseline / peak / delta | 87,132 / 92,083 / 4,951 MB |
| Pipeline duration | 13 minutes 26 seconds |
| Guarded Infomaniak upload | 44 seconds |
| Remote validation | 3 seconds |

The public root manifest advertises contract `xcbenz-spatial-value-tiles`
`1.0.0`, package `immutable-chunks-cloud-dual-v1`, status `dual_publish`,
fallback `whole_grid_split_binary_v1`, and `requires_range=false`. Both
manifests return `200` with `no-cache`. Representative revision metadata and an
XVT return `200` with `application/octet-stream` for the tile and one-year
immutable caching. The exact legacy Wind H000 fallback also returns `200` with
91,396 bytes.

A fresh production browser session requested six CH1 Wind H03 tiles for the
Switzerland view and six H04 tiles for prefetch, with no whole-grid step
request. Cloud/Rain requested six `cloud4` and six Rain tiles per foreground
step plus prefetch, again with no `.bin` request. Both products rendered
continuously without visible seams or application errors.

Rollback remains publication-based and is not a frontend redeploy. Set
`ENABLE_VALUE_TILES=false` in both executor configurations and publish the next
validated candidate. Retention then removes the tile capability and tile tree,
while the deployed frontend automatically uses the still-published whole-grid
reader. Do not remove either fallback until at least 48 hours and four
successful production publication cycles have passed and removal is separately
approved.

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

Production currently advertises this capability and the frontend uses it.
Disabled mode remains the tested rollback path, and the whole-grid files remain
published as the automatic frontend fallback during the observation window.
