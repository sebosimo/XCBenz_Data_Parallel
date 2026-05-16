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
