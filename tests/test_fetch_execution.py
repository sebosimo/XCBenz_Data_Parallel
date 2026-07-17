import datetime as dt
import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import numpy as np
import xarray as xr

import fetch_data
import fetch_data_ch2
from forecast_fetch import execution
from forecast_fetch import stac
from forecast_fetch.config import FetchStartupConfig, OutputRoots
from forecast_fetch.execution import FetchRuntime, execute_fetch
from forecast_fetch.planning import CH1_POLICY, CH2_POLICY, ProductSelection
from web_profiles import build_bundle_step_values


REFERENCE_TIME = dt.datetime(2026, 7, 17, 3, tzinfo=dt.timezone.utc)


def _profile_fields():
    coordinates = {
        "latitude": ("cell", np.asarray([47.0, 48.0], dtype=np.float32)),
        "longitude": ("cell", np.asarray([8.0, 9.0], dtype=np.float32)),
    }

    def field(values):
        first = np.asarray(values, dtype=np.float32)
        return xr.DataArray(
            np.stack((first, first + 99.0), axis=1),
            dims=("level", "cell"),
            coords=coordinates,
        )

    return {
        "T": field([280.0, 270.0]),
        "U": field([3.0, 4.0]),
        "V": field([4.0, 3.0]),
        "P": field([90_000.0, 80_000.0]),
        "QV": field([0.005, 0.004]),
    }


def _height_field():
    first = np.asarray([100.0, 200.0, 300.0], dtype=np.float32)
    return xr.DataArray(
        np.stack((first, first + 1_000.0), axis=1),
        dims=("half_level", "cell"),
        coords={
            "latitude": ("cell", np.asarray([47.0, 48.0], dtype=np.float32)),
            "longitude": ("cell", np.asarray([8.0, 9.0], dtype=np.float32)),
        },
    )


