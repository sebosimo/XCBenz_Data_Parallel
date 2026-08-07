#!/usr/bin/env python3
"""Refuse forecast downgrades and retained-history loss."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from forecast_retention import (
    FORECAST_PRODUCT_MANIFESTS,
    load_json_object,
    manifest_run_tags,
    product_manifest_path,
    required_retained_runs,
    valid_run_tags,
)


MODELS = ("icon-ch1", "icon-ch2")
MODEL_LABELS = {"icon-ch1": "ch1", "icon-ch2": "ch2"}


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"manifest is not a JSON object: {path}")
    return payload


def latest_run_tag(manifest: dict[str, Any], model: str) -> str | None:
    model_key = "icon-ch1" if model == "ch1" else "icon-ch2"
    tags = valid_run_tags(manifest_run_tags(manifest, model_key))
    return max(tags) if tags else None


def downgrade_reasons(candidate: dict[str, Any], current: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    for model in ("ch1", "ch2"):
        candidate_tag = latest_run_tag(candidate, model)
        current_tag = latest_run_tag(current, model)
        if current_tag and not candidate_tag:
            reasons.append(f"{model}:candidate_missing;current={current_tag}")
        elif current_tag and candidate_tag and candidate_tag < current_tag:
            reasons.append(
                f"{model}:candidate={candidate_tag};current={current_tag}"
            )
    return reasons


def retained_history_reasons(
    candidate: dict[str, Any], current: dict[str, Any]
) -> list[str]:
    reasons: list[str] = []
    for model_key in MODELS:
        candidate_runs = manifest_run_tags(candidate, model_key)
        current_runs = manifest_run_tags(current, model_key)
        required = required_retained_runs(current_runs, candidate_runs, model_key=model_key)
        missing = sorted(required - candidate_runs, reverse=True)
        if missing:
            reasons.append(
                f"{MODEL_LABELS[model_key]}:missing_retained={','.join(missing)}"
            )
    return reasons


def _latest(tags: set[str]) -> str | None:
    valid = valid_run_tags(tags)
    return valid[0] if valid else None


def product_history_reasons(
    candidate_root: Path,
    current_root: Path,
    *,
    required_products: tuple[str, ...],
    require_value_tiles: bool,
) -> list[str]:
    reasons: list[str] = []
    candidate = load_json_object(candidate_root / "manifest.json")

    products = list(required_products)
    if require_value_tiles:
        products.append("value_tiles")

    for product in products:
        if product not in FORECAST_PRODUCT_MANIFESTS:
            raise ValueError(f"Unsupported guarded forecast product: {product}")
        candidate_path = product_manifest_path(candidate_root, product)
        current_path = product_manifest_path(current_root, product)
        candidate_product = load_json_object(candidate_path, missing_ok=True)
        current_product = load_json_object(current_path, missing_ok=True)

        for model_key in MODELS:
            candidate_runs = manifest_run_tags(candidate_product, model_key)
            current_runs = manifest_run_tags(current_product, model_key)
            candidate_latest = _latest(manifest_run_tags(candidate, model_key))
            policy_candidate = {candidate_latest} if candidate_latest else set()
            required = required_retained_runs(
                current_runs,
                policy_candidate,
                model_key=model_key,
            )
            missing = sorted(required - candidate_runs, reverse=True)
            if missing:
                reasons.append(
                    f"{product}/{MODEL_LABELS[model_key]}:missing={','.join(missing)}"
                )
    return reasons


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prevent an older forecast snapshot from replacing production."
    )
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--current", type=Path)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--current-root", type=Path)
    parser.add_argument(
        "--require-products",
        default="",
        help="Comma-separated whole-grid products that must retain history.",
    )
    parser.add_argument("--require-value-tiles", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    candidate_path = args.candidate or (
        args.candidate_root / "manifest.json" if args.candidate_root else None
    )
    current_path = args.current or (
        args.current_root / "manifest.json" if args.current_root else None
    )
    if candidate_path is None or current_path is None:
        raise SystemExit("Provide candidate/current manifests or candidate/current roots")
    candidate = load_manifest(candidate_path)
    current = load_manifest(current_path)
    reasons = downgrade_reasons(candidate, current)
    if reasons:
        print(
            "[publish-guard] Refusing forecast downgrade: " + ", ".join(reasons)
        )
        return 43

    history_reasons = retained_history_reasons(candidate, current)
    if args.require_products or args.require_value_tiles:
        if args.candidate_root is None or args.current_root is None:
            raise SystemExit("Product history checks require candidate and current roots")
        required_products = tuple(
            product.strip() for product in args.require_products.split(",") if product.strip()
        )
        history_reasons.extend(
            product_history_reasons(
                args.candidate_root,
                args.current_root,
                required_products=required_products,
                require_value_tiles=args.require_value_tiles,
            )
        )
    if history_reasons:
        print(
            "[publish-guard] Refusing retained forecast history loss: "
            + "; ".join(history_reasons)
        )
        return 44

    summary = []
    for model in ("ch1", "ch2"):
        summary.append(
            f"{model}:candidate={latest_run_tag(candidate, model) or 'none'};"
            f"current={latest_run_tag(current, model) or 'none'}"
        )
    print("[publish-guard] Candidate is not older: " + ", ".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
