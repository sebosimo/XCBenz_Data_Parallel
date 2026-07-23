import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import generate_web_exports
from generate_web_exports import expected_profile_chunks, export_value_tiles_capability, scan_profile_chunks
from value_tiles import capability_declaration


def _temp_workspace():
    return tempfile.mkdtemp(prefix="xcb_web_exports_", dir=os.getenv("TEST_TMPDIR", r"C:\tmp"))


class GenerateWebExportsTests(unittest.TestCase):
    def test_value_tile_capability_is_absent_when_feature_is_disabled(self):
        manifest = {}
        with mock.patch.object(generate_web_exports, "value_tiles_enabled", return_value=False), mock.patch.object(
            generate_web_exports, "generate_value_tiles"
        ) as generator:
            self.assertIsNone(export_value_tiles_capability(manifest))
        generator.assert_not_called()
        self.assertNotIn("capabilities", manifest)

    def test_value_tile_capability_is_added_only_after_generation_returns(self):
        manifest = {"capabilities": {"existing": {"version": 1}}}
        generated = {"counts": {"runs": 1, "variants": 8, "tiles": 96}}
        with mock.patch.object(generate_web_exports, "value_tiles_enabled", return_value=True), mock.patch.object(
            generate_web_exports, "generate_value_tiles", return_value=generated
        ) as generator:
            self.assertEqual(export_value_tiles_capability(manifest), generated)
        generator.assert_called_once_with(
            generate_web_exports.WEB_DIR,
            selected_runs=None,
            validate=True,
        )
        self.assertEqual(manifest["capabilities"]["existing"], {"version": 1})
        self.assertEqual(manifest["capabilities"]["spatial_value_tiles"], capability_declaration())

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
