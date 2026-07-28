import unittest

from pipeline_orchestration.forecast_completeness import (
    expected_step_labels,
    missing_profile_horizons,
    parse_horizon_hours,
    profile_run_errors,
    stac_variable_horizons,
)
from scripts import validate_remote_web_exports as remote


def feature(variable, horizon, *, reference="2026-07-28T12:00:00Z"):
    return {
        "properties": {
            "forecast:reference_datetime": reference,
            "forecast:variable": variable,
            "forecast:perturbed": False,
            "forecast:horizon": horizon,
        }
    }


class SourceCompletenessTests(unittest.TestCase):
    def test_iso_duration_forms_are_parsed(self):
        self.assertEqual(parse_horizon_hours("PT12H"), 12)
        self.assertEqual(parse_horizon_hours("P0DT12H"), 12)
        self.assertEqual(parse_horizon_hours("P5D"), 120)
        self.assertIsNone(parse_horizon_hours("PT30M"))

    def test_post_pagination_merges_cursor_with_original_filters(self):
        calls = []

        def post_json(url, payload, timeout):
            calls.append((url, payload, timeout))
            if len(calls) == 1:
                return {
                    "features": [feature("T", "P0DT0H")],
                    "links": [
                        {
                            "rel": "next",
                            "href": "https://example.test/search",
                            "method": "POST",
                            "merge": True,
                            "body": {"cursor": "page-2"},
                        }
                    ],
                }
            return {"features": [feature("T", "P0DT1H")], "links": []}

        observed = stac_variable_horizons(
            collection_id="collection",
            reference_datetime="2026-07-28T12:00:00Z",
            variable="T",
            post_json=post_json,
            timeout=7,
        )

        self.assertEqual(observed, {0, 1})
        self.assertEqual(calls[1][1]["cursor"], "page-2")
        self.assertEqual(calls[1][1]["forecast:variable"], "T")
        self.assertIs(calls[1][1]["forecast:perturbed"], False)

    def test_present_terminal_horizon_does_not_hide_intermediate_gap(self):
        def post_json(_url, payload, _timeout):
            variable = payload["forecast:variable"]
            horizons = [0, 1, 3]
            return {
                "features": [feature(variable, f"P0DT{horizon}H") for horizon in horizons],
                "links": [],
            }

        missing = missing_profile_horizons(
            collection_id="collection",
            reference_datetime="2026-07-28T12:00:00Z",
            expected_count=4,
            variables=("T", "U"),
            post_json=post_json,
            timeout=7,
        )

        self.assertEqual(missing, {"T": (2,), "U": (2,)})

    def test_source_extras_do_not_make_a_complete_run_incomplete(self):
        def post_json(_url, payload, _timeout):
            variable = payload["forecast:variable"]
            return {
                "features": [feature(variable, f"P0DT{horizon}H") for horizon in range(5)],
                "links": [],
            }

        missing = missing_profile_horizons(
            collection_id="collection",
            reference_datetime="2026-07-28T12:00:00Z",
            expected_count=4,
            variables=("T",),
            post_json=post_json,
            timeout=7,
        )

        self.assertEqual(missing, {})


class PublishedCompletenessTests(unittest.TestCase):
    def test_expected_steps_cover_ch1_03z_and_ch2(self):
        self.assertEqual(len(expected_step_labels("icon-ch1", "20260728_0300")), 46)
        self.assertEqual(expected_step_labels("icon-ch1", "20260728_1200")[-1], "H33")
        self.assertEqual(expected_step_labels("icon-ch2", "20260728_1200")[-1], "H120")

    def test_every_location_must_have_the_exact_step_sequence(self):
        complete = list(expected_step_labels("icon-ch2", "20260728_1200"))
        partial = [step for step in complete if step != "H070"]
        errors = profile_run_errors(
            "icon-ch2",
            "20260728_1200",
            {
                "locations": {
                    "Bern": {"steps": complete},
                    "Zurich": {"steps": partial},
                }
            },
        )

        self.assertEqual(len(errors), 1)
        self.assertIn("Zurich: 120/121 steps", errors[0])
        self.assertIn("missing H070", errors[0])

    def test_remote_model_validation_checks_both_latest_runs(self):
        ch1_steps = list(expected_step_labels("icon-ch1", "20260728_1200"))
        ch2_steps = list(expected_step_labels("icon-ch2", "20260728_1200"))
        manifest = {
            "models": {
                "icon-ch1": {
                    "latest_run": "20260728_1200",
                    "runs": {
                        "20260728_1200": {
                            "locations": {
                                "Bern": {
                                    "steps": ch1_steps,
                                    "emagram_bundle": "ch1/bundle.json",
                                }
                            }
                        }
                    },
                },
                "icon-ch2": {
                    "latest_run": "20260728_1200",
                    "runs": {
                        "20260728_1200": {
                            "locations": {
                                "Bern": {
                                    "steps": ch2_steps,
                                    "emagram_bundle": "ch2/bundle.json",
                                }
                            }
                        }
                    },
                },
            }
        }

        self.assertEqual(
            remote.validate_models(manifest),
            [
                ("icon-ch1", "20260728_1200", "Bern"),
                ("icon-ch2", "20260728_1200", "Bern"),
            ],
        )
        manifest["models"]["icon-ch2"]["runs"]["20260728_1200"]["locations"]["Bern"]["steps"].pop(70)
        with self.assertRaisesRegex(remote.ValidationError, "missing H070"):
            remote.validate_models(manifest)


if __name__ == "__main__":
    unittest.main()
