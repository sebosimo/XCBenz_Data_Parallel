# Coding Server Forecast Pipeline Prototype

This prototype moves forecast execution onto the Hetzner coding server while
keeping the public browser contract at `https://data.xcbenz.com/web_exports/`.

It does not remove the GitHub Actions workflow. Keep Actions available as the
fallback until the coding-server run has been benchmarked and observed.

## What Runs Locally

`scripts/run_coding_server_pipeline.py` runs the same broad job families as
`.github/workflows/daily_plot.yml`:

- preflight against the latest MeteoSwiss CH1 and CH2 runs
- CH1 map chunks, now parallelized locally
- CH1 direct profile chunks
- CH2 map chunks
- CH2 direct profile chunks
- map chunk merge
- retention, manifest generation, `web_exports` generation, and validation
- optional data-host deploy
- optional `data-web` backup branch push

The runner monitors `/proc/stat`, load average, and `/proc/meminfo`. It logs CPU,
load, available RAM, and active fetch workers, and delays starting new workers
when configured limits are exceeded.

## Local Smoke Plan

From the data repo checkout on the coding server:

```bash
uv run python scripts/run_coding_server_pipeline.py \
  --plan-only \
  --skip-deploy \
  --no-push-data-branch \
  --ch1-run-tag 20260624_0300 \
  --ch2-run-tag 20260624_0000
```

For a full local benchmark without publishing:

```bash
XCBENZ_RUN_MODE=force-refresh \
uv run python scripts/run_coding_server_pipeline.py \
  --skip-deploy \
  --no-push-data-branch
```

Logs are written under `.local_pipeline/runs/<timestamp>/logs/`.

`--job-layout combined` is an experimental speed mode for the local runner. It
uses one worker per horizon chunk to write both map outputs and direct profile
chunks from the same downloaded fields. For the pinned non-03Z test run below,
that reduces top-level fetch jobs from 12 to 6 while preserving the same
browser-facing output validation contract. Keep the default split layout
available until the combined layout has more production-like runs behind it.

`--ch1-chunk-size` and `--ch2-chunk-size` are experimental runner-only knobs.
The default `0` preserves the legacy chunk plans. Positive values split each
model into fixed-size horizon chunks, merging a tiny final remainder into the
previous chunk.

`--prefetch-next-horizon` is also experimental. It keeps final outputs the same
but lets each fetch worker download horizon `h+1` while decoding and writing
horizon `h`.

`--max-ch1-jobs` is an experimental scheduler cap for active CH1 workers. The
default `0` leaves the normal FIFO scheduler unchanged.

`--release-profile-only-fields` is an experimental memory-reduction switch for
direct profile chunks. After a horizon's profile bundle values are extracted, it
frees pressure-only fields before map accumulators continue.

`--direct-wind-web` is an experimental wind-output switch for the local runner.
When enabled, wind chunks write browser-ready `cache_wind_maps/{model}/{run}/`
metadata and `steps/*.bin` files directly instead of writing intermediate
`cache_wind_packed/*.nc` files. The final `web_exports/wind_maps/...` contract
stays the same.

## Benchmark Notes

### 2026-06-24 Max-Jobs 7 Dry Run

Command shape:

```bash
cd /home/sebas/projects/XCBenz_Data_Parallel
.venv/bin/python scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd .venv/bin/python \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260624T2040Z
```

Result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `2c18f89`
- server: `codex-cx43-nbg1`, 8 CPU, 15 GiB RAM
- run window: `2026-06-24T20:40:19Z` to `2026-06-24T20:49:32Z`
- total runtime: about 9m13s
- fetch phase runtime: about 6m32s, from first fetch start to last fetch done
- selected runs: CH1 `20260624_1800`, CH2 `20260624_1200`
- top-level fetch jobs planned: 12
- top-level fetch jobs active at peak: 7
- deploy: skipped
- `data-web` push: skipped
- validation: passed

Validation summary:

```text
profiles=90160
bundles=1120
region_forecasts=1120
wind_steps=3864
sunshine_steps=636
rain_steps=644
sunrain_steps=636
cloud_steps=2576
```

Output sizes:

```text
web_exports:        1.2G
map_chunks:         332M
web_profile_chunks: 78M
run logs:           2.3M
```

Resource observations:

- sampled CPU peaked around 84%
- sampled load1 peaked at 7.71
- sampled available RAM stayed above the configured 4096 MB floor
- lowest sampled available RAM was about 4931 MB
- public SSH timed out once during peak load, then recovered

Interpretation:

