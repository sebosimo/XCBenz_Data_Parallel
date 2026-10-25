"""Exercise real local promotion and retention without production credentials."""

import datetime as dt
import json
from io import BytesIO
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np

from w_forecast.config import Config
from w_forecast.feed import Feed
from w_forecast.publication import capacity, lease, promote, prune, publish, verified, verify_https


class WPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TEST_TMPDIR", "/tmp"))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote"
        self.remote.mkdir()
        self.stage = self.root / "stage"
        self.stage.mkdir()
        policy = Path(__file__).resolve().parents[1] / "deploy/w-prototype/infomaniak-w.htaccess"
        shutil.copyfile(policy, self.stage / "w.htaccess")
        self.reference = dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc)

    def candidate(self, reference=None, model="ch1"):
        reference = reference or self.reference
        config = Config(bbox=(0, 0, .1, .1), spacing=.05, altitudes=(1000,))
        feed = Feed(self.stage / model, config, model=model, packaging="packed-v2")
        maximum = 120 if model == "ch2" else 45 if reference.hour == 3 else 33
        frames = [feed.frame(np.ones((3, 3)), reference, h, 1000) for h in range(maximum + 1)]
        return feed.publish(frames, reference, list(range(maximum + 1)), maximum,
                            {}, {}, mode="scheduled_forecast")

    def test_both_models_publish_exact_bytes_and_preserve_unrelated_data(self):
        unrelated = self.remote / "web_exports/sunrain_maps/marker"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("unchanged")
        for model in ("ch1", "ch2"):
            pointer = self.candidate(model=model)
            _, _, files = verified(self.stage / model, model)
            before = {name: (self.stage / model / name).read_bytes() for name in files}
            promote(self.remote, self.stage, model)
            target = self.remote / "web_exports/w_forecast" / model
            self.assertEqual(json.loads((target / "latest.json").read_bytes()), pointer)
            self.assertEqual(before, {name: (target / name).read_bytes() for name in files})
            verified(target, model)
        self.assertEqual(unrelated.read_text(), "unchanged")
        self.assertFalse((self.remote / ".xcbenz_web_exports_publish.lock").exists())

    def test_corrupt_upload_cannot_change_current_pointer(self):
        self.candidate()
        promote(self.remote, self.stage, "ch1")
        target = self.remote / "web_exports/w_forecast/ch1"
        before = (target / "latest.json").read_bytes()
        self.candidate(self.reference + dt.timedelta(hours=6))
        next((self.stage / "ch1/objects").iterdir()).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "checksum"):
            promote(self.remote, self.stage, "ch1")
        self.assertEqual((target / "latest.json").read_bytes(), before)

    def test_busy_lease_blocks_and_never_removes_foreign_metadata(self):
        self.candidate()
        lock = self.remote / ".xcbenz_web_exports_publish.lock"
        lock.mkdir()
        (lock / "owner").write_text("another-publisher")
        with self.assertRaises(BlockingIOError):
            promote(self.remote, self.stage, "ch1")
        self.assertEqual((lock / "owner").read_text(), "another-publisher")
        self.assertFalse((self.remote / "web_exports/w_forecast/ch1/latest.json").exists())

    def test_guard_serializes_other_writers_and_v1_metadata_is_complete(self):
        with lease(self.remote):
            lock = self.remote / ".xcbenz_web_exports_publish.lock"
            self.assertEqual((lock / "protocol_version").read_text().strip(), "1")
            self.assertEqual(int((lock / "lease_seconds").read_text()), 300)
            with self.assertRaises(BlockingIOError):
                with lease(self.remote):
                    self.fail("overlapping publisher")

    def test_downgrade_and_failed_pointer_commit_keep_previous_readable(self):
        self.candidate(self.reference + dt.timedelta(hours=6))
        promote(self.remote, self.stage, "ch1")
        target = self.remote / "web_exports/w_forecast/ch1"
        before = (target / "latest.json").read_bytes()
        self.candidate()
        with self.assertRaisesRegex(ValueError, "downgrade"):
            promote(self.remote, self.stage, "ch1")
        self.candidate(self.reference + dt.timedelta(hours=12))
        replace = os.replace
        def fail_pointer(source, destination):
            if Path(destination) == target / "latest.json":
                raise OSError("injected pointer failure")
            return replace(source, destination)
        with patch("w_forecast.publication.os.replace", side_effect=fail_pointer):
            with self.assertRaisesRegex(OSError, "injected"):
                promote(self.remote, self.stage, "ch1")
        self.assertEqual((target / "latest.json").read_bytes(), before)
        verified(target, "ch1")

    def test_retention_keeps_two_runs_and_grace_then_removes_only_expired_w(self):
        for hours in (0, 6, 12):
            self.candidate(self.reference + dt.timedelta(hours=hours))
            promote(self.remote, self.stage, "ch1")
        target = self.remote / "web_exports/w_forecast/ch1"
        self.assertEqual(len(list((target / "runs").iterdir())), 3)
        unknown = target / "objects/unrelated.txt"
        unknown.write_text("keep")
        for path in target.rglob("*"):
            os.utime(path, (time.time() - 30000,) * 2)
        orphan = target / ("objects/" + "f" * 64 + ".wvp")
        orphan.write_bytes(b"expired")
        os.utime(orphan, (time.time() - 30000,) * 2)
        prune(target)
        self.assertEqual(len(list((target / "runs").iterdir())), 2)
        self.assertFalse(orphan.exists())
        self.assertEqual(unknown.read_text(), "keep")
        verified(target, "ch1")

    def test_quota_checks_fail_before_upload_and_ignore_shared_host_free_space(self):
        with patch("w_forecast.publication.subprocess.run") as run:
            run.return_value.stdout = "245000000 .\n"
            with self.assertRaisesRegex(ValueError, "quota"):
                capacity(self.remote, 1024**3)
        with patch("w_forecast.publication.subprocess.run") as run, patch("w_forecast.publication.shutil.disk_usage") as usage:
            run.return_value.stdout = "1 .\n"
            usage.return_value.free = 1024
            with self.assertRaisesRegex(ValueError, "filesystem reserve"):
                capacity(self.remote, 1024**3)

    def publish_env(self):
        return dict(INFOMANIAK_HOST="fixture", INFOMANIAK_USER="test",
                    INFOMANIAK_DATA_ROOT=str(self.remote), INFOMANIAK_SSH_KEY_PATH="fixture-key",
                    INFOMANIAK_KNOWN_HOSTS_PATH="fixture-known-hosts")

    def test_unchanged_pointer_verifies_https_without_upload(self):
        pointer = self.candidate()
        with patch.dict(os.environ, self.publish_env(), clear=True), patch(
            "w_forecast.publication.public_pointer", return_value=pointer
        ), patch("w_forecast.publication.verify_https") as verify, patch(
            "w_forecast.publication.subprocess.run"
        ) as run:
            publish(self.stage, "ch1")
        verify.assert_called_once()
        run.assert_not_called()
        self.assertFalse(list(self.stage.glob(".publish-*")))

    def test_upload_uses_only_pinned_files_and_cleans_stage_after_failure(self):
        self.candidate()
        (self.stage / "ch1/private-log").write_text("never publish")
        import subprocess
        calls = []
        def transport(command, **kwargs):
            calls.append(command)
            if command[0] == "rsync":
                snapshot = Path(command[-2]).parent
                if snapshot.name.startswith(".publish-"):
                    self.assertFalse((snapshot / "ch1/private-log").exists())
                    asset = next((snapshot / "ch1/objects").iterdir())
                    self.assertEqual(asset.stat().st_ino, (self.stage / "ch1/objects" / asset.name).stat().st_ino)
                raise subprocess.CalledProcessError(23, command)
        with patch.dict(os.environ, self.publish_env(), clear=True), patch(
            "w_forecast.publication.public_pointer", return_value=None
        ), patch("w_forecast.publication.subprocess.run", side_effect=transport):
            with self.assertRaises(subprocess.CalledProcessError):
                publish(self.stage, "ch1")
        self.assertIn("rm -rf --", calls[-1][-1])
        self.assertFalse(list(self.stage.glob(".publish-*")))
        verified(self.stage / "ch1", "ch1")

    def test_https_validation_checks_actual_range_cors_and_encoding_headers(self):
        pointer = self.candidate()
        manifest = json.loads((self.stage / "ch1" / pointer["manifest"]).read_bytes())
        asset = manifest["frames"][0]["artifact"]
        class Response(BytesIO):
            def __init__(self, body, status=200, headers=None):
                super().__init__(body)
                self.status, self.headers = status, headers or {}
        def responses(encoding=None):
            headers = {"Content-Range": f"bytes 0-15/{asset['bytes']}",
                       "Access-Control-Allow-Origin": "*",
                       "Access-Control-Expose-Headers": "Content-Range, Content-Length, Accept-Ranges"}
            if encoding:
                headers["Content-Encoding"] = encoding
            prefix = (self.stage / "ch1" / asset["path"]).read_bytes()[:16]
            return [Response(json.dumps(pointer).encode(), headers={"Cache-Control": "no-cache, must-revalidate"}),
                    Response((self.stage / "ch1" / pointer["manifest"]).read_bytes()),
                    Response(prefix, 206, headers), Response(prefix, 206, headers)]
        with patch("w_forecast.publication.urlopen", side_effect=responses()):
            verify_https("https://fixture/", "ch1", pointer)
        with patch("w_forecast.publication.urlopen", side_effect=responses("gzip")):
            with self.assertRaisesRegex(ValueError, "byte ranges, encoding or CORS"):
                verify_https("https://fixture/", "ch1", pointer)


if __name__ == "__main__":
    unittest.main()
