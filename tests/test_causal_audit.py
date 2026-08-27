import json
import tempfile
import unittest
from pathlib import Path

from live_grid.audit import AuditWriter
from live_grid.config import StrategyConfig
from live_grid.session import (
    ClockEvent,
    ContractEvent,
    LiveGridSession,
    OrderEvent,
    PositionQueryCompleteEvent,
    TradeEvent,
    TickEvent,
)


def config() -> StrategyConfig:
    return StrategyConfig.from_mapping(
        {
            "version": 1,
            "symbol": "rb2601",
            "exchange": "SHFE",
            "target_lots": 1,
            "w_ticks": 2,
            "d_ticks": 3,
            "s_ticks": 2,
            "stable_market_seconds": 2,
            "max_tick_age_seconds": 60,
            "quote_windows": [{"start": "00:00", "end": "23:59"}],
        }
    )


def replacement_config() -> StrategyConfig:
    return StrategyConfig.from_mapping(
        {
            "version": 1,
            "symbol": "rb2601",
            "exchange": "SHFE",
            "target_lots": 1,
            "w_ticks": 2,
            "d_ticks": 2,
            "s_ticks": 2,
            "stable_market_seconds": 2,
            "reanchor_confirmation_seconds": 1,
            "max_tick_age_seconds": 60,
            "quote_windows": [{"start": "00:00", "end": "23:59"}],
        }
    )


def start_quoting_session(strategy: StrategyConfig | None = None) -> tuple[LiveGridSession, list[object]]:
    strategy = strategy or replacement_config()
    session = LiveGridSession(strategy, simnow_confirmed=True)
    session.handle(ContractEvent("rb2601", "SHFE", 1.0, size=5.0))
    session.handle(PositionQueryCompleteEvent("position-1", "rb2601", "SHFE", 0))
    session.handle(TickEvent("rb2601", "SHFE", 100.0, 99.0, 100.0, 0.0))
    session.handle(TickEvent("rb2601", "SHFE", 100.0, 99.0, 100.0, 0.5))
    actions = session.handle(ClockEvent(2.0))
    for action in actions:
        session.handle(
            OrderEvent(
                f"order-{action.payload['client_id']}",
                "rb2601",
                "SHFE",
                action.payload["side"],
                "NOTTRADED",
                1,
                client_id=action.payload["client_id"],
            )
        )
    return session, actions


