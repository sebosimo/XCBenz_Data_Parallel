import os
import unittest
import urllib.error
from unittest import mock

from scripts import preflight_runs


class LiveManifestTests(unittest.TestCase):
    def test_configured_live_manifest_is_preferred(self):
        url = "https://data.xcbenz.com/web_exports/manifest.json"
        manifest = {"models": {"icon-ch1": {"runs": {}}}}

        with mock.patch.dict(
            os.environ,
            {"XCBENZ_PREFLIGHT_MANIFEST_URL": url},
            clear=False,
        ), mock.patch.object(
            preflight_runs,
            "get_json",
            return_value=manifest,
        ) as get_json:
            loaded = preflight_runs.load_existing_manifest()

        self.assertIs(loaded, manifest)
        get_json.assert_called_once_with(url, timeout=15)

    def test_non_object_live_manifest_fails_closed(self):
        url = "https://data.xcbenz.com/web_exports/manifest.json"

        with mock.patch.dict(
            os.environ,
            {"XCBENZ_PREFLIGHT_MANIFEST_URL": url},
            clear=False,
        ), mock.patch.object(
            preflight_runs,
            "get_json",
            return_value=[],
        ):
            with self.assertRaisesRegex(ValueError, "not a JSON object"):
                preflight_runs.load_existing_manifest()


class PreflightDecisionTests(unittest.TestCase):
    def _run(self, *, latest, manifest, force=False):
        outputs = {}
        env = {"FORCE_REFRESH": "true" if force else "false"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            preflight_runs,
            "latest_run",
            side_effect=lambda model: latest.get(model),
        ), mock.patch.object(
            preflight_runs,
            "load_existing_manifest",
            return_value=manifest,
        ) as load_manifest, mock.patch.object(
            preflight_runs,
            "write_output",
            side_effect=lambda name, value: outputs.__setitem__(name, value),
        ):
            rc = preflight_runs.main()
        return rc, outputs, load_manifest

    def test_force_refresh_runs_both_models_without_loading_manifest(self):
        rc, outputs, load_manifest = self._run(
            latest={"ch1": "20260717_0300", "ch2": "20260717_0000"},
            manifest={},
            force=True,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(outputs["should_run"], "true")
        self.assertEqual(outputs["should_run_ch1"], "true")
        self.assertEqual(outputs["should_run_ch2"], "true")
        self.assertEqual(outputs["reason"], "force_refresh")
        load_manifest.assert_not_called()

    def test_complete_latest_runs_are_skipped(self):
        ch1_steps = [{}] * 46
        ch2_steps = [{}] * 121
        manifest = {
            "models": {
                "icon-ch1": {"runs": {"20260717_0300": {"locations": {"Bern": {"steps": ch1_steps}}}}},
                "icon-ch2": {"runs": {"20260717_0000": {"locations": {"Bern": {"steps": ch2_steps}}}}},
            }
        }
        rc, outputs, _ = self._run(
            latest={"ch1": "20260717_0300", "ch2": "20260717_0000"},
            manifest=manifest,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(outputs["should_run"], "false")
        self.assertEqual(outputs["reason"], "latest_runs_already_published")

    def test_incomplete_published_run_selects_only_its_owner(self):
        manifest = {
            "models": {
                "icon-ch1": {"runs": {"20260717_0300": {"locations": {"Bern": {"steps": [{}] * 45}}}}},
                "icon-ch2": {"runs": {"20260717_0000": {"locations": {"Bern": {"steps": [{}] * 121}}}}},
            }
        }
        _, outputs, _ = self._run(
            latest={"ch1": "20260717_0300", "ch2": "20260717_0000"},
            manifest=manifest,
        )
        self.assertEqual(outputs["should_run_ch1"], "true")
        self.assertEqual(outputs["should_run_ch2"], "false")
        self.assertIn("ch1:20260717_0300_incomplete:45/46", outputs["reason"])

    def test_unavailable_live_manifest_is_not_replaced_by_git_snapshot(self):
        url = "https://data.xcbenz.com/web_exports/manifest.json"
        failure = urllib.error.URLError("data host unavailable")

        with mock.patch.dict(
            os.environ,
            {"XCBENZ_PREFLIGHT_MANIFEST_URL": url},
            clear=False,
        ), mock.patch.object(
            preflight_runs,
            "get_json",
            side_effect=failure,
        ):
            with self.assertRaises(urllib.error.URLError):
                preflight_runs.load_existing_manifest()


if __name__ == "__main__":
    unittest.main()
