"""Tests for Brotli (.br) sidecar generation on web_exports."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.precompress_web_exports import (
    detect_brotli_encoder,
    precompress_web_exports,
    should_precompress_file,
)


class PrecompressWebExportsTests(unittest.TestCase):
    def test_precompresses_forecast_artifacts_skips_range_profiles_and_live_subtrees(self) -> None:
        encoder = detect_brotli_encoder()
        self.assertIsNotNone(encoder, "Expected at least one Brotli encoder (node, brotli CLI, or python brotli)")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "web_exports"
            wind_step = root / "wind_maps" / "icon-ch1" / "20261121_0600" / "800m_AGL" / "steps" / "H00.bin"
            wind_step.parent.mkdir(parents=True, exist_ok=True)
            wind_step.write_bytes(bytes([12, 12, 12, 13] * 1024))

            manifest = root / "manifest.compact.json"
            manifest.write_text(json.dumps({"entries": ["item"] * 500}), encoding="utf-8")

            tiny = root / "tiny.json"
            tiny.write_text('{"ok":true}', encoding="utf-8")

            profiles = root / "emagrams" / "icon-ch1" / "20261121_0600" / "bern" / "profiles.bin"
            profiles.parent.mkdir(parents=True, exist_ok=True)
            profiles.write_bytes(b"\x00\x00\x80?" * 2048)

            bundle = root / "emagrams" / "icon-ch1" / "20261121_0600" / "bern" / "bundle.json"
            bundle.write_text(json.dumps({"steps": ["H00"] * 400}), encoding="utf-8")

            live_file = root / "live_stations" / "latest.json"
            live_file.parent.mkdir(parents=True, exist_ok=True)
            live_file.write_text(json.dumps({"stations": ["s"] * 400}), encoding="utf-8")

            value_tile = root / "value_tiles" / "v1" / "icon-ch1" / "20261121_0600" / "abcdef012345" / "t0_0.xvt"
            value_tile.parent.mkdir(parents=True, exist_ok=True)
            value_tile.write_bytes(b"XVT1" + b"\x00" * 4096)

            orphan = root / "wind_maps" / "removed.json.br"
            orphan.write_bytes(b"stale")

            first = precompress_web_exports(root, verify=True, require_encoder=True)
            self.assertEqual(first.compressed_files, 3)
            self.assertEqual(first.reused_files, 0)
            self.assertEqual(first.removed_orphans, 1)
            self.assertLess(first.compressed_bytes, first.source_bytes)

            self.assertTrue(Path(f"{wind_step}.br").is_file())
            self.assertTrue(Path(f"{manifest}.br").is_file())
            self.assertTrue(Path(f"{bundle}.br").is_file())
            self.assertFalse(Path(f"{tiny}.br").exists())
            self.assertFalse(Path(f"{profiles}.br").exists())
            self.assertFalse(Path(f"{live_file}.br").exists())
            self.assertFalse(Path(f"{value_tile}.br").exists())
            self.assertFalse(orphan.exists())

            # Second pass reuses up-to-date sidecars without recompressing.
            second = precompress_web_exports(root, verify=True, require_encoder=True)
            self.assertEqual(second.compressed_files, 0)
            self.assertEqual(second.reused_files, 3)

            # Updating a source file refreshes its sidecar.
            manifest.write_text(json.dumps({"entries": ["updated"] * 600}), encoding="utf-8")
            future = manifest.stat().st_mtime + 10
            os.utime(manifest, (future, future))
            third = precompress_web_exports(root, verify=True, require_encoder=True)
            self.assertEqual(third.compressed_files, 1)
            self.assertEqual(third.reused_files, 2)

    def test_incompressible_files_do_not_keep_bloated_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "web_exports"
            root.mkdir(parents=True, exist_ok=True)
            noisy = root / "wind_maps" / "random.bin"
            noisy.parent.mkdir(parents=True, exist_ok=True)
            noisy.write_bytes(os.urandom(4096))

            result = precompress_web_exports(root, verify=True, require_encoder=True)
            self.assertEqual(result.compressed_files, 0)
            self.assertEqual(result.skipped_incompressible, 1)
            self.assertFalse(Path(f"{noisy}.br").exists())

    def test_should_precompress_file_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "web_exports"
            profiles = root / "emagrams" / "icon-ch1" / "20261121_0600" / "bern" / "profiles.bin"
            profiles.parent.mkdir(parents=True, exist_ok=True)
            profiles.write_bytes(b"0" * 2048)
            self.assertFalse(should_precompress_file(profiles, root))

            vt = root / "value_tiles" / "v1" / "sample.xvt"
            vt.parent.mkdir(parents=True, exist_ok=True)
            vt.write_bytes(b"0" * 2048)
            self.assertFalse(should_precompress_file(vt, root, include_value_tiles=False))
            self.assertTrue(should_precompress_file(vt, root, include_value_tiles=True))


if __name__ == "__main__":
    unittest.main()
