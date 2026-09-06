"""Execute the production publisher against local, credential-free transports."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy_data_infomaniak.sh"
TRANSPORT = r"""
import json, os, pathlib, shutil, subprocess, sys
command = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ["TEST_REMOTE"])
fault = os.environ["TEST_FAULT"]
with open(os.environ["TEST_CALLS"], "a") as log:
    log.write(json.dumps([command, args]) + "\n")
if command == "ssh":
    script = args[-1]
    if fault == "foreign-owner" and "ownership lost before commit" in script:
        (root / ".xcbenz_web_exports_publish.lock/owner").write_text("successor")
    sys.exit(subprocess.call(["/bin/sh", "-c", script]))
elif command == "rsync":
    if fault == "upload":
        sys.exit(23)
    source, target = args[-2:]
    source = source.removeprefix("test@fixture:")
    target = target.removeprefix("test@fixture:")
    if pathlib.Path(source).is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)
elif command == "mv":
    if fault == "promote" and args == ["remote/_upload_tmp_test/web_exports", "remote/web_exports"]:
        sys.exit(44)
    sys.exit(subprocess.call(["/usr/bin/mv", *args]))
else:
    raise AssertionError(command)
"""


class ForecastPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            dir=os.environ.get("TEST_TMPDIR", "/tmp")
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote"
        self.current = self.remote / "web_exports"
        self.current.mkdir(parents=True)
        (self.current / "old-forecast").write_text("old")
        self.subtrees = (
            "live_stations",
            "webcams",
            "radar_maps",
            "airspace",
            "fai_records",
            "satellite_cloud_maps",
        )
        for subtree in self.subtrees:
            (self.current / subtree).mkdir()
            (self.current / subtree / "marker").write_text(subtree)
        self.other = self.remote / "_upload_tmp_other"
        self.other.mkdir()
        (self.other / "marker").write_text("unrelated")
        self.candidate = self.root / "candidate"
        self.candidate.mkdir()
        (self.candidate / "manifest.json").write_text('{"new":true}')
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("ssh", "rsync", "mv"):
            executable = self.bin / name
            executable.write_text(f"#!{sys.executable}\n" + TRANSPORT)
            executable.chmod(0o755)
        for name in ("key", "known_hosts"):
            (self.root / name).write_text("test placeholder")
        self.calls = self.root / "calls.jsonl"

    def publish(self, fault=""):
        # A fresh environment cannot pick up production credentials or endpoints.
        env = {
            "PATH": f"{self.bin}:{os.defpath}",
            "INFOMANIAK_HOST": "fixture",
            "INFOMANIAK_USER": "test",
            # Deliberately relative: the fenced commit must not leak its cd.
            "INFOMANIAK_DATA_ROOT": "remote",
            "INFOMANIAK_SSH_KEY_PATH": str(self.root / "key"),
            "INFOMANIAK_KNOWN_HOSTS_PATH": str(self.root / "known_hosts"),
            "WEB_EXPORT_DIR": str(self.candidate),
            "RUNNER_TEMP": str(self.root),
            "PYTHON_BIN": sys.executable,
            "RELEASE_ID": "test",
            "LOCK_ID": "forecast-test",
            "DEPLOY_RETRIES": "2",
            "DEPLOY_RETRY_DELAY_SECONDS": "0",
            "DEPLOY_CAPACITY_PROBE_MAX_BYTES": "1",
            "DEPLOY_LOCK_HEARTBEAT_SECONDS": "1",
            "DEPLOY_LOCK_LEASE_SECONDS": "10",
            "DEPLOY_LOCK_RELEASE_TIMEOUT_SECONDS": "2",
            "DEPLOY_LOCK_WAIT_SECONDS": "1",
            "DEPLOY_LOCK_POLL_SECONDS": "1",
            "TEST_REMOTE": str(self.remote),
            "TEST_FAULT": fault,
            "TEST_CALLS": str(self.calls),
        }
        return subprocess.run(
            ["bash", str(SCRIPT)],
            cwd=self.root,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def assert_unrelated_candidate_preserved(self):
        self.assertEqual((self.other / "marker").read_text(), "unrelated")
        self.assertFalse((self.remote / "_upload_tmp_test").exists())

    def test_success_promotes_with_relative_root_and_preserves_every_live_subtree(self):
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            json.loads((self.current / "manifest.json").read_text()), {"new": True}
        )
        self.assertEqual(
            (self.remote / "_previous_web_exports/old-forecast").read_text(), "old"
        )
        for subtree in self.subtrees:
            self.assertEqual((self.current / subtree / "marker").read_text(), subtree)
        self.assertFalse((self.remote / ".xcbenz_web_exports_publish.lock").exists())
        self.assert_unrelated_candidate_preserved()

    def test_foreign_owner_blocks_commit_and_cleanup_preserves_successor_lease(self):
        result = self.publish("foreign-owner")
        self.assertEqual(result.returncode, 49, result.stdout + result.stderr)
        self.assertEqual((self.current / "old-forecast").read_text(), "old")
        self.assertFalse((self.current / "manifest.json").exists())
        self.assertEqual(
            (self.remote / ".xcbenz_web_exports_publish.lock/owner").read_text(),
            "successor",
        )
        self.assert_unrelated_candidate_preserved()

    def test_failed_promotion_restores_the_complete_previous_tree(self):
        result = self.publish("promote")
        self.assertEqual(result.returncode, 44, result.stdout + result.stderr)
        self.assertEqual((self.current / "old-forecast").read_text(), "old")
        self.assertFalse((self.current / "manifest.json").exists())
        for subtree in self.subtrees:
            self.assertEqual((self.current / subtree / "marker").read_text(), subtree)
        self.assertFalse((self.remote / ".xcbenz_web_exports_publish.lock").exists())
        self.assert_unrelated_candidate_preserved()

    def test_failed_upload_retries_and_returns_original_status(self):
        result = self.publish("upload")
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(sum(command == "rsync" for command, _ in calls), 2)
        self.assertEqual((self.current / "old-forecast").read_text(), "old")
        self.assert_unrelated_candidate_preserved()


if __name__ == "__main__":
    unittest.main()
