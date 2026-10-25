import base64
from dataclasses import replace
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import numpy as np
import xarray as xr

import generate_web_exports as exports
from generate_combined_manifest import scan_direct_wind_maps
from wind_maps import WindMapAccumulator, WindMapLevel, load_config
from wind_directions import ALTITUDES_M, MISSING, WindDirectionAccumulator, validate_publication


def synthetic_accumulators(root, model='ch1', run='20261025_0000'):
    reference = dt.datetime.strptime(run, '%Y%m%d_%H%M').replace(tzinfo=dt.timezone.utc)
    config = replace(load_config(path=str(Path(__file__).resolve().parents[1] / 'wind_maps_config.json'), log=lambda *_: None),
                     crop={'lon_min': 0.8, 'lon_max': 0.96, 'lat_min': 42.4, 'lat_max': 42.56},
                     levels=(WindMapLevel('3000m_AMSL', 3000, 'AMSL'),))
    parent = WindMapAccumulator(model, run, reference, config, out_root=str(root), log=lambda *_: None)
    coords = {'lat': ('cell', [42.4, 42.4, 42.56, 42.56]), 'lon': ('cell', [0.8, 0.96, 0.8, 0.96])}
    heights = np.array([10000., 5000., 0.])[:, None] * np.ones((1, 4))
    fields = {
        'U': xr.DataArray(heights / 100 + 5, dims=('z', 'cell'), coords=coords),
        'V': xr.DataArray(-heights / 200, dims=('z', 'cell'), coords=coords),
        'HHL': xr.DataArray(np.array([12000., 8000., 2000., -2000.])[:, None] * np.ones((1, 4)), dims=('half', 'cell'), coords=coords),
    }
    return parent, WindDirectionAccumulator(parent), fields, reference


class WindDirectionTests(unittest.TestCase):
    def test_all_altitudes_reuse_geometry_and_preserve_strong_winds(self):
        with tempfile.TemporaryDirectory() as temp:
            parent, directions, fields, reference = synthetic_accumulators(Path(temp) / 'cache_wind_maps')
            self.assertTrue(parent.append(fields, 0, reference))
            # Preparing the sidecar must not triangulate the domain a second time.
            with mock.patch('wind_maps.Delaunay', side_effect=AssertionError('duplicate triangulation')):
                self.assertTrue(directions.append(fields, 0, reference))
            parent.finalize(); directions.finalize()
            self.assertLessEqual(directions.source_indices.size, parent.source_indices.size)
            for altitude in ALTITUDES_M:
                level = Path(temp) / 'cache_wind_maps/ch1/20261025_0000/directions' / f'{altitude}m_AMSL'
                metadata = json.loads((level / 'metadata.json').read_text())
                self.assertEqual((metadata['grid']['width'], metadata['grid']['height']), (3, 3))
                self.assertAlmostEqual(metadata['grid']['lon']['step'], .08, places=4)
                raw = np.frombuffer((level / 'steps/H00.bin').read_bytes(), dtype='<i2').reshape(3, 3, 2)
                np.testing.assert_array_equal(raw[..., 0], round((altitude / 100 + 5) / .25))
                np.testing.assert_array_equal(raw[..., 1], round(-altitude / 200 / .25))
            self.assertGreater(raw[0, 0, 0], 127)
            self.assertEqual(directions._encode_web_component(np.array([[np.nan]]))[0, 0], MISSING)

    def test_chunk_merge_keeps_every_altitude_and_hour(self):
        from scripts.merge_map_chunks import merge_wind_direction_chunks
        with tempfile.TemporaryDirectory() as temp:
            previous = os.getcwd()
            try:
                os.chdir(temp)
                for hour in (0, 1):
                    root = Path('map_chunks/ch1') / f'part{hour}' / 'cache_wind_maps'
                    parent, directions, fields, reference = synthetic_accumulators(root)
                    parent.append(fields, hour, reference)
                    directions.append(fields, hour, reference)
                    directions.finalize()
                merge_wind_direction_chunks()
                levels = scan_direct_wind_maps(directions=True)['ch1']['20261025_0000']['levels']
                self.assertEqual(len(levels), 10)
                for entry in levels.values():
                    self.assertEqual([s['horizon'] for s in entry['steps']], [0, 1])
                    self.assertTrue(all(Path(s['path']).stat().st_size == 36 for s in entry['steps']))
            finally:
                os.chdir(previous)

    def test_below_ground_stays_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            parent, directions, fields, reference = synthetic_accumulators(Path(temp))
            fields['HHL'].values[-1] = 4000
            fields['HHL'].values[-2] = 6000
            parent.append(fields, 0, reference)
            directions.append(fields, 0, reference)
            directions.finalize()
            raw = np.fromfile(Path(temp) / 'ch1/20261025_0000/directions/1000m_AMSL/steps/H00.bin', dtype='<i2')
            self.assertTrue(np.all(raw == MISSING))

    def test_export_retention_index_and_validation_keep_layers_separate(self):
        from scripts import apply_web_retention as retention
        with tempfile.TemporaryDirectory() as temp:
            previous = os.getcwd()
            try:
                os.chdir(temp)
                parent, directions, fields, reference = synthetic_accumulators(Path('cache_wind_maps'))
                for hour in range(34):
                    parent.append(fields, hour, reference)
                    directions.append(fields, hour, reference)
                parent.finalize(); directions.finalize()
                normal = scan_direct_wind_maps()
                coarse = scan_direct_wind_maps(directions=True)
                with mock.patch.object(exports, 'WEB_DIR', Path('web_exports')), mock.patch.dict(os.environ, {'WIND_WEB_LEVELS': 'all'}):
                    exports.export_wind_maps({'wind_maps': normal})
                    output = exports.export_wind_maps({'wind_directions': coarse}, directions=True)
                levels = output['models']['icon-ch1']['runs']['20261025_0000']['levels']
                self.assertEqual(set(levels), {f'{h}m_AMSL' for h in ALTITUDES_M})
                self.assertTrue(all(not entry['steps'] for entry in levels.values()))
                validate_publication(Path('web_exports'), {'icon-ch1': '20261025_0000'})
                with mock.patch.object(retention, 'WEB_DIR', Path('web_exports')):
                    rebuilt = retention.rebuild_wind_manifest(directions=True)
                    regular = retention.rebuild_wind_manifest()
                self.assertEqual(set(rebuilt['models']['icon-ch1']['runs']['20261025_0000']['levels']), set(levels))
                self.assertEqual(set(regular['models']['icon-ch1']['runs']['20261025_0000']['levels']), {'3000m_AMSL'})
                path = Path(levels['8000m_AMSL']['metadata'])
                payload = json.loads(path.read_text()); payload['steps'].pop(); path.write_text(json.dumps(payload))
                with self.assertRaisesRegex(ValueError, 'incomplete direction hours'):
                    validate_publication(Path('web_exports'), {'icon-ch1': '20261025_0000'})
            finally:
                os.chdir(previous)


if __name__ == '__main__':
    unittest.main()
