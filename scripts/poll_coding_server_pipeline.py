#!/usr/bin/env python3
"""Cheap scheduler wrapper for the coding-server forecast pipeline.

The poller is intended to be called frequently by cron or a systemd timer. It
does small STAC probes first, records local state, and only starts the heavy
direct-output pipeline when a complete CH1/CH2 run pair is available and has not
already been published successfully.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import shlex
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
STATE_VERSION = 1
SEARCH_URL = "https://data.geo.admin.ch/api/stac/v1/search"
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class ModelConfig:
    key: str
    collection_id: str
    slot_hours: int
    lookback_slots: int
    terminal_horizon: int
    probe_variables: tuple[str, ...]


MODELS = {
    "ch1": ModelConfig(
        key="ch1",
        collection_id="ch.meteoschweiz.ogd-forecasting-icon-ch1",
        slot_hours=3,
        lookback_slots=16,
        terminal_horizon=33,
        probe_variables=(
            "T",
            "U",
            "V",
            "P",
            "QV",
            "U_10M",
            "V_10M",
            "TOT_PREC",
            "CLCT",
            "DURSUN",
            "DURSUN_M",
            "ASWDIR_S",
            "ASWDIFD_S",
        ),
    ),
    "ch2": ModelConfig(
        key="ch2",
        collection_id="ch.meteoschweiz.ogd-forecasting-icon-ch2",
        slot_hours=6,
        lookback_slots=20,
        terminal_horizon=120,
        probe_variables=(
            "T",
            "U",
            "V",
            "P",
            "QV",
            "U_10M",
            "V_10M",
            "TOT_PREC",
            "CLCT",
            "DURSUN",
            "DURSUN_M",
            "ASWDIR_S",
            "ASWDIFD_S",
        ),
    ),
}


def now_label() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(f"{now_label()} [pipeline-poller] {message}", flush=True)


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise SystemExit(f"{name} must be boolean-like, got {raw!r}")


def parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def python_command(raw: str) -> list[str]:
    parts = shlex.split(raw)
    if not parts:
        raise SystemExit("Python command is empty")
    return parts


def iso_horizon(hours: int) -> str:
    days = hours // 24
    remainder = hours % 24
    return f"P{days}DT{remainder}H"


def tag_for(reference_time: dt.datetime) -> str:
    return reference_time.strftime("%Y%m%d_%H%M")


def ref_for(reference_time: dt.datetime) -> str:
    return reference_time.strftime("%Y-%m-%dT%H:%M:%SZ")


def ch1_terminal_horizon(reference_time: dt.datetime) -> int:
    return 45 if reference_time.hour == 3 else 33


def terminal_horizon(model: str, reference_time: dt.datetime) -> int:
    if model == "ch1":
        return ch1_terminal_horizon(reference_time)
    return MODELS[model].terminal_horizon


def post_json(url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "XCBenz_Data_Parallel/pipeline-poller",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def stac_has_asset(config: ModelConfig, reference_time: dt.datetime, variable: str, horizon: int, timeout: int) -> bool:
    ref = ref_for(reference_time)
    payload = {
        "collections": [config.collection_id],
        "forecast:variable": variable,
        "forecast:reference_datetime": ref,
        "forecast:perturbed": False,
        "forecast:horizon": iso_horizon(horizon),
        "limit": 1,
    }
    response = post_json(SEARCH_URL, payload, timeout)
    for feature in response.get("features") or []:
        props = feature.get("properties") or {}
        if (
            props.get("forecast:reference_datetime") == ref
            and props.get("forecast:variable") == variable
            and props.get("forecast:perturbed") is False
        ):
            return True
    return False


def candidate_times(config: ModelConfig, now: dt.datetime) -> list[dt.datetime]:
    hour = (now.hour // config.slot_hours) * config.slot_hours
    start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return [start - dt.timedelta(hours=idx * config.slot_hours) for idx in range(config.lookback_slots)]


def run_is_complete(model: str, reference_time: dt.datetime, timeout: int) -> tuple[bool, str]:
    config = MODELS[model]
    horizon = terminal_horizon(model, reference_time)
    try:
        for variable in config.probe_variables:
            if not stac_has_asset(config, reference_time, variable, horizon, timeout):
                return False, f"missing {variable} H+{horizon:03d}"
        if not stac_has_asset(config, reference_time, "T", 0, timeout):
            return False, "missing T H+000"
    except Exception as exc:  # noqa: BLE001 - a failed probe should not start a heavy run.
        return False, f"probe failed: {exc}"
    return True, f"complete through H+{horizon:03d}"


def latest_complete_run(model: str, now: dt.datetime, timeout: int) -> tuple[str | None, list[dict[str, str]]]:
    report: list[dict[str, str]] = []
    for candidate in candidate_times(MODELS[model], now):
        tag = tag_for(candidate)
        complete, reason = run_is_complete(model, candidate, timeout)
        report.append({"run": tag, "status": "complete" if complete else "incomplete", "reason": reason})
        log(f"{model} probe {tag}: {'complete' if complete else 'incomplete'} ({reason})")
        if complete:
            return tag, report
    return None, report


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": STATE_VERSION}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - corrupt state should not block polling forever.
        log(f"state file unreadable, starting fresh: {path} ({exc})")
        return {"version": STATE_VERSION}
    if not isinstance(payload, dict):
        return {"version": STATE_VERSION}
    payload.setdefault("version", STATE_VERSION)
    return payload


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def pair_key(ch1: str, ch2: str) -> str:
    return f"ch1={ch1};ch2={ch2}"


def last_success_pair(state: dict[str, Any]) -> str | None:
    success = state.get("last_success")
    if not isinstance(success, dict):
        return None
    runs = success.get("runs")
    if not isinstance(runs, dict):
        return None
    ch1 = runs.get("ch1")
    ch2 = runs.get("ch2")
    if isinstance(ch1, str) and isinstance(ch2, str):
        return pair_key(ch1, ch2)
    return None


def retry_backoff_active(state: dict[str, Any], current_pair: str, retry_minutes: int, now: dt.datetime) -> bool:
    attempt = state.get("last_attempt")
    if not isinstance(attempt, dict):
        return False
    if attempt.get("pair") != current_pair or attempt.get("status") != "failed":
        return False
    raw_time = attempt.get("finished_at") or attempt.get("started_at")
    if not isinstance(raw_time, str):
        return False
    try:
        finished_at = dt.datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
    except ValueError:
        return False
    return now - finished_at < dt.timedelta(minutes=retry_minutes)


def pipeline_deploys(run_mode: str, skip_deploy: bool) -> bool:
    return run_mode in {"deploy-data-host", "standard-deploy-data-host"} and not skip_deploy


def build_pipeline_command(args: argparse.Namespace, ch1: str, ch2: str) -> list[str]:
    command = [
        *python_command(args.python_cmd),
        "scripts/run_coding_server_pipeline.py",
        "--run-mode",
        args.run_mode,
        "--ch1-run-tag",
        ch1,
        "--ch2-run-tag",
        ch2,
    ]
    if args.skip_deploy:
        command.append("--skip-deploy")
    if args.no_push_data_branch:
        command.append("--no-push-data-branch")
    if args.no_restore_web_exports:
        command.append("--no-restore-web-exports")
    return command


@contextlib.contextmanager
def local_lock(lock_file: str):
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
            log(f"another poller process is already running; lock={lock_file}")
            raise SystemExit(0)
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield


def run_poller(args: argparse.Namespace) -> int:
    now = dt.datetime.now(dt.timezone.utc)
    state_path = Path(args.state_file)
    if not state_path.is_absolute():
        state_path = REPO_ROOT / state_path
    state = load_state(state_path)

    latest: dict[str, str | None] = {}
    probe_reports: dict[str, list[dict[str, str]]] = {}
    for model in ("ch1", "ch2"):
        latest[model], probe_reports[model] = latest_complete_run(model, now, args.probe_timeout)

    state["last_poll"] = {
        "checked_at": now_label(),
        "latest_complete": latest,
        "probe_reports": probe_reports,
    }

    if not latest["ch1"] or not latest["ch2"]:
        state["last_poll"]["decision"] = "wait_for_complete_pair"
        write_state(state_path, state)
        log(f"waiting for complete pair: ch1={latest['ch1']!r} ch2={latest['ch2']!r}")
        return 0

    current_pair = pair_key(latest["ch1"], latest["ch2"])
    if not args.force_run and current_pair == last_success_pair(state):
        state["last_poll"]["decision"] = "already_successful"
        write_state(state_path, state)
        log(f"latest pair already succeeded: {current_pair}")
        return 0

    if not args.force_run and retry_backoff_active(state, current_pair, args.retry_minutes, now):
        state["last_poll"]["decision"] = "retry_backoff"
        write_state(state_path, state)
        log(f"previous attempt failed recently; retry backoff active for {current_pair}")
        return 0

    command = build_pipeline_command(args, latest["ch1"], latest["ch2"])
    state["last_poll"]["decision"] = "run_pipeline"
    state["last_attempt"] = {
        "pair": current_pair,
        "runs": {"ch1": latest["ch1"], "ch2": latest["ch2"]},
        "status": "running",
        "started_at": now_label(),
        "command": command,
    }
    write_state(state_path, state)

    log(f"starting pipeline for {current_pair}")
    log("command: " + " ".join(shlex.quote(part) for part in command))
    if args.plan_only:
        state["last_attempt"]["status"] = "planned"
        state["last_attempt"]["finished_at"] = now_label()
        write_state(state_path, state)
        return 0

    completed = subprocess.run(command, cwd=REPO_ROOT, env=os.environ.copy(), check=False)
    finished_at = now_label()
    state = load_state(state_path)
    state["last_attempt"] = {
        "pair": current_pair,
        "runs": {"ch1": latest["ch1"], "ch2": latest["ch2"]},
        "status": "succeeded" if completed.returncode == 0 else "failed",
        "started_at": state.get("last_attempt", {}).get("started_at", now_label()),
        "finished_at": finished_at,
        "returncode": completed.returncode,
        "command": command,
    }
    if completed.returncode == 0:
        state["last_success"] = {
            "pair": current_pair,
            "runs": {"ch1": latest["ch1"], "ch2": latest["ch2"]},
            "completed_at": finished_at,
            "published": pipeline_deploys(args.run_mode, args.skip_deploy),
            "data_branch_push_allowed": not args.no_push_data_branch,
        }
    write_state(state_path, state)
    if completed.returncode == 0:
        log(f"pipeline succeeded for {current_pair}")
    else:
        log(f"pipeline failed for {current_pair}; returncode={completed.returncode}")
    return completed.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Poll MeteoSwiss and run the direct coding-server pipeline when ready.")
    parser.add_argument("--python-cmd", default=os.getenv("XCBENZ_PYTHON_CMD", "uv run python"))
    parser.add_argument("--run-mode", default=os.getenv("XCBENZ_RUN_MODE", "standard-deploy-data-host"))
    parser.add_argument("--state-file", default=os.getenv("XCBENZ_POLL_STATE_FILE", ".local_pipeline/poller_state.json"))
    parser.add_argument("--lock-file", default=os.getenv("XCBENZ_POLL_LOCK_FILE", "/run/lock/xcbenz-coding-server-poller.lock"))
    parser.add_argument("--probe-timeout", type=int, default=parse_int_env("XCBENZ_POLL_PROBE_TIMEOUT", 12))
    parser.add_argument("--retry-minutes", type=int, default=parse_int_env("XCBENZ_POLL_RETRY_MINUTES", 10))
    parser.add_argument("--force-run", action="store_true", default=env_bool("XCBENZ_POLL_FORCE_RUN", False))
    parser.add_argument("--plan-only", action="store_true", help="Probe and print the pipeline command without executing it.")
    parser.add_argument("--skip-deploy", action="store_true", default=env_bool("XCBENZ_POLL_SKIP_DEPLOY", False))
    parser.add_argument(
        "--no-push-data-branch",
        action="store_true",
        default=env_bool("XCBENZ_POLL_NO_PUSH_DATA_BRANCH", False),
    )
    parser.add_argument(
        "--no-restore-web-exports",
        action="store_true",
        default=env_bool("XCBENZ_POLL_NO_RESTORE_WEB_EXPORTS", True),
        help="Do not restore previous web_exports before generating fresh direct outputs.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    with local_lock(args.lock_file):
        return run_poller(args)


if __name__ == "__main__":
    sys.exit(main())
