import argparse
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import poll_coding_server_pipeline as poller  # noqa: E402


def _temp_workspace():
    return tempfile.mkdtemp(prefix="xcb_poller_", dir=os.getenv("TEST_TMPDIR", r"C:\tmp"))


def _args(state_file, **overrides):
    values = {
        "python_cmd": "python",
        "run_mode": "standard-deploy-data-host",
        "state_file": state_file,
        "lock_file": "unused.lock",
        "probe_timeout": 1,
        "retry_minutes": 10,
        "force_run": False,
        "plan_only": False,
        "skip_deploy": False,
        "no_push_data_branch": False,
        "no_restore_web_exports": True,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


class PollCodingServerPipelineTests(unittest.TestCase):
    def test_build_pipeline_command_defaults_to_fresh_direct_outputs(self):
        args = _args("state.json", python_cmd="/venv/bin/python", no_push_data_branch=True)

        command = poller.build_pipeline_command(args, "20260628_2100", "20260628_1800")

        self.assertEqual(command[:2], ["/venv/bin/python", "scripts/run_coding_server_pipeline.py"])
        self.assertIn("--ch1-run-tag", command)
        self.assertIn("20260628_2100", command)
        self.assertIn("--ch2-run-tag", command)
        self.assertIn("20260628_1800", command)
        self.assertIn("--no-push-data-branch", command)
        self.assertIn("--no-restore-web-exports", command)

    def test_retry_backoff_blocks_recent_failed_pair(self):
        now = dt.datetime(2026, 6, 29, 12, 0, tzinfo=dt.timezone.utc)
        state = {
            "last_attempt": {
                "pair": "ch1=20260628_2100;ch2=20260628_1800",
                "status": "failed",
                "finished_at": "2026-06-29T11:45:00Z",
            }
        }

        blocked = poller.retry_backoff_active(
            state,
            "ch1=20260628_2100;ch2=20260628_1800",
            retry_minutes=30,
            now=now,
        )

        self.assertTrue(blocked)

    def test_poller_skips_pair_that_already_succeeded(self):
        tmp = _temp_workspace()
        try:
            state_path = Path(tmp) / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "last_success": {
                            "pair": "ch1=20260628_2100;ch2=20260628_1800",
                            "runs": {"ch1": "20260628_2100", "ch2": "20260628_1800"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            args = _args(str(state_path))

            with mock.patch.object(
                poller,
                "latest_complete_run",
                side_effect=[("20260628_2100", []), ("20260628_1800", [])],
            ), mock.patch.object(poller.subprocess, "run") as run_mock:
                rc = poller.run_poller(args)

            self.assertEqual(rc, 0)
            run_mock.assert_not_called()
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["last_poll"]["decision"], "already_successful")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_poller_marks_success_only_after_runner_succeeds(self):
        tmp = _temp_workspace()
        try:
            state_path = Path(tmp) / "state.json"
            args = _args(str(state_path), skip_deploy=True, no_push_data_branch=True)
            completed = subprocess_completed(returncode=0)

            with mock.patch.object(
                poller,
                "latest_complete_run",
                side_effect=[("20260628_2100", []), ("20260628_1800", [])],
            ), mock.patch.object(poller.subprocess, "run", return_value=completed):
                rc = poller.run_poller(args)

            self.assertEqual(rc, 0)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["last_attempt"]["status"], "succeeded")
            self.assertEqual(state["last_success"]["runs"]["ch1"], "20260628_2100")
            self.assertFalse(state["last_success"]["published"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def subprocess_completed(returncode):
    return type("Completed", (), {"returncode": returncode})()


if __name__ == "__main__":
    unittest.main()
