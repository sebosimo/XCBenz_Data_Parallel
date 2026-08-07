#!/usr/bin/env python3
"""Check whether local web exports contain authoritative retained history."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from forecast_retention import (
    FORECAST_PRODUCT_MANIFESTS,
    load_json_object,
    manifest_run_tags,
    product_manifest_path,
    required_retained_runs,
)


MODELS = ("icon-ch1", "icon-ch2")
DEFAULT_PRODUCTS = ("wind", "sunshine", "rain", "sunrain", "cloud")


def required_current_runs(
    current_runs: set[str], intended_run: str, *, model_key: str
) -> set[str]:
    required = required_retained_runs(
        current_runs,
        {intended_run},
        model_key=model_key,
    )
    return required & current_runs


def history_gaps(
    local_root: Path,
    current_root: Path,
    *,
    intended_runs: dict[str, str],
    products: tuple[str, ...] = DEFAULT_PRODUCTS,
    include_value_tiles: bool = False,
) -> list[str]:
    gaps: list[str] = []
    current_manifest = load_json_object(current_root / "manifest.json")
    local_manifest = load_json_object(local_root / "manifest.json", missing_ok=True)

    for model_key in MODELS:
        current_runs = manifest_run_tags(current_manifest, model_key)
        local_runs = manifest_run_tags(local_manifest, model_key)
        required = required_current_runs(
            current_runs,
            intended_runs[model_key],
            model_key=model_key,
        )
        for run in sorted(required - local_runs, reverse=True):
            gaps.append(f"root/{model_key}/{run}:manifest_missing")
        for run in sorted(required, reverse=True):
            if not (local_root / "region_forecasts" / model_key / run).is_dir():
                gaps.append(f"root/{model_key}/{run}:region_forecasts_missing")
            if not (local_root / "emagrams" / model_key / run).is_dir():
                gaps.append(f"root/{model_key}/{run}:emagrams_missing")

    checked_products = list(products)
    if include_value_tiles:
        checked_products.append("value_tiles")

    for product in checked_products:
        if product not in FORECAST_PRODUCT_MANIFESTS:
            raise ValueError(f"Unsupported forecast product: {product}")
        current_path = product_manifest_path(current_root, product)
        if not current_path.exists():
            continue
        local_path = product_manifest_path(local_root, product)
        current_product = load_json_object(current_path)
        local_product = load_json_object(local_path, missing_ok=True)
        product_dir = FORECAST_PRODUCT_MANIFESTS[product].parents[0]
        for model_key in MODELS:
            current_runs = manifest_run_tags(current_product, model_key)
            local_runs = manifest_run_tags(local_product, model_key)
            required = required_current_runs(
                current_runs,
                intended_runs[model_key],
                model_key=model_key,
            )
            for run in sorted(required - local_runs, reverse=True):
                gaps.append(f"{product}/{model_key}/{run}:manifest_missing")
            for run in sorted(required, reverse=True):
                if not (local_root / product_dir / model_key / run).is_dir():
                    gaps.append(f"{product}/{model_key}/{run}:files_missing")
    return gaps


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check local retained forecast history against production."
    )
    parser.add_argument("--local-root", type=Path, required=True)
    parser.add_argument("--current-root", type=Path, required=True)
    parser.add_argument("--ch1-run-tag", required=True)
    parser.add_argument("--ch2-run-tag", required=True)
    parser.add_argument("--include-value-tiles", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    gaps = history_gaps(
        args.local_root,
        args.current_root,
        intended_runs={
            "icon-ch1": args.ch1_run_tag,
            "icon-ch2": args.ch2_run_tag,
        },
        include_value_tiles=args.include_value_tiles,
    )
    if gaps:
        print("[history-check] Local forecast history needs hydration:")
        for gap in gaps:
            print(f"[history-check]   {gap}")
        return 10
    print("[history-check] Local forecast history covers every retained production run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
