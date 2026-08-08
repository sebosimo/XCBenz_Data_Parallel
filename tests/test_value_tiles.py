from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from value_tiles import (
    CHANNELS,
    CONTRACT,
    CONTRACT_VERSION,
    FINE_GRID,
    LEGACY_FINE_GRID,
    LEGACY_WIND_GRID,
    PACKAGE,
    WIND_GRID,
    GridSpec,
    capability_declaration,
    canonical_json_bytes,
    encode_xvt,
    generate_value_tiles,
    pack_cloud_codes,
    parse_value_tile_run_selection,
    parse_xvt,
    prune_value_tile_manifest,
    sha256_bytes,
    unpack_cloud_codes,
    validate_value_tile_publication,
    value_tiles_enabled,
)


def _temp_workspace() -> Path:
    return Path(tempfile.mkdtemp(prefix="xcb_value_tiles_", dir=os.getenv("TEST_TMPDIR", r"C:\tmp")))


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _grid_payload(grid: GridSpec) -> dict:
    return {
        "projection": "EPSG:4326",
        "width": grid.width,
        "height": grid.height,
        "lon": {
            "start": grid.lon_origin / grid.coordinate_scale,
            "step": grid.lon_step / grid.coordinate_scale,
        },
        "lat": {
            "start": grid.lat_origin / grid.coordinate_scale,
            "step": grid.lat_step / grid.coordinate_scale,
        },
    }


def _write_variant(
    web_root: Path,
    family: str,
    model: str,
    run: str,
    variant: str,
    grid: GridSpec,
    encoding: dict,
    payload: bytes,
    *,
    step: str = "H00",
    valid_time: str = "2026-07-16T03:00:00+00:00",
) -> Path:
    root = web_root / family / model / run / variant
    step_path = root / "steps" / f"{step}.bin"
    step_path.parent.mkdir(parents=True, exist_ok=True)
    step_path.write_bytes(payload)
    metadata = {
        "grid": _grid_payload(grid),
        "encoding": encoding,
        "steps": [
            {
                "step": step,
                "horizon": 0,
                "valid_time": valid_time,
                "url": step_path.as_posix(),
                "byte_length": len(payload),
            }
        ],
    }
    metadata_path = root / "metadata.json"
    _write_json(metadata_path, metadata)
    return metadata_path


def _write_complete_whole_grid(
    web_root: Path,
    *,
    run: str = "20260716_0300",
    wind_grid: GridSpec = WIND_GRID,
    fine_grid: GridSpec = FINE_GRID,
    include_high: bool = True,
) -> None:
    model = "icon-ch1"
    wind_cells = wind_grid.width * wind_grid.height
    fine_cells = fine_grid.width * fine_grid.height
    _write_variant(
        web_root,
        "wind_maps",
        model,
        run,
        "800m_AGL",
        wind_grid,
        {
            "format": "int8-interleaved-u-v",
            "dtype": "int8",
            "components": ["u", "v"],
            "scale_factor": 0.25,
            "add_offset": 0.0,
            "missing_value": -128,
        },
        bytes((index % 127 for index in range(wind_cells * 2))),
    )
    _write_variant(
        web_root,
        "sunrain_maps",
        model,
        run,
        "surface",
        fine_grid,
        {
            "format": "uint8-semantic-sunrain-code",
            "dtype": "uint8",
            "components": ["sunrain_code"],
            "units": ["code"],
            "missing_value": 0,
            "reserved_values": [251, 252, 253, 254, 255],
        },
        bytes((1 + index % 200 for index in range(fine_cells))),
    )
    _write_variant(
        web_root,
        "rain_maps",
        model,
        run,
        "surface",
        fine_grid,
        {
            "format": "uint8-interleaved-components",
            "dtype": "uint8",
            "components": ["precipitation_mm"],
            "units": ["mm"],
            "scale_factor": 0.2,
            "add_offset": 0.0,
            "missing_value": 255,
        },
        bytes((index % 255 for index in range(fine_cells))),
    )
    for offset, layer in enumerate(("total", "low", "mid", "high")):
        if layer == "high" and not include_high:
            continue
        codes = bytes(((index + offset) % 11 for index in range(fine_cells)))
        _write_variant(
            web_root,
            "cloud_maps",
            model,
            run,
            layer,
            fine_grid,
            {
                "format": "packed-uint4-cloud-cover",
                "dtype": "uint8",
                "components": ["cloud_cover_pct"],
                "units": ["%"],
                "bits_per_value": 4,
                "quantization_step_pct": 10,
                "add_offset": 0.0,
                "missing_code": 15,
                "reserved_codes": [11, 12, 13, 14],
                "nibble_order": "even_cell_low_nibble_odd_cell_high_nibble",
            },
            pack_cloud_codes(codes),
        )


