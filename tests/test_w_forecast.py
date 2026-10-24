import datetime as dt
import gzip
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np

from w_forecast.config import Config
from w_forecast.feed import Feed, FILL, pack
from w_forecast.geometry import Plan, decode_w, horizontal_plan, vertical_brackets
from w_forecast.grib import download
from w_forecast.runner import run


REFERENCE = dt.datetime(2026, 10, 24, 0, tzinfo=dt.timezone.utc)
GRID_ID = {"uuidOfHGrid": "horizontal", "uuidOfVGrid": "vertical"}


class GeometryTests(unittest.TestCase):
    def config(self, altitudes=(100, 200)):
        return Config(bbox=(0, 0, 0.1, 0.1), spacing=0.05, altitudes=altitudes)

    def test_half_level_interpolation_and_terrain_mask(self):
        terrain = np.asarray([0, 150, 300], dtype=np.float32)
        heights = [(level, terrain + (81 - level) * 100, GRID_ID) for level in range(1, 82)]
        lower, fraction = vertical_brackets(iter(heights), np.arange(3), (50, 200), GRID_ID)
        np.testing.assert_array_equal(lower > 0, [[True, False, False], [True, True, False]])
        # An analytic W(z)=z/1000 must interpolate exactly at half-level heights.
        values = np.zeros_like(fraction)
        for row, altitude in enumerate((50, 200)):
            for col in range(3):
                if lower[row, col]:
                    lo = terrain[col] + (81 - lower[row, col]) * 100
                    hi = lo + 100
                    values[row, col] = (lo * (1 - fraction[row, col]) + hi * fraction[row, col]) / 1000
                    self.assertAlmostEqual(float(values[row, col]), altitude / 1000, places=6)

    def test_truncated_or_wrong_grid_hhl_fails(self):
        records = [(i, np.array([8100 - i * 100]), GRID_ID) for i in range(1, 81)]
        with self.assertRaisesRegex(ValueError, "truncated"):
            vertical_brackets(iter(records), np.arange(1), (100,), GRID_ID)
        with self.assertRaisesRegex(ValueError, "UUID"):
            vertical_brackets(iter(records), np.arange(1), (100,), {**GRID_ID, "uuidOfVGrid": "other"})

    def test_barycentric_plane_and_no_extrapolation(self):
        config = Config(bbox=(0, 0, 0.1, 0.1), spacing=0.01, altitudes=(100,))
        lat = np.array([0, 0, .02, .02])
        lon = np.array([0, .02, 0, .02])
        arrays = horizontal_plan(lat, lon, config)
        plan = Plan(config, {}, arrays)
        source = lon[arrays["source_indices"]] + 2 * lat[arrays["source_indices"]]
        result = plan.regrid(source)
        self.assertAlmostEqual(float(result[1, 1]), .03, places=6)
        self.assertTrue(np.isnan(result[-1, -1]))

    def test_zero_weight_nan_does_not_hide_exact_native_value(self):
        plan = Plan(self.config(), {}, {
            "vertices": np.array([[0, 1, 2]]), "weights": np.array([[1, 0, 0]]),
            "target_indices": np.array([0]),
        })
        self.assertEqual(plan.regrid(np.array([2, np.nan, np.nan]))[0, 0], 2)

    def test_stream_decode_skips_unused_levels_and_checks_time_and_member(self):
        terrain = np.array([0., 50., 300.])
        heights = [(i, terrain + (81 - i) * 100, GRID_ID) for i in range(1, 82)]
        lower, fraction = vertical_brackets(iter(heights), np.arange(3), (100, 200), GRID_ID)
        plan = Plan(self.config(), {"grid_identity": GRID_ID, "native_cells": 3}, {
            "source_indices": np.arange(3), "lower": lower, "fraction": fraction,
        })
        records = [{**GRID_ID, "level": i, "numberOfDataPoints": 3,
                    "name": "Geometric vertical velocity", "units": "m s**-1",
                    "typeOfLevel": "generalVertical", "dataDate": 20261024,
                    "dataTime": 0, "endStep": 1, "forecastTime": 1,
                    "indicatorOfUnitOfTimeRange": 1, "stepType": "instant", "perturbationNumber": 0,
                    "missingValue": 9999, "values": (terrain + (81 - i) * 100) / 1000}
                   for i in range(1, 82)]
        from contextlib import contextmanager
        @contextmanager
        def messages(_):
            yield iter(records)
        with patch("w_forecast.geometry.messages", messages), patch("w_forecast.geometry.codes_get", side_effect=lambda h, k: h[k]), patch("w_forecast.grib.codes_get", side_effect=lambda h, k: h[k]), patch("w_forecast.geometry.codes_get_values", side_effect=lambda h: h["values"]) as values:
            native, stats = decode_w("unused", plan, REFERENCE, 1)
            np.testing.assert_allclose(native[:, :2], [[.1, .1], [.2, .2]], atol=1e-6)
            self.assertTrue(np.all(np.isnan(native[:, 2])))
            self.assertEqual(values.call_count, stats["decoded_levels"])
            self.assertLess(stats["decoded_levels"], 10)
            for record in records:
                record.update(endStep="0m", forecastTime=0, indicatorOfUnitOfTimeRange=0)
            zero, _ = decode_w("unused", plan, REFERENCE, 0)
            np.testing.assert_allclose(zero, native, equal_nan=True)
            records[0]["forecastTime"] = 2
            with self.assertRaisesRegex(ValueError, "reference time"):
                decode_w("unused", plan, REFERENCE, 1)


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Config(bbox=(0, 0, .1, .1), spacing=.1, altitudes=(100,))
        self.feed = Feed(self.temporary.name, self.config)

    def frame(self, horizon=1, reference=REFERENCE):
        frame = self.feed.frame(np.array([[-1.234, np.nan], [2.345, 0.]]), reference, horizon, 100)
        frame["source_file"] = {"sha256": "source", "bytes": 123}
        return frame

    def publish(self, frames, reference=REFERENCE):
        return self.feed.publish(frames, reference, [1], 33, {"static_sha256": {"x": "y"}}, {"git_commit": "test"})

    def test_round_trip_sign_precision_missing_and_checksum(self):
        frame = self.frame()
        path = Path(self.temporary.name) / frame["artifact"]["path"]
        raw = np.frombuffer(gzip.decompress(path.read_bytes()), dtype="<i2").reshape(2, 2)
        self.assertEqual(raw[0, 1], FILL)
        self.assertAlmostEqual(raw[0, 0] * .01, -1.23)
        self.assertAlmostEqual(raw[1, 0] * .01, 2.35)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), frame["artifact"]["sha256"])
        with self.assertRaisesRegex(ValueError, "refusing to clip"):
            pack(np.array([1000.]))

    def test_atomic_complete_pointer_and_verified_resume(self):
        frame = self.frame()
        pointer = self.publish([frame])
        root = Path(self.temporary.name)
        manifest_bytes = (root / pointer["manifest"]).read_bytes()
        self.assertEqual(hashlib.sha256(manifest_bytes).hexdigest(), pointer["sha256"])
        self.assertEqual(len(self.feed.existing(REFERENCE, {"x": "y"})), 1)
        self.assertEqual(self.feed.existing(REFERENCE, {"x": "different"}), {})
        before = (root / "latest.json").read_bytes()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.publish([])
        self.assertEqual(before, (root / "latest.json").read_bytes())
        (root / frame["artifact"]["path"]).write_bytes(b"broken")
        self.assertEqual(self.feed.existing(REFERENCE), {})
        with self.assertRaisesRegex(ValueError, "validation"):
            self.publish([frame])
        self.assertEqual(before, (root / "latest.json").read_bytes())

    def test_older_source_cannot_replace_newer_pointer(self):
        self.publish([self.frame()])
        old = REFERENCE - dt.timedelta(hours=3)
        with self.assertRaisesRegex(ValueError, "older run"):
            self.publish([self.frame(reference=old)], old)

    def test_same_content_is_deduplicated_and_current_survives_retention(self):
        first = self.frame()
        second = self.frame()
        self.assertEqual(first["artifact"], second["artifact"])
        self.publish([first])
        self.feed.prune()
        self.assertTrue((Path(self.temporary.name) / first["artifact"]["path"]).is_file())

    def test_current_coverage_cannot_shrink(self):
        first = self.frame(1)
        second = self.frame(2)
        self.feed.publish([first, second], REFERENCE, [1, 2], 33, {}, {})
        with self.assertRaisesRegex(ValueError, "reduce coverage"):
            self.publish([first])

    def test_retention_keeps_two_runs_and_removes_only_expired_unreachable_objects(self):
        root = Path(self.temporary.name)
        for offset in (0, 3, 6):
            reference = REFERENCE + dt.timedelta(hours=offset)
            self.publish([self.frame(reference=reference)], reference)
        old = time.time() - 86400
        for path in (root / "runs").glob("*/*.json"):
            os.utime(path, (old, old))
        orphan = root / "objects" / ("a" * 64 + ".bin.gz")
        orphan.write_bytes(b"unreachable")
        os.utime(orphan, (old, old))
        self.feed.prune()
        self.assertEqual(len(list((root / "runs").iterdir())), 2)
        self.assertFalse(orphan.exists())
        current = json.loads((root / "latest.json").read_text())
        self.assertTrue((root / current["manifest"]).exists())


