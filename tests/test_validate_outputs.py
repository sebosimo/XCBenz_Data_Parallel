import unittest

from scripts.validate_outputs import map_run_set_mismatches


def manifest(ch1: list[str], ch2: list[str]) -> dict:
    return {
        "models": {
            "icon-ch1": {"runs": {run: {} for run in ch1}},
            "icon-ch2": {"runs": {run: {} for run in ch2}},
        }
    }


class ValidateOutputRunSetTests(unittest.TestCase):
    def test_matching_product_run_sets_are_valid(self):
        root = manifest(["20260807_1500", "20260807_1200"], ["20260807_1200"])
        self.assertEqual(
            map_run_set_mismatches(root, {"wind": root, "sunshine": root}),
            [],
        )

    def test_missing_product_history_is_reported(self):
        root = manifest(["20260807_1500", "20260807_1200"], ["20260807_1200"])
        wind = manifest(["20260807_1500"], ["20260807_1200"])
        self.assertEqual(
            map_run_set_mismatches(root, {"wind": wind}),
            [
                "wind/icon-ch1: runs=['20260807_1500'] "
                "expected=['20260807_1500', '20260807_1200']"
            ],
        )


if __name__ == "__main__":
    unittest.main()
