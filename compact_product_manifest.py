"""Compact browser-startup representation for forecast map product manifests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


COMPACT_PRODUCT_MANIFEST_FILENAME = "manifest.compact.json"
COMPACT_PRODUCT_MANIFEST_PRODUCT = "forecast_map_manifest_compact"
STEP_KEYS = ("step", "horizon", "valid_time")
STARTUP_STEP_KEYS = (*STEP_KEYS, "url", "byte_length")


def _startup_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: step.get(key) for key in STARTUP_STEP_KEYS} for step in steps]


def project_product_manifest_for_startup(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the browser startup contract represented by a compact manifest.

    Per-frame statistics are intentionally excluded. They remain available in
    each product's metadata.json, which the browser fetches before frame data.
    """

    projected = {
        key: value
        for key, value in manifest.items()
        if key not in ("models", "url")
    }
    projected_models: dict[str, Any] = {}
    legacy_product = str(manifest.get("product") or "")
    group_key = "levels" if legacy_product == "wind_maps" else "products"
    for model_key, model in (manifest.get("models") or {}).items():
        projected_runs: dict[str, Any] = {}
        for run_tag, run in (model.get("runs") or {}).items():
            projected_items: dict[str, Any] = {}
            for item_name, item in (run.get(group_key) or {}).items():
                item_static = {"metadata": item.get("metadata")}
                projected_items[item_name] = {
                    **item_static,
                    "steps": _startup_steps(item.get("steps") or []),
                }
            projected_runs[run_tag] = {
                "layout": run.get("layout"),
                group_key: projected_items,
            }
        projected_models[model_key] = {"runs": projected_runs}
    projected["models"] = projected_models
    return projected


def _default_step_url(metadata_url: str, step_name: str) -> str:
    directory, separator, _filename = metadata_url.rpartition("/")
    if not separator:
        directory = "."
    return f"{directory}/steps/{step_name}.bin"


def _encode_step_urls(metadata_url: str, steps: list[dict[str, Any]]) -> list[Any] | None:
    urls = [str(step.get("url") or "") for step in steps]
    names = [str(step.get("step") or "") for step in steps]
    if any(not url for url in urls) or any(not name for name in names):
        raise ValueError("product manifest steps require non-empty step names and URLs")
    if all(url == _default_step_url(metadata_url, name) for url, name in zip(urls, names, strict=True)):
        return None

    templates: list[tuple[str, str]] = []
    for url, name in zip(urls, names, strict=True):
        marker = url.rfind(name)
        if marker < 0:
            break
        templates.append((url[:marker], url[marker + len(name):]))
    if len(templates) == len(urls) and len(set(templates)) == 1:
        prefix, suffix = templates[0]
        return [0, prefix, suffix]
    return [1, *urls]


def _decode_step_urls(
    encoding: Any,
    metadata_url: str,
    step_names: list[str],
) -> list[str]:
    if encoding is None:
        return [_default_step_url(metadata_url, name) for name in step_names]
    if not isinstance(encoding, list) or not encoding:
        raise ValueError("compact product manifest URL encoding is invalid")
    if encoding[0] == 0 and len(encoding) == 3:
        prefix, suffix = encoding[1:]
        if not isinstance(prefix, str) or not isinstance(suffix, str):
            raise ValueError("compact product manifest URL template is invalid")
        return [f"{prefix}{name}{suffix}" for name in step_names]
    if encoding[0] == 1 and len(encoding) == len(step_names) + 1:
        urls = encoding[1:]
        if not all(isinstance(url, str) and url for url in urls):
            raise ValueError("compact product manifest URL list is invalid")
        return urls
    raise ValueError("compact product manifest URL encoding is invalid")


def _encode_byte_lengths(steps: list[dict[str, Any]]) -> Any:
    lengths = [step.get("byte_length") for step in steps]
    if any(not isinstance(length, (int, float)) or isinstance(length, bool) for length in lengths):
        raise ValueError("product manifest steps require numeric byte lengths")
    return lengths[0] if lengths and len(set(lengths)) == 1 else lengths


def _decode_byte_lengths(encoded: Any, step_count: int) -> list[Any]:
    if isinstance(encoded, (int, float)) and not isinstance(encoded, bool):
        return [encoded] * step_count
    if isinstance(encoded, list) and len(encoded) == step_count:
        if not all(isinstance(length, (int, float)) and not isinstance(length, bool) for length in encoded):
            raise ValueError("compact product manifest byte lengths are invalid")
        return encoded
    raise ValueError("compact product manifest byte lengths differ from the schedule")