`XCBENZ_LOCAL_MAX_JOBS=7` is viable for throughput, but it is aggressive for an
interactive coding server. Use `4` or `5` as the safer starting point if Codex
or SSH responsiveness matters while the pipeline is running.

### 2026-06-24 Max-Jobs 4 Dry Run

Command shape:

```bash
cd /home/sebas/projects/XCBenz_Data_Parallel_max4_pinned_20260624T2104Z
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --max-jobs 4 \
  --ch1-run-tag 20260624_1800 \
  --ch2-run-tag 20260624_1200 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260624T2104Z-max4-pinned
```

Result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `2c18f89`
- server: `codex-cx43-nbg1`, 8 CPU, 15 GiB RAM
- run window: `2026-06-24T21:04:27Z` to `2026-06-24T21:14:28Z`
- total runtime: about 10m01s
- fetch phase runtime: about 8m20s, from first fetch start to last fetch done
- selected runs: CH1 `20260624_1800`, CH2 `20260624_1200`
- top-level fetch jobs planned: 12
- top-level fetch jobs active at peak: 4
- deploy: skipped
- `data-web` push: skipped
- validation: passed

Validation summary matched the max-jobs 7 dry run:

```text
profiles=90160
bundles=1120
region_forecasts=1120
wind_steps=3864
sunshine_steps=636
rain_steps=644
sunrain_steps=636
cloud_steps=2576
```

Output sizes also matched the max-jobs 7 dry run:

```text
web_exports:        1.2G
map_chunks:         332M
web_profile_chunks: 78M
run logs:           2.5M
```

Resource observations:

- sampled CPU peaked around 56%
- sampled load1 peaked at 3.79
- sampled available RAM stayed above the configured 4096 MB floor
- lowest sampled available RAM was about 5909 MB
- no SSH timeout was observed during this run

Interpretation:

`XCBENZ_LOCAL_MAX_JOBS=4` keeps the coding server much more responsive while
still running the fetch phase in parallel. Compared with max-jobs 7, the fetch
phase took about 1m48s longer, but sampled peak CPU, load, and RAM pressure were
all materially lower. Total runtime was only about 48s longer in this pair of
tests, but fetch phase runtime is the cleaner comparison because post-fetch
network/cache timing differed between runs.

### 2026-06-25 Combined Layout Dry Runs

These tests used the same pinned source runs as the 2026-06-24 comparison:
CH1 `20260624_1800` and CH2 `20260624_1200`. Deploy and `data-web` push were
skipped. Because the restored `data-web` branch had advanced by 2026-06-25, the
final retained `web_exports` counts were lower than the 2026-06-24 dry runs; use
fetch phase timing and resource samples for scheduler comparison, not retained
snapshot counts across dates.

Max-jobs 4 command shape:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --job-layout combined \
  --max-jobs 4 \
  --ch1-run-tag 20260624_1800 \
  --ch2-run-tag 20260624_1200 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260625T0000Z-combined-max4
```

Max-jobs 4 result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `7608647`
- fetch jobs planned: 6
- fetch jobs active at peak: 4
- run window: `2026-06-25T06:17:52Z` to `2026-06-25T06:26:01Z`
- total runtime: about 8m09s
- fetch phase runtime: about 6m22s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 59%
- sampled load1 peaked at 4.20
- lowest sampled available RAM was about 6640 MB
- no SSH timeout was observed

Max-jobs 5 changed only `--max-jobs` and used run dir
`.local_pipeline/runs/manual-coding-server-test-20260625T0635Z-combined-max5`.

Max-jobs 5 result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `7608647`
- fetch jobs planned: 6
- fetch jobs active at peak: 5
- run window: `2026-06-25T06:27:26Z` to `2026-06-25T06:34:47Z`
- total runtime: about 7m21s
- fetch phase runtime: about 5m58s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 68%
- sampled load1 peaked at 4.98
- lowest sampled available RAM was about 6063 MB
- no SSH timeout was observed

Validation summary for both combined runs:

```text
profiles=68460
bundles=840
region_forecasts=840
wind_steps=2934
sunshine_steps=483
rain_steps=489
sunrain_steps=483
cloud_steps=1956
```

Output sizes for both combined runs:

```text
web_exports:        843M
map_chunks:         332M
web_profile_chunks: 78M
run logs:           1.8M
```

Interpretation:

Combined max-jobs 4 is the best measured balance so far: it is about 1m58s
faster in fetch phase than split max-jobs 4 and slightly faster than split
max-jobs 7, while using much less CPU/load than split max-jobs 7. Combined
max-jobs 5 is faster again, but only by about 24s of fetch time and with a
sampled CPU peak near 68%, so it is less aligned with an interactive coding
server target. Neither combined run reached the sub-5-minute fetch target; the
next optimization target is reducing per-horizon CH1 processing and decode work
inside each combined worker.

### 2026-06-28 Cached Location-Index Dry Run

Commit `a09800b` caches per-worker profile location indices and static height
profiles in `fetch_data.py` and `fetch_data_ch2.py`. This removes repeated
nearest-grid scans for every location and horizon in direct profile chunk mode.

The original pinned 2026-06-24 source runs were no longer available from
MeteoSwiss by this date, so this benchmark used current latest runs instead of a
direct A/B replay:

- CH1 `20260628_0300`
- CH2 `20260628_0000`

Command shape:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --job-layout combined \
  --max-jobs 4 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260628T0548Z-cachedidx-current-max4
```

