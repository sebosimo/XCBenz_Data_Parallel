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
