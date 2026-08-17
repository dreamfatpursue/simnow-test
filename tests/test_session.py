import time
import unittest

from live_grid.config import StrategyConfig
from live_grid.session import (
    ClockEvent,
    ContractEvent,
    InterruptEvent,
    LiveGridSession,
    OrderEvent,
    PositionQueryCompleteEvent,
    SessionState,
    TickEvent,
    TradeEvent,
)


def make_config(**changes: object) -> StrategyConfig:
    raw = {
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
    raw.update(changes)
    return StrategyConfig.from_mapping(raw)


def start_session(config: StrategyConfig | None = None) -> LiveGridSession:
    config = config or make_config()
    session = LiveGridSession(config, simnow_confirmed=True)
    actions = session.handle(ContractEvent(config.effective["symbol"], config.effective["exchange"], 1.0))
    assert actions[0].kind == "query_position"
    request_id = actions[0].payload["request_id"]
    session.handle(PositionQueryCompleteEvent(request_id, config.effective["symbol"], config.effective["exchange"], 0))
    return session


def open_window(session: LiveGridSession, order_id: str, side: str, *, traded: int, volume: int | None = None, exchange: str = "SHFE") -> None:
    """首成交已进入价差窗口后，把已成交侧委托推到终态。"""
    session.handle(
        OrderEvent(order_id, "rb2601", exchange, side, "ALLTRADED", volume if volume is not None else traded, traded=traded)
    )


def expire_window(session: LiveGridSession):
    """价差窗口超时，进入 FLATTENING；返回本次产生的动作。"""
    wait = session.config.effective["closing_wait_seconds"]
    return session.handle(ClockEvent(session._now + wait + 0.1))


def drive_to_final_reconcile(session: LiveGridSession, *, filled: str = "buy-1", side: str = "BUY", exchange: str = "SHFE", opposite_id: str = "sell-1", opposite_side: str = "SELL") -> str:
    """首成交 → 窗口超时 → FAK 平仓 → 撤对侧；返回收尾对账的 request_id。"""
    open_window(session, filled, side, traded=1, exchange=exchange)
    flatten = expire_window(session)[0]
    client_id = flatten.payload["client_id"]
    flatten_side = "SELL" if side == "BUY" else "BUY"
    session.handle(
        OrderEvent("f-1", "rb2601", exchange, flatten_side, "ALLTRADED", 1, price=99 if flatten_side == "SELL" else 101, traded=1, client_id=client_id)
    )
    closing = session.handle(OrderEvent(opposite_id, "rb2601", exchange, opposite_side, "CANCELLED", 1))
    return next(action for action in closing if action.kind == "query_position").payload["request_id"]


def finish_flattened_round(session: LiveGridSession, flatten_id: str, *, flatten_side: str, exchange: str = "SHFE", opposite_id: str | None = None, opposite_side: str | None = None) -> None:
    """平仓成交 → 撤对侧 → 对账净仓为零收尾（对侧默认 sell-1/SELL）。"""
    session.handle(OrderEvent(flatten_id, "rb2601", exchange, flatten_side, "ALLTRADED", 1, traded=1, client_id=flatten_id))
    session.handle(TradeEvent(flatten_id, "rb2601", exchange, flatten_side, 1, 99 if flatten_side == "SELL" else 101, "flat-trade", client_id=flatten_id))
    opposite_id = opposite_id or "sell-1"
    opposite_side = opposite_side or "SELL"
    closing = session.handle(OrderEvent(opposite_id, "rb2601", exchange, opposite_side, "CANCELLED", 1))
    query = next(action for action in closing if action.kind == "query_position").payload["request_id"]
    session.handle(PositionQueryCompleteEvent(query, "rb2601", exchange, 0))


class LiveGridSessionTests(unittest.TestCase):
    def test_confirmation_is_required_before_session_can_emit_any_action(self) -> None:
        config = make_config()
        session = LiveGridSession(config, simnow_confirmed=False)
        self.assertEqual(session.state, SessionState.PREVIEW)
        self.assertEqual(session.handle(ContractEvent("rb2601", "SHFE", 1.0)), [])

        confirmed_session = LiveGridSession(config, simnow_confirmed=True)
        self.assertEqual(confirmed_session.state, SessionState.WAITING_FOR_CONTRACT)
        self.assertEqual(
            confirmed_session.handle(ContractEvent("rb2601", "SHFE", 1.0))[0].kind,
            "query_position",
        )

    def test_exchange_time_and_contract_size_are_recorded_verbatim_in_audit_events(self) -> None:
        session = LiveGridSession(make_config(), simnow_confirmed=True)
        session.handle(ContractEvent("rb2601", "SHFE", 1.0, size=5.0))
        session.handle(PositionQueryCompleteEvent("position-1", "rb2601", "SHFE", 0))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        session.handle(ClockEvent(2))
        buy = next(
            action
            for action in session.actions
            if action.kind == "submit_order" and action.payload["side"] == "BUY"
        )
        session.handle(
            OrderEvent(
                "buy-1",
                "rb2601",
                "SHFE",
                "BUY",
                "NOTTRADED",
                1,
                client_id=buy.payload["client_id"],
                exchange_time="2026-08-17T21:00:01+08:00",
            )
        )
        session.handle(
            TradeEvent(
                "buy-1",
                "rb2601",
                "SHFE",
                "BUY",
                1,
                60,
                "trade-1",
                exchange_time="2026-08-17T21:03:05+08:00",
            )
        )
        contract_record = next(record for record in session.audit_events if record["event"]["type"] == "ContractEvent")
        order_record = next(record for record in session.audit_events if record["event"]["type"] == "OrderEvent")
        trade_record = next(record for record in session.audit_events if record["event"]["type"] == "TradeEvent")
        self.assertEqual(contract_record["event"]["data"]["size"], 5.0)
        self.assertEqual(order_record["event"]["data"]["exchange_time"], "2026-08-17T21:00:01+08:00")
        self.assertEqual(trade_record["event"]["data"]["exchange_time"], "2026-08-17T21:03:05+08:00")

    def test_no_quote_until_contract_position_and_two_second_stable_market(self) -> None:
        config = make_config()
        session = LiveGridSession(config, simnow_confirmed=True)
        self.assertEqual(session.state, SessionState.WAITING_FOR_CONTRACT)
        self.assertEqual(session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0)), [])
        query = session.handle(ContractEvent("rb2601", "SHFE", 1.0))[0]
        session.handle(PositionQueryCompleteEvent(query.payload["request_id"], "rb2601", "SHFE", 0))
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertEqual(session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0)), [])
        self.assertEqual(session.handle(ClockEvent(1.9)), [])
        actions = session.handle(ClockEvent(2.0))
        self.assertEqual([action.kind for action in actions], ["submit_order", "submit_order"])
        self.assertEqual({action.payload["side"] for action in actions}, {"BUY", "SELL"})

    def test_prices_are_tick_aligned_and_crossing_without_trade_does_not_close(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        actions = session.handle(ClockEvent(2))
        self.assertEqual([action.payload["price"] for action in actions], [60, 140])
        session.handle(TickEvent("rb2601", "SHFE", 60, 59, 61, 3))
        self.assertEqual(session.state, SessionState.QUOTING)

    def test_partial_first_fill_flattens_residual_after_window(self) -> None:
        session = start_session(make_config(target_lots=2))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 2, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 2, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        self.assertEqual(session.state, SessionState.CLOSING_WAIT)
        open_window(session, "buy-1", "BUY", traded=1, volume=2)

        flatten_actions = expire_window(session)

        self.assertEqual(session.state, SessionState.FLATTENING)
        flatten = flatten_actions[0]
        self.assertEqual(flatten.kind, "submit_order")
        self.assertEqual(flatten.payload["side"], "SELL")
        self.assertEqual(flatten.payload["offset"], "CLOSETODAY")
        self.assertEqual(flatten.payload["order_type"], "FAK")
        self.assertEqual(flatten.payload["volume"], 1)

    def test_opposite_fill_in_window_completes_spread_without_flatten(self) -> None:
        session = start_session(make_config(max_round_trips=1))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        self.assertEqual(session.state, SessionState.CLOSING_WAIT)
        open_window(session, "buy-1", "BUY", traded=1)

        # 窗口内对侧成交：立即结束窗口，两侧终态后直接对账，全程无 FAK、无撤单。
        query_actions = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "ALLTRADED", 1, traded=1))
        self.assertEqual(session.state, SessionState.CLOSING_RECONCILE)
        self.assertEqual([action.kind for action in query_actions], ["query_position"])
        self.assertEqual(session.handle(TradeEvent("sell-1", "rb2601", "SHFE", "SELL", 1, 140, "trade-2")), [])

        session.handle(PositionQueryCompleteEvent(query_actions[0].payload["request_id"], "rb2601", "SHFE", 0))
        self.assertEqual(session.state, SessionState.FINISHED)
        summary = session.summary()
        self.assertEqual(summary["round_trips"], 1)
        self.assertEqual(summary["flatten_attempts"], [])
        self.assertFalse(any(action.kind == "cancel_order" for action in session.actions))
        self.assertFalse(
            any(action.kind == "submit_order" and action.payload.get("order_type") == "FAK" for action in session.actions)
        )

    def test_partial_opposite_fill_in_window_cancels_remainder_and_flattens_residual(self) -> None:
        session = start_session(make_config(target_lots=2, max_round_trips=1))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 2, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 2, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 2, 60, "trade-1"))
        open_window(session, "buy-1", "BUY", traded=2, volume=2)

        # 对侧部分成交 1 手：结束窗口、撤掉剩余 1 手，对账后残余净仓 1 手走 FAK。
        cancel_actions = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "PARTTRADED", 2, traded=1))
        self.assertEqual([action.kind for action in cancel_actions], ["cancel_order"])
        session.handle(TradeEvent("sell-1", "rb2601", "SHFE", "SELL", 1, 140, "trade-2"))
        query_actions = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 2, traded=1))
        self.assertEqual(session.state, SessionState.CLOSING_RECONCILE)

        flatten = session.handle(PositionQueryCompleteEvent(query_actions[0].payload["request_id"], "rb2601", "SHFE", 1))[0]
        self.assertEqual(flatten.payload["volume"], 1)
        self.assertEqual(flatten.payload["order_type"], "FAK")
        final = session.handle(OrderEvent("f-1", "rb2601", "SHFE", "SELL", "ALLTRADED", 1, price=99, traded=1, client_id=flatten.payload["client_id"]))
        if not any(action.kind == "query_position" for action in final):
            final += session.handle(TradeEvent("f-1", "rb2601", "SHFE", "SELL", 1, 99, "flat-trade", client_id=flatten.payload["client_id"]))
        query2 = next(action for action in final if action.kind == "query_position").payload["request_id"]
        session.handle(PositionQueryCompleteEvent(query2, "rb2601", "SHFE", 0))
        self.assertEqual(session.state, SessionState.FINISHED)

    def test_first_fill_opens_window_then_flattens_and_cancels_opposite_after_timeout(self) -> None:
        session = start_session(make_config(closing_wait_seconds=1, max_round_trips=1))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))

        actions = session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        self.assertEqual(session.state, SessionState.CLOSING_WAIT)
        self.assertEqual(actions, [])
        self.assertEqual(session.first_fill["volume"], 1)
        self.assertFalse(any(action.kind == "cancel_order" for action in session.actions))
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "ALLTRADED", 1, traded=1))

        self.assertEqual(session.handle(ClockEvent(2.5)), [])

        flatten_actions = session.handle(ClockEvent(3.0))
        self.assertEqual(session.state, SessionState.FLATTENING)
        self.assertEqual([action.kind for action in flatten_actions], ["submit_order"])
        flatten = flatten_actions[0]
        self.assertEqual(flatten.payload["order_type"], "FAK")
        self.assertEqual(flatten.payload["side"], "SELL")
        self.assertEqual(flatten.payload["volume"], 1)
        self.assertFalse(any(action.kind == "cancel_order" for action in session.actions))

        cancel_actions = session.handle(OrderEvent("f-1", "rb2601", "SHFE", "SELL", "ALLTRADED", 1, price=99, traded=1, client_id=flatten.payload["client_id"]))
        self.assertEqual([action.kind for action in cancel_actions], ["cancel_order"])
        self.assertEqual(session.handle(TradeEvent("f-1", "rb2601", "SHFE", "SELL", 1, 99, "trade-2")), [])

        query_actions = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        self.assertEqual(session.state, SessionState.CLOSING_RECONCILE)
        self.assertEqual([action.kind for action in query_actions], ["query_position"])

        session.handle(PositionQueryCompleteEvent(query_actions[0].payload["request_id"], "rb2601", "SHFE", 0))
        self.assertEqual(session.state, SessionState.FINISHED)
        self.assertEqual(session.summary()["round_trips"], 1)

    def test_first_full_fill_flattens_then_cancels_opposite_and_reconciles(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))

        actions = session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        self.assertEqual(session.state, SessionState.CLOSING_WAIT)
        self.assertEqual(session.first_fill["volume"], 1)
        self.assertEqual(actions, [])
        open_window(session, "buy-1", "BUY", traded=1)
        flatten = expire_window(session)[0]

        cancel_actions = session.handle(
            OrderEvent("f-1", "rb2601", "SHFE", "SELL", "ALLTRADED", 1, price=99, traded=1, client_id=flatten.payload["client_id"])
        )
        self.assertEqual([action.kind for action in cancel_actions], ["cancel_order"])

        closing = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        self.assertEqual(session.state, SessionState.CLOSING_RECONCILE)
        self.assertTrue(session.cancellation_terminal)
        self.assertEqual([action.kind for action in closing], ["query_position"])

    def test_rolling_action_limit_pauses_after_sixty_normal_actions(self) -> None:
        session = start_session(
            make_config(
                reanchor_confirmation_seconds=0.1,
                stable_market_seconds=0.1,
                action_limit_per_minute=60,
            )
        )
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(0.1))
        active_orders = []
        for number, action in enumerate(submitted, 1):
            order_id = f"order-{number}"
            active_orders.append((order_id, action.payload["side"]))
            session.handle(
                OrderEvent(
                    order_id,
                    "rb2601",
                    "SHFE",
                    action.payload["side"],
                    "NOTTRADED",
                    1,
                    client_id=action.payload["client_id"],
                )
            )

        anchor = 100
        for cycle in range(15):
            base = 0.2 + cycle * 0.33
            outside = anchor + 21
            session.handle(TickEvent("rb2601", "SHFE", outside, outside - 1, outside + 1, base))
            actions = session.handle(
                TickEvent("rb2601", "SHFE", outside, outside - 1, outside + 1, base + 0.11)
            )
            self.assertEqual([action.kind for action in actions], ["cancel_order", "cancel_order"])
            for number, (order_id, side) in enumerate(active_orders, 1):
                session.handle(OrderEvent(order_id, "rb2601", "SHFE", side, "CANCELLED", 1))

            session.handle(TickEvent("rb2601", "SHFE", outside, outside - 1, outside + 1, base + 0.22))
            actions = session.handle(ClockEvent(base + 0.33))
            if cycle < 14:
                self.assertEqual([action.kind for action in actions], ["submit_order", "submit_order"])
                active_orders = []
                for number, action in enumerate(actions, 1):
                    order_id = f"order-{cycle + 2}-{number}"
                    active_orders.append((order_id, action.payload["side"]))
                    session.handle(
                        OrderEvent(
                            order_id,
                            "rb2601",
                            "SHFE",
                            action.payload["side"],
                            "NOTTRADED",
                            1,
                            client_id=action.payload["client_id"],
                        )
                    )
                anchor += 20
            else:
                self.assertEqual([action.kind for action in actions], ["audit_warning"])
                self.assertEqual(actions[0].payload["code"], "normal_action_limit_reached")
                self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)

    def test_wide_book_fails_strict_protection_gate_without_quotes(self) -> None:
        session = start_session(make_config(w_ticks=1, d_ticks=1, book_protection_multiple=2))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        self.assertEqual(session.handle(ClockEvent(2)), [])
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)

    def test_mismatched_position_query_cannot_open_or_flatten(self) -> None:
        config = make_config()
        session = LiveGridSession(config, simnow_confirmed=True)
        query = session.handle(ContractEvent("rb2601", "SHFE", 1.0))[0]
        self.assertEqual(session.handle(PositionQueryCompleteEvent("stale", "rb2601", "SHFE", 0)), [])
        self.assertEqual(session.state, SessionState.WAITING_FOR_ZERO_POSITION)
        self.assertEqual(session.handle(PositionQueryCompleteEvent(query.payload["request_id"], "rb2601", "SHFE", 1)), [])
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertEqual(session.failure_reason, "nonzero_startup_position")
        self.assertEqual(session.final_net_position, 1)

    def test_interrupt_before_zero_confirmation_does_not_flatten_existing_position(self) -> None:
        config = make_config()
        session = LiveGridSession(config, simnow_confirmed=True)
        session.handle(ContractEvent("rb2601", "SHFE", 1.0))

        self.assertEqual(session.handle(InterruptEvent()), [])
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertEqual(session.failure_reason, "interrupted_before_zero_position")
        self.assertEqual(session.final_net_position, None)

    def test_replacement_waits_for_terminal_callbacks_and_then_requalifies_market(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        for number, action in enumerate(submitted, 1):
            session.handle(
                OrderEvent(
                    f"order-{number}",
                    "rb2601",
                    "SHFE",
                    action.payload["side"],
                    "NOTTRADED",
                    1,
                    client_id=action.payload["client_id"],
                )
            )
        actions = session.handle(TickEvent("rb2601", "SHFE", 121, 120, 122, 3))
        actions += session.handle(TickEvent("rb2601", "SHFE", 121, 120, 122, 4))
        self.assertEqual(session.state, SessionState.REPLACING)
        self.assertEqual([action.kind for action in actions], ["cancel_order"] * 2)
        self.assertEqual(session.handle(ClockEvent(5))[0].kind, "audit_warning")
        session.handle(OrderEvent("order-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1))
        self.assertEqual(session.state, SessionState.REPLACING)
        session.handle(OrderEvent("order-2", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertEqual(session.handle(ClockEvent(5.9)), [])
        session.handle(TickEvent("rb2601", "SHFE", 121, 120, 122, 6))
        self.assertEqual(session.handle(ClockEvent(7.9)), [])
        self.assertEqual([a.kind for a in session.handle(ClockEvent(8))], ["submit_order", "submit_order"])

    def test_reanchor_requires_two_seconds_of_new_valid_market_data(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        for number, action in enumerate(submitted, 1):
            session.handle(OrderEvent(f"order-{number}", "rb2601", "SHFE", action.payload["side"], "NOTTRADED", 1, client_id=action.payload["client_id"]))
        session.handle(TickEvent("rb2601", "SHFE", 121, 120, 122, 3))
        session.handle(TickEvent("rb2601", "SHFE", 121, 120, 122, 4))
        session.handle(OrderEvent("order-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1))
        session.handle(OrderEvent("order-2", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertEqual(session.handle(ClockEvent(7)), [])
        session.handle(TickEvent("rb2601", "SHFE", 121, 120, 122, 7))
        self.assertEqual(session.handle(ClockEvent(8.9)), [])
        actions = session.handle(ClockEvent(9))
        self.assertEqual([action.kind for action in actions], ["submit_order", "submit_order"])
        self.assertEqual([action.payload["price"] for action in actions], [70, 150])

    def test_safety_cancel_bypasses_action_limit_but_replacement_submit_does_not(self) -> None:
        session = start_session(make_config(action_limit_per_minute=2))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        for number, action in enumerate(submitted, 1):
            session.handle(
                OrderEvent(
                    f"order-{number}",
                    "rb2601",
                    "SHFE",
                    action.payload["side"],
                    "NOTTRADED",
                    1,
                    client_id=action.payload["client_id"],
                )
            )
        actions = session.handle(TickEvent("rb2601", "SHFE", 0, 0, 0, 3))
        self.assertEqual(session.state, SessionState.REPLACING)
        self.assertEqual([action.kind for action in actions], ["cancel_order", "cancel_order"])

    def test_normal_action_limit_is_visible_as_audit_warning(self) -> None:
        session = start_session(make_config(action_limit_per_minute=1))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        actions = session.handle(ClockEvent(2))
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertEqual([action.kind for action in actions], ["audit_warning"])

    def test_cancel_timeout_still_reconciles_and_marks_failure(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        for number, action in enumerate(submitted, 1):
            session.handle(
                OrderEvent(
                    f"order-{number}",
                    "rb2601",
                    "SHFE",
                    action.payload["side"],
                    "NOTTRADED",
                    1,
                    client_id=action.payload["client_id"],
                )
            )
        session.handle(TradeEvent("order-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        self.assertEqual(session.state, SessionState.CLOSING_WAIT)
        session.handle(InterruptEvent())
        actions = session.handle(ClockEvent(12))
        self.assertEqual(session.state, SessionState.CLOSING_RECONCILE)
        self.assertEqual(session.failure_reason, "cancel_timeout")
        self.assertEqual([action.kind for action in actions], ["query_position"])

    def test_closing_query_failure_does_not_reuse_startup_zero_position(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        query = drive_to_final_reconcile(session)
        session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 0, error_id=9, error_msg="query failed"))
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertEqual(session.failure_reason, "closing_position_query_failed")
        self.assertIsNone(session.final_net_position)

    def test_nonmatching_closing_query_cannot_start_flattening(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        query = drive_to_final_reconcile(session)
        self.assertEqual(session.handle(PositionQueryCompleteEvent("stale", "rb2601", "SHFE", 1)), [])
        self.assertEqual(session.state, SessionState.CLOSING_RECONCILE)
        self.assertEqual(session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 1))[0].kind, "submit_order")

    def test_late_opening_fill_after_cancel_reconciles_new_exposure(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        open_window(session, "buy-1", "BUY", traded=1)
        expire_window(session)
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "late-trade"))
        self.assertEqual(session.final_net_position, 2)
        self.assertEqual(session.state, SessionState.FLATTENING)

    def test_late_opening_fill_after_flatten_failure_exposes_residual(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        open_window(session, "buy-1", "BUY", traded=1)
        flatten = expire_window(session)[0]
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "REJECTED", 1, client_id=flatten.payload["client_id"]))
        session.handle(TradeEvent("sell-1", "rb2601", "SHFE", "SELL", 1, 140, "late-trade"))
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertEqual(session.failure_reason, "flatten_rejected")
        self.assertEqual(session.final_net_position, 0)

    def test_order_and_trade_callbacks_for_same_late_fill_count_once(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        open_window(session, "buy-1", "BUY", traded=1)
        expire_window(session)
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "late-trade"))
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "ALLTRADED", 2, traded=2))
        self.assertEqual(session.final_net_position, 2)

    def test_late_fill_that_offsets_flatten_waits_for_flatten_terminal_callback(self) -> None:
        session = start_session(make_config(max_round_trips=1))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        open_window(session, "buy-1", "BUY", traded=1)
        flatten = expire_window(session)[0]
        client_id = flatten.payload["client_id"]
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=client_id))
        session.handle(TradeEvent("sell-1", "rb2601", "SHFE", "SELL", 1, 140, "late-trade"))
        self.assertEqual(session.state, SessionState.FLATTENING)
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1, traded=0, client_id=client_id))
        finish_flattened_round(session, "flatten-1", flatten_side="SELL")
        self.assertEqual(session.state, SessionState.FINISHED)

    def test_late_fill_from_previous_flatten_attempt_is_accounted(self) -> None:
        session = start_session(make_config(max_round_trips=1))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        open_window(session, "buy-1", "BUY", traded=1)
        flatten = expire_window(session)[0]
        client_id = flatten.payload["client_id"]
        second = session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1, client_id=client_id))[0]
        self.assertEqual(second.kind, "submit_order")
        second_id = second.payload["client_id"]
        session.handle(TradeEvent("flatten-1", "rb2601", "SHFE", "SELL", 1, 99, "late-flatten", client_id=client_id))
        self.assertEqual(session.final_net_position, 0)
        session.handle(OrderEvent("flatten-2", "rb2601", "SHFE", "SELL", "CANCELLED", 1, client_id=second_id))
        finish_flattened_round(session, "flatten-2", flatten_side="SELL")
        self.assertEqual(session.state, SessionState.FINISHED)

    def test_short_position_uses_buy_close_on_non_shfe(self) -> None:
        session = start_session(make_config(exchange="DCE"))
        session.handle(TickEvent("rb2601", "DCE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "DCE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "DCE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("sell-1", "rb2601", "DCE", "SELL", 1, 140, "trade-1"))
        open_window(session, "sell-1", "SELL", traded=1, exchange="DCE")
        flatten = expire_window(session)[0]
        self.assertEqual(flatten.payload["side"], "BUY")
        self.assertEqual(flatten.payload["offset"], "CLOSE")
        self.assertEqual(flatten.payload["price"], 101)

    def test_ine_long_position_uses_close_today(self) -> None:
        session = start_session(make_config(exchange="INE"))
        session.handle(TickEvent("rb2601", "INE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "INE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "INE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "INE", "BUY", 1, 60, "trade-1"))
        open_window(session, "buy-1", "BUY", traded=1, exchange="INE")
        flatten = expire_window(session)[0]
        self.assertEqual(flatten.payload["offset"], "CLOSETODAY")

    def test_partial_flatten_retries_only_residual_position(self) -> None:
        session = start_session(make_config(target_lots=2))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 2, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 2, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 2, 60, "trade-1"))
        open_window(session, "buy-1", "BUY", traded=2, volume=2)
        flatten = expire_window(session)[0]
        self.assertEqual(flatten.payload["volume"], 2)
        client_id = flatten.payload["client_id"]
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "PARTTRADED", 2, traded=1, client_id=client_id))
        session.handle(TradeEvent("flatten-1", "rb2601", "SHFE", "SELL", 1, 99, "flatten-trade", client_id=client_id))
        retry = session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "CANCELLED", 2, traded=1, client_id=client_id))
        self.assertEqual(len(retry), 1)
        self.assertEqual(retry[0].payload["volume"], 1)

    def test_flatten_timeout_cancels_active_order_and_exposes_residual(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        open_window(session, "buy-1", "BUY", traded=1)
        flatten = expire_window(session)[0]
        client_id = flatten.payload["client_id"]
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=client_id))
        actions = session.handle(ClockEvent(session._now + session.config.effective["flatten_timeout_seconds"] + 0.1))
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertEqual(session.failure_reason, "flatten_timeout")
        self.assertEqual(session.final_net_position, 1)
        self.assertEqual([action.kind for action in actions], ["cancel_order", "cancel_order"])

    def test_flatten_adverse_price_is_capped_at_ten_ticks(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        open_window(session, "buy-1", "BUY", traded=1)
        flatten = expire_window(session)[0]
        client_id = flatten.payload["client_id"]
        session.handle(TickEvent("rb2601", "SHFE", 80, 80, 81, 1))
        retry = session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1, client_id=client_id))
        self.assertEqual(retry[0].payload["price"], 89)

    def test_invalid_book_waits_for_recovery_before_flatten(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        session.handle(TickEvent("rb2601", "SHFE", 0, 0, 0, 3))
        open_window(session, "buy-1", "BUY", traded=1)

        # 窗口超时但无有效盘口：等待而不立即失败
        self.assertEqual(expire_window(session), [])
        self.assertEqual(session.state, SessionState.FLATTENING)
        self.assertIsNone(session.failure_reason)
        self.assertEqual(session.handle(ClockEvent(session._now + 0.5)), [])

        # 盘口恢复：行情事件本身触发受限 FAK 平仓（仍在 flatten_timeout 窗口内）
        flatten = session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, session._now + 0.2))
        self.assertEqual(flatten[0].kind, "submit_order")
        self.assertEqual(flatten[0].payload["order_type"], "FAK")

    def test_invalid_book_until_flatten_timeout_fails(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        session.handle(TickEvent("rb2601", "SHFE", 0, 0, 0, 3))
        open_window(session, "buy-1", "BUY", traded=1)
        expire_window(session)
        self.assertEqual(session.state, SessionState.FLATTENING)
        session.handle(ClockEvent(session._now + session.config.effective["flatten_timeout_seconds"] + 0.1))
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertEqual(session.failure_reason, "flatten_timeout")

    def test_flatten_rejection_is_failed(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        open_window(session, "buy-1", "BUY", traded=1)
        flatten = expire_window(session)[0]
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "REJECTED", 1, client_id=flatten.payload["client_id"]))
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertEqual(session.failure_reason, "flatten_rejected")

    def test_flatten_trade_waits_for_terminal_order_callback_before_finishing(self) -> None:
        session = start_session(make_config(max_round_trips=1))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        open_window(session, "buy-1", "BUY", traded=1)
        flatten = expire_window(session)[0]
        client_id = flatten.payload["client_id"]
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "PARTTRADED", 1, traded=1, client_id=client_id))
        session.handle(TradeEvent("flatten-1", "rb2601", "SHFE", "SELL", 1, 99, "flatten-trade", client_id=client_id))
        self.assertEqual(session.state, SessionState.FLATTENING)
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "ALLTRADED", 1, traded=1, client_id=client_id))
        finish_flattened_round(session, "flatten-1", flatten_side="SELL")
        self.assertEqual(session.state, SessionState.FINISHED)

    def test_interrupt_uses_cancel_and_reconcile_instead_of_direct_exit(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        for number, action in enumerate(submitted, 1):
            session.handle(
                OrderEvent(
                    f"order-{number}",
                    "rb2601",
                    "SHFE",
                    action.payload["side"],
                    "NOTTRADED",
                    1,
                    client_id=action.payload["client_id"],
                )
            )
        actions = session.handle(InterruptEvent())
        self.assertEqual(session.state, SessionState.CLOSING_CANCELS)
        self.assertEqual([action.kind for action in actions], ["cancel_order", "cancel_order"])


def bind_quotes(session: LiveGridSession, submitted, tag: str) -> tuple[str, str]:
    buy_id, sell_id = f"{tag}-buy", f"{tag}-sell"
    buy = next(action for action in submitted if action.payload["side"] == "BUY")
    sell = next(action for action in submitted if action.payload["side"] == "SELL")
    session.handle(OrderEvent(buy_id, "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
    session.handle(OrderEvent(sell_id, "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
    return buy_id, sell_id


def complete_round(session: LiveGridSession, tag: str, filled_side: str, net: int) -> str:
    """价差窗口超时后：FAK 平回零仓、平仓终态撤对侧、对账收尾；返回平仓委托 client_id。"""
    buy_id, sell_id = f"{tag}-buy", f"{tag}-sell"
    filled = buy_id if filled_side == "BUY" else sell_id
    open_window(session, filled, filled_side, traded=1)
    flatten = expire_window(session)[0]
    client_id = flatten.payload["client_id"]
    flatten_side = "SELL" if net > 0 else "BUY"
    session.handle(OrderEvent(f"{tag}-flat", "rb2601", "SHFE", flatten_side, "ALLTRADED", 1, traded=1, client_id=client_id))
    session.handle(TradeEvent(f"{tag}-flat", "rb2601", "SHFE", flatten_side, 1, 99 if net > 0 else 101, f"{tag}-f-trade", client_id=client_id))
    opposite = sell_id if filled == buy_id else buy_id
    opposite_side = "SELL" if opposite == sell_id else "BUY"
    closing = session.handle(OrderEvent(opposite, "rb2601", "SHFE", opposite_side, "CANCELLED", 1))
    query = next(action for action in closing if action.kind == "query_position").payload["request_id"]
    session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 0))
    return client_id


def complete_interrupted_round(session: LiveGridSession, tag: str, filled_side: str, net: int) -> None:
    """窗口期间中断后：撤全部 → 对账 → FAK 平净仓 → 对账收尾。"""
    buy_id, sell_id = f"{tag}-buy", f"{tag}-sell"
    filled = buy_id if filled_side == "BUY" else sell_id
    session.handle(OrderEvent(filled, "rb2601", "SHFE", filled_side, "CANCELLED", 1, traded=1))
    opposite = sell_id if filled == buy_id else buy_id
    opposite_side = "SELL" if opposite == sell_id else "BUY"
    closing = session.handle(OrderEvent(opposite, "rb2601", "SHFE", opposite_side, "CANCELLED", 1))
    query = next(action for action in closing if action.kind == "query_position").payload["request_id"]
    flatten = session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", net))[0]
    client_id = flatten.payload["client_id"]
    flatten_side = "SELL" if net > 0 else "BUY"
    final = session.handle(OrderEvent(f"{tag}-flat", "rb2601", "SHFE", flatten_side, "ALLTRADED", 1, traded=1, client_id=client_id))
    session.handle(TradeEvent(f"{tag}-flat", "rb2601", "SHFE", flatten_side, 1, 99 if net > 0 else 101, f"{tag}-f-trade", client_id=client_id))
    if final and final[0].kind == "cancel_order":
        final = session.handle(OrderEvent(opposite, "rb2601", "SHFE", opposite_side, "CANCELLED", 1))
    query2 = next(action for action in final if action.kind == "query_position").payload["request_id"]
    session.handle(PositionQueryCompleteEvent(query2, "rb2601", "SHFE", 0))


class ContinuousQuotingTests(unittest.TestCase):
    def test_completed_round_resumes_quoting_until_max_round_trips(self) -> None:
        session = start_session(make_config(max_round_trips=2))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        bind_quotes(session, submitted, "r1")

        session.handle(TradeEvent("r1-buy", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        complete_round(session, "r1", "BUY", 1)
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertEqual(session.summary()["round_trips"], 1)
        self.assertIsNone(session.summary()["stop_reason"])

        session.handle(TickEvent("rb2601", "SHFE", 200, 199, 201, 3))
        submitted = session.handle(ClockEvent(10))
        self.assertEqual([action.kind for action in submitted], ["submit_order", "submit_order"])
        self.assertEqual(
            sorted(action.payload["price"] for action in submitted),
            [160, 240],
        )
        bind_quotes(session, submitted, "r2")

        session.handle(TradeEvent("r2-sell", "rb2601", "SHFE", "SELL", 1, 240, "trade-2"))
        complete_round(session, "r2", "SELL", -1)
        self.assertEqual(session.state, SessionState.FINISHED)
        summary = session.summary()
        self.assertEqual(summary["round_trips"], 2)
        self.assertEqual(summary["stop_reason"], "max_round_trips")
        self.assertEqual(summary["final_net_position"], 0)

    def test_session_end_time_stops_quoting_and_closes_cleanly(self) -> None:
        from datetime import datetime, timedelta

        soon = (datetime.now() + timedelta(minutes=1)).strftime("%H:%M")
        session = start_session(make_config(session_end_time=soon, stable_market_seconds=2))
        at0 = time.monotonic()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, at0))
        submitted = session.handle(ClockEvent(at0 + 3))
        self.assertEqual([action.kind for action in submitted], ["submit_order", "submit_order"])
        bind_quotes(session, submitted, "e1")

        actions = session.handle(ClockEvent(at0 + 120))
        self.assertEqual(session.state, SessionState.CLOSING_CANCELS)
        self.assertEqual([action.kind for action in actions], ["cancel_order", "cancel_order"])
        session.handle(OrderEvent("e1-buy", "rb2601", "SHFE", "BUY", "CANCELLED", 1))
        closing = session.handle(OrderEvent("e1-sell", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = next(action for action in closing if action.kind == "query_position").payload["request_id"]
        session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 0))
        self.assertEqual(session.state, SessionState.FINISHED)
        self.assertEqual(session.summary()["stop_reason"], "session_end")

    def test_past_clock_time_wraps_to_next_day_and_keeps_quoting(self) -> None:
        from datetime import datetime

        now = datetime.now()
        if now.minute >= 1:
            past = now.replace(minute=now.minute - 1).strftime("%H:%M")
        elif now.hour >= 1:
            past = f"{now.hour - 1:02d}:59"
        else:
            past = "00:00"
        session = start_session(make_config(session_end_time=past))
        at0 = time.monotonic()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, at0))
        submitted = session.handle(ClockEvent(at0 + 3))
        self.assertEqual([action.kind for action in submitted], ["submit_order", "submit_order"])
        self.assertEqual(session.state, SessionState.QUOTING)

    def test_late_fill_during_stable_wait_after_round_triggers_new_closing(self) -> None:
        session = start_session(make_config(max_round_trips=5))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        bind_quotes(session, submitted, "r1")
        session.handle(TradeEvent("r1-buy", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        complete_round(session, "r1", "BUY", 1)
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)

        # 上一轮已撤卖单在稳定等待期迟到成交：立即进入新一轮收口
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 4))
        session.handle(TradeEvent("r1-sell", "rb2601", "SHFE", "SELL", 1, 140, "late-trade"))
        self.assertEqual(session.state, SessionState.CLOSING_RECONCILE)
        self.assertIsNone(session.failure_reason)
        query = session.actions[-1].payload["request_id"]
        flatten = session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", -1))[0]
        client_id = flatten.payload["client_id"]
        final = session.handle(OrderEvent("r1-late-flat", "rb2601", "SHFE", "BUY", "ALLTRADED", 1, traded=1, client_id=client_id))
        if not any(action.kind == "query_position" for action in final):
            final += session.handle(TradeEvent("r1-late-flat", "rb2601", "SHFE", "BUY", 1, 101, "f2-trade", client_id=client_id))
        query2 = next(action for action in final if action.kind == "query_position").payload["request_id"]
        session.handle(PositionQueryCompleteEvent(query2, "rb2601", "SHFE", 0))
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertEqual(session.summary()["round_trips"], 2)

    def test_interrupt_during_round_closing_stops_after_flatten(self) -> None:
        session = start_session(make_config(max_round_trips=5))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        bind_quotes(session, submitted, "r1")
        session.handle(TradeEvent("r1-buy", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        self.assertEqual(session.state, SessionState.CLOSING_WAIT)
        session.handle(InterruptEvent())
        self.assertEqual(session.stop_reason, "interrupted")
        self.assertEqual(session.state, SessionState.CLOSING_CANCELS)
        complete_interrupted_round(session, "r1", "BUY", 1)
        self.assertEqual(session.state, SessionState.FINISHED)
        summary = session.summary()
        self.assertEqual(summary["stop_reason"], "interrupted")
        self.assertEqual(summary["round_trips"], 1)

    def test_stop_reason_first_wins_over_max_round_trips(self) -> None:
        session = start_session(make_config(max_round_trips=1))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        bind_quotes(session, submitted, "r1")
        session.handle(TradeEvent("r1-buy", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        session.handle(InterruptEvent())
        complete_interrupted_round(session, "r1", "BUY", 1)
        self.assertEqual(session.state, SessionState.FINISHED)
        self.assertEqual(session.summary()["stop_reason"], "interrupted")

    def test_late_flatten_fill_that_changes_net_triggers_new_closing(self) -> None:
        session = start_session(make_config(max_round_trips=5))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        bind_quotes(session, submitted, "r1")
        session.handle(TradeEvent("r1-buy", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        flatten_client = complete_round(session, "r1", "BUY", 1)
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)

        # 平仓单迟到成交回报（新的成交编号）使净仓再次非零
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 4))
        session.handle(
            TradeEvent("r1-flat", "rb2601", "SHFE", "SELL", 1, 99, "dup-flatten-trade", client_id=flatten_client)
        )
        self.assertEqual(session.state, SessionState.CLOSING_RECONCILE)
        query = session.actions[-1].payload["request_id"]
        flatten = session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", -1))[0]
        client_id = flatten.payload["client_id"]
        final = session.handle(OrderEvent("r2-flat", "rb2601", "SHFE", "BUY", "ALLTRADED", 1, traded=1, client_id=client_id))
        if not any(action.kind == "query_position" for action in final):
            final += session.handle(TradeEvent("r2-flat", "rb2601", "SHFE", "BUY", 1, 101, "r2-flat-trade", client_id=client_id))
        query2 = next(action for action in final if action.kind == "query_position").payload["request_id"]
        session.handle(PositionQueryCompleteEvent(query2, "rb2601", "SHFE", 0))
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertEqual(session.summary()["round_trips"], 2)


if __name__ == "__main__":
    unittest.main()
