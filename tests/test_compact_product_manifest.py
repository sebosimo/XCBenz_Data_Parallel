import gzip
import json
import unittest

from compact_product_manifest import (
    build_compact_product_manifest,
    expand_compact_product_manifest,
    project_product_manifest_for_startup,
)


class CompactProductManifestTests(unittest.TestCase):
    def _manifest(self, *, varying_lengths: bool = False, unusual_urls: bool = False):
        steps = [
            {
                "step": f"H{horizon:03d}",
                "horizon": horizon,
                "valid_time": f"2026-08-23T{horizon:02d}:00:00+00:00",
                "url": (
                    f"custom/frame-{horizon}.dat"
                    if unusual_urls
                    else f"web_exports/cloud_maps/icon-ch2/run/{{product}}/steps/H{horizon:03d}.bin"
                ),
                "byte_length": 1000 + horizon if varying_lengths else 1000,
                "missing_cell_count": 4,
                "mean_cloud_cover_pct": 55.5,
            }
            for horizon in range(12)
        ]
        return {
            "schema_version": 1,
            "product": "cloud_maps",
            "default_product": "total",
            "models": {
                "icon-ch2": {
                    "runs": {
                        "20260823_0000": {
                            "layout": "split_binary_by_step",
                            "products": {
                                product: {
                                    "metadata": f"web_exports/cloud_maps/icon-ch2/run/{product}/metadata.json",
                                    "source": "direct-grib",
                                    "components": ["cloud_cover"],
                                    "grid": {"width": 400, "height": 300},
                                    "steps": [
                                        {
                                            **step,
                                            "url": (
                                                step["url"]
                                                if unusual_urls
                                                else step["url"].format(product=product)
                                            ),
                                        }
                                        for step in steps
                                    ],
                                    "step_count": len(steps),
                                    "bytes": sum(step["byte_length"] for step in steps),
                                }
                                for product in ("low", "mid", "high", "total")
                            },
                        }
                    }
                }
            },
            "counts": {"runs": 1, "products": 4, "steps": 48, "bytes": 48000},
        }

    def test_reuses_schedule_urls_and_byte_length_without_transferring_step_statistics(self):
        manifest = self._manifest()

        compact = build_compact_product_manifest(manifest)
        expanded = expand_compact_product_manifest(compact)

        self.assertEqual(expanded, project_product_manifest_for_startup(manifest))
        self.assertEqual(compact["schema_version"], 2)
        self.assertEqual(len(compact["schedules"]), 1)
        self.assertIsNone(compact["entries"][0][6])
        self.assertEqual(compact["entries"][0][7], 1000)
        self.assertNotIn("missing_cell_count", expanded["models"]["icon-ch2"]["runs"]["20260823_0000"]["products"]["low"]["steps"][0])
        legacy_gzip = len(gzip.compress(json.dumps(manifest).encode()))
        compact_gzip = len(gzip.compress(json.dumps(compact, separators=(",", ":")).encode()))
        self.assertLess(compact_gzip, legacy_gzip * 0.6)

    def test_preserves_varying_byte_lengths(self):
        manifest = self._manifest(varying_lengths=True)
        compact = build_compact_product_manifest(manifest)

        self.assertIsInstance(compact["entries"][0][7], list)
        self.assertEqual(expand_compact_product_manifest(compact), project_product_manifest_for_startup(manifest))

    def test_falls_back_to_explicit_urls_when_they_cannot_be_templated(self):
        manifest = self._manifest(unusual_urls=True)
        compact = build_compact_product_manifest(manifest)

        self.assertEqual(compact["entries"][0][6][0], 1)
        self.assertEqual(expand_compact_product_manifest(compact), project_product_manifest_for_startup(manifest))

    def test_expands_previous_lossless_schema_for_rolling_deploy_compatibility(self):
        compact = {
            "schema_version": 1,
            "product": "forecast_map_manifest_compact",
            "legacy": [1, "rain_maps", "products", {"default_product": "surface"}, {}],
            "schedules": [[["H000", 0, "2026-08-23T00:00:00+00:00"]]],
            "entries": [[
                "icon-ch2",
                "20260823_0000",
                "split_binary_by_step",
                "surface",
                {"metadata": "surface/metadata.json"},
                0,
                [{"url": "surface/steps/H000.bin", "byte_length": 10, "max_value": 5}],
            ]],
        }

        expanded = expand_compact_product_manifest(compact)

        self.assertEqual(expanded["models"]["icon-ch2"]["runs"]["20260823_0000"]["products"]["surface"]["steps"][0]["max_value"], 5)


if __name__ == "__main__":
    unittest.main()
