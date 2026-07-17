import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_coding_server_pipeline as runner  # noqa: E402


def _split_jobs(run_tag: str):
    args = argparse.Namespace(
        job_layout="split",
        ch1_chunk_size=0,
        ch2_chunk_size=0,
        combined_job_order="model",
    )
    return runner.build_jobs(
        args=args,
        base={},
        run_dir=Path("test-run"),
        py=["python"],
        latest_ch1=run_tag,
        latest_ch2="20260716_1200",
    )


class RunCodingServerPipelineTests(unittest.TestCase):
    def test_non_03z_split_profile_jobs_end_at_h033(self):
        names = {job.name for job in _split_jobs("20260716_1500")}

        self.assertIn("ch1-profile-H000_H016", names)
        self.assertIn("ch1-profile-H017_H033", names)
        self.assertNotIn("ch1-profile-H017_H045", names)
        self.assertNotIn("ch1-profile-H034_H045", names)

    def test_03z_split_profile_jobs_cover_extended_horizon(self):
        names = {job.name for job in _split_jobs("20260716_0300")}

        self.assertIn("ch1-profile-H000_H016", names)
        self.assertIn("ch1-profile-H017_H033", names)
        self.assertIn("ch1-profile-H034_H045", names)
        self.assertNotIn("ch1-profile-H017_H045", names)

    def test_resource_monitor_records_filesystem_disk_high_water(self):
        monitor = runner.ResourceMonitor(
            sample_seconds=15,
            max_cpu_percent=88,
            max_load_percent=110,
            min_available_mb=4096,
        )
        mb = 1024 * 1024
        disk_samples = [
            mock.Mock(used=100 * mb, total=1000 * mb),
            mock.Mock(used=145 * mb, total=1000 * mb),
            mock.Mock(used=130 * mb, total=1000 * mb),
        ]
        with mock.patch.object(monitor, "_read_cpu_percent", return_value=None), mock.patch.object(
            monitor, "_read_load1", return_value=None
        ), mock.patch.object(monitor, "_read_memory_mb", return_value=(None, None)), mock.patch.object(
            runner.shutil, "disk_usage", side_effect=disk_samples
        ):
            monitor.sample_once(log_sample=False)
            monitor.sample_once(log_sample=False)
            monitor.sample_once(log_sample=False)

        self.assertEqual(monitor.disk_high_water(), (100.0, 145.0, 45.0))

    def test_manual_run_tags_bypass_network_preflight_with_structured_outputs(self):
        args = argparse.Namespace(
            ch1_run_tag="20260716_1500",
            ch2_run_tag="20260716_1200",
        )
        with mock.patch.object(runner, "run_checked") as run_checked:
            outputs = runner.run_preflight(args, {}, Path("logs"), ["python"])
        run_checked.assert_not_called()
        self.assertEqual(outputs["reason"], "manual_run_tags")
        self.assertEqual(outputs["latest_ch1"], "20260716_1500")
        self.assertEqual(outputs["latest_ch2"], "20260716_1200")

    def test_already_complete_preflight_stops_before_fetch_or_publish(self):
        with tempfile.TemporaryDirectory(prefix="xcb_runner_") as tmp:
            args = argparse.Namespace(
                python_cmd="python",
                run_mode="standard",
                skip_deploy=False,
                push_data_branch=False,
                run_dir=tmp,
                repository="sebosimo/XCBenz_Data_Parallel",
                data_branch="data-web",
                data_host_base_url="https://data.example/",
                web_export_data_root="",
                ch1_run_tag=None,
                ch2_run_tag=None,
                plan_only=False,
                download_workers=4,
                prefetch_next_horizon=False,
                release_profile_only_fields=False,
            )
            with mock.patch.object(
                runner,
                "run_preflight",
                return_value={"should_run": "false", "reason": "latest_runs_already_published"},
            ), mock.patch.object(runner, "build_jobs") as build_jobs, mock.patch.object(
                runner,
                "run_parallel_jobs",
            ) as run_jobs:
                rc = runner.run_pipeline(args)
            self.assertEqual(rc, 0)
            build_jobs.assert_not_called()
            run_jobs.assert_not_called()


if __name__ == "__main__":
    unittest.main()
