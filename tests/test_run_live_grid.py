import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_live_grid
from live_grid.config import StrategyConfig


def config() -> StrategyConfig:
    return StrategyConfig.from_mapping(
        {
            "version": 1,
            "symbol": "rb2601",
            "exchange": "SHFE",
            "target_lots": 1,
        }
    )


class RunLiveGridTests(unittest.TestCase):
    def test_startup_failure_writes_complete_safety_summary(self) -> None:
        strategy = config()
        with tempfile.TemporaryDirectory() as config_root, tempfile.TemporaryDirectory() as audit_root:
            config_path = Path(config_root) / "strategy.json"
            config_path.write_text(json.dumps(strategy.effective), encoding="utf-8")
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
            summary = json.loads((run_directories[0] / "summary.json").read_text())
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


if __name__ == "__main__":
    unittest.main()
