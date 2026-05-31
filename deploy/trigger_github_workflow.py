#!/usr/bin/env python3
"""Trigger the XCBenz data workflow from an external scheduler.

The script is intentionally small and dependency-free so it can run from cron
or systemd on the Hetzner server without touching the data-processing stack.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote


SUCCESS_STATUS_CODES = {200, 201, 202, 204}


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def add_common_argument(
    parser: argparse.ArgumentParser,
    name: str,
    env_name: str,
    default: str,
    help_text: str,
) -> None:
    parser.add_argument(
        name,
        default=env(env_name, default),
        help=f"{help_text} Defaults to ${env_name} or {default!r}.",
    )


@contextlib.contextmanager
def nonblocking_lock(lock_file: str):
    """Hold a best-effort non-blocking POSIX lock for this process."""
    if os.name != "posix":
        yield
        return

    import fcntl

    path = Path(lock_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"Another trigger process is already running; lock={lock_file}")
            raise SystemExit(0)

        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Trigger the XCBenz GitHub Actions data workflow."
    )
    add_common_argument(
        parser,
        "--repository",
        "XCBENZ_GITHUB_REPOSITORY",
        "sebosimo/XCBenz_Data_Parallel",
        "GitHub repository in owner/name form.",
    )
    add_common_argument(
        parser,
        "--workflow",
        "XCBENZ_GITHUB_WORKFLOW",
        "daily_plot.yml",
        "Workflow file name or workflow id.",
    )
    add_common_argument(
        parser,
        "--ref",
        "XCBENZ_GITHUB_REF",
        "main",
        "Git ref to dispatch.",
    )
    parser.add_argument(
        "--run-mode",
        choices=(
            "force-refresh",
            "standard",
            "deploy-data-host",
            "standard-deploy-data-host",
        ),
        default=env("XCBENZ_RUN_MODE", "standard-deploy-data-host"),
        help=(
            "Workflow run mode. The default checks for new data and deploys "
            "only when preflight decides there is work to do."
        ),
    )
    add_common_argument(
        parser,
        "--lock-file",
        "XCBENZ_TRIGGER_LOCK_FILE",
        "/run/lock/xcbenz-github-actions-trigger.lock",
        "Non-blocking lock file used to avoid duplicate local triggers.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(env("XCBENZ_GITHUB_API_TIMEOUT", "20")),
        help="GitHub API timeout in seconds.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the dispatch payload without calling GitHub.",
    )
    return parser


def build_payload(args: argparse.Namespace) -> dict[str, object]:
    return {
        "ref": args.ref,
        "return_run_details": True,
        "inputs": {"run_mode": args.run_mode},
    }


def token_from_env() -> str:
    token = env("XCBENZ_GITHUB_TOKEN") or env("GITHUB_TOKEN")
    if not token:
        raise SystemExit(
            "Missing token. Set XCBENZ_GITHUB_TOKEN in the service environment."
        )
    return token


def trigger(args: argparse.Namespace) -> int:
    payload = build_payload(args)
    if args.dry_run:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    token = token_from_env()
    owner_repo = quote(args.repository, safe="/")
    workflow = quote(args.workflow, safe="")
    url = (
        f"https://api.github.com/repos/{owner_repo}/actions/workflows/"
        f"{workflow}/dispatches"
    )
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "xcbenz-actions-trigger/1.0",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )

    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            status = response.status
    except urllib.error.HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        status = error.code
    except urllib.error.URLError as error:
        print(f"{started_at} GitHub dispatch failed: {error}", file=sys.stderr)
        return 1

    if status not in SUCCESS_STATUS_CODES:
        print(
            f"{started_at} GitHub dispatch failed with HTTP {status}: "
            f"{response_body[:4000]}",
            file=sys.stderr,
        )
        return 1

    if response_body.strip():
        print(f"{started_at} GitHub dispatch accepted: {response_body}")
    else:
        print(f"{started_at} GitHub dispatch accepted with HTTP {status}")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    with nonblocking_lock(args.lock_file):
        return trigger(args)


if __name__ == "__main__":
    raise SystemExit(main())
