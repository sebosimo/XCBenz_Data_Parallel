#!/usr/bin/env python3
"""Compare lossless spatial value-tile packages using published step binaries.

The input directory is intentionally disposable. It contains representative
metadata JSON and identity-encoded step binaries downloaded from web_exports.
No production generator or publisher code is invoked by this analysis.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


FINE_GRID_STEP = 0.02
HALO = 1

SELECTORS: dict[str, tuple[float, float, float, float]] = {
    "switzerland": (5.5, 45.5, 11.0, 48.2),
    "alps": (4.0, 43.0, 16.5, 48.8),
    "french_alps_north": (4.5, 45.1, 7.8, 46.8),
    "french_alps_south": (4.5, 43.4, 7.8, 45.2),
    "austrian_alps": (9.5, 46.2, 15.2, 48.2),
    "central_alps": (8.8, 45.6, 13.2, 47.6),
    "eastern_alps": (12.0, 45.6, 16.5, 47.8),
}

TRANSITIONS = (
    ("switzerland", "central_alps"),
    ("central_alps", "austrian_alps"),
    ("austrian_alps", "eastern_alps"),
    ("switzerland", "alps"),
)

DATASET_FILES = {
    "wind": ("wind_metadata.json", "wind_H02.bin"),
    "sunrain": ("sunrain_metadata.json", "sunrain_H02.bin"),
    "rain": ("rain_metadata.json", "rain_H02.bin"),
    "cloud_total": ("cloud_total_metadata.json", "cloud_total_H02.bin"),
    "cloud_low": ("cloud_low_metadata.json", "cloud_low_H02.bin"),
    "cloud_mid": ("cloud_mid_metadata.json", "cloud_mid_H02.bin"),
    "cloud_high": ("cloud_high_metadata.json", "cloud_high_H02.bin"),
}

CHANNEL_IDS = {
    "wind": 1,
    "sunrain": 2,
    "rain": 3,
    "cloud_total": 4,
    "cloud_low": 5,
    "cloud_mid": 6,
    "cloud_high": 7,
}

ENCODING_IDS = {
    "int8-interleaved-u-v": (1, 2),
    "uint8-semantic-sunrain-code": (2, 1),
    "uint8-interleaved-components": (3, 1),
    "packed-uint4-cloud-cover": (4, 1),
}

CLOUD_DATASETS = ("cloud_total", "cloud_low", "cloud_mid", "cloud_high")


@dataclass(frozen=True)
class Grid:
    width: int
    height: int
    lon_start: float
    lat_start: float
    lon_step: float
    lat_step: float


@dataclass(frozen=True)
class Dataset:
    name: str
    grid: Grid
    encoding_format: str
    missing_value: int
    cell_bytes: int
    values: bytes
    step_count: int

    @property
    def is_cloud(self) -> bool:
        return self.encoding_format == "packed-uint4-cloud-cover"

    def encode_window(self, x_start: int, y_start: int, width: int, height: int) -> bytes:
        """Return a row-major encoded window, padding outside the grid as missing."""
        if width <= 0 or height <= 0:
            raise ValueError("window dimensions must be positive")

        if self.is_cloud:
            cells = bytearray([self.missing_value] * (width * height))
        else:
            missing = bytes((self.missing_value & 0xFF,)) * self.cell_bytes
            cells = bytearray(missing * (width * height))

        for out_y in range(height):
            source_y = y_start + out_y
            if not 0 <= source_y < self.grid.height:
                continue
            for out_x in range(width):
                source_x = x_start + out_x
                if not 0 <= source_x < self.grid.width:
                    continue
                source_index = source_y * self.grid.width + source_x
                output_index = out_y * width + out_x
                if self.is_cloud:
                    cells[output_index] = self.values[source_index]
                else:
                    source_offset = source_index * self.cell_bytes
                    output_offset = output_index * self.cell_bytes
                    cells[output_offset : output_offset + self.cell_bytes] = self.values[
                        source_offset : source_offset + self.cell_bytes
                    ]

        return pack_cloud_codes(cells, self.missing_value) if self.is_cloud else bytes(cells)


def unpack_cloud_codes(payload: bytes, cell_count: int) -> bytes:
    values = bytearray(cell_count)
    for index in range(cell_count):
        packed = payload[index // 2]
        values[index] = packed & 0x0F if index % 2 == 0 else packed >> 4
    return bytes(values)


def pack_cloud_codes(values: Sequence[int], pad_code: int = 15) -> bytes:
    packed = bytearray((len(values) + 1) // 2)
    for index in range(0, len(values), 2):
        low = int(values[index]) & 0x0F
        high = int(values[index + 1]) & 0x0F if index + 1 < len(values) else pad_code & 0x0F
        packed[index // 2] = low | (high << 4)
    return bytes(packed)


def _axis_step(axis: dict) -> float:
    # Published metadata currently derives this from float32 coordinates, so
    # exact 0.02/0.04-degree grids appear as values such as 0.020000457....
    # Normalize before selector-to-cell math to avoid phantom boundary tiles.
    return round(float(axis["step"]), 5)


def load_dataset(root: Path, name: str) -> Dataset:
    metadata_name, binary_name = DATASET_FILES[name]
    metadata = json.loads((root / metadata_name).read_text(encoding="utf-8"))
    raw = (root / binary_name).read_bytes()
    grid_payload = metadata["grid"]
    grid = Grid(
        width=int(grid_payload["width"]),
        height=int(grid_payload["height"]),
        lon_start=float(grid_payload["lon"]["start"]),
        lat_start=float(grid_payload["lat"]["start"]),
        lon_step=_axis_step(grid_payload["lon"]),
        lat_step=_axis_step(grid_payload["lat"]),
    )
    encoding = metadata["encoding"]
    encoding_format = str(encoding["format"])
    if encoding_format == "packed-uint4-cloud-cover":
        missing = int(encoding["missing_code"])
        expected = (grid.width * grid.height + 1) // 2
        if len(raw) != expected:
            raise ValueError(f"{binary_name}: expected {expected} bytes, found {len(raw)}")
        values = unpack_cloud_codes(raw, grid.width * grid.height)
        cell_bytes = 1
    else:
        missing = int(encoding["missing_value"])
        cell_bytes = 2 if encoding_format == "int8-interleaved-u-v" else 1
        expected = grid.width * grid.height * cell_bytes
        if len(raw) != expected:
            raise ValueError(f"{binary_name}: expected {expected} bytes, found {len(raw)}")
        values = raw
    return Dataset(
        name=name,
        grid=grid,
        encoding_format=encoding_format,
        missing_value=missing,
        cell_bytes=cell_bytes,
        values=values,
        step_count=len(metadata.get("steps") or []),
    )


def tile_core_for_grid(fine_core: tuple[int, int], grid: Grid) -> tuple[int, int]:
    lon_ratio = max(1, round(abs(grid.lon_step) / FINE_GRID_STEP))
    lat_ratio = max(1, round(abs(grid.lat_step) / FINE_GRID_STEP))
    if fine_core[0] % lon_ratio or fine_core[1] % lat_ratio:
        raise ValueError(f"fine tile {fine_core} does not align with grid stride {(lon_ratio, lat_ratio)}")
    return fine_core[0] // lon_ratio, fine_core[1] // lat_ratio


def tile_grid_shape(grid: Grid, core: tuple[int, int]) -> tuple[int, int]:
    return math.ceil(grid.width / core[0]), math.ceil(grid.height / core[1])


def bbox_cell_range(grid: Grid, bbox: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
    lon_min, lat_min, lon_max, lat_max = bbox
    x0 = math.floor((lon_min - grid.lon_start) / grid.lon_step)
    x1 = math.ceil((lon_max - grid.lon_start) / grid.lon_step)
    y0 = math.floor((lat_min - grid.lat_start) / grid.lat_step)
    y1 = math.ceil((lat_max - grid.lat_start) / grid.lat_step)
    return (
        max(0, min(grid.width - 1, x0)),
        max(0, min(grid.height - 1, y0)),
        max(0, min(grid.width - 1, x1)),
        max(0, min(grid.height - 1, y1)),
    )


def tiles_for_bbox(
    grid: Grid, core: tuple[int, int], bbox: tuple[float, float, float, float]
) -> set[tuple[int, int]]:
    x0, y0, x1, y1 = bbox_cell_range(grid, bbox)
    return {
        (tx, ty)
        for ty in range(y0 // core[1], y1 // core[1] + 1)
        for tx in range(x0 // core[0], x1 // core[0] + 1)
    }


def _valid_core_size(grid: Grid, core: tuple[int, int], tx: int, ty: int) -> tuple[int, int]:
    return max(0, min(core[0], grid.width - tx * core[0])), max(
        0, min(core[1], grid.height - ty * core[1])
    )


def make_container(
    datasets: Sequence[Dataset],
    sections: Sequence[bytes],
    core: tuple[int, int],
    tx: int,
    ty: int,
    payload_width: int,
    payload_height: int,
) -> bytes:
    if len(datasets) != len(sections) or not datasets:
        raise ValueError("container needs matching non-empty datasets and sections")
    grid = datasets[0].grid
    if any(dataset.grid != grid for dataset in datasets):
        raise ValueError("grouped channels must share one grid")

    header_bytes = 48 + 16 * len(sections)
    payload = b"".join(sections)
    valid_width, valid_height = _valid_core_size(grid, core, tx, ty)
    touches_domain_edge = (
        tx == 0
        or ty == 0
        or tx * core[0] + core[0] >= grid.width
        or ty * core[1] + core[1] >= grid.height
    )
    flags = 1 if touches_domain_edge else 0
    if len(sections) > 1:
        flags |= 2
    base = struct.pack(
        "<4sBBHHHHBBHHHHHHIIIII",
        b"XVT1",
        1,
        0,
        header_bytes,
        flags,
        tx,
        ty,
        HALO,
        len(sections),
        core[0],
        core[1],
        valid_width,
        valid_height,
        payload_width,
        payload_height,
        grid.width,
        grid.height,
        len(payload),
        zlib.crc32(payload) & 0xFFFFFFFF,
        0,
    )
    if len(base) != 48:
        raise AssertionError(f"base header is {len(base)} bytes, expected 48")

    directory = bytearray()
    offset = 0
    value_count = payload_width * payload_height
    for dataset, section in zip(datasets, sections):
        encoding_id, component_count = ENCODING_IDS[dataset.encoding_format]
        directory.extend(
            struct.pack(
                "<HBBIII",
                CHANNEL_IDS[dataset.name],
                encoding_id,
                component_count,
                offset,
                len(section),
                value_count,
            )
        )
        offset += len(section)
    return base + bytes(directory) + payload


def tile_container(datasets: Sequence[Dataset], core: tuple[int, int], tx: int, ty: int) -> bytes:
    width = core[0] + 2 * HALO
    height = core[1] + 2 * HALO
    x_start = tx * core[0] - HALO
    y_start = ty * core[1] - HALO
    sections = [dataset.encode_window(x_start, y_start, width, height) for dataset in datasets]
    return make_container(datasets, sections, core, tx, ty, width, height)


def ideal_container(datasets: Sequence[Dataset], bbox: tuple[float, float, float, float]) -> bytes:
    grid = datasets[0].grid
    x0, y0, x1, y1 = bbox_cell_range(grid, bbox)
    width = x1 - x0 + 1
    height = y1 - y0 + 1
    sections = [dataset.encode_window(x0, y0, width, height) for dataset in datasets]
    return make_container(datasets, sections, (width, height), 0, 0, width, height)


def gzip_size(payload: bytes, level: int) -> int:
    return len(gzip.compress(payload, compresslevel=level, mtime=0))


def archive_index_size(
    group_name: str,
    sizes: dict[tuple[int, int], int],
    level: int,
) -> int:
    offset = 16
    blocks = []
    for (tx, ty), length in sorted(sizes.items(), key=lambda item: (item[0][1], item[0][0])):
        blocks.append({"x": tx, "y": ty, "offset": offset, "length": length})
        offset += length
    payload = {
        "contract": "xcbenz-value-tiles",
        "contract_version": "1.0.0",
        "group": group_name,
        "archive": "step.xva",
        "blocks": blocks,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return gzip_size(encoded, level)


def parse_tile_sizes(value: str) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    for item in value.split(","):
        width, separator, height = item.strip().lower().partition("x")
        if not separator:
            raise argparse.ArgumentTypeError(f"invalid tile size {item!r}; expected WIDTHxHEIGHT")
        parsed = (int(width), int(height))
        if min(parsed) <= 0:
            raise argparse.ArgumentTypeError("tile sizes must be positive")
        result.append(parsed)
    return result


def _group_definitions(datasets: dict[str, Dataset]) -> dict[str, tuple[Dataset, ...]]:
    return {
        "wind": (datasets["wind"],),
        "sunrain": (datasets["sunrain"],),
        "rain": (datasets["rain"],),
        "cloud_total": (datasets["cloud_total"],),
        "cloud_low": (datasets["cloud_low"],),
        "cloud_mid": (datasets["cloud_mid"],),
        "cloud_high": (datasets["cloud_high"],),
        "cloud4": tuple(datasets[name] for name in CLOUD_DATASETS),
    }


ORDINARY_MODE_GROUPS = {
    "wind_selected_level": ("wind",),
    "sunrain": ("sunrain",),
    "cloud_total": (*CLOUD_DATASETS, "rain"),
    "cloud_low": ("cloud_low", "rain"),
    "cloud_stack": (*CLOUD_DATASETS, "rain"),
}

HYBRID_MODE_GROUPS = {
    "wind_selected_level": ("wind",),
    "sunrain": ("sunrain",),
    "cloud_total": ("cloud4", "rain"),
    "cloud_low": ("cloud_low", "rain"),
    "cloud_stack": ("cloud4", "rain"),
}

IDEAL_MODE_GROUPS = ORDINARY_MODE_GROUPS


def _tile_sizes_by_group(
    groups: dict[str, tuple[Dataset, ...]], fine_core: tuple[int, int], level: int
) -> tuple[
    dict[str, dict[tuple[int, int], int]],
    dict[str, tuple[int, int]],
    dict[str, int],
]:
    sizes: dict[str, dict[tuple[int, int], int]] = {}
    cores: dict[str, tuple[int, int]] = {}
    index_sizes: dict[str, int] = {}
    for name, datasets in groups.items():
        core = tile_core_for_grid(fine_core, datasets[0].grid)
        cores[name] = core
        tiles_x, tiles_y = tile_grid_shape(datasets[0].grid, core)
        group_sizes = {
            (tx, ty): gzip_size(tile_container(datasets, core, tx, ty), level)
            for ty in range(tiles_y)
            for tx in range(tiles_x)
        }
        sizes[name] = group_sizes
        index_sizes[name] = archive_index_size(name, group_sizes, level)
    return sizes, cores, index_sizes


def _keys_for_view(
    group_names: Sequence[str],
    groups: dict[str, tuple[Dataset, ...]],
    cores: dict[str, tuple[int, int]],
    bbox: tuple[float, float, float, float],
) -> set[tuple[str, int, int]]:
    keys: set[tuple[str, int, int]] = set()
    for group_name in group_names:
        grid = groups[group_name][0].grid
        keys.update((group_name, tx, ty) for tx, ty in tiles_for_bbox(grid, cores[group_name], bbox))
    return keys


def _key_bytes(key: tuple[str, int, int], sizes: dict[str, dict[tuple[int, int], int]]) -> int:
    group_name, tx, ty = key
    return sizes[group_name][(tx, ty)]


def _mode_metrics(
    package: str,
    mode: str,
    group_names: Sequence[str],
    groups: dict[str, tuple[Dataset, ...]],
    sizes: dict[str, dict[tuple[int, int], int]],
    cores: dict[str, tuple[int, int]],
    index_sizes: dict[str, int],
    level: int,
) -> dict:
    views: dict[str, dict] = {}
    keys_by_view: dict[str, set[tuple[str, int, int]]] = {}
    for selector, bbox in SELECTORS.items():
        keys = _keys_for_view(group_names, groups, cores, bbox)
        keys_by_view[selector] = keys
        block_bytes = sum(_key_bytes(key, sizes) for key in keys)
        index_bytes = sum(index_sizes[name] for name in group_names) if package == "range_archives" else 0
        requests = len(keys) + (len(group_names) if package == "range_archives" else 0)
        ideal_bytes = sum(gzip_size(ideal_container(groups[name], bbox), level) for name in IDEAL_MODE_GROUPS[mode])
        total_bytes = block_bytes + index_bytes
        views[selector] = {
            "compressed_bytes": total_bytes,
            "requests": requests,
            "blocks": len(keys),
            "index_bytes": index_bytes,
            "ideal_crop_bytes": ideal_bytes,
            "overfetch_pct": round((total_bytes / ideal_bytes - 1.0) * 100.0, 1) if ideal_bytes else 0.0,
        }

    transitions: dict[str, dict] = {}
    for source, target in TRANSITIONS:
        source_keys = keys_by_view[source]
        target_keys = keys_by_view[target]
        shared = source_keys & target_keys
        new = target_keys - source_keys
        target_block_bytes = sum(_key_bytes(key, sizes) for key in target_keys)
        shared_bytes = sum(_key_bytes(key, sizes) for key in shared)
        transitions[f"{source}->{target}"] = {
            "new_compressed_bytes": sum(_key_bytes(key, sizes) for key in new),
            "new_requests": len(new),
            "target_cache_reuse_pct": round(shared_bytes / target_block_bytes * 100.0, 1)
            if target_block_bytes
            else 100.0,
        }
    return {"views": views, "transitions": transitions}


def _published_file_counts(
    datasets: dict[str, Dataset],
    groups: dict[str, tuple[Dataset, ...]],
    cores: dict[str, tuple[int, int]],
    retained_run_slots: int,
    wind_levels: int,
) -> dict[str, dict[str, int]]:
    wind_tiles = len(
        sizes_for_shape(datasets["wind"].grid, cores["wind"])
    )
    fine_tiles = len(
        sizes_for_shape(datasets["rain"].grid, cores["rain"])
    )
    wind_steps = datasets["wind"].step_count * wind_levels
    sunrain_steps = datasets["sunrain"].step_count
    rain_steps = datasets["rain"].step_count
    cloud_steps = sum(datasets[name].step_count for name in CLOUD_DATASETS)
    cloud_group_steps = max(datasets[name].step_count for name in CLOUD_DATASETS)
    current_payloads = wind_steps + sunrain_steps + rain_steps + cloud_steps
    ordinary_payloads = wind_steps * wind_tiles + (sunrain_steps + rain_steps + cloud_steps) * fine_tiles
    hybrid_payloads = wind_steps * wind_tiles + (
        sunrain_steps + rain_steps + cloud_steps + cloud_group_steps
    ) * fine_tiles
    archive_files = current_payloads * 2  # one archive and one index per existing channel-step
    return {
        "current_whole_grid": {
            "per_model_run": current_payloads,
            "retained_two_models": current_payloads * retained_run_slots,
        },
        "ordinary_chunks": {
            "per_model_run": ordinary_payloads,
            "retained_two_models": ordinary_payloads * retained_run_slots,
        },
        "range_archives": {
            "per_model_run": archive_files,
            "retained_two_models": archive_files * retained_run_slots,
        },
        "hybrid_cloud_channels": {
            "per_model_run": hybrid_payloads,
            "retained_two_models": hybrid_payloads * retained_run_slots,
        },
    }


def sizes_for_shape(grid: Grid, core: tuple[int, int]) -> set[tuple[int, int]]:
    tiles_x, tiles_y = tile_grid_shape(grid, core)
    return {(tx, ty) for ty in range(tiles_y) for tx in range(tiles_x)}


def analyze(
    datasets: dict[str, Dataset],
    tile_sizes: Iterable[tuple[int, int]],
    gzip_level: int = 9,
    retained_run_slots: int = 8,
    wind_levels: int = 8,
) -> dict:
    groups = _group_definitions(datasets)
    candidates: dict[str, dict] = {}
    for fine_core in tile_sizes:
        sizes, cores, index_sizes = _tile_sizes_by_group(groups, fine_core, gzip_level)
        packages = {}
        for package, modes in (
            ("ordinary_chunks", ORDINARY_MODE_GROUPS),
            ("range_archives", ORDINARY_MODE_GROUPS),
            ("hybrid_cloud_channels", HYBRID_MODE_GROUPS),
        ):
            packages[package] = {
                mode: _mode_metrics(
                    package,
                    mode,
                    group_names,
                    groups,
                    sizes,
                    cores,
                    index_sizes,
                    gzip_level,
                )
                for mode, group_names in modes.items()
            }
        candidates[f"{fine_core[0]}x{fine_core[1]}"] = {
            "fine_grid_core_cells": list(fine_core),
            "wind_grid_core_cells": list(cores["wind"]),
            "fine_grid_tiles": len(sizes["rain"]),
            "wind_grid_tiles": len(sizes["wind"]),
            "published_files": _published_file_counts(
                datasets, groups, cores, retained_run_slots, wind_levels
            ),
            "packages": packages,
        }

    return {
        "method": {
            "compression": f"gzip level {gzip_level}, deterministic mtime=0",
            "body_bytes_only": True,
            "tile_container": "XVT1 48-byte base header plus 16-byte channel entries",
            "halo_cells": HALO,
            "selectors": {name: list(bbox) for name, bbox in SELECTORS.items()},
            "cloud_total_behavior": "current frontend loads total, low, mid, high, and rain",
            "hybrid_cloud_behavior": (
                "individual total/low/mid/high tiles plus cloud4 for stack/current-total; rain remains separate"
            ),
            "range_archive_index": "one gzip JSON index and one archive per existing channel-step",
        },
        "sample": {
            name: {
                "grid": [dataset.grid.width, dataset.grid.height],
                "step": [dataset.grid.lon_step, dataset.grid.lat_step],
                "encoding": dataset.encoding_format,
                "step_count": dataset.step_count,
            }
            for name, dataset in datasets.items()
        },
        "candidates": candidates,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-root", type=Path, required=True)
    parser.add_argument(
        "--tile-sizes",
        type=parse_tile_sizes,
        default=parse_tile_sizes("64x64,96x64,128x96,160x112,192x128,256x128,256x192"),
        help="comma-separated core dimensions on the 0.02-degree grid",
    )
    parser.add_argument("--gzip-level", type=int, default=9, choices=range(1, 10))
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    datasets = {name: load_dataset(args.sample_root, name) for name in DATASET_FILES}
    result = analyze(datasets, args.tile_sizes, gzip_level=args.gzip_level)
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
