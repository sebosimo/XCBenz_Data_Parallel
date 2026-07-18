import json
import os
import tempfile
import unittest
from pathlib import Path

from web_export_support import (
    CLOUD_MAPS,
    RAIN_MAPS,
    SUNSHINE_MAPS,
    export_split_binary_maps,
    publication_url,
    rebuild_split_binary_manifest,
    relative_publication_path,
    resolve_publication_url,
    scan_split_binary_maps,
)


FIXTURE = Path(__file__).parent / "fixtures" / "web_export_support" / "source_metadata.json"


class WebExportSupportTests(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory(prefix="xcb_web_support_", dir=os.getenv("TEST_TMPDIR", "/tmp"))
        self.previous_cwd = Path.cwd()
        os.chdir(self.workspace.name)

    def tearDown(self):
        os.chdir(self.previous_cwd)
        self.workspace.cleanup()

    def write_source_product(
        self,
        cache_root: Path,
        *,
        run: str = "20260718_0300",
        product: str = "surface",
        write_step: bool = True,
    ) -> Path:
        product_dir = cache_root / "ch1" / run / product
        step_path = product_dir / "steps" / "H00.bin"
        if write_step:
            step_path.parent.mkdir(parents=True, exist_ok=True)
            step_path.write_bytes(b"\x07\x09")
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["steps"][0]["path"] = step_path.as_posix()
        metadata_path = product_dir / "metadata.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(payload), encoding="utf-8")
        return metadata_path

    def test_source_scan_rejects_partial_products_but_keeps_complete_cloud_layers(self):
        rain_root = Path("cache_rain_maps")
        self.write_source_product(rain_root, write_step=False)
        self.assertEqual(scan_split_binary_maps(rain_root, RAIN_MAPS, log=lambda _message: None), {})

        cloud_root = Path("cache_cloud_maps")
        self.write_source_product(cloud_root, product="total")
        self.write_source_product(cloud_root, product="low", write_step=False)
        scanned = scan_split_binary_maps(cloud_root, CLOUD_MAPS, log=lambda _message: None)
        products = scanned["ch1"]["20260718_0300"]["products"]
        self.assertEqual(list(products), ["total"])

    def test_generated_and_retained_surface_manifests_are_byte_identical(self):
        for spec in (SUNSHINE_MAPS, RAIN_MAPS):
            with self.subTest(product=spec.manifest_key):
                cache_root = Path(f"cache_{spec.manifest_key}")
                self.write_source_product(cache_root)
                scanned = scan_split_binary_maps(cache_root, spec, log=lambda _message: None)
                output_root = Path("web_exports") / spec.manifest_key
                exported = export_split_binary_maps(
                    {spec.manifest_key: scanned},
                    spec,
                    output_root=output_root,
                    path_url=lambda path: path.as_posix(),
                    log=lambda _message: None,
                )
                self.assertIsNotNone(exported)
                manifest_path = output_root / "manifest.json"
                generated_bytes = manifest_path.read_bytes()

                rebuilt = rebuild_split_binary_manifest(
                    output_root,
                    spec,
                    path_url=lambda path: path.as_posix(),
                )
                self.assertIsNotNone(rebuilt)
                self.assertEqual(manifest_path.read_bytes(), generated_bytes)
                self.assertEqual(
                    generated_bytes,
                    (json.dumps(rebuilt, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode(),
                )

    def test_retention_reindexes_metadata_without_reapplying_source_completeness(self):
        output_root = Path("web_exports/rain_maps")
        metadata_path = output_root / "icon-ch1" / "20260718_0300" / "surface" / "metadata.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps(
                {
                    "source": "cache_rain_maps/ch1/20260718_0300/surface/metadata.json",
                    "encoding": {"components": ["precipitation_mm"]},
                    "grid": {"width": 2, "height": 1},
                    "steps": [{"step": "H00", "url": "missing.bin", "byte_length": 2}],
                }
            ),
            encoding="utf-8",
        )

        manifest = rebuild_split_binary_manifest(
            output_root,
            RAIN_MAPS,
            path_url=lambda path: path.as_posix(),
        )
        self.assertEqual(manifest["counts"], {"runs": 1, "products": 1, "steps": 1, "bytes": 2})

    def test_invalid_metadata_is_logged_and_skipped(self):
        metadata_path = Path("cache_rain_maps/ch1/20260718_0300/surface/metadata.json")
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text("{invalid", encoding="utf-8")
        messages = []
        self.assertEqual(scan_split_binary_maps("cache_rain_maps", RAIN_MAPS, log=messages.append), {})
        self.assertEqual(len(messages), 1)
        self.assertIn("Skipping invalid rain-map metadata", messages[0])

    def test_publication_paths_are_named_resolved_and_contained(self):
        root = Path("web_exports")
        contained = root / "rain_maps" / "manifest.json"
        self.assertEqual(relative_publication_path(root, contained), Path("rain_maps/manifest.json"))
        self.assertEqual(publication_url(root, contained), "web_exports/rain_maps/manifest.json")
        self.assertEqual(
            resolve_publication_url(root, "web_exports/rain_maps/manifest.json"),
            contained,
        )
        with self.assertRaisesRegex(ValueError, "escapes publication root"):
            relative_publication_path(root, Path("manifest.json"))


if __name__ == "__main__":
    unittest.main()
