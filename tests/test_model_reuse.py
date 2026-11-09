import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from pipeline_orchestration.model_reuse import (
    REQUIRED_ROOTS,
    ModelReuseStore,
    generation_identities,
    merge_reused_profiles,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_coding_server_pipeline as runner

RUN = "20261107_0000"


def write_outputs(root, model="ch2", run=RUN):
    for product in REQUIRED_ROOTS:
        path = root / product / f"icon-{model}" / run / "fixture.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"validated output")


class ModelReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.web = self.root / "web_exports"
        self.store = ModelReuseStore(self.root / "state", self.web)
        write_outputs(self.web)
        self.manifest = {"runs_ch2": {RUN: {"location": ["H000", "H001"]}}}

    def record(self):
        self.store.record_validated("ch2", RUN, "generator-v1", self.manifest)

    def test_reuse_is_bound_to_model_run_and_generator(self):
        self.assertIsNone(self.store.load("ch2", RUN, "generator-v1"))
        self.record()
        self.assertIsNotNone(self.store.load("ch2", RUN, "generator-v1"))
        self.assertIsNone(self.store.load("ch2", "20261107_0600", "generator-v1"))
        self.assertIsNone(self.store.load("ch2", RUN, "generator-v2"))
        self.assertIsNone(self.store.load("ch1", RUN, "generator-v1"))

    def test_corrupt_missing_and_added_files_invalidate_receipt(self):
        target = self.web / "wind_maps/icon-ch2" / RUN / "fixture.bin"
        self.record()
        before = target.stat()
        target.write_bytes(b"corrupted output")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertIsNone(self.store.load("ch2", RUN, "generator-v1"))
        write_outputs(self.web)
        self.record()
        target.unlink()
        self.assertIsNone(self.store.load("ch2", RUN, "generator-v1"))
        write_outputs(self.web)
        self.record()
        target.with_name("unexpected.bin").write_bytes(b"extra")
        self.assertIsNone(self.store.load("ch2", RUN, "generator-v1"))

    def test_miss_diagnostics_distinguish_inputs_from_outputs_without_private_values(self):
        reasons = []

        def load(run=RUN, identity="generator-v1"):
            return self.store.load("ch2", run, identity, on_miss=reasons.append)

        self.assertIsNone(load())
        self.assertEqual(reasons.pop(), "missing_receipt_or_output")
        self.record()
        self.assertIsNone(load(run="20261107_0600"))
        self.assertEqual(reasons.pop(), "different_model_run")
        self.assertIsNone(load(identity="private-settings-digest"))
        self.assertEqual(reasons.pop(), "different_generation_inputs")
        target = self.web / "wind_maps/icon-ch2" / RUN / "fixture.bin"
        target.write_bytes(b"changed")
        self.assertIsNone(load())
        self.assertEqual(reasons.pop(), "changed_output_inventory")
        (self.store.state_root / "ch2.json").write_text("private-invalid-json")
        self.assertIsNone(load())
        self.assertEqual(reasons.pop(), "unverifiable_receipt_or_output:JSONDecodeError")
        self.record()
        self.assertIsNotNone(load())
        self.assertEqual(reasons, [])

    def test_receipts_are_private_bounded_and_replace_previous_run(self):
        with self.assertRaises(ValueError):
            ModelReuseStore(self.web / "state", self.web)
        self.record()
        next_run = "20261107_0600"
        write_outputs(self.web, run=next_run)
        self.store.record_validated("ch2", next_run, "generator-v1", {
            "runs_ch2": {next_run: {"location": ["H000"]}},
        })
        self.assertEqual(["ch2.json"], [p.name for p in self.store.state_root.iterdir()])
        self.assertIsNone(self.store.load("ch2", RUN, "generator-v1"))
        self.assertEqual(0o600, (self.store.state_root / "ch2.json").stat().st_mode & 0o777)
        (self.store.state_root / "ch2.json").write_text("broken")
        self.assertIsNone(self.store.load("ch2", next_run, "generator-v1"))

    def test_symlinks_cannot_be_reused(self):
        self.record()
        target = self.web / "emagrams/icon-ch2" / RUN / "fixture.bin"
        outside = self.root / "outside"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(outside)
        self.assertIsNone(self.store.load("ch2", RUN, "generator-v1"))

    def test_inventory_and_serialized_receipt_limits_fail_closed(self):
        self.record()
        with mock.patch("pipeline_orchestration.model_reuse.MAX_FILES", 2):
            self.assertIsNone(self.store.load("ch2", RUN, "generator-v1"))
            with self.assertRaisesRegex(ValueError, "file budget"):
                self.record()
        with mock.patch("pipeline_orchestration.model_reuse.MAX_RECEIPT_BYTES", 32):
            self.assertIsNone(self.store.load("ch2", RUN, "generator-v1"))
            with self.assertRaisesRegex(ValueError, "byte budget"):
                self.record()
        self.assertIsNotNone(self.store.load("ch2", RUN, "generator-v1"))

    def test_identity_covers_code_locations_policy_and_static_assets(self):
        (self.root / "locations.json").write_text("{}")
        (self.root / "fetch_data.py").write_text("VERSION = 1")
        static = self.root / "static_data"
        static.mkdir()
        (static / "grid.grib2").write_bytes(b"grid-1")
        env = {"WIND_MAP_GRID_STRIDE": "2", "HOSTNAME": "one", "PRIVATE_TOKEN": "secret"}
        with mock.patch("importlib.metadata.distributions", return_value=[]):
            first = generation_identities(self.root, env, {})
            self.assertEqual(first, generation_identities(self.root, {**env, "HOSTNAME": "two"}, {}))
            self.assertNotEqual(first, generation_identities(self.root, {**env, "WIND_MAP_GRID_STRIDE": "3"}, {}))
            for path in (self.root / "fetch_data.py", self.root / "locations.json", static / "grid.grib2"):
                previous = path.read_bytes()
                path.write_bytes(previous + b"changed")
                self.assertNotEqual(first, generation_identities(self.root, env, {}))
                path.write_bytes(previous)

    def test_partial_generation_preserves_reused_profile_provenance(self):
        self.record()
        receipt = self.store.load("ch2", RUN, "generator-v1")
        path = self.root / "manifest.json"
        path.write_text(json.dumps({
            "runs": {"new-ch1": {}}, "runs_ch2": {},
            "wind_maps": {"ch1": {"new-ch1": {}}, "ch2": {RUN: {}}},
        }))
        merge_reused_profiles(path, {"ch2": receipt})
        merged = json.loads(path.read_bytes())
        self.assertEqual(merged["runs_ch2"], self.manifest["runs_ch2"])
        self.assertEqual(merged["runs"], {"new-ch1": {}})
        self.assertNotIn("ch2", merged["wind_maps"])

    def test_reference_updates_invalidate_only_the_owning_model(self):
        static = self.root / "static_data"
        static.mkdir()
        assets = {}
        for model in ("ch1", "ch2"):
            for kind in ("horizontal", "vertical"):
                path = static / f"{kind}_constants_icon-{model}-eps.grib2"
                path.write_bytes(b"reference bytes including run metadata")
                assets[path] = model
        with mock.patch("importlib.metadata.distributions", return_value=[]):
            original = generation_identities(self.root, {}, {})
            for path, owner in assets.items():
                with self.subTest(asset=path.name):
                    previous = path.read_bytes()
                    path.write_bytes(previous + b"new reference metadata")
                    after = generation_identities(self.root, {}, {})
                    other = "ch2" if owner == "ch1" else "ch1"
                    self.assertNotEqual(original[owner], after[owner])
                    self.assertEqual(original[other], after[other])
                    path.unlink()
                    missing = generation_identities(self.root, {}, {})
                    self.assertNotEqual(original[owner], missing[owner])
                    self.assertEqual(original[other], missing[other])
                    path.write_bytes(previous)

    def test_unknown_and_alternate_static_assets_remain_shared(self):
        with mock.patch("importlib.metadata.distributions", return_value=[]):
            original = generation_identities(self.root, {}, {})
            for name in (
                "static_data/new_grid.grib2",
                "static_data_custom/horizontal_constants_icon-ch1-eps.grib2",
                "static_data/subfolder/vertical_constants_icon-ch2-eps.grib2",
            ):
                with self.subTest(asset=name):
                    path = self.root / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"unclassified scientific input")
                    after = generation_identities(self.root, {}, {})
                    for model in ("ch1", "ch2"):
                        self.assertNotEqual(original[model], after[model])
                    path.unlink()

    def test_shared_inputs_invalidate_both_models_and_diagnostics_hide_values(self):
        source = self.root / "fetch_data.py"
        source.write_text("VERSION = 1")
        env = {"WEB_EXPORT_DATA_ROOT": "private-value", "CH1_RUN_TAG": RUN}
        captured = {}
        with mock.patch("importlib.metadata.distributions", return_value=[]):
            original = generation_identities(
                self.root, env, {}, on_identity=lambda model, parts: captured.update({model: parts}),
            )
            self.assertNotEqual(original["ch1"], original["ch2"])
            self.assertEqual(original, generation_identities(
                self.root, {**env, "CH1_RUN_TAG": "20261107_0300", "HOSTNAME": "new-container"}, {},
            ))
            variants = [
                generation_identities(self.root, {**env, "REQUIRE_WIND_DIRECTIONS": "true"}, {}),
                generation_identities(self.root, env, {"job_layout": "combined"}),
            ]
            source.write_text("VERSION = 2")
            variants.append(generation_identities(self.root, env, {}))
            source.write_text("VERSION = 1")
            with mock.patch("pipeline_orchestration.model_reuse.sys.version", "different-python"):
                variants.append(generation_identities(self.root, env, {}))
            with mock.patch("importlib.metadata.distributions", return_value=[
                mock.Mock(metadata={"Name": "scientific-package"}, version="new-version"),
            ]):
                variants.append(generation_identities(self.root, env, {}))
            for variant in variants:
                for model in ("ch1", "ch2"):
                    self.assertNotEqual(original[model], variant[model])
        self.assertNotIn("private-value", json.dumps(captured))
        for model, components in captured.items():
            self.assertEqual(original[model], components["identity"])
            self.assertEqual(set(components), {"source", "runtime", "policy", "settings", "static", "identity"})
            for value in components.values():
                self.assertRegex(value, r"^[a-f0-9]{64}$")

    def test_model_scoped_identity_keeps_output_verification_and_rejects_old_receipts(self):
        self.record()
        with mock.patch("importlib.metadata.distributions", return_value=[]):
            identities = generation_identities(self.root, {}, {})
        reasons = []
        self.assertIsNone(self.store.load("ch2", RUN, identities["ch2"], on_miss=reasons.append))
        self.assertEqual(reasons, ["different_generation_inputs"])
        self.store.record_validated("ch2", RUN, identities["ch2"], self.manifest)
        self.assertIsNotNone(self.store.load("ch2", RUN, identities["ch2"]))
        self.assertIsNone(self.store.load("ch2", RUN, identities["ch1"]))
        target = self.web / "wind_maps/icon-ch2" / RUN / "fixture.bin"
        target.write_bytes(b"changed output")
        self.assertIsNone(self.store.load("ch2", RUN, identities["ch2"]))

    def test_partial_export_generates_tiles_only_for_changed_model_but_validates_both(self):
        with (
            mock.patch.object(runner, "run_checked") as checked,
            mock.patch.object(runner, "merge_reused_profiles") as merge,
        ):
            runner.serial_publish_steps(
                argparse.Namespace(), {"WEB_EXPORT_DATA_ROOT": "https://example.test"},
                self.root, [sys.executable], latest_ch1="20261107_0900", latest_ch2=RUN,
                reused_models={"ch2": {"run": RUN}},
            )
        merge.assert_called_once()
        calls = {call.args[0]: call for call in checked.call_args_list}
        generated = calls["generate-web-exports"].kwargs["env"]
        validated = calls["validate-outputs"].kwargs["env"]
        self.assertEqual(generated["VALUE_TILE_GENERATE_RUNS"], "icon-ch1=20261107_0900")
        self.assertEqual(validated["VALUE_TILE_FULL_VALIDATION_RUNS"], f"icon-ch1=20261107_0900,icon-ch2={RUN}")

    def run_fixture_pipeline(self, *, force=False, failure=None, hydrate=None, identities=None, after_validation=None):
        """Exercise the real planner, reuse store, and receipt/publication ordering."""
        with mock.patch.dict(os.environ, {}, clear=True):
            args = runner.build_parser().parse_args([
                "--python-cmd", sys.executable,
                "--run-mode", "deploy-data-host" if force else "standard-deploy-data-host",
                "--no-restore-web-exports", "--no-push-data-branch", "--skip-static-prewarm",
                "--ch1-run-tag", RUN, "--ch2-run-tag", RUN,
                "--model-reuse-state-dir", str(self.store.state_root),
            ])
        events = []

        def generated(*_args, **_kwargs):
            events.append("validate")
            if failure:
                raise failure
            write_outputs(self.web, model="ch1")
            (self.root / "manifest.json").write_text(json.dumps({
                "runs": {RUN: {"location": ["H000", "H001"]}}, **self.manifest,
            }))
            if after_validation:
                after_validation()

        def hydrated(*_args, **_kwargs):
            events.append("hydrate")
            if hydrate:
                hydrate()

        with ExitStack() as stack:
            for name, value in (
                ("REPO_ROOT", self.root), ("ResourceMonitor", mock.Mock()),
            ):
                stack.enter_context(mock.patch.object(runner, name, value))
            stack.enter_context(mock.patch.object(runner, "generation_identities", side_effect=identities or [{"ch1": "generator-v1", "ch2": "generator-v1"}] * 2))
            stack.enter_context(mock.patch.object(runner, "hydrate_forecast_history", side_effect=hydrated))
            stack.enter_context(mock.patch.object(runner, "serial_publish_steps", side_effect=generated))
            jobs = stack.enter_context(mock.patch.object(runner, "run_parallel_jobs"))
            deployed = stack.enter_context(mock.patch.object(runner, "deploy_outputs", side_effect=lambda *_: events.append("deploy")))
            try:
                rc = runner.run_pipeline(args)
            except BaseException:
                deployed.assert_not_called()
                raise
        self.assertEqual(rc, 0)
        self.assertEqual(deployed.call_count, 1)
        self.assertEqual(events, ["hydrate", "validate", "deploy"])
        return {job.name.split("-", 1)[0] for job in jobs.call_args.args[0]}

    def test_runner_skips_only_verified_model_and_records_after_validation(self):
        self.record()
        self.assertEqual(self.run_fixture_pipeline(), {"ch1"})
        self.assertIsNotNone(self.store.load("ch1", RUN, "generator-v1"))

    def test_force_refresh_recomputes_both_models(self):
        self.record()
        self.assertEqual(self.run_fixture_pipeline(force=True), {"ch1", "ch2"})

    def test_runner_uses_each_model_identity_for_load_recheck_and_receipt(self):
        self.record()
        identities = {"ch1": "new-ch1-identity", "ch2": "generator-v1"}
        self.assertEqual(self.run_fixture_pipeline(identities=[identities, identities]), {"ch1"})
        self.assertIsNotNone(self.store.load("ch1", RUN, identities["ch1"]))
        self.assertIsNotNone(self.store.load("ch2", RUN, identities["ch2"]))
        self.assertIsNone(self.store.load("ch1", RUN, identities["ch2"]))

    def test_reused_outputs_changed_during_generation_abort_publication(self):
        self.record()
        target = self.web / "wind_maps/icon-ch2" / RUN / "fixture.bin"
        with self.assertRaisesRegex(RuntimeError, "reused ch2 output changed"):
            self.run_fixture_pipeline(after_validation=lambda: target.write_bytes(b"changed"))
        self.assertFalse((self.store.state_root / "ch1.json").exists())

    def test_history_hydration_precedes_reuse_verification(self):
        self.record()
        target = self.web / "wind_maps/icon-ch2" / RUN / "fixture.bin"
        models = self.run_fixture_pipeline(hydrate=lambda: target.write_bytes(b"replacement"))
        self.assertEqual(models, {"ch1", "ch2"})

    def test_validation_failure_never_creates_receipts_or_publishes(self):
        with self.assertRaisesRegex(RuntimeError, "validation failed"):
            self.run_fixture_pipeline(failure=RuntimeError("validation failed"))
        self.assertFalse(self.store.state_root.exists())

    def test_changed_generation_inputs_abort_before_receipt_or_publication(self):
        with self.assertRaisesRegex(RuntimeError, "generation inputs changed"):
            self.run_fixture_pipeline(identities=[{"ch1": "generator-v1", "ch2": "generator-v1"}, {"ch1": "changed", "ch2": "generator-v1"}])
        self.assertFalse(self.store.state_root.exists())

    def test_retry_reusing_both_models_retains_outputs_and_still_validates(self):
        (self.root / "locations.json").write_text("{}")
        with (
            mock.patch.object(runner, "REPO_ROOT", self.root),
            mock.patch.object(runner, "run_checked") as checked,
            mock.patch.object(runner, "merge_reused_profiles"),
        ):
            runner.serial_publish_steps(
                argparse.Namespace(), {"WEB_EXPORT_DATA_ROOT": "https://example.test"},
                self.root, [sys.executable], latest_ch1=RUN, latest_ch2=RUN,
                reused_models={"ch1": {"run": RUN}, "ch2": {"run": RUN}},
            )
        calls = [call.args[0] for call in checked.call_args_list]
        self.assertNotIn("generate-web-exports", calls)
        self.assertEqual(calls[-2:], ["apply-web-retention", "validate-outputs"])
        self.assertEqual((self.root / "web_exports_staging/locations.json").read_text(), "{}")

    def test_real_retention_merges_partial_model_and_rebuilds_both_model_indexes(self):
        staging = self.root / "web_exports_staging"
        staging.mkdir()
        (staging / "locations.json").write_text(json.dumps({"location": {"type": "region"}}))
        for root, model in ((self.web, "ch2"), (staging, "ch1")):
            forecast = root / f"region_forecasts/icon-{model}/{RUN}/location.json"
            forecast.parent.mkdir(parents=True, exist_ok=True)
            forecast.write_text(json.dumps({"steps": ["H000"], "products": {}}))
            bundle = root / f"emagrams/icon-{model}/{RUN}/location/bundle.json"
            bundle.parent.mkdir(parents=True, exist_ok=True)
            bundle.with_name("profiles.bin").write_bytes(bytes(20))
            bundle.write_text(json.dumps({
                "product": "emagram_bundle", "encoding": {
                    "dtype": "float32", "format": "float32-le-step-variable-level",
                    "variables": ["p", "t", "qv", "u", "v"],
                    "step_count": 1, "level_count": 1, "byte_length": 20,
                    "data": f"web_exports/emagrams/icon-{model}/{RUN}/location/profiles.bin",
                },
            }))
        retained = self.web / f"emagrams/icon-ch2/{RUN}/location/profiles.bin"
        previous = retained.read_bytes()
        completed = subprocess.run(
            [sys.executable, str(runner.REPO_ROOT / "scripts/apply_web_retention.py")],
            cwd=self.root, capture_output=True, text=True, check=False,
            env={**os.environ, "ENABLE_VALUE_TILES": "false", "PYTHONDONTWRITEBYTECODE": "1",
                 "WEB_EXPORT_DIR": "web_exports", "WEB_EXPORT_STAGING_DIR": "web_exports_staging"},
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(retained.read_bytes(), previous)
        manifest = json.loads((self.web / "manifest.json").read_bytes())
        for model in ("icon-ch1", "icon-ch2"):
            self.assertEqual(manifest["models"][model]["latest_run"], RUN)
            self.assertIn("location", manifest["models"][model]["runs"][RUN]["locations"])


if __name__ == "__main__":
    unittest.main()
