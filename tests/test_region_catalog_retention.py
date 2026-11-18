import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import apply_web_retention as retention


class RegionCatalogRetentionTests(unittest.TestCase):
    def test_catalog_merge_keeps_current_names_and_omits_retired_points(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            web, staging = root / "web_exports", root / "staging"
            web.mkdir()
            staging.mkdir()
            catalog = {
                key: {"type": "region", "display_name": key}
                for key in ("bern", "draft_terrain_048")
            }
            catalog['draft_terrain_048']['display_name'] = 'Champagne rémoise - Reims'
            (staging / "locations.json").write_text(json.dumps(catalog))

            def forecast(tree, run, location_id):
                path = tree / "region_forecasts/icon-ch1" / run / f"{location_id}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({
                    "location": {"type": "region", "display_name": location_id},
                    "steps": ["H00"], "valid_times": ["2026-11-17T18:00:00Z"],
                    "products": {"emagram_bundle": f"web_exports/emagrams/icon-ch1/{run}/{location_id}/bundle.json"},
                }))
                return path

            for run in ("20261117_1800", "20261117_1500"):
                forecast(web, run, "bern")
                retired = forecast(web, run, "reims")
            forecast(staging, "20261117_1800", "bern")
            forecast(staging, "20261117_1800", "draft_terrain_048")
            with mock.patch.object(retention, "WEB_DIR", web), mock.patch.object(retention, "STAGING_DIR", staging):
                retention.merge_staging(include_value_tiles=False)
                retention.rebuild_main_manifest(5, None, None, None, None, None, None)
            manifest = json.loads((web / "manifest.json").read_bytes())
            runs = manifest["models"]["icon-ch1"]["runs"]
            self.assertEqual(set(runs["20261117_1800"]["locations"]), set(catalog))
            self.assertEqual(set(runs["20261117_1500"]["locations"]), {"bern"})
            for run in runs.values():
                for location_id, entry in run['locations'].items():
                    self.assertEqual(entry['display_name'], catalog[location_id]['display_name'])
            self.assertEqual(manifest["counts"]["emagram_bundles"], 3)
            self.assertEqual(manifest["counts"]["region_forecasts"], 3)
            self.assertTrue(retired.exists(), "Existing files stay subject to normal run retention")


if __name__ == "__main__":
    unittest.main()
