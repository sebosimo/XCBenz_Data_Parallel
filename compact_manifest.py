"""Compact, lossless representation of the browser root forecast manifest."""

from __future__ import annotations

from typing import Any


COMPACT_MANIFEST_SCHEMA_VERSION = 2
COMPACT_MANIFEST_PRODUCT = "forecast_root_manifest_compact"
COMPACT_MANIFEST_FILENAME = "manifest.compact.json"

THERMAL_PANEL_FLAG = 1
EMAGRAM_BUNDLE_FLAG = 2
EMAGRAM_TEMPLATE_FLAG = 4


def _format_template(template: str, *, model: str, run: str, location_id: str) -> str:
    return (
        template.replace("{model}", model)
        .replace("{run}", run)
        .replace("{location_id}", location_id)
    )


def _emagram_template(manifest: dict[str, Any]) -> str:
    products = manifest.get("products") or {}
    configured = products.get("emagrams")
    if isinstance(configured, str):
        return configured

    bundle = products.get("emagram_bundles")
    if isinstance(bundle, str) and bundle.endswith("/bundle.json"):
        return f"{bundle[:-len('/bundle.json')]}/{{step}}.json"
    return "web_exports/emagrams/{model}/{run}/{location_id}/{step}.json"


def _templates(manifest: dict[str, Any]) -> dict[str, str]:
    products = manifest.get("products") or {}
    templates = {
        "region_forecast": products.get("region_forecasts"),
        "thermal_panel": products.get("thermal_panels"),
        "emagram_bundle": products.get("emagram_bundles"),
        "emagram": _emagram_template(manifest),
    }
    invalid = [name for name, value in templates.items() if not isinstance(value, str)]
    if invalid:
        raise ValueError(f"root manifest lacks compact path template(s): {', '.join(invalid)}")
    return templates  # type: ignore[return-value]