Result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `a09800b`
- fetch jobs planned: 7 because this was a 03Z CH1 run
- fetch jobs active at peak: 4
- run window: `2026-06-28T05:48:06Z` to `2026-06-28T05:56:58Z`
- total runtime: about 8m52s
- fetch phase runtime: about 7m24s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 63%
- sampled load1 peaked at 4.44
- lowest sampled available RAM was about 4412 MB
- no SSH timeout was observed

Validation summary:

```text
profiles=68460
bundles=840
region_forecasts=840
wind_steps=2934
sunshine_steps=483
rain_steps=489
sunrain_steps=483
cloud_steps=1956
```

Output sizes:

```text
web_exports:        891M
map_chunks:         371M
web_profile_chunks: 84M
run logs:           2.1M
```

Interpretation:

The cached-index change validated on a real current run, but this was a heavier
03Z CH1 case and cannot be compared directly with the earlier pinned non-03Z
timings. It also exceeded the preferred 6 GB RAM headroom during the initial
four-worker wave. For 03Z runs, combined max-jobs 4 is viable but not yet within
the desired sub-5-minute target or the preferred resource envelope. The next
optimization should focus on reducing CH1 per-horizon decode/map work, or on a
layout that avoids starting three CH1 combined workers at once when RAM
headroom matters.

### 2026-06-28 Batched Per-Horizon Download Dry Run

Commit `a66d8c2` adds `XCBENZ_FETCH_HORIZON_BATCH=true` for the local
coding-server runner. With the flag enabled, each CH1/CH2 fetch worker requests
the profile/wind, rain, cloud, and radiation variables for a horizon through one
download pool, then passes the files through the same decode/write blocks as
before. The output shape is unchanged; only the timing of the requests changes.

This was run against the same current source runs as the cached-index benchmark:

- CH1 `20260628_0300`
- CH2 `20260628_0000`

Command shape:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --job-layout combined \
  --max-jobs 4 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260628T065402Z-batched-current-max4
```

Result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `a66d8c2`
- fetch jobs planned: 7 because this was a 03Z CH1 run
- fetch jobs active at peak: 4
- run window: `2026-06-28T06:54:04Z` to `2026-06-28T07:02:26Z`
- total runtime: about 8m22s
- fetch phase runtime: about 6m56s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 67%
- sampled load1 peaked at 3.93
- lowest sampled available RAM was about 4403 MB
- no SSH timeout was observed

Validation summary matched the cached-index current run:

```text
profiles=68460
bundles=840
region_forecasts=840
wind_steps=2934
sunshine_steps=483
rain_steps=489
sunrain_steps=483
cloud_steps=1956
```

Output sizes:

```text
web_exports:        891M
map caches:         about 369M
web_profile_chunks: 84M
run logs:           2.0M
```

Interpretation:

Compared with the previous current-run cached-index benchmark, per-horizon
download batching improved fetch phase runtime by about 28s and total runtime
by about 30s. This is directionally useful, but it did not reach the
sub-5-minute fetch target and it still dipped below the preferred 6 GB RAM
headroom during the first four-worker wave. The remaining runtime is likely not
only request scheduling: CH2 tail chunks and per-variable decode/write work are
now a larger share of the measured wall time.

Max-jobs 5 changed only `--max-jobs` and used run dir
`.local_pipeline/runs/manual-coding-server-test-20260628T071024Z-batched-current-max5`.

Max-jobs 5 result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `20cf607`
- fetch jobs planned: 7 because this was a 03Z CH1 run
- fetch jobs active at peak: 5
- run window: `2026-06-28T07:10:26Z` to `2026-06-28T07:17:10Z`
- total runtime: about 6m44s
- fetch phase runtime: about 5m18s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 74%
- sampled load1 peaked at 4.99
- lowest sampled available RAM was about 3823 MB
- no SSH timeout was observed

Validation summary and output sizes matched the max-jobs 4 batched current run.

Interpretation:

Max-jobs 5 is the fastest measured 03Z current-run layout so far, improving
fetch phase runtime by about 1m38s over batched max-jobs 4. It still did not
reach the sub-5-minute fetch target, and it is not aligned with the preferred
interactive coding-server resource envelope: the first wave held five workers,
peaked around 74% sampled CPU, and dropped available RAM below 4 GB.

### 2026-06-28 Vectorized Direct-Profile Extraction Dry Run

Commit `6136782` changes direct profile chunk writing to select all configured
location indices for a variable in one xarray operation per horizon, instead of
selecting each location one by one. This keeps `bundle.json` and `profiles.bin`
ordering unchanged and only reduces profile extraction overhead inside the
combined workers.

The benchmark used the same current source runs as the previous 2026-06-28
tests:

- CH1 `20260628_0300`
- CH2 `20260628_0000`

Command shape:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --job-layout combined \
  --max-jobs 4 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260628T072352Z-vectorized-current-max4
```

