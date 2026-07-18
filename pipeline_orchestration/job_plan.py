"""Deterministic forecast job plans for the Coding Server and GitHub fallback."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from forecast_fetch.planning import CH1_POLICY, CH2_POLICY


MAP_CACHE_NAMES = {
    "wind": "cache_wind_maps",
    "sunshine": "cache_sunshine_maps",
    "rain": "cache_rain_maps",
    "sunrain": "cache_sunrain_maps",
    "cloud": "cache_cloud_maps",
}


def chunk_id(start: int, end: int) -> str:
    return CH1_POLICY.profile_chunk_id(start, end)


def parse_run_tag(run_tag: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(run_tag, "%Y%m%d_%H%M")
    except ValueError as exc:
        raise ValueError(f"Invalid run tag {run_tag!r}; expected YYYYMMDD_HHMM") from exc


def horizon_chunks(end: int, chunk_size: int) -> tuple[tuple[int, int], ...]:
    size = max(1, chunk_size)
    chunks: list[tuple[int, int]] = []
    start = 0
    while start <= end:
        chunk_end = min(end, start + size - 1)
        chunks.append((start, chunk_end))
        start = chunk_end + 1
    if len(chunks) > 1:
        last_start, last_end = chunks[-1]
        if last_end - last_start + 1 < max(2, size // 2):
            previous_start, _previous_end = chunks[-2]
            chunks[-2:] = [(previous_start, last_end)]
    return tuple(chunks)


def ch1_profile_ranges(run_tag: str) -> tuple[tuple[int, int], ...]:
    if parse_run_tag(run_tag).hour == 3:
        return ((0, 16), (17, 33), (34, 45))
    return ((0, 16), (17, 33))


def profile_ranges(model: str, run_tag: str) -> tuple[tuple[int, int], ...]:
    if model in {"ch1", "icon-ch1"}:
        return ch1_profile_ranges(run_tag)
    if model in {"ch2", "icon-ch2"}:
        parse_run_tag(run_tag)
        return ch2_ranges(0)
    return ()


def profile_chunk_ids(model: str, run_tag: str) -> tuple[str, ...]:
    try:
        ranges = profile_ranges(model, run_tag)
    except ValueError:
        if model in {"ch1", "icon-ch1"}:
            ranges = ((0, 16), (17, 33), (34, 45))
        elif model in {"ch2", "icon-ch2"}:
            ranges = ch2_ranges(0)
        else:
            ranges = ()
    return tuple(chunk_id(start, end) for start, end in ranges)


def expected_horizon_count(model: str, run_tag: str) -> int:
    reference = parse_run_tag(run_tag)
    if model in {"ch1", "icon-ch1"}:
        return CH1_POLICY.maximum_horizon(reference) + 1
    if model in {"ch2", "icon-ch2"}:
        return CH2_POLICY.maximum_horizon(reference) + 1
    raise ValueError(f"Unsupported forecast model {model!r}")


def ch1_map_ranges(run_tag: str, chunk_size: int) -> tuple[tuple[int, int], ...]:
    end = CH1_POLICY.maximum_horizon(parse_run_tag(run_tag))
    return horizon_chunks(end, chunk_size) if chunk_size > 0 else ch1_profile_ranges(run_tag)


def ch2_ranges(chunk_size: int) -> tuple[tuple[int, int], ...]:
    if chunk_size > 0:
        return horizon_chunks(120, chunk_size)
    return ((0, 30), (31, 60), (61, 90), (91, 120))


@dataclass(frozen=True)
class PlannedChunk:
    id: str
    start: int
    end: int
    expected_horizon_count: int
    cache_roots: tuple[tuple[str, str], ...] = ()

    def roots(self) -> dict[str, str]:
        return dict(self.cache_roots)

    def to_matrix_entry(self) -> dict[str, str]:
        entry = {
            "id": self.id,
            "start": str(self.start),
            "end": str(self.end),
            "expected_horizon_count": str(self.expected_horizon_count),
        }
        entry.update(self.cache_roots)
        return entry

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "start": self.start,
            "end": self.end,
            "expected_horizon_count": self.expected_horizon_count,
            "cache_roots": self.roots(),
        }


@dataclass(frozen=True)
class SurfaceJobPlan:
    map_chunks: tuple[PlannedChunk, ...]
    profile_chunks: tuple[PlannedChunk, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "map_chunks": [chunk.to_dict() for chunk in self.map_chunks],
            "profile_chunks": [chunk.to_dict() for chunk in self.profile_chunks],
        }


@dataclass(frozen=True)
class ModelJobPlan:
    model: str
    run_tag: str
    expected_horizon_count: int
    coding_server: SurfaceJobPlan
    github: SurfaceJobPlan

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "run_tag": self.run_tag,
            "expected_horizon_count": self.expected_horizon_count,
            "coding_server": self.coding_server.to_dict(),
            "github": self.github.to_dict(),
        }


@dataclass(frozen=True)
class PipelineJobPlan:
    schema_version: int
    ch1: ModelJobPlan
    ch2: ModelJobPlan

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "models": {
                "ch1": self.ch1.to_dict(),
                "ch2": self.ch2.to_dict(),
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def github_outputs(self) -> dict[str, str]:
        return {
            "ch1_map_matrix": matrix_json(self.ch1.github.map_chunks),
            "ch1_profile_matrix": matrix_json(self.ch1.github.profile_chunks),
            "ch2_map_matrix": matrix_json(self.ch2.github.map_chunks),
            "ch2_profile_matrix": matrix_json(self.ch2.github.profile_chunks),
            "job_plan": self.to_json(),
        }


def matrix_json(chunks: tuple[PlannedChunk, ...]) -> str:
    return json.dumps(
        {"chunk": [chunk.to_matrix_entry() for chunk in chunks]},
        sort_keys=True,
        separators=(",", ":"),
    )


def _cache_roots(model: str, chunk: str, surface: str) -> tuple[tuple[str, str], ...]:
    roots = []
    for product, cache in MAP_CACHE_NAMES.items():
        if surface == "github" and model == "ch1":
            root = cache
        elif surface == "github":
            chunk_cache = "cache_wind_packed" if product == "wind" else cache
            root = f"map_chunk_outputs/{chunk}/{chunk_cache}"
        else:
            root = f"map_chunks/{model}/{chunk}/{cache}"
        roots.append((f"{product}_root", root))
    return tuple(roots)


def _chunks(
    ranges: tuple[tuple[int, int], ...],
    expected_horizon_count: int,
    *,
    model: str,
    surface: str,
    include_cache_roots: bool,
) -> tuple[PlannedChunk, ...]:
    result = []
    for start, end in ranges:
        identifier = chunk_id(start, end)
        roots = _cache_roots(model, identifier, surface) if include_cache_roots else ()
        result.append(PlannedChunk(identifier, start, end, expected_horizon_count, roots))
    return tuple(result)


def build_job_plan(
    ch1_run_tag: str,
    ch2_run_tag: str,
    *,
    ch1_chunk_size: int = 0,
    ch2_chunk_size: int = 0,
) -> PipelineJobPlan:
    ch1_expected = expected_horizon_count("ch1", ch1_run_tag)
    ch2_expected = expected_horizon_count("ch2", ch2_run_tag)
    ch1_profiles = profile_ranges("ch1", ch1_run_tag)
    local_ch1_maps = ch1_map_ranges(ch1_run_tag, ch1_chunk_size)
    local_ch2_maps = ch2_ranges(ch2_chunk_size)
    github_ch1_maps = ((0, ch1_expected - 1),)
    github_ch2_maps = ch2_ranges(0)

    ch1 = ModelJobPlan(
        model="ch1",
        run_tag=ch1_run_tag,
        expected_horizon_count=ch1_expected,
        coding_server=SurfaceJobPlan(
            map_chunks=_chunks(local_ch1_maps, ch1_expected, model="ch1", surface="coding_server", include_cache_roots=True),
            profile_chunks=_chunks(ch1_profiles, ch1_expected, model="ch1", surface="coding_server", include_cache_roots=False),
        ),
        github=SurfaceJobPlan(
            map_chunks=_chunks(github_ch1_maps, ch1_expected, model="ch1", surface="github", include_cache_roots=True),
            profile_chunks=_chunks(ch1_profiles, ch1_expected, model="ch1", surface="github", include_cache_roots=False),
        ),
    )
    ch2 = ModelJobPlan(
        model="ch2",
        run_tag=ch2_run_tag,
        expected_horizon_count=ch2_expected,
        coding_server=SurfaceJobPlan(
            map_chunks=_chunks(local_ch2_maps, ch2_expected, model="ch2", surface="coding_server", include_cache_roots=True),
            profile_chunks=_chunks(local_ch2_maps, ch2_expected, model="ch2", surface="coding_server", include_cache_roots=False),
        ),
        github=SurfaceJobPlan(
            map_chunks=_chunks(github_ch2_maps, ch2_expected, model="ch2", surface="github", include_cache_roots=True),
            profile_chunks=_chunks(github_ch2_maps, ch2_expected, model="ch2", surface="github", include_cache_roots=False),
        ),
    )
    return PipelineJobPlan(schema_version=1, ch1=ch1, ch2=ch2)


def write_github_outputs(path: Path, outputs: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for name, value in outputs.items():
            handle.write(f"{name}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Print the deterministic XCBenz orchestration job plan.")
    parser.add_argument("--ch1-run-tag", required=True)
    parser.add_argument("--ch2-run-tag", required=True)
    parser.add_argument("--ch1-chunk-size", type=int, default=0)
    parser.add_argument("--ch2-chunk-size", type=int, default=0)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    plan = build_job_plan(
        args.ch1_run_tag,
        args.ch2_run_tag,
        ch1_chunk_size=args.ch1_chunk_size,
        ch2_chunk_size=args.ch2_chunk_size,
    )
    if args.github_output:
        write_github_outputs(args.github_output, plan.github_outputs())
    else:
        print(plan.to_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
