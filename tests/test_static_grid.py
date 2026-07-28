import unittest
from unittest import mock

import numpy as np
import xarray as xr

from forecast_fetch.static_grid import load_horizontal_grid


class StaticGridTests(unittest.TestCase):
    def test_current_multigroup_layout_uses_surface_tlat_tlon(self):
        current = xr.Dataset(
            {
                "tlat": ("values", np.asarray([42.0, 50.0])),
                "tlon": ("values", np.asarray([-1.0, 18.0])),
            }
        )

        with mock.patch(
            "forecast_fetch.static_grid.xr.open_dataset",
            return_value=current,
        ) as open_dataset:
            grid = load_horizontal_grid("current.grib2")

        np.testing.assert_array_equal(grid["lat"].values, [42.0, 50.0])
        np.testing.assert_array_equal(grid["lon"].values, [-1.0, 18.0])
        self.assertEqual(
            open_dataset.call_args.kwargs["backend_kwargs"],
            {"indexpath": "", "filter_by_keys": {"typeOfLevel": "surface"}},
        )

    def test_legacy_single_group_layout_is_the_fallback(self):
        legacy = xr.Dataset(
            coords={
                "latitude": ("cell", np.asarray([46.0, 47.0])),
                "longitude": ("cell", np.asarray([7.0, 8.0])),
            }
        )

        with mock.patch(
            "forecast_fetch.static_grid.xr.open_dataset",
            side_effect=[ValueError("no surface group"), legacy],
        ) as open_dataset:
            grid = load_horizontal_grid("legacy.grib2")

        self.assertEqual(open_dataset.call_count, 2)
        np.testing.assert_array_equal(grid["lat"].values, [46.0, 47.0])
        np.testing.assert_array_equal(grid["lon"].values, [7.0, 8.0])

    def test_missing_coordinates_fail_closed(self):
        empty = xr.Dataset({"lsm": ("values", np.asarray([0.0, 1.0]))})
        with mock.patch(
            "forecast_fetch.static_grid.xr.open_dataset",
            side_effect=[empty, empty],
        ):
            with self.assertRaisesRegex(ValueError, "no explicit latitude/longitude"):
                load_horizontal_grid("broken.grib2")


if __name__ == "__main__":
    unittest.main()
