import unittest
from types import SimpleNamespace

import numpy as np
import xarray as xr

from wind_maps import (
    HorizontalMapGeometry,
    _HorizontalWeights,
    _interpolate_vertical,
    _single_level_values,
    WindMapAccumulator,
    is_wind_maps_enabled,
    load_config,
)


class WindMapTests(unittest.TestCase):
    def test_flags_are_off_by_default_and_model_scoped(self):
        self.assertFalse(is_wind_maps_enabled("ch1", env={}))
        self.assertFalse(is_wind_maps_enabled("ch1", env={"ENABLE_WIND_MAPS": "true"}))
        self.assertTrue(
            is_wind_maps_enabled(
                "ch1",
                env={"ENABLE_WIND_MAPS": "true", "ENABLE_WIND_MAPS_CH1": "true"},
            )
        )
        self.assertFalse(
            is_wind_maps_enabled(
                "ch2",
                env={"ENABLE_WIND_MAPS": "true", "ENABLE_WIND_MAPS_CH1": "true"},
            )
        )

    def test_config_loads_all_existing_levels_and_crop(self):
        cfg = load_config(log=lambda *_: None)
        self.assertEqual(
            [level.name for level in cfg.enabled_levels],
            [
                "10m_AGL",
                "800m_AGL",
                "1000m_AMSL",
                "1500m_AMSL",
                "2000m_AMSL",
                "2500m_AMSL",
                "3000m_AMSL",
                "4000m_AMSL",
            ],
        )
        self.assertEqual(cfg.crop["lon_min"], 0.8)
        self.assertEqual(cfg.crop["lon_max"], 16.4)
        self.assertEqual(cfg.crop["lat_min"], 42.4)
        self.assertEqual(cfg.crop["lat_max"], 50.0)
        self.assertEqual(cfg.domain_id, "icon-ch-common-safe")
        self.assertEqual(cfg.domain_label, "ICON-CH Common Safe Domain")
        self.assertEqual(cfg.max_seconds, 0)

    def test_config_can_limit_levels_for_manual_trials(self):
        cfg = load_config(env={"WIND_MAP_LEVELS": "800m_AGL,1500m_AMSL"}, log=lambda *_: None)
        self.assertEqual([level.name for level in cfg.enabled_levels], ["800m_AGL", "1500m_AMSL"])

    def test_export_style_uses_dataset_crop_attrs(self):
        lat = np.asarray([[43.0, 43.0], [43.02, 43.02]], dtype=np.float32)
        lon = np.asarray([[4.0, 4.02], [4.0, 4.02]], dtype=np.float32)

        accumulator = object.__new__(WindMapAccumulator)
        style = accumulator._wind_style_payload(
            lat,
            lon,
            {
                "crop_lon_min": 4.0,
                "crop_lon_max": 16.5,
                "crop_lat_min": 43.0,
                "crop_lat_max": 48.8,
                "domain_id": "alps",
                "domain_label": "Alps",
            },
        )

        self.assertEqual(style["map_bbox"], [4.0, 43.0, 16.5, 48.8])
        self.assertEqual(style["domain"]["id"], "alps")

    def test_interpolation_below_lowest_model_layer_remains_missing(self):
        heights = np.asarray(
            [
                [35.0, 45.0],
                [110.0, 120.0],
                [300.0, 320.0],
            ],
            dtype=np.float32,
        )
        values = np.asarray(
            [
                [2.0, 4.0],
                [6.0, 8.0],
                [10.0, 12.0],
            ],
            dtype=np.float32,
        )

        result = _interpolate_vertical(heights, values, 10.0)

        self.assertTrue(np.all(np.isnan(result)))

    def test_horizontal_weights_do_not_extrapolate_far_outside_domain(self):
        weights = _HorizontalWeights(
            np.asarray([0.0, 1.0, 0.0], dtype=np.float32),
            np.asarray([0.0, 0.0, 1.0], dtype=np.float32),
            np.asarray([[0.25, 10.0]], dtype=np.float32),
            np.asarray([[0.25, 10.0]], dtype=np.float32),
        )

        result = weights.apply(np.asarray([1.0, 2.0, 3.0], dtype=np.float32))

        self.assertTrue(np.isfinite(result[0, 0]))
        self.assertTrue(np.isnan(result[0, 1]))

    def test_horizontal_map_geometry_is_prepared_once_for_shared_products(self):
        config = SimpleNamespace(
            crop={"lon_min": 0.0, "lon_max": 1.0, "lat_min": 0.0, "lat_max": 1.0},
            grid_spacing_deg=1.0,
            source_padding_deg=0.0,
        )
        geometry = HorizontalMapGeometry(config)
        lat = np.asarray([0.0, 0.0, 1.0, 1.0], dtype=np.float32)
        lon = np.asarray([0.0, 1.0, 0.0, 1.0], dtype=np.float32)

        first = geometry.prepare(lat, lon)
        first_weights = geometry.weights
        second = geometry.prepare(lat.copy(), lon.copy())

        self.assertIs(first, geometry)
        self.assertIs(second, geometry)
        self.assertIs(geometry.weights, first_weights)
        self.assertEqual(geometry.target_lat.shape, (2, 2))
        self.assertEqual(geometry.source_indices.tolist(), [0, 1, 2, 3])

        with self.assertRaisesRegex(ValueError, "different native grid shape"):
            geometry.prepare(lat[:3], lon[:3])

    def test_single_level_values_extracts_native_10m_wind(self):
        data = xr.DataArray(
            np.asarray([[1.0, 2.0, 3.0]], dtype=np.float32),
            dims=("heightAboveGround", "values"),
        )

        values = _single_level_values(data, "values")

        np.testing.assert_allclose(values, [1.0, 2.0, 3.0])


if __name__ == "__main__":
    unittest.main()
