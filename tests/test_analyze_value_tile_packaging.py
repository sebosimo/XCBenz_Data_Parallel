from __future__ import annotations

import struct
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import analyze_value_tile_packaging as MODULE  # noqa: E402


def dataset(name: str, width: int, height: int, values: bytes, *, cloud: bool = False):
    return MODULE.Dataset(
        name=name,
        grid=MODULE.Grid(width, height, 4.0, 43.0, 0.02, 0.02),
        encoding_format="packed-uint4-cloud-cover" if cloud else "uint8-interleaved-components",
        missing_value=15 if cloud else 255,
        cell_bytes=1,
        values=values,
        step_count=1,
    )


def test_cloud_pack_round_trip_preserves_codes_and_odd_padding():
    codes = bytes([0, 1, 10, 15, 7])
    packed = MODULE.pack_cloud_codes(codes, 15)
    assert packed == bytes([0x10, 0xFA, 0xF7])
    assert MODULE.unpack_cloud_codes(packed, len(codes)) == codes


def test_window_has_one_cell_halo_and_missing_domain_padding():
    source = dataset("rain", 3, 2, bytes([1, 2, 3, 4, 5, 6]))
    window = source.encode_window(-1, -1, 4, 4)
    assert window == bytes(
        [
            255,
            255,
            255,
            255,
            255,
            1,
            2,
            3,
            255,
            4,
            5,
            6,
            255,
            255,
            255,
            255,
        ]
    )


def test_selector_uses_core_tiles_while_payload_halo_covers_edges():
    grid = MODULE.Grid(10, 8, 4.0, 43.0, 0.02, 0.02)
    bbox = (4.02, 43.02, 4.11, 43.11)
    assert MODULE.tiles_for_bbox(grid, (4, 4), bbox) == {
        (0, 0),
        (1, 0),
        (0, 1),
        (1, 1),
    }


def test_hybrid_cloud_group_preserves_single_layer_bytes_and_reduces_stack_requests():
    grid_values = bytes(range(16))
    cloud_values = bytes(value % 11 for value in grid_values)
    datasets = {
        "wind": MODULE.Dataset(
            "wind",
            MODULE.Grid(4, 4, 4.0, 43.0, 0.04, 0.04),
            "int8-interleaved-u-v",
            -128,
            2,
            bytes([1, 2] * 16),
            1,
        ),
        "sunrain": dataset("sunrain", 4, 4, grid_values),
        "rain": dataset("rain", 4, 4, grid_values),
        "cloud_total": dataset("cloud_total", 4, 4, cloud_values, cloud=True),
        "cloud_low": dataset("cloud_low", 4, 4, cloud_values, cloud=True),
        "cloud_mid": dataset("cloud_mid", 4, 4, cloud_values, cloud=True),
        "cloud_high": dataset("cloud_high", 4, 4, cloud_values, cloud=True),
    }
    result = MODULE.analyze(datasets, [(4, 4)])
    candidate = result["candidates"]["4x4"]["packages"]
    ordinary_stack = candidate["ordinary_chunks"]["cloud_stack"]["views"]["alps"]
    hybrid_stack = candidate["hybrid_cloud_channels"]["cloud_stack"]["views"]["alps"]
    ordinary_low = candidate["ordinary_chunks"]["cloud_low"]["views"]["alps"]
    hybrid_low = candidate["hybrid_cloud_channels"]["cloud_low"]["views"]["alps"]
    assert hybrid_stack["requests"] < ordinary_stack["requests"]
    assert hybrid_low["compressed_bytes"] == ordinary_low["compressed_bytes"]


def test_edge_padding_flag_covers_domain_halo_but_not_interior_tiles():
    source = dataset("rain", 6, 6, bytes(range(36)))
    edge = MODULE.tile_container((source,), (2, 2), 0, 1)
    interior = MODULE.tile_container((source,), (2, 2), 1, 1)
    edge_flags = struct.unpack_from("<H", edge, 8)[0]
    interior_flags = struct.unpack_from("<H", interior, 8)[0]
    assert edge_flags & 1
    assert not interior_flags & 1
