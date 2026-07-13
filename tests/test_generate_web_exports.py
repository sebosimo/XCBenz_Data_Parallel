import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from generate_web_exports import expected_profile_chunks, scan_profile_chunks


def _temp_workspace():
    return tempfile.mkdtemp(prefix="xcb_web_exports_", dir=os.getenv("TEST_TMPDIR", r"C:\tmp"))


class GenerateWebExportsTests(unittest.TestCase):
    def test_ch1_non_03z_direct_chunks_end_at_h033(self):
        tmp = Path(_temp_workspace())
        try:
            root = tmp / "web_profile_chunks" / "icon-ch1"
            run = root / "20260629_1500"
            locations = {"loc_a": {"display_name": "Loc A"}}
            for chunk in ("H000_H016", "H017_H033"):
                chunk_dir = run / chunk / "loc_a"
                chunk_dir.mkdir(parents=True, exist_ok=True)
                (chunk_dir / "chunk.json").write_text(json.dumps({"chunk": chunk}), encoding="utf-8")

            runs = scan_profile_chunks(root, locations)

            self.assertEqual(expected_profile_chunks("icon-ch1", "20260629_1500"), {"H000_H016", "H017_H033"})
            self.assertIn("20260629_1500", runs)
            self.assertEqual(len(runs["20260629_1500"]["loc_a"]), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ch1_03z_direct_chunks_end_at_h045(self):
        tmp = Path(_temp_workspace())
        try:
            root = tmp / "web_profile_chunks" / "icon-ch1"
            run = root / "20260629_0300"
            locations = {"loc_a": {"display_name": "Loc A"}}
            for chunk in ("H000_H016", "H017_H033", "H034_H045"):
                chunk_dir = run / chunk / "loc_a"
                chunk_dir.mkdir(parents=True, exist_ok=True)
                (chunk_dir / "chunk.json").write_text(json.dumps({"chunk": chunk}), encoding="utf-8")

            runs = scan_profile_chunks(root, locations)

            self.assertEqual(
                expected_profile_chunks("icon-ch1", "20260629_0300"),
                {"H000_H016", "H017_H033", "H034_H045"},
            )
            self.assertIn("20260629_0300", runs)
            self.assertEqual(len(runs["20260629_0300"]["loc_a"]), 3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()