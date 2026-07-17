import datetime as dt
import unittest

from forecast_fetch.config import FetchConfigError, OutputRoots, parse_startup_config
from forecast_fetch.planning import CH1_POLICY, CH2_POLICY, ProductSelection, completion_operation_trace


DEFAULT_ROOTS = OutputRoots(
    wind="wind",
    sunshine="sunshine",
    rain="rain",
    sunrain="sunrain",
    cloud="cloud",
)


def _utc(hour):
    return dt.datetime(2026, 7, 17, hour, tzinfo=dt.timezone.utc)


class FetchPlanningTests(unittest.TestCase):
    def test_model_run_limits_cadence_and_static_assets_are_characterized(self):
        self.assertEqual(CH1_POLICY.maximum_horizon(_utc(3)), 45)
        self.assertEqual(CH1_POLICY.maximum_horizon(_utc(0)), 33)
        self.assertEqual(CH1_POLICY.maximum_horizon(_utc(6)), 33)
        self.assertEqual(
            (
                CH1_POLICY.run_interval_hours,
                CH1_POLICY.discovery_slots,
                CH1_POLICY.discovery_limit,
                CH1_POLICY.processing_candidate_limit,
            ),
            (3, 16, 1, 3),
        )
        self.assertEqual(CH1_POLICY.step_label(7), "H07")
        self.assertEqual(
            CH1_POLICY.static_assets,
            (
                "vertical_constants_icon-ch1-eps.grib2",
                "horizontal_constants_icon-ch1-eps.grib2",
            ),
        )
        self.assertEqual(CH2_POLICY.maximum_horizon(_utc(3)), 120)
        self.assertEqual(
            (
                CH2_POLICY.run_interval_hours,
                CH2_POLICY.discovery_slots,
                CH2_POLICY.discovery_limit,
                CH2_POLICY.processing_candidate_limit,
            ),
            (6, 20, 2, 2),
        )
        self.assertEqual(CH2_POLICY.step_label(7), "H007")

    def test_direct_chunk_default_plan_matches_existing_fetch_groups(self):
        products = ProductSelection(wind=False, sunshine=True, rain=True, sunrain=True, cloud=False)
        for policy in (CH1_POLICY, CH2_POLICY):
            with self.subTest(model=policy.model):
                initial = policy.horizon_plan(0, profile_mode="direct-chunk", products=products)
                self.assertEqual(initial.profile, ("T", "U", "V", "P", "QV"))
                self.assertEqual(initial.map_fields, ("U", "V", "U_10M", "V_10M"))
                self.assertEqual(initial.primary, ("T", "U", "V", "P", "QV", "U_10M", "V_10M"))
                self.assertEqual(initial.rain, ("TOT_PREC",))
                self.assertEqual(initial.cloud, ())
                self.assertEqual(initial.radiation, ())
                self.assertEqual(
                    initial.batch,
                    ("T", "U", "V", "P", "QV", "U_10M", "V_10M", "TOT_PREC"),
                )
                next_hour = policy.horizon_plan(1, profile_mode="direct-chunk", products=products)
                self.assertEqual(
                    next_hour.radiation,
                    ("ASWDIR_S", "ASWDIFD_S", "DURSUN", "DURSUN_M"),
                )
                self.assertEqual(next_hour.batch[-4:], next_hour.radiation)

    def test_map_surface_cloud_and_rain_sets_are_independent(self):
        products = ProductSelection(wind=True, sunshine=False, rain=False, sunrain=False, cloud=True)
        for policy in (CH1_POLICY, CH2_POLICY):
            with self.subTest(model=policy.model):
                plan = policy.horizon_plan(4, profile_mode="none", products=products)
                self.assertEqual(plan.profile, ())
                self.assertEqual(plan.map_fields, ("U", "V", "U_10M", "V_10M"))
                self.assertEqual(plan.rain, ())
                self.assertEqual(plan.cloud, ("CLCT", "CLCL", "CLCM", "CLCH"))
                self.assertEqual(plan.radiation, ())
                self.assertEqual(
                    plan.batch,
                    ("U", "V", "U_10M", "V_10M", "CLCT", "CLCL", "CLCM", "CLCH"),
                )

    def test_representative_fetch_decode_accumulate_finalize_and_cleanup_trace_is_structured(self):
        products = ProductSelection(wind=True, sunshine=True, rain=True, sunrain=True, cloud=True)
        plan = CH1_POLICY.horizon_plan(1, profile_mode="direct-chunk", products=products)
        trace = [(operation.phase, operation.owner) for operation in plan.operation_trace(
            profile_mode="direct-chunk",
            products=products,
        )]
        self.assertEqual(
            trace,
            [
                ("fetch", "primary"), ("decode", "primary"),
                ("fetch", "rain"), ("decode", "rain"),
                ("fetch", "cloud"), ("decode", "cloud"),
                ("fetch", "radiation"), ("decode", "radiation"),
                ("accumulate", "profile"), ("accumulate", "wind"),
                ("accumulate", "sunshine"), ("accumulate", "rain"),
                ("accumulate", "sunrain"), ("accumulate", "cloud"),
                ("cleanup", "temporary-downloads"),
            ],
        )
        self.assertEqual(
            [(operation.phase, operation.owner) for operation in completion_operation_trace(
                profile_mode="direct-chunk",
                products=products,
            )],
            [
                ("finalize", "profile"),
                ("finalize", "wind"), ("cleanup", "old-wind-runs"),
                ("finalize", "sunshine"), ("cleanup", "old-sunshine-runs"),
                ("finalize", "rain"), ("cleanup", "old-rain-runs"),
                ("finalize", "sunrain"), ("cleanup", "old-sunrain-runs"),
                ("finalize", "cloud"), ("cleanup", "old-cloud-runs"),
            ],
        )


