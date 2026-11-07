"""Reuse validated model exports without copying forecast data into a cache.

Two private receipts describe the last validated CH1/CH2 outputs. Reuse hashes
the retained files again, so missing, changed, or additional files cause a miss.
Receipts are written only after the ordinary combined-output validation passes.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import sys
import tempfile
from pathlib import Path

PRODUCT_ROOTS = (
    "region_forecasts", "emagrams", "wind_maps", "sunshine_maps", "rain_maps",
    "sunrain_maps", "cloud_maps", "thermal_panels", "value_tiles/v1",
)
REQUIRED_ROOTS = PRODUCT_ROOTS[:7]
MAX_RECEIPT_BYTES = 32 * 1024 * 1024
MAX_FILES = 100000
MODEL_FIELDS = {"ch1": "runs", "ch2": "runs_ch2"}


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def generation_identity(repo: Path, env: dict[str, str], settings: dict) -> str:
    """Bind receipts to source, dependencies, locations, static data, and policy.

    Persist only the aggregate digest. Environment values may be private.
    Container IDs, temporary paths, and scheduler state are not scientific inputs.
    """
    sources = set(repo.glob("*.py"))
    for directory in ("forecast_fetch", "pipeline_orchestration", "scripts"):
        sources.update((repo / directory).rglob("*.py"))
    for name in ("locations.json", "requirements.txt", "pyproject.toml", "uv.lock"):
        if (repo / name).is_file():
            sources.add(repo / name)
    for directory in repo.glob("static_data*"):
        if directory.is_dir():
            sources.update(path for path in directory.rglob("*.grib2") if path.is_file())
    prefixes = (
        "CH1_", "CH2_", "ENABLE_", "WIND_", "RAIN_", "SUNRAIN_", "SUNSHINE_",
        "CLOUD_", "VALUE_", "REQUIRE_", "WEB_", "EMAGRAM_", "GRIB_", "ECCODES_",
    )
    policy = {key: value for key, value in env.items() if key.startswith(prefixes)}
    for key in ("CH1_RUN_TAG", "CH2_RUN_TAG"):
        policy.pop(key, None)
    payload = {
        "version": 1,
        "files": {str(path.relative_to(repo)): file_digest(path) for path in sorted(sources)},
        "python": sys.version,
        "dependencies": sorted(
            (distribution.metadata.get("Name", ""), distribution.version)
            for distribution in importlib.metadata.distributions()
        ),
        "policy": policy,
        "settings": settings,
    }
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def _validate_model_run(model: str, run: str) -> None:
    if model not in MODEL_FIELDS or not re.fullmatch(r"\d{8}_\d{4}", run):
        raise ValueError("invalid model reuse identity")


def _inventory(root: Path, model: str, run: str) -> dict:
    _validate_model_run(model, run)
    inventory = {}
    for product in PRODUCT_ROOTS:
        directory = root / product / f"icon-{model}" / run
        if not directory.exists():
            if product in REQUIRED_ROOTS:
                raise ValueError(f"missing model product: {product}")
            continue
        if directory.is_symlink() or not directory.resolve().is_relative_to(root.resolve()):
            raise ValueError("model reuse directory escapes export root")
        files = 0
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError("model reuse does not accept symlinks")
            if not path.is_file():
                continue
            before = path.stat()
            digest = file_digest(path)
            after = path.stat()
            if any(getattr(before, key) != getattr(after, key) for key in (
                "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns",
            )):
                raise ValueError("model output changed during verification")
            inventory[path.relative_to(root).as_posix()] = [before.st_size, digest]
            files += 1
            if len(inventory) > MAX_FILES:
                raise ValueError("model reuse inventory exceeds its file budget")
        if not files and product in REQUIRED_ROOTS:
            raise ValueError(f"empty model product: {product}")
    return inventory


class ModelReuseStore:
    def __init__(self, state_root: Path, web_root: Path):
        self.state_root = state_root.resolve()
        self.web_root = web_root.resolve()
        if (
            self.state_root.is_relative_to(self.web_root)
            or self.web_root.is_relative_to(self.state_root)
        ):
            raise ValueError("model reuse receipts must be outside public exports")

    def load(self, model: str, run: str, identity: str) -> dict | None:
        _validate_model_run(model, run)
        path = self.state_root / f"{model}.json"
        try:
            if path.is_symlink() or path.stat().st_size > MAX_RECEIPT_BYTES:
                return None
            receipt = json.loads(path.read_bytes())
            if (
                receipt["version"] != 1 or receipt["identity"] != identity
                or receipt["model"] != model or receipt["run"] != run
                or not isinstance(receipt["profile_steps"], dict) or not receipt["profile_steps"]
            ):
                return None
            return receipt if receipt["files"] == _inventory(self.web_root, model, run) else None
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def record_validated(self, model: str, run: str, identity: str, source_manifest: dict) -> None:
        _validate_model_run(model, run)
        steps = source_manifest[MODEL_FIELDS[model]][run]
        if not isinstance(steps, dict) or not steps:
            raise ValueError("validated source manifest has no profile steps")
        payload = _json_bytes({
            "version": 1, "identity": identity, "model": model, "run": run,
            "profile_steps": steps, "files": _inventory(self.web_root, model, run),
        })
        if len(payload) > MAX_RECEIPT_BYTES:
            raise ValueError("model reuse receipt exceeds its byte budget")
        self.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = None
        try:
            descriptor, name = tempfile.mkstemp(dir=self.state_root, prefix=f".{model}.")
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_root / f"{model}.json")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def merge_reused_profiles(manifest_path: Path, receipts: dict[str, dict]) -> None:
    """Keep validated profile provenance; export only freshly computed maps.

    apply_web_retention merges the partial export with retained validated runs
    and rebuilds the browser manifests before the ordinary full validation.
    """
    if not receipts:
        return
    manifest = json.loads(manifest_path.read_bytes())
    for model, receipt in receipts.items():
        manifest[MODEL_FIELDS[model]] = {receipt["run"]: receipt["profile_steps"]}
        for product in ("wind_maps", "wind_directions", "sunshine_maps", "rain_maps", "sunrain_maps", "cloud_maps"):
            manifest.get(product, {}).pop(model, None)
    manifest_path.write_bytes(_json_bytes(manifest))