Result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `6136782`
- fetch jobs planned: 7 because this was a 03Z CH1 run
- fetch jobs active at peak: 4
- run window: `2026-06-28T07:23:53Z` to `2026-06-28T07:32:13Z`
- total runtime: about 8m20s
- fetch phase runtime: about 6m54s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 67%
- sampled load1 peaked at 4.26
- lowest sampled available RAM was about 4099 MB
- no SSH timeout was observed

Validation summary matched the earlier current runs.

Interpretation:

Vectorized point extraction validated, but it was not a material speedup for the
full coding-server run: fetch phase improved by only about 2s versus batched
max-jobs 4, likely within normal MeteoSwiss/network variation. The dominant
runtime remains broader per-horizon download/decode/write work and the CH2 tail,
not the per-location profile selection loop by itself.

### 2026-06-28 Download-Workers 8 Dry Run

This run kept the vectorized code at commit `d84222c` and changed only
`--download-workers 8`, leaving the process-level scheduler at
`--job-layout combined --max-jobs 4`. The intent was to test whether higher
intra-worker request concurrency helps without starting more top-level workers.

The benchmark used the same current source runs as the previous 2026-06-28
tests:

- CH1 `20260628_0300`
- CH2 `20260628_0000`

Command shape:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --job-layout combined \
  --max-jobs 4 \
  --download-workers 8 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260628T073655Z-dw8-current-max4
```

Result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `d84222c`
- fetch jobs planned: 7 because this was a 03Z CH1 run
- fetch jobs active at peak: 4
- run window: `2026-06-28T07:36:57Z` to `2026-06-28T07:45:02Z`
- total runtime: about 8m05s
- fetch phase runtime: about 6m38s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 69%
- sampled load1 peaked at 4.70
- lowest sampled available RAM was about 4488 MB
- no SSH timeout was observed

Validation summary and output sizes matched the earlier current runs.

Interpretation:

Raising `DOWNLOAD_WORKERS` from 6 to 8 is the fastest measured max-jobs 4
configuration so far, improving fetch phase runtime by about 16s versus the
vectorized max-jobs 4 run and about 18s versus the first batched max-jobs 4
run. It still misses the sub-5-minute target and does not meet the preferred
resource envelope, so it is useful as a measured upper setting but not enough by
itself to justify calling the coding-server migration complete.

### 2026-06-28 Smaller CH2 Chunk Dry Run

Commit `f53bd7b` adds an experimental `--ch2-chunk-size` runner option. The
default keeps the legacy four CH2 chunks, while positive values split CH2 into
smaller contiguous horizon chunks and merge tiny final remainders into the
previous chunk. This run used `--ch2-chunk-size 15` with the previous fastest
max-jobs 4 tuning, `--download-workers 8`.

The benchmark used the same current source runs as the previous 2026-06-28
tests:

- CH1 `20260628_0300`
- CH2 `20260628_0000`

Command shape:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --job-layout combined \
  --max-jobs 4 \
  --download-workers 8 \
  --ch2-chunk-size 15 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260628T075255Z-ch2c15-dw8-current-max4
```

