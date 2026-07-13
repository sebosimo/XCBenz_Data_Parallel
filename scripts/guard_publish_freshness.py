#!/usr/bin/env python3
"""Refuse to replace production with an older forecast manifest."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


RUN_TAG = re.compile(r"^\d{8}_\d{4}$")
MODEL_PATHS = {
    "ch1": (("models", "icon-ch1", "runs"), ("runs",)),
    "ch2": (("models", "icon-ch2", "runs"), ("runs_ch2",)),
}


def load_manifest(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"manifest is not a JSON object: {path}")
    return payload


def value_at_path(payload: dict[str, object], path: tuple[str, ...]) -> object:
    value: object = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def runs_for_model(manifest: dict[str, object], model: str) -> dict[str, object]:
    for path in MODEL_PATHS[model]:
        runs = value_at_path(manifest, path)
        if isinstance(runs, dict):
            return runs
    return {}


def latest_run_tag(manifest: dict[str, object], model: str) -> str | None:
    tags = [
        tag
        for tag in runs_for_model(manifest, model)
        if isinstance(tag, str) and RUN_TAG.fullmatch(tag)
    ]
    return max(tags) if tags else None


def downgrade_reasons(
    candidate: dict[str, object], current: dict[str, object]
) -> list[str]:
    reasons: list[str] = []
    for model in MODEL_PATHS:
        candidate_tag = latest_run_tag(candidate, model)
        current_tag = latest_run_tag(current, model)
        if current_tag and not candidate_tag:
            reasons.append(f"{model}:candidate_missing;current={current_tag}")
        elif current_tag and candidate_tag and candidate_tag < current_tag:
            reasons.append(
                f"{model}:candidate={candidate_tag};current={current_tag}"
            )
    return reasons


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prevent an older forecast snapshot from replacing production."
    )
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--current", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    candidate = load_manifest(args.candidate)
    current = load_manifest(args.current)
    reasons = downgrade_reasons(candidate, current)
    if reasons:
        print(
            "[publish-guard] Refusing forecast downgrade: " + ", ".join(reasons)
        )
        return 43

    summary = []
    for model in MODEL_PATHS:
        summary.append(
            f"{model}:candidate={latest_run_tag(candidate, model) or 'none'};"
            f"current={latest_run_tag(current, model) or 'none'}"
        )
    print("[publish-guard] Candidate is not older: " + ", ".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
