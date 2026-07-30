import unittest

import numpy as np

from wind_streamline_feasibility import (
    MAGIC,
    PathGeometry,
    Presentation,
    WindSampler,
    clip_paths_to_draw_bounds,
    decode_bundle,
    encode_bundle,
    integrate_paths,
    presentation_geometry,
    simplify_paths,
)


def metadata(width=4, height=4):
    return {
        "grid": {
            "width": width,
            "height": height,
            "lon": {"start": 5.0, "step": 0.1},
            "lat": {"start": 46.0, "step": 0.1},
        },
        "encoding": {"missing_value": -128, "scale_factor": 0.25},
    }


class WindStreamlineFeasibilityTests(unittest.TestCase):
    def test_bundle_round_trip_preserves_quantized_paths(self):
        paths = [
            PathGeometry(8.12, ((5.0, 46.0), (5.05, 46.03), (5.1, 46.08))),
            PathGeometry(2.5, ((5.2, 46.1), (5.21, 46.11))),
        ]

        encoded, stats = encode_bundle(paths, (5.0, 46.0, 5.3, 46.3))
        decoded, bbox = decode_bundle(encoded)

        self.assertEqual(encoded[:4], MAGIC)
        self.assertEqual(stats["encoded_paths"], 2)
        self.assertEqual(stats["encoded_points"], 5)
        self.assertEqual(bbox, (5.0, 46.0, 5.3, 46.3))
        self.assertAlmostEqual(decoded[0].seed_speed_ms, 8.12)
        self.assertAlmostEqual(decoded[0].points[-1][0], 5.1, places=4)
        self.assertAlmostEqual(decoded[0].points[-1][1], 46.08, places=4)

    def test_bundle_rejects_trailing_bytes(self):
        encoded, _ = encode_bundle(
            [PathGeometry(1.0, ((5.0, 46.0), (5.1, 46.1)))],
            (5.0, 46.0, 5.3, 46.3),
        )

        with self.assertRaisesRegex(ValueError, "trailing bytes"):
            decode_bundle(encoded + b"\0")

    def test_sampler_matches_bilinear_wind_interpolation(self):
        values = np.zeros((4, 4, 2), dtype=np.int8)
        values[:, :, 0] = np.arange(16, dtype=np.int8).reshape(4, 4)
        values[:, :, 1] = 8
        sampler = WindSampler(metadata(), values.tobytes())

        sampled = sampler.sample(5.05, 46.05)

        self.assertIsNotNone(sampled)
        self.assertAlmostEqual(sampled[0], 0.625)
        self.assertAlmostEqual(sampled[1], 2.0)

    def test_integrator_generates_paths_from_a_uniform_field(self):
        values = np.zeros((20, 20, 2), dtype=np.int8)
        values[:, :, 0] = 20
        fixture_metadata = metadata(20, 20)
        fixture_metadata["grid"]["lon"]["step"] = 0.2
        fixture_metadata["grid"]["lat"]["step"] = 0.2
        presentation = Presentation(
            "fixture",
            160,
            120,
            "desktop",
            bbox=(5.2, 46.2, 7.8, 48.8),
            overscan=0,
        )

        paths, stats = integrate_paths(fixture_metadata, values.tobytes(), presentation)

        self.assertGreater(len(paths), 0)
        self.assertEqual(stats["accepted_paths"], len(paths))
        self.assertTrue(all(path.points[-1][0] > path.points[0][0] for path in paths))

    def test_profiles_retain_current_desktop_and_mobile_integration_counts(self):
        desktop = presentation_geometry(Presentation("desktop", 1024, 640, "desktop"))
        mobile = presentation_geometry(Presentation("mobile", 411, 520, "phone-portrait"))

        self.assertEqual(desktop.steps, 32)
        self.assertEqual(mobile.steps, 52)
        self.assertEqual(desktop.dx_px, 16)
        self.assertGreater(mobile.trajectory_seconds, desktop.trajectory_seconds)

    def test_simplification_preserves_end_segment_and_subpixel_curve(self):
        paths = [
            PathGeometry(
                5.0,
                (
                    (5.0, 46.0),
                    (5.01, 46.01001),
                    (5.02, 46.02),
                    (5.03, 46.03),
                ),
            )
        ]
        snapshot = {
            "draw_bounds": {
                "west_x": 0.5,
                "north_y": 0.35,
                "east_x": 0.53,
                "south_y": 0.38,
            },
            "draw_width": 800,
            "draw_height": 600,
        }

        simplified, stats = simplify_paths(paths, snapshot, 0.5)

        self.assertLess(len(simplified[0].points), len(paths[0].points))
        self.assertEqual(simplified[0].points[-2:], paths[0].points[-2:])
        self.assertGreater(stats["simplification_removed_points"], 0)

    def test_view_clipping_suppresses_arrowheads_on_clipped_endpoints(self):
        snapshot = {
            "draw_bounds": {
                "west_x": 0.5,
                "north_y": 0.35,
                "east_x": 0.53,
                "south_y": 0.38,
            },
            "draw_width": 800,
            "draw_height": 600,
        }
        paths = [
            PathGeometry(5.0, ((-1.0, 46.0), (5.5, 46.5), (20.0, 47.0))),
        ]

        clipped, stats = clip_paths_to_draw_bounds(paths, snapshot)

        self.assertGreater(len(clipped), 0)
        self.assertTrue(all(path.seed_speed_ms == 0 for path in clipped))
        self.assertTrue(stats["clipped_to_draw_bounds"])


if __name__ == "__main__":
    unittest.main()