Result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `f53bd7b`
- fetch jobs planned: 11 because CH2 was split into eight chunks
- fetch jobs active at peak: 4
- run window: `2026-06-28T07:52:56Z` to `2026-06-28T08:00:53Z`
- total runtime: about 7m57s
- fetch phase runtime: about 6m32s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 69%
- sampled load1 peaked at 4.35
- lowest sampled available RAM was about 4336 MB
- no SSH timeout was observed

Validation summary and final `web_exports` size matched the earlier current
runs. Intermediate `web_profile_chunks` was slightly larger because more CH2
chunk directories were staged before final export generation.

Interpretation:

Smaller CH2 chunks reduced the max-jobs 4 fetch phase by about 6s versus the
download-workers 8 run with legacy CH2 chunks. This proves arbitrary CH2 chunk
boundaries can preserve output shape, but the speedup is too small by itself.
The likely next scheduler lever is job ordering: the current combined layout
starts all CH1 chunks before most CH2 chunks, which keeps the first-wave RAM
pressure high and still leaves CH2 work after CH1 drains.

### 2026-06-28 Interleaved Job Order Dry Runs

Commit `695c818` adds an experimental `--combined-job-order interleave` option.
The default remains `ch1-first`. Interleaving starts two CH1 combined workers
and two CH2 combined workers first, then starts remaining CH1/CH2 jobs in the
planned order. This is intended to reduce the first-wave CH1 memory spike and
pull CH2 work earlier without increasing `--max-jobs`.

The first interleaved run used latest available runs after MeteoSwiss rolled CH1
forward:

- CH1 `20260628_0600`
- CH2 `20260628_0000`

Command shape:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --job-layout combined \
  --combined-job-order interleave \
  --max-jobs 4 \
  --download-workers 8 \
  --ch2-chunk-size 15 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260628T080659Z-interleave-ch2c15-dw8-current-max4
```

Latest-run result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `695c818`
- fetch jobs planned: 10 because CH1 06Z only needs two CH1 chunks
- fetch jobs active at peak: 4
- run window: `2026-06-28T08:07:01Z` to `2026-06-28T08:13:52Z`
- total runtime: about 6m51s
- fetch phase runtime: about 5m30s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 61%
- sampled load1 peaked at 4.38
- lowest sampled available RAM was about 6760 MB
- no SSH timeout was observed

This run is not directly comparable to the earlier 03Z benchmarks because CH1
06Z has fewer CH1 chunks, but it is the first measured run that stayed above
the preferred 6 GB RAM headroom while using four workers.

A second run pinned CH1 back to the same 03Z source used by the previous
2026-06-28 comparisons:

- CH1 `20260628_0300`
- CH2 `20260628_0000`

Pinned 03Z command shape:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --job-layout combined \
  --combined-job-order interleave \
  --max-jobs 4 \
  --download-workers 8 \
  --ch2-chunk-size 15 \
  --ch1-run-tag 20260628_0300 \
  --ch2-run-tag 20260628_0000 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260628T081456Z-interleave-ch2c15-dw8-pinned03-max4
```

Pinned 03Z result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `695c818`
- fetch jobs planned: 11 because CH1 03Z needs three CH1 chunks
- fetch jobs active at peak: 4
- run window: `2026-06-28T08:14:57Z` to `2026-06-28T08:22:38Z`
- total runtime: about 7m41s
- fetch phase runtime: about 6m15s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 60%
- sampled load1 peaked at 4.71
- lowest sampled available RAM was about 4501 MB
- no SSH timeout was observed

Validation summary and final output sizes matched the earlier pinned 03Z current
runs.

Interpretation:

Interleaving is the best measured max-jobs 4 scheduler so far for the pinned
03Z workload, improving fetch phase runtime by about 17s versus smaller CH2
chunks with `ch1-first`, and about 23s versus the download-workers 8 run with
legacy CH2 chunks. It also improves first-wave RAM when CH1 has only two chunks.
For heavier CH1 03Z runs, however, it still starts the third CH1 chunk before
the two long CH1 chunks finish, so RAM can still dip well below the preferred
6 GB headroom.

A third run changed only `--max-jobs` from 4 to 5 to test the upper bound of
the same interleaved layout:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --job-layout combined \
  --combined-job-order interleave \
  --max-jobs 5 \
  --download-workers 8 \
  --ch2-chunk-size 15 \
  --ch1-run-tag 20260628_0300 \
  --ch2-run-tag 20260628_0000 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260628T091319Z-interleave-ch2c15-dw8-pinned03-max5
