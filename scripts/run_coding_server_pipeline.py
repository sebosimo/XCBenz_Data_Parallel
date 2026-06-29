#!/usr/bin/env python3
"""Prototype local forecast runner for the Hetzner coding server.

This mirrors the GitHub Actions workflow shape without requiring GitHub-hosted
runners. It starts the same fetch job families locally, adds CH1 map chunks, and
uses a lightweight /proc resource monitor to avoid launching new heavy jobs when
the coding server is already saturated.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPOSITORY = "sebosimo/XCBenz_Data_Parallel"
DEFAULT_DATA_BRANCH = "data-web"
DEFAULT_DATA_HOST_BASE_URL = "https://data.xcbenz.com"
STATIC_ASSETS = (
    (
        "ch1",
        "https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-forecasting-icon-ch1/assets",
        ("vertical_constants_icon-ch1-eps.grib2", "horizontal_constants_icon-ch1-eps.grib2"),
    ),
    (
        "ch2",
        "https://data.geo.admin.ch/api/stac/v1/collections/ch.meteoschweiz.ogd-forecasting-icon-ch2/assets",
        ("vertical_constants_icon-ch2-eps.grib2", "horizontal_constants_icon-ch2-eps.grib2"),
    ),
)
RUN_MODE_CHOICES = (
    "force-refresh",
    "standard",
    "deploy-data-host",
    "standard-deploy-data-host",
)
TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Job:
    name: str
    command: list[str]
    env: dict[str, str]


@dataclass
class ResourceSnapshot:
    cpu_percent: float | None
    load1: float | None
    mem_available_mb: float | None
    mem_total_mb: float | None
    active_jobs: int


def now_label() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message: str) -> None:
    print(f"{now_label()} [local-pipeline] {message}", flush=True)


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


def parse_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be a number, got {raw!r}") from exc


def python_command(raw: str) -> list[str]:
    parts = shlex.split(raw)
    if not parts:
        raise SystemExit("Python command is empty")
    return parts


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


def is_deploy_mode(run_mode: str) -> bool:
    return run_mode in {"deploy-data-host", "standard-deploy-data-host"}


def is_force_refresh(run_mode: str) -> bool:
    return run_mode in {"force-refresh", "deploy-data-host"}


def raw_data_branch_url(repository: str, data_branch: str) -> str:
    return f"https://raw.githubusercontent.com/{repository}/{data_branch}"


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
            log(f"another local forecast pipeline is already running; lock={lock_file}")
            raise SystemExit(0)
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield


class ResourceMonitor:
    def __init__(
        self,
        *,
        sample_seconds: float,
        max_cpu_percent: float,
        max_load_percent: float,
        min_available_mb: float,
    ) -> None:
        self.sample_seconds = sample_seconds
        self.max_cpu_percent = max_cpu_percent
        self.max_load_percent = max_load_percent
        self.min_available_mb = min_available_mb
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_cpu_times: tuple[int, int] | None = None
        self._snapshot = ResourceSnapshot(None, None, None, None, 0)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="resource-monitor", daemon=True)
        self._thread.start()
        self.sample_once(log_sample=True)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def set_active_jobs(self, count: int) -> None:
        with self._lock:
            self._snapshot.active_jobs = count

    def snapshot(self) -> ResourceSnapshot:
        with self._lock:
            return ResourceSnapshot(**self._snapshot.__dict__)

    def can_start_job(self) -> tuple[bool, list[str]]:
        snap = self.snapshot()
        reasons: list[str] = []
        if snap.mem_available_mb is not None and snap.mem_available_mb < self.min_available_mb:
            reasons.append(
                f"available RAM {snap.mem_available_mb:.0f} MB < floor {self.min_available_mb:.0f} MB"
            )
        if snap.cpu_percent is not None and snap.cpu_percent > self.max_cpu_percent:
            reasons.append(f"CPU {snap.cpu_percent:.1f}% > limit {self.max_cpu_percent:.1f}%")
        if snap.load1 is not None:
            cpus = max(1, os.cpu_count() or 1)
            load_percent = 100.0 * snap.load1 / cpus
            if load_percent > self.max_load_percent:
                reasons.append(f"load {load_percent:.1f}% of CPU count > limit {self.max_load_percent:.1f}%")
        return not reasons, reasons

    def _loop(self) -> None:
        while not self._stop.wait(self.sample_seconds):
            self.sample_once(log_sample=True)

    def sample_once(self, *, log_sample: bool) -> None:
        cpu_percent = self._read_cpu_percent()
        load1 = self._read_load1()
        mem_available_mb, mem_total_mb = self._read_memory_mb()
        with self._lock:
            active_jobs = self._snapshot.active_jobs
            self._snapshot = ResourceSnapshot(
                cpu_percent=cpu_percent,
                load1=load1,
                mem_available_mb=mem_available_mb,
                mem_total_mb=mem_total_mb,
                active_jobs=active_jobs,
            )
        if log_sample:
            parts = [f"active_jobs={active_jobs}"]
            if cpu_percent is not None:
                parts.append(f"cpu={cpu_percent:.1f}%")
            if load1 is not None:
                parts.append(f"load1={load1:.2f}")
            if mem_available_mb is not None and mem_total_mb is not None:
                parts.append(f"ram_available={mem_available_mb:.0f}/{mem_total_mb:.0f} MB")
            log("resource " + " ".join(parts))

    def _read_cpu_percent(self) -> float | None:
        stat_path = Path("/proc/stat")
        if not stat_path.exists():
            return None
        try:
            first = stat_path.read_text(encoding="utf-8").splitlines()[0]
            parts = [int(value) for value in first.split()[1:]]
        except Exception:
            return None
        if len(parts) < 5:
            return None
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        previous = self._last_cpu_times
        self._last_cpu_times = (idle, total)
        if previous is None:
            return None
        prev_idle, prev_total = previous
        total_delta = total - prev_total
        idle_delta = idle - prev_idle
        if total_delta <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (1.0 - idle_delta / total_delta)))

    @staticmethod
    def _read_load1() -> float | None:
        try:
            return float(os.getloadavg()[0])
        except (AttributeError, OSError):
            return None

    @staticmethod
    def _read_memory_mb() -> tuple[float | None, float | None]:
        meminfo = Path("/proc/meminfo")
        if not meminfo.exists():
            return None, None
        values: dict[str, float] = {}
        try:
            for line in meminfo.read_text(encoding="utf-8").splitlines():
                key, rest = line.split(":", 1)
                values[key] = float(rest.strip().split()[0]) / 1024.0
        except Exception:
            return None, None
        return values.get("MemAvailable"), values.get("MemTotal")


def run_checked(
    label: str,
    command: list[str],
    *,
    env: dict[str, str],
    log_dir: Path,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    log_path = log_dir / f"{safe_name(label)}.log"
    log(f"start {label}; log={log_path}")
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.write_text(completed.stdout or "", encoding="utf-8")
    if completed.returncode != 0 and not allow_failure:
        raise SystemExit(f"{label} failed with exit {completed.returncode}; see {log_path}")
    if completed.returncode != 0:
        log(f"{label} failed with exit {completed.returncode}; continuing by request")
    else:
        log(f"done {label}")
    return completed


def safe_rmtree(path: Path) -> None:
    resolved = path.resolve()
    repo = REPO_ROOT.resolve()
    if resolved == repo or repo not in resolved.parents:
        raise RuntimeError(f"refusing to remove path outside repository: {resolved}")
    if path.exists():
        shutil.rmtree(path)


def parse_output_file(path: Path) -> dict[str, str]:
    outputs: dict[str, str] = {}
    if not path.exists():
        return outputs
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        outputs[key.strip()] = value.strip()
    return outputs


def run_preflight(args: argparse.Namespace, env: dict[str, str], log_dir: Path, py: list[str]) -> dict[str, str]:
    if args.ch1_run_tag and args.ch2_run_tag:
        return {
            "should_run": "true",
            "should_run_ch1": "true",
            "should_run_ch2": "true",
            "reason": "manual_run_tags",
            "latest_ch1": args.ch1_run_tag,
            "latest_ch2": args.ch2_run_tag,
        }

    output_file = log_dir / "preflight_outputs.txt"
    preflight_env = dict(env)
    preflight_env.update(
        {
            "FORCE_REFRESH": "true" if is_force_refresh(args.run_mode) else "false",
            "GITHUB_OUTPUT": str(output_file),
            "GITHUB_REPOSITORY": args.repository,
            "DATA_BRANCH": args.data_branch,
        }
    )
    run_checked(
        "preflight",
        [*py, "scripts/preflight_runs.py"],
        env=preflight_env,
        log_dir=log_dir,
    )
    return parse_output_file(output_file)


def chunk_id(start: int, end: int) -> str:
    return f"H{start:03d}_H{end:03d}"


def run_hour(run_tag: str) -> int:
    try:
        return int(run_tag.split("_", 1)[1][:2])
    except Exception as exc:
        raise SystemExit(f"Invalid run tag {run_tag!r}; expected YYYYMMDD_HHMM") from exc


def horizon_chunks(end: int, chunk_size: int) -> list[tuple[int, int]]:
    size = max(1, chunk_size)
    chunks = []
    start = 0
    while start <= end:
        chunk_end = min(end, start + size - 1)
        chunks.append((start, chunk_end))
        start = chunk_end + 1
    if len(chunks) > 1:
        last_start, last_end = chunks[-1]
        if last_end - last_start + 1 < max(2, size // 2):
            prev_start, _prev_end = chunks[-2]
            chunks[-2:] = [(prev_start, last_end)]
    return chunks


def ch1_chunks(run_tag: str, chunk_size: int) -> list[tuple[int, int]]:
    end = 45 if run_hour(run_tag) == 3 else 33
    if chunk_size <= 0:
        if end == 45:
            return [(0, 16), (17, 33), (34, 45)]
        return [(0, 16), (17, 33)]
    return horizon_chunks(end, chunk_size)


def ch2_chunks(chunk_size: int) -> list[tuple[int, int]]:
    if chunk_size <= 0:
        return [(0, 30), (31, 60), (61, 90), (91, 120)]
    return horizon_chunks(120, chunk_size)


def order_combined_jobs(ch1_jobs: list[Job], ch2_jobs: list[Job], order: str) -> list[Job]:
    if order == "interleave":
        return [*ch1_jobs[:2], *ch2_jobs[:2], *ch1_jobs[2:], *ch2_jobs[2:]]
    return [*ch1_jobs, *ch2_jobs]


def map_output_roots(model: str, cid: str) -> dict[str, str]:
    root = Path("map_chunks") / model / cid
    prefix = "CH1" if model == "ch1" else "CH2"
    wind_cache = "cache_wind_maps"
    return {
        f"{prefix}_WIND_MAP_OUT_ROOT": str(root / wind_cache),
        f"{prefix}_SUNSHINE_MAP_OUT_ROOT": str(root / "cache_sunshine_maps"),
        f"{prefix}_RAIN_MAP_OUT_ROOT": str(root / "cache_rain_maps"),
        f"{prefix}_SUNRAIN_MAP_OUT_ROOT": str(root / "cache_sunrain_maps"),
        f"{prefix}_CLOUD_MAP_OUT_ROOT": str(root / "cache_cloud_maps"),
    }


def base_pipeline_env(args: argparse.Namespace, deploy: bool) -> dict[str, str]:
    env = os.environ.copy()
    web_data_root = args.web_export_data_root
    if not web_data_root:
        web_data_root = args.data_host_base_url if deploy else raw_data_branch_url(args.repository, args.data_branch)
    env.update(
        {
            "FORCE_REFRESH": "true" if is_force_refresh(args.run_mode) else "false",
            "DATA_BRANCH": args.data_branch,
            "DATA_HOST_BASE_URL": args.data_host_base_url,
            "WEB_EXPORT_DATA_ROOT": web_data_root,
            "DOWNLOAD_WORKERS": str(args.download_workers),
            "XCBENZ_FETCH_HORIZON_BATCH": "true",
            "XCBENZ_PREFETCH_NEXT_HORIZON": "true" if args.prefetch_next_horizon else "false",
            "XCBENZ_RELEASE_PROFILE_ONLY_FIELDS": "true" if args.release_profile_only_fields else "false",
            "ENABLE_WIND_MAPS": "true",
            "ENABLE_WIND_MAPS_CH1": "true",
            "ENABLE_WIND_MAPS_CH2": "true",
            "ENABLE_SUNSHINE_MAPS": "true",
            "ENABLE_RAIN_MAPS": "true",
            "ENABLE_RAIN_MAPS_CH1": "true",
            "ENABLE_RAIN_MAPS_CH2": "true",
            "ENABLE_SUNRAIN_MAPS": "true",
            "ENABLE_SUNRAIN_MAPS_CH1": "true",
            "ENABLE_SUNRAIN_MAPS_CH2": "true",
            "ENABLE_CLOUD_MAPS": "true",
            "ENABLE_CLOUD_MAPS_CH1": "true",
            "ENABLE_CLOUD_MAPS_CH2": "true",
            "WIND_MAP_MAX_SECONDS": "0",
        }
    )
    return env


def disable_maps(env: dict[str, str], model: str) -> None:
    env.update(
        {
            "ENABLE_WIND_MAPS": "false",
            f"ENABLE_WIND_MAPS_{model.upper()}": "false",
            "ENABLE_SUNSHINE_MAPS": "false",
            "ENABLE_RAIN_MAPS": "false",
            "ENABLE_SUNRAIN_MAPS": "false",
            "ENABLE_CLOUD_MAPS": "false",
            f"ENABLE_CLOUD_MAPS_{model.upper()}": "false",
        }
    )


def job_env(base: dict[str, str], run_dir: Path, name: str) -> dict[str, str]:
    env = dict(base)
    env["XCBENZ_FETCH_TMP_DIR"] = str(run_dir / "tmp" / safe_name(name))
    return env


def build_jobs(
    *,
    args: argparse.Namespace,
    base: dict[str, str],
    run_dir: Path,
    py: list[str],
    latest_ch1: str,
    latest_ch2: str,
) -> list[Job]:
    jobs: list[Job] = []

    if args.job_layout == "combined":
        ch1_jobs: list[Job] = []
        ch2_jobs: list[Job] = []
        for start, end in ch1_chunks(latest_ch1, args.ch1_chunk_size):
            cid = chunk_id(start, end)
            name = f"ch1-combined-{cid}"
            env = job_env(base, run_dir, name)
            env.update(
                {
                    "CH1_RUN_TAG": latest_ch1,
                    "CH1_PROFILE_MODE": "direct-chunk",
                    "CH1_PROFILE_CHUNK_ID": cid,
                    "CH1_HORIZON_START": str(start),
                    "CH1_HORIZON_END": str(end),
                    "CH1_REQUIRE_FULL_HORIZON_RUN": "true",
                    **map_output_roots("ch1", cid),
                }
            )
            ch1_jobs.append(Job(name, [*py, "fetch_data.py"], env))

        for start, end in ch2_chunks(args.ch2_chunk_size):
            cid = chunk_id(start, end)
            name = f"ch2-combined-{cid}"
            env = job_env(base, run_dir, name)
            env.update(
                {
                    "CH2_RUN_TAG": latest_ch2,
                    "CH2_PROFILE_MODE": "direct-chunk",
                    "CH2_PROFILE_CHUNK_ID": cid,
                    "CH2_HORIZON_START": str(start),
                    "CH2_HORIZON_END": str(end),
                    "CH2_REQUIRE_FULL_HORIZON_RUN": "true",
                    **map_output_roots("ch2", cid),
                }
            )
            ch2_jobs.append(Job(name, [*py, "fetch_data_ch2.py"], env))

        return order_combined_jobs(ch1_jobs, ch2_jobs, args.combined_job_order)

    for start, end in ch1_chunks(latest_ch1, args.ch1_chunk_size):
        cid = chunk_id(start, end)
        name = f"ch1-map-{cid}"
        env = job_env(base, run_dir, name)
        env.update(
            {
                "CH1_RUN_TAG": latest_ch1,
                "CH1_PROFILE_MODE": "none",
                "CH1_HORIZON_START": str(start),
                "CH1_HORIZON_END": str(end),
                "CH1_REQUIRE_FULL_HORIZON_RUN": "true",
                **map_output_roots("ch1", cid),
            }
        )
        jobs.append(Job(name, [*py, "fetch_data.py"], env))

    for start, end in [(0, 16), (17, 45)]:
        cid = chunk_id(start, end)
        name = f"ch1-profile-{cid}"
        env = job_env(base, run_dir, name)
        env.update(
            {
                "CH1_RUN_TAG": latest_ch1,
                "CH1_PROFILE_MODE": "direct-chunk",
                "CH1_PROFILE_CHUNK_ID": cid,
                "CH1_HORIZON_START": str(start),
                "CH1_HORIZON_END": str(end),
                "CH1_REQUIRE_FULL_HORIZON_RUN": "true",
            }
        )
        disable_maps(env, "CH1")
        jobs.append(Job(name, [*py, "fetch_data.py"], env))

    ch2_ranges = ch2_chunks(args.ch2_chunk_size)
    for start, end in ch2_ranges:
        cid = chunk_id(start, end)
        name = f"ch2-map-{cid}"
        env = job_env(base, run_dir, name)
        env.update(
            {
                "CH2_RUN_TAG": latest_ch2,
                "CH2_PROFILE_MODE": "none",
                "CH2_HORIZON_START": str(start),
                "CH2_HORIZON_END": str(end),
                "CH2_REQUIRE_FULL_HORIZON_RUN": "true",
                **map_output_roots("ch2", cid),
            }
        )
        jobs.append(Job(name, [*py, "fetch_data_ch2.py"], env))

    for start, end in ch2_ranges:
        cid = chunk_id(start, end)
        name = f"ch2-profile-{cid}"
        env = job_env(base, run_dir, name)
        env.update(
            {
                "CH2_RUN_TAG": latest_ch2,
                "CH2_PROFILE_MODE": "direct-chunk",
                "CH2_PROFILE_CHUNK_ID": cid,
                "CH2_HORIZON_START": str(start),
                "CH2_HORIZON_END": str(end),
                "CH2_REQUIRE_FULL_HORIZON_RUN": "true",
            }
        )
        disable_maps(env, "CH2")
        jobs.append(Job(name, [*py, "fetch_data_ch2.py"], env))

    return jobs


def auto_max_jobs(job_count: int) -> int:
    cpus = os.cpu_count() or 2
    return min(job_count, max(2, cpus - 1))


def run_parallel_jobs(
    jobs: list[Job],
    *,
    max_jobs: int,
    max_ch1_jobs: int = 0,
    monitor: ResourceMonitor,
    log_dir: Path,
) -> None:
    pending = list(jobs)
    running: dict[str, tuple[subprocess.Popen[Any], Any, Path]] = {}
    failures: list[tuple[str, int, Path]] = []
    last_wait_reason = ""

    while pending or running:
        for name, (proc, handle, job_log) in list(running.items()):
            rc = proc.poll()
            if rc is None:
                continue
            handle.close()
            running.pop(name)
            monitor.set_active_jobs(len(running))
            if rc == 0:
                log(f"done {name}; log={job_log}")
            else:
                log(f"failed {name} exit={rc}; log={job_log}")
                failures.append((name, rc, job_log))

        if failures:
            pending.clear()

        while pending and len(running) < max_jobs:
            ok, reasons = monitor.can_start_job()
            if not ok:
                reason = "; ".join(reasons)
                if reason != last_wait_reason:
                    log(f"waiting to start more jobs: {reason}")
                    last_wait_reason = reason
                break
            last_wait_reason = ""
            job_index = 0
            if max_ch1_jobs > 0:
                active_ch1 = sum(1 for name in running if name.startswith("ch1-"))
                if active_ch1 >= max_ch1_jobs:
                    job_index = next(
                        (idx for idx, candidate in enumerate(pending) if not candidate.name.startswith("ch1-")),
                        -1,
                    )
                    if job_index < 0:
                        reason = f"active CH1 jobs {active_ch1} >= cap {max_ch1_jobs}"
                        if reason != last_wait_reason:
                            log(f"waiting to start more jobs: {reason}")
                            last_wait_reason = reason
                        break
            job = pending.pop(job_index)
            job_log = log_dir / f"{safe_name(job.name)}.log"
            handle = job_log.open("w", encoding="utf-8")
            handle.write(f"{now_label()} command: {' '.join(job.command)}\n")
            handle.flush()
            log(f"start {job.name}; log={job_log}")
            proc = subprocess.Popen(
                job.command,
                cwd=REPO_ROOT,
                env=job.env,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
            )
            running[job.name] = (proc, handle, job_log)
            monitor.set_active_jobs(len(running))

        if pending or running:
            time.sleep(2)

    if failures:
        details = ", ".join(f"{name}=exit{rc}" for name, rc, _path in failures)
        raise SystemExit(f"fetch job failure(s): {details}")


def restore_web_exports(args: argparse.Namespace, env: dict[str, str], log_dir: Path) -> None:
    run_checked(
        "restore-data-branch-fetch",
        ["git", "fetch", "--depth=1", "origin", args.data_branch],
        env=env,
        log_dir=log_dir,
        allow_failure=True,
    )
    run_checked(
        "restore-web-exports",
        ["git", "checkout", "FETCH_HEAD", "--", "web_exports/"],
        env=env,
        log_dir=log_dir,
        allow_failure=True,
    )
    run_checked(
        "restore-web-exports-reset-index",
        ["git", "reset", "HEAD", "--", "web_exports/"],
        env=env,
        log_dir=log_dir,
        allow_failure=True,
    )


def load_asset_index(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": "XCBenz_Data_Parallel/local-runner"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8")).get("assets", {})


def find_asset_url(assets: Any, filename: str) -> str | None:
    if isinstance(assets, dict):
        payload = assets.get(filename) or {}
        href = payload.get("href") if isinstance(payload, dict) else None
        return str(href) if href else None
    if isinstance(assets, list):
        for item in assets:
            if isinstance(item, dict) and item.get("id") == filename:
                href = item.get("href")
                return str(href) if href else None
    return None


def download_asset(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "XCBenz_Data_Parallel/local-runner"})
    with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as handle:
        shutil.copyfileobj(response, handle, length=1024 * 1024)
    tmp.replace(target)


def prewarm_static_data(log_dir: Path) -> None:
    static_dir = REPO_ROOT / "static_data"
    static_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "prewarm-static-data.log"
    with log_path.open("w", encoding="utf-8") as handle:
        for model, assets_url, filenames in STATIC_ASSETS:
            handle.write(f"{now_label()} {model} assets {assets_url}\n")
            assets = load_asset_index(assets_url)
            for filename in filenames:
                target = static_dir / filename
                if target.exists() and target.stat().st_size > 0:
                    handle.write(f"{now_label()} exists {target}\n")
                    continue
                href = find_asset_url(assets, filename)
                if not href:
                    raise SystemExit(f"Could not find static asset {filename} in {assets_url}; see {log_path}")
                handle.write(f"{now_label()} downloading {href} -> {target}\n")
                handle.flush()
                download_asset(href, target)
                handle.write(f"{now_label()} wrote {target} bytes={target.stat().st_size}\n")
    log(f"static data prewarmed; log={log_path}")


def serial_publish_steps(args: argparse.Namespace, env: dict[str, str], log_dir: Path, py: list[str]) -> None:
    run_checked("merge-map-chunks", [*py, "scripts/merge_map_chunks.py"], env=env, log_dir=log_dir)
    run_checked("apply-retention", [*py, "scripts/apply_retention.py"], env=env, log_dir=log_dir)
    run_checked("generate-combined-manifest", [*py, "generate_combined_manifest.py"], env=env, log_dir=log_dir)

    generate_env = dict(env)
    generate_env.update(
        {
            "WIND_WEB_LEVELS": "10m_AGL,800m_AGL,1500m_AMSL,2000m_AMSL,3000m_AMSL,4000m_AMSL",
            "WEB_EXPORT_DIR": "web_exports_staging",
            "WEB_EXPORT_URL_PREFIX": "web_exports",
        }
    )
    run_checked("generate-web-exports", [*py, "generate_web_exports.py"], env=generate_env, log_dir=log_dir)

    retention_env = dict(env)
    retention_env.update(
        {
            "WEB_EXPORT_STAGING_DIR": "web_exports_staging",
            "WEB_EXPORT_DIR": "web_exports",
        }
    )
    run_checked("apply-web-retention", [*py, "scripts/apply_web_retention.py"], env=retention_env, log_dir=log_dir)

    validate_env = dict(env)
    validate_env["EXPECTED_WEB_EXPORT_DATA_ROOT"] = env["WEB_EXPORT_DATA_ROOT"]
    run_checked("validate-outputs", [*py, "scripts/validate_outputs.py"], env=validate_env, log_dir=log_dir)


def deploy_outputs(args: argparse.Namespace, env: dict[str, str], log_dir: Path, py: list[str]) -> None:
    deploy_env = dict(env)
    deploy_env["DATA_HOST_BASE_URL"] = args.data_host_base_url
    run_checked(
        "deploy-data-host",
        ["bash", "scripts/deploy_data_infomaniak.sh"],
        env=deploy_env,
        log_dir=log_dir,
    )
    if not args.skip_remote_validate:
        validate_env = dict(env)
        validate_env["DATA_BASE_URL"] = args.data_host_base_url
        run_checked(
            "validate-remote-web-exports",
            [*py, "scripts/validate_remote_web_exports.py"],
            env=validate_env,
            log_dir=log_dir,
        )


def push_data_branch_snapshot(args: argparse.Namespace, env: dict[str, str], log_dir: Path, run_dir: Path) -> None:
    source = REPO_ROOT / "web_exports"
    if not (source / "manifest.json").exists():
        raise SystemExit("Cannot push data branch: web_exports/manifest.json is missing")

    remote = subprocess.check_output(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=REPO_ROOT,
        text=True,
    ).strip()
    if not remote:
        raise SystemExit("Cannot push data branch: remote.origin.url is not configured")

    push_dir = run_dir / "data-branch-push"
    safe_rmtree(push_dir) if push_dir.exists() else None
    push_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, push_dir / "web_exports")

    push_env = dict(env)
    commands = [
        ("push-init", ["git", "init"]),
        ("push-remote", ["git", "remote", "add", "origin", remote]),
        ("push-branch", ["git", "checkout", "-b", "data-snapshot"]),
        ("push-user-name", ["git", "config", "user.name", "XCBenz Coding Server"]),
        ("push-user-email", ["git", "config", "user.email", "coding-server@xcbenz.local"]),
        ("push-add", ["git", "add", "-f", "web_exports/"]),
        (
            "push-commit",
            ["git", "commit", "-m", f"Web export snapshot: {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC"],
        ),
        ("push-data-branch", ["git", "push", "origin", f"HEAD:{args.data_branch}", "--force"]),
    ]
    for label, command in commands:
        log_path = log_dir / f"{label}.log"
        completed = subprocess.run(
            command,
            cwd=push_dir,
            env=push_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        log_path.write_text(completed.stdout or "", encoding="utf-8")
        if completed.returncode != 0:
            raise SystemExit(f"{label} failed with exit {completed.returncode}; see {log_path}")
    log(f"pushed web_exports snapshot to {args.data_branch}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the XCBenz forecast pipeline locally.")
    parser.add_argument("--run-mode", choices=RUN_MODE_CHOICES, default=os.getenv("XCBENZ_RUN_MODE", "standard-deploy-data-host"))
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", DEFAULT_REPOSITORY))
    parser.add_argument("--data-branch", default=os.getenv("DATA_BRANCH", DEFAULT_DATA_BRANCH))
    parser.add_argument("--data-host-base-url", default=os.getenv("DATA_HOST_BASE_URL", DEFAULT_DATA_HOST_BASE_URL))
    parser.add_argument("--web-export-data-root", default=os.getenv("WEB_EXPORT_DATA_ROOT"))
    parser.add_argument("--python-cmd", default=os.getenv("XCBENZ_PYTHON_CMD", "uv run python"))
    parser.add_argument("--download-workers", type=int, default=parse_int_env("DOWNLOAD_WORKERS", 6))
    parser.add_argument("--max-jobs", type=int, default=parse_int_env("XCBENZ_LOCAL_MAX_JOBS", 0))
    parser.add_argument(
        "--max-ch1-jobs",
        type=int,
        default=parse_int_env("XCBENZ_LOCAL_MAX_CH1_JOBS", 0),
        help="Experimental cap for concurrently active CH1 fetch jobs; 0 disables the cap.",
    )
    parser.add_argument(
        "--ch1-chunk-size",
        type=int,
        default=parse_int_env("XCBENZ_CH1_CHUNK_SIZE", 0),
        help="Experimental CH1 horizon chunk size; 0 keeps the legacy CH1 chunks.",
    )
    parser.add_argument(
        "--ch2-chunk-size",
        type=int,
        default=parse_int_env("XCBENZ_CH2_CHUNK_SIZE", 0),
        help="Experimental CH2 horizon chunk size; 0 keeps the legacy four CH2 chunks.",
    )
    parser.add_argument(
        "--job-layout",
        choices=("split", "combined"),
        default=os.getenv("XCBENZ_JOB_LAYOUT", "split"),
        help="Use split map/profile workers or combined workers that write both products per horizon chunk.",
    )
    parser.add_argument(
        "--combined-job-order",
        choices=("ch1-first", "interleave"),
        default=os.getenv("XCBENZ_COMBINED_JOB_ORDER", "ch1-first"),
        help="Experimental ordering for combined-layout fetch jobs.",
    )
    parser.add_argument(
        "--prefetch-next-horizon",
        action="store_true",
        default=env_bool("XCBENZ_PREFETCH_NEXT_HORIZON", False),
        help="Experimentally download the next horizon while decoding the current horizon.",
    )
    parser.add_argument(
        "--release-profile-only-fields",
        action="store_true",
        default=env_bool("XCBENZ_RELEASE_PROFILE_ONLY_FIELDS", False),
        help="Experimentally free direct-profile-only fields before map accumulation.",
    )
    parser.add_argument("--max-cpu-percent", type=float, default=parse_float_env("XCBENZ_LOCAL_MAX_CPU_PERCENT", 88.0))
    parser.add_argument("--max-load-percent", type=float, default=parse_float_env("XCBENZ_LOCAL_MAX_LOAD_PERCENT", 110.0))
    parser.add_argument("--min-available-mb", type=float, default=parse_float_env("XCBENZ_LOCAL_MIN_AVAILABLE_MB", 4096.0))
    parser.add_argument("--resource-sample-seconds", type=float, default=parse_float_env("XCBENZ_RESOURCE_SAMPLE_SECONDS", 15.0))
    parser.add_argument("--run-dir", default=os.getenv("XCBENZ_LOCAL_RUN_DIR"))
    parser.add_argument("--lock-file", default=os.getenv("XCBENZ_LOCAL_LOCK_FILE", "/run/lock/xcbenz-coding-server-forecast.lock"))
    parser.add_argument("--ch1-run-tag", default=os.getenv("CH1_RUN_TAG"))
    parser.add_argument("--ch2-run-tag", default=os.getenv("CH2_RUN_TAG"))
    parser.add_argument("--plan-only", action="store_true", help="Print the local job plan and exit before running jobs.")
    parser.add_argument("--skip-deploy", action="store_true", help="Generate and validate web_exports but do not publish them.")
    parser.add_argument("--skip-static-prewarm", action="store_true")
    parser.add_argument("--skip-remote-validate", action="store_true")
    parser.add_argument("--restore-web-exports", dest="restore_web_exports", action="store_true", default=env_bool("XCBENZ_RESTORE_WEB_EXPORTS", True))
    parser.add_argument("--no-restore-web-exports", dest="restore_web_exports", action="store_false")
    parser.add_argument("--push-data-branch", dest="push_data_branch", action="store_true", default=None)
    parser.add_argument("--no-push-data-branch", dest="push_data_branch", action="store_false")
    return parser


def run_pipeline(args: argparse.Namespace) -> int:
    py = python_command(args.python_cmd)
    deploy = is_deploy_mode(args.run_mode) and not args.skip_deploy
    if args.push_data_branch is None:
        args.push_data_branch = env_bool("XCBENZ_PUSH_DATA_BRANCH", deploy)

    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(args.run_dir) if args.run_dir else Path(".local_pipeline") / "runs" / timestamp
    if not run_dir.is_absolute():
        run_dir = REPO_ROOT / run_dir
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    base_env = base_pipeline_env(args, deploy)
    outputs = run_preflight(args, base_env, log_dir, py)
    should_run = outputs.get("should_run", "false").lower() == "true"
    latest_ch1 = outputs.get("latest_ch1") or args.ch1_run_tag
    latest_ch2 = outputs.get("latest_ch2") or args.ch2_run_tag
    log(f"preflight should_run={should_run} reason={outputs.get('reason', '')}")

    if not should_run and not args.plan_only:
        log("No forecast update needed.")
        return 0
    if not latest_ch1 or not latest_ch2:
        raise SystemExit(f"Preflight did not provide both run tags: ch1={latest_ch1!r} ch2={latest_ch2!r}")

    jobs = build_jobs(
        args=args,
        base=base_env,
        run_dir=run_dir,
        py=py,
        latest_ch1=latest_ch1,
        latest_ch2=latest_ch2,
    )
    max_jobs = args.max_jobs if args.max_jobs > 0 else auto_max_jobs(len(jobs))
    log(
        f"job plan: {len(jobs)} fetch jobs, max_jobs={max_jobs}, layout={args.job_layout}, "
        f"max_ch1_jobs={args.max_ch1_jobs}, "
        f"ch1_chunk_size={args.ch1_chunk_size}, ch2_chunk_size={args.ch2_chunk_size}, "
        f"prefetch_next_horizon={args.prefetch_next_horizon}, "
        f"release_profile_only_fields={args.release_profile_only_fields}, "
        f"latest_ch1={latest_ch1}, latest_ch2={latest_ch2}, deploy={deploy}, "
        f"push_data_branch={args.push_data_branch}"
    )
    for job in jobs:
        log(f"plan {job.name}")
    if args.plan_only:
        return 0

    if not args.skip_static_prewarm:
        prewarm_static_data(log_dir)

    for staging in ("map_chunks", "web_profile_chunks", "web_exports_staging"):
        safe_rmtree(REPO_ROOT / staging)

    monitor = ResourceMonitor(
        sample_seconds=args.resource_sample_seconds,
        max_cpu_percent=args.max_cpu_percent,
        max_load_percent=args.max_load_percent,
        min_available_mb=args.min_available_mb,
    )
    monitor.start()
    try:
        run_parallel_jobs(
            jobs,
            max_jobs=max_jobs,
            max_ch1_jobs=args.max_ch1_jobs,
            monitor=monitor,
            log_dir=log_dir,
        )
    finally:
        monitor.stop()

    if args.restore_web_exports:
        restore_web_exports(args, base_env, log_dir)
    serial_publish_steps(args, base_env, log_dir, py)
    if deploy:
        deploy_outputs(args, base_env, log_dir, py)
    if args.push_data_branch:
        push_data_branch_snapshot(args, base_env, log_dir, run_dir)

    log(f"Complete. Logs: {log_dir}")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    with local_lock(args.lock_file):
        return run_pipeline(args)


if __name__ == "__main__":
    raise SystemExit(main())
