import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WeatherServerDeployTests(unittest.TestCase):
    def test_image_uses_pinned_requirements_and_wheel_eccodes_selfcheck(self):
        dockerfile = (
            ROOT / "deploy" / "weather-server" / "Dockerfile"
        ).read_text(encoding="utf-8")
        self.assertIn("python:3.12-slim-bookworm", dockerfile)
        self.assertIn("-r /app/requirements.txt", dockerfile)
        self.assertIn("python -m eccodes selfcheck", dockerfile)
        self.assertNotIn("libeccodes", dockerfile)

    def test_wrapper_uses_shared_lock_unless_coordinator_holds_it(self):
        wrapper = (
            ROOT / "deploy" / "weather-server" / "run_forecast.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("XCBENZ_HEAVY_LOCK_HELD", wrapper)
        self.assertIn("export XCBENZ_PYTHON_CMD=python", wrapper)
        self.assertIn("export PYTHON_BIN=python", wrapper)
        self.assertIn("/run/lock/xcbenz-heavy.lock", wrapper)
        self.assertIn("flock -x", wrapper)
        self.assertIn("run_coding_server_pipeline.py", wrapper)

    def test_example_uses_measured_four_job_configuration(self):
        env_example = (
            ROOT / "deploy" / "weather-server" / "forecast.env.example"
        ).read_text(encoding="utf-8")
        expected_settings = (
            "XCBENZ_LOCAL_MAX_JOBS=4",
            "DOWNLOAD_WORKERS=8",
            "XCBENZ_JOB_LAYOUT=combined",
            "XCBENZ_COMBINED_JOB_ORDER=interleave",
            "XCBENZ_CH2_CHUNK_SIZE=15",
            "XCBENZ_PREFETCH_NEXT_HORIZON=true",
            "XCBENZ_RELEASE_PROFILE_ONLY_FIELDS=true",
            "PYTHON_BIN=python",
        )
        for setting in expected_settings:
            with self.subTest(setting=setting):
                self.assertIn(setting, env_example)

    def test_deploy_fails_early_when_python_executable_is_missing(self):
        deploy_script = (
            ROOT / "scripts" / "deploy_data_infomaniak.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('command -v "$PYTHON_BIN"', deploy_script)
        self.assertIn("Python executable not found", deploy_script)

    def test_deploy_script_uses_compatible_fenced_publish_lease(self):
        deploy_path = ROOT / "scripts" / "deploy_data_infomaniak.sh"
        deploy_script = deploy_path.read_text(encoding="utf-8")

        subprocess.run(["bash", "-n", str(deploy_path)], check=True)
        for contract_field in (
            "protocol_version",
            "publisher",
            "host",
            "pid",
            "acquired_at",
            "acquired_at_epoch",
            "lease_seconds",
            "heartbeat_at",
        ):
            with self.subTest(contract_field=contract_field):
                self.assertIn(contract_field, deploy_script)
        self.assertIn("DEPLOY_LOCK_PROTOCOL_VERSION=1", deploy_script)
        self.assertIn("Quarantined expired publish lease", deploy_script)
        self.assertIn("REMOTE_LOCK_GUARD", deploy_script)
        self.assertIn("flock -w", deploy_script)
        self.assertIn("current_protocol", deploy_script)
        self.assertIn("current_heartbeat", deploy_script)
        self.assertIn("assert_remote_lease", deploy_script)
        self.assertIn("rollback", deploy_script.lower())
        self.assertIn("maintain_remote_candidates", deploy_script)
        self.assertIn("DEPLOY_CANDIDATE_QUARANTINE_AFTER_SECONDS", deploy_script)
        self.assertIn("DEPLOY_CANDIDATE_DELETE_AFTER_SECONDS", deploy_script)
        self.assertIn(".xcbenz_upload_candidate.quarantine", deploy_script)
        self.assertIn(
            'DEPLOY_CANDIDATE_QUARANTINE_AFTER_SECONDS:-21600', deploy_script
        )
        self.assertIn(
            'DEPLOY_CANDIDATE_DELETE_AFTER_SECONDS:-324000', deploy_script
        )
        self.assertNotIn("DEPLOY_LOCK_STALE_SECONDS", deploy_script)
        self.assertNotIn("Removing stale publish lock", deploy_script)

    def test_failed_deploy_removes_only_its_exact_upload_candidate(self):
        deploy_script = (
            ROOT / "scripts" / "deploy_data_infomaniak.sh"
        ).read_text(encoding="utf-8")

        cleanup_start = deploy_script.index("cleanup_remote_upload_candidate()")
        cleanup_end = deploy_script.index("\ncleanup()", cleanup_start)
        candidate_cleanup = deploy_script[cleanup_start:cleanup_end]
        self.assertIn("'$REMOTE_ROOT'/_upload_tmp_*", candidate_cleanup)
        self.assertIn("rm -rf -- '$REMOTE_TMP'", candidate_cleanup)
        self.assertIn(
            "Refusing to remove unexpected remote upload path", candidate_cleanup
        )

        trap_cleanup_start = deploy_script.index("cleanup()", cleanup_end)
        trap_cleanup_end = deploy_script.index("\ntrap cleanup EXIT", trap_cleanup_start)
        trap_cleanup = deploy_script[trap_cleanup_start:trap_cleanup_end]
        self.assertIn("if (( exit_code != 0 )); then", trap_cleanup)
        self.assertIn("cleanup_remote_upload_candidate", trap_cleanup)

    def test_release_is_retried_owner_checked_and_time_bounded(self):
        deploy_script = (
            ROOT / "scripts" / "deploy_data_infomaniak.sh"
        ).read_text(encoding="utf-8")

        release_start = deploy_script.index("release_remote_lock()")
        release_end = deploy_script.index("\ncleanup()", release_start)
        release = deploy_script[release_start:release_end]
        self.assertIn('retry "release remote publish lease"', release)
        self.assertIn("DEPLOY_LOCK_RELEASE_TIMEOUT_SECONDS", release)
        self.assertIn("actual_owner", release)
        self.assertIn("'$LOCK_ID'", release)

    def test_commit_lease_check_preserves_relative_remote_root(self):
        deploy_script = (
            ROOT / "scripts" / "deploy_data_infomaniak.sh"
        ).read_text(encoding="utf-8")

        switch_start = deploy_script.index('retry "switch remote web_exports directory"')
        switch_end = deploy_script.index("\nassert_remote_lease", switch_start)
        remote_switch = deploy_script[switch_start:switch_end]

        lease_check_start = remote_switch.index("    (\n      cd '$REMOTE_LOCK'")
        lease_check_end = remote_switch.index("\n    )\n    flock -u 9")
        remote_mutation = remote_switch.index("mkdir -p '$REMOTE_ROOT'")
        self.assertLess(lease_check_start, lease_check_end)
        self.assertLess(lease_check_end, remote_mutation)

    def test_manifest_download_does_not_require_chown_capability(self):
        deploy_script = (
            ROOT / "scripts" / "deploy_data_infomaniak.sh"
        ).read_text(encoding="utf-8")

        freshness_start = deploy_script.index("check_publish_freshness()")
        freshness_end = deploy_script.index("\nrelease_remote_lock()", freshness_start)
        freshness_check = deploy_script[freshness_start:freshness_end]
        self.assertIn("--no-owner --no-group", freshness_check)

    def test_retry_preserves_the_failed_command_status(self):
        deploy_script = (
            ROOT / "scripts" / "deploy_data_infomaniak.sh"
        ).read_text(encoding="utf-8")

        retry_start = deploy_script.index("retry()")
        retry_end = deploy_script.index("\nrequire_env()", retry_start)
        retry_function = deploy_script[retry_start:retry_end]
        self.assertIn("else\n      rc=$?", retry_function)

    def test_container_context_excludes_runtime_data_and_secrets(self):
        ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for entry in (
            ".git",
            ".local_pipeline",
            "cache_*",
            "web_exports",
            "data",
        ):
            self.assertIn(entry, ignore)


if __name__ == "__main__":
    unittest.main()
