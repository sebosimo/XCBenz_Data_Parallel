"""Shared mechanics for browser-ready split-binary map publications.

Product encoders and completeness policy stay in their product modules.  This
module only owns the directory/metadata mechanics that are byte-for-byte the
same for the sunshine, rain, Sun+Rain, and cloud web exports.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


JsonObject = dict[str, Any]
Log = Callable[[str], None]
PathUrl = Callable[[Path], str]


@dataclass(frozen=True)
class SplitBinaryMapSpec:
    manifest_key: str
    default_product: str
    source_products: tuple[str, ...]
    log_name: str
    scan_name: str


SUNSHINE_MAPS = SplitBinaryMapSpec(
    "sunshine_maps", "surface", ("surface",), "sunshine", "sunshine-map"
)
RAIN_MAPS = SplitBinaryMapSpec("rain_maps", "surface", ("surface",), "rain", "rain-map")
SUNRAIN_MAPS = SplitBinaryMapSpec(
    "sunrain_maps", "surface", ("surface",), "Sun+Rain", "Sun+Rain map"
)
CLOUD_MAPS = SplitBinaryMapSpec(
    "cloud_maps",
    "total",
    ("total", "low", "mid", "high"),
    "cloud",
    "cloud-map",
)


def load_json(path: Path, *, missing_ok: bool = False) -> JsonObject:
    if missing_ok and not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: Any, *, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        if pretty:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        else:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def source_model_to_web(source_key: str) -> str:
    """Map the two direct-cache model keys to their public model keys."""
    return "icon-ch1" if source_key == "ch1" else "icon-ch2"


def relative_publication_path(publication_root: Path, path: Path) -> Path:
    """Return a path relative to the publication root, rejecting escapes."""
    try:
        return path.resolve().relative_to(publication_root.resolve())
    except ValueError as exc:
        raise ValueError(f"Path escapes publication root {publication_root}: {path}") from exc


def publication_url(publication_root: Path, path: Path, *, prefix: str = "web_exports") -> str:
    """Build a public URL for contained paths and preserve external source paths."""
    try:
        relative_path = relative_publication_path(publication_root, path)
    except ValueError:
        return path.as_posix()
    return (Path(prefix) / relative_path).as_posix() if prefix else relative_path.as_posix()


def resolve_publication_url(publication_root: Path, value: str | None) -> Path | None:
    """Resolve a manifest URL against its local publication root."""
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    if path.parts and path.parts[0] in {"web_exports", publication_root.name}:
        return publication_root.joinpath(*path.parts[1:])
    return path


def _source_step_exists(step: JsonObject) -> bool:
    return os.path.exists(str(step.get("path") or "").replace("/", os.sep))


def scan_split_binary_maps(
    cache_dir: str | os.PathLike[str],
    spec: SplitBinaryMapSpec,
    *,
    log: Log,
    models: Iterable[str] = ("ch1", "ch2"),
) -> JsonObject:
    """Scan complete browser-ready cache entries into a source manifest.

    A product is publishable only when metadata has at least one step and every
    referenced source binary exists.  Invalid products are logged and skipped;
    a run remains visible when at least one configured product is complete.
    """
    cache_root = Path(cache_dir)
    if not cache_root.exists():
        return {}

    result: JsonObject = {}
    for model in models:
        model_dir = cache_root / model
        if not model_dir.is_dir():
            continue
        model_runs: JsonObject = {}
        for run_dir in sorted((path for path in model_dir.iterdir() if path.is_dir()), reverse=True):
            products: JsonObject = {}
            for product_name in spec.source_products:
                metadata_path = run_dir / product_name / "metadata.json"
                if not metadata_path.exists():
                    continue
                relative_metadata = Path(os.path.relpath(metadata_path, ".")).as_posix()
                try:
                    metadata = load_json(metadata_path)
                    steps = metadata.get("steps") or []
                    if not steps or not all(_source_step_exists(step) for step in steps):
                        continue
                    products[product_name] = {
                        "metadata": relative_metadata,
                        "components": metadata.get("encoding", {}).get("components", []),
                        "steps": steps,
                        "step_count": len(steps),
                        "bytes": sum(int(step.get("byte_length") or 0) for step in steps),
                    }
                except Exception as exc:
                    log(f"Skipping invalid {spec.scan_name} metadata {relative_metadata}: {exc}")
            if products:
                model_runs[run_dir.name] = {
                    "layout": "browser_ready_split_binary_by_step",
                    "products": products,
                }
        if model_runs:
            result[model] = model_runs
    return result


def export_split_binary_product(
    *,
    model_key: str,
    run_tag: str,
    product_name: str,
    source_metadata_path: Path,
    output_root: Path,
    path_url: PathUrl,
    missing_label: str,
) -> JsonObject:
    """Copy one already-encoded map product into its public tree."""
    source_metadata = load_json(source_metadata_path)
    output_dir = output_root / model_key / run_tag / product_name
    steps_dir = output_dir / "steps"
    output_steps: list[JsonObject] = []

    for source_step in source_metadata.get("steps") or []:
        source_path = Path(str(source_step.get("path", "")))
        if not source_path.exists():
            raise FileNotFoundError(f"{missing_label} step missing: {source_path}")
        step_label = str(source_step["step"])
        step_path = steps_dir / f"{step_label}.bin"
        step_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, step_path)
        output_step = dict(source_step)
        output_step.pop("path", None)
        output_step["url"] = path_url(step_path)
        output_step["byte_length"] = int(step_path.stat().st_size)
        output_steps.append(output_step)

    metadata_path = output_dir / "metadata.json"
    metadata = dict(source_metadata)
    metadata["model"] = model_key
    metadata["run"] = run_tag
    metadata["source"] = path_url(source_metadata_path)
    metadata["steps"] = output_steps
    write_json(metadata_path, metadata, pretty=True)

    return {
        "metadata": path_url(metadata_path),
        "source": path_url(source_metadata_path),
        "components": metadata.get("encoding", {}).get("components", []),
        "grid": {
            "width": metadata.get("grid", {}).get("width"),
            "height": metadata.get("grid", {}).get("height"),
        },
        "steps": output_steps,
        "step_count": len(output_steps),
        "bytes": sum(step["byte_length"] for step in output_steps),
    }


def export_split_binary_maps(
    source_manifest: JsonObject,
    spec: SplitBinaryMapSpec,
    *,
    output_root: Path,
    path_url: PathUrl,
    log: Log,
    schema_version: int = 1,
) -> JsonObject | None:
    """Publish one declarative split-binary product family."""
    source_maps = source_manifest.get(spec.manifest_key) or {}
    if not source_maps:
        return None

    manifest: JsonObject = {
        "schema_version": schema_version,
        "product": spec.manifest_key,
        "default_product": spec.default_product,
        "models": {},
        "counts": {"runs": 0, "products": 0, "steps": 0, "bytes": 0},
    }
    for source_key, source_runs in source_maps.items():
        model_key = source_model_to_web(source_key)
        model_manifest: JsonObject = {"runs": {}}
        for run_tag, run_entry in source_runs.items():
            run_manifest: JsonObject = {"layout": "split_binary_by_step", "products": {}}
            for product_name, product_entry in (run_entry.get("products") or {}).items():
                source_metadata_path = Path(product_entry.get("metadata", ""))
                if not source_metadata_path.exists():
                    log(
                        f"WARN {spec.log_name} source metadata missing for "
                        f"{model_key} {run_tag}: {source_metadata_path}"
                    )
                    continue
                try:
                    output_product_name = (
                        product_name if len(spec.source_products) > 1 else spec.default_product
                    )
                    exported = export_split_binary_product(
                        model_key=model_key,
                        run_tag=run_tag,
                        product_name=output_product_name,
                        source_metadata_path=source_metadata_path,
                        output_root=output_root,
                        path_url=path_url,
                        missing_label=spec.log_name,
                    )
                except Exception as exc:
                    log(f"WARN {spec.log_name} export failed for {source_metadata_path}: {exc}")
                    continue
                run_manifest["products"][product_name] = exported
                manifest["counts"]["products"] += 1
                manifest["counts"]["steps"] += exported["step_count"]
                manifest["counts"]["bytes"] += exported["bytes"]
            if run_manifest["products"]:
                model_manifest["runs"][run_tag] = run_manifest
                manifest["counts"]["runs"] += 1
        if model_manifest["runs"]:
            manifest["models"][model_key] = model_manifest

    if not manifest["models"]:
        return None
    manifest_path = output_root / "manifest.json"
    write_json(manifest_path, manifest, pretty=True)
    manifest["url"] = path_url(manifest_path)
    return manifest


def rebuild_split_binary_manifest(
    root: Path,
    spec: SplitBinaryMapSpec,
    *,
    path_url: PathUrl,
) -> JsonObject | None:
    """Re-index retained public metadata without reapplying source completeness."""
    if not root.exists():
        return None
    manifest: JsonObject = {
        "schema_version": 1,
        "product": spec.manifest_key,
        "default_product": spec.default_product,
        "models": {},
        "counts": {"runs": 0, "products": 0, "steps": 0, "bytes": 0},
    }
    for model_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        model_manifest: JsonObject = {"runs": {}}
        for run_dir in sorted((path for path in model_dir.iterdir() if path.is_dir()), reverse=True):
            run_manifest: JsonObject = {"layout": "split_binary_by_step", "products": {}}
            for product_dir in sorted(path for path in run_dir.iterdir() if path.is_dir()):
                metadata_path = product_dir / "metadata.json"
                if not metadata_path.exists():
                    continue
                metadata = load_json(metadata_path)
                steps = metadata.get("steps") or []
                grid = metadata.get("grid") or {}
                byte_count = sum(int(step.get("byte_length") or 0) for step in steps)
                run_manifest["products"][product_dir.name] = {
                    "metadata": path_url(metadata_path),
                    "source": metadata.get("source"),
                    "components": (metadata.get("encoding") or {}).get("components", []),
                    "grid": {"width": grid.get("width"), "height": grid.get("height")},
                    "steps": steps,
                    "step_count": len(steps),
                    "bytes": byte_count,
                }
                manifest["counts"]["products"] += 1
                manifest["counts"]["steps"] += len(steps)
                manifest["counts"]["bytes"] += byte_count
            if run_manifest["products"]:
                model_manifest["runs"][run_dir.name] = run_manifest
                manifest["counts"]["runs"] += 1
        if model_manifest["runs"]:
            manifest["models"][model_dir.name] = model_manifest
    if not manifest["models"]:
        return None
    write_json(root / "manifest.json", manifest, pretty=True)
    return manifest
