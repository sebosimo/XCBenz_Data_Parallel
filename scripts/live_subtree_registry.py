from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REGISTRY_ID = "xcbenz-live-subtrees"
REGISTRY_SCHEMA_VERSION = 1
SAFE_SUBTREE = re.compile(r"[a-z][a-z0-9_]*")


def subtree_digest(subtrees: tuple[str, ...]) -> str:
    payload = b"\0".join(subtree.encode("ascii") for subtree in subtrees) + b"\0"
    return hashlib.sha256(payload).hexdigest()


def load_live_subtrees(path: Path) -> tuple[str, ...]:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("live-subtree registry must be a JSON object")
    if payload.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise ValueError("live-subtree registry schema version is unsupported")
    if payload.get("registry_id") != REGISTRY_ID:
        raise ValueError("live-subtree registry identity is invalid")
    values = payload.get("subtrees")
    if not isinstance(values, list) or not values:
        raise ValueError("live-subtree registry must contain subtrees")
    if any(not isinstance(value, str) or SAFE_SUBTREE.fullmatch(value) is None for value in values):
        raise ValueError("live-subtree registry contains an unsafe path segment")
    subtrees = tuple(values)
    if subtrees != tuple(sorted(set(subtrees))):
        raise ValueError("live-subtree registry must be sorted and unique")
    if payload.get("subtree_digest") != subtree_digest(subtrees):
        raise ValueError("live-subtree registry digest does not match its contents")
    return subtrees


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and print live-owned subtrees")
    parser.add_argument("registry", type=Path)
    parser.add_argument("--shell-words", action="store_true")
    args = parser.parse_args()
    subtrees = load_live_subtrees(args.registry)
    if args.shell_words:
        print(" ".join(subtrees))
    else:
        print("\n".join(subtrees))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
