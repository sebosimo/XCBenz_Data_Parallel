#!/usr/bin/env python3
"""Build a deterministic, non-advertised XWS2 Wind streamline shadow package."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

for thread_variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(thread_variable, "1")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wind_streamline_tiles import (
    COMPACT_VARIANT_PRESETS,
    DEFAULT_PROFILE_NAMES,
    PROFILES,
    build_shadow_package,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path, help="Wind level metadata.json")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--steps",
        help="Comma-separated step labels; defaults to every metadata step",
    )
    parser.add_argument(
        "--profiles",
        default=",".join(DEFAULT_PROFILE_NAMES),
        help=f"Comma-separated profiles ({','.join(PROFILES)})",
    )
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument(
        "--variant",
        choices=tuple(COMPACT_VARIANT_PRESETS),
        default="baseline",
        help="Named size/quality experiment preset",
    )
    parser.add_argument(
        "--simplify-px",
        type=float,
        help="Override the preset simplification tolerance",
    )
    parser.add_argument(
        "--quantization-bits",
        type=int,
        choices=range(12, 17),
        help="Override the preset coordinate quantization",
    )
    parser.add_argument(
        "--integration",
        choices=("vectorized", "scalar"),
        default="vectorized",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    steps = (
        [item.strip() for item in args.steps.split(",") if item.strip()]
        if args.steps
        else None
    )
    profiles = [
        item.strip() for item in args.profiles.split(",") if item.strip()
    ]
    preset = COMPACT_VARIANT_PRESETS[args.variant]
    quantization_maximum = (
        (1 << args.quantization_bits) - 1
        if args.quantization_bits is not None
        else preset["quantization_maximum"]
    )
    result = build_shadow_package(
        args.metadata,
        args.output_dir,
        steps=steps,
        profile_names=profiles,
        simplify_tolerance_px=(
            args.simplify_px
            if args.simplify_px is not None
            else preset["simplification_tolerance_px"]
        ),
        workers=args.workers,
        integration_mode=args.integration,
        experiment_variant=args.variant,
        quantization_maximum=quantization_maximum,
        collapse_quantized_duplicates=preset["collapse_quantized_duplicates"],
        trajectory_scales=preset["trajectory_scales"],
    )
    manifest = result["manifest"]
    benchmark = result["benchmark"]
    print(
        json.dumps(
            {
                "output": str(args.output_dir.resolve()),
                "revision": manifest["revision"],
                "counts": manifest["counts"],
                "workers": benchmark["workers"],
                "wall_ms": benchmark["wall_ms"],
                "maximum_worker_peak_rss_bytes": benchmark[
                    "maximum_worker_peak_rss_bytes"
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
