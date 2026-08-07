import unittest

from scripts.check_forecast_history import required_current_runs


class ForecastHistoryCheckTests(unittest.TestCase):
    def test_only_still_retained_public_runs_are_required(self):
        current = {
            "20260807_1200",
            "20260807_0900",
            "20260807_0300",
            "20260806_0300",
        }
        self.assertEqual(
            required_current_runs(
                current,
                "20260807_1500",
                model_key="icon-ch1",
            ),
            {"20260807_1200", "20260807_0300", "20260806_0300"},
        )

    def test_expired_public_anchor_does_not_trigger_hydration(self):
        current = {
            "20260807_1200",
            "20260807_0900",
            "20260807_0300",
            "20260806_0300",
        }
        self.assertEqual(
            required_current_runs(
                current,
                "20260808_0900",
                model_key="icon-ch1",
            ),
            {"20260807_1200", "20260807_0300"},
        )


if __name__ == "__main__":
    unittest.main()
