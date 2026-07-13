"""
generate_combined_manifest.py

Called by the direct pipeline after fetch chunks complete.
Scans direct profile chunks and browser-ready split-binary map caches, then
writes manifest.json for web export generation.
"""
import os
import json
import datetime

PROFILE_CHUNK_DIR = "web_profile_chunks"
CACHE_DIR_WIND_MAPS = "cache_wind_maps"
CACHE_DIR_SUNSHINE_MAPS = "cache_sunshine_maps"
CACHE_DIR_RAIN_MAPS = "cache_rain_maps"
CACHE_DIR_SUNRAIN_MAPS = "cache_sunrain_maps"
CACHE_DIR_CLOUD_MAPS = "cache_cloud_maps"


def log(msg):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} [INFO] {msg}", flush=True)


def _step_number(step_label):
    return int(str(step_label).replace("H", ""))


def scan_profile_chunks(model_key, root=PROFILE_CHUNK_DIR):
    """Return {run_tag: {location_id: [step_labels]}} from direct profile chunks."""
    model_root = os.path.join(root, model_key)
    runs = {}
    if not os.path.isdir(model_root):
        return runs

    for run in sorted(os.listdir(model_root), reverse=True):
        run_path = os.path.join(model_root, run)
        if not os.path.isdir(run_path):
            continue
        locations = {}
        for chunk in sorted(os.listdir(run_path)):
            chunk_path = os.path.join(run_path, chunk)
            if not os.path.isdir(chunk_path):
                continue
            for location_id in sorted(os.listdir(chunk_path)):
                metadata_path = os.path.join(chunk_path, location_id, "chunk.json")
                if not os.path.exists(metadata_path):
                    continue
                try:
                    with open(metadata_path, "r", encoding="utf-8") as f:
                        metadata = json.load(f)
                    steps = [str(step.get("step")) for step in metadata.get("steps") or [] if step.get("step")]
                except Exception as exc:
                    log(f"Skipping invalid profile chunk {metadata_path}: {exc}")
                    continue
                if steps:
                    locations.setdefault(location_id, set()).update(steps)
        if locations:
            runs[run] = {
                location_id: sorted(steps, key=_step_number)
                for location_id, steps in sorted(locations.items())
            }
    return runs

def scan_direct_wind_maps(cache_dir=CACHE_DIR_WIND_MAPS):
    """
    Scan browser-ready wind-map files.

    Layout:
      cache_wind_maps/{model}/{run}/{level}/metadata.json
      cache_wind_maps/{model}/{run}/{level}/steps/{step}.bin
    """
    wind_maps = {}
    if not os.path.exists(cache_dir):
        return wind_maps

    for model in ("ch1", "ch2"):
        model_dir = os.path.join(cache_dir, model)
        if not os.path.isdir(model_dir):
            continue
        model_runs = {}
        for run in sorted(os.listdir(model_dir), reverse=True):
            run_path = os.path.join(model_dir, run)
            if not os.path.isdir(run_path):
                continue
            levels = {}
            for level_dir_name in sorted(os.listdir(run_path)):
                metadata_path = os.path.join(run_path, level_dir_name, "metadata.json")
                if not os.path.exists(metadata_path):
                    continue
                rel_metadata = os.path.relpath(metadata_path, ".").replace(os.sep, "/")
                try:
                    with open(metadata_path, "r", encoding="utf-8") as f:
                        metadata = json.load(f)
                    steps = metadata.get("steps") or []
                    if not steps:
                        continue
                    if not all(os.path.exists((step.get("path") or "").replace("/", os.sep)) for step in steps):
                        continue
                    level = metadata.get("level") or {}
                    grid = metadata.get("grid") or {}
                    level_name = str(level.get("name") or level_dir_name)
                    levels[level_name] = {
                        "metadata": rel_metadata,
                        "components": (metadata.get("encoding") or {}).get("components", []),
                        "level_type": str(level.get("type") or ""),
                        "level_h": float(level.get("height_m") or 0.0),
                        "grid": {
                            "width": grid.get("width"),
                            "height": grid.get("height"),
                            "source_stride": grid.get("source_stride"),
                        },
                        "steps": steps,
                        "step_count": len(steps),
                        "bytes": sum(int(step.get("byte_length") or 0) for step in steps),
                    }
                except Exception as exc:
                    log(f"Skipping invalid direct wind-map metadata {rel_metadata}: {exc}")
            if levels:
                model_runs[run] = {
                    "layout": "browser_ready_split_binary_by_step",
                    "levels": levels,
                }
        if model_runs:
            wind_maps[model] = model_runs

    return wind_maps


