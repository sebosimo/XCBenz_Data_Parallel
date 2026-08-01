import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from wind_streamline_feasibility import (
    Geometry,
    TileProfile,
    integrate_paths_from_snapshot,
    longitude_to_mercator_x,
    latitude_to_mercator_y,
    tile_profile_snapshot,
)
from wind_streamline_tiles import (
    CONTRACT,
    CONTRACT_VERSION,
    CONTINUES_AFTER,
    CONTINUES_BEFORE,
    GENERATOR_REVISION,
    HEADER,
    MANIFEST_LAYOUT,
    ORIGINAL_END,
    ORIGINAL_START,
    PACKAGE,
    ProductionProfile,
    ProjectedPath,
    TileFragment,
    build_shadow_package,
    decode_tile,
    encode_tile,
    expected_complete_pilot_steps,
    integrate_projected_paths,
    integrate_projected_paths_vectorized,
    partition_paths,
    validate_shadow_package,
)


def temporary_directory():
    return Path(
        tempfile.mkdtemp(
            prefix="xcb_wind_streamline_tiles_",
            dir=os.getenv("TEST_TMPDIR"),
        )
    )


def fixture_metadata(width=20, height=20, steps=("H00",)):
    return {
        "schema_version": 4,
        "product": "wind_map_level",
        "model": "icon-ch1",
        "run": "20260729_1200",
        "level": {"name": "800m_AGL", "type": "agl", "height_m": 800},
        "grid": {
            "width": width,
            "height": height,
            "lon": {"start": 5.0, "step": 0.2},
            "lat": {"start": 46.0, "step": 0.2},
        },
        "encoding": {
            "dtype": "int8",
            "missing_value": -128,
            "scale_factor": 0.25,
        },
        "steps": [
            {"step": step, "path": f"steps/{step}.bin"} for step in steps
        ],
    }


def fixture_profile(name="fixture", zoom=6):
    return ProductionProfile(
        id=77,
        name=name,
        tile_zoom=zoom,
        pixels_per_mercator_unit=8_000,
        geometry=Geometry(16, 13, 0.62, 155, 8, 0.84, 600, True),
    )


def write_fixture(root: Path, steps=("H00",)) -> Path:
    metadata = fixture_metadata(steps=steps)
    (root / "steps").mkdir(parents=True)
    values = np.zeros((20, 20, 2), dtype=np.int8)
    values[:, :, 0] = 20
    for index, step in enumerate(steps):
        step_values = values.copy()
        step_values[:, :, 1] = index
        (root / "steps" / f"{step}.bin").write_bytes(step_values.tobytes())
    metadata_path = root / "metadata.json"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    return metadata_path


