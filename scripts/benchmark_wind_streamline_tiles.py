#!/usr/bin/env python3
"""Generate an experimental full-domain, multi-resolution Wind tile package."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wind_streamline_feasibility import TILE_PROFILES, build_tile_package


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", type=Path, help="Wind level metadata.json")
    parser.add_argument("--step", default="H00")
    parser.add_argument(
        "--profiles",
        default=(
            "compact-overview,compact-regional,shared-detail,"
            "wide-overview,wide-regional"
        ),
        help=f"Comma-separated profiles ({','.join(TILE_PROFILES)})",
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--simplify-px",
        type=float,
        default=0.15,
        help="Douglas-Peucker tolerance in 512px tile coordinates",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    names = [item.strip() for item in args.profiles.split(",") if item.strip()]
    unknown = sorted(set(names) - set(TILE_PROFILES))
    if unknown:
        raise SystemExit(f"Unknown tile profile(s): {', '.join(unknown)}")
    result = build_tile_package(
        args.metadata,
        args.step,
        args.output_dir,
        profiles=[TILE_PROFILES[name] for name in names],
        simplify_tolerance_px=args.simplify_px,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
