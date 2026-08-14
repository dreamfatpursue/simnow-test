import json
import tempfile
import unittest
from pathlib import Path

from live_grid.audit import AuditError, AuditWriter, MultiContractAuditWriter
from live_grid.config import MultiContractConfig, StrategyConfig
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


def multi_config() -> MultiContractConfig:
    return MultiContractConfig.from_mapping(
        {
            "version": 2,
            "contracts": [
                {"symbol": "rb2601", "exchange": "SHFE", "target_lots": 1},
                {"symbol": "AP610", "exchange": "CZCE", "target_lots": 2},
            ],
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


class MultiContractAuditTests(unittest.TestCase):
    def test_run_directory_holds_per_contract_subdirs_and_run_summary(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            strategy = multi_config()
            run_audit = MultiContractAuditWriter(strategy, root)

            self.assertEqual(
                [writer.directory.name for writer in run_audit.writers],
                ["rb2601@SHFE", "AP610@CZCE"],
            )
            run_effective = json.loads((run_audit.directory / "effective_strategy.json").read_text())
            self.assertEqual(run_effective["sha256"], strategy.sha256)
            self.assertEqual(len(run_effective["effective"]["contracts"]), 2)

            for writer, contract in zip(run_audit.writers, strategy.contracts):
                writer.record(ClockEvent(1), [Action("audit", {"value": "ok"})], "WAITING_FOR_CONTRACT", 1)
                writer.finish(
                    {
                        "terminal_state": "FINISHED",
                        "target_symbol": contract.effective["symbol"],
                        "failure_reason": None,
                    }
                )
                self.assertTrue((writer.directory / "events.jsonl").exists())
                self.assertEqual(
                    json.loads((writer.directory / "effective_strategy.json").read_text())["sha256"],
                    strategy.sha256,
                )
                self.assertEqual(
                    json.loads((writer.directory / "summary.json").read_text())["terminal_state"],
                    "FINISHED",
                )

            directory = run_audit.finish(
                {"terminal_states": {"rb2601@SHFE": "FINISHED"}, "strategy_hash": strategy.sha256}
            )
            self.assertTrue((directory / "summary.json").exists())
            content = "\n".join(path.read_text() for path in directory.rglob("*") if path.is_file())
            self.assertNotIn("password", content)
            self.assertNotIn("auth_code", content)


if __name__ == "__main__":
    unittest.main()
