import time
import unittest

from live_grid.config import MultiContractConfig, StrategyConfig
from live_grid.session import (
    ClockEvent,
    ConnectionEvent,
    ContractEvent,
    InterruptEvent,
    LiveGridSession,
    OrderActionErrorEvent,
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
        "max_tick_age_seconds": 60,
        "quote_windows": [{"start": "00:00", "end": "23:59"}],
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


def qualify_market(
    session: LiveGridSession,
    last: float = 100,
    bid: float = 99,
    ask: float = 101,
    start: float = 0.0,
    *,
    symbol: str = "rb2601",
    exchange: str = "SHFE",
):
    """两条新鲜有效 Tick 后再等到稳定窗口结束，使 WAITING_FOR_STABLE_QUOTE 可以挂单。"""
    max_age = float(session.config.effective["max_tick_age_seconds"])
    stable = float(session.config.effective["stable_market_seconds"])
    gap = min(0.5, max_age, max(stable / 2.0, 1e-9))
    session.handle(TickEvent(symbol, exchange, last, bid, ask, start))
    session.handle(TickEvent(symbol, exchange, last, bid, ask, start + gap))
    return session.handle(ClockEvent(start + stable))


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
        qualify_market(session)
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
        self.assertEqual(session.handle(ClockEvent(2.0)), [])
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 2.0))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 2.5))
        actions = session.handle(ClockEvent(4.0))
        self.assertEqual([action.kind for action in actions], ["submit_order", "submit_order"])
        self.assertEqual({action.payload["side"] for action in actions}, {"BUY", "SELL"})

    def test_stable_market_requires_two_fresh_ticks_before_quoting(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0.5))
        actions = session.handle(ClockEvent(2.0))
        self.assertEqual([action.kind for action in actions], ["submit_order", "submit_order"])

    def test_stable_market_gap_above_max_tick_age_restarts_two_second_window(self) -> None:
        session = start_session(make_config(max_tick_age_seconds=1.5))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 1.6))
        self.assertEqual(session.handle(ClockEvent(2.0)), [])
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 2.1))
        actions = session.handle(ClockEvent(3.6))
        self.assertEqual([action.kind for action in actions], ["submit_order", "submit_order"])

    def test_stable_market_stale_last_tick_at_submit_restarts_two_second_window(self) -> None:
        session = start_session(make_config(max_tick_age_seconds=1.5))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0.4))
        self.assertEqual(session.handle(ClockEvent(2.0)), [])
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 2.0))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 2.5))
        actions = session.handle(ClockEvent(4.0))
        self.assertEqual([action.kind for action in actions], ["submit_order", "submit_order"])

    def test_prices_are_tick_aligned_and_crossing_without_trade_does_not_close(self) -> None:
        session = start_session()
        actions = qualify_market(session)
        self.assertEqual([action.payload["price"] for action in actions], [60, 140])
        session.handle(TickEvent("rb2601", "SHFE", 60, 59, 61, 3))
        self.assertEqual(session.state, SessionState.QUOTING)

    def test_partial_first_fill_flattens_residual_after_window(self) -> None:
        session = start_session(make_config(target_lots=2))
        submitted = qualify_market(session)
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

    def test_order_fill_in_quoting_starts_closing_before_trade_event(self) -> None:
        session = start_session(make_config(max_round_trips=1))
        submitted = qualify_market(session)
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))

        actions = session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "ALLTRADED", 1, traded=1, price=60))
        self.assertEqual(session.state, SessionState.CLOSING_WAIT)
        self.assertEqual(session.first_fill["volume"], 1)
        self.assertEqual(session.first_fill["side"], "BUY")
        self.assertFalse(any(action.kind == "submit_order" for action in actions))

        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        self.assertEqual(session.state, SessionState.CLOSING_WAIT)
        flatten = expire_window(session)[0]
        self.assertEqual(flatten.payload["volume"], 1)
        self.assertEqual(flatten.payload["side"], "SELL")

    def test_order_fill_during_reanchor_starts_closing_instead_of_requoting(self) -> None:
        session = start_session()
        submitted = qualify_market(session)
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
        session.handle(TickEvent("rb2601", "SHFE", 121, 120, 122, 3))
        session.handle(TickEvent("rb2601", "SHFE", 121, 120, 122, 4))
        self.assertEqual(session.state, SessionState.REPLACING)

        session.handle(OrderEvent("order-1", "rb2601", "SHFE", "BUY", "ALLTRADED", 1, traded=1))
        self.assertEqual(session.state, SessionState.CLOSING_WAIT)
        session.handle(OrderEvent("order-2", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        session.handle(TickEvent("rb2601", "SHFE", 121, 120, 122, 6))
        actions = session.handle(ClockEvent(8))
        self.assertEqual(session.state, SessionState.FLATTENING)
        self.assertEqual([action.payload.get("order_type") for action in actions], ["FAK"])
        self.assertEqual(actions[0].payload["side"], "SELL")

    def test_order_fill_in_stable_wait_starts_late_closing_without_trade_event(self) -> None:
        session = start_session(make_config(max_round_trips=5))
        submitted = qualify_market(session)
        bind_quotes(session, submitted, "r1")
        session.handle(TradeEvent("r1-buy", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        complete_round(session, "r1", "BUY", 1)
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)

        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 4))
        session.handle(OrderEvent("r1-sell", "rb2601", "SHFE", "SELL", "ALLTRADED", 1, traded=1))
        self.assertNotEqual(session.state, SessionState.QUOTING)
        self.assertIn(session.state, {SessionState.CLOSING_CANCELS, SessionState.CLOSING_RECONCILE})
        self.assertIsNone(session.failure_reason)

    def test_opposite_fill_in_window_completes_spread_without_flatten(self) -> None:
        session = start_session(make_config(max_round_trips=1))
        submitted = qualify_market(session)
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
        submitted = qualify_market(session)
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

    def test_zero_window_flattens_immediately_after_first_fill(self) -> None:
        session = start_session(make_config(closing_wait_seconds=0, max_round_trips=1))
        submitted = qualify_market(session)
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))

        actions = session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        self.assertEqual(session.state, SessionState.FLATTENING)
        self.assertEqual([action.kind for action in actions], ["submit_order"])
        self.assertEqual(actions[0].payload["order_type"], "FAK")
        open_window(session, "buy-1", "BUY", traded=1)
        flatten_id = actions[0].payload["client_id"]
        cancel_actions = session.handle(OrderEvent("f-1", "rb2601", "SHFE", "SELL", "ALLTRADED", 1, price=99, traded=1, client_id=flatten_id))
        self.assertEqual([action.kind for action in cancel_actions], ["cancel_order"])
        closing = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        session.handle(PositionQueryCompleteEvent(closing[0].payload["request_id"], "rb2601", "SHFE", 0))
        self.assertEqual(session.state, SessionState.FINISHED)

    def test_same_order_second_fill_during_window_updates_flatten_volume(self) -> None:
        session = start_session(make_config(target_lots=2))
        submitted = qualify_market(session)
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 2, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 2, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        self.assertEqual(session.state, SessionState.CLOSING_WAIT)

        # 同一委托在窗口内补满 1 手：净仓记为 2，超时 FAK 平 2 手。
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "PARTTRADED", 2, traded=2))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-2"))
        flatten = expire_window(session)[0]
        self.assertEqual(session.state, SessionState.FLATTENING)
        self.assertEqual(flatten.payload["volume"], 2)

    def test_opposite_fill_during_flatten_flips_net_and_reflattens(self) -> None:
        session = start_session(make_config(target_lots=2, max_round_trips=1))
        submitted = qualify_market(session)
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 2, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 2, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        open_window(session, "buy-1", "BUY", traded=1, volume=2)
        flatten = expire_window(session)[0]
        self.assertEqual(flatten.payload["volume"], 1)
        client_id = flatten.payload["client_id"]
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=client_id))

        # FAK 在途时对侧成交 2 手：净仓翻为 -1，先撤 FAK 再按翻向后的净仓重新平仓。
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "ALLTRADED", 2, traded=2))
        session.handle(TradeEvent("sell-1", "rb2601", "SHFE", "SELL", 2, 140, "late-trade"))
        self.assertEqual(session.final_net_position, -1)
        retry = session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1, traded=0, client_id=client_id))
        self.assertEqual(retry[0].payload["side"], "BUY")
        self.assertEqual(retry[0].payload["volume"], 1)
        retry_id = retry[0].payload["client_id"]
        final = session.handle(OrderEvent("f-2", "rb2601", "SHFE", "BUY", "ALLTRADED", 1, price=101, traded=1, client_id=retry_id))
        if not any(action.kind == "query_position" for action in final):
            final += session.handle(TradeEvent("f-2", "rb2601", "SHFE", "BUY", 1, 101, "flat-trade", client_id=retry_id))
        query = next(action for action in final if action.kind == "query_position").payload["request_id"]
        session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 0))
        self.assertEqual(session.state, SessionState.FINISHED)

    def test_flatten_rejection_with_zero_net_stays_failed_without_resurrection(self) -> None:
        session = start_session(make_config(max_round_trips=1))
        submitted = qualify_market(session)
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        open_window(session, "buy-1", "BUY", traded=1)
        flatten = expire_window(session)[0]
        client_id = flatten.payload["client_id"]
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=client_id))
        # 迟到对侧成交与 FAK 拒单都要保留风险托管，不被复活回收口。
        session.handle(TradeEvent("sell-1", "rb2601", "SHFE", "SELL", 1, 140, "late-trade"))
        actions = session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "REJECTED", 1, client_id=client_id))
        self.assertEqual(session.state, SessionState.RISK_HOLD)
        self.assertEqual(session.failure_reason, "flatten_rejected")
        self.assertFalse(any(action.kind == "query_position" for action in actions))

    def test_first_fill_opens_window_then_flattens_and_cancels_opposite_after_timeout(self) -> None:
        session = start_session(make_config(closing_wait_seconds=1, max_round_trips=1))
        submitted = qualify_market(session)
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

    def test_order_fill_starts_window_immediately_and_later_trade_does_not_restart_it(self) -> None:
        session = start_session(make_config(closing_wait_seconds=1, max_round_trips=1))
        submitted = qualify_market(session)
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))

        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "ALLTRADED", 1, traded=1))
        self.assertEqual(session.state, SessionState.CLOSING_WAIT)
        self.assertEqual(session._window_started_at, 2)

        self.assertEqual(session.handle(ClockEvent(2.999)), [])
        self.assertEqual(session.state, SessionState.CLOSING_WAIT)

        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        self.assertEqual(session.state, SessionState.CLOSING_WAIT)
        self.assertEqual(session._window_started_at, 2)

        self.assertEqual(len(session.handle(ClockEvent(3.0))), 1)
        self.assertEqual(session.state, SessionState.FLATTENING)

    def test_first_full_fill_flattens_then_cancels_opposite_and_reconciles(self) -> None:
        session = start_session()
        submitted = qualify_market(session)
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
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0.05))
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
            session.handle(TickEvent("rb2601", "SHFE", outside, outside - 1, outside + 1, base + 0.27))
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
        session = start_session(make_config(w_ticks=1, d_ticks=1, s_ticks=1, book_protection_multiple=2))
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
        self.assertEqual(session.state, SessionState.RISK_HOLD)
        self.assertEqual(session.failure_reason, "nonzero_startup_position")
        self.assertEqual(session.final_net_position, 1)

    def test_interrupt_before_zero_confirmation_does_not_flatten_existing_position(self) -> None:
        config = make_config()
        session = LiveGridSession(config, simnow_confirmed=True)
        session.handle(ContractEvent("rb2601", "SHFE", 1.0))

        self.assertEqual(session.handle(InterruptEvent()), [])
        self.assertEqual(session.state, SessionState.RISK_HOLD)
        self.assertEqual(session.failure_reason, "interrupted_before_zero_position")
        self.assertEqual(session.stop_reason, "interrupted")
        self.assertEqual(session.final_net_position, None)

    def test_replacement_waits_for_terminal_callbacks_and_then_requalifies_market(self) -> None:
        session = start_session()
        submitted = qualify_market(session)
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
        delayed = session.handle(ClockEvent(5))
        self.assertEqual([action.kind for action in delayed[:2]], ["cancel_order", "cancel_order"])
        self.assertTrue(any(action.kind == "audit_warning" for action in delayed))
        session.handle(OrderEvent("order-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1))
        self.assertEqual(session.state, SessionState.REPLACING)
        session.handle(OrderEvent("order-2", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertEqual(session.handle(ClockEvent(5.9)), [])
        session.handle(TickEvent("rb2601", "SHFE", 121, 120, 122, 6))
        session.handle(TickEvent("rb2601", "SHFE", 121, 120, 122, 6.5))
        self.assertEqual(session.handle(ClockEvent(7.9)), [])
        self.assertEqual([a.kind for a in session.handle(ClockEvent(8))], ["submit_order", "submit_order"])

    def test_reanchor_requires_two_seconds_of_new_valid_market_data(self) -> None:
        session = start_session()
        submitted = qualify_market(session)
        for number, action in enumerate(submitted, 1):
            session.handle(OrderEvent(f"order-{number}", "rb2601", "SHFE", action.payload["side"], "NOTTRADED", 1, client_id=action.payload["client_id"]))
        session.handle(TickEvent("rb2601", "SHFE", 121, 120, 122, 3))
        session.handle(TickEvent("rb2601", "SHFE", 121, 120, 122, 4))
        session.handle(OrderEvent("order-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1))
        session.handle(OrderEvent("order-2", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertEqual(session.handle(ClockEvent(7)), [])
        session.handle(TickEvent("rb2601", "SHFE", 121, 120, 122, 7))
        session.handle(TickEvent("rb2601", "SHFE", 121, 120, 122, 7.5))
        self.assertEqual(session.handle(ClockEvent(8.9)), [])
        actions = session.handle(ClockEvent(9))
        self.assertEqual([action.kind for action in actions], ["submit_order", "submit_order"])
        self.assertEqual([action.payload["price"] for action in actions], [70, 150])

    def test_reanchor_step_within_band_lands_price_inside_new_band(self) -> None:
        session = start_session(make_config(w_ticks=2, d_ticks=2, s_ticks=2))
        submitted = qualify_market(session, last=100, bid=99, ask=100)
        for number, action in enumerate(submitted, 1):
            session.handle(OrderEvent(f"order-{number}", "rb2601", "SHFE", action.payload["side"], "NOTTRADED", 1, client_id=action.payload["client_id"]))

        # 价格仅越出带宽 1 tick：重锚后必须落入新带内，不得进入反复撤挂循环。
        session.handle(TickEvent("rb2601", "SHFE", 103, 102, 103, 3))
        reanchor_actions = session.handle(TickEvent("rb2601", "SHFE", 103, 102, 103, 4.1))
        self.assertEqual([action.kind for action in reanchor_actions], ["cancel_order", "cancel_order"])
        self.assertEqual(session.audit_events[-1]["trace"][0]["calculation"]["new_anchor_ticks"], 102)
        for action in reanchor_actions:
            session.handle(
                OrderEvent(
                    action.payload["order_id"],
                    "rb2601",
                    "SHFE",
                    "BUY" if action.payload["client_id"].endswith("buy") else "SELL",
                    "CANCELLED",
                    1,
                    client_id=action.payload["client_id"],
                )
            )
        session.handle(TickEvent("rb2601", "SHFE", 103, 102, 103, 6))
        session.handle(TickEvent("rb2601", "SHFE", 103, 102, 103, 6.5))
        replacement_quotes = session.handle(ClockEvent(8))
        self.assertEqual([action.kind for action in replacement_quotes], ["submit_order", "submit_order"])
        self.assertEqual(session.state, SessionState.QUOTE_PENDING)
        for number, action in enumerate(replacement_quotes, 3):
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
        self.assertEqual(session.state, SessionState.QUOTING)

        # 重挂后同价行情不再触发重锚：会话稳定在 QUOTING。
        self.assertEqual(session.handle(TickEvent("rb2601", "SHFE", 103, 102, 103, 9)), [])
        self.assertEqual(session.state, SessionState.QUOTING)

    def test_safety_cancel_bypasses_action_limit_but_replacement_submit_does_not(self) -> None:
        session = start_session(make_config(action_limit_per_minute=2))
        submitted = qualify_market(session)
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
        actions = qualify_market(session)
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertEqual([action.kind for action in actions], ["audit_warning"])

    def test_cancel_timeout_still_reconciles_and_marks_failure(self) -> None:
        session = start_session()
        submitted = qualify_market(session)
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
        self.assertEqual(session.state, SessionState.RISK_HOLD)
        self.assertEqual(session.failure_reason, "cancel_timeout")
        self.assertTrue(any(action.kind == "cancel_order" for action in actions))

    def test_closing_query_failure_does_not_reuse_startup_zero_position(self) -> None:
        session = start_session()
        submitted = qualify_market(session)
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        query = drive_to_final_reconcile(session)
        session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 0, error_id=9, error_msg="query failed"))
        self.assertEqual(session.state, SessionState.RISK_HOLD)
        self.assertEqual(session.failure_reason, "closing_position_query_failed")
        self.assertIsNone(session.final_net_position)

    def test_nonmatching_closing_query_cannot_start_flattening(self) -> None:
        session = start_session()
        submitted = qualify_market(session)
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
        submitted = qualify_market(session)
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
        submitted = qualify_market(session)
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        open_window(session, "buy-1", "BUY", traded=1)
        flatten = expire_window(session)[0]
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "REJECTED", 1, client_id=flatten.payload["client_id"]))
        session.handle(TradeEvent("sell-1", "rb2601", "SHFE", "SELL", 1, 140, "late-trade"))
        self.assertEqual(session.state, SessionState.RISK_HOLD)
        self.assertEqual(session.failure_reason, "flatten_rejected")
        self.assertEqual(session.final_net_position, 1)

    def test_order_and_trade_callbacks_for_same_late_fill_count_once(self) -> None:
        session = start_session()
        submitted = qualify_market(session)
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
        submitted = qualify_market(session)
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
        submitted = qualify_market(session)
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
        submitted = qualify_market(session, symbol="rb2601", exchange="DCE")
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
        submitted = qualify_market(session, symbol="rb2601", exchange="INE")
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
        submitted = qualify_market(session)
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
        submitted = qualify_market(session)
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
        self.assertEqual(session.state, SessionState.RISK_HOLD)
        self.assertEqual(session.failure_reason, "flatten_timeout")
        self.assertEqual(session.final_net_position, 1)
        self.assertEqual([action.kind for action in actions], ["cancel_order", "cancel_order"])

    def test_flatten_adverse_price_is_capped_at_ten_ticks(self) -> None:
        session = start_session()
        submitted = qualify_market(session)
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
        submitted = qualify_market(session)
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
        submitted = qualify_market(session)
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
        self.assertEqual(session.state, SessionState.RISK_HOLD)
        self.assertEqual(session.failure_reason, "flatten_timeout")

    def test_flatten_rejection_is_risk_hold(self) -> None:
        session = start_session()
        submitted = qualify_market(session)
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        open_window(session, "buy-1", "BUY", traded=1)
        flatten = expire_window(session)[0]
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "REJECTED", 1, client_id=flatten.payload["client_id"]))
        self.assertEqual(session.state, SessionState.RISK_HOLD)
        self.assertEqual(session.failure_reason, "flatten_rejected")

    def test_flatten_trade_waits_for_terminal_order_callback_before_finishing(self) -> None:
        session = start_session(make_config(max_round_trips=1))
        submitted = qualify_market(session)
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
        submitted = qualify_market(session)
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
    def test_multi_contract_parameters_drive_distinct_quotes_and_independent_stops(self) -> None:
        first = {k: v for k, v in make_config(max_round_trips=1).effective.items() if k != "version"}
        second = {**first, "symbol": "rb2610", "w_ticks": 10, "d_ticks": 5,
                  "s_ticks": 5, "max_round_trips": 2, "closing_wait_seconds": 3}
        config = MultiContractConfig.from_mapping({"version": 2, "contracts": [first, second]})
        sessions = [start_session(c) for c in config.contracts]
        for session, expected in zip(sessions, ([60, 140], [85, 115])):
            symbol = session.config.effective["symbol"]
            submitted = qualify_market(session, symbol=symbol)
            self.assertEqual(sorted(a.payload["price"] for a in submitted), expected)
            # Complete both sides inside the spread window, then reconcile a zero position.
            for action in submitted:
                p = action.payload
                session.handle(OrderEvent(p["client_id"], symbol, "SHFE", p["side"], "ALLTRADED",
                                          1, traded=1, price=p["price"], client_id=p["client_id"]))
                session.handle(TradeEvent(p["client_id"], symbol, "SHFE", p["side"],
                                          1, p["price"], p["client_id"] + "-fill", client_id=p["client_id"]))
            query = next(a for a in reversed(session.actions) if a.kind == "query_position")
            session.handle(PositionQueryCompleteEvent(query.payload["request_id"], symbol, "SHFE", 0))
            self.assertEqual(session.summary()["round_trips"], 1)
        self.assertEqual(sessions[0].state, SessionState.FINISHED)
        self.assertEqual(sessions[0].stop_reason, "max_round_trips")
        self.assertEqual(sessions[1].state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertIsNone(sessions[1].stop_reason)
        self.assertEqual([a.payload["price"] for a in qualify_market(sessions[1], symbol="rb2610", start=5)],
                         [85, 115])

    def test_completed_round_resumes_quoting_until_max_round_trips(self) -> None:
        session = start_session(make_config(max_round_trips=2))
        submitted = qualify_market(session)
        bind_quotes(session, submitted, "r1")

        session.handle(TradeEvent("r1-buy", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        complete_round(session, "r1", "BUY", 1)
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertEqual(session.summary()["round_trips"], 1)
        self.assertIsNone(session.summary()["stop_reason"])

        submitted = qualify_market(session, last=200, bid=199, ask=201, start=3)
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

    def test_quote_windows_cancel_before_break_resume_and_finish_at_final_close(self) -> None:
        from datetime import datetime

        windows = [
            {"start": "09:00", "end": "10:15"},
            {"start": "10:30", "end": "11:30"},
            {"start": "13:30", "end": "15:00"},
        ]
        session = start_session(make_config(quote_windows=windows))
        session._created_wall_time = datetime(2026, 8, 24, 8, 50)
        first = qualify_market(session, start=100)
        bind_quotes(session, first, "morning")

        actions = session.handle(ClockEvent(110, wall_time="2026-08-24T10:14:55"))
        self.assertEqual(session.state, SessionState.CLOSING_CANCELS)
        self.assertEqual([action.kind for action in actions], ["cancel_order", "cancel_order"])
        session.handle(OrderEvent("morning-buy", "rb2601", "SHFE", "BUY", "CANCELLED", 1))
        closing = session.handle(OrderEvent("morning-sell", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = next(action for action in closing if action.kind == "query_position").payload["request_id"]
        session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 0))
        self.assertEqual(session.state, SessionState.PAUSED)

        session.handle(ClockEvent(120, wall_time="2026-08-24T10:30:00"))
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        second = qualify_market(session, start=120)
        self.assertEqual([action.kind for action in second], ["submit_order", "submit_order"])
        bind_quotes(session, second, "afternoon")

        actions = session.handle(ClockEvent(130, wall_time="2026-08-24T14:59:55"))
        self.assertEqual(session.state, SessionState.CLOSING_CANCELS)
        self.assertEqual([action.kind for action in actions], ["cancel_order", "cancel_order"])
        session.handle(OrderEvent("afternoon-buy", "rb2601", "SHFE", "BUY", "CANCELLED", 1))
        closing = session.handle(OrderEvent("afternoon-sell", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = next(action for action in closing if action.kind == "query_position").payload["request_id"]
        session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 0))
        self.assertEqual(session.state, SessionState.FINISHED)
        self.assertEqual(session.summary()["stop_reason"], "quote_window_end")

    def test_window_pause_resets_stable_gate_without_active_quotes(self) -> None:
        from datetime import datetime

        session = start_session(
            make_config(
                quote_windows=[
                    {"start": "09:00", "end": "10:15"},
                    {"start": "10:30", "end": "11:30"},
                ]
            )
        )
        session._created_wall_time = datetime(2026, 8, 24, 8, 50)
        session.handle(ClockEvent(10, wall_time="2026-08-24T10:14:55"))
        self.assertEqual(session.state, SessionState.PAUSED)

        # 休市期间的 Tick 不得污染下一窗口的稳定行情门槛。
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 10.1))
        session.handle(ClockEvent(11, wall_time="2026-08-24T10:30:00"))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 12.0))
        self.assertEqual(session.handle(ClockEvent(13.0, wall_time="2026-08-24T10:30:01")), [])
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 13.0))
        self.assertEqual(
            [action.kind for action in session.handle(ClockEvent(15.0, wall_time="2026-08-24T10:30:03"))],
            ["submit_order", "submit_order"],
        )

    def test_stale_quote_cancels_active_orders_and_requires_fresh_ticks(self) -> None:
        session = start_session(make_config(max_tick_age_seconds=1.5))
        submitted = qualify_market(session)
        bind_quotes(session, submitted, "stale")

        actions = session.handle(ClockEvent(4))
        self.assertEqual(session.state, SessionState.REPLACING)
        self.assertEqual([action.kind for action in actions], ["cancel_order", "cancel_order"])
        session.handle(OrderEvent("stale-buy", "rb2601", "SHFE", "BUY", "CANCELLED", 1))
        session.handle(OrderEvent("stale-sell", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertEqual(session.handle(ClockEvent(5)), [])
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 5))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 5.5))
        self.assertEqual([action.kind for action in session.handle(ClockEvent(7))], ["submit_order", "submit_order"])

    def test_window_close_intent_survives_flattening(self) -> None:
        from datetime import datetime

        session = start_session(
            make_config(
                closing_wait_seconds=1,
                quote_windows=[{"start": "09:00", "end": "10:15"}],
            )
        )
        session._created_wall_time = datetime(2026, 8, 24, 8, 50)
        submitted = qualify_market(session)
        bind_quotes(session, submitted, "close")
        session.handle(TradeEvent("close-buy", "rb2601", "SHFE", "BUY", 1, 60, "close-trade"))

        flatten = session.handle(ClockEvent(3.1, wall_time="2026-08-24T10:14:51"))[0]
        self.assertEqual(session.state, SessionState.FLATTENING)
        session.handle(ClockEvent(4.0, wall_time="2026-08-24T10:14:55"))
        session.handle(
            OrderEvent(
                "close-flat",
                "rb2601",
                "SHFE",
                "SELL",
                "ALLTRADED",
                1,
                traded=1,
                client_id=flatten.payload["client_id"],
            )
        )
        session.handle(OrderEvent("close-buy", "rb2601", "SHFE", "BUY", "ALLTRADED", 1, traded=1))
        closing = session.handle(OrderEvent("close-sell", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = next(action for action in closing if action.kind == "query_position").payload["request_id"]
        session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 0))
        self.assertEqual(session.state, SessionState.FINISHED)
        self.assertEqual(session.summary()["stop_reason"], "quote_window_end")

    def test_cross_midnight_windows_keep_the_final_close_on_next_day(self) -> None:
        from datetime import datetime

        session = start_session(
            make_config(
                quote_windows=[
                    {"start": "21:00", "end": "23:00"},
                    {"start": "23:30", "end": "02:30"},
                ]
            )
        )
        session._created_wall_time = datetime(2026, 8, 24, 20, 0)
        first = qualify_market(session, start=100)
        bind_quotes(session, first, "night-1")
        actions = session.handle(ClockEvent(110, wall_time="2026-08-24T22:59:55"))
        self.assertEqual([action.kind for action in actions], ["cancel_order", "cancel_order"])
        session.handle(OrderEvent("night-1-buy", "rb2601", "SHFE", "BUY", "CANCELLED", 1))
        closing = session.handle(OrderEvent("night-1-sell", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = next(action for action in closing if action.kind == "query_position").payload["request_id"]
        session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 0))
        self.assertEqual(session.state, SessionState.PAUSED)

        session.handle(ClockEvent(120, wall_time="2026-08-24T23:30:00"))
        second = qualify_market(session, start=120)
        bind_quotes(session, second, "night-2")
        actions = session.handle(ClockEvent(130, wall_time="2026-08-25T02:29:55"))
        self.assertEqual([action.kind for action in actions], ["cancel_order", "cancel_order"])
        session.handle(OrderEvent("night-2-buy", "rb2601", "SHFE", "BUY", "CANCELLED", 1))
        closing = session.handle(OrderEvent("night-2-sell", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = next(action for action in closing if action.kind == "query_position").payload["request_id"]
        session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 0))
        self.assertEqual(session.state, SessionState.FINISHED)

    def test_start_inside_current_window_keeps_safety_gates_and_can_quote(self) -> None:
        session = start_session(
            make_config(quote_windows=[{"start": "09:00", "end": "15:00"}])
        )
        self.assertEqual(
            session.handle(ClockEvent(1, wall_time="2026-08-24T10:00:00")),
            [],
        )
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertIsNone(session.failure_reason)
        self.assertEqual(
            [action.kind for action in qualify_market(session)],
            ["submit_order", "submit_order"],
        )

    def test_start_inside_cross_midnight_window_anchors_to_previous_day(self) -> None:
        from datetime import datetime

        session = start_session(
            make_config(
                quote_windows=[
                    {"start": "21:00", "end": "23:00"},
                    {"start": "23:30", "end": "02:30"},
                ]
            )
        )
        session.handle(ClockEvent(1, wall_time="2026-08-25T01:00:00"))
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertEqual(
            session._schedule_windows,
            (
                (datetime(2026, 8, 24, 21, 0), datetime(2026, 8, 24, 23, 0)),
                (datetime(2026, 8, 24, 23, 30), datetime(2026, 8, 25, 2, 30)),
            ),
        )
        self.assertEqual(
            [action.kind for action in qualify_market(session)],
            ["submit_order", "submit_order"],
        )

    def test_late_fill_during_stable_wait_after_round_triggers_new_closing(self) -> None:
        session = start_session(make_config(max_round_trips=5))
        submitted = qualify_market(session)
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
        submitted = qualify_market(session)
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
        submitted = qualify_market(session)
        bind_quotes(session, submitted, "r1")
        session.handle(TradeEvent("r1-buy", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        session.handle(InterruptEvent())
        complete_interrupted_round(session, "r1", "BUY", 1)
        self.assertEqual(session.state, SessionState.FINISHED)
        self.assertEqual(session.summary()["stop_reason"], "interrupted")

    def test_late_flatten_fill_that_changes_net_triggers_new_closing(self) -> None:
        session = start_session(make_config(max_round_trips=5))
        submitted = qualify_market(session)
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


def open_flatten_and_wait_opposite_cancel(session: LiveGridSession, tag: str) -> str:
    """首成交→窗口超时→FAK 平仓并收到成交终态；返回对侧未终态的 client_id。"""
    buy_id, sell_id = f"{tag}-buy", f"{tag}-sell"
    submitted = qualify_market(session)
    bind_quotes(session, submitted, tag)
    session.handle(TradeEvent(buy_id, "rb2601", "SHFE", "BUY", 1, 60, f"{tag}-trade"))
    open_window(session, buy_id, "BUY", traded=1)
    actions = expire_window(session)
    client_id = actions[0].payload["client_id"]
    session.handle(OrderEvent(f"{tag}-flat", "rb2601", "SHFE", "SELL", "ALLTRADED", 1, traded=1, client_id=client_id))
    session.handle(TradeEvent(f"{tag}-flat", "rb2601", "SHFE", "SELL", 1, 99, f"{tag}-flat-trade", client_id=client_id))
    return sell_id


class LiveGridSessionRecoveryTests(unittest.TestCase):
    def test_interrupt_during_quote_pair_failure_stops_instead_of_resuming(self) -> None:
        """报价对失败收口期间操作员中断：收口完成后必须终止，不得恢复报价。"""
        session = start_session(make_config(max_round_trips=5))
        submitted = qualify_market(session)
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")

        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "REJECTED", 1, client_id=buy.payload["client_id"]))
        self.assertEqual(session.state, SessionState.CLOSING_CANCELS)
        session.handle(InterruptEvent())
        self.assertEqual(session.stop_reason, "interrupted")

        closing = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1, client_id=sell.payload["client_id"]))
        query = next(action for action in closing if action.kind == "query_position")
        session.handle(PositionQueryCompleteEvent(query.payload["request_id"], "rb2601", "SHFE", 0))

        self.assertEqual(session.state, SessionState.FINISHED)
        self.assertEqual(session.stop_reason, "interrupted")
        self.assertEqual(session.handle(ClockEvent(session._now + 5)), [])

    def test_quote_pair_failure_without_interrupt_resumes_quoting(self) -> None:
        """无停止意图时，报价对失败收口后仍回到稳定行情门槛继续联调。"""
        session = start_session(make_config(max_round_trips=5))
        submitted = qualify_market(session)
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")

        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "REJECTED", 1, client_id=buy.payload["client_id"]))
        closing = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1, client_id=sell.payload["client_id"]))
        query = next(action for action in closing if action.kind == "query_position")
        session.handle(PositionQueryCompleteEvent(query.payload["request_id"], "rb2601", "SHFE", 0))

        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        self.assertIsNone(session.stop_reason)

    def test_risk_hold_recovery_respects_stop_reason_set_before_hold(self) -> None:
        """进入托管前操作员已中断：恢复确认零仓后按停止意图收尾，不续挂。"""
        session = start_session(make_config(max_round_trips=5))
        submitted = qualify_market(session)
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        self.assertEqual(session.state, SessionState.QUOTING)

        session.handle(InterruptEvent())
        self.assertEqual(session.stop_reason, "interrupted")
        session.handle(ConnectionEvent("td", False, "disconnect"))
        self.assertEqual(session.state, SessionState.RISK_HOLD)
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))

        recovery = session.handle(ClockEvent(session._now + 2))
        query_action = next(action for action in recovery if action.kind == "query_position")
        session.handle(PositionQueryCompleteEvent(query_action.payload["request_id"], "rb2601", "SHFE", 0))

        self.assertEqual(session.state, SessionState.FINISHED)
        self.assertEqual(session.stop_reason, "interrupted")

    def test_interrupt_during_risk_hold_records_stop_and_finishes_after_zero_reconcile(self) -> None:
        """风险托管中第一次中断也必须留下停止意图，恢复零仓后不得续挂。"""
        session = start_session(make_config(max_round_trips=5))
        session.handle(ConnectionEvent("market", False, "disconnect"))
        self.assertEqual(session.state, SessionState.RISK_HOLD)

        actions = session.handle(InterruptEvent())
        self.assertEqual(session.stop_reason, "interrupted")
        self.assertEqual([action.kind for action in actions], ["audit_warning"])
        trace = session.audit_events[-1]["trace"][0]
        self.assertEqual(trace["code"], "interrupt_risk_hold")
        self.assertEqual(trace["calculation"]["reason"], "interrupted")

        recovery = session.handle(ClockEvent(session._now + 2))
        query_action = next(action for action in recovery if action.kind == "query_position")
        session.handle(PositionQueryCompleteEvent(query_action.payload["request_id"], "rb2601", "SHFE", 0))

        self.assertEqual(session.state, SessionState.FINISHED)
        self.assertEqual(session.summary()["stop_reason"], "interrupted")
        self.assertEqual(session.summary()["final_net_position"], 0)
        self.assertEqual(session.handle(ClockEvent(session._now + 5)), [])

    def test_ctp_error_26_confirms_terminal_order_without_risk_hold(self) -> None:
        """撤单错误26=委托已全成交或已撤销：是终态证明，不再升级 RISK_HOLD。"""
        session = start_session(make_config(max_round_trips=1))
        sell_id = open_flatten_and_wait_opposite_cancel(session, "r1")

        # 撤对侧时交易所回报错误26：本地仍认为活跃，但该委托在交易所侧已是终态
        session.handle(
            OrderActionErrorEvent(
                order_id=sell_id,
                symbol="rb2601",
                exchange="SHFE",
                client_id=sell_id,
                error_id=26,
                error_msg="CTP:报单已全成交或已撤销，不能再撤",
                action="cancel",
            )
        )
        self.assertEqual(session.state, SessionState.CLOSING_CANCELS)

        # 迟到的真实终端回报到达后，收口链路照常走完并计满轮次
        closing = session.handle(OrderEvent(sell_id, "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = next(action for action in closing if action.kind == "query_position").payload["request_id"]
        session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 0))
        self.assertEqual(session.state, SessionState.FINISHED)
        summary = session.summary()
        self.assertEqual(summary["round_trips"], 1)
        self.assertEqual(summary["stop_reason"], "max_round_trips")
        self.assertIsNone(summary["failure_reason"])

    def test_non_terminal_cancel_error_still_enters_risk_hold(self) -> None:
        """除错误26外的撤单失败仍按风险托管处理，保持原有保守边界。"""
        session = start_session(make_config(max_round_trips=1))
        sell_id = open_flatten_and_wait_opposite_cancel(session, "r1")

        session.handle(
            OrderActionErrorEvent(
                order_id=sell_id,
                symbol="rb2601",
                exchange="SHFE",
                client_id=sell_id,
                error_id=-1,
                error_msg="CTP:撤销失败",
                action="cancel",
            )
        )
        self.assertEqual(session.state, SessionState.RISK_HOLD)
        self.assertEqual(session.summary()["failure_reason"], "cancel_failed")

    def test_risk_hold_recovery_counts_completed_fill_and_finishes_at_max_round_trips(self) -> None:
        """成交已入账后断线托管：恢复确认零仓时必须补记本轮并按轮数上限收尾。"""
        session = start_session(make_config(max_round_trips=1))
        sell_id = open_flatten_and_wait_opposite_cancel(session, "r1")

        # 对侧撤单终态未到期间交易前置断线 → 风险托管；断线期间迟到终端回报到达
        session.handle(ConnectionEvent("trade", False))
        self.assertEqual(session.state, SessionState.RISK_HOLD)
        session.handle(OrderEvent(sell_id, "rb2601", "SHFE", "SELL", "CANCELLED", 1))

        # 重连后恢复查仓；净仓为零的确认要补记账，而不是漏记后再多跑一轮
        session.handle(ConnectionEvent("trade", True))
        recovery = session.handle(ClockEvent(session._now + 2))
        query_action = next(action for action in recovery if action.kind == "query_position")
        session.handle(PositionQueryCompleteEvent(query_action.payload["request_id"], "rb2601", "SHFE", 0))

        self.assertEqual(session.state, SessionState.FINISHED)
        summary = session.summary()
        self.assertEqual(summary["round_trips"], 1)
        self.assertEqual(summary["stop_reason"], "max_round_trips")
        self.assertIsNone(summary["failure_reason"])
        self.assertFalse(summary["risk_hold"])

    def test_recovery_without_fills_keeps_zero_round_count(self) -> None:
        """无成交的托管恢复不虚构轮次，仅在确认零仓后回到稳定行情等待。"""
        session = start_session(make_config())
        session.handle(ConnectionEvent("market", False))
        self.assertEqual(session.state, SessionState.RISK_HOLD)

        session.handle(ConnectionEvent("market", True))
        recovery = session.handle(ClockEvent(session._now + 2))
        query_action = next(action for action in recovery if action.kind == "query_position")
        session.handle(PositionQueryCompleteEvent(query_action.payload["request_id"], "rb2601", "SHFE", 0))

        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)
        summary = session.summary()
        self.assertEqual(summary["round_trips"], 0)
        self.assertIsNone(summary["stop_reason"])


if __name__ == "__main__":
    unittest.main()
