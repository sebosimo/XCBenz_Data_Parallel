import copy
import json
from pathlib import Path
import unittest

from generate_web_exports import SCHEMA_VERSION
from rain_maps import RAIN_COMPONENTS, RAIN_SCHEMA_VERSION


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "web_contracts"


def load_fixture(name):
    with (FIXTURE_DIR / name).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def require(value, expected_type, path):
    if not isinstance(value, expected_type):
        raise AssertionError(f"{path} must be {expected_type.__name__}")
    return value


def validate_step(value, path):
    step = require(value, dict, path)
    require(step.get("step"), str, f"{path}.step")
    require(step.get("horizon"), int, f"{path}.horizon")
    require(step.get("valid_time"), str, f"{path}.valid_time")
    require(step.get("url"), str, f"{path}.url")
    require(step.get("byte_length"), int, f"{path}.byte_length")


def validate_root_manifest(root):
    require(root, dict, "$")
    if root.get("schema_version") != SCHEMA_VERSION:
        raise AssertionError("$.schema_version must match the web-export producer")
    require(root.get("generated_at"), str, "$.generated_at")
    urls = require(root.get("urls"), dict, "$.urls")
    require(urls.get("locations"), str, "$.urls.locations")
    models = require(root.get("models"), dict, "$.models")
    for model_key, model_value in models.items():
        runs = require(require(model_value, dict, f"$.models.{model_key}").get("runs"), dict, f"$.models.{model_key}.runs")
        for run_key, run_value in runs.items():
            locations = require(require(run_value, dict, f"$.models.{model_key}.runs.{run_key}").get("locations"), dict, f"$.models.{model_key}.runs.{run_key}.locations")
            for location_key, location_value in locations.items():
                path = f"$.models.{model_key}.runs.{run_key}.locations.{location_key}"
                location = require(location_value, dict, path)
                require(location.get("display_name"), str, f"{path}.display_name")
                steps = require(location.get("steps"), list, f"{path}.steps")
                for index, step in enumerate(steps):
                    require(step, str, f"{path}.steps[{index}]")


def validate_rain_manifest(root):
    require(root, dict, "$")
    if root.get("schema_version") != SCHEMA_VERSION:
        raise AssertionError("$.schema_version must match the web-export producer")
    if root.get("product") != "rain_maps":
        raise AssertionError('$.product must be "rain_maps"')
    models = require(root.get("models"), dict, "$.models")
    product = models["icon-ch1"]["runs"]["20260718_0000"]["products"]["surface"]
    grid = require(product.get("grid"), dict, "$.models.icon-ch1.runs.20260718_0000.products.surface.grid")
    require(grid.get("width"), int, "$.models.icon-ch1.runs.20260718_0000.products.surface.grid.width")
    require(grid.get("height"), int, "$.models.icon-ch1.runs.20260718_0000.products.surface.grid.height")
    for index, step in enumerate(require(product.get("steps"), list, "$.models.icon-ch1.runs.20260718_0000.products.surface.steps")):
        validate_step(step, f"$.models.icon-ch1.runs.20260718_0000.products.surface.steps[{index}]")


def validate_rain_metadata(root):
    require(root, dict, "$")
    if root.get("schema_version") != RAIN_SCHEMA_VERSION:
        raise AssertionError("$.schema_version must match the rain producer")
    if root.get("product") != "rain_map_surface":
        raise AssertionError('$.product must be "rain_map_surface"')
    encoding = require(root.get("encoding"), dict, "$.encoding")
    if encoding.get("components") != RAIN_COMPONENTS:
        raise AssertionError("$.encoding.components must match the rain producer")
    grid = require(root.get("grid"), dict, "$.grid")
    require(grid.get("width"), int, "$.grid.width")
    require(grid.get("height"), int, "$.grid.height")
    for index, step in enumerate(require(root.get("steps"), list, "$.steps")):
        validate_step(step, f"$.steps[{index}]")


class WebContractFixtureTests(unittest.TestCase):
    def test_backend_owned_contract_examples_match_producer_constants(self):
        validate_root_manifest(load_fixture("root_manifest.json"))
        validate_rain_manifest(load_fixture("rain_manifest.json"))
        validate_rain_metadata(load_fixture("rain_metadata.json"))

    def test_representative_nested_mutation_fails_the_backend_contract(self):
        fixture = copy.deepcopy(load_fixture("rain_manifest.json"))
        fixture["models"]["icon-ch1"]["runs"]["20260718_0000"]["products"]["surface"]["steps"][0]["byte_length"] = "2"
        with self.assertRaisesRegex(AssertionError, r"steps\[0\]\.byte_length must be int"):
            validate_rain_manifest(fixture)


if __name__ == "__main__":
    unittest.main()