def build_compact_product_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    schedules: list[list[list[Any]]] = []
    schedule_indexes: dict[str, int] = {}
    entries: list[list[Any]] = []
    legacy_product = str(manifest.get("product") or "")
    group_key = "levels" if legacy_product == "wind_maps" else "products"

    for model_key, model in (manifest.get("models") or {}).items():
        for run_tag, run in (model.get("runs") or {}).items():
            for item_name, item in (run.get(group_key) or {}).items():
                steps = item.get("steps") or []
                schedule = [[step.get(key) for key in STEP_KEYS] for step in steps]
                schedule_key = json.dumps(schedule, separators=(",", ":"), ensure_ascii=False)
                schedule_index = schedule_indexes.get(schedule_key)
                if schedule_index is None:
                    schedule_index = len(schedules)
                    schedule_indexes[schedule_key] = schedule_index
                    schedules.append(schedule)
                # Product metadata contains the grid, encoding, source, style,
                # and complete step statistics. The startup index only needs
                # its URL before that file is loaded.
                item_static = {"metadata": item.get("metadata")}
                metadata_url = str(item_static.get("metadata") or "")
                if not metadata_url:
                    raise ValueError("product manifest entries require a metadata URL")
                entries.append(
                    [
                        model_key,
                        run_tag,
                        run.get("layout"),
                        item_name,
                        item_static,
                        schedule_index,
                        _encode_step_urls(metadata_url, steps),
                        _encode_byte_lengths(steps),
                    ]
                )

    defaults = {
        key: value
        for key, value in manifest.items()
        if key not in ("schema_version", "product", "models", "counts", "url")
    }
    return {
        "schema_version": 2,
        "product": COMPACT_PRODUCT_MANIFEST_PRODUCT,
        "legacy": [manifest.get("schema_version"), legacy_product, group_key, defaults, manifest.get("counts")],
        "schedules": schedules,
        "entries": entries,
    }


def _expand_legacy_entry(
    entry: list[Any],
    schedules: list[Any],
) -> tuple[Any, Any, Any, Any, dict[str, Any], list[dict[str, Any]]]:
    model_key, run_tag, layout, item_name, item_static, schedule_index, step_details = entry
    try:
        schedule = schedules[int(schedule_index)]
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("compact product manifest schedule reference is invalid") from exc
    if len(schedule) != len(step_details):
        raise ValueError("compact product manifest step columns differ in length")
    steps = []
    for base, details in zip(schedule, step_details, strict=True):
        if len(base) != len(STEP_KEYS):
            raise ValueError("compact product manifest schedule row is invalid")
        steps.append({**dict(zip(STEP_KEYS, base, strict=True)), **dict(details or {})})
    return model_key, run_tag, layout, item_name, dict(item_static or {}), steps


def _expand_startup_entry(
    entry: list[Any],
    schedules: list[Any],
) -> tuple[Any, Any, Any, Any, dict[str, Any], list[dict[str, Any]]]:
    model_key, run_tag, layout, item_name, item_static, schedule_index, url_encoding, encoded_lengths = entry
    try:
        schedule = schedules[int(schedule_index)]
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("compact product manifest schedule reference is invalid") from exc
    if any(not isinstance(base, list) or len(base) != len(STEP_KEYS) for base in schedule):
        raise ValueError("compact product manifest schedule row is invalid")
    static = dict(item_static or {})
    metadata_url = str(static.get("metadata") or "")
    if not metadata_url:
        raise ValueError("compact product manifest entry has no metadata URL")
    step_names = [str(base[0]) for base in schedule]
    urls = _decode_step_urls(url_encoding, metadata_url, step_names)
    lengths = _decode_byte_lengths(encoded_lengths, len(schedule))
    steps = [
        {
            **dict(zip(STEP_KEYS, base, strict=True)),
            "url": url,
            "byte_length": byte_length,
        }
        for base, url, byte_length in zip(schedule, urls, lengths, strict=True)
    ]
    return model_key, run_tag, layout, item_name, static, steps


def expand_compact_product_manifest(compact: dict[str, Any]) -> dict[str, Any]:
    if compact.get("product") != COMPACT_PRODUCT_MANIFEST_PRODUCT:
        raise ValueError("unsupported compact product manifest")
    compact_schema = compact.get("schema_version")
    if compact_schema not in (1, 2):
        raise ValueError("unsupported compact product manifest schema")
    legacy = compact.get("legacy") or []
    if len(legacy) != 5:
        raise ValueError("compact product manifest legacy header is invalid")
    schema_version, product, group_key, defaults, counts = legacy
    if group_key not in ("levels", "products"):
        raise ValueError("compact product manifest group is invalid")
    manifest: dict[str, Any] = {
        "schema_version": schema_version,
        "product": product,
        **dict(defaults or {}),
        "models": {},
        "counts": counts,
    }
    schedules = compact.get("schedules") or []
    for entry in compact.get("entries") or []:
        if not isinstance(entry, list):
            raise ValueError("compact product manifest entry is invalid")
        if compact_schema == 1 and len(entry) == 7:
            expanded = _expand_legacy_entry(entry, schedules)
        elif compact_schema == 2 and len(entry) == 8:
            expanded = _expand_startup_entry(entry, schedules)
        else:
            raise ValueError("compact product manifest entry is invalid")
        model_key, run_tag, layout, item_name, item_static, steps = expanded
        model = manifest["models"].setdefault(model_key, {"runs": {}})
        run = model["runs"].setdefault(run_tag, {"layout": layout, group_key: {}})
        if run.get("layout") != layout:
            raise ValueError("compact product manifest run layout is inconsistent")
        run[group_key][item_name] = {**item_static, "steps": steps}
    return manifest


def write_compact_product_manifest(path: Path, manifest: dict[str, Any]) -> None:
    compact = build_compact_product_manifest(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(compact, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        handle.write("\n")
