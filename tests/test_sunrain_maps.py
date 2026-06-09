import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sunrain_maps import (
    SUNRAIN_RESERVED_VALUES,
    SunRainMapAccumulator,
    encode_rain_amount_codes,
    encode_sunshine_percent_codes,
    is_sunrain_maps_enabled,
    is_sunrain_run_complete,
)


def _temp_workspace():
    return tempfile.mkdtemp(prefix="xcb_sunrain_", dir=os.getenv("TEST_TMPDIR", r"C:\tmp"))


def _sample_grid():
    return object()


def _config():
    return SimpleNamespace(
        crop={"lon_min": 0.0, "lon_max": 1.0, "lat_min": 0.0, "lat_max": 1.0},
        grid_spacing_deg=1.0,
        source_padding_deg=0.0,
        horizon_stride=1,
    )


class SunRainMapTests(unittest.TestCase):
    def test_flags_are_on_by_default_and_model_scoped(self):
        self.assertTrue(is_sunrain_maps_enabled("ch1", env={}))
        self.assertFalse(is_sunrain_maps_enabled("ch1", env={"ENABLE_SUNRAIN_MAPS": "false"}))
        self.assertFalse(
            is_sunrain_maps_enabled(
                "ch1",
                env={"ENABLE_SUNRAIN_MAPS": "true", "ENABLE_SUNRAIN_MAPS_CH1": "false"},
            )
        )

    def test_encoders_keep_missing_and_reserved_ranges_clean(self):
        sunshine = encode_sunshine_percent_codes(np.asarray([np.nan, 0.0, 1.2, 99.6, 120.0], dtype=np.float32))
        self.assertEqual(sunshine.tolist(), [0, 1, 1, 100, 100])

        rain, clipped = encode_rain_amount_codes(
            np.asarray([np.nan, 0.2, 5.0, 5.5, 30.0, 31.0, 80.0, 90.0], dtype=np.float32)
        )
        self.assertEqual(rain.tolist(), [0, 102, 150, 151, 200, 201, 250, 250])
        self.assertEqual(clipped, 1)
        self.assertFalse(any(int(value) in SUNRAIN_RESERVED_VALUES for value in rain))

    def test_accumulator_writes_semantic_step_binary(self):
        tmp = _temp_workspace()
        try:
            ref = datetime.datetime(2026, 6, 9, 3, tzinfo=datetime.timezone.utc)
            acc = SunRainMapAccumulator("ch1", "20260609_0300", ref, _config(), log=lambda *_: None, out_root=tmp)
            acc.seed_previous_raw(np.asarray([0.0, 0.0, 0.0, 0.0], dtype=np.float32))
            acc.target_lat = np.asarray([[0.0, 0.0], [1.0, 1.0]], dtype=np.float32)
            acc.target_lon = np.asarray([[0.0, 1.0], [0.0, 1.0]], dtype=np.float32)
            acc.source_indices = np.arange(4)
            acc.weights = SimpleNamespace(apply=lambda values: np.asarray(values, dtype=np.float32).reshape(2, 2))
            acc.prepared = True

            self.assertTrue(
                acc.append(
                    _sample_grid(),
                    {
                        "DURSUN": np.asarray([0.0, 1800.0, 3600.0, np.nan], dtype=np.float32),
                        "DURSUN_M": np.asarray([3600.0, 3600.0, 3600.0, 3600.0], dtype=np.float32),
                    },
                    {"TOT_PREC": np.asarray([0.0, 0.2, 90.0, np.nan], dtype=np.float32)},
                    1,
                    ref,
                )
            )
            result = acc.finalize()
            metadata_path = os.path.join(tmp, "ch1", "20260609_0300", "surface", "metadata.json")
            step_path = os.path.join(tmp, "ch1", "20260609_0300", "surface", "steps", "H01.bin")

            self.assertEqual(result["files"], 2)
            self.assertTrue(os.path.exists(metadata_path))
            self.assertTrue(os.path.exists(step_path))

            values = np.fromfile(step_path, dtype="u1")
            self.assertEqual(values.size, 4)
            self.assertEqual(values.tolist(), [1, 102, 250, 0])
            self.assertFalse(np.any(values >= 251))

            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            self.assertEqual(metadata["product"], "sunrain_map_surface")
            self.assertEqual(metadata["encoding"]["format"], "uint8-semantic-sunrain-code")
            self.assertEqual(metadata["encoding"]["components"], ["sunrain_code"])
            self.assertEqual(metadata["encoding"]["missing_value"], 0)
            self.assertEqual(metadata["encoding"]["reserved_values"], SUNRAIN_RESERVED_VALUES)
            self.assertEqual(metadata["steps"][0]["byte_length"], 4)
            self.assertEqual(metadata["steps"][0]["rain_cell_count"], 2)
            self.assertEqual(metadata["steps"][0]["sunshine_cell_count"], 1)
            self.assertEqual(metadata["steps"][0]["missing_cell_count"], 1)
            self.assertEqual(metadata["steps"][0]["clipped_precipitation_cell_count"], 1)
            self.assertTrue(is_sunrain_run_complete("ch1", "20260609_0300", root=tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
