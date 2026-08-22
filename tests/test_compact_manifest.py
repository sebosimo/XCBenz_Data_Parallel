import json
import unittest
from unittest import mock

from compact_manifest import build_compact_manifest, expand_compact_manifest
from scripts import validate_remote_web_exports as remote


def sample_manifest():
    steps = ["H00", "H01"]
    valid_times = ["2026-08-22T00:00:00Z", "2026-08-22T01:00:00Z"]
    return {
        "schema_version": 1,
        "generated_at": "2026-08-22T00:00:00Z",
        "source": {"data_root": "https://example.test/data"},
        "urls": {"locations": "web_exports/locations.json", "regions": None},
        "products": {
            "region_forecasts": "web_exports/region_forecasts/{model}/{run}/{location_id}.json",
            "emagrams": None,
            "emagram_bundles": "web_exports/emagrams/{model}/{run}/{location_id}/bundle.json",
            "thermal_panels": "web_exports/thermal_panels/{model}/{run}/{location_id}.json",
            "maps": {"wind": "web_exports/wind_maps/manifest.json"},
        },
        "models": {
            "icon-ch1": {
                "label": "ICON-CH1",
                "profile_source": "direct_chunks",
                "latest_run": "20260822_0000",
                "runs": {
                    "20260822_0000": {
                        "locations": {
                            "bern": {
                                "type": "region",
                                "display_name": "Bern",
                                "steps": steps,
                                "valid_times": valid_times,
                                "region_forecast": "web_exports/region_forecasts/icon-ch1/20260822_0000/bern.json",
                                "thermal_panel": None,
                                "emagram_template": None,
                                "emagram_bundle": "web_exports/emagrams/icon-ch1/20260822_0000/bern/bundle.json",
                            },
                            "custom": {
                                "type": "legacy",
                                "display_name": "Custom",
                                "steps": steps,
                                "valid_times": valid_times,
                                "region_forecast": "custom/forecast.json",
                                "thermal_panel": "web_exports/thermal_panels/icon-ch1/20260822_0000/custom.json",
                                "emagram_template": "custom/{step}.json",
                                "emagram_bundle": None,
                                "future_field": "preserved",
                            },
                        }
                    }
                },
                "counts": {"runs": 1, "locations": 2},
            }
        },
        "counts": {"locations": 2},
        "notes": ["Preserve this note."],
        "capabilities": {"spatial_value_tiles": {"contract": "spatial-value-tiles"}},
    }


class CompactManifestTests(unittest.TestCase):
    def test_round_trip_preserves_the_root_manifest(self):
        manifest = sample_manifest()

        compact = build_compact_manifest(manifest)

        self.assertEqual(expand_compact_manifest(compact), manifest)
        self.assertEqual(compact["schema_version"], 2)
        self.assertEqual(compact["product"], "forecast_root_manifest_compact")

    def test_reuses_location_and_schedule_tables(self):
        compact = build_compact_manifest(sample_manifest())

        self.assertEqual(len(compact["locations"]), 2)
        self.assertEqual(len(compact["schedules"]), 1)
        rows = compact["models"]["icon-ch1"]["runs"]["20260822_0000"]["locations"]
        self.assertEqual([row[1] for row in rows], [0, 0])

        expanded_locations = (
            expand_compact_manifest(compact)["models"]["icon-ch1"]["runs"]["20260822_0000"]["locations"]
        )
        self.assertIsNot(expanded_locations["bern"]["steps"], expanded_locations["custom"]["steps"])

    def test_compact_json_is_smaller_for_repeated_locations(self):
        manifest = sample_manifest()
        locations = manifest["models"]["icon-ch1"]["runs"]["20260822_0000"]["locations"]
        template = dict(locations["bern"])
        for index in range(100):
            location_id = f"location-{index}"
            item = dict(template)
            item["display_name"] = f"Location {index}"
            item["region_forecast"] = f"web_exports/region_forecasts/icon-ch1/20260822_0000/{location_id}.json"
            item["emagram_bundle"] = f"web_exports/emagrams/icon-ch1/20260822_0000/{location_id}/bundle.json"
            locations[location_id] = item

        current_bytes = len(json.dumps(manifest, separators=(",", ":")))
        compact_bytes = len(json.dumps(build_compact_manifest(manifest), separators=(",", ":")))

        self.assertLess(compact_bytes, current_bytes / 3)

    def test_remote_validation_requires_the_compact_and_legacy_views_to_match(self):
        manifest = sample_manifest()
        compact = build_compact_manifest(manifest)
        response = (compact, "https://data.test/web_exports/manifest.compact.json", {})

        with mock.patch.object(remote, "fetch_json", return_value=response):
            remote.validate_compact_manifest(manifest)

        changed = dict(manifest)
        changed["generated_at"] = "2026-08-22T01:00:00Z"
        with mock.patch.object(remote, "fetch_json", return_value=response):
            with self.assertRaisesRegex(remote.ValidationError, "does not expand"):
                remote.validate_compact_manifest(changed)


if __name__ == "__main__":
    unittest.main()
