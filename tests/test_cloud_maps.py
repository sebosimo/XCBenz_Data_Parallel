import datetime
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from cloud_maps import (
    CLOUD_MISSING_CODE,
    CLOUD_QUANTIZATION_STEP_PCT,
    CLOUD_RESERVED_CODES,
    CloudMapAccumulator,
    is_cloud_maps_enabled,
    is_cloud_run_complete,
    pack_cloud_codes,
    quantize_cloud_cover_codes,
    unpack_cloud_codes,
)
from generate_combined_manifest import scan_cloud_maps


def _temp_workspace():
    return tempfile.mkdtemp(prefix="xcb_cloud_", dir=os.getenv("TEST_TMPDIR", r"C:\tmp"))


def _config():
    return SimpleNamespace(
        crop={"lon_min": 0.0, "lon_max": 2.0, "lat_min": 0.0, "lat_max": 0.0},
        grid_spacing_deg=1.0,
        source_padding_deg=0.0,
        horizon_stride=1,
    )


def _prepared_accumulator(tmp, model="ch1"):
    ref = datetime.datetime(2026, 6, 11, 3, tzinfo=datetime.timezone.utc)
    acc = CloudMapAccumulator(model, "20260611_0300", ref, _config(), log=lambda *_: None, out_root=tmp)
    acc.target_lat = np.asarray([[0.0, 0.0, 0.0]], dtype=np.float32)
    acc.target_lon = np.asarray([[0.0, 1.0, 2.0]], dtype=np.float32)
    acc.source_indices = np.arange(3)
    acc.weights = SimpleNamespace(apply=lambda values: np.asarray(values, dtype=np.float32).reshape(1, 3))
    acc.prepared = True
    return acc, ref


