import json
import math
import tempfile
import unittest
from pathlib import Path

from live_grid.config import StrategyConfig, StrategyConfigError


def valid_config() -> dict:
    return {
        "version": 1,
        "symbol": "rb2601",
        "exchange": "SHFE",
        "target_lots": 1,
        "w_ticks": 20,
        "d_ticks": 20,
        "s_ticks": 10,
        "book_protection_multiple": 2,
        "reanchor_confirmation_seconds": 1,
        "stable_market_seconds": 2,
        "action_limit_per_minute": 60,
        "cancel_timeout_seconds": 10,
        "flatten_timeout_seconds": 3,
        "flatten_adverse_ticks": 10,
    }


class StrategyConfigTests(unittest.TestCase):
    def test_config_normalizes_and_requires_simnow_confirmation(self) -> None:
        config = StrategyConfig.from_mapping(valid_config())

        self.assertEqual(config.effective["exchange"], "SHFE")
        self.assertEqual(config.effective["target_lots"], 1)
        self.assertEqual(len(config.sha256), 64)
        self.assertFalse(config.can_submit(simnow_confirmed=False))
        self.assertTrue(config.can_submit(simnow_confirmed=True))

    def test_config_rejects_credentials_and_invalid_values(self) -> None:
        with self.assertRaisesRegex(StrategyConfigError, "凭证"):
            StrategyConfig.from_mapping(valid_config() | {"password": "secret"})

        with self.assertRaisesRegex(StrategyConfigError, "target_lots"):
            StrategyConfig.from_mapping(valid_config() | {"target_lots": 0})

        with self.assertRaisesRegex(StrategyConfigError, "exchange"):
            StrategyConfig.from_mapping(valid_config() | {"exchange": "UNKNOWN"})

        with self.assertRaisesRegex(StrategyConfigError, "flatten_timeout_seconds"):
            StrategyConfig.from_mapping(valid_config() | {"flatten_timeout_seconds": math.inf})

    def test_config_loads_from_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.json"
            path.write_text(json.dumps(valid_config()), encoding="utf-8")
            config = StrategyConfig.from_json_file(path)
            self.assertEqual(config.effective["symbol"], "rb2601")


if __name__ == "__main__":
    unittest.main()
