"""Run isolated cold and reused forecast validation with publication disabled.

Execute in the existing forecast image, holding the host's heavy-work lock.
Mount a disposable copy of this repository at /audit/code, production exports
read-only at /seed, and a disposable directory at /audit. No credentials are
required or accepted. The caller must enforce a container lifetime and disk
budget while holding the lock until the container is stopped.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/audit/code")
RESULTS = Path("/audit/results")


def command(argv, label, env):
    started = time.monotonic()
    path = RESULTS / f"{label}.log"
    print(json.dumps({"event": "phase_started", "phase": label}), flush=True)
    with path.open("w") as stream:
        result = subprocess.run(argv, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT, check=False)
    summary = {"phase": label, "seconds": time.monotonic() - started, "exit_code": result.returncode}
    print(json.dumps(summary), flush=True)
    if result.returncode:
        raise RuntimeError(f"canary phase failed: {label}; inspect {path}")
    return summary


def main():
    if Path("/run/secrets").exists() or any(key.startswith("INFOMANIAK_") for key in os.environ):
        raise SystemExit("canary must not receive publication credentials")
    if (ROOT / "web_exports").exists() or RESULTS.exists():
        raise SystemExit("canary output paths must be new")
    if shutil.disk_usage(ROOT).free < 40 * 1024**3:
        raise SystemExit("canary requires 40 GiB free before copying retained history")
    RESULTS.mkdir()
    source_manifest = json.loads(Path("/seed/manifest.json").read_bytes())
    runs = {model: source_manifest["models"][f"icon-{model}"]["latest_run"] for model in ("ch1", "ch2")}
    if not all(runs.values()):
        raise SystemExit("seed does not identify a complete model pair")
    shutil.copytree("/seed", ROOT / "web_exports")
    env = {
        **os.environ, "PYTHONDONTWRITEBYTECODE": "1", "ENABLE_VALUE_TILES": "true",
        "REQUIRE_WIND_DIRECTIONS": "true", "WEB_EXPORT_DATA_ROOT": "https://data.xcbenz.com",
        "XCBENZ_PUSH_DATA_BRANCH": "false", "XCBENZ_RESTORE_WEB_EXPORTS": "false",
        "XDG_CACHE_HOME": "/audit/cache", "TMPDIR": "/audit/tmp",
    }
    Path(env["TMPDIR"]).mkdir(exist_ok=True)
    common = [
        sys.executable, "scripts/run_coding_server_pipeline.py", "--python-cmd", sys.executable,
        "--run-mode", "standard", "--skip-deploy", "--no-push-data-branch",
        "--no-restore-web-exports", "--model-reuse", "--model-reuse-state-dir", "/audit/receipts",
        "--lock-file", "/audit/runner.lock", "--web-export-data-root", "https://data.xcbenz.com",
        "--ch1-run-tag", runs["ch1"], "--ch2-run-tag", runs["ch2"],
        "--job-layout", "combined", "--combined-job-order", "interleave",
        "--ch2-chunk-size", "15", "--prefetch-next-horizon", "--release-profile-only-fields",
        "--max-jobs", "4", "--download-workers", "8", "--resource-sample-seconds", "30",
    ]
    records = [command([*common, "--run-dir", "/audit/results/cold"], "cold", env)]
    receipts = {model: json.loads(Path(f"/audit/receipts/{model}.json").read_bytes()) for model in runs}
    if not all(receipts[model]["run"] == runs[model] for model in runs):
        raise RuntimeError("cold validation did not create the expected receipts")
    records.append(command([*common, "--run-dir", "/audit/results/reused"], "reused", env))
    for model in runs:
        after = json.loads(Path(f"/audit/receipts/{model}.json").read_bytes())
        if after["files"] != receipts[model]["files"]:
            raise RuntimeError(f"reused {model} output differs from cold output")
    warm_log = (RESULTS / "reused.log").read_text()
    if "model reuse: verified=ch1,ch2, fetch_jobs=0" not in warm_log:
        raise RuntimeError("reused canary did not skip both model fetch plans")
    record = {
        "runs": runs, "phases": records, "model_files_identical": True,
        "files_verified": {model: len(receipts[model]["files"]) for model in runs},
        "bytes_verified": {model: sum(value[0] for value in receipts[model]["files"].values()) for model in runs},
        "receipt_inventory_digests": {
            model: hashlib.sha256(json.dumps(receipts[model]["files"], sort_keys=True).encode()).hexdigest()
            for model in runs
        },
        "published": False,
    }
    (RESULTS / "summary.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record), flush=True)


if __name__ == "__main__":
    main()
