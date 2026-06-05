import datetime
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

import numpy as np
import xarray as xr

from rain_maps import RAIN_FILL_VALUE, RainMapAccumulator, is_rain_maps_enabled


def _temp_workspace():
    return tempfile.mkdtemp(prefix="xcb_rain_", dir=os.getenv("TEST_TMPDIR", r"C:\tmp"))


def _sample_grid():
    return xr.DataArray(
        np.zeros(4, dtype=np.float32),
        dims=("values",),
        coords={
            "latitude": ("values", np.asarray([0.0, 0.0, 1.0, 1.0], dtype=np.float32)),
            "longitude": ("values", np.asarray([0.0, 1.0, 0.0, 1.0], dtype=np.float32)),
        },
    )


def _config():
    return SimpleNamespace(
        crop={"lon_min": 0.0, "lon_max": 1.0, "lat_min": 0.0, "lat_max": 1.0},
        grid_spacing_deg=1.0,
        source_padding_deg=0.0,
        horizon_stride=1,
    )


class RainMapTests(unittest.TestCase):
    def test_flags_are_on_by_default_and_model_scoped(self):
        self.assertTrue(is_rain_maps_enabled("ch1", env={}))
        self.assertFalse(is_rain_maps_enabled("ch1", env={"ENABLE_RAIN_MAPS": "false"}))
        self.assertFalse(
            is_rain_maps_enabled(
                "ch1",
                env={"ENABLE_RAIN_MAPS": "true", "ENABLE_RAIN_MAPS_CH1": "false"},
            )
        )

    def test_accumulator_deaccumulates_clips_missing_and_writes_metadata(self):
        tmp = _temp_workspace()
        try:
            ref = datetime.datetime(2026, 6, 5, 3, tzinfo=datetime.timezone.utc)
            acc = RainMapAccumulator("ch1", "20260605_0300", ref, _config(), log=lambda *_: None, out_root=tmp)
            acc.seed_previous_raw(np.asarray([1.0, 5.0, 10.0, 0.0], dtype=np.float32))
            acc.target_lat = np.asarray([[0.0, 0.0], [1.0, 1.0]], dtype=np.float32)
            acc.target_lon = np.asarray([[0.0, 1.0], [0.0, 1.0]], dtype=np.float32)
            acc.source_indices = np.arange(4)
            acc.weights = SimpleNamespace(apply=lambda values: np.asarray(values, dtype=np.float32).reshape(2, 2))
            acc.prepared = True

            self.assertTrue(
                acc.append(
                    _sample_grid(),
                    {"TOT_PREC": np.asarray([2.0, 4.0, 70.0, np.nan], dtype=np.float32)},
                    2,
                    ref,
                )
            )
            result = acc.finalize()
            metadata_path = os.path.join(tmp, "ch1", "20260605_0300", "surface", "metadata.json")
            step_path = os.path.join(tmp, "ch1", "20260605_0300", "surface", "steps", "H02.bin")

            self.assertEqual(result["files"], 2)
            self.assertTrue(os.path.exists(metadata_path))
            self.assertTrue(os.path.exists(step_path))

            values = np.fromfile(step_path, dtype="u1")
            self.assertEqual(values.tolist(), [5, 0, 254, int(RAIN_FILL_VALUE)])

            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            self.assertEqual(metadata["source_variable"], "TOT_PREC")
            self.assertEqual(metadata["derived_component"], "deaccumulated_step_precipitation")
            self.assertEqual(metadata["encoding"]["components"], ["precipitation_mm"])
            self.assertEqual(metadata["encoding"]["dtype"], "uint8")
            self.assertEqual(metadata["steps"][0]["clipped_precipitation_cell_count"], 1)
            self.assertEqual(metadata["steps"][0]["path"].replace("\\", "/"), step_path.replace("\\", "/"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