class FetchExecutionTests(unittest.TestCase):
    def _run_synthetic_profile(self, module, policy, workspace):
        events = []
        fields = _profile_fields()
        locations = {
            "Zurich": {
                "display_name": "Zurich",
                "type": "city",
                "lat": 47.0,
                "lon": 8.0,
            }
        }
        Path("locations.json").write_text(json.dumps(locations), encoding="utf-8")

        def record(name, result=None):
            def callback(*args, **kwargs):
                events.append(name)
                return result

            return callback

        def fetch_variables(_collection, variables, *_args):
            events.append(("fetch", tuple(variables)))
            return {variable: f"/synthetic/{variable}.grib2" for variable in variables}

        def decode(downloaded, **_kwargs):
            events.append(("decode", tuple(downloaded)))
            return ({variable: fields[variable] for variable in downloaded}, bool(downloaded))

        def append(*args, **kwargs):
            events.append("append-profile")
            return module.append_profile_chunk(*args, **kwargs)

        def finalize(*args, **kwargs):
            events.append("finalize-profile")
            return module.finalize_profile_chunk(*args, **kwargs)

        startup = FetchStartupConfig(
            model=policy.model,
            force_refresh=False,
            profile_mode="direct-chunk",
            horizon_start=0,
            horizon_end=0,
            profile_chunk_id="H000_H000",
            pinned_run=None,
            require_full_horizon_run=False,
            horizon_fetch_batch=False,
            prefetch_next_horizon=False,
            release_profile_only_fields=False,
            download_workers=1,
            fetch_tmp_dir=None,
            products=ProductSelection(
                wind=False,
                sunshine=False,
                rain=False,
                sunrain=False,
                cloud=False,
            ),
            output_roots=OutputRoots(
                wind=str(workspace / "wind"),
                sunshine=str(workspace / "sunshine"),
                rain=str(workspace / "rain"),
                sunrain=str(workspace / "sunrain"),
                cloud=str(workspace / "cloud"),
            ),
        )
        runtime = FetchRuntime(
            policy=policy,
            output_directories=("web_profile_chunks",),
            log=lambda _message, _level="INFO": None,
            load_wind_map_config=record("wind-config"),
            download_static_files=record("static"),
            get_latest_available_runs=record("discover", [REFERENCE_TIME]),
            has_profile_horizon=record("probe", True),
            load_static_hhl=record("hhl", _height_field()),
            load_static_grid=record("grid", None),
            fetch_variable_files=fetch_variables,
            seed_previous_radiation=record(
                "seed-radiation",
                {variable: None for variable in policy.sunshine_variables},
            ),
            seed_previous_rain=record("seed-rain", {"TOT_PREC": None}),
            location_indices=lambda sample, values: (
                events.append("location-indices")
                or module._location_indices(sample, values)
            ),
            append_profile_chunk=append,
            finalize_profile_chunk=finalize,
        )

        cleanup_patches = [
            mock.patch.object(execution, name, side_effect=record(name))
            for name in (
                "cleanup_old_wind_runs",
                "cleanup_old_sunshine_runs",
                "cleanup_old_rain_runs",
                "cleanup_old_sunrain_runs",
                "cleanup_old_cloud_runs",
            )
        ]
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(execution, "_decode_fields", side_effect=decode))
            for patcher in cleanup_patches:
                stack.enter_context(patcher)
            execute_fetch(runtime, startup)

        tag = REFERENCE_TIME.strftime("%Y%m%d_%H%M")
        output = (
            Path("web_profile_chunks")
            / f"icon-{policy.model}"
            / tag
            / "H000_H000"
            / "Zurich"
        )
        return events, output

    def test_both_wrappers_follow_the_same_ordered_engine_trace_and_emit_exact_profile_bytes(self):
        expected_values = build_bundle_step_values(
            p=np.asarray([90_000.0, 80_000.0], dtype=np.float32),
            t=np.asarray([280.0, 270.0], dtype=np.float32),
            qv=np.asarray([0.005, 0.004], dtype=np.float32),
            u=np.asarray([3.0, 4.0], dtype=np.float32),
            v=np.asarray([4.0, 3.0], dtype=np.float32),
            level_count=2,
        )
        expected_bytes = np.asarray([expected_values], dtype="<f4").tobytes()
        traces = []

        for module, policy in ((fetch_data, CH1_POLICY), (fetch_data_ch2, CH2_POLICY)):
            with self.subTest(model=policy.model), tempfile.TemporaryDirectory(
                prefix=f"xcb_{policy.model}_execution_",
                dir=os.getenv("TEST_TMPDIR", "/tmp"),
            ) as temporary:
                previous_cwd = Path.cwd()
                try:
                    os.chdir(temporary)
                    events, output = self._run_synthetic_profile(module, policy, Path(temporary))
                    traces.append(events)
                    self.assertEqual((output / "profiles.bin").read_bytes(), expected_bytes)
                    metadata = json.loads((output / "chunk.json").read_text(encoding="utf-8"))
                    self.assertEqual(metadata["height"], [150.0, 250.0])
                    self.assertEqual(metadata["steps"][0]["step"], policy.step_label(0))
                    self.assertEqual(metadata["encoding"]["byte_length"], len(expected_bytes))
                finally:
                    os.chdir(previous_cwd)

        self.assertEqual(traces[0], traces[1])
        self.assertEqual(
            traces[0],
            [
                "static",
                "discover",
                "hhl",
                "grid",
                "seed-radiation",
                ("fetch", ("T", "U", "V", "P", "QV")),
                ("decode", ("T", "U", "V", "P", "QV")),
                "location-indices",
                "append-profile",
                "finalize-profile",
                "cleanup_old_wind_runs",
                "cleanup_old_sunshine_runs",
                "cleanup_old_rain_runs",
                "cleanup_old_sunrain_runs",
                "cleanup_old_cloud_runs",
            ],
        )

    def test_policy_owns_stac_download_and_cleanup_differences(self):
        self.assertEqual(CH1_POLICY.stac_assets_url, f"{CH1_POLICY.stac_base_url}/assets")
        self.assertEqual(CH2_POLICY.stac_assets_url, f"{CH2_POLICY.stac_base_url}/assets")
        self.assertEqual(CH1_POLICY.temporary_prefix("batch"), "temp_batch")
        self.assertEqual(CH2_POLICY.temporary_prefix("batch"), "temp_ch2_batch")
        self.assertEqual(CH1_POLICY.discovery_lookback_hours, 48)
        self.assertEqual(CH2_POLICY.discovery_lookback_hours, 120)
        self.assertEqual(CH1_POLICY.required_probe_horizon(33), 33)
        self.assertEqual(CH2_POLICY.required_probe_horizon(33), 120)
        self.assertEqual(
            (
                CH1_POLICY.download_deadline_seconds,
                CH1_POLICY.remove_partial_downloads,
                CH1_POLICY.cleanup_anchor_hour,
            ),
            (None, False, 3),
        )
        self.assertEqual(
            (
                CH2_POLICY.download_deadline_seconds,
                CH2_POLICY.remove_partial_downloads,
                CH2_POLICY.cleanup_anchor_hour,
            ),
            (90, True, 0),
        )

    def test_download_retry_policy_preserves_ch1_partial_and_removes_ch2_partial(self):
        def failing_response():
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.raise_for_status.return_value = None

            def chunks():
                yield b"partial"
                raise OSError("synthetic stream failure")

            response.iter_content.side_effect = chunks
            return response

        with tempfile.TemporaryDirectory(
            prefix="xcb_stac_policy_",
            dir=os.getenv("TEST_TMPDIR", "/tmp"),
        ) as temporary:
            for policy, expected_exists in ((CH1_POLICY, True), (CH2_POLICY, False)):
                with self.subTest(model=policy.model):
                    target = Path(temporary) / f"{policy.model}.grib2"
                    with mock.patch.object(
                        stac.requests,
                        "get",
                        return_value=failing_response(),
                    ) as request, mock.patch.object(stac.time, "sleep"):
                        succeeded = stac.download_file(
                            "https://example.invalid/forecast.grib2",
                            str(target),
                            policy=policy,
                            log=lambda *_args: None,
                            max_retries=1,
                        )
                    self.assertFalse(succeeded)
                    self.assertEqual(target.exists(), expected_exists)
                    request.assert_called_once_with(
                        "https://example.invalid/forecast.grib2",
                        stream=True,
                        timeout=policy.request_timeout_seconds,
                    )

    def test_mocked_product_run_orders_fetch_decode_accumulate_finalize_and_cleanup(self):
        traces = []
        for policy in (CH1_POLICY, CH2_POLICY):
            with self.subTest(model=policy.model), tempfile.TemporaryDirectory(
                prefix=f"xcb_{policy.model}_trace_",
                dir=os.getenv("TEST_TMPDIR", "/tmp"),
            ) as temporary:
                previous_cwd = Path.cwd()
                events = []
                try:
                    os.chdir(temporary)
                    Path("locations.json").write_text(
                        json.dumps({"A": {"lat": 47.0, "lon": 8.0}}),
                        encoding="utf-8",
                    )
                    fields = _profile_fields()
                    sample = fields["T"]

                    def fetch_variables(_collection, variables, *_args):
                        owner = (
                            "radiation"
                            if variables and variables[0] == "ASWDIR_S"
                            else "rain"
                            if tuple(variables) == policy.rain_variables
                            else "cloud"
                            if tuple(variables) == policy.cloud_variables
                            else "primary"
                        )
                        events.append(("fetch", owner))
                        return {
                            variable: f"/synthetic/{owner}-{variable}.grib2"
                            for variable in variables
                        }

                    def decode(downloaded, *, owner, **_kwargs):
                        events.append(("decode", owner))
                        return (
                            {variable: fields.get(variable, sample) for variable in downloaded},
                            bool(downloaded),
                        )

                    class Accumulator:
                        def __init__(self, owner):
                            self.owner = owner

                        def seed_previous_raw(self, _value):
                            events.append(("seed", self.owner))

                        def append(self, *_args):
                            events.append(("accumulate", self.owner))

                        def finalize(self):
                            events.append(("finalize", self.owner))

                    def accumulator_factory(owner):
                        return lambda *_args, **_kwargs: Accumulator(owner)

                    startup = FetchStartupConfig(
                        model=policy.model,
                        force_refresh=False,
                        profile_mode="direct-chunk",
                        horizon_start=1,
                        horizon_end=1,
                        profile_chunk_id="H001_H001",
                        pinned_run=REFERENCE_TIME,
                        require_full_horizon_run=False,
                        horizon_fetch_batch=False,
                        prefetch_next_horizon=False,
                        release_profile_only_fields=False,
                        download_workers=1,
                        fetch_tmp_dir=None,
                        products=ProductSelection(
                            wind=True,
                            sunshine=True,
                            rain=True,
                            sunrain=True,
                            cloud=True,
                        ),
                        output_roots=OutputRoots(
                            wind="wind",
                            sunshine="sunshine",
                            rain="rain",
                            sunrain="sunrain",
                            cloud="cloud",
                        ),
                    )
                    runtime = FetchRuntime(
                        policy=policy,
                        output_directories=("outputs",),
                        log=lambda *_args: None,
                        load_wind_map_config=lambda **_kwargs: object(),
                        download_static_files=lambda: events.append("static"),
                        get_latest_available_runs=lambda **_kwargs: [],
                        has_profile_horizon=lambda *_args: True,
                        load_static_hhl=lambda: None,
                        load_static_grid=lambda: None,
                        fetch_variable_files=fetch_variables,
                        seed_previous_radiation=lambda *_args: {
                            variable: None for variable in policy.sunshine_variables
                        },
                        seed_previous_rain=lambda *_args: {"TOT_PREC": None},
                        location_indices=lambda *_args: {"A": 0},
                        append_profile_chunk=lambda *_args, **_kwargs: events.append(
                            ("accumulate", "profile")
                        ),
                        finalize_profile_chunk=lambda *_args, **_kwargs: events.append(
                            ("finalize", "profile")
                        ),
                    )

                    radiation_dataset = xr.Dataset(
                        {"value": ("cell", np.asarray([2.0, 3.0], dtype=np.float32))}
                    )

                    def open_radiation(*_args, **_kwargs):
                        events.append(("decode", "radiation"))
                        return radiation_dataset.copy()

                    patches = {
                        "WindMapAccumulator": accumulator_factory("wind"),
                        "SunshineMapAccumulator": accumulator_factory("sunshine"),
                        "RainMapAccumulator": accumulator_factory("rain"),
                        "SunRainMapAccumulator": accumulator_factory("sunrain"),
                        "CloudMapAccumulator": accumulator_factory("cloud"),
                    }
                    with ExitStack() as stack:
                        stack.enter_context(
                            mock.patch.object(execution, "_decode_fields", side_effect=decode)
                        )
                        stack.enter_context(
                            mock.patch.object(execution.xr, "open_dataset", side_effect=open_radiation)
                        )
                        for name, factory in patches.items():
                            stack.enter_context(mock.patch.object(execution, name, side_effect=factory))
                        for name in (
                            "cleanup_old_wind_runs",
                            "cleanup_old_sunshine_runs",
                            "cleanup_old_rain_runs",
                            "cleanup_old_sunrain_runs",
                            "cleanup_old_cloud_runs",
                        ):
                            owner = name.removeprefix("cleanup_old_").removesuffix("_runs")
                            stack.enter_context(
                                mock.patch.object(
                                    execution,
                                    name,
                                    side_effect=lambda *_args, _owner=owner, **_kwargs: events.append(
                                        ("cleanup", _owner)
                                    ),
                                )
                            )
                        execute_fetch(runtime, startup)
                    traces.append(events)
                finally:
                    os.chdir(previous_cwd)

        self.assertEqual(traces[0], traces[1])
        phases = [
            event
            for event in traces[0]
            if isinstance(event, tuple) and event[0] in {
                "fetch",
                "decode",
                "accumulate",
                "finalize",
                "cleanup",
            }
        ]
        self.assertEqual(
            phases,
            [
                ("fetch", "primary"),
                ("decode", "primary"),
                ("fetch", "rain"),
                ("decode", "rain"),
                ("fetch", "cloud"),
                ("decode", "cloud"),
                ("fetch", "radiation"),
                ("decode", "radiation"),
                ("decode", "radiation"),
                ("decode", "radiation"),
                ("decode", "radiation"),
                ("accumulate", "profile"),
                ("accumulate", "sunshine"),
                ("accumulate", "rain"),
                ("accumulate", "sunrain"),
                ("accumulate", "cloud"),
                ("accumulate", "wind"),
                ("finalize", "wind"),
                ("finalize", "sunshine"),
                ("finalize", "rain"),
                ("finalize", "sunrain"),
                ("finalize", "cloud"),
                ("finalize", "profile"),
                ("cleanup", "wind"),
                ("cleanup", "sunshine"),
                ("cleanup", "rain"),
                ("cleanup", "sunrain"),
                ("cleanup", "cloud"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
