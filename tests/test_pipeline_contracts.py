import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import merge_map_chunks  # noqa: E402
from generate_web_exports import expected_profile_chunks  # noqa: E402
from pipeline_orchestration.job_plan import build_job_plan  # noqa: E402


WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "daily_plot.yml"
REQUIREMENTS_PATH = REPO_ROOT / "requirements.txt"


def assert_workflow_wiring(workflow: str) -> None:
    required = (
        "python -m pipeline_orchestration.job_plan",
        "ch1_map_matrix: ${{ steps.plan.outputs.ch1_map_matrix }}",
        "ch1_profile_matrix: ${{ steps.plan.outputs.ch1_profile_matrix }}",
        "ch2_map_matrix: ${{ steps.plan.outputs.ch2_map_matrix }}",
        "ch2_profile_matrix: ${{ steps.plan.outputs.ch2_profile_matrix }}",
        "matrix: ${{ fromJSON(needs.plan.outputs.ch1_map_matrix) }}",
        "matrix: ${{ fromJSON(needs.plan.outputs.ch1_profile_matrix) }}",
        "matrix: ${{ fromJSON(needs.plan.outputs.ch2_map_matrix) }}",
        "matrix: ${{ fromJSON(needs.plan.outputs.ch2_profile_matrix) }}",
    )
    for token in required:
        if token not in workflow:
            raise AssertionError(f"workflow is not wired to the shared job plan: {token}")


class PipelineContractTests(unittest.TestCase):
    def test_grib_geometry_dependencies_are_pinned_to_production_versions(self):
        requirements = set(REQUIREMENTS_PATH.read_text(encoding="utf-8").splitlines())
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        quality = (REPO_ROOT / ".github" / "workflows" / "quality.yml").read_text(encoding="utf-8")
        self.assertIn("xarray==2026.4.0", requirements)
        self.assertIn("cfgrib==0.9.15.1", requirements)
        self.assertIn("eccodes==2.39.2", requirements)
        self.assertIn("eccodes-cosmo-resources-python==2.38.3.1", requirements)
        self.assertNotIn("libeccodes-dev", workflow)
        self.assertNotIn("libeccodes-dev", quality)
        self.assertEqual(workflow.count("uv run python -m eccodes selfcheck"), 5)
        self.assertIn("uv run python -m eccodes selfcheck", quality)

    def test_ch1_profile_plans_match_for_regular_and_03z_runs(self):
        for run_tag in ("20260716_1500", "20260716_0300"):
            plan = build_job_plan(run_tag, "20260716_1200")
            workflow_ids = {chunk.id for chunk in plan.ch1.github.profile_chunks}
            runner_ids = {chunk.id for chunk in plan.ch1.coding_server.profile_chunks}
            export_ids = expected_profile_chunks("icon-ch1", run_tag)
            self.assertEqual(workflow_ids, runner_ids)
            self.assertEqual(workflow_ids, export_ids)

    def test_ch2_profile_plans_match_runner_workflow_and_export_completeness(self):
        plan = build_job_plan("20260716_1500", "20260716_1200")
        workflow_ids = {chunk.id for chunk in plan.ch2.github.profile_chunks}
        runner_ids = {chunk.id for chunk in plan.ch2.coding_server.profile_chunks}
        self.assertEqual(workflow_ids, runner_ids)
        self.assertEqual(workflow_ids, expected_profile_chunks("icon-ch2", "20260716_1200"))

    def test_workflow_consumes_all_shared_matrices_and_planned_cache_roots(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        assert_workflow_wiring(workflow)
        self.assertNotIn("cache_wind_packed/ch1", workflow)
        self.assertIn("cache_wind_maps/ch1/", workflow)
        self.assertIn("CH1_WIND_MAP_OUT_ROOT: ${{ matrix.chunk.wind_root }}", workflow)
        self.assertIn("CH2_WIND_MAP_OUT_ROOT: ${{ matrix.chunk.wind_root }}", workflow)
        self.assertNotIn("- id: H000_H030", workflow)

    def test_workflow_wiring_mutation_is_rejected(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        mutated = workflow.replace(
            "needs.plan.outputs.ch2_map_matrix",
            "needs.plan.outputs.ch2_profile_matrix",
        )
        with self.assertRaisesRegex(AssertionError, "ch2_map_matrix"):
            assert_workflow_wiring(mutated)

    def test_ch2_wind_staging_merges_into_the_canonical_root(self):
        with tempfile.TemporaryDirectory(dir=os.getenv("TEST_TMPDIR", "/tmp")) as temp_dir:
            workspace = Path(temp_dir)
            step_source = (
                workspace
                / "map_chunks/ch2/ch2-map-H000_H030/map_chunk_outputs/H000_H030"
                / "cache_wind_packed/ch2/20260716_1200/10m_AGL/steps/H000.bin"
            )
            step_source.parent.mkdir(parents=True)
            step_source.write_bytes(b"wind")
            metadata_path = step_source.parent.parent / "metadata.json"
            metadata_path.write_text(
                json.dumps(
                    {
                        "model": "ch2",
                        "run": "20260716_1200",
                        "product_name": "10m_AGL",
                        "steps": [
                            {
                                "horizon": 0,
                                "step": "H000",
                                "path": str(step_source),
                                "byte_length": 4,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            previous_cwd = Path.cwd()
            try:
                os.chdir(workspace)
                merge_map_chunks.merge_direct_wind_chunks()
            finally:
                os.chdir(previous_cwd)

            output_step = workspace / "cache_wind_maps/ch2/20260716_1200/10m_AGL/steps/H000.bin"
            output_metadata = output_step.parent.parent / "metadata.json"
            self.assertEqual(output_step.read_bytes(), b"wind")
            self.assertEqual(json.loads(output_metadata.read_text(encoding="utf-8"))["steps"][0]["byte_length"], 4)

    def test_ch2_fetcher_honors_the_isolated_download_root(self):
        wrapper = (REPO_ROOT / "fetch_data_ch2.py").read_text(encoding="utf-8")
        shared_stac = (REPO_ROOT / "forecast_fetch/stac.py").read_text(encoding="utf-8")
        self.assertIn('temporary_root=os.getenv("XCBENZ_FETCH_TMP_DIR")', wrapper)
        self.assertIn("temp_dir = str(temporary_root) if temporary_root else None", shared_stac)
        self.assertIn("os.path.join(temp_dir,", shared_stac)

    def test_publish_python_steps_use_the_installed_uv_environment(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        scripts = (
            "scripts/apply_retention.py",
            "scripts/apply_web_retention.py",
            "scripts/validate_outputs.py",
            "scripts/validate_remote_web_exports.py",
        )
        for script in scripts:
            self.assertIn(f"run: uv run python {script}", workflow)
            self.assertNotIn(f"run: python {script}", workflow)

    def test_ephemeral_data_branch_publish_has_no_invalid_main_cleanup(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn('git commit --quiet -m "Web export snapshot:', workflow)
        self.assertIn('git push --quiet origin HEAD:"$DATA_BRANCH" --force', workflow)
        self.assertNotIn("git checkout -f main", workflow)
        self.assertNotIn("git branch -D data-temp", workflow)


if __name__ == "__main__":
    unittest.main()