class CausalAuditTests(unittest.TestCase):
    def test_first_quote_records_structured_calculation_trace(self) -> None:
        strategy = config()
        session = LiveGridSession(strategy, simnow_confirmed=True)

        session.handle(ContractEvent("rb2601", "SHFE", 10.0, size=5.0))
        session.handle(PositionQueryCompleteEvent("position-1", "rb2601", "SHFE", 0))
        session.handle(TickEvent("rb2601", "SHFE", 100.0, 99.0, 101.0, 0.0))
        session.handle(TickEvent("rb2601", "SHFE", 100.0, 99.0, 101.0, 0.5))
        actions = session.handle(ClockEvent(2.0))

        self.assertEqual([action.kind for action in actions], ["submit_order", "submit_order"])
        record = session.audit_events[-1]
        self.assertEqual(record["event"]["type"], "ClockEvent")
        trace = record["trace"]
        quote_trace = next(item for item in trace if item["code"] == "quote_submitted")
        self.assertEqual(quote_trace["client_ids"], ["quote-1-buy", "quote-1-sell"])
        self.assertEqual(
            quote_trace["market"],
            {
                "last_price": 100.0,
                "bid_price": 99.0,
                "ask_price": 101.0,
                "limit_up": None,
                "limit_down": None,
            },
        )
        self.assertEqual(
            quote_trace["calculation"],
            {
                "anchor_ticks": 10,
                "anchor_price": 100.0,
                "pricetick": 10.0,
                "w_ticks": 2,
                "d_ticks": 3,
                "distance_ticks": 5,
                "buy_price": 50.0,
                "sell_price": 150.0,
            },
        )

    def test_new_audit_writes_schema_version_with_effective_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            writer = AuditWriter(config(), root)
            directory = writer.finish({"terminal_state": "FINISHED"})

            effective = json.loads((Path(directory) / "effective_strategy.json").read_text(encoding="utf-8"))
            self.assertEqual(effective["audit_schema_version"], 2)

    def test_market_pause_records_book_protection_operands(self) -> None:
        session, _ = start_quoting_session()

        actions = session.handle(TickEvent("rb2601", "SHFE", 100.0, 99.0, 101.0, 3.0))

        self.assertEqual([action.kind for action in actions], ["cancel_order", "cancel_order"])
        trace = session.audit_events[-1]["trace"][0]
        self.assertEqual(trace["code"], "market_pause")
        self.assertEqual(trace["client_ids"], ["quote-1-buy", "quote-1-sell"])
        self.assertEqual(
            trace["calculation"],
            {
                "w_ticks": 2,
                "d_ticks": 2,
                "distance_ticks": 4,
                "spread_ticks": 2,
                "protection_multiple": 2,
                "comparison": "distance_ticks > protection_multiple * spread_ticks",
                "passed": False,
            },
        )

    def test_quote_order_status_changes_are_traceable(self) -> None:
        session, submitted = start_quoting_session()
        buy = next(action for action in submitted if action.payload["side"] == "BUY")

        session.handle(
            OrderEvent(
                "order-quote-1-buy",
                "rb2601",
                "SHFE",
                "BUY",
                "ALLTRADED",
                1,
                traded=1,
                client_id=buy.payload["client_id"],
                exchange_time="2026-08-18T09:00:01+08:00",
            )
        )

        trace = session.audit_events[-1]["trace"]
        status_trace = next(item for item in trace if item["code"] == "order_status")
        self.assertEqual(status_trace["client_ids"], [buy.payload["client_id"]])
        self.assertEqual(
            status_trace["calculation"],
            {
                "order_id": "order-quote-1-buy",
                "previous_status": "NOTTRADED",
                "status": "ALLTRADED",
                "traded": 1,
                "traded_delta": 1,
                "volume": 1,
                "exchange_time": "2026-08-18T09:00:01+08:00",
            },
        )

    def test_reanchor_records_previous_and_next_quote_relationship(self) -> None:
        session, initial_actions = start_quoting_session()

        session.handle(TickEvent("rb2601", "SHFE", 121.0, 120.0, 121.0, 3.0))
        reanchor_actions = session.handle(TickEvent("rb2601", "SHFE", 121.0, 120.0, 121.0, 4.1))

        self.assertEqual([action.kind for action in reanchor_actions], ["cancel_order", "cancel_order"])
        reanchor_trace = session.audit_events[-1]["trace"][0]
        self.assertEqual(reanchor_trace["code"], "reanchor")
        self.assertEqual(reanchor_trace["client_ids"], ["quote-1-buy", "quote-1-sell"])
        self.assertEqual(reanchor_trace["calculation"]["old_anchor_ticks"], 100)
        self.assertEqual(reanchor_trace["calculation"]["new_anchor_ticks"], 120)
        self.assertEqual(reanchor_trace["calculation"]["confirmation_seconds"], 1)

        for action in reanchor_actions:
            session.handle(
                OrderEvent(
                    f"order-{action.payload['client_id']}",
                    "rb2601",
                    "SHFE",
                    "BUY" if action.payload["client_id"].endswith("buy") else "SELL",
                    "CANCELLED",
                    1,
                    client_id=action.payload["client_id"],
                )
            )
        session.handle(TickEvent("rb2601", "SHFE", 121.0, 120.0, 121.0, 5.0))
        session.handle(TickEvent("rb2601", "SHFE", 121.0, 120.0, 121.0, 5.5))
        replacement = session.handle(ClockEvent(7.0))

        self.assertEqual([action.kind for action in replacement], ["submit_order", "submit_order"])
        quote_trace = next(
            item for item in session.audit_events[-1]["trace"] if item["code"] == "quote_submitted"
        )
        self.assertEqual(quote_trace["code"], "quote_submitted")
        self.assertEqual(quote_trace["replacement"]["reason"], "reanchor")
        self.assertEqual(
            quote_trace["replacement"]["previous_client_ids"],
            [action.payload["client_id"] for action in initial_actions],
        )

    def test_reanchor_waits_until_confirmation_threshold(self) -> None:
        session, _ = start_quoting_session()

        self.assertEqual(session.handle(TickEvent("rb2601", "SHFE", 121.0, 120.0, 121.0, 3.0)), [])
        self.assertEqual(session.handle(TickEvent("rb2601", "SHFE", 121.0, 120.0, 121.0, 3.9)), [])
        self.assertEqual(session.state.value, "QUOTING")
        self.assertNotIn("reanchor", [trace["code"] for trace in session.audit_events[-1].get("trace", [])])

        actions = session.handle(TickEvent("rb2601", "SHFE", 121.0, 120.0, 121.0, 4.0))
        self.assertEqual([action.kind for action in actions], ["cancel_order", "cancel_order"])
        self.assertEqual(session.audit_events[-1]["trace"][0]["code"], "reanchor")

    def test_fill_window_timeout_flatten_and_reconcile_are_causally_recorded(self) -> None:
        strategy = replacement_config()
        strategy = StrategyConfig.from_mapping(
            {**strategy.effective, "closing_wait_seconds": 2, "max_round_trips": 1}
        )
        session, submitted = start_quoting_session(strategy)
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        # start_quoting_session uses replacement_config; the trace seam is independent of strategy reload.
        cancel_from_fill = session.handle(
            TradeEvent(
                "order-quote-1-buy",
                "rb2601",
                "SHFE",
                "BUY",
                1,
                90.0,
                "trade-open",
                client_id=buy.payload["client_id"],
                exchange_time="2026-08-18T09:00:01+08:00",
                at=3.0,
            )
        )
        first_trace = session.audit_events[-1]["trace"]
        self.assertEqual(first_trace[0]["code"], "first_fill")
        self.assertEqual(first_trace[0]["client_ids"], [buy.payload["client_id"]])
        self.assertEqual(first_trace[0]["calculation"]["volume"], 1)
        self.assertEqual(first_trace[0]["calculation"]["exchange_time"], "2026-08-18T09:00:01+08:00")

        timeout_actions = session.handle(ClockEvent(5.1))
        self.assertEqual(timeout_actions[0].kind, "submit_order")
        timeout_trace = session.audit_events[-1]["trace"]
        self.assertEqual(timeout_trace[0]["code"], "window_timeout")
        self.assertEqual(timeout_trace[0]["calculation"]["net_position"], 1)

        flatten = timeout_actions[0]
        session.handle(
            OrderEvent(
                "flatten-order",
                "rb2601",
                "SHFE",
                "SELL",
                "NOTTRADED",
                1,
                client_id=flatten.payload["client_id"],
            )
        )
        self.assertIn(
            "order_status",
            [trace["code"] for trace in session.audit_events[-1]["trace"]],
        )
        cancel_from_fill = session.handle(
            TradeEvent(
                "flatten-order",
                "rb2601",
                "SHFE",
                "SELL",
                1,
                99.0,
                "trade-close",
                client_id=flatten.payload["client_id"],
                exchange_time="2026-08-18T09:00:05+08:00",
                at=5.2,
            )
        )
        close_trace = session.audit_events[-1]["trace"]
        self.assertEqual(close_trace[0]["code"], "flatten_fill")
        self.assertTrue(any(action.kind == "cancel_order" for action in cancel_from_fill))
        session.handle(
            OrderEvent(
                "flatten-order",
                "rb2601",
                "SHFE",
                "SELL",
                "ALLTRADED",
                1,
                traded=1,
                client_id=flatten.payload["client_id"],
            )
        )
        self.assertIn(
            "remaining_cancel",
            [trace["code"] for trace in session.audit_events[-1]["trace"]],
        )

        session.handle(
            OrderEvent(
                "order-quote-1-sell",
                "rb2601",
                "SHFE",
                "SELL",
                "CANCELLED",
                1,
                client_id=sell.payload["client_id"],
            )
        )
        reconcile_actions = session.handle(
            OrderEvent(
                "order-quote-1-buy",
                "rb2601",
                "SHFE",
                "BUY",
                "CANCELLED",
                1,
                client_id=buy.payload["client_id"],
            )
        )
        query_id = next(action for action in reconcile_actions if action.kind == "query_position").payload["request_id"]
        self.assertIn(
            "closing_position_query",
            [trace["code"] for trace in session.audit_events[-1]["trace"]],
        )
        session.handle(PositionQueryCompleteEvent(query_id, "rb2601", "SHFE", 0))
        self.assertIn(
            "round_finished",
            [trace["code"] for trace in session.audit_events[-1]["trace"]],
        )


if __name__ == "__main__":
    unittest.main()
