import json
import tempfile
import unittest
from pathlib import Path

from live_grid.audit import AuditError, AuditEventDecoder, AuditWriter, MultiContractAuditWriter
from live_grid.config import MultiContractConfig, StrategyConfig
from live_grid.session import (
    Action,
    ClockEvent,
    ContractEvent,
    OrderEvent,
    OrderQueryCompleteEvent,
    TradeEvent,
    TradeQueryCompleteEvent,
)


def config() -> StrategyConfig:
    return StrategyConfig.from_mapping(
        {
            "version": 1,
            "symbol": "rb2601",
            "exchange": "SHFE",
            "target_lots": 1,
            "max_tick_age_seconds": 60,
            "quote_windows": [{"start": "00:00", "end": "23:59"}],
        }
    )


def multi_config() -> MultiContractConfig:
    return MultiContractConfig.from_mapping(
        {
            "version": 2,
            "contracts": [
                {
                    "symbol": "rb2601", "exchange": "SHFE", "target_lots": 1,
                    "w_ticks": 25, "max_round_trips": 3,
                    "max_tick_age_seconds": 60,
                    "quote_windows": [{"start": "00:00", "end": "23:59"}],
                },
                {
                    "symbol": "AP610", "exchange": "CZCE", "target_lots": 2,
                    "w_ticks": 30, "max_round_trips": 5,
                    "max_tick_age_seconds": 60,
                    "quote_windows": [{"start": "00:00", "end": "23:59"}],
                },
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

    def test_exchange_time_and_contract_size_persist_in_events_log(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            writer = AuditWriter(config(), root)
            writer.record(
                ContractEvent("rb2601", "SHFE", 1.0, size=10.0),
                [],
                "WAITING_FOR_ZERO_POSITION",
                1,
            )
            writer.record(
                TradeEvent("order-1", "rb2601", "SHFE", "BUY", 1, 3410.0, "trade-1", exchange_time="2026-08-17T21:03:05+08:00"),
                [],
                "QUOTING",
                2,
            )
            writer.finish({"terminal_state": "FINISHED", "failure_reason": None})

            lines = (writer.directory / "events.jsonl").read_text().splitlines()
            contract = json.loads(lines[0])["event"]["data"]
            trade = json.loads(lines[1])["event"]["data"]
            self.assertEqual(contract["size"], 10.0)
            self.assertEqual(trade["exchange_time"], "2026-08-17T21:03:05+08:00")

    def test_repeated_query_snapshots_round_trip_and_trade_identity_is_removed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            writer = AuditWriter(config(), root)
            order = OrderEvent(
                "order-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1,
                traded=0, price=3400.0, client_id="quote-1-buy",
            )
            order_trace = [{"code": "query_checked", "calculation": {"count": 1}}]
            for request_id, at in (("orders-1", 1.0), ("orders-2", 2.0)):
                writer.record(
                    OrderQueryCompleteEvent(request_id, "rb2601", "SHFE", (order,)),
                    [Action("query_position", {"request_id": request_id})],
                    "RISK_HOLD",
                    at,
                    trace=order_trace,
                )

            raw_trade = {
                "BrokerID": "broker-secret",
                "InvestorID": "investor-secret",
                "UserID": "user-secret",
                "InstrumentID": "rb2601",
                "ExchangeID": "SHFE",
                "TradeID": "trade-1",
                "OrderRef": "order-ref-1",
                "TradeDate": "20260928",
                "TradeTime": "09:30:00",
                "Direction": "0",
                "OffsetFlag": "0",
                "HedgeFlag": "1",
                "Price": 3400.0,
                "Volume": 1,
                "UnrecognizedPrivateField": "must-not-persist",
            }
            trade_trace = [{"code": "trade_query_checked", "calculation": {"count": 1}}]
            for request_id, at in (("trades-1", 3.0), ("trades-2", 4.0)):
                writer.record(
                    TradeQueryCompleteEvent(
                        request_id, "rb2601", "SHFE", (raw_trade,), error_id=0,
                    ),
                    [],
                    "RISK_HOLD",
                    at,
                    trace=trade_trace,
                )
            writer.close()

            path = Path(writer.directory) / "events.jsonl"
            raw_records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertIn("query_snapshot_id", raw_records[0])
            self.assertEqual(raw_records[1]["query_snapshot_ref"], raw_records[0]["query_snapshot_id"])
            self.assertEqual(raw_records[1]["query_snapshot_count"], 1)
            self.assertNotIn("orders", raw_records[1]["event"]["data"])
            self.assertNotIn("trace", raw_records[1])
            self.assertIn("query_snapshot_id", raw_records[2])
            self.assertEqual(raw_records[3]["query_snapshot_ref"], raw_records[2]["query_snapshot_id"])

            decoder = AuditEventDecoder()
            records = [decoder.decode(record) for record in raw_records]
            self.assertEqual(records[0]["event"]["data"]["orders"], records[1]["event"]["data"]["orders"])
            self.assertEqual(records[0]["trace"], records[1]["trace"])
            self.assertEqual(records[1]["event"]["data"]["request_id"], "orders-2")
            self.assertEqual(records[1]["actions"][0]["data"]["payload"]["request_id"], "orders-2")
            self.assertEqual(records[2]["event"]["data"]["trades"][0]["TradeID"], "trade-1")
            self.assertEqual(records[2]["event"]["data"]["trades"], records[3]["event"]["data"]["trades"])
            self.assertEqual(records[2]["trace"], records[3]["trace"])
            content = path.read_text()
            for private_value in ("broker-secret", "investor-secret", "user-secret", "must-not-persist"):
                self.assertNotIn(private_value, content)
            self.assertIn("InstrumentID", content)
            self.assertIn("TradeID", content)

    def test_query_snapshot_changes_and_errors_keep_full_records(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            writer = AuditWriter(config(), root)
            base = OrderEvent("order-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id="q1")
            changed = OrderEvent("order-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1, client_id="q1")
            trace_one = [{"code": "check", "calculation": {"version": 1}}]
            trace_two = [{"code": "check", "calculation": {"version": 2}}]
            for event, trace in (
                (OrderQueryCompleteEvent("q1", "rb2601", "SHFE", (base,)), trace_one),
                (OrderQueryCompleteEvent("q2", "rb2601", "SHFE", (base,)), trace_one),
                (OrderQueryCompleteEvent("q3", "rb2601", "SHFE", (base,)), trace_two),
                (OrderQueryCompleteEvent("q4", "rb2601", "SHFE", (changed,)), None),
                (OrderQueryCompleteEvent("q5", "rb2601", "SHFE", (), error_id=1, error_msg="offline"), None),
                (OrderQueryCompleteEvent("q6", "rb2601", "SHFE", (base,)), None),
            ):
                writer.record(event, [], "RISK_HOLD", float(len(event.request_id)), trace=trace)
            writer.close()
            records = [
                json.loads(line)
                for line in (Path(writer.directory) / "events.jsonl").read_text().splitlines()
            ]
            self.assertIn("query_snapshot_id", records[0])
            self.assertIn("query_snapshot_ref", records[1])
            self.assertIn("query_snapshot_id", records[2])
            self.assertIn("query_snapshot_id", records[3])
            self.assertNotIn("query_snapshot_id", records[4])
            self.assertNotIn("query_snapshot_ref", records[4])
            self.assertIn("query_snapshot_id", records[5])

    def test_query_snapshot_decoder_rejects_missing_reference(self) -> None:
        decoder = AuditEventDecoder()
        records = (
            {
                "event": {"type": "OrderQueryCompleteEvent", "data": {"request_id": "q2"}},
                "query_snapshot_ref": 99,
                "query_snapshot_count": 1,
            },
            {
                "event": {"type": "OrderQueryCompleteEvent", "data": {"request_id": "q2"}},
                "query_snapshot_ref": None,
                "query_snapshot_count": 1,
            },
            {
                "event": {"type": "OrderQueryCompleteEvent", "data": {"request_id": "q2"}},
                "query_snapshot_count": 1,
            },
        )
        for record in records:
            with self.subTest(record=record), self.assertRaises(AuditError):
                decoder.decode(record)


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
            self.assertNotIn("w_ticks", run_effective["effective"])
            self.assertEqual([c["w_ticks"] for c in run_effective["effective"]["contracts"]], [25, 30])

            for writer, contract in zip(run_audit.writers, strategy.contracts):
                self.assertEqual(
                    json.loads((writer.directory / "effective_strategy.json").read_text())["effective"],
                    contract.effective,
                )
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

    def test_run_writer_records_account_snapshots_without_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            run_audit = MultiContractAuditWriter(multi_config(), root)

            run_audit.record_account(balance=1_000_000.0, available=999_000.0, at=12.5)
            run_audit.record_account(balance=1_000_050.0, available=998_950.0, at=16.5)

            snapshots = [
                json.loads(line)
                for line in (run_audit.directory / "account.jsonl").read_text().splitlines()
            ]
            self.assertEqual(
                snapshots,
                [
                    {"at": 12.5, "balance": 1_000_000.0, "available": 999_000.0},
                    {"at": 16.5, "balance": 1_000_050.0, "available": 998_950.0},
                ],
            )

            run_audit.finish({"terminal_states": {"rb2601@SHFE": "FINISHED"}})
            with self.assertRaises(AuditError):
                run_audit.record_account(balance=1.0, available=1.0, at=20.0)

    def test_account_id_is_rejected_at_audit_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            writer = AuditWriter(config(), root)
            with self.assertRaises(AuditError):
                writer.record({"accountid": "SimNow8888"}, [], "PREVIEW", 0)
            with self.assertRaises(AuditError):
                writer.record({"account_id": "SimNow8888"}, [], "PREVIEW", 0)
            writer.close()


if __name__ == "__main__":
    unittest.main()
