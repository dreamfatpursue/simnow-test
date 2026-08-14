import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_live_grid
from live_grid.config import MultiContractConfig


def multi_strategy_doc() -> dict:
    return {
        "version": 2,
        "contracts": [
            {"symbol": "rb2601", "exchange": "SHFE", "target_lots": 1},
            {"symbol": "AP610", "exchange": "CZCE", "target_lots": 2},
        ],
    }


def write_config(root: str, doc: dict) -> Path:
    config_path = Path(root) / "strategy.json"
    config_path.write_text(json.dumps(doc), encoding="utf-8")
    return config_path


class RunLiveGridTests(unittest.TestCase):
    def test_startup_failure_writes_complete_run_summary(self) -> None:
        with tempfile.TemporaryDirectory() as config_root, tempfile.TemporaryDirectory() as audit_root:
            config_path = write_config(config_root, multi_strategy_doc())
            argv = [
                "run_live_grid.py",
                "--config",
                str(config_path),
                "--confirm-simnow",
                "--audit-dir",
                audit_root,
            ]
            with patch.object(sys, "argv", argv), patch.object(
                run_live_grid,
                "load_settings",
                side_effect=RuntimeError("missing credentials"),
            ) as load_settings:
                self.assertEqual(run_live_grid.main(), 3)
            load_settings.assert_called_once()

            run_directories = list(Path(audit_root).iterdir())
            self.assertEqual(len(run_directories), 1)
            run_directory = run_directories[0]
            run_summary = json.loads((run_directory / "summary.json").read_text())
            self.assertEqual(run_summary["failure_reason"], "missing credentials")
            self.assertEqual(run_summary["terminal_state"], "FAILED")
            self.assertEqual(
                run_summary["terminal_states"],
                {"rb2601@SHFE": "FAILED", "AP610@CZCE": "FAILED"},
            )
            self.assertEqual(run_summary["strategy_hash"], MultiContractConfig.from_mapping(multi_strategy_doc()).sha256)

            contract_directory = run_directory / "rb2601@SHFE"
            summary = json.loads((contract_directory / "summary.json").read_text())
            self.assertEqual(summary["terminal_state"], "FAILED")
            self.assertEqual(summary["failure_reason"], "missing credentials")
            for key in (
                "startup_position_result",
                "first_fill",
                "cancellation_terminal",
                "closing_position_request_id",
                "closing_position_result",
                "flatten_attempts",
                "final_net_position",
                "active_order_count",
                "active_orders",
            ):
                self.assertIn(key, summary)
            self.assertEqual(summary["active_order_count"], 0)
            self.assertEqual(summary["active_orders"], [])
            self.assertTrue((contract_directory / "events.jsonl").exists())

    def test_missing_confirmation_runs_preview_without_connecting(self) -> None:
        with tempfile.TemporaryDirectory() as config_root, tempfile.TemporaryDirectory() as audit_root:
            config_path = write_config(config_root, multi_strategy_doc())
            argv = [
                "run_live_grid.py",
                "--config",
                str(config_path),
                "--audit-dir",
                audit_root,
            ]
            with patch.object(sys, "argv", argv), patch.object(
                run_live_grid,
                "load_settings",
                side_effect=AssertionError("预览模式不得加载凭证"),
            ) as load_settings:
                self.assertEqual(run_live_grid.main(), 0)
            load_settings.assert_not_called()

            run_directory = list(Path(audit_root).iterdir())[0]
            run_summary = json.loads((run_directory / "summary.json").read_text())
            self.assertEqual(
                run_summary["terminal_states"],
                {"rb2601@SHFE": "PREVIEW", "AP610@CZCE": "PREVIEW"},
            )
            self.assertEqual(run_summary["failure_reason"], "confirmation_required")
            for name in ("rb2601@SHFE", "AP610@CZCE"):
                summary = json.loads((run_directory / name / "summary.json").read_text())
                self.assertEqual(summary["terminal_state"], "PREVIEW")
                self.assertEqual(summary["failure_reason"], "confirmation_required")

    def test_legacy_single_contract_config_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as config_root, tempfile.TemporaryDirectory() as audit_root:
            config_path = write_config(
                config_root,
                {
                    "version": 1,
                    "symbol": "rb2601",
                    "exchange": "SHFE",
                    "target_lots": 1,
                },
            )
            argv = [
                "run_live_grid.py",
                "--config",
                str(config_path),
                "--confirm-simnow",
                "--audit-dir",
                audit_root,
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(run_live_grid.main(), 2)
            self.assertEqual(list(Path(audit_root).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
