#!/usr/bin/env python3
"""Benchmark retained spatial value-tile file creation, traversal, and deletion."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence


DEFAULT_FILE_COUNT = 66_144
TILES_PER_STEP = 12


def benchmark_fixture(root: Path, *, file_count: int, payload_bytes: int = 1) -> dict[str, Any]:
    if file_count <= 0 or payload_bytes < 0:
        raise ValueError("file_count must be positive and payload_bytes must be non-negative")
    root = Path(root).resolve()
    if root.exists():
        raise FileExistsError(f"benchmark fixture root already exists: {root}")
    root.mkdir(parents=True)
    marker = root / ".xcbenz-value-tile-benchmark"
    marker.write_text("disposable benchmark fixture\n", encoding="utf-8")
    payload = bytes(payload_bytes)

    create_started = time.perf_counter()
    created = 0
    run_slots = 8
    group = 0
    while created < file_count:
        slot = group % run_slots
        model = "icon-ch1" if slot < 4 else "icon-ch2"
        run = f"202607{12 + slot:02d}_{(slot % 4) * 6:02d}00"
        revision = f"{slot + 1:012x}"
        group_in_run = group // run_slots
        variant = f"fixture_{group_in_run // 128:02d}"
        step = f"H{group_in_run % 128:03d}"
        step_root = root / "value_tiles" / "v1" / model / run / revision / "fixture" / variant / step
        step_root.mkdir(parents=True, exist_ok=True)
        for tile_index in range(TILES_PER_STEP):
            if created >= file_count:
                break
            tile_y, tile_x = divmod(tile_index, 4)
            (step_root / f"t{tile_y}_{tile_x}.xvt").write_bytes(payload)
            created += 1
        group += 1
    create_seconds = time.perf_counter() - create_started

    traverse_started = time.perf_counter()
    traversed = sum(1 for path in root.rglob("*.xvt") if path.is_file())
    traversal_seconds = time.perf_counter() - traverse_started
    if traversed != file_count:
        raise RuntimeError(f"fixture traversal found {traversed} files, expected {file_count}")

    delete_started = time.perf_counter()
    if not marker.exists() or marker.parent != root:
        raise RuntimeError("refusing to delete an unmarked benchmark fixture")
    shutil.rmtree(root)
    deletion_seconds = time.perf_counter() - delete_started

    return {
        "file_count": file_count,
        "payload_bytes_per_file": payload_bytes,
        "create_seconds": round(create_seconds, 3),
        "traversal_seconds": round(traversal_seconds, 3),
        "deletion_seconds": round(deletion_seconds, 3),
        "files_per_second_create": round(file_count / create_seconds, 1),
        "files_per_second_traversal": round(file_count / traversal_seconds, 1),
        "files_per_second_delete": round(file_count / deletion_seconds, 1),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=DEFAULT_FILE_COUNT)
    parser.add_argument("--payload-bytes", type=int, default=1)
    parser.add_argument("--workspace", type=Path, default=Path(tempfile.gettempdir()))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.workspace.mkdir(parents=True, exist_ok=True)
    fixture = Path(tempfile.mkdtemp(prefix="xcb_value_tile_fs_parent_", dir=args.workspace))
    fixture.rmdir()
    result = benchmark_fixture(fixture, file_count=args.files, payload_bytes=args.payload_bytes)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
