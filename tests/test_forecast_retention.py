import unittest

from forecast_retention import kept_model_run_tags, kept_run_tags, parse_run_tag


class ForecastRetentionPolicyTests(unittest.TestCase):
    def test_ch1_keeps_latest_two_and_two_03z_anchors(self):
        runs = [
            "20260807_1500",
            "20260807_1200",
            "20260807_0300",
            "20260806_2100",
            "20260806_0300",
            "20260805_0300",
        ]
        self.assertEqual(
            kept_model_run_tags("icon-ch1", runs),
            {"20260807_1500", "20260807_1200", "20260807_0300", "20260806_0300"},
        )

    def test_ch2_midnight_overlap_yields_three_runs(self):
        runs = ["20260807_0600", "20260807_0000", "20260806_1800", "20260806_0000"]
        self.assertEqual(
            kept_model_run_tags("icon-ch2", runs),
            {"20260807_0600", "20260807_0000", "20260806_0000"},
        )

    def test_latest_run_date_controls_delayed_retention(self):
        runs = ["20260805_1200", "20260805_0900", "20260805_0300", "20260804_0300"]
        self.assertEqual(
            kept_model_run_tags("icon-ch1", runs),
            {"20260805_1200", "20260805_0900", "20260805_0300", "20260804_0300"},
        )

    def test_expired_anchor_is_removed_when_a_new_day_arrives(self):
        runs = [
            "20260808_0900",
            "20260808_0600",
            "20260808_0300",
            "20260807_0300",
            "20260806_0300",
        ]
        self.assertEqual(
            kept_model_run_tags("icon-ch1", runs),
            {"20260808_0900", "20260808_0600", "20260808_0300", "20260807_0300"},
        )

    def test_malformed_tags_are_ignored(self):
        self.assertIsNone(parse_run_tag("not-a-run"))
        self.assertEqual(
            kept_run_tags(["bad", "20260807_0300"], anchor_hour=3),
            {"20260807_0300"},
        )


if __name__ == "__main__":
    unittest.main()