class FetchConfigTests(unittest.TestCase):
    def test_legacy_defaults_and_reversed_range_remain_compatible(self):
        config = parse_startup_config(
            "ch1",
            {"CH1_HORIZON_START": "12", "CH1_HORIZON_END": "3", "UNRELATED_SECRET": "ignored"},
            default_output_roots=DEFAULT_ROOTS,
        )
        self.assertEqual((config.horizon_start, config.horizon_end), (3, 12))
        self.assertEqual(config.profile_chunk_id, "H003_H012")
        self.assertEqual(config.profile_mode, "direct-chunk")
        self.assertTrue(config.require_full_horizon_run)
        self.assertEqual(config.products, ProductSelection())
        self.assertEqual(config.download_workers, 4)

    def test_valid_flags_paths_and_run_alias_form_an_immutable_config(self):
        config = parse_startup_config(
            "ch2",
            {
                "CH2_PROFILE_MODE": "none",
                "CH2_REFERENCE_TIME": "2026-07-17T06:00:00Z",
                "CH2_PROFILE_CHUNK_ID": "maps-only",
                "CH2_WIND_MAP_OUT_ROOT": "/tmp/wind",
                "ENABLE_WIND_MAPS": "yes",
                "ENABLE_WIND_MAPS_CH2": "1",
                "ENABLE_RAIN_MAPS_CH2": "off",
                "XCBENZ_FETCH_HORIZON_BATCH": "true",
                "XCBENZ_PREFETCH_NEXT_HORIZON": "on",
                "DOWNLOAD_WORKERS": "8",
                "XCBENZ_FETCH_TMP_DIR": "/tmp/fetch",
            },
            default_output_roots=DEFAULT_ROOTS,
        )
        self.assertEqual(config.pinned_run, _utc(6))
        self.assertTrue(config.products.wind)
        self.assertFalse(config.products.rain)
        self.assertEqual(config.output_roots.wind, "/tmp/wind")
        self.assertEqual(str(config.fetch_tmp_dir), "/tmp/fetch")
        self.assertTrue(config.horizon_fetch_batch and config.prefetch_next_horizon)
        self.assertFalse(config.require_full_horizon_run)
        with self.assertRaises((AttributeError, TypeError)):
            config.download_workers = 2

    def test_invalid_owned_values_fail_fast_with_the_variable_name(self):
        cases = [
            ({"FORCE_REFRESH": "sometimes"}, "FORCE_REFRESH"),
            ({"DOWNLOAD_WORKERS": "many"}, "DOWNLOAD_WORKERS"),
            ({"DOWNLOAD_WORKERS": "9"}, "DOWNLOAD_WORKERS"),
            ({"CH1_PROFILE_MODE": "legacy"}, "CH1_PROFILE_MODE"),
            ({"CH1_RUN_TAG": "latest"}, "CH1_RUN_TAG"),
            ({"CH1_WIND_MAP_OUT_ROOT": ""}, "CH1_WIND_MAP_OUT_ROOT"),
            ({"ENABLE_WIND_MAPS": "false", "ENABLE_WIND_MAPS_CH1": "invalid"}, "ENABLE_WIND_MAPS_CH1"),
        ]
        for env, name in cases:
            with self.subTest(name=name), self.assertRaisesRegex(FetchConfigError, name):
                parse_startup_config("ch1", env, default_output_roots=DEFAULT_ROOTS)

    def test_conflicting_run_aliases_do_not_echo_unrelated_values(self):
        env = {
            "CH2_RUN_TAG": "20260717_0600",
            "CH2_REFERENCE_TIME": "20260717_1200",
            "UNRELATED_SECRET": "do-not-print",
        }
        with self.assertRaises(FetchConfigError) as raised:
            parse_startup_config("ch2", env, default_output_roots=DEFAULT_ROOTS)
        message = str(raised.exception)
        self.assertIn("CH2_RUN_TAG", message)
        self.assertIn("CH2_REFERENCE_TIME", message)
        self.assertNotIn("do-not-print", message)


if __name__ == "__main__":
    unittest.main()
