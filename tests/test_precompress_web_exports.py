"""Tests for Brotli (.br) sidecar generation, cleanup, reuse, and remote validation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest import mock

from pipeline_orchestration.model_reuse import generation_identities
from scripts.precompress_web_exports import (
    cleanup_precompressed_sidecars,
    decompress_brotli_bytes,
    detect_brotli_encoder,
    is_run_scoped_path,
    precompress_web_exports,
    should_precompress_file,
)
from scripts import validate_remote_web_exports as remote_val


def _make_headers(pairs: list[tuple[str, str]]) -> Message:
    msg = Message()
    for key, value in pairs:
        msg.add_header(key, value)
    return msg


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
            leftover_tmp = root / "wind_maps" / "crashed.bin.br.tmp"
            leftover_tmp.write_bytes(b"partial")

            first = precompress_web_exports(root, verify=True, require_encoder=True)
            self.assertEqual(first.compressed_files, 3)
            self.assertEqual(first.reused_files, 0)
            self.assertEqual(first.removed_orphans, 2)
            self.assertLess(first.compressed_bytes, first.source_bytes)

            self.assertTrue(Path(f"{wind_step}.br").is_file())
            self.assertTrue(Path(f"{manifest}.br").is_file())
            self.assertTrue(Path(f"{bundle}.br").is_file())
            self.assertFalse(Path(f"{tiny}.br").exists())
            self.assertFalse(Path(f"{profiles}.br").exists())
            self.assertFalse(Path(f"{live_file}.br").exists())
            self.assertFalse(Path(f"{value_tile}.br").exists())
            self.assertFalse(orphan.exists())
            self.assertFalse(leftover_tmp.exists())

            # Second pass reuses run-scoped sidecars (wind_step + bundle) and always
            # refreshes mutable top-level manifests (manifest.compact.json) even when mtime is older.
            older = manifest.stat().st_mtime - 60
            manifest.write_text(json.dumps({"entries": ["hydrated-older-mtime"] * 600}), encoding="utf-8")
            os.utime(manifest, (older, older))
            second = precompress_web_exports(root, verify=True, require_encoder=True)
            self.assertEqual(second.compressed_files, 1)
            self.assertEqual(second.reused_files, 2)
            self.assertEqual(
                decompress_brotli_bytes(Path(f"{manifest}.br").read_bytes()),
                manifest.read_bytes(),
            )

    def test_cleanup_and_no_encoder_remove_stale_sidecars_outside_live_subtrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "web_exports"
            manifest = root / "manifest.compact.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps({"entries": ["item"] * 500}), encoding="utf-8")
            manifest_br = Path(f"{manifest}.br")
            manifest_br.write_bytes(b"stale-br")
            tmp_br = root / "wind_maps" / "leftover.bin.br.tmp"
            tmp_br.parent.mkdir(parents=True, exist_ok=True)
            tmp_br.write_bytes(b"tmp")

            live_br = root / "live_stations" / "keep.json.br"
            live_br.parent.mkdir(parents=True, exist_ok=True)
            live_br.write_bytes(b"live-owned")

            with mock.patch("scripts.precompress_web_exports.detect_brotli_encoder", return_value=None):
                result = precompress_web_exports(root, require_encoder=False)
            self.assertTrue(result.skipped_no_encoder)
            self.assertEqual(result.removed_orphans, 2)
            self.assertFalse(manifest_br.exists())
            self.assertFalse(tmp_br.exists())
            self.assertTrue(live_br.is_file())

            # Direct cleanup_precompressed_sidecars (used when PRECOMPRESS_WEB_EXPORTS=false)
            manifest_br.write_bytes(b"stale-again")
            removed = cleanup_precompressed_sidecars(root)
            self.assertEqual(removed, 1)
            self.assertFalse(manifest_br.exists())
            self.assertTrue(live_br.is_file())

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

    def test_should_precompress_file_and_run_scope_policy(self) -> None:
        self.assertTrue(is_run_scoped_path(Path("wind_maps/icon-ch1/20261121_0600/800m_AGL/steps/H00.bin")))
        self.assertFalse(is_run_scoped_path(Path("manifest.compact.json")))
        self.assertFalse(is_run_scoped_path(Path("wind_maps/manifest.compact.json")))

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

    def test_model_reuse_fingerprint_tracks_precompress_env_vars(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        base = generation_identities(repo, {"PRECOMPRESS_WEB_EXPORTS": "true"}, {})
        disabled = generation_identities(repo, {"PRECOMPRESS_WEB_EXPORTS": "false"}, {})
        vt_enabled = generation_identities(
            repo, {"PRECOMPRESS_WEB_EXPORTS": "true", "PRECOMPRESS_VALUE_TILES": "true"}, {}
        )
        self.assertNotEqual(base["ch1"], disabled["ch1"])
        self.assertNotEqual(base["ch1"], vt_enabled["ch1"])

    def test_remote_brotli_validator_checks_headers_and_decoded_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "web_exports"
            sample = root / "manifest.compact.json"
            sample.parent.mkdir(parents=True, exist_ok=True)
            plain = json.dumps({"entries": ["item"] * 500}).encode("utf-8")
            sample.write_bytes(plain)
            precompress_web_exports(root, verify=True, require_encoder=True)
            encoded = Path(f"{sample}.br").read_bytes()

        good_headers = _make_headers([
            ("Content-Encoding", "br"),
            ("Content-Type", "application/json; charset=utf-8"),
            ("Vary", "Accept-Encoding"),
            ("Cache-Control", "no-cache, must-revalidate"),
        ])
        with mock.patch.object(
            remote_val,
            "fetch",
            return_value=remote_val.FetchResult("https://data.xcbenz.com/web_exports/manifest.compact.json", encoded, good_headers),
        ):
            self.assertTrue(
                remote_val.validate_brotli_response(
                    "https://data.xcbenz.com/web_exports/manifest.compact.json",
                    expected_plain=plain,
                    expected_content_type="application/json",
                    expected_cache_token="no-cache",
                    required=True,
                )
            )

        # Reject double-encoding.
        double_headers = _make_headers([
            ("Content-Encoding", "br"),
            ("Content-Encoding", "gzip"),
            ("Content-Type", "application/json"),
            ("Vary", "Accept-Encoding"),
            ("Cache-Control", "no-cache"),
        ])
        with mock.patch.object(
            remote_val,
            "fetch",
            return_value=remote_val.FetchResult("https://data.xcbenz.com/web_exports/manifest.compact.json", encoded, double_headers),
        ):
            with self.assertRaisesRegex(remote_val.ValidationError, "unexpected Content-Encoding"):
                remote_val.validate_brotli_response(
                    "https://data.xcbenz.com/web_exports/manifest.compact.json",
                    expected_plain=plain,
                    expected_content_type="application/json",
                    expected_cache_token="no-cache",
                )

        # Reject wrong Content-Type.
        wrong_type = _make_headers([
            ("Content-Encoding", "br"),
            ("Content-Type", "application/octet-stream"),
            ("Vary", "Accept-Encoding"),
            ("Cache-Control", "no-cache"),
        ])
        with mock.patch.object(
            remote_val,
            "fetch",
            return_value=remote_val.FetchResult("https://data.xcbenz.com/web_exports/manifest.compact.json", encoded, wrong_type),
        ):
            with self.assertRaisesRegex(remote_val.ValidationError, "Content-Type"):
                remote_val.validate_brotli_response(
                    "https://data.xcbenz.com/web_exports/manifest.compact.json",
                    expected_plain=plain,
                    expected_content_type="application/json",
                    expected_cache_token="no-cache",
                )

        # Reject missing Vary.
        missing_vary = _make_headers([
            ("Content-Encoding", "br"),
            ("Content-Type", "application/json"),
            ("Cache-Control", "no-cache"),
        ])
        with mock.patch.object(
            remote_val,
            "fetch",
            return_value=remote_val.FetchResult("https://data.xcbenz.com/web_exports/manifest.compact.json", encoded, missing_vary),
        ):
            with self.assertRaisesRegex(remote_val.ValidationError, "Vary"):
                remote_val.validate_brotli_response(
                    "https://data.xcbenz.com/web_exports/manifest.compact.json",
                    expected_plain=plain,
                    expected_content_type="application/json",
                    expected_cache_token="no-cache",
                )

        # Reject stale/mismatched decoded SHA-256.
        with mock.patch.object(
            remote_val,
            "fetch",
            return_value=remote_val.FetchResult("https://data.xcbenz.com/web_exports/manifest.compact.json", encoded, good_headers),
        ):
            with self.assertRaisesRegex(remote_val.ValidationError, "SHA-256"):
                remote_val.validate_brotli_response(
                    "https://data.xcbenz.com/web_exports/manifest.compact.json",
                    expected_plain=b'{"different":true}',
                    expected_content_type="application/json",
                    expected_cache_token="no-cache",
                )


if __name__ == "__main__":
    unittest.main()
