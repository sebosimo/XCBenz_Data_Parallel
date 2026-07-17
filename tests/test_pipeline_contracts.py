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
import preflight_runs  # noqa: E402
import run_coding_server_pipeline as runner  # noqa: E402
from generate_web_exports import expected_profile_chunks  # noqa: E402


WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "daily_plot.yml"


def matrix_ids(run_tag: str) -> set[str]:
    return {item["id"] for item in preflight_runs.ch1_profile_matrix(run_tag)["chunk"]}


class PipelineContractTests(unittest.TestCase):
    def test_ch1_profile_plans_match_for_regular_and_03z_runs(self):
        for run_tag in ("20260716_1500", "20260716_0300"):
            workflow_ids = matrix_ids(run_tag)
            runner_ids = {runner.chunk_id(start, end) for start, end in runner.ch1_profile_chunks(run_tag)}
            export_ids = expected_profile_chunks("icon-ch1", run_tag)
            self.assertEqual(workflow_ids, runner_ids)
            self.assertEqual(workflow_ids, export_ids)

    def test_workflow_consumes_the_tested_profile_matrix_and_canonical_ch1_wind_root(self):
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("ch1_profile_matrix: ${{ steps.preflight.outputs.ch1_profile_matrix }}", workflow)
        self.assertIn("matrix: ${{ fromJSON(needs.preflight.outputs.ch1_profile_matrix) }}", workflow)
        self.assertNotIn("cache_wind_packed/ch1", workflow)
        self.assertIn("cache_wind_maps/ch1/", workflow)
        self.assertIn(
            "CH2_WIND_MAP_OUT_ROOT: map_chunk_outputs/${{ matrix.chunk.id }}/cache_wind_packed",
            workflow,
        )

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
        source = (REPO_ROOT / "fetch_data_ch2.py").read_text(encoding="utf-8")
        self.assertIn('temp_dir = os.getenv("XCBENZ_FETCH_TMP_DIR")', source)
        self.assertIn("os.path.join(temp_dir,", source)

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


if __name__ == "__main__":
    unittest.main()
