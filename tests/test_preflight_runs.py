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