class DownloadTests(unittest.TestCase):
    def test_truncated_download_never_replaces_cached_file_and_removes_part(self):
        class Response:
            status_code = 200
            headers = {"Content-Length": "100"}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def iter_content(self, size): yield b"short"
        class Session:
            def get(self, *args, **kwargs): return Response()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "W.grib2"
            path.write_bytes(b"previous")
            with patch("w_forecast.grib.check_space"), self.assertRaisesRegex(RuntimeError, "truncated"):
                download("https://example.invalid/signed", path, session=Session())
            self.assertEqual(path.read_bytes(), b"previous")
            self.assertFalse(path.with_suffix(".grib2.part").exists())


class RunnerTests(unittest.TestCase):
    def test_interrupted_run_resumes_private_checkpoint_without_partial_publication(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            config = Config(bbox=(0, 0, .1, .1), spacing=.1, altitudes=(100,))
            plan = SimpleNamespace(metadata={"grid_identity": GRID_ID}, regrid=lambda native: native)
            args = SimpleNamespace(bbox=config.bbox, spacing=config.spacing, altitudes=config.altitudes,
                                   work_dir=str(root / "work"), output=str(root / "feed"),
                                   run="20261024_0000", start=0, end=2, keep_runs=2)
            source = SimpleNamespace(session=SimpleNamespace(close=lambda: None),
                                     static_urls=lambda: {"horizontal": "horizontal", "vertical": "vertical"},
                                     w_url=lambda reference, horizon: None if horizon == 2 else f"W{horizon}")
            metadata = {"sha256": "source", "bytes": 100, "download_seconds": 0, "reused": False}
            with patch("w_forecast.runner.Source", return_value=source), patch("w_forecast.runner.download", return_value=metadata) as downloader, patch("w_forecast.runner.prepare", return_value=(plan, {"reused": False, "seconds": 0})), patch("w_forecast.runner.decode_w", return_value=(np.ones((1, 2, 2)), {"decoded_levels": 2, "messages": 81})), patch("w_forecast.runner.revision", return_value={"git_commit": "test"}):
                with self.assertRaisesRegex(RuntimeError, "not available"):
                    run(args)
                self.assertFalse((root / "feed/latest.json").exists())
                checkpoints = list((root / "work/checkpoints").glob("*.json"))
                self.assertEqual(len(checkpoints), 1)
                self.assertEqual(len(json.loads(checkpoints[0].read_text())["frames"]), 2)
                downloader.reset_mock()
                source.w_url = lambda reference, horizon: f"W{horizon}"
                report = run(args)
                self.assertTrue((root / "feed/latest.json").exists())
                self.assertEqual([s["resumed"] for s in report["steps"]], [True, True, False])
                self.assertEqual([call.args[0] for call in downloader.call_args_list], ["horizontal", "vertical", "W2"])
                self.assertFalse(checkpoints[0].exists())


if __name__ == "__main__":
    unittest.main()