def build_compact_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Normalize repeated schedules, location metadata, and forecast paths."""

    templates = _templates(manifest)
    locations: list[list[str]] = []
    location_indexes: dict[tuple[str, str, str], int] = {}
    schedules: list[list[list[str]]] = []
    schedule_indexes: dict[tuple[tuple[str, ...], tuple[str, ...]], int] = {}
    compact_models: dict[str, Any] = {}

    for model_key in sorted((manifest.get("models") or {}).keys()):
        model = manifest["models"][model_key]
        compact_runs: dict[str, Any] = {}
        for run_tag in sorted((model.get("runs") or {}).keys(), reverse=True):
            run = model["runs"][run_tag]
            rows: list[list[Any]] = []
            run_locations = run.get("locations") or {}
            for location_id in sorted(run_locations):
                location = run_locations[location_id]
                location_key = (
                    location_id,
                    str(location.get("type", "legacy")),
                    str(location.get("display_name", location_id)),
                )
                location_index = location_indexes.get(location_key)
                if location_index is None:
                    location_index = len(locations)
                    location_indexes[location_key] = location_index
                    locations.append(list(location_key))

                schedule_key = (
                    tuple(str(step) for step in location.get("steps") or []),
                    tuple(str(valid_time) for valid_time in location.get("valid_times") or []),
                )
                schedule_index = schedule_indexes.get(schedule_key)
                if schedule_index is None:
                    schedule_index = len(schedules)
                    schedule_indexes[schedule_key] = schedule_index
                    schedules.append([list(schedule_key[0]), list(schedule_key[1])])

                expected = {
                    name: _format_template(template, model=model_key, run=run_tag, location_id=location_id)
                    for name, template in templates.items()
                }
                flags = 0
                if location.get("thermal_panel") is not None:
                    flags |= THERMAL_PANEL_FLAG
                if location.get("emagram_bundle") is not None:
                    flags |= EMAGRAM_BUNDLE_FLAG
                if location.get("emagram_template") is not None:
                    flags |= EMAGRAM_TEMPLATE_FLAG

                known = {
                    "type",
                    "display_name",
                    "steps",
                    "valid_times",
                    "region_forecast",
                    "thermal_panel",
                    "emagram_template",
                    "emagram_bundle",
                }
                overrides = {key: value for key, value in location.items() if key not in known}
                for key in ("region_forecast", "thermal_panel", "emagram_template", "emagram_bundle"):
                    value = location.get(key)
                    expected_value = expected[
                        "emagram" if key == "emagram_template" else key
                    ] if value is not None else None
                    if value != expected_value:
                        overrides[key] = value

                row: list[Any] = [location_index, schedule_index, flags]
                if overrides:
                    row.append(overrides)
                rows.append(row)

            compact_run = {key: value for key, value in run.items() if key != "locations"}
            compact_run["locations"] = rows
            compact_runs[run_tag] = compact_run

        compact_model = {key: value for key, value in model.items() if key != "runs"}
        compact_model["runs"] = compact_runs
        compact_models[model_key] = compact_model

    compact = {
        key: value
        for key, value in manifest.items()
        if key not in {"schema_version", "models"}
    }
    compact.update(
        {
            "schema_version": COMPACT_MANIFEST_SCHEMA_VERSION,
            "product": COMPACT_MANIFEST_PRODUCT,
            "legacy_schema_version": manifest.get("schema_version", 1),
            "templates": templates,
            "locations": locations,
            "schedules": schedules,
            "models": compact_models,
        }
    )
    return compact


def expand_compact_manifest(compact: dict[str, Any]) -> dict[str, Any]:
    """Expand the compact contract to the existing browser Manifest shape."""

    if compact.get("schema_version") != COMPACT_MANIFEST_SCHEMA_VERSION:
        raise ValueError("unsupported compact manifest schema version")
    if compact.get("product") != COMPACT_MANIFEST_PRODUCT:
        raise ValueError("unsupported compact manifest product")

    templates = compact["templates"]
    locations = compact["locations"]
    schedules = compact["schedules"]
    models: dict[str, Any] = {}
    for model_key, compact_model in compact["models"].items():
        runs: dict[str, Any] = {}
        for run_tag, compact_run in compact_model["runs"].items():
            run_locations: dict[str, Any] = {}
            for row in compact_run["locations"]:
                location_id, location_type, display_name = locations[row[0]]
                steps, valid_times = schedules[row[1]]
                flags = row[2]
                values = {
                    "type": location_type,
                    "display_name": display_name,
                    "steps": list(steps),
                    "valid_times": list(valid_times),
                    "region_forecast": _format_template(
                        templates["region_forecast"],
                        model=model_key,
                        run=run_tag,
                        location_id=location_id,
                    ),
                    "thermal_panel": (
                        _format_template(
                            templates["thermal_panel"],
                            model=model_key,
                            run=run_tag,
                            location_id=location_id,
                        )
                        if flags & THERMAL_PANEL_FLAG
                        else None
                    ),
                    "emagram_template": (
                        _format_template(
                            templates["emagram"],
                            model=model_key,
                            run=run_tag,
                            location_id=location_id,
                        )
                        if flags & EMAGRAM_TEMPLATE_FLAG
                        else None
                    ),
                    "emagram_bundle": (
                        _format_template(
                            templates["emagram_bundle"],
                            model=model_key,
                            run=run_tag,
                            location_id=location_id,
                        )
                        if flags & EMAGRAM_BUNDLE_FLAG
                        else None
                    ),
                }
                if len(row) > 3:
                    values.update(row[3])
                run_locations[location_id] = values

            run = {key: value for key, value in compact_run.items() if key != "locations"}
            run["locations"] = run_locations
            runs[run_tag] = run

        model = {key: value for key, value in compact_model.items() if key != "runs"}
        model["runs"] = runs
        models[model_key] = model

    manifest = {
        key: value
        for key, value in compact.items()
        if key
        not in {
            "schema_version",
            "product",
            "legacy_schema_version",
            "templates",
            "locations",
            "schedules",
            "models",
        }
    }
    manifest["schema_version"] = compact["legacy_schema_version"]
    manifest["models"] = models
    return manifest