def scan_sunshine_maps(cache_dir=CACHE_DIR_SUNSHINE_MAPS):
    """
    Scan browser-ready sunshine-map files.

    Layout:
      cache_sunshine_maps/{model}/{run}/surface/metadata.json
      cache_sunshine_maps/{model}/{run}/surface/steps/{step}.bin
    """
    sunshine_maps = {}
    if not os.path.exists(cache_dir):
        return sunshine_maps

    for model in ("ch1", "ch2"):
        model_dir = os.path.join(cache_dir, model)
        if not os.path.isdir(model_dir):
            continue
        model_runs = {}
        for run in sorted(os.listdir(model_dir), reverse=True):
            metadata_path = os.path.join(model_dir, run, "surface", "metadata.json")
            if not os.path.exists(metadata_path):
                continue
            rel_metadata = os.path.relpath(metadata_path, ".").replace(os.sep, "/")
            try:
                with open(metadata_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
                steps = metadata.get("steps") or []
                if not steps:
                    continue
                if not all(os.path.exists((step.get("path") or "").replace("/", os.sep)) for step in steps):
                    continue
                model_runs[run] = {
                    "layout": "browser_ready_split_binary_by_step",
                    "products": {
                        "surface": {
                            "metadata": rel_metadata,
                            "components": metadata.get("encoding", {}).get("components", []),
                            "steps": steps,
                            "step_count": len(steps),
                            "bytes": sum(int(step.get("byte_length") or 0) for step in steps),
                        }
                    },
                }
            except Exception as exc:
                log(f"Skipping invalid sunshine-map metadata {rel_metadata}: {exc}")
        if model_runs:
            sunshine_maps[model] = model_runs

    return sunshine_maps


def scan_rain_maps(cache_dir=CACHE_DIR_RAIN_MAPS):
    """
    Scan browser-ready rain-map files.

    Layout:
      cache_rain_maps/{model}/{run}/surface/metadata.json
      cache_rain_maps/{model}/{run}/surface/steps/{step}.bin
    """
    rain_maps = {}
    if not os.path.exists(cache_dir):
        return rain_maps

    for model in ("ch1", "ch2"):
        model_dir = os.path.join(cache_dir, model)
        if not os.path.isdir(model_dir):
            continue
        model_runs = {}
        for run in sorted(os.listdir(model_dir), reverse=True):
            metadata_path = os.path.join(model_dir, run, "surface", "metadata.json")
            if not os.path.exists(metadata_path):
                continue
            rel_metadata = os.path.relpath(metadata_path, ".").replace(os.sep, "/")
            try:
                with open(metadata_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
                steps = metadata.get("steps") or []
                if not steps:
                    continue
                if not all(os.path.exists((step.get("path") or "").replace("/", os.sep)) for step in steps):
                    continue
                model_runs[run] = {
                    "layout": "browser_ready_split_binary_by_step",
                    "products": {
                        "surface": {
                            "metadata": rel_metadata,
                            "components": metadata.get("encoding", {}).get("components", []),
                            "steps": steps,
                            "step_count": len(steps),
                            "bytes": sum(int(step.get("byte_length") or 0) for step in steps),
                        }
                    },
                }
            except Exception as exc:
                log(f"Skipping invalid rain-map metadata {rel_metadata}: {exc}")
        if model_runs:
            rain_maps[model] = model_runs

    return rain_maps


def scan_sunrain_maps(cache_dir=CACHE_DIR_SUNRAIN_MAPS):
    """
    Scan browser-ready Sun+Rain map files.

    Layout:
      cache_sunrain_maps/{model}/{run}/surface/metadata.json
      cache_sunrain_maps/{model}/{run}/surface/steps/{step}.bin
    """
    sunrain_maps = {}
    if not os.path.exists(cache_dir):
        return sunrain_maps

    for model in ("ch1", "ch2"):
        model_dir = os.path.join(cache_dir, model)
        if not os.path.isdir(model_dir):
            continue
        model_runs = {}
        for run in sorted(os.listdir(model_dir), reverse=True):
            metadata_path = os.path.join(model_dir, run, "surface", "metadata.json")
            if not os.path.exists(metadata_path):
                continue
            rel_metadata = os.path.relpath(metadata_path, ".").replace(os.sep, "/")
            try:
                with open(metadata_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
                steps = metadata.get("steps") or []
                if not steps:
                    continue
                if not all(os.path.exists((step.get("path") or "").replace("/", os.sep)) for step in steps):
                    continue
                model_runs[run] = {
                    "layout": "browser_ready_split_binary_by_step",
                    "products": {
                        "surface": {
                            "metadata": rel_metadata,
                            "components": metadata.get("encoding", {}).get("components", []),
                            "steps": steps,
                            "step_count": len(steps),
                            "bytes": sum(int(step.get("byte_length") or 0) for step in steps),
                        }
                    },
                }
            except Exception as exc:
                log(f"Skipping invalid Sun+Rain map metadata {rel_metadata}: {exc}")
        if model_runs:
            sunrain_maps[model] = model_runs

    return sunrain_maps


def scan_cloud_maps(cache_dir=CACHE_DIR_CLOUD_MAPS):
    """
    Scan browser-ready cloud-map files.

    Layout:
      cache_cloud_maps/{model}/{run}/{layer}/metadata.json
      cache_cloud_maps/{model}/{run}/{layer}/steps/{step}.bin
    """
    cloud_maps = {}
    if not os.path.exists(cache_dir):
        return cloud_maps

    for model in ("ch1", "ch2"):
        model_dir = os.path.join(cache_dir, model)
        if not os.path.isdir(model_dir):
            continue
        model_runs = {}
        for run in sorted(os.listdir(model_dir), reverse=True):
            run_path = os.path.join(model_dir, run)
            if not os.path.isdir(run_path):
                continue
            products = {}
            for product in ("total", "low", "mid", "high"):
                metadata_path = os.path.join(run_path, product, "metadata.json")
                if not os.path.exists(metadata_path):
                    continue
                rel_metadata = os.path.relpath(metadata_path, ".").replace(os.sep, "/")
                try:
                    with open(metadata_path, "r", encoding="utf-8") as f:
                        metadata = json.load(f)
                    steps = metadata.get("steps") or []
                    if not steps:
                        continue
                    if not all(os.path.exists((step.get("path") or "").replace("/", os.sep)) for step in steps):
                        continue
                    products[product] = {
                        "metadata": rel_metadata,
                        "components": metadata.get("encoding", {}).get("components", []),
                        "steps": steps,
                        "step_count": len(steps),
                        "bytes": sum(int(step.get("byte_length") or 0) for step in steps),
                    }
                except Exception as exc:
                    log(f"Skipping invalid cloud-map metadata {rel_metadata}: {exc}")
            if products:
                model_runs[run] = {
                    "layout": "browser_ready_split_binary_by_step",
                    "products": products,
                }
        if model_runs:
            cloud_maps[model] = model_runs

    return cloud_maps


def main():
    runs_ch1 = scan_profile_chunks("icon-ch1")
    runs_ch2 = scan_profile_chunks("icon-ch2")
    wind_maps = scan_direct_wind_maps()
    sunshine_maps = scan_sunshine_maps()
    rain_maps = scan_rain_maps()
    sunrain_maps = scan_sunrain_maps()
    cloud_maps = scan_cloud_maps()

    generated_at = max(runs_ch1.keys()) if runs_ch1 else (
        max(runs_ch2.keys()) if runs_ch2 else ""
    )

    manifest = {
        "generated_at": generated_at,
        "schema_version": 3,
        "runs": runs_ch1,
        "runs_ch2": runs_ch2,
        "wind_maps": wind_maps,
        "sunshine_maps": sunshine_maps,
        "rain_maps": rain_maps,
        "sunrain_maps": sunrain_maps,
        "cloud_maps": cloud_maps,
    }

    with open("manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f)

    log(
        f"Manifest written: {len(runs_ch1)} CH1 direct profile run(s), "
        f"{len(runs_ch2)} CH2 direct profile run(s), "
        f"{sum(len(runs) for runs in wind_maps.values())} wind-map run(s), "
        f"{sum(len(runs) for runs in sunshine_maps.values())} sunshine-map run(s), "
        f"{sum(len(runs) for runs in rain_maps.values())} rain-map run(s), "
        f"{sum(len(runs) for runs in sunrain_maps.values())} Sun+Rain map run(s), "
        f"{sum(len(runs) for runs in cloud_maps.values())} cloud-map run(s)"
    )


if __name__ == "__main__":
    main()
