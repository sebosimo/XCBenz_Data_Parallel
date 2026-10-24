import datetime as dt
from pathlib import Path
import tempfile
from threading import Event
import unittest

import numpy as np

from w_forecast.config import Config, MODELS
from w_forecast.feed import Feed
from w_forecast.packing import compress, decompress, package, read_block, read_directory
from w_forecast.prefetch import downloads
from w_forecast.guard import ready


class PackingTests(unittest.TestCase):
    def test_predictor_round_trip_includes_all_int16_values(self):
        values = np.arange(-32768, 32768, dtype=np.int32).astype("<i2").reshape(256, 256)
        np.testing.assert_array_equal(decompress(compress(values), 256, 256), values)
        with self.assertRaises(ValueError):
            decompress(compress(values), 1, 1)

    def test_tiles_and_halos_reconstruct_odd_sized_grid_exactly(self):
        values = np.random.default_rng(42).integers(-32768, 32768, size=(259, 515), dtype=np.int16)
        payload, metadata = package(values)
        directory, offset = read_directory(payload)
        self.assertEqual(metadata["initial_bytes"], offset + directory["overview"]["block"]["bytes"])
        np.testing.assert_array_equal(read_block(payload, directory["overview"]["block"], offset), values[::4, ::4])
        restored = np.empty_like(values)
        padded = np.pad(values, 1, mode="edge")
        for tile in directory["tiles"]:
            decoded = read_block(payload, tile["block"], offset)
            x, y, w, h = (tile[k] for k in ("x", "y", "core_width", "core_height"))
            np.testing.assert_array_equal(decoded, padded[y:y+h+2, x:x+w+2])
            restored[y:y+h, x:x+w] = decoded[1:h+1, 1:w+1]
        np.testing.assert_array_equal(restored, values)

    def test_ch2_contract_and_grid_are_independent(self):
        reference = dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc)
        self.assertEqual(MODELS["ch1"].maximum_horizon(reference), 33)
        self.assertEqual(MODELS["ch2"].maximum_horizon(reference), 120)
        self.assertEqual(MODELS["ch2"].spacing, .02)
        with tempfile.TemporaryDirectory() as root:
            config = Config(bbox=(0, 0, .1, .1), spacing=.1, altitudes=(100,))
            feed = Feed(root, config, model="ch2", packaging="packed-v2")
            frame = feed.frame(np.array([[0., -1.], [np.nan, 2.]]), reference, 120, 100)
            pointer = feed.publish([frame], reference, [120], 120, {}, {})
            self.assertEqual(pointer["schema"], "xcbenz.w.v2")
            self.assertEqual(len(feed.existing(reference)), 1)


class PrefetchTests(unittest.TestCase):
    def test_weather_limits_defer_without_consuming_main_forecast_headroom(self):
        self.assertTrue(ready(4096, 88, 8.8, 8))
        self.assertFalse(ready(4095, 20, 1, 8))
        self.assertFalse(ready(8192, 89, 1, 8))
        self.assertFalse(ready(8192, 20, 9, 8))
    def test_only_one_next_download_runs_and_all_raw_files_are_released(self):
        second_started, allow_second = Event(), Event()
        seen = []
        def fetch(h, path, cancelled):
            seen.append(h)
            path.write_bytes(b"raw")
            if h == 1:
                second_started.set()
                if not allow_second.wait(2):
                    raise RuntimeError("test timed out")
            return {"bytes": 3}
        with tempfile.TemporaryDirectory() as root:
            with downloads([0, 1, 2], fetch, root) as pending:
                self.assertEqual(next(pending)[0], 0)
                self.assertTrue(second_started.wait(2))
                self.assertEqual(seen, [0, 1])
                self.assertEqual(len(list(Path(root).glob("*.grib2"))), 2)
                allow_second.set()
                self.assertEqual(next(pending)[0], 1)
                self.assertEqual(next(pending)[0], 2)
            self.assertEqual(list(Path(root).glob("*.grib2*")), [])

    def test_processing_failure_cancels_and_cleans_lookahead(self):
        started = Event()
        def fetch(h, path, cancelled):
            path.write_bytes(b"raw")
            if h == 1:
                started.set()
                if not cancelled.wait(2):
                    raise RuntimeError("test timed out")
                raise RuntimeError("cancelled")
            return {}
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "processing failed"):
                with downloads([0, 1], fetch, root) as pending:
                    next(pending)
                    self.assertTrue(started.wait(2))
                    raise ValueError("processing failed")
            self.assertEqual(list(Path(root).glob("*.grib2*")), [])


if __name__ == "__main__":
    unittest.main()
