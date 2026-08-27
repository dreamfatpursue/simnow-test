from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from live_grid.ctp_adapter import CtpLiveGridAdapter
from live_grid.session import (
    ClockEvent,
    ConnectionEvent,
    LiveGridSession,
    OrderEvent,
    OrderQueryCompleteEvent,
    PositionQueryCompleteEvent,
    SessionState,
    TickEvent,
)
from tests.test_session import bind_quotes, make_config, qualify_market, start_session


class _Audit:
    def record(self, *args, **kwargs):
        pass

    def close(self):
        pass


class _Gateway:
    def __init__(self):
        self.calls = []

    def query_order(self):
        self.calls.append("order")
        return 11

    def query_trade(self):
        self.calls.append("trade")
        return 12

    def query_position(self):
        self.calls.append("position")
        return 13


class _Engine:
    def __init__(self):
        self.gateway = _Gateway()
        self.cancelled = []

    def get_gateway(self, name):
        return self.gateway

    def subscribe(self, request, name):
        pass

    def cancel_order(self, request, name):
        self.cancelled.append(request)


class LiveGridP0Tests(unittest.TestCase):
    def test_quote_price_must_stay_passive_to_both_sides(self):
        session = start_session(make_config(w_ticks=2, d_ticks=1, s_ticks=2))
        actions = qualify_market(session, last=100, bid=80, ask=81)
        self.assertEqual(actions, [])
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertIn("quote_rejected", {trace["code"] for trace in session.last_audit_trace})

    def test_invalid_or_out_of_range_limits_block_quotes(self):
        session = start_session(make_config())
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0, limit_up=0, limit_down=0))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0.5, limit_up=0, limit_down=0))
        self.assertEqual(session.handle(ClockEvent(2)), [])
        session = start_session(make_config())
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0, limit_up=110, limit_down=90))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0.5, limit_up=110, limit_down=90))
        self.assertEqual(session.handle(ClockEvent(2)), [])

    def test_single_leg_rejection_cancels_the_accepted_companion(self):
        session = start_session()
        submitted = qualify_market(session)
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        actions = session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "REJECTED", 1, client_id=buy.payload["client_id"]))
        self.assertEqual(session.state, SessionState.CLOSING_CANCELS)
        self.assertEqual([action.kind for action in actions], ["cancel_order"])
        self.assertEqual(actions[0].payload["order_id"], "sell-1")
        actions = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = next(action for action in actions if action.kind == "query_position")
        session.handle(PositionQueryCompleteEvent(query.payload["request_id"], "rb2601", "SHFE", 0))
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)

    def test_ack_timeout_queries_orders_before_deciding(self):
        session = start_session()
        submitted = qualify_market(session)
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "SUBMITTING", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "SUBMITTING", 1, client_id=sell.payload["client_id"]))
        actions = session.handle(ClockEvent(session._now + 5))
        self.assertEqual(session.state, SessionState.QUOTE_PENDING)
        self.assertEqual([action.kind for action in actions], ["query_order"])
        query = actions[0]
        session.handle(
            OrderQueryCompleteEvent(
                query.payload["request_id"],
                "rb2601",
                "SHFE",
                orders=(
                    OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1),
                    OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1),
                ),
            )
        )
        self.assertEqual(session.state, SessionState.QUOTING)

    def test_ack_query_timeout_enters_risk_hold(self):
        session = start_session()
        submitted = qualify_market(session)
        for action in submitted:
            session.handle(
                OrderEvent(
                    f"{action.payload['side'].lower()}-1",
                    "rb2601",
                    "SHFE",
                    action.payload["side"],
                    "SUBMITTING",
                    1,
                    client_id=action.payload["client_id"],
                )
            )
        session.handle(ClockEvent(session._now + 5))
        actions = session.handle(ClockEvent(session._now + session.config.effective["cancel_timeout_seconds"]))
        self.assertEqual(session.state, SessionState.RISK_HOLD)
        self.assertTrue(any(action.kind == "cancel_order" for action in actions))
        self.assertEqual(session.handle(ConnectionEvent("trade", True)), [])
        self.assertEqual(session.state, SessionState.RISK_HOLD)

    def test_exchange_tick_time_must_be_fresh_and_strictly_increasing(self):
        session = start_session(make_config(max_tick_age_seconds=10))
        old = (datetime.now().astimezone() - timedelta(seconds=30)).isoformat()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0, exchange_time=old))
        self.assertIsNone(session._stable_since)
        now = datetime.now().astimezone()
        first = now.isoformat()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 1, exchange_time=first))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 2, exchange_time=first))
        self.assertEqual(session._stable_tick_count, 0)

    def test_stale_cancel_is_retried_after_one_second(self):
        session = start_session(make_config(max_tick_age_seconds=1.5))
        submitted = qualify_market(session)
        bind_quotes(session, submitted, "retry")
        first = session.handle(ClockEvent(4))
        second = session.handle(ClockEvent(5))
        self.assertEqual(len([a for a in first if a.kind == "cancel_order"]), 2)
        self.assertEqual(len([a for a in second if a.kind == "cancel_order"]), 2)

    def test_startup_orphan_open_order_is_cancelled_before_position_query(self):
        session = LiveGridSession(make_config(), simnow_confirmed=True)
        adapter = CtpLiveGridAdapter([session], {}, [_Audit()])
        adapter.main_engine = _Engine()
        adapter._on_contract(
            SimpleNamespace(
                data=SimpleNamespace(symbol="rb2601", exchange=SimpleNamespace(value="SHFE"), pricetick=1)
            )
        )
        raw = {
            "InstrumentID": "rb2601",
            "ExchangeID": "SHFE",
            "FrontID": 1,
            "SessionID": 2,
            "OrderRef": "3",
            "OrderStatus": "3",
            "Direction": "0",
            "CombOffsetFlag": "0",
            "VolumeTotalOriginal": 1,
            "VolumeTraded": 0,
            "LimitPrice": 60,
        }
        adapter._on_order_query_complete(
            SimpleNamespace(data=SimpleNamespace(request_id=11, orders=(raw,), error_id=0, error_msg=""))
        )
        self.assertEqual([call for call in adapter.main_engine.gateway.calls], ["order"])
        self.assertEqual(len(adapter.main_engine.cancelled), 1)
        adapter._on_order(
            SimpleNamespace(
                data=SimpleNamespace(
                    orderid="1_2_3",
                    reference=None,
                    symbol="rb2601",
                    exchange=SimpleNamespace(value="SHFE"),
                    direction=SimpleNamespace(value="多"),
                    status=SimpleNamespace(value="已撤销"),
                    volume=1,
                    traded=0,
                    price=60,
                    offset=SimpleNamespace(value="开仓"),
                )
            )
        )
        self.assertEqual(adapter.main_engine.gateway.calls, ["order", "trade"])
        adapter._on_trade_query_complete(SimpleNamespace(data=SimpleNamespace(request_id=12, trades=(), error_id=0, error_msg="")))
        self.assertEqual(adapter.main_engine.gateway.calls, ["order", "trade", "position"])
        adapter._on_position_query_complete(SimpleNamespace(data=SimpleNamespace(request_id=13, positions=(), error_id=0, error_msg="")))
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)


if __name__ == "__main__":
    unittest.main()