```

Pinned 03Z max-jobs 5 result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `695c818`
- fetch jobs planned: 11
- fetch jobs active at peak: 5
- run window: `2026-06-28T09:13:21Z` to `2026-06-28T09:20:21Z`
- total runtime: about 7m00s
- fetch phase runtime: about 5m28s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 77%
- sampled load1 peaked at 5.18
- lowest sampled available RAM was about 3739 MB
- no SSH timeout was observed

The max-jobs 5 interleaved run is not a candidate configuration. It was slower
than the earlier batched max-jobs 5 run and violated both resource goals more
clearly than max-jobs 4. Further gains should come from reducing per-horizon
decode/write/map work or changing how intermediate artifacts are generated, not
from adding more scheduler pressure.

### 2026-06-28 CH1 Chunk Size 12 Dry Run

Commit `98d0c45` adds an experimental `--ch1-chunk-size` runner option. The
first benchmark used `--ch1-chunk-size 12` with the previous best max-jobs 4
shape:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --job-layout combined \
  --combined-job-order interleave \
  --max-jobs 4 \
  --download-workers 8 \
  --ch1-chunk-size 12 \
  --ch2-chunk-size 15 \
  --ch1-run-tag 20260628_0300 \
  --ch2-run-tag 20260628_0000 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260628T093100Z-ch1c12-ch2c15-dw8-pinned03-max4
```

Result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `98d0c45`
- fetch jobs planned: 12
- fetch jobs active at peak: 4
- run window: `2026-06-28T09:30:38Z` to `2026-06-28T09:38:12Z`
- total runtime: about 7m34s
- fetch phase runtime: about 6m10s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 63%
- sampled load1 peaked at 4.74
- lowest sampled available RAM was about 2744 MB
- no SSH timeout was observed

Validation summary matched the pinned 03Z runs:

```text
profiles=68460
bundles=840
region_forecasts=840
wind_steps=2934
sunshine_steps=483
rain_steps=489
sunrain_steps=483
cloud_steps=1956
```

Interpretation:

CH1 chunk size 12 is not a candidate configuration. It only improved fetch
runtime by about 5s versus the previous best max-jobs 4 interleaved run, while
making RAM much worse because both short CH2 jobs finished early and the queue
started the remaining CH1 chunks before the first CH1 chunks completed. The
option is still useful for controlled experiments, but this result reinforces
that the next speed gain likely needs to reduce per-horizon download/decode work
rather than split CH1 more aggressively under the current scheduler.

### 2026-06-28 Next-Horizon Prefetch Dry Run

Commit `c8dd3fb` adds optional `--prefetch-next-horizon`. This benchmark used
the previous best max-jobs 4 geometry and enabled one-horizon-ahead prefetching:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --job-layout combined \
  --combined-job-order interleave \
  --max-jobs 4 \
  --download-workers 8 \
  --ch2-chunk-size 15 \
  --prefetch-next-horizon \
  --ch1-run-tag 20260628_0300 \
  --ch2-run-tag 20260628_0000 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260628T095000Z-prefetch-ch2c15-dw8-pinned03-max4
```

Result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `c8dd3fb`
- fetch jobs planned: 11
- fetch jobs active at peak: 4
- run window: `2026-06-28T09:49:41Z` to `2026-06-28T09:56:29Z`
- total runtime: about 6m48s
- fetch phase runtime: about 5m24s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 72%
- sampled load1 peaked at 6.23
- lowest sampled available RAM was about 3847 MB
- one scheduler pause occurred because available RAM dipped below the 4096 MB floor
- no SSH timeout was observed

Validation summary matched the pinned 03Z runs:

```text
profiles=68460
bundles=840
region_forecasts=840
wind_steps=2934
sunshine_steps=483
rain_steps=489
sunrain_steps=483
cloud_steps=1956
```

Interpretation:

Prefetch is the best measured max-jobs 4 runtime improvement so far, reducing
the pinned 03Z fetch phase by about 51s versus interleaved CH2 chunk size 15
without prefetch. It is still not a candidate for the stated resource target:
CPU, load, and RAM all moved in the wrong direction. The next useful prefetch
tests are lower-pressure combinations, for example max-jobs 3 or lower
download-workers, to see whether part of the speed gain can be retained while
keeping the coding server responsive.

A lower-pressure max-jobs 3 comparison used the same command shape but changed
only `--max-jobs`:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --job-layout combined \
  --combined-job-order interleave \
  --max-jobs 3 \
  --download-workers 8 \
  --ch2-chunk-size 15 \
  --prefetch-next-horizon \
  --ch1-run-tag 20260628_0300 \
  --ch2-run-tag 20260628_0000 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260628T100000Z-prefetch-ch2c15-dw8-pinned03-max3
```