class ValueTileContainerTests(unittest.TestCase):
    def test_canonical_tile_fixtures_lock_bytes_and_decoded_contract(self):
        fixture_path = Path(__file__).parent / "fixtures/value_tiles/canonical_tiles.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(fixture["fixture_schema_version"], 1)
        self.assertEqual(fixture["contract"], CONTRACT)
        self.assertEqual(fixture["contract_version"], CONTRACT_VERSION)

        cases = {case["name"]: case for case in fixture["cases"]}
        for name, case in cases.items():
            with self.subTest(case=name):
                payload = bytes.fromhex(case["payload_hex"])
                self.assertEqual(sha256_bytes(payload), case["sha256"])
                parsed = parse_xvt(payload)
                expected = case["expected"]
                self.assertEqual(parsed.flags, expected["flags"])
                self.assertEqual((parsed.tile_x, parsed.tile_y), tuple(expected["tile"]))
                self.assertEqual((parsed.core_width, parsed.core_height), tuple(expected["core"]))
                self.assertEqual(
                    (parsed.valid_core_width, parsed.valid_core_height),
                    tuple(expected["valid_core"]),
                )
                self.assertEqual(
                    (parsed.payload_width, parsed.payload_height),
                    tuple(expected["payload"]),
                )
                self.assertEqual((parsed.grid_width, parsed.grid_height), tuple(expected["grid"]))
                self.assertEqual(
                    [section.channel.name for section in parsed.sections],
                    [section["channel"] for section in expected["sections"]],
                )
                for parsed_section, expected_section in zip(parsed.sections, expected["sections"]):
                    decoded = (
                        unpack_cloud_codes(parsed_section.payload, parsed_section.decoded_cell_count)
                        if parsed_section.channel.is_cloud
                        else parsed_section.payload
                    )
                    self.assertEqual(list(decoded), expected_section["decoded_values"])

        for corruption in fixture["corruption_cases"]:
            with self.subTest(case=corruption["name"]):
                payload = bytearray.fromhex(cases[corruption["source_case"]]["payload_hex"])
                payload[-corruption["byte_offset_from_end"]] ^= corruption["xor"]
                with self.assertRaisesRegex(ValueError, corruption["error"]):
                    parse_xvt(bytes(payload))

    def test_canonical_revision_fixture_locks_digest_and_path_revision(self):
        fixture_path = Path(__file__).parent / "fixtures/value_tiles/canonical_revision.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertEqual(fixture["fixture_schema_version"], 1)
        digest = sha256_bytes(canonical_json_bytes(fixture["record"]))
        self.assertEqual(digest, fixture["revision_sha256"])
        self.assertEqual(digest[:12], fixture["revision"])

    def test_exact_single_channel_tile_and_padding_flag(self):
        grid = GridSpec("test", 3, 2, 100_000, 0, 0, 1, 1, 2, 2)
        payload = encode_xvt(grid, 0, 0, [(CHANNELS["rain"], bytes(range(6)))])
        parsed = parse_xvt(payload)

        self.assertEqual(parsed.tile_x, 0)
        self.assertEqual(parsed.tile_y, 0)
        self.assertEqual(parsed.valid_core_width, 2)
        self.assertEqual(parsed.valid_core_height, 2)
        self.assertEqual(parsed.payload_width, 4)
        self.assertEqual(parsed.payload_height, 4)
        self.assertEqual(parsed.flags, 1)
        self.assertEqual(
            parsed.sections[0].payload,
            bytes(
                [
                    255,
                    255,
                    255,
                    255,
                    255,
                    0,
                    1,
                    2,
                    255,
                    3,
                    4,
                    5,
                    255,
                    255,
                    255,
                    255,
                ]
            ),
        )
        self.assertEqual(
            payload.hex(),
            "5856543101004000010000000000010102000200020002000400040003000000"
            "0200000010000000dc3cbb920000000003000301000000001000000010000000"
            "ffffffffff000102ff030405ffffffff",
        )

    def test_grouped_cloud_sections_keep_independent_nibble_streams(self):
        grid = GridSpec("test", 2, 1, 100_000, 0, 0, 1, 1, 2, 1)
        payload = encode_xvt(
            grid,
            0,
            0,
            [
                (CHANNELS["cloud_total"], bytes([0, 1])),
                (CHANNELS["cloud_low"], bytes([9, 10])),
            ],
        )
        parsed = parse_xvt(payload)

        self.assertEqual(parsed.flags, 3)
        self.assertEqual(
            [section.channel.name for section in parsed.sections],
            ["cloud_total", "cloud_low"],
        )
        self.assertEqual(
            unpack_cloud_codes(parsed.sections[0].payload, 12),
            bytes([15, 15, 15, 15, 15, 0, 1, 15, 15, 15, 15, 15]),
        )
        self.assertEqual(
            unpack_cloud_codes(parsed.sections[1].payload, 12),
            bytes([15, 15, 15, 15, 15, 9, 10, 15, 15, 15, 15, 15]),
        )

    def test_crc_corruption_is_rejected(self):
        grid = GridSpec("test", 2, 2, 100_000, 0, 0, 1, 1, 2, 2)
        payload = bytearray(encode_xvt(grid, 0, 0, [(CHANNELS["sunrain_code"], bytes([1, 2, 3, 4]))]))
        payload[-1] ^= 1
        with self.assertRaisesRegex(ValueError, "CRC-32"):
            parse_xvt(bytes(payload))

    def test_capability_is_opt_in_and_declares_no_range(self):
        self.assertFalse(value_tiles_enabled({}))
        self.assertTrue(value_tiles_enabled({"ENABLE_VALUE_TILES": "true"}))
        capability = capability_declaration()
        self.assertEqual(capability["contract"], CONTRACT)
        self.assertEqual(capability["contract_version"], CONTRACT_VERSION)
        self.assertEqual(capability["package"], PACKAGE)
        self.assertFalse(capability["requires_range"])

    def test_run_selection_parser_is_explicit_and_rejects_ambiguous_values(self):
        self.assertIsNone(parse_value_tile_run_selection(None))
        self.assertEqual(
            parse_value_tile_run_selection(
                "icon-ch1=20260716_1500,icon-ch2=20260716_1200"
            ),
            {
                ("icon-ch1", "20260716_1500"),
                ("icon-ch2", "20260716_1200"),
            },
        )
        with self.assertRaisesRegex(ValueError, "comma-separated"):
            parse_value_tile_run_selection("ch1=latest")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            parse_value_tile_run_selection(
                "icon-ch1=20260716_1500,icon-ch1=20260716_1500"
            )


