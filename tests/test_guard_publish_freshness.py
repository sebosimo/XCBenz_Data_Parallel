import unittest

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


if __name__ == "__main__":
    unittest.main()
