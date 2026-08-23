import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from web_profiles import (
    EMAGRAM_BUNDLE_VARIABLES,
    build_bundle_step_values,
    expected_byte_length,
    merge_profile_chunks,
    write_profile_chunk,
)


def _temp_workspace():
    return Path(tempfile.mkdtemp(prefix="xcb_profiles_", dir=os.getenv("TEST_TMPDIR", r"C:\tmp")))


class WebProfileTests(unittest.TestCase):
    def test_build_bundle_step_values_stores_only_lossless_base_variables(self):
        values = build_bundle_step_values(
            p=np.asarray([90000.0, 80000.0], dtype=np.float32),
            t=np.asarray([283.15, 273.15], dtype=np.float32),
            qv=np.asarray([0.006, 0.004], dtype=np.float32),
            u=np.asarray([3.0, 0.0], dtype=np.float32),
            v=np.asarray([4.0, -2.0], dtype=np.float32),
            level_count=2,
        )

        self.assertEqual(values.shape, (len(EMAGRAM_BUNDLE_VARIABLES), 2))
        self.assertEqual(EMAGRAM_BUNDLE_VARIABLES, ("p", "t", "qv", "u", "v"))
        self.assertAlmostEqual(values[EMAGRAM_BUNDLE_VARIABLES.index("t"), 0], 283.15, places=4)
        self.assertAlmostEqual(values[EMAGRAM_BUNDLE_VARIABLES.index("p"), 1], 80000.0, places=4)

    def test_chunk_merge_writes_stable_bundle_contract(self):
        tmp = _temp_workspace()
        try:
            chunks = tmp / "chunks"
            out = tmp / "web_exports" / "emagrams" / "icon-ch2" / "20260520_0600" / "oberwallis"
            location = {"display_name": "Oberwallis", "lat": 46.3, "lon": 8.0, "type": "region"}

            for chunk_id, step, horizon, temp_c in [
                ("H031_H031", "H031", 31, 6.0),
                ("H000_H000", "H000", 0, 10.0),
            ]:
                values = build_bundle_step_values(
                    p=np.asarray([90000.0, 80000.0], dtype=np.float32),
                    t=np.asarray([temp_c + 273.15, temp_c + 270.15], dtype=np.float32),
                    qv=np.asarray([0.006, 0.004], dtype=np.float32),
                    u=np.asarray([1.0, 2.0], dtype=np.float32),
                    v=np.asarray([0.0, 0.0], dtype=np.float32),
                    level_count=2,
                )[None, :, :]
                write_profile_chunk(
                    output_root=chunks,
                    model_key="icon-ch2",
                    run_tag="20260520_0600",
                    chunk_id=chunk_id,
                    location_id="oberwallis",
                    location_meta=location,
                    ref_time="2026-05-20T06:00:00+00:00",
                    height_values=np.asarray([1000.0, 2000.0], dtype=np.float32),
                    steps=[{"step": step, "horizon": horizon, "valid_time": f"valid-{step}", "surface": None}],
                    values=values,
                )

            exports = merge_profile_chunks(
                sorted(chunks.glob("icon-ch2/20260520_0600/*/oberwallis/chunk.json")),
                output_dir=out,
                model_key="icon-ch2",
                run_tag="20260520_0600",
                location_id="oberwallis",
                location_meta=location,
                url_for=lambda path: path.relative_to(tmp).as_posix(),
                write_json=lambda path, payload: path.write_text(json.dumps(payload), encoding="utf-8"),
            )

            bundle = json.loads((out / "bundle.json").read_text(encoding="utf-8"))
            self.assertEqual(bundle["schema_version"], 2)
            self.assertEqual([step["step"] for step in bundle["steps"]], ["H000", "H031"])
            self.assertEqual(bundle["encoding"]["dtype"], "float32")
            self.assertEqual(bundle["encoding"]["variables"], list(EMAGRAM_BUNDLE_VARIABLES))
            self.assertEqual((out / "profiles.bin").stat().st_size, expected_byte_length(2, 2))
            self.assertEqual([item["bundle_step_index"] for item in exports], [0, 1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
