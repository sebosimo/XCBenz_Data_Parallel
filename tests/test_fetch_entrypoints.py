import os
import unittest
from unittest import mock

import fetch_data
import fetch_data_ch2
from forecast_fetch.config import FetchConfigError


class FetchEntrypointTests(unittest.TestCase):
    def test_invalid_owned_config_fails_before_output_directory_or_log_mutation(self):
        for module in (fetch_data, fetch_data_ch2):
            with self.subTest(module=module.__name__), mock.patch.dict(
                os.environ,
                {"FORCE_REFRESH": "invalid"},
                clear=True,
            ), mock.patch.object(module.os, "makedirs") as makedirs, mock.patch.object(module, "log") as log:
                with self.assertRaisesRegex(FetchConfigError, "FORCE_REFRESH"):
                    module.main()
                makedirs.assert_not_called()
                log.assert_not_called()

    def test_cli_constants_are_owned_by_the_pure_model_policy(self):
        self.assertEqual((fetch_data.HHL_FILENAME, fetch_data.HGRID_FILENAME), fetch_data.CH1_POLICY.static_assets)
        self.assertEqual(fetch_data_ch2.COLLECTION_CH2, fetch_data_ch2.CH2_POLICY.collection)
        self.assertEqual(
            (fetch_data_ch2.HHL_FILENAME, fetch_data_ch2.HGRID_FILENAME),
            fetch_data_ch2.CH2_POLICY.static_assets,
        )

    def test_supported_wrappers_parse_the_legacy_environment_then_delegate(self):
        for module, policy in (
            (fetch_data, fetch_data.CH1_POLICY),
            (fetch_data_ch2, fetch_data_ch2.CH2_POLICY),
        ):
            with self.subTest(module=module.__name__), mock.patch.dict(
                os.environ,
                {},
                clear=True,
            ), mock.patch.object(module, "execute_fetch") as execute:
                module.main()
                runtime, startup = execute.call_args.args
                self.assertIs(runtime.policy, policy)
                self.assertEqual(startup.model, policy.model)
                self.assertIs(runtime.fetch_variable_files, module.fetch_variable_files)


if __name__ == "__main__":
    unittest.main()
