import json
import os
import shutil
import tempfile
import unittest

from generate_combined_manifest import scan_direct_wind_maps


def _temp_workspace():
    return tempfile.mkdtemp(prefix="xcb_manifest_", dir=os.getenv("TEST_TMPDIR", r"C:\tmp"))


class ManifestWindMapTests(unittest.TestCase):
    def test_scan_direct_wind_maps_emits_completed_levels_only(self):
        tmp = _temp_workspace()
        cwd = os.getcwd()
        try:
            os.chdir(tmp)
            level_dir = os.path.join("cache_wind_maps", "ch1", "20260510_0300", "800m_AGL")
            steps_dir = os.path.join(level_dir, "steps")
            os.makedirs(steps_dir, exist_ok=True)
            step_path = os.path.join(steps_dir, "H00.bin")
            with open(step_path, "wb") as f:
                f.write(b"\x00\x01")
            with open(os.path.join(level_dir, "metadata.json"), "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "level": {"name": "800m_AGL", "type": "AGL", "height_m": 800.0},
                        "encoding": {"components": ["u", "v"]},
                        "grid": {"width": 1, "height": 1, "source_stride": 2},
                        "steps": [{"step": "H00", "horizon": 0, "path": step_path, "byte_length": 2}],
                    },
                    f,
                )

            manifest = scan_direct_wind_maps()
            level = manifest["ch1"]["20260510_0300"]["levels"]["800m_AGL"]

            self.assertEqual(level["components"], ["u", "v"])
            self.assertEqual(level["step_count"], 1)
            self.assertEqual(level["bytes"], 2)
            self.assertEqual(manifest["ch1"]["20260510_0300"]["layout"], "browser_ready_split_binary_by_step")
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