class CloudMapTests(unittest.TestCase):
    def test_flags_are_off_by_default_and_model_scoped(self):
        self.assertFalse(is_cloud_maps_enabled("ch1", env={}))
        self.assertFalse(is_cloud_maps_enabled("ch1", env={"ENABLE_CLOUD_MAPS": "true"}))
        self.assertTrue(
            is_cloud_maps_enabled(
                "ch1",
                env={"ENABLE_CLOUD_MAPS": "true", "ENABLE_CLOUD_MAPS_CH1": "true"},
            )
        )
        self.assertFalse(
            is_cloud_maps_enabled(
                "ch1",
                env={"ENABLE_CLOUD_MAPS": "true", "ENABLE_CLOUD_MAPS_CH1": "false"},
            )
        )

    def test_quantization_pack_unpack_and_padding(self):
        codes = quantize_cloud_cover_codes(np.asarray([0.0, 4.9, 5.0, 14.9, 95.0, 100.0, np.nan]))
        self.assertEqual(codes.tolist(), [0, 0, 1, 1, 10, 10, CLOUD_MISSING_CODE])

        packed = pack_cloud_codes(np.asarray([0, 1, 10], dtype="u1"))
        self.assertEqual(packed.tolist(), [0x10, 0xFA])
        self.assertEqual(unpack_cloud_codes(packed, 3).tolist(), [0, 1, 10])
        self.assertEqual((packed[-1] >> 4).item(), CLOUD_MISSING_CODE)

        with self.assertRaises(ValueError):
            pack_cloud_codes(np.asarray([11], dtype="u1"))

    def test_accumulator_writes_four_layer_products(self):
        tmp = _temp_workspace()
        try:
            acc, ref = _prepared_accumulator(tmp)
            self.assertTrue(
                acc.append(
                    object(),
                    {
                        "CLCT": np.asarray([0.0, 14.0, 100.0], dtype=np.float32),
                        "CLCL": np.asarray([np.nan, 44.0, 55.0], dtype=np.float32),
                        "CLCM": np.asarray([25.0, 50.0, 75.0], dtype=np.float32),
                        "CLCH": np.asarray([101.0, -5.0, 9.0], dtype=np.float32),
                    },
                    2,
                    ref,
                )
            )
            result = acc.finalize()
            self.assertEqual(result["files"], 8)

            total_step = Path(tmp) / "ch1" / "20260611_0300" / "total" / "steps" / "H02.bin"
            self.assertEqual(total_step.read_bytes(), bytes([0x10, 0xFA]))

            low_step = Path(tmp) / "ch1" / "20260611_0300" / "low" / "steps" / "H02.bin"
            self.assertEqual(low_step.read_bytes(), bytes([0x4F, 0xF6]))

            metadata_path = Path(tmp) / "ch1" / "20260611_0300" / "total" / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["product"], "cloud_map_layer")
            self.assertEqual(metadata["source_variable"], "CLCT")
            self.assertEqual(metadata["encoding"]["format"], "packed-uint4-cloud-cover")
            self.assertEqual(metadata["encoding"]["bits_per_value"], 4)
            self.assertEqual(metadata["encoding"]["quantization_step_pct"], CLOUD_QUANTIZATION_STEP_PCT)
            self.assertEqual(metadata["encoding"]["missing_code"], CLOUD_MISSING_CODE)
            self.assertEqual(metadata["encoding"]["reserved_codes"], CLOUD_RESERVED_CODES)
            self.assertEqual(metadata["encoding"]["nibble_order"], "even_cell_low_nibble_odd_cell_high_nibble")
            self.assertEqual(metadata["steps"][0]["byte_length"], 2)
            self.assertTrue(is_cloud_run_complete("ch1", "20260611_0300", root=tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_manifest_scan_and_web_export_copy_cloud_layers(self):
        tmp = _temp_workspace()
        try:
            acc, ref = _prepared_accumulator(tmp)
            acc.append(
                object(),
                {
                    "CLCT": np.asarray([0.0, 10.0, 20.0], dtype=np.float32),
                    "CLCL": np.asarray([30.0, 40.0, 50.0], dtype=np.float32),
                    "CLCM": np.asarray([60.0, 70.0, 80.0], dtype=np.float32),
                    "CLCH": np.asarray([90.0, 100.0, np.nan], dtype=np.float32),
                },
                0,
                ref,
            )
            acc.finalize()

            manifest = scan_cloud_maps(tmp)
            products = manifest["ch1"]["20260611_0300"]["products"]
            self.assertEqual(set(products), {"total", "low", "mid", "high"})
            self.assertEqual(products["total"]["step_count"], 1)

            import generate_web_exports

            web_dir = Path(tmp) / "web_exports"
            with mock.patch.object(generate_web_exports, "WEB_DIR", web_dir), mock.patch.object(
                generate_web_exports,
                "WEB_URL_PREFIX",
                "web_exports",
            ), mock.patch.object(generate_web_exports, "CLOUD_WEB_DIR", web_dir / "cloud_maps"):
                exported = generate_web_exports.export_cloud_maps({"cloud_maps": manifest})

            self.assertIsNotNone(exported)
            self.assertEqual(exported["counts"]["products"], 4)
            self.assertEqual(exported["counts"]["steps"], 4)
            output_step = web_dir / "cloud_maps" / "icon-ch1" / "20260611_0300" / "total" / "steps" / "H00.bin"
            self.assertTrue(output_step.exists())
            output_metadata = web_dir / "cloud_maps" / "icon-ch1" / "20260611_0300" / "total" / "metadata.json"
            output_payload = json.loads(output_metadata.read_text(encoding="utf-8"))
            self.assertEqual(output_payload["steps"][0]["url"], "web_exports/cloud_maps/icon-ch1/20260611_0300/total/steps/H00.bin")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_validation_rejects_reserved_cloud_codes(self):
        tmp = _temp_workspace()
        old_cwd = os.getcwd()
        try:
            os.chdir(tmp)
            _write_minimal_map_fixtures(Path("web_exports"))
            cloud_step = Path("web_exports/cloud_maps/icon-ch1/20260611_0300/total/steps/H00.bin")
            cloud_step.write_bytes(bytes([0xB0, 0xFF]))

            from scripts.validate_outputs import validate_map_encodings

            with self.assertRaisesRegex(ValueError, "reserved code 11"):
                validate_map_encodings()
        finally:
            os.chdir(old_cwd)
            shutil.rmtree(tmp, ignore_errors=True)


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_minimal_map_fixtures(root):
    for product, encoding in (
        (
            "wind_maps",
            {"format": "int8-interleaved-u-v", "dtype": "int8", "missing_value": -128},
        ),
        (
            "sunshine_maps",
            {
                "format": "uint8-interleaved-components",
                "dtype": "uint8",
                "components": ["sunshine_fraction_pct"],
                "missing_value": 255,
            },
        ),
        (
            "rain_maps",
            {
                "format": "uint8-interleaved-components",
                "dtype": "uint8",
                "components": ["precipitation_mm"],
                "units": ["mm"],
                "missing_value": 255,
            },
        ),
        (
            "sunrain_maps",
            {
                "format": "uint8-semantic-sunrain-code",
                "dtype": "uint8",
                "components": ["sunrain_code"],
                "units": ["code"],
                "missing_value": 0,
                "reserved_values": [251, 252, 253, 254, 255],
            },
        ),
    ):
        base = root / product / "icon-ch1" / "20260611_0300" / "surface"
        step = base / "steps" / "H00.bin"
        step.parent.mkdir(parents=True, exist_ok=True)
        step.write_bytes(bytes([1, 2, 3, 4]))
        steps = [{"step": "H00", "url": step.as_posix(), "byte_length": 4}]
        _write_json(base / "metadata.json", {"encoding": encoding, "grid": {"width": 2, "height": 2}, "steps": steps})

    cloud_base = root / "cloud_maps" / "icon-ch1" / "20260611_0300" / "total"
    cloud_step = cloud_base / "steps" / "H00.bin"
    cloud_step.parent.mkdir(parents=True, exist_ok=True)
    cloud_step.write_bytes(bytes([0x10, 0x32]))
    _write_json(
        cloud_base / "metadata.json",
        {
            "encoding": {
                "format": "packed-uint4-cloud-cover",
                "dtype": "uint8",
                "components": ["cloud_cover_pct"],
                "units": ["%"],
                "bits_per_value": 4,
                "quantization_step_pct": 10,
                "missing_code": 15,
            },
            "grid": {"width": 2, "height": 2},
            "steps": [{"step": "H00", "url": cloud_step.as_posix(), "byte_length": 2}],
        },
    )


if __name__ == "__main__":
    unittest.main()
