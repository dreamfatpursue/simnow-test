import json
import tempfile
import unittest
from pathlib import Path

from live_grid.activity import ActivityIdentity, ActivityLock
from live_grid.console_projection import AuditProjector
from trading_console import ConsoleState


def run_identity(audit_dir: str | Path, contracts=("rb2601@SHFE", "AP610@CZCE")) -> ActivityIdentity:
    return ActivityIdentity(
        run_id="run-1",
        pid=321,
        started_at="2026-09-04T01:02:03+00:00",
        environment="first",
        market_data_mode="normal",
        strategy_hash="hash",
        contracts=tuple(contracts),
        audit_dir=str(audit_dir),
    )


def write_event(path: Path, *, at: float, state: str, event_type: str = "ClockEvent", data: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "at": at,
                "event": {"type": event_type, "data": data or {"at": at}},
                "state_before": state,
                "state_after": state,
                "actions": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )


class AuditProjectorTests(unittest.TestCase):
    def test_projection_maps_mixed_contracts_to_highest_priority_stage(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            audit_root = Path(root) / "audit" / "run-1"
            write_event(
                audit_root / "rb2601@SHFE" / "events.jsonl",
                at=10,
                state="QUOTING",
                event_type="ConnectionEvent",
                data={"kind": "trade", "connected": True},
            )
            with (audit_root / "rb2601@SHFE" / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "at": 11,
                            "event": {
                                "type": "TickEvent",
                                "data": {
                                    "last_price": 100,
                                    "bid_price": 99,
                                    "ask_price": 101,
                                    "exchange_time": "2026-09-04T01:02:03+00:00",
                                    "at": 11,
                                },
                            },
                            "state_before": "QUOTING",
                            "state_after": "QUOTING",
                            "actions": [],
                        }
                    )
                    + "\n"
                )
            write_event(audit_root / "AP610@CZCE" / "events.jsonl", at=12, state="RISK_HOLD")

            snapshot = AuditProjector(run_identity(audit_root)).refresh(now=12)
            self.assertEqual(snapshot["overall_stage"], "风险")
            rb = next(item for item in snapshot["contracts"] if item["symbol"] == "rb2601")
            ap = next(item for item in snapshot["contracts"] if item["symbol"] == "AP610")
            self.assertEqual(rb["stage"], "交易运行")
            self.assertEqual(rb["state_label"], "交易运行／正在报价")
            self.assertTrue(rb["trade_connected"])
            self.assertEqual(rb["latest_tick"]["bid_price"], 99)
            self.assertEqual(ap["state"], "RISK_HOLD")
            self.assertTrue(ap["risk"])

    def test_projection_ignores_unfinished_tail_until_next_refresh_completes_it(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            audit_root = Path(root) / "audit" / "run-1"
            event_path = audit_root / "rb2601@SHFE" / "events.jsonl"
            write_event(event_path, at=1, state="QUOTING")
            projector = AuditProjector(run_identity(audit_root, ("rb2601@SHFE",)))
            self.assertEqual(projector.refresh(now=1)["contracts"][0]["state"], "QUOTING")

            tail = json.dumps(
                {
                    "at": 2,
                    "event": {"type": "ClockEvent", "data": {"at": 2}},
                    "state_before": "QUOTING",
                    "state_after": "RISK_HOLD",
                    "actions": [],
                }
            )
            with event_path.open("a", encoding="utf-8") as handle:
                handle.write(tail[:20])
            self.assertEqual(projector.refresh(now=2)["contracts"][0]["state"], "QUOTING")
            with event_path.open("a", encoding="utf-8") as handle:
                handle.write(tail[20:] + "\n")
            self.assertEqual(projector.refresh(now=2)["contracts"][0]["state"], "RISK_HOLD")

    def test_projection_derives_orders_trades_position_window_rounds_and_risk_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            audit_root = Path(root) / "audit" / "run-1"
            (audit_root / "effective_strategy.json").parent.mkdir(parents=True, exist_ok=True)
            (audit_root / "effective_strategy.json").write_text(
                json.dumps(
                    {
                        "effective": {
                            "max_round_trips": 3,
                            "contracts": [
                                {
                                    "symbol": "rb2601",
                                    "exchange": "SHFE",
                                    "max_tick_age_seconds": 5,
                                    "quote_windows": [{"start": "09:00", "end": "10:00"}],
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            event_path = audit_root / "rb2601@SHFE" / "events.jsonl"
            records = [
                {
                    "at": 10,
                    "event": {
                        "type": "ClockEvent",
                        "data": {"at": 10, "wall_time": "2026-09-04T09:30:00+08:00"},
                    },
                    "state_before": "WAITING_FOR_STABLE_QUOTE",
                    "state_after": "WAITING_FOR_STABLE_QUOTE",
                    "actions": [],
                },
                {
                    "at": 11,
                    "event": {
                        "type": "TickEvent",
                        "data": {
                            "at": 11,
                            "last_price": 100,
                            "bid_price": 99,
                            "ask_price": 101,
                            "exchange_time": "2026-09-04T09:20:01+08:00",
                        },
                    },
                    "state_before": "WAITING_FOR_STABLE_QUOTE",
                    "state_after": "WAITING_FOR_STABLE_QUOTE",
                    "actions": [],
                },
                {
                    "at": 12,
                    "event": {"type": "ClockEvent", "data": {"at": 12, "wall_time": "2026-09-04T09:30:02+08:00"}},
                    "state_before": "WAITING_FOR_STABLE_QUOTE",
                    "state_after": "QUOTE_PENDING",
                    "actions": [
                        {
                            "type": "Action",
                            "data": {
                                "kind": "submit_order",
                                "payload": {
                                    "client_id": "quote-1-buy",
                                    "symbol": "rb2601",
                                    "exchange": "SHFE",
                                    "side": "BUY",
                                    "offset": "OPEN",
                                    "order_type": "LIMIT",
                                    "volume": 1,
                                    "price": 100,
                                },
                            },
                        }
                    ],
                },
                {
                    "at": 13,
                    "event": {
                        "type": "OrderEvent",
                        "data": {
                            "order_id": "ex-1",
                            "client_id": "quote-1-buy",
                            "symbol": "rb2601",
                            "exchange": "SHFE",
                            "side": "BUY",
                            "offset": "OPEN",
                            "status": "SUBMITTING",
                            "volume": 1,
                            "traded": 0,
                            "price": 100,
                        },
                    },
                    "state_before": "QUOTE_PENDING",
                    "state_after": "QUOTE_PENDING",
                    "actions": [],
                },
                {
                    "at": 14,
                    "event": {
                        "type": "OrderEvent",
                        "data": {
                            "order_id": "ex-1",
                            "client_id": "quote-1-buy",
                            "symbol": "rb2601",
                            "exchange": "SHFE",
                            "side": "BUY",
                            "offset": "OPEN",
                            "status": "NOTTRADED",
                            "volume": 1,
                            "traded": 0,
                            "price": 100,
                        },
                    },
                    "state_before": "QUOTE_PENDING",
                    "state_after": "QUOTING",
                    "actions": [],
                },
                {
                    "at": 15,
                    "event": {
                        "type": "TradeEvent",
                        "data": {
                            "order_id": "ex-1",
                            "client_id": "quote-1-buy",
                            "symbol": "rb2601",
                            "exchange": "SHFE",
                            "side": "BUY",
                            "volume": 1,
                            "price": 100,
                            "trade_id": "trade-1",
                            "exchange_time": "2026-09-04T09:30:05+08:00",
                        },
                    },
                    "state_before": "QUOTING",
                    "state_after": "CLOSING_WAIT",
                    "actions": [],
                },
                {
                    "at": 16,
                    "event": {
                        "type": "TradeEvent",
                        "data": {
                            "order_id": "ex-1",
                            "client_id": "quote-1-buy",
                            "symbol": "rb2601",
                            "exchange": "SHFE",
                            "side": "BUY",
                            "volume": 1,
                            "price": 100,
                            "trade_id": "trade-1",
                        },
                    },
                    "state_before": "CLOSING_WAIT",
                    "state_after": "CLOSING_WAIT",
                    "actions": [],
                },
                {
                    "at": 17,
                    "event": {
                        "type": "PositionQueryCompleteEvent",
                        "data": {
                            "request_id": "position-2",
                            "symbol": "rb2601",
                            "exchange": "SHFE",
                            "net_position": 1,
                            "error_id": 0,
                        },
                    },
                    "state_before": "CLOSING_RECONCILE",
                    "state_after": "CLOSING_RECONCILE",
                    "actions": [],
                },
                {
                    "at": 18,
                    "event": {"type": "ClockEvent", "data": {"at": 18}},
                    "state_before": "CLOSING_RECONCILE",
                    "state_after": "CLOSING_RECONCILE",
                    "actions": [],
                    "trace": [
                        {"code": "round_finished", "calculation": {"round_trips": 1}},
                    ],
                },
                {
                    "at": 19,
                    "event": {"type": "ClockEvent", "data": {"at": 19}},
                    "state_before": "CLOSING_RECONCILE",
                    "state_after": "RISK_HOLD",
                    "actions": [],
                    "trace": [
                        {"code": "risk_hold", "calculation": {"reason": "cancel_timeout"}},
                    ],
                },
                {
                    "at": 20,
                    "event": {"type": "ClockEvent", "data": {"at": 20}},
                    "state_before": "RISK_HOLD",
                    "state_after": "WAITING_FOR_STABLE_QUOTE",
                    "actions": [],
                },
                {
                    "at": 21,
                    "event": {"type": "ClockEvent", "data": {"at": 21}},
                    "state_before": "WAITING_FOR_STABLE_QUOTE",
                    "state_after": "RISK_HOLD",
                    "actions": [],
                    "trace": [
                        {"code": "risk_hold", "calculation": {"reason": "cancel_timeout"}},
                    ],
                },
            ]
            event_path.parent.mkdir(parents=True, exist_ok=True)
            event_path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

            snapshot = AuditProjector(run_identity(audit_root, ("rb2601@SHFE",))).refresh(now=20)
            item = snapshot["contracts"][0]
            self.assertEqual(snapshot["effective"]["max_round_trips"], 3)
            self.assertEqual(snapshot["audit_started_at"], 10)
            self.assertEqual(item["causal_timeline"][0]["label"], "本轮完成")
            self.assertIn("撤单", item["causal_timeline"][1]["reason_label"])
            self.assertEqual(item["current_quote_window"]["start"], "09:00")
            self.assertEqual(item["max_round_trips"], 3)
            self.assertEqual(item["round_trips"], 1)
            self.assertTrue(item["tick_stale"])
            self.assertEqual(len(item["logical_orders"]), 1)
            self.assertEqual(item["logical_orders"][0]["status_path"], ["SUBMITTING", "NOTTRADED"])
            self.assertEqual(item["active_order_count"], 1)
            self.assertEqual(len(item["trades"]), 1)
            self.assertEqual(item["trades"][0]["trade_id"], "trade-1")
            self.assertEqual(item["confirmed_position"]["net_position"], 1)
            self.assertEqual(item["confirmed_position"]["request_id"], "position-2")
            self.assertEqual(len(item["risk_events"]), 2)
            self.assertEqual(item["risk_reason"], "cancel_timeout")
            self.assertEqual([entry["code"] for entry in item["causal_timeline"]], ["round_finished", "risk_hold", "risk_hold"])
            self.assertNotIn("pnl", json.dumps(item).lower())


class CurrentOverviewTests(unittest.TestCase):
    def test_current_overview_distinguishes_active_terminal_and_abnormal_process(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            audit_root = Path(root) / "audit" / "run-1"
            lock_path = Path(root) / "activity.lock"
            identity = run_identity(audit_root, ("rb2601@SHFE",))
            held, _ = ActivityLock.try_acquire(lock_path, identity)
            write_event(audit_root / "rb2601@SHFE" / "events.jsonl", at=1, state="WAITING_FOR_CONTRACT")
            state = ConsoleState(root, activity_lock_path=lock_path)
            self.assertEqual(state.current_overview()["status"], "active")
            held.release()

            abnormal = state.current_overview()
            self.assertEqual(abnormal["status"], "process_abnormal_exit")
            self.assertEqual(abnormal["run_risk"]["reason"], "process_abnormal_exit")
            (audit_root / "summary.json").write_text(json.dumps({"terminal_state": "FAILED", "all_finished": False}), encoding="utf-8")
            terminal = state.current_overview()
            self.assertEqual(terminal["status"], "terminal")
            self.assertEqual(terminal["terminal_state"], "FAILED")


if __name__ == "__main__":
    unittest.main()