class WindStreamlineTileTests(unittest.TestCase):
    def test_complete_pilot_timeline_follows_the_run_cycle(self):
        self.assertEqual(
            expected_complete_pilot_steps("icon-ch1", "20260731_1200")[-1],
            "H33",
        )
        self.assertEqual(
            expected_complete_pilot_steps("icon-ch1", "20260731_0300")[-1],
            "H45",
        )

    def test_canonical_xws2_fixture_locks_bytes_decode_and_revision(self):
        fixture_path = (
            Path(__file__).parent
            / "fixtures"
            / "wind_streamline_tiles"
            / "canonical_xws2.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(fixture["fixture_schema_version"], 1)
        self.assertEqual(fixture["contract"], CONTRACT)
        self.assertEqual(fixture["contract_version"], CONTRACT_VERSION)
        self.assertEqual(fixture["package"], PACKAGE)
        self.assertEqual(fixture["generator_revision"], GENERATOR_REVISION)

        profile_record = fixture["profile"]
        profile = ProductionProfile(
            id=profile_record["id"],
            name=profile_record["name"],
            tile_zoom=profile_record["tile_zoom"],
            pixels_per_mercator_unit=profile_record[
                "pixels_per_mercator_unit"
            ],
            geometry=fixture_profile().geometry,
        )
        tile_record = fixture["tile"]
        payload = bytes.fromhex(tile_record["payload_hex"])
        self.assertEqual(len(payload), tile_record["byte_length"])
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            tile_record["sha256"],
        )
        decoded = decode_tile(
            payload,
            profile,
            tuple(tile_record["coordinates"]),
        )
        self.assertEqual(
            [
                {
                    "path_id": fragment.path_id,
                    "fragment_order": fragment.fragment_order,
                    "flags": fragment.flags,
                    "terminal_speed_ms": fragment.terminal_speed_ms,
                    "points": [list(point) for point in fragment.points],
                }
                for fragment in decoded.fragments
            ],
            tile_record["fragments"],
        )

        revision = fixture["revision"]
        canonical_record = json.dumps(
            revision["record"],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(canonical_record).hexdigest()
        self.assertEqual(digest, revision["sha256"])
        self.assertEqual(digest[:16], revision["id"])

    def test_xws2_header_and_fragment_round_trip(self):
        profile = fixture_profile()
        fragments = [
            TileFragment(
                path_id=12,
                fragment_order=0,
                flags=ORIGINAL_START | CONTINUES_AFTER,
                terminal_speed_ms=0,
                points=((0.51, 0.35), (0.515, 0.352), (0.52, 0.354)),
            ),
            TileFragment(
                path_id=18,
                fragment_order=2,
                flags=CONTINUES_BEFORE | ORIGINAL_END,
                terminal_speed_ms=8.12,
                points=((0.51, 0.36), (0.52, 0.365)),
            ),
        ]

        encoded, stats = encode_tile(
            fragments,
            profile,
            (33, 22),
            (0.5, 0.3, 0.6, 0.4),
        )
        decoded = decode_tile(encoded, profile, (33, 22))

        self.assertEqual(HEADER.size, 32)
        self.assertEqual(encoded[:4], b"XWS2")
        self.assertEqual(stats["fragment_count"], 2)
        self.assertEqual(decoded.point_count, 5)
        self.assertEqual(decoded.fragments[0].path_id, 12)
        self.assertEqual(decoded.fragments[1].path_id, 18)
        self.assertEqual(decoded.fragments[1].terminal_speed_ms, 8.12)

    def test_compact_quantization_collapses_duplicate_points(self):
        profile = fixture_profile(zoom=1)
        fragment = TileFragment(
            path_id=3,
            fragment_order=0,
            flags=ORIGINAL_START | ORIGINAL_END,
            terminal_speed_ms=4,
            points=((0.1, 0.1), (0.100001, 0.100001), (0.2, 0.2)),
        )
        encoded, stats = encode_tile(
            [fragment],
            profile,
            (0, 0),
            (0, 0, 1, 1),
            collapse_quantized_duplicates=True,
            quantization_maximum=8_191,
        )
        decoded = decode_tile(
            encoded,
            profile,
            (0, 0),
            quantization_maximum=8_191,
        )

        self.assertEqual(stats["point_count"], 2)
        self.assertEqual(decoded.point_count, 2)
        self.assertLessEqual(max(decoded.fragments[0].points[1]), 8_191)

    def test_compact_variant_is_revisioned_and_validated(self):
        root = temporary_directory()
        self.addCleanup(shutil_rmtree, root)
        metadata_path = write_fixture(root / "source")
        result = build_shadow_package(
            metadata_path,
            root / "compact",
            experiment_variant="recommended",
            quantization_maximum=8_191,
            collapse_quantized_duplicates=True,
            simplify_tolerance_px=0.75,
            trajectory_scales={"lod-detail": 0.8},
        )

        self.assertEqual(result["manifest"]["experiment_variant"], "recommended")
        self.assertEqual(result["manifest"]["quantization_maximum"], 8_191)
        validated = validate_shadow_package(root / "compact")
        self.assertEqual(validated["revision"], result["manifest"]["revision"])

    def test_xws2_rejects_crc_and_tile_identity_mismatch(self):
        profile = fixture_profile()
        fragment = TileFragment(
            path_id=1,
            fragment_order=0,
            flags=ORIGINAL_START | ORIGINAL_END,
            terminal_speed_ms=4,
            points=((0.51, 0.35), (0.52, 0.36)),
        )
        encoded, _ = encode_tile(
            [fragment],
            profile,
            (33, 22),
            (0.5, 0.3, 0.6, 0.4),
        )
        corrupt = bytearray(encoded)
        corrupt[-1] ^= 0x01

        with self.assertRaisesRegex(ValueError, "CRC32"):
            decode_tile(bytes(corrupt), profile, (33, 22))
        with self.assertRaisesRegex(ValueError, "coordinate identity"):
            decode_tile(encoded, profile, (34, 22))

    def test_global_quantization_keeps_tile_boundary_vertex_bit_identical(self):
        profile = fixture_profile(zoom=1)
        path = ProjectedPath(
            path_id=9,
            seed_speed_ms=6,
            points=((0.49, 0.4), (0.51, 0.4)),
        )

        tiles, _ = partition_paths([path], profile, 0.0)

        self.assertEqual(set(tiles), {(0, 0), (1, 0)})
        left, _ = encode_tile(tiles[(0, 0)], profile, (0, 0), (0, 0, 1, 1))
        right, _ = encode_tile(tiles[(1, 0)], profile, (1, 0), (0, 0, 1, 1))
        left_decoded = decode_tile(left, profile, (0, 0))
        right_decoded = decode_tile(right, profile, (1, 0))
        self.assertEqual(
            left_decoded.fragments[0].points[-1],
            right_decoded.fragments[0].points[0],
        )
        self.assertEqual(left_decoded.fragments[0].fragment_order, 0)
        self.assertEqual(right_decoded.fragments[0].fragment_order, 1)

    def test_path_reentry_has_unambiguous_fragment_order(self):
        profile = fixture_profile(zoom=2)
        path = ProjectedPath(
            path_id=5,
            seed_speed_ms=4,
            points=((0.24, 0.4), (0.27, 0.4), (0.24, 0.4)),
        )

        tiles, _ = partition_paths([path], profile, 0.0)
        identities = [
            (fragment.path_id, fragment.fragment_order)
            for fragments in tiles.values()
            for fragment in fragments
        ]

        self.assertEqual(sorted(identities), [(5, 0), (5, 1), (5, 2)])
        self.assertEqual(len(tiles[(0, 1)]), 2)

    def test_projected_scalar_integrator_matches_geographic_oracle(self):
        metadata = fixture_metadata()
        values = np.zeros((20, 20, 2), dtype=np.int8)
        values[:, :, 0] = 20
        profile = fixture_profile()
        feasibility_profile = TileProfile(
            profile.name,
            profile.tile_zoom,
            profile.pixels_per_mercator_unit,
            profile.geometry,
        )
        snapshot = tile_profile_snapshot(feasibility_profile, metadata["grid"])

        expected, expected_stats = integrate_paths_from_snapshot(
            metadata,
            values.tobytes(),
            snapshot,
        )
        actual, actual_stats, _ = integrate_projected_paths(
            metadata,
            values.tobytes(),
            profile,
        )
        vectorized, vectorized_stats, _ = integrate_projected_paths_vectorized(
            metadata,
            values.tobytes(),
            profile,
        )

        self.assertEqual(actual_stats["tested_seeds"], expected_stats["tested_seeds"])
        self.assertEqual(len(actual), len(expected))
        self.assertEqual(vectorized_stats["tested_seeds"], expected_stats["tested_seeds"])
        self.assertEqual(len(vectorized), len(expected))
        self.assertEqual(
            [len(path.points) for path in actual],
            [len(path.points) for path in expected],
        )
        self.assertEqual(
            [len(path.points) for path in vectorized],
            [len(path.points) for path in expected],
        )
        for actual_path, vectorized_path, expected_path in zip(
            actual, vectorized, expected
        ):
            expected_end = (
                longitude_to_mercator_x(expected_path.points[-1][0]),
                latitude_to_mercator_y(expected_path.points[-1][1]),
            )
            self.assertAlmostEqual(actual_path.points[-1][0], expected_end[0])
            self.assertAlmostEqual(actual_path.points[-1][1], expected_end[1])
            self.assertAlmostEqual(vectorized_path.points[-1][0], expected_end[0])
            self.assertAlmostEqual(vectorized_path.points[-1][1], expected_end[1])

    def test_shadow_package_is_deterministic_across_worker_counts(self):
        root = temporary_directory()
        self.addCleanup(shutil_rmtree, root)
        metadata_path = write_fixture(root / "source", ("H00", "H01"))
        first = build_shadow_package(
            metadata_path,
            root / "one-worker",
            steps=("H00", "H01"),
            workers=1,
        )
        second = build_shadow_package(
            metadata_path,
            root / "two-workers",
            steps=("H00", "H01"),
            workers=2,
        )

        self.assertEqual(
            first["manifest"]["revision_sha256"],
            second["manifest"]["revision_sha256"],
        )
        self.assertEqual(first["manifest"]["manifest_layout"], MANIFEST_LAYOUT)
        self.assertEqual(first["manifest"]["simplification_tolerance_px"], 0.5)
        self.assertEqual(
            first["manifest"]["profile_names"],
            [
                "lod-overview",
                "lod-regional",
                "lod-local",
                "lod-detail",
            ],
        )
        self.assertNotIn("profiles", first["manifest"]["steps"][0])
        step_document = json.loads(
            (root / "one-worker" / first["manifest"]["steps"][0]["path"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("profiles", step_document["step"])
        self.assertIn("lod-regional", step_document["step"]["profiles"])
        profiles = step_document["step"]["profiles"]
        scales = [
            profiles[name]["profile"]["lod_control"]["selection_scale"]
            for name in first["manifest"]["profile_names"]
        ]
        self.assertEqual(
            profiles["lod-detail"]["profile"]["lod_control"]["algorithm"],
            "fixed-camera-scale-bands-v1",
        )
        self.assertEqual(
            profiles["lod-detail"]["profile"]["lod_control"]["responsive_modes"],
            ["compact", "wide"],
        )
        for lower, upper in zip(scales, scales[1:]):
            self.assertAlmostEqual(upper / lower, 1.65)
        self.assertEqual(
            profiles["lod-local"]["profile"]["geometry"]["dx_px"],
            14.0,
        )
        self.assertEqual(
            profiles["lod-detail"]["profile"]["geometry"],
            profiles["lod-local"]["profile"]["geometry"],
        )
        validated = validate_shadow_package(root / "one-worker")
        self.assertEqual(validated["counts"], first["manifest"]["counts"])
        self.assertEqual(validated["revision"], first["manifest"]["revision"])

        declared_tile = next(
            (root / "one-worker").glob("profiles/**/*.xws")
        )
        declared_tile.unlink()
        with self.assertRaisesRegex(ValueError, "missing|bytes"):
            validate_shadow_package(root / "one-worker")

    def test_split_step_manifest_is_content_verified(self):
        root = temporary_directory()
        self.addCleanup(shutil_rmtree, root)
        metadata_path = write_fixture(root / "source")
        result = build_shadow_package(metadata_path, root / "package")
        descriptor = result["manifest"]["steps"][0]
        step_path = root / "package" / descriptor["path"]
        step_path.write_bytes(step_path.read_bytes() + b" ")

        with self.assertRaisesRegex(ValueError, "step manifest bytes"):
            validate_shadow_package(root / "package")


def shutil_rmtree(path: Path):
    import shutil

    shutil.rmtree(path)


if __name__ == "__main__":
    unittest.main()