class ValueTileGenerationTests(unittest.TestCase):
    def test_local_validator_rejects_revision_paths_outside_web_exports(self):
        workspace = _temp_workspace()
        try:
            web_root = workspace / "web_exports"
            manifest = {
                "contract": CONTRACT,
                "contract_version": CONTRACT_VERSION,
                "package": PACKAGE,
                "models": {
                    "icon-ch1": {
                        "runs": {
                            "20260716_0300": {
                                "revision": "000000000000",
                                "revision_sha256": "0" * 64,
                                "revision_record": (workspace / "outside.json").as_posix(),
                                "variants": {},
                            }
                        }
                    }
                },
                "counts": {"models": 1, "runs": 1, "variants": 0, "tiles": 0},
            }
            with self.assertRaisesRegex(ValueError, "escapes its allowed root"):
                validate_value_tile_publication(web_root, manifest=manifest)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def test_generation_is_deterministic_and_preserves_whole_grid_files(self):
        workspace = _temp_workspace()
        try:
            web_root = workspace / "web_exports"
            _write_complete_whole_grid(web_root)
            whole_grid = web_root / "rain_maps/icon-ch1/20260716_0300/surface/steps/H00.bin"
            original = whole_grid.read_bytes()

            first = generate_value_tiles(web_root)
            first_run = first["models"]["icon-ch1"]["runs"]["20260716_0300"]
            counts = validate_value_tile_publication(web_root)
            second = generate_value_tiles(web_root)
            second_run = second["models"]["icon-ch1"]["runs"]["20260716_0300"]

            self.assertEqual(first_run["revision_sha256"], second_run["revision_sha256"])
            self.assertEqual(first["counts"], {"models": 1, "runs": 1, "variants": 8, "tiles": 160})
            self.assertEqual(counts, {"runs": 1, "variants": 8, "steps": 8, "tiles": 160})
            self.assertEqual(whole_grid.read_bytes(), original)
            self.assertEqual(
                set(first_run["variants"]),
                {
                    "wind/800m_AGL",
                    "sunrain/surface",
                    "rain/surface",
                    "cloud/total",
                    "cloud/low",
                    "cloud/mid",
                    "cloud/high",
                    "cloud/cloud4",
                },
            )

            rain_metadata = web_root / "rain_maps/icon-ch1/20260716_0300/surface/metadata.json"
            payload = json.loads(rain_metadata.read_text(encoding="utf-8"))
            payload["steps"][0]["valid_time"] = "2026-07-16T04:00:00+00:00"
            _write_json(rain_metadata, payload)
            changed = generate_value_tiles(web_root)
            changed_run = changed["models"]["icon-ch1"]["runs"]["20260716_0300"]
            self.assertNotEqual(first_run["revision_sha256"], changed_run["revision_sha256"])

            rain_step = web_root / "rain_maps/icon-ch1/20260716_0300/surface/steps/H00.bin"
            rain_bytes = bytearray(rain_step.read_bytes())
            rain_bytes[0] ^= 1
            rain_step.write_bytes(rain_bytes)
            payload_changed = generate_value_tiles(web_root)
            payload_changed_run = payload_changed["models"]["icon-ch1"]["runs"]["20260716_0300"]
            self.assertNotEqual(changed_run["revision_sha256"], payload_changed_run["revision_sha256"])

            revision_root = Path(payload_changed_run["revision_record"].replace("web_exports/", "", 1)).parent
            tile_path = web_root / revision_root / "rain/surface/H00/t0_0.xvt"
            tile_path.unlink()
            with self.assertRaisesRegex(ValueError, "tile file set"):
                validate_value_tile_publication(web_root)

            restored = generate_value_tiles(web_root)
            self.assertEqual(restored["counts"]["tiles"], 160)
            self.assertIsNone(prune_value_tile_manifest(web_root, {"icon-ch1": set()}))
            self.assertFalse((web_root / "value_tiles").exists())
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def test_generation_supports_retained_legacy_and_expanded_grid_runs(self):
        workspace = _temp_workspace()
        try:
            web_root = workspace / "web_exports"
            _write_complete_whole_grid(web_root)
            _write_complete_whole_grid(
                web_root,
                run="20260715_0300",
                wind_grid=LEGACY_WIND_GRID,
                fine_grid=LEGACY_FINE_GRID,
            )
            current_wind_metadata = (
                web_root / "wind_maps/icon-ch1/20260716_0300/800m_AGL/metadata.json"
            )
            current_wind_payload = json.loads(current_wind_metadata.read_text(encoding="utf-8"))
            current_wind_payload["grid"]["lat"]["step"] = 0.03999
            _write_json(current_wind_metadata, current_wind_payload)

            manifest = generate_value_tiles(web_root)

            self.assertEqual(
                manifest["counts"],
                {"models": 1, "runs": 2, "variants": 16, "tiles": 256},
            )
            runs = manifest["models"]["icon-ch1"]["runs"]
            current_record = json.loads(
                (web_root / Path(runs["20260716_0300"]["revision_record"].replace("web_exports/", "", 1)))
                .with_name("revision.json")
                .read_text(encoding="utf-8")
            )
            legacy_record = json.loads(
                (web_root / Path(runs["20260715_0300"]["revision_record"].replace("web_exports/", "", 1)))
                .with_name("revision.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(
                set(current_record["record"]["grids"]),
                {FINE_GRID.id, WIND_GRID.id},
            )
            self.assertEqual(
                set(legacy_record["record"]["grids"]),
                {LEGACY_FINE_GRID.id, LEGACY_WIND_GRID.id},
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def test_incremental_generation_packages_only_selected_runs(self):
        workspace = _temp_workspace()
        try:
            web_root = workspace / "web_exports"
            _write_complete_whole_grid(web_root)
            _write_complete_whole_grid(
                web_root,
                run="20260715_0300",
                wind_grid=LEGACY_WIND_GRID,
                fine_grid=LEGACY_FINE_GRID,
            )

            manifest = generate_value_tiles(
                web_root,
                selected_runs={("icon-ch1", "20260716_0300")},
                validate=False,
            )

            self.assertEqual(
                manifest["counts"],
                {"models": 1, "runs": 1, "variants": 8, "tiles": 160},
            )
            self.assertEqual(
                set(manifest["models"]["icon-ch1"]["runs"]),
                {"20260716_0300"},
            )
            self.assertFalse(
                (web_root / "value_tiles/v1/icon-ch1/20260715_0300").exists()
            )
            with self.assertRaisesRegex(ValueError, "were not discovered"):
                generate_value_tiles(
                    web_root,
                    selected_runs={("icon-ch2", "20260716_1200")},
                    validate=False,
                )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def test_selective_validation_deeply_checks_only_new_runs(self):
        workspace = _temp_workspace()
        try:
            web_root = workspace / "web_exports"
            _write_complete_whole_grid(web_root)
            _write_complete_whole_grid(
                web_root,
                run="20260715_0300",
                wind_grid=LEGACY_WIND_GRID,
                fine_grid=LEGACY_FINE_GRID,
            )
            manifest = generate_value_tiles(web_root)
            legacy_entry = manifest["models"]["icon-ch1"]["runs"]["20260715_0300"]
            legacy_revision = (
                web_root
                / Path(legacy_entry["revision_record"].replace("web_exports/", "", 1))
            ).parent
            legacy_tile = next(legacy_revision.rglob("*.xvt"))
            corrupted = bytearray(legacy_tile.read_bytes())
            corrupted[-1] ^= 1
            legacy_tile.write_bytes(corrupted)

            counts = validate_value_tile_publication(
                web_root,
                full_runs={("icon-ch1", "20260716_0300")},
            )
            self.assertEqual(counts["runs"], 2)
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                validate_value_tile_publication(web_root)
            with self.assertRaisesRegex(ValueError, "absent from the manifest"):
                validate_value_tile_publication(
                    web_root,
                    full_runs={("icon-ch2", "20260716_1200")},
                )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def test_incomplete_cloud_set_is_rejected_without_manifest(self):
        workspace = _temp_workspace()
        try:
            web_root = workspace / "web_exports"
            _write_complete_whole_grid(web_root, include_high=False)
            with self.assertRaisesRegex(ValueError, "all four Cloud layers"):
                generate_value_tiles(web_root)
            self.assertFalse((web_root / "value_tiles/v1/manifest.json").exists())
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def test_staging_host_rules_are_explicit_and_not_deployed(self):
        root = Path(__file__).resolve().parents[1]
        staging = (root / "deploy/infomaniak-value-tiles-staging.htaccess").read_text(encoding="utf-8")
        production = (root / "deploy/infomaniak-data.htaccess").read_text(encoding="utf-8")
        production_deploy = (root / "scripts/deploy_data_infomaniak.sh").read_text(encoding="utf-8")
        staging_deploy = (root / "scripts/deploy_value_tiles_staging_infomaniak.sh").read_text(encoding="utf-8")
        workflow = (root / ".github/workflows/daily_plot.yml").read_text(encoding="utf-8")
        coding_server_env = (root / "deploy/coding-server-pipeline.env.example").read_text(encoding="utf-8")
        self.assertIn("application/octet-stream .bin .xvt", staging)
        self.assertIn("max-age=31536000, immutable", staging)
        cache_block = staging.split('<FilesMatch "\\.(bin|xvt|json|geojson)$">', 1)[1].split(
            "</FilesMatch>", 1
        )[0]
        self.assertLess(cache_block.index("max-age=3600"), cache_block.index("max-age=31536000, immutable"))
        self.assertIn("env=xcbenz_value_tile_immutable", cache_block)
        self.assertIn("AddOutputFilterByType DEFLATE", staging)
        self.assertIn("AddOutputFilterByType BROTLI_COMPRESS", staging)
        self.assertIn("^/value-tiles-staging/web_exports/value_tiles/v1/", staging)
        self.assertIn("application/octet-stream .bin .xvt", production)
        self.assertIn("^/web_exports/value_tiles/v1/", production)
        production_cache_block = production.split('<FilesMatch "\\.(bin|xvt|json|geojson)$">', 1)[1].split(
            "</FilesMatch>", 1
        )[0]
        self.assertLess(
            production_cache_block.index("max-age=3600"),
            production_cache_block.index("max-age=31536000, immutable"),
        )
        self.assertNotIn("infomaniak-value-tiles-staging.htaccess", production_deploy)
        self.assertIn(
            "for subtree in live_stations webcams radar_maps airspace fai_records satellite_cloud_maps; do",
            production_deploy,
        )
        self.assertIn('EXPECTED_REMOTE_ROOT="sites/data.xcbenz.com/value-tiles-staging"', staging_deploy)
        self.assertIn('EXPECTED_BASE_URL="https://data.xcbenz.com/value-tiles-staging"', staging_deploy)
        self.assertIn("refusing remote root", staging_deploy)
        self.assertIn("refusing data URL", staging_deploy)
        self.assertIn("infomaniak-value-tiles-staging.htaccess", staging_deploy)
        self.assertIn("EXPECTED_VALUE_TILES_STATE", staging_deploy)
        self.assertIn("PRODUCTION_WEB_EXPORTS", staging_deploy)
        self.assertIn('retry "release staging publish lock"', staging_deploy)
        self.assertIn("DEPLOY_LOCK_RELEASE_TIMEOUT_SECONDS", staging_deploy)
        self.assertIn("actual_owner", staging_deploy)
        self.assertIn(
            "for subtree in live_stations webcams radar_maps airspace fai_records satellite_cloud_maps; do",
            staging_deploy,
        )
        self.assertNotIn("deploy_data_infomaniak.sh", staging_deploy)
        self.assertIn("ENABLE_VALUE_TILES: ${{ vars.ENABLE_VALUE_TILES || 'false' }}", workflow)
        self.assertIn("ENABLE_VALUE_TILES=false", coding_server_env)


class ValueTileRemoteValidationTests(unittest.TestCase):
    def test_remote_expected_tile_state_rejects_missing_or_unexpected_capability(self):
        from scripts import validate_remote_web_exports as remote

        enabled_manifest = {"capabilities": {"spatial_value_tiles": capability_declaration()}}
        disabled_manifest = {}

        remote.validate_expected_value_tile_state(enabled_manifest, "enabled")
        remote.validate_expected_value_tile_state(disabled_manifest, "disabled")
        remote.validate_expected_value_tile_state(enabled_manifest, "optional")
        with self.assertRaisesRegex(remote.ValidationError, "expected but the capability is absent"):
            remote.validate_expected_value_tile_state(disabled_manifest, "enabled")
        with self.assertRaisesRegex(remote.ValidationError, "expected to be disabled"):
            remote.validate_expected_value_tile_state(enabled_manifest, "disabled")
        with self.assertRaisesRegex(remote.ValidationError, "must be optional, enabled, or disabled"):
            remote.validate_expected_value_tile_state(disabled_manifest, "invalid")

    def test_remote_disabled_state_requires_tile_manifest_http_404(self):
        from urllib.error import HTTPError

        from scripts import validate_remote_web_exports as remote

        url = remote.resolve_url(capability_declaration()["manifest"])
        with mock.patch.object(remote, "urlopen", side_effect=HTTPError(url, 404, "Not Found", {}, None)):
            remote.require_missing(capability_declaration()["manifest"])

        with mock.patch.object(remote, "urlopen", side_effect=HTTPError(url, 403, "Forbidden", {}, None)):
            with self.assertRaisesRegex(remote.ValidationError, "expected HTTP 404"):
                remote.require_missing(capability_declaration()["manifest"])

        response = mock.MagicMock()
        response.__enter__.return_value = response
        with mock.patch.object(remote, "urlopen", return_value=response):
            with self.assertRaisesRegex(remote.ValidationError, "still published"):
                remote.require_missing(capability_declaration()["manifest"])

    def test_remote_smoke_validates_identity_tile_hash_mime_and_cache(self):
        from scripts import validate_remote_web_exports as remote

        grid = GridSpec("test", 2, 2, 100_000, 0, 0, 1, 1, 2, 2)
        tile = encode_xvt(grid, 0, 0, [(CHANNELS["rain"], bytes([1, 2, 3, 4]))])
        logical_path = "rain/surface/H00/t0_0.xvt"
        record = {
            "model": "icon-ch1",
            "run": "20260716_0300",
            "tiles": [
                {
                    "logical_path": logical_path,
                    "byte_length": len(tile),
                    "sha256": sha256_bytes(tile),
                }
            ],
        }
        digest = sha256_bytes(canonical_json_bytes(record))
        revision = digest[:12]
        revision_url = f"web_exports/value_tiles/v1/icon-ch1/20260716_0300/{revision}/revision.json"
        metadata_url = f"web_exports/value_tiles/v1/icon-ch1/20260716_0300/{revision}/rain/surface/metadata.json"
        tile_manifest = {
            "contract": CONTRACT,
            "contract_version": CONTRACT_VERSION,
            "package": PACKAGE,
            "models": {
                "icon-ch1": {
                    "runs": {
                        "20260716_0300": {
                            "revision": revision,
                            "revision_sha256": digest,
                            "revision_record": revision_url,
                            "variants": {"rain/surface": {"metadata": metadata_url}},
                        }
                    }
                }
            },
        }
        wrapper = {"revision": revision, "revision_sha256": digest, "record": record}
        metadata = {
            "tile_matrix": {"url_template": "{step}/t{tile_y}_{tile_x}.xvt"},
            "steps": [{"step": "H00"}],
        }

        def fake_fetch_json(path: str, *, context_url: str | None = None):
            del context_url
            if path == capability_declaration()["manifest"]:
                return tile_manifest, remote.resolve_url(path), {"Cache-Control": "no-cache, must-revalidate"}
            if path == revision_url:
                return wrapper, remote.resolve_url(path), {"Cache-Control": "public, max-age=31536000, immutable"}
            if path == metadata_url:
                return metadata, remote.resolve_url(path), {"Cache-Control": "public, max-age=31536000, immutable"}
            raise AssertionError(path)

        tile_url = remote.resolve_url("H00/t0_0.xvt", context_url=remote.resolve_url(metadata_url))
        with mock.patch.object(remote, "fetch_json", side_effect=fake_fetch_json), mock.patch.object(
            remote,
            "fetch",
            return_value=remote.FetchResult(
                tile_url,
                tile,
                {
                    "Cache-Control": "public, max-age=31536000, immutable",
                    "Content-Type": "application/octet-stream",
                },
            ),
        ) as fetch_mock:
            remote.validate_value_tiles({"capabilities": {"spatial_value_tiles": capability_declaration()}})

        fetch_mock.assert_called_once_with(tile_url)


class ValueTileRetentionIntegrationTests(unittest.TestCase):
    def test_disabled_retention_removes_existing_and_staged_tile_publications(self):
        from scripts import apply_web_retention as retention

        workspace = _temp_workspace()
        try:
            web_root = workspace / "web_exports"
            staging_root = workspace / "web_exports_staging"
            _write_json(web_root / "value_tiles/v1/manifest.json", {"corrupt": "existing"})
            _write_json(staging_root / "value_tiles/v1/manifest.json", {"corrupt": "staged"})
            _write_json(staging_root / "locations.json", {})

            def assert_tiles_removed_before_retention():
                self.assertFalse((web_root / "value_tiles").exists())
                return {}

            with mock.patch.object(retention, "WEB_DIR", web_root), mock.patch.object(
                retention, "STAGING_DIR", staging_root
            ), mock.patch.object(retention, "value_tiles_enabled", return_value=False), mock.patch.object(
                retention, "apply_retention", side_effect=assert_tiles_removed_before_retention
            ), mock.patch.object(retention, "validate_emagram_bundles", return_value=0), mock.patch.object(
                retention, "rebuild_wind_manifest", return_value=None
            ), mock.patch.object(retention, "rebuild_sunshine_manifest", return_value=None), mock.patch.object(
                retention, "rebuild_rain_manifest", return_value=None
            ), mock.patch.object(retention, "rebuild_sunrain_manifest", return_value=None), mock.patch.object(
                retention, "rebuild_cloud_manifest", return_value=None
            ):
                retention.main()

            self.assertFalse((web_root / "value_tiles").exists())
            root_manifest = json.loads((web_root / "manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn("capabilities", root_manifest)
            self.assertEqual(
                root_manifest["products"]["maps"]["satellite_cloud"],
                "web_exports/satellite_cloud_maps/manifest.json",
            )
            self.assertFalse((web_root / "satellite_cloud_maps").exists())
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def test_staging_merge_keeps_old_runs_and_rebuilt_root_advertises_capability(self):
        from scripts import apply_web_retention as retention

        workspace = _temp_workspace()
        try:
            web_root = workspace / "web_exports"
            staging_root = workspace / "web_exports_staging"

            def tile_manifest(run: str, revision: str, tile_count: int) -> dict:
                return {
                    "contract": CONTRACT,
                    "contract_version": CONTRACT_VERSION,
                    "package": PACKAGE,
                    "generated_at": "2026-07-16T03:00:00+00:00",
                    "models": {
                        "icon-ch1": {
                            "runs": {
                                run: {
                                    "run": run,
                                    "revision": revision,
                                    "revision_sha256": revision.ljust(64, "0"),
                                    "revision_record": (
                                        f"web_exports/value_tiles/v1/icon-ch1/{run}/{revision}/revision.json"
                                    ),
                                    "tile_count": tile_count,
                                    "variants": {"rain/surface": {"metadata": "unused"}},
                                }
                            }
                        }
                    },
                    "counts": {"models": 1, "runs": 1, "variants": 1, "tiles": tile_count},
                }

            _write_json(
                web_root / "value_tiles/v1/manifest.json",
                tile_manifest("20260716_0300", "111111111111", 12),
            )
            _write_json(
                staging_root / "value_tiles/v1/manifest.json",
                tile_manifest("20260716_0600", "222222222222", 24),
            )
            _write_json(staging_root / "locations.json", {})

            with mock.patch.object(retention, "WEB_DIR", web_root), mock.patch.object(
                retention, "STAGING_DIR", staging_root
            ):
                retention.merge_staging()
                merged = json.loads((web_root / "value_tiles/v1/manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(
                    set(merged["models"]["icon-ch1"]["runs"]),
                    {"20260716_0300", "20260716_0600"},
                )
                self.assertEqual(merged["counts"]["tiles"], 36)
                retention.rebuild_main_manifest(0, None, None, None, None, None, merged)

            root_manifest = json.loads((web_root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                root_manifest["capabilities"]["spatial_value_tiles"],
                capability_declaration(),
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def test_filesystem_benchmark_counts_and_deletes_only_its_fixture(self):
        from scripts.benchmark_value_tile_filesystem import benchmark_fixture

        workspace = _temp_workspace()
        fixture = workspace / "fixture"
        try:
            result = benchmark_fixture(fixture, file_count=25, payload_bytes=2)
            self.assertEqual(result["file_count"], 25)
            self.assertEqual(result["payload_bytes_per_file"], 2)
            self.assertFalse(fixture.exists())
            self.assertTrue(workspace.exists())
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