Max-jobs 3 result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `c8dd3fb`
- fetch jobs planned: 11
- fetch jobs active at peak: 3
- run window: `2026-06-28T09:58:32Z` to `2026-06-28T10:06:42Z`
- total runtime: about 8m10s
- fetch phase runtime: about 6m48s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 55%
- sampled load1 peaked at 4.49
- lowest sampled available RAM was about 6609 MB
- no scheduler pause or SSH timeout was observed

Max-jobs 3 is closer to the resource target and keeps the preferred 6 GB RAM
headroom, but it gives up too much runtime. It is useful as a resource floor,
not as a candidate for the sub-5-minute goal.

Commit `65ea895` adds `--max-ch1-jobs` to test whether max-jobs 4 can stay busy
on CH2 while avoiding the RAM spike from three overlapping CH1 workers. The
comparison used `--max-jobs 4 --max-ch1-jobs 2` with prefetch enabled.

CH1 cap result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `65ea895`
- fetch jobs planned: 11
- fetch jobs active at peak: 4
- run window: `2026-06-28T10:12:44Z` to `2026-06-28T10:20:36Z`
- total runtime: about 7m52s
- fetch phase runtime: about 6m28s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 70%
- sampled load1 peaked at 5.57
- lowest sampled available RAM was about 5967 MB
- no SSH timeout was observed

The CH1 cap improved RAM versus uncapped max-jobs 4 prefetch, but it missed the
6 GB headroom target by a small amount and gave up too much time. The long tail
was the final CH1 chunk running alone after CH2 drained, so this is also not a
candidate.

A download-workers 6 comparison used the uncapped max-jobs 4 prefetch shape and
changed only `--download-workers` from 8 to 6.

Download-workers 6 result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `65ea895`
- fetch jobs planned: 11
- fetch jobs active at peak: 4
- run window: `2026-06-28T11:09:37Z` to `2026-06-28T11:16:28Z`
- total runtime: about 6m51s
- fetch phase runtime: about 5m28s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 71%
- sampled load1 peaked at 5.80
- lowest sampled available RAM was about 5472 MB
- no SSH timeout was observed

Download-workers 6 did not solve the resource problem and was slightly slower
than download-workers 8. The pressure is not only per-horizon download fanout;
it is the combination of active decode/write work and overlapping CH1 chunks.

### 2026-06-28 Profile-Only Field Release Dry Run

Commit `946b2ba` adds optional `--release-profile-only-fields`. This benchmark
used the previous fastest max-jobs 4 prefetch shape and enabled early release of
profile-only fields:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --job-layout combined \
  --combined-job-order interleave \
  --max-jobs 4 \
  --download-workers 8 \
  --ch2-chunk-size 15 \
  --prefetch-next-horizon \
  --release-profile-only-fields \
  --ch1-run-tag 20260628_0300 \
  --ch2-run-tag 20260628_0000 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-20260628T112600Z-prefetch-releasefields-ch2c15-dw8-pinned03-max4
