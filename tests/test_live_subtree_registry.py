import json
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.live_subtree_registry import load_live_subtrees, subtree_digest

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config/live_subtree_registry.json"
EXPECTED_DIGEST = "c25abfd7d6e108b5da8e00763bc0e5df2183f2479ad434b7e3e502ad1324939b"


class LiveSubtreeRegistryTests(unittest.TestCase):
    def test_registry_is_valid_and_matches_reviewed_digest(self):
        subtrees = load_live_subtrees(REGISTRY)

        self.assertEqual(subtree_digest(subtrees), EXPECTED_DIGEST)
        self.assertEqual(
            subtrees,
            (
                "airspace",
                "fai_records",
                "live_stations",
                "radar_maps",
                "satellite_cloud_maps",
                "webcams",
            ),
        )

    def test_forecast_and_staging_publishers_read_the_registry(self):
        hard_coded_loop = (
            "for subtree in live_stations webcams radar_maps airspace "
            "fai_records satellite_cloud_maps"
        )
        for relative in (
            "scripts/deploy_data_infomaniak.sh",
            "scripts/deploy_value_tiles_staging_infomaniak.sh",
        ):
            with self.subTest(script=relative):
                path = ROOT / relative
                script = path.read_text(encoding="utf-8")
                subprocess.run(["bash", "-n", str(path)], check=True)
                self.assertIn("live_subtree_registry.py", script)
                self.assertIn("LIVE_SUBTREE_WORDS", script)
                self.assertNotIn(hard_coded_loop, script)

    def test_registry_cli_emits_validated_shell_words(self):
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/live_subtree_registry.py"),
                "--shell-words",
                str(REGISTRY),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertEqual(completed.stdout.strip().split(), list(load_live_subtrees(REGISTRY)))

    def test_vendored_live_data_registry_matches_when_sibling_is_available(self):
        sibling = ROOT.parent / "XCBenz_Live_Data/config/live_subtree_registry.json"
        if not sibling.is_file():
            self.skipTest("XCBenz_Live_Data sibling checkout is unavailable")
        self.assertEqual(json.loads(REGISTRY.read_text()), json.loads(sibling.read_text()))


if __name__ == "__main__":
    unittest.main()
