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
    session = LiveGridSession(config, simnow_confirmed=True, hash_prefix=config.sha256[:8])
    actions = session.handle(ContractEvent(config.effective["symbol"], config.effective["exchange"], 1.0))
    assert actions[0].kind == "query_position"
    request_id = actions[0].payload["request_id"]
    session.handle(PositionQueryCompleteEvent(request_id, config.effective["symbol"], config.effective["exchange"], 0))
    return session


class LiveGridSessionTests(unittest.TestCase):
    def test_confirmation_is_required_before_session_can_emit_any_action(self) -> None:
        config = make_config()
        for simnow_confirmed, hash_prefix in ((False, config.sha256[:8]), (True, "deadbeef")):
            session = LiveGridSession(config, simnow_confirmed=simnow_confirmed, hash_prefix=hash_prefix)
            self.assertEqual(session.state, SessionState.PREVIEW)
            self.assertEqual(session.handle(ContractEvent("rb2601", "SHFE", 1.0)), [])

    def test_no_quote_until_contract_position_and_two_second_stable_market(self) -> None:
        config = make_config()
        session = LiveGridSession(config, simnow_confirmed=True, hash_prefix=config.sha256[:8])
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

    def test_first_partial_trade_cancels_remaining_orders_and_reconciles(self) -> None:
        session = start_session(make_config(target_lots=2))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 2, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 2, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        self.assertEqual(session.state, SessionState.CLOSING_CANCELS)
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "CANCELLED", 2, traded=1))
        actions = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 2))
        self.assertEqual(session.state, SessionState.CLOSING_RECONCILE)
        self.assertEqual([action.kind for action in actions], ["query_position"])
        closing_query = actions[0].payload["request_id"]
        flatten_actions = session.handle(PositionQueryCompleteEvent(closing_query, "rb2601", "SHFE", 1))
        self.assertEqual(session.state, SessionState.FLATTENING)
        flatten = flatten_actions[0]
        self.assertEqual(flatten.kind, "submit_order")
        self.assertEqual(flatten.payload["side"], "SELL")
        self.assertEqual(flatten.payload["offset"], "CLOSETODAY")
        self.assertEqual(flatten.payload["order_type"], "FAK")

    def test_wide_book_fails_strict_protection_gate_without_quotes(self) -> None:
        session = start_session(make_config(w_ticks=1, d_ticks=1, book_protection_multiple=2))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        self.assertEqual(session.handle(ClockEvent(2)), [])
        self.assertEqual(session.state, SessionState.WAITING_FOR_STABLE_QUOTE)

    def test_mismatched_position_query_cannot_open_or_flatten(self) -> None:
        config = make_config()
        session = LiveGridSession(config, simnow_confirmed=True, hash_prefix=config.sha256[:8])
        query = session.handle(ContractEvent("rb2601", "SHFE", 1.0))[0]
        self.assertEqual(session.handle(PositionQueryCompleteEvent("stale", "rb2601", "SHFE", 0)), [])
        self.assertEqual(session.state, SessionState.WAITING_FOR_ZERO_POSITION)
        self.assertEqual(session.handle(PositionQueryCompleteEvent(query.payload["request_id"], "rb2601", "SHFE", 1)), [])
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertEqual(session.failure_reason, "nonzero_startup_position")
        self.assertEqual(session.final_net_position, 1)

    def test_interrupt_before_zero_confirmation_does_not_flatten_existing_position(self) -> None:
        config = make_config()
        session = LiveGridSession(config, simnow_confirmed=True, hash_prefix=config.sha256[:8])
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
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1, traded=1))
        actions = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = actions[0].payload["request_id"]
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
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1, traded=1))
        query = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))[0].payload["request_id"]
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
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1, traded=1))
        actions = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = actions[0].payload["request_id"]
        session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 1))
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
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1, traded=1))
        actions = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = actions[0].payload["request_id"]
        flatten = session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 1))[0]
        client_id = flatten.payload["client_id"]
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "REJECTED", 1, client_id=client_id))
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
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1, traded=1))
        actions = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = actions[0].payload["request_id"]
        session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 1))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "late-trade"))
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1, traded=1))
        self.assertEqual(session.final_net_position, 2)

    def test_late_fill_that_offsets_flatten_waits_for_flatten_terminal_callback(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1, traded=1))
        actions = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = actions[0].payload["request_id"]
        flatten = session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 1))[0]
        client_id = flatten.payload["client_id"]
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=client_id))
        session.handle(TradeEvent("sell-1", "rb2601", "SHFE", "SELL", 1, 140, "late-trade"))
        self.assertEqual(session.state, SessionState.FLATTENING)
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1, traded=0, client_id=client_id))
        self.assertEqual(session.state, SessionState.FINISHED)

    def test_late_fill_from_previous_flatten_attempt_is_accounted(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1, traded=1))
        actions = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = actions[0].payload["request_id"]
        flatten = session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 1))[0]
        client_id = flatten.payload["client_id"]
        second = session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1, client_id=client_id))[0]
        self.assertEqual(second.kind, "submit_order")
        second_id = second.payload["client_id"]
        session.handle(TradeEvent("flatten-1", "rb2601", "SHFE", "SELL", 1, 99, "late-flatten", client_id=client_id))
        self.assertEqual(session.final_net_position, 0)
        session.handle(OrderEvent("flatten-2", "rb2601", "SHFE", "SELL", "CANCELLED", 1, client_id=second_id))
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
        session.handle(OrderEvent("buy-1", "rb2601", "DCE", "BUY", "CANCELLED", 1))
        actions = session.handle(OrderEvent("sell-1", "rb2601", "DCE", "SELL", "CANCELLED", 1))
        query = actions[0].payload["request_id"]
        flatten = session.handle(PositionQueryCompleteEvent(query, "rb2601", "DCE", -1))[0]
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
        session.handle(OrderEvent("buy-1", "rb2601", "INE", "BUY", "CANCELLED", 1, traded=1))
        actions = session.handle(OrderEvent("sell-1", "rb2601", "INE", "SELL", "CANCELLED", 1))
        query = actions[0].payload["request_id"]
        flatten = session.handle(PositionQueryCompleteEvent(query, "rb2601", "INE", 1))[0]
        self.assertEqual(flatten.payload["offset"], "CLOSETODAY")

    def test_partial_flatten_retries_only_residual_position(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1, traded=1))
        query = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))[0].payload["request_id"]
        flatten = session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 2))[0]
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
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1, traded=1))
        query = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))[0].payload["request_id"]
        flatten = session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 1))[0]
        client_id = flatten.payload["client_id"]
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=client_id))
        actions = session.handle(ClockEvent(5.1))
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertEqual(session.failure_reason, "flatten_timeout")
        self.assertEqual(session.final_net_position, 1)
        self.assertEqual([action.kind for action in actions], ["cancel_order"])

    def test_flatten_adverse_price_is_capped_at_ten_ticks(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1, traded=1))
        query = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))[0].payload["request_id"]
        flatten = session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 1))[0]
        client_id = flatten.payload["client_id"]
        session.handle(TickEvent("rb2601", "SHFE", 80, 80, 81, 1))
        retry = session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1, client_id=client_id))
        self.assertEqual(retry[0].payload["price"], 89)

    def test_missing_executable_quote_fails_before_flatten_order(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        session.handle(TickEvent("rb2601", "SHFE", 0, 0, 0, 3))
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1, traded=1))
        actions = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = actions[0].payload["request_id"]
        self.assertEqual(session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 1)), [])
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertEqual(session.failure_reason, "no_executable_quote")

    def test_flatten_rejection_is_failed(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1, traded=1))
        actions = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = actions[0].payload["request_id"]
        flatten = session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 1))[0]
        client_id = flatten.payload["client_id"]
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "REJECTED", 1, client_id=client_id))
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertEqual(session.failure_reason, "flatten_rejected")

    def test_flatten_trade_waits_for_terminal_order_callback_before_finishing(self) -> None:
        session = start_session()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, 0))
        submitted = session.handle(ClockEvent(2))
        buy = next(action for action in submitted if action.payload["side"] == "BUY")
        sell = next(action for action in submitted if action.payload["side"] == "SELL")
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "NOTTRADED", 1, client_id=buy.payload["client_id"]))
        session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "NOTTRADED", 1, client_id=sell.payload["client_id"]))
        session.handle(TradeEvent("buy-1", "rb2601", "SHFE", "BUY", 1, 60, "trade-1"))
        session.handle(OrderEvent("buy-1", "rb2601", "SHFE", "BUY", "CANCELLED", 1, traded=1))
        actions = session.handle(OrderEvent("sell-1", "rb2601", "SHFE", "SELL", "CANCELLED", 1))
        query = actions[0].payload["request_id"]
        flatten = session.handle(PositionQueryCompleteEvent(query, "rb2601", "SHFE", 1))[0]
        client_id = flatten.payload["client_id"]
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "PARTTRADED", 1, traded=1, client_id=client_id))
        session.handle(TradeEvent("flatten-1", "rb2601", "SHFE", "SELL", 1, 99, "flatten-trade", client_id=client_id))
        self.assertEqual(session.state, SessionState.FLATTENING)
        session.handle(OrderEvent("flatten-1", "rb2601", "SHFE", "SELL", "ALLTRADED", 1, traded=1, client_id=client_id))
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


if __name__ == "__main__":
    unittest.main()