```

Result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `946b2ba`
- fetch jobs planned: 11
- fetch jobs active at peak: 4
- run window: `2026-06-28T11:25:38Z` to `2026-06-28T11:32:26Z`
- total runtime: about 6m48s
- fetch phase runtime: about 5m18s, from first fetch start to last fetch done
- validation: passed
- sampled CPU peaked around 71%
- sampled load1 peaked at 6.81
- lowest sampled available RAM was about 6174 MB
- no scheduler pause or SSH timeout was observed

The aggregate validation counts were higher than earlier pinned runs because
the restored `web_exports` baseline retained additional current runs:

- CH1: `20260627_0300`, `20260628_0300`, `20260628_0600`, `20260628_0900`
- CH2: `20260627_0000`, `20260628_0000`, `20260628_0600`

Interpretation:

Early field release is a useful improvement. It produced the best pinned 03Z
fetch time so far while keeping the preferred 6 GB RAM headroom. It still does
not meet the CPU/load goal; the remaining bottleneck is compute pressure during
decode/map/profile work, not just retained field memory.

### 2026-06-28 Direct Wind Web Output Prototype

This prototype adds experimental `--direct-wind-web` /
`XCBENZ_DIRECT_WIND_WEB`. The old packed NetCDF wind path remains the default.
With the flag enabled:

- fetch workers write `cache_wind_maps/{model}/{run}/{level}/metadata.json`
  and `steps/*.bin` directly
- `scripts/merge_map_chunks.py` merges those split-binary wind chunks by level
- `generate_combined_manifest.py` marks direct wind runs as
  `browser_ready_split_binary_by_step`
- `generate_web_exports.py` copies direct wind step binaries into
  `web_exports/wind_maps/...` without reopening a wind NetCDF

Local synthetic compatibility checks on Windows passed:

- direct wind metadata matches the old NetCDF-derived browser metadata after
  ignoring only the provenance `source` path
- direct wind browser step binaries are byte-for-byte identical to the old
  NetCDF-derived `steps/*.bin` output for the same synthetic source fields
- direct chunk merge plus direct web export produced valid `url` step references
  and expected byte lengths

The first Hetzner benchmark used the previous best pinned 03Z command shape plus
`--direct-wind-web`:

```bash
/home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  scripts/run_coding_server_pipeline.py \
  --run-mode force-refresh \
  --skip-deploy \
  --no-push-data-branch \
  --python-cmd /home/sebas/projects/XCBenz_Data_Parallel/.venv/bin/python \
  --job-layout combined \
  --combined-job-order interleave \
  --max-jobs 4 \
  --download-workers 8 \
  --ch2-chunk-size 15 \
  --prefetch-next-horizon \
  --release-profile-only-fields \
  --direct-wind-web \
  --ch1-run-tag 20260628_0300 \
  --ch2-run-tag 20260628_0000 \
  --run-dir .local_pipeline/runs/manual-coding-server-test-direct-wind-web-pinned03-max4
```

Result:

- branch: `codex/coding-server-pipeline-prototype`
- commit: `331b9708`
- server: `codex-cx43-nbg1`, 8 CPU, 15 GiB RAM
- worktree:
  `/home/sebas/projects/XCBenz_Data_Parallel_directwind_20260628T162446Z`
- run window: `2026-06-28T16:25:09Z` to `2026-06-28T16:31:54Z`
- total runtime: about 6m45s
- fetch phase runtime: about 5m22s, from first fetch start to last fetch done
- selected runs: CH1 `20260628_0300`, CH2 `20260628_0000`
- fetch jobs planned: 11
- fetch jobs active at peak: 4
- deploy: skipped
- `data-web` push: skipped
- validation: passed
- sampled CPU peaked around 69%
- sampled load1 peaked at 6.78
- lowest sampled available RAM was about 6969 MB
- no scheduler pause or SSH timeout was observed

Validation summary:

```text
profiles=90160
bundles=1120
region_forecasts=1120
wind_steps=3864
sunshine_steps=636
rain_steps=644
sunrain_steps=636
cloud_steps=2576
```

Output sizes:

```text
web_exports:        1.2G
map_chunks:         241M
web_profile_chunks: 86M
```

Interpretation:

Direct wind web output validated and materially reduced intermediate map output
size. `map_chunks` dropped to 241M, compared with roughly 371M in the earlier
pinned 03Z runs that carried packed wind NetCDF chunks. It also removed the
expensive xarray wind chunk concat: `merge-map-chunks` completed in about 2s,
and `generate-web-exports` completed in about 1s.

The direct path did not improve fetch runtime versus the previous best
profile-only field release baseline: fetch was about 5m22s here versus about
5m18s before. Total runtime was slightly better, about 6m45s versus about
6m48s, and resource headroom improved: CPU peak about 69% versus about 71%,
and RAM minimum about 6969 MB versus about 6174 MB. This is a strong validation
for removing the packed wind NetCDF intermediate, but it does not by itself
reach the sub-5-minute fetch target.

## Server Setup

Assuming the repo is checked out at `/opt/xcbenz/XCBenz_Data_Parallel` and the
service user is `xcbenz`:

```bash
sudo install -d -m 0750 -o root -g xcbenz /etc/xcbenz
sudo install -m 0600 -o root -g xcbenz deploy/coding-server-pipeline.env.example /etc/xcbenz/coding-server-forecast.env
sudo editor /etc/xcbenz/coding-server-forecast.env

sudo install -m 0644 deploy/systemd/xcbenz-coding-server-forecast.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/xcbenz-coding-server-forecast.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Run a manual service test before enabling the timer:

```bash
sudo systemctl start xcbenz-coding-server-forecast.service
sudo journalctl -u xcbenz-coding-server-forecast.service -n 100 --no-pager
```

Enable the timer only after the manual run is acceptable:

```bash
sudo systemctl enable --now xcbenz-coding-server-forecast.timer
```

When this timer becomes the primary publisher, disable the old GitHub-dispatch
timer so both schedulers do not compete:

```bash
sudo systemctl disable --now xcbenz-github-actions-trigger.timer
```
