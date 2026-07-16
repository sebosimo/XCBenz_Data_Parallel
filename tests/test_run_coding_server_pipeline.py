import argparse
import sys
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
