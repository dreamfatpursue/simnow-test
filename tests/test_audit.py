import json
import tempfile
import unittest
from pathlib import Path

from live_grid.audit import AuditError, AuditWriter
from live_grid.config import StrategyConfig
from live_grid.session import Action, ClockEvent


def config() -> StrategyConfig:
    return StrategyConfig.from_mapping(
        {
            "version": 1,
            "symbol": "rb2601",
            "exchange": "SHFE",
            "target_lots": 1,
        }
    )


class AuditTests(unittest.TestCase):
    def test_run_has_isolated_config_events_and_summary_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            writer = AuditWriter(config(), root)
            writer.record(
                ClockEvent(1),
                [Action("audit", {"value": "ok"})],
                "WAITING_FOR_CONTRACT",
                1,
                state_before="PREVIEW",
            )
            directory = writer.finish({"terminal_state": "FINISHED", "failure_reason": None})

            self.assertTrue(directory.is_dir())
            self.assertEqual(json.loads((directory / "effective_strategy.json").read_text())["sha256"], config().sha256)
            self.assertEqual(len((directory / "events.jsonl").read_text().splitlines()), 1)
            event = json.loads((directory / "events.jsonl").read_text())
            self.assertEqual(event["state_before"], "PREVIEW")
            self.assertEqual(event["state_after"], "WAITING_FOR_CONTRACT")
            summary = json.loads((directory / "summary.json").read_text())
            self.assertEqual(summary["terminal_state"], "FINISHED")
            content = "\n".join(path.read_text() for path in directory.iterdir())
            self.assertNotIn("password", content)
            self.assertNotIn("auth_code", content)

    def test_credential_keys_are_rejected_at_audit_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            writer = AuditWriter(config(), root)
            with self.assertRaises(AuditError):
                writer.record({"password": "secret"}, [], "PREVIEW", 0)
            with self.assertRaises(AuditError):
                writer.record({"密码": "secret"}, [], "PREVIEW", 0)
            writer.close()


if __name__ == "__main__":
    unittest.main()
