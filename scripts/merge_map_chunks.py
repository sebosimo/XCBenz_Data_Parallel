"""Merge isolated map chunk artifacts into the normal publish cache layout."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wind_maps import wind_netcdf_encoding


CHUNK_ROOT = Path("map_chunks")
WIND_ROOT = Path("cache_wind_packed")
SUNSHINE_ROOT = Path("cache_sunshine_maps")
RAIN_ROOT = Path("cache_rain_maps")
SUNRAIN_ROOT = Path("cache_sunrain_maps")
NETCDF_ENGINE = "netcdf4"


def log(message: str) -> None:
    print(f"[map-merge] {message}", flush=True)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def wind_sources() -> dict[tuple[str, str, str], list[Path]]:
    groups: dict[tuple[str, str, str], list[Path]] = {}
    for path in CHUNK_ROOT.glob("**/cache_wind_packed/*/*/Wind_*.nc"):
        parts = path.parts
        try:
            idx = parts.index("cache_wind_packed")
        except ValueError:
            continue
        model, run_tag, filename = parts[idx + 1], parts[idx + 2], parts[idx + 3]
        groups.setdefault((model, run_tag, filename), []).append(path)
    return groups


def merge_wind_group(model: str, run_tag: str, filename: str, paths: list[Path]) -> None:
    datasets = []
    try:
        for path in sorted(paths):
            ds = xr.open_dataset(path, engine=NETCDF_ENGINE)
            datasets.append(ds.load())
            ds.close()
        if not datasets:
            return
        merged = xr.concat(datasets, dim="step")
        order = np.argsort(np.asarray(merged["horizon"].values, dtype=np.int32))
        merged = merged.isel(step=order)
        horizons = np.asarray(merged["horizon"].values, dtype=np.int32)
        _, unique_indices = np.unique(horizons, return_index=True)
        merged = merged.isel(step=np.sort(unique_indices))
        out_path = WIND_ROOT / model / run_tag / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
        merged.to_netcdf(
            tmp_path,
            engine=NETCDF_ENGINE,
            format="NETCDF4",
            encoding=wind_netcdf_encoding(merged),
        )
        tmp_path.replace(out_path)
        log(f"merged wind {model}/{run_tag}/{filename}: {merged.sizes.get('step', 0)} step(s)")
    finally:
        for ds in datasets:
            ds.close()


def merge_wind_chunks() -> None:
    for (model, run_tag, filename), paths in sorted(wind_sources().items()):
        merge_wind_group(model, run_tag, filename, paths)


def sunshine_sources() -> dict[tuple[str, str, str], list[Path]]:
    groups: dict[tuple[str, str, str], list[Path]] = {}
    for path in CHUNK_ROOT.glob("**/cache_sunshine_maps/*/*/*/metadata.json"):
        parts = path.parts
        try:
            idx = parts.index("cache_sunshine_maps")
        except ValueError:
            continue
        model, run_tag, product = parts[idx + 1], parts[idx + 2], parts[idx + 3]
        groups.setdefault((model, run_tag, product), []).append(path)
    return groups


def split_binary_sources(cache_name: str) -> dict[tuple[str, str, str], list[Path]]:
    groups: dict[tuple[str, str, str], list[Path]] = {}
    for path in CHUNK_ROOT.glob(f"**/{cache_name}/*/*/*/metadata.json"):
        parts = path.parts
        try:
            idx = parts.index(cache_name)
        except ValueError:
            continue
        model, run_tag, product = parts[idx + 1], parts[idx + 2], parts[idx + 3]
        groups.setdefault((model, run_tag, product), []).append(path)
    return groups


def merge_split_binary_group(
    product_label: str,
    output_root: Path,
    model: str,
    run_tag: str,
    product: str,
    paths: list[Path],
) -> None:
    merged_steps: dict[int, dict[str, Any]] = {}
    output_dir = output_root / model / run_tag / product
    steps_dir = output_dir / "steps"
    base_metadata: dict[str, Any] | None = None
    for metadata_path in sorted(paths):
        metadata = load_json(metadata_path)
        if base_metadata is None:
            base_metadata = dict(metadata)
        for step in metadata.get("steps") or []:
            horizon = int(step.get("horizon"))
            step_name = str(step.get("step") or f"H{horizon:03d}")
            source_path = Path(str(step.get("path") or ""))
            if not source_path.is_file():
                source_path = metadata_path.parent / "steps" / f"{step_name}.bin"
            if not source_path.is_file():
                raise FileNotFoundError(f"missing {product_label} chunk step: {source_path}")
            target_path = steps_dir / f"{step_name}.bin"
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            output_step = dict(step)
            output_step["path"] = target_path.as_posix()
            output_step["byte_length"] = int(target_path.stat().st_size)
            merged_steps[horizon] = output_step
    if base_metadata is None:
        return
    base_metadata["steps"] = [merged_steps[key] for key in sorted(merged_steps)]
    base_metadata["run"] = run_tag
    write_json(output_dir / "metadata.json", base_metadata)
    log(f"merged {product_label} {model}/{run_tag}/{product}: {len(merged_steps)} step(s)")


def merge_sunshine_group(model: str, run_tag: str, product: str, paths: list[Path]) -> None:
    merge_split_binary_group("sunshine", SUNSHINE_ROOT, model, run_tag, product, paths)


def merge_sunshine_chunks() -> None:
    for (model, run_tag, product), paths in sorted(split_binary_sources("cache_sunshine_maps").items()):
        merge_sunshine_group(model, run_tag, product, paths)


def merge_rain_chunks() -> None:
    for (model, run_tag, product), paths in sorted(split_binary_sources("cache_rain_maps").items()):
        merge_split_binary_group("rain", RAIN_ROOT, model, run_tag, product, paths)


def merge_sunrain_chunks() -> None:
    for (model, run_tag, product), paths in sorted(split_binary_sources("cache_sunrain_maps").items()):
        merge_split_binary_group("Sun+Rain", SUNRAIN_ROOT, model, run_tag, product, paths)


def main() -> int:
    if not CHUNK_ROOT.exists():
        log("no map chunk root found; nothing to merge")
        return 0
    merge_wind_chunks()
    merge_sunshine_chunks()
    merge_rain_chunks()
    merge_sunrain_chunks()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
