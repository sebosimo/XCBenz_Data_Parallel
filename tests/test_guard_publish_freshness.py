import unittest
from pathlib import Path
from unittest import mock

from scripts import guard_publish_freshness


def nested_manifest(ch1: list[str], ch2: list[str]) -> dict:
    return {
        "models": {
            "icon-ch1": {"runs": {tag: {} for tag in ch1}},
            "icon-ch2": {"runs": {tag: {} for tag in ch2}},
        }
    }


class PublishFreshnessGuardTests(unittest.TestCase):
    def test_equal_latest_runs_are_allowed(self):
        current = nested_manifest(["20260713_1500"], ["20260713_1200"])
        candidate = nested_manifest(["20260713_1500"], ["20260713_1200"])
        self.assertEqual(
            guard_publish_freshness.downgrade_reasons(candidate, current), []
        )

    def test_newer_candidate_is_allowed(self):
        current = nested_manifest(["20260713_1500"], ["20260713_1200"])
        candidate = nested_manifest(["20260713_1800"], ["20260713_1200"])
        self.assertEqual(
            guard_publish_freshness.downgrade_reasons(candidate, current), []
        )

    def test_older_candidate_is_rejected_per_model(self):
        current = nested_manifest(["20260713_1800"], ["20260713_1200"])
        candidate = nested_manifest(["20260713_1500"], ["20260713_1200"])
        self.assertEqual(
            guard_publish_freshness.downgrade_reasons(candidate, current),
            ["ch1:candidate=20260713_1500;current=20260713_1800"],
        )

    def test_missing_candidate_model_is_rejected(self):
        current = nested_manifest(["20260713_1800"], ["20260713_1200"])
        candidate = nested_manifest(["20260713_1800"], [])
        self.assertEqual(
            guard_publish_freshness.downgrade_reasons(candidate, current),
            ["ch2:candidate_missing;current=20260713_1200"],
        )

    def test_legacy_manifest_layout_is_supported(self):
        current = {
            "runs": {"20260713_1500": {}},
            "runs_ch2": {"20260713_1200": {}},
        }
        candidate = {
            "runs": {"20260713_1800": {}},
            "runs_ch2": {"20260713_1200": {}},
        }
        self.assertEqual(
            guard_publish_freshness.downgrade_reasons(candidate, current), []
        )

    def test_missing_previous_latest_run_is_rejected(self):
        current = nested_manifest(
            ["20260807_1200", "20260807_0900", "20260807_0300", "20260806_0300"],
            ["20260807_1200"],
        )
        candidate = nested_manifest(
            ["20260807_1500", "20260807_0300", "20260806_0300"],
            ["20260807_1200"],
        )
        self.assertEqual(
            guard_publish_freshness.retained_history_reasons(candidate, current),
            ["ch1:missing_retained=20260807_1200"],
        )

    def test_expired_anchor_can_be_removed(self):
        current = nested_manifest(
            ["20260807_1500", "20260807_1200", "20260807_0300", "20260806_0300"],
            ["20260807_1200"],
        )
        candidate = nested_manifest(
            ["20260808_0900", "20260808_0600", "20260808_0300", "20260807_0300"],
            ["20260807_1200"],
        )
        self.assertEqual(
            guard_publish_freshness.retained_history_reasons(candidate, current), []
        )

    def test_product_guard_requires_new_latest_and_retained_product_runs(self):
        candidate_root = Path("candidate")
        current_root = Path("current")
        manifests = {
            candidate_root / "manifest.json": nested_manifest(
                ["20260807_1500"], ["20260807_1200"]
            ),
            candidate_root / "wind_maps/manifest.json": nested_manifest(
                ["20260807_1500", "20260807_0300"], ["20260807_1200"]
            ),
            current_root / "wind_maps/manifest.json": nested_manifest(
                ["20260807_1200", "20260807_0300"], ["20260807_1200"]
            ),
        }
        with mock.patch.object(
            guard_publish_freshness,
            "load_json_object",
            side_effect=lambda path, missing_ok=False: manifests.get(path, {}),
        ):
            self.assertEqual(
                guard_publish_freshness.product_history_reasons(
                    candidate_root,
                    current_root,
                    required_products=("wind",),
                    require_value_tiles=False,
                ),
                ["wind/ch1:missing=20260807_1200"],
            )


if __name__ == "__main__":
    unittest.main()
