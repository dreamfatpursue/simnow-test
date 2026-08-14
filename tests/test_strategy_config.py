import json
import math
import tempfile
import unittest
from pathlib import Path

from live_grid.config import MultiContractConfig, StrategyConfig, StrategyConfigError


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


def valid_multi_contract_config() -> dict:
    return {
        "version": 2,
        "contracts": [
            {"symbol": "rb2601", "exchange": "shfe", "target_lots": 1},
            {"symbol": "AP610", "exchange": "CZCE", "target_lots": 2},
        ],
        "w_ticks": 25,
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


class MultiContractConfigTests(unittest.TestCase):
    def test_multi_contract_config_builds_per_contract_configs_with_shared_identity(self) -> None:
        config = MultiContractConfig.from_mapping(valid_multi_contract_config())

        self.assertEqual(len(config.contracts), 2)
        first, second = config.contracts
        self.assertEqual(first.effective["symbol"], "rb2601")
        self.assertEqual(first.effective["exchange"], "SHFE")
        self.assertEqual(first.effective["target_lots"], 1)
        self.assertEqual(second.effective["symbol"], "AP610")
        self.assertEqual(second.effective["exchange"], "CZCE")
        self.assertEqual(second.effective["target_lots"], 2)

        self.assertEqual(first.effective["w_ticks"], 25)
        self.assertEqual(second.effective["w_ticks"], 25)
        self.assertEqual(second.effective["d_ticks"], 20)

        self.assertEqual(len(config.sha256), 64)
        self.assertEqual(config.sha256, first.sha256)
        self.assertEqual(config.sha256, second.sha256)
        self.assertEqual(
            [entry["symbol"] for entry in config.effective["contracts"]],
            ["rb2601", "AP610"],
        )

    def test_multi_contract_config_rejects_legacy_format_and_wrong_version(self) -> None:
        with self.assertRaisesRegex(StrategyConfigError, "旧单合约格式已停用.*contracts"):
            MultiContractConfig.from_mapping(valid_config())

        with self.assertRaisesRegex(StrategyConfigError, "缺少策略字段: contracts"):
            MultiContractConfig.from_mapping({"version": 2})

        with self.assertRaisesRegex(StrategyConfigError, "version"):
            MultiContractConfig.from_mapping(
                valid_multi_contract_config() | {"version": 1},
            )

    def test_multi_contract_config_reports_entry_errors_with_index(self) -> None:
        with self.assertRaisesRegex(StrategyConfigError, r"contracts\[1\] 缺少字段: exchange"):
            MultiContractConfig.from_mapping(
                valid_multi_contract_config()
                | {
                    "contracts": [
                        {"symbol": "rb2601", "exchange": "SHFE", "target_lots": 1},
                        {"symbol": "AP610", "target_lots": 2},
                    ]
                }
            )

        with self.assertRaisesRegex(StrategyConfigError, r"contracts\[0\] 未知字段: hedge"):
            MultiContractConfig.from_mapping(
                valid_multi_contract_config()
                | {
                    "contracts": [
                        {"symbol": "rb2601", "exchange": "SHFE", "target_lots": 1, "hedge": True}
                    ]
                }
            )

        with self.assertRaisesRegex(StrategyConfigError, r"contracts\[1\]: target_lots 必须是正整数"):
            MultiContractConfig.from_mapping(
                valid_multi_contract_config()
                | {"contracts": [
                    {"symbol": "rb2601", "exchange": "SHFE", "target_lots": 1},
                    {"symbol": "AP610", "exchange": "CZCE", "target_lots": 0},
                ]}
            )

        with self.assertRaisesRegex(StrategyConfigError, r"contracts\[0\]: exchange 不是有效交易所"):
            MultiContractConfig.from_mapping(
                valid_multi_contract_config()
                | {"contracts": [{"symbol": "rb2601", "exchange": "UNKNOWN", "target_lots": 1}]}
            )

        with self.assertRaisesRegex(StrategyConfigError, r"contracts\[1\]: 重复合约: rb2601@SHFE"):
            MultiContractConfig.from_mapping(
                valid_multi_contract_config()
                | {"contracts": [
                    {"symbol": "rb2601", "exchange": "SHFE", "target_lots": 1},
                    {"symbol": " rb2601 ", "exchange": "shfe", "target_lots": 2},
                ]}
            )

    def test_multi_contract_config_validates_top_level_and_loads_from_json_file(self) -> None:
        with self.assertRaisesRegex(StrategyConfigError, "contracts 必须是非空数组"):
            MultiContractConfig.from_mapping(valid_multi_contract_config() | {"contracts": []})

        with self.assertRaisesRegex(StrategyConfigError, "contracts 必须是非空数组"):
            MultiContractConfig.from_mapping(valid_multi_contract_config() | {"contracts": "rb2601"})

        with self.assertRaisesRegex(StrategyConfigError, "未知策略字段: hedge_symbol"):
            MultiContractConfig.from_mapping(valid_multi_contract_config() | {"hedge_symbol": "AP610"})

        with self.assertRaisesRegex(StrategyConfigError, "^w_ticks 必须是正整数$"):
            MultiContractConfig.from_mapping(valid_multi_contract_config() | {"w_ticks": 0})

        with self.assertRaisesRegex(StrategyConfigError, "flatten_timeout_seconds 必须是正数"):
            MultiContractConfig.from_mapping(
                valid_multi_contract_config() | {"flatten_timeout_seconds": math.inf}
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.json"
            path.write_text(json.dumps(valid_multi_contract_config()), encoding="utf-8")
            config = MultiContractConfig.from_json_file(path)
            self.assertEqual(len(config.contracts), 2)
            self.assertEqual(config.contracts[1].effective["symbol"], "AP610")

            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(StrategyConfigError, "根节点必须是 JSON 对象"):
                MultiContractConfig.from_json_file(path)

    def test_multi_contract_config_accepts_session_controls_with_defaults(self) -> None:
        doc = valid_multi_contract_config()
        config = MultiContractConfig.from_mapping(doc)
        self.assertEqual(config.effective["session_end_time"], "")
        self.assertEqual(config.effective["max_round_trips"], 10)

        config = MultiContractConfig.from_mapping(doc | {"session_end_time": "23:00", "max_round_trips": 3})
        self.assertEqual(config.effective["session_end_time"], "23:00")
        self.assertEqual(config.effective["max_round_trips"], 3)
        self.assertEqual(config.contracts[0].effective["session_end_time"], "23:00")

        with self.assertRaisesRegex(StrategyConfigError, "session_end_time"):
            MultiContractConfig.from_mapping(doc | {"session_end_time": "9点"})
        with self.assertRaisesRegex(StrategyConfigError, "session_end_time"):
            MultiContractConfig.from_mapping(doc | {"session_end_time": "24:30"})
        with self.assertRaisesRegex(StrategyConfigError, "max_round_trips 必须是正整数"):
            MultiContractConfig.from_mapping(doc | {"max_round_trips": 0})


if __name__ == "__main__":
    unittest.main()
