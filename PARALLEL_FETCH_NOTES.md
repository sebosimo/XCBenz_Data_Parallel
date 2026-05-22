# Parallel Fetch Sandbox

This branch is prepared for a separate public sandbox repository, not for direct
replacement of the live `sebosimo/XCBenz_Data` workflow yet.

## Goals

- Keep the same generated data contract as `XCBenz_Data`.
- Keep all currently required variables and map products enabled.
- Publish sandbox output to `data-test` by default.
- Compare runtime, manifests, file counts, and frontend behavior before moving
  the workflow back to the live data repo.

## What Changed

- `.github/workflows/daily_plot.yml` now has a fast preflight job.
- Scheduled runs skip heavy setup when the latest CH1 and CH2 runs are already
  present in the sandbox data branch manifest.
- CH1 and CH2 fetches run in separate jobs.
- The publish job downloads both artifacts, regenerates the combined manifest
  and `web_exports/`, then force-pushes the sandbox data branch.
- The unconditional disk cleanup action is removed.
- `fetch_data.py` and `fetch_data_ch2.py` use bounded per-horizon concurrent
  STAC lookup and GRIB download workers. GRIB decode remains sequential.

## Variables Kept

The sandbox keeps the same current production variables:

- multi-level: `T`, `U`, `V`, `P`, `QV`
- native 10 m wind: `U_10M`, `V_10M`
- radiation scalars: `ASWDIR_S`, `ASWDIFD_S`
- sunshine maps: `DURSUN`, `DURSUN_M`
- static geometry: `HHL`, `HGRID`

## First Test

Run the workflow manually with:

```text
force_refresh: true
data_branch: data-test
download_workers: 6
```

Then compare:

- total workflow runtime
- CH1 fetch job runtime
- CH2 fetch job runtime
- `manifest.json` counts
- `web_exports/manifest.json` counts
- one sample emagram JSON
- one wind metadata file and step binary count
- one sunshine metadata file and step binary count

The frontend can test against the sandbox with:

```text
VITE_XCBENZ_DATA_BASE_URL=https://raw.githubusercontent.com/<owner>/<sandbox-repo>/data-test
```

## Notes From First Run

The first manual run proved the split workflow shape:

- CH1 fetch completed in about 16 minutes.
- CH2 fetch completed in about 23 minutes.
- Web export generation completed.
- The final push failed because `static_data/vertical_constants_icon-ch1-eps.grib2`
  is larger than GitHub's normal 100 MB blob limit.

`static_data/` is runner-side input data, not browser-facing output, so it is
not published to `data-test`.

## Notes From Successful Run

The second manual run used `download_workers: 6` and completed successfully in
about 16 minutes end to end. The worker default is now 6 for scheduled and
manual runs.

The sandbox sets `web_exports/manifest.json` `source.data_root` to:

```text
https://raw.githubusercontent.com/sebosimo/XCBenz_Data_Parallel/data-test
```

This keeps frontend smoke tests pointed at the sandbox instead of the production
data branch.

## Partial Run Backfill

Preflight now checks more than run-tag presence. A scheduled, non-forced run
continues if the latest published run has fewer than the expected horizon count:

- CH1 normal runs: 34 steps (`H00`-`H33`)
- CH1 03Z long run: 46 steps (`H00`-`H45`)
- CH2 runs: 121 steps (`H000`-`H120`)

This matters because MeteoSwiss can expose a new run before all horizons are
available. The fetch scripts already skip completed local horizons, so repeated
scheduled attempts backfill missing horizons instead of redownloading the whole
published run.

Preflight also emits per-model run decisions. If only CH1 has a new or
incomplete run, the CH2 fetch job is skipped entirely, and the publish job
restores the existing CH2 data from `data-test` before merging the fresh CH1
artifact.

## Complete-Run Chunk Pinning

Chunked profile and map jobs must all use the same model cycle selected by
preflight. MeteoSwiss can expose a new run before all horizons are available; if
chunk jobs independently choose or fall back between runs, publish can receive
mixed-run artifacts that cannot form one complete bundled run.

The workflow now passes preflight decisions to chunk jobs:

- `CH1_RUN_TAG` for CH1 map/profile jobs
- `CH2_RUN_TAG` for CH2 map/profile jobs
- `CH1_REQUIRE_FULL_HORIZON_RUN=true` and
  `CH2_REQUIRE_FULL_HORIZON_RUN=true` for strict chunk runs

Validated run `26281069968` selected `20260522_0600` for both CH1 and CH2 after
rejecting incomplete CH1 `20260522_0900` at H+033. CH1 and CH2 chunk logs both
showed the corresponding pinned run, and publish completed with `0` `.nc` files
under `web_exports/`.

## Infomaniak Data Host Preparation

The beta2 production-candidate path uses `data-web` as the cleaner generated
data branch name, leaving `data-test` available for sandbox experiments. The
`data-web` branch was seeded from validated `data-test` commit `108251b`.

The workflow has a gated manual deploy path for the future static data host:

```text
deploy_data_host: false
data_host_base_url: https://data.xcbenz.com
```

When `deploy_data_host=true`, validated `web_exports/` can be uploaded to
Infomaniak through `scripts/deploy_data_infomaniak.sh`, then checked with
`scripts/validate_remote_web_exports.py`. Scheduled runs do not deploy to
Infomaniak until this flag is explicitly enabled or the schedule is changed.

## Retention

The parallel workflow applies the same data-branch retention policy as the
existing archive in the final publish workspace before generating
`manifest.json` and `web_exports/`.

- CH1 keeps the two most recent runs plus the 03Z anchor from today/yesterday.
- CH2 keeps the two most recent runs plus the 00Z anchor from today/yesterday.

This publish-side pruning is required because artifact downloads overlay files
onto a restored data branch; they do not delete old run directories on their
own.
