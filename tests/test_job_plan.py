import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pipeline_orchestration.job_plan import build_job_plan


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "job_plan" / "cases.json"


class JobPlanTests(unittest.TestCase):
    def test_representative_plan_fixtures(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(fixture["fixture_schema_version"], 1)
        for case in fixture["cases"]:
            with self.subTest(case=case["name"]):
                plan = build_job_plan(case["ch1_run_tag"], case["ch2_run_tag"])
                expected = case["expected"]
                self.assertEqual(plan.ch1.expected_horizon_count, expected["ch1_horizon_count"])
                self.assertEqual(
                    [chunk.id for chunk in plan.ch1.github.map_chunks],
                    expected["ch1_github_map_chunks"],
                )
                self.assertEqual(
                    [chunk.id for chunk in plan.ch1.github.profile_chunks],
                    expected["ch1_profile_chunks"],
                )
                self.assertEqual(plan.ch2.expected_horizon_count, expected["ch2_horizon_count"])
                self.assertEqual(
                    [chunk.id for chunk in plan.ch2.github.map_chunks],
                    expected["ch2_map_chunks"],
                )
                self.assertEqual(
                    plan.ch1.github.map_chunks[0].roots()["wind_root"],
                    expected["github_ch1_wind_root"],
                )
                self.assertEqual(
                    plan.ch2.github.map_chunks[0].roots()["wind_root"],
                    expected["github_ch2_wind_root"],
                )
                self.assertEqual(
                    plan.ch2.coding_server.map_chunks[0].roots()["wind_root"],
                    expected["coding_server_ch2_wind_root"],
                )

    def test_03z_and_non_03z_profile_contracts_and_completeness(self):
        regular = build_job_plan("20260716_1500", "20260716_1200")
        extended = build_job_plan("20260716_0300", "20260716_1200")

        self.assertEqual(
            [chunk.id for chunk in regular.ch1.github.profile_chunks],
            ["H000_H016", "H017_H033"],
        )
        self.assertEqual(
            [chunk.id for chunk in extended.ch1.coding_server.profile_chunks],
            ["H000_H016", "H017_H033", "H034_H045"],
        )
        self.assertEqual(regular.ch1.expected_horizon_count, 34)
        self.assertEqual(extended.ch1.expected_horizon_count, 46)
        self.assertEqual(regular.ch2.expected_horizon_count, 121)

    def test_github_and_coding_server_use_explicit_compatible_cache_roots(self):
        plan = build_job_plan("20260716_1500", "20260716_1200")
        github_ch1 = plan.ch1.github.map_chunks[0].roots()
        github_ch2 = plan.ch2.github.map_chunks[0].roots()
        local_ch2 = plan.ch2.coding_server.map_chunks[0].roots()

        self.assertEqual(github_ch1["wind_root"], "cache_wind_maps")
        self.assertEqual(
            github_ch2["wind_root"],
            "map_chunk_outputs/H000_H030/cache_wind_packed",
        )
        self.assertEqual(
            local_ch2["wind_root"],
            "map_chunks/ch2/H000_H030/cache_wind_maps",
        )

    def test_github_matrices_and_plan_json_are_deterministic(self):
        first = build_job_plan("20260716_0300", "20260716_1200")
        second = build_job_plan("20260716_0300", "20260716_1200")
        self.assertEqual(first.to_json(), second.to_json())
        outputs = first.github_outputs()
        self.assertEqual(
            [chunk["id"] for chunk in json.loads(outputs["ch2_map_matrix"])["chunk"]],
            ["H000_H030", "H031_H060", "H061_H090", "H091_H120"],
        )

    def test_cli_serializes_outputs_without_network_or_runtime_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "github_output"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pipeline_orchestration.job_plan",
                    "--ch1-run-tag",
                    "20260716_1500",
                    "--ch2-run-tag",
                    "20260716_1200",
                    "--github-output",
                    str(output_path),
                ],
                cwd=REPO_ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            contents = output_path.read_text(encoding="utf-8")
            self.assertIn("ch1_map_matrix=", contents)
            self.assertIn("ch2_profile_matrix=", contents)
            self.assertFalse((Path(temporary) / ".local_pipeline").exists())

    def test_invalid_run_tag_fails_before_a_plan_is_created(self):
        with self.assertRaisesRegex(ValueError, "expected YYYYMMDD_HHMM"):
            build_job_plan("latest", "20260716_1200")


if __name__ == "__main__":
    unittest.main()
