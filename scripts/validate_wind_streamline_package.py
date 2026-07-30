#!/usr/bin/env python3
"""Validate a generated XWS2 package before isolated beta2 publication."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from wind_streamline_tiles import validate_shadow_package


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--require-complete-pilot", action="store_true")
    args = parser.parse_args()
    result = validate_shadow_package(
        args.package,
        require_complete_pilot=args.require_complete_pilot,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
