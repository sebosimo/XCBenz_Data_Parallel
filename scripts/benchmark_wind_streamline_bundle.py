#!/usr/bin/env python3
"""Generate and size experimental precomputed Wind streamline bundles."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wind_streamline_feasibility import PRESENTATIONS, benchmark_bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path, help="Wind level metadata.json")
    parser.add_argument("--step", default="H00")
    parser.add_argument(
        "--profiles",
        default="desktop,mobile",
        help=f"Comma-separated profiles ({','.join(PRESENTATIONS)})",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--simplify-px",
        type=float,
        default=0.0,
        help="Douglas-Peucker tolerance in presentation CSS pixels",
    )
    parser.add_argument(
        "--clip-to-view",
        action="store_true",
        help="Clip geometry to the presentation viewport as a lower bound for visible vector tiles",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    profiles = [item.strip() for item in args.profiles.split(",") if item.strip()]
    unknown = sorted(set(profiles) - set(PRESENTATIONS))
    if unknown:
        raise SystemExit(f"Unknown presentation profile(s): {', '.join(unknown)}")
    results = []
    tolerance_tag = (
        "raw"
        if args.simplify_px <= 0
        else f"s{round(args.simplify_px * 100):03d}"
    )
    for profile in profiles:
        clip_tag = "-clip" if args.clip_to_view else ""
        output_path = args.output_dir / f"{args.step.lower()}-{profile}-{tolerance_tag}{clip_tag}.xws"
        results.append(
            benchmark_bundle(
                args.metadata,
                args.step,
                PRESENTATIONS[profile],
                output_path,
                simplify_tolerance_px=args.simplify_px,
                clip_to_view=args.clip_to_view,
            )
        )
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
