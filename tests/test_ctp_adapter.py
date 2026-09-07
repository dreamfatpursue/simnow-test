import importlib.util
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from live_grid.ctp_adapter import CtpLiveGridAdapter, verify_project_gateway
from live_grid.config import StrategyConfig
from live_grid.session import (
    ClockEvent,
    ContractEvent,
    LiveGridSession,
    OrderEvent,
    PositionQueryCompleteEvent,
    TickEvent,
    TradeEvent,
)
from vnpy.trader.constant import Exchange
from vnpy_ctp.api import THOST_FTDC_PD_Long
from vnpy_ctp.gateway.ctp_gateway import CtpTdApi, symbol_contract_map
from vnpy_ctp.gateway.position_query import EVENT_POSITION_QUERY_COMPLETE, PositionQueryComplete


def config(symbol: str = "rb2601", exchange: str = "SHFE", **overrides) -> StrategyConfig:
    doc: dict = {
        "version": 1,
        "symbol": symbol,
        "exchange": exchange,
        "target_lots": 1,
        "max_tick_age_seconds": 60,
        "quote_windows": [{"start": "00:00", "end": "23:59"}],
    }
    doc.update(overrides)
    return StrategyConfig.from_mapping(doc)


def make_adapter(*specs: tuple[str, str], stable_market_seconds: float = 2.0):
    sessions = [
        LiveGridSession(
            config(symbol, exchange, stable_market_seconds=stable_market_seconds),
            simnow_confirmed=True,
        )
        for symbol, exchange in specs
    ]
    audits = [RecordingAudit() for _ in sessions]
    adapter = CtpLiveGridAdapter(sessions, {}, audits)
    adapter.main_engine = FakeMainEngine()
    return adapter, sessions, audits


def contract_event(symbol: str, exchange: str, pricetick: float = 1.0, size: float | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        data=SimpleNamespace(
            symbol=symbol,
            exchange=SimpleNamespace(value=exchange),
            pricetick=pricetick,
            size=size,
        )
    )


def tick_event(symbol: str, exchange: str, last: float = 100.0, bid: float = 99.0, ask: float = 101.0) -> SimpleNamespace:
    return SimpleNamespace(
        data=SimpleNamespace(
            symbol=symbol,
            exchange=SimpleNamespace(value=exchange),
            last_price=last,
            bid_price_1=bid,
            ask_price_1=ask,
            datetime=datetime.now(timezone.utc),
            trading_day="20260824",
            action_day="20260824",
            update_millisec=0,
            limit_up=200.0,
            limit_down=0.1,
        )
    )


def position_result(request_id: int, positions: tuple = ()) -> SimpleNamespace:
    return SimpleNamespace(data=SimpleNamespace(request_id=request_id, positions=positions, error_id=0, error_msg=""))


def held_position(symbol: str, exchange: str, direction: str, volume: int) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        exchange=SimpleNamespace(value=exchange),
        direction=SimpleNamespace(value=direction),
        volume=volume,
    )


class RecordingAudit:
    def __init__(self) -> None:
        self.events = []
        self.closed = False

    def record(self, event, actions, state, at, state_before=None, trace=None) -> None:
        self.events.append((event, actions, state, at, state_before, trace))

    def close(self) -> None:
        self.closed = True


class RecordingRunAudit:
    def __init__(self) -> None:
        self.snapshots = []

    def record_account(self, *, balance, available, at) -> None:
        self.snapshots.append({"balance": balance, "available": available, "at": at})


class FakeGateway:
    def __init__(self) -> None:
        self.query_count = 0

    def query_position(self) -> int:
        self.query_count += 1
        return 41


class FakeMainEngine:
    def __init__(self) -> None:
        self.gateway = FakeGateway()
        self.subscriptions = []
        self.sent = []
        self.cancelled = []

    def get_gateway(self, name):
        return self.gateway

    def subscribe(self, request, name) -> None:
        self.subscriptions.append((request, name))

    def send_order(self, request, name) -> str:
        self.sent.append((request, name))
        return f"CTP.{len(self.sent)}"

    def cancel_order(self, request, name) -> None:
        self.cancelled.append((request, name))


class CtpAdapterTests(unittest.TestCase):
    def test_connect_setting_asks_gateway_for_target_instruments_only(self) -> None:
        adapter, _, _ = make_adapter(("AP701", "CZCE"), ("IF2610", "CFFEX"))
        adapter.gateway_setting = {"用户名": "demo"}

        setting = adapter.connect_setting()

        self.assertEqual(setting["用户名"], "demo")
        self.assertEqual(setting["查询合约"], ["AP701.CZCE", "IF2610.CFFEX"])

    def test_adapter_routes_two_contract_sessions_independently(self) -> None:
        adapter, sessions, audits = make_adapter(("rb2601", "SHFE"), ("AP610", "CZCE"))
        engine = adapter.main_engine
        rb, ap = sessions

        adapter._on_contract(contract_event("rb2601", "SHFE"))
        adapter._on_contract(contract_event("AP610", "CZCE"))
        adapter._on_contract(contract_event("hc2601", "SHFE"))
        self.assertEqual(rb.state.value, "WAITING_FOR_ZERO_POSITION")
        self.assertEqual(ap.state.value, "WAITING_FOR_ZERO_POSITION")
        self.assertEqual(
            sorted(request.symbol for request, _ in engine.subscriptions),
            ["AP610", "rb2601"],
        )
        self.assertEqual(engine.gateway.query_count, 1)

        adapter._on_position_query_complete(position_result(41))
        self.assertEqual(rb.state.value, "WAITING_FOR_STABLE_QUOTE")
        self.assertEqual(ap.state.value, "WAITING_FOR_STABLE_QUOTE")
        rb_events = [entry[0] for entry in audits[0].events]
        self.assertEqual(
            [(type(event).__name__, getattr(event, "symbol", None)) for event in rb_events],
            [("ContractEvent", "rb2601"), ("PositionQueryCompleteEvent", "rb2601")],
        )

        adapter._on_tick(tick_event("rb2601", "SHFE"))
        adapter._on_tick(tick_event("AP610", "CZCE"))
        self.assertEqual(audits[0].events[-1][0].last_price, 100)
        self.assertEqual(audits[1].events[-1][0].symbol, "AP610")

        t0 = time.monotonic()
        for session, symbol, exchange in zip(sessions, ("rb2601", "AP610"), ("SHFE", "CZCE"), strict=True):
            session.handle(TickEvent(symbol, exchange, 100, 99, 101, t0))
            session.handle(TickEvent(symbol, exchange, 100, 99, 101, t0 + 0.5))
            for action in session.handle(ClockEvent(t0 + 2.0)):
                adapter._dispatch(action)
        self.assertEqual(len(engine.sent), 4)
        self.assertEqual(
            sorted(request.symbol for request, _ in engine.sent),
            ["AP610", "AP610", "rb2601", "rb2601"],
        )

        for index, (request, _) in enumerate(engine.sent, 1):
            adapter._on_order(
                SimpleNamespace(
                    data=SimpleNamespace(
                        orderid=str(index),
                        reference=None,
                        symbol=request.symbol,
                        exchange=SimpleNamespace(value="SHFE" if request.symbol == "rb2601" else "CZCE"),
                        direction=SimpleNamespace(value="多" if request.direction.value == "多" else "空"),
                        status=SimpleNamespace(value="未成交"),
                        volume=1,
                        traded=0,
                        price=request.price,
                    )
                )
            )
        self.assertEqual(len(rb.orders), 2)
        self.assertEqual(len(ap.orders), 2)

        adapter._on_trade(
            SimpleNamespace(
                data=SimpleNamespace(
                    orderid="1",
                    reference=None,
                    symbol="rb2601",
                    exchange=SimpleNamespace(value="SHFE"),
                    direction=SimpleNamespace(value="多"),
                    volume=1,
                    price=60,
                    tradeid="trade-1",
                )
            )
        )
        self.assertEqual(rb.state.value, "CLOSING_WAIT")
        self.assertEqual(ap.state.value, "QUOTING")
        self.assertEqual(engine.cancelled, [])

    def test_position_query_fan_out_rejects_whole_run_when_any_target_nonzero(self) -> None:
        adapter, sessions, audits = make_adapter(("rb2601", "SHFE"), ("AP610", "CZCE"), ("hc2601", "SHFE"))
        engine = adapter.main_engine
        rb, ap, hc = sessions

        adapter._on_contract(contract_event("rb2601", "SHFE"))
        adapter._on_contract(contract_event("AP610", "CZCE"))
        adapter._on_position_query_complete(
            position_result(41, (held_position("AP610", "CZCE", "多", 1),))
        )

        self.assertEqual(rb.state.value, "WAITING_FOR_STABLE_QUOTE")
        self.assertEqual(ap.state.value, "RISK_HOLD")
        self.assertEqual(ap.failure_reason, "nonzero_startup_position")
        self.assertEqual(hc.state.value, "WAITING_FOR_CONTRACT")
        self.assertIsNone(hc.failure_reason)
        self.assertEqual(engine.sent, [])
        self.assertEqual(engine.cancelled, [])

    def test_sessions_cannot_quote_until_every_contract_passed_the_zero_gate(self) -> None:
        adapter, sessions, audits = make_adapter(
            ("rb2601", "SHFE"),
            ("AP610", "CZCE"),
            stable_market_seconds=0.05,
        )
        engine = adapter.main_engine
        rb, ap = sessions

        adapter._on_contract(contract_event("rb2601", "SHFE"))
        adapter._on_position_query_complete(position_result(41))
        self.assertEqual(rb.state.value, "WAITING_FOR_STABLE_QUOTE")

        for _ in range(12):
            adapter._on_tick(tick_event("rb2601", "SHFE"))
            adapter._on_timer(SimpleNamespace(data=None))
            time.sleep(0.01)
        self.assertEqual(rb.state.value, "WAITING_FOR_STABLE_QUOTE")
        self.assertEqual(engine.sent, [])

        adapter._on_contract(contract_event("AP610", "CZCE"))
        self.assertEqual(engine.gateway.query_count, 2)
        adapter._on_position_query_complete(position_result(41))
        self.assertEqual(ap.state.value, "WAITING_FOR_STABLE_QUOTE")

        for _ in range(12):
            adapter._on_tick(tick_event("rb2601", "SHFE"))
            adapter._on_tick(tick_event("AP610", "CZCE"))
            adapter._on_timer(SimpleNamespace(data=None))
            time.sleep(0.01)
        self.assertEqual(rb.state.value, "QUOTE_PENDING")
        self.assertEqual(ap.state.value, "QUOTE_PENDING")
        self.assertEqual(len(engine.sent), 4)

    def test_two_startup_queries_merge_into_one_account_level_query(self) -> None:
        class OnceRefusingGateway:
            def __init__(self) -> None:
                self.query_count = 0

            def query_position(self) -> int | None:
                self.query_count += 1
                return None if self.query_count == 1 else 51

        adapter, sessions, audits = make_adapter(("rb2601", "SHFE"), ("AP610", "CZCE"))
        engine = adapter.main_engine
        engine.gateway = OnceRefusingGateway()
        rb, ap = sessions
        clock = [1000.0]
        adapter._clock = lambda: clock[0]

        adapter._on_contract(contract_event("rb2601", "SHFE"))
        # 第一次发送被拒后进入退避；跨过间隔再接第二个合约，
        # 该合约的待查登记与 rb 合并进下一次账户级查询。
        clock[0] += 1.01
        adapter._on_contract(contract_event("AP610", "CZCE"))
        self.assertEqual(engine.gateway.query_count, 2)
        self.assertEqual(rb.state.value, "WAITING_FOR_ZERO_POSITION")
        self.assertEqual(ap.state.value, "WAITING_FOR_ZERO_POSITION")

        adapter._on_timer(SimpleNamespace(data=None))
        self.assertEqual(engine.gateway.query_count, 2)

        adapter._on_position_query_complete(position_result(51))
        self.assertEqual(rb.state.value, "WAITING_FOR_STABLE_QUOTE")
        self.assertEqual(ap.state.value, "WAITING_FOR_STABLE_QUOTE")
        self.assertEqual(engine.gateway.query_count, 2)

    def test_adapter_translates_ctp_events_and_actions_at_live_boundary(self) -> None:
        adapter, sessions, audits = make_adapter(("rb2601", "SHFE"))
        session, audit = sessions[0], audits[0]
        engine = adapter.main_engine

        adapter._on_contract(contract_event("rb2601", "SHFE"))
        self.assertEqual(session.state.value, "WAITING_FOR_ZERO_POSITION")
        self.assertEqual(engine.gateway.query_count, 1)
        self.assertEqual(engine.subscriptions[0][0].symbol, "rb2601")

        adapter._on_position_query_complete(position_result(41))
        self.assertEqual(session.state.value, "WAITING_FOR_STABLE_QUOTE")
        self.assertIsInstance(audit.events[-1][0], PositionQueryCompleteEvent)
        self.assertEqual(audit.events[-1][0].request_id, "position-1")

        now = time.monotonic()
        adapter._on_tick(tick_event("rb2601", "SHFE"))
        self.assertEqual(audit.events[-1][0].last_price, 100)
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, now))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, now + 0.5))
        actions = session.handle(ClockEvent(now + 2.1))
        self.assertEqual([action.kind for action in actions], ["submit_order", "submit_order"])
        for action in actions:
            adapter._dispatch(action)
        self.assertEqual(len(engine.sent), 2)
        self.assertTrue(all(request.reference.startswith("quote-") for request, _ in engine.sent))

        for index, (request, _) in enumerate(engine.sent, 1):
            adapter._on_order(
                SimpleNamespace(
                    data=SimpleNamespace(
                        orderid=str(index),
                        reference=None,
                        symbol="rb2601",
                        exchange=SimpleNamespace(value="SHFE"),
                        direction=SimpleNamespace(value="多" if request.direction.value == "多" else "空"),
                        status=SimpleNamespace(value="未成交"),
                        volume=1,
                        traded=0,
                        price=request.price,
                    )
                )
            )

        adapter._on_trade(
            SimpleNamespace(
                data=SimpleNamespace(
                    orderid="1",
                    reference=None,
                    symbol="rb2601",
                    exchange=SimpleNamespace(value="SHFE"),
                    direction=SimpleNamespace(value="多"),
                    volume=1,
                    price=60,
                    tradeid="trade-1",
                )
            )
        )
        self.assertIsInstance(audit.events[-1][0], TradeEvent)
        self.assertEqual(audit.events[-1][0].volume, 1)
        # 价差窗口：首成交不撤对侧，窗口超时后直接 FAK 平仓。
        self.assertEqual(session.state.value, "CLOSING_WAIT")
        self.assertEqual(engine.cancelled, [])
        flatten_actions = session.handle(ClockEvent(now + 3.5))
        for action in flatten_actions:
            adapter._dispatch(action)
        self.assertEqual(session.state.value, "FLATTENING")
        self.assertEqual(len(engine.sent), 3)
        self.assertEqual(engine.sent[-1][0].type.value, "FAK")

    def test_quote_ack_timeout_dispatches_qry_order_before_cancel(self) -> None:
        class QueryGateway(FakeGateway):
            def __init__(self) -> None:
                super().__init__()
                self.order_query_count = 0

            def query_order(self) -> int:
                self.order_query_count += 1
                return 55

        class QueryEngine(FakeMainEngine):
            def __init__(self) -> None:
                super().__init__()
                self.gateway = QueryGateway()

        adapter, sessions, _ = make_adapter(("rb2601", "SHFE"))
        session = sessions[0]
        adapter.main_engine = QueryEngine()
        session.handle(ContractEvent("rb2601", "SHFE", 1.0))
        session.handle(PositionQueryCompleteEvent("position-1", "rb2601", "SHFE", 0))

        now = time.monotonic()
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, now))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, now + 0.5))
        submitted = session.handle(ClockEvent(now + 2.1))
        for action in submitted:
            adapter._dispatch(action)

        timeout_actions = session.handle(ClockEvent(session._now + 5))
        self.assertEqual([action.kind for action in timeout_actions], ["query_order"])
        adapter._dispatch(timeout_actions[0])
        self.assertEqual(adapter.main_engine.gateway.order_query_count, 1)
        self.assertEqual(adapter.main_engine.cancelled, [])

        adapter._on_order_query_complete(
            SimpleNamespace(
                data=SimpleNamespace(
                    request_id=55,
                    error_id=0,
                    error_msg="",
                    orders=(
                        {"order_id": "1", "InstrumentID": "rb2601", "ExchangeID": "SHFE", "OrderStatus": "3", "Direction": "0", "CombOffsetFlag": "0", "VolumeTotalOriginal": 1, "VolumeTraded": 0, "LimitPrice": 80},
                        {"order_id": "2", "InstrumentID": "rb2601", "ExchangeID": "SHFE", "OrderStatus": "3", "Direction": "1", "CombOffsetFlag": "0", "VolumeTotalOriginal": 1, "VolumeTraded": 0, "LimitPrice": 120},
                    ),
                )
            )
        )
        self.assertEqual(session.state.value, "QUOTING")
        self.assertEqual(adapter.main_engine.cancelled, [])

    def test_adapter_passes_exchange_time_and_contract_size_into_events(self) -> None:
        adapter, sessions, audits = make_adapter(("rb2601", "SHFE"))
        session, audit = sessions[0], audits[0]
        adapter.main_engine.gateway.query_count = 0

        adapter._on_contract(contract_event("rb2601", "SHFE", size=5.0))
        adapter._on_position_query_complete(position_result(41))
        now = time.monotonic()
        adapter._on_tick(tick_event("rb2601", "SHFE"))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, now))
        session.handle(TickEvent("rb2601", "SHFE", 100, 99, 101, now + 0.5))
        for action in session.handle(ClockEvent(now + 2.1)):
            adapter._dispatch(action)

        insert_time = datetime(2026, 8, 17, 21, 0, 1, tzinfo=timezone(timedelta(hours=8)))
        trade_time = datetime(2026, 8, 17, 21, 3, 5, tzinfo=timezone(timedelta(hours=8)))
        adapter._on_order(
            SimpleNamespace(
                data=SimpleNamespace(
                    orderid="1",
                    reference=None,
                    symbol="rb2601",
                    exchange=SimpleNamespace(value="SHFE"),
                    direction=SimpleNamespace(value="多"),
                    status=SimpleNamespace(value="未成交"),
                    volume=1,
                    traded=0,
                    price=60,
                    datetime=insert_time,
                )
            )
        )
        adapter._on_trade(
            SimpleNamespace(
                data=SimpleNamespace(
                    orderid="1",
                    reference=None,
                    symbol="rb2601",
                    exchange=SimpleNamespace(value="SHFE"),
                    direction=SimpleNamespace(value="多"),
                    volume=1,
                    price=60,
                    tradeid="trade-1",
                    datetime=trade_time,
                )
            )
        )

        contract = next(
            event for event, *_ in audit.events if isinstance(event, ContractEvent)
        )
        order = next(
            event for event, *_ in reversed(audit.events) if isinstance(event, OrderEvent)
        )
        trade = next(
            event for event, *_ in reversed(audit.events) if isinstance(event, TradeEvent)
        )
        self.assertEqual(contract.size, 5.0)
        self.assertEqual(order.exchange_time, "2026-08-17T21:00:01+08:00")
        self.assertEqual(trade.exchange_time, "2026-08-17T21:03:05+08:00")

    def test_account_events_become_run_level_snapshots_without_account_id(self) -> None:
        session = LiveGridSession(config("rb2601", "SHFE"), simnow_confirmed=True)
        run_audit = RecordingRunAudit()
        adapter = CtpLiveGridAdapter([session], {}, [RecordingAudit()], run_audit=run_audit)
        started = time.monotonic()

        adapter._on_account(
            SimpleNamespace(
                data=SimpleNamespace(accountid="SimNow8888", balance=1_000_000.0, available=900_000.0)
            )
        )
        adapter._on_account(
            SimpleNamespace(
                data=SimpleNamespace(accountid="SimNow8888", balance=1_000_050.0, available=899_900.0)
            )
        )

        self.assertEqual(len(run_audit.snapshots), 2)
        self.assertEqual(run_audit.snapshots[0]["balance"], 1_000_000.0)
        self.assertEqual(run_audit.snapshots[0]["available"], 900_000.0)
        for snapshot in run_audit.snapshots:
            self.assertGreaterEqual(snapshot["at"], started)
            self.assertEqual(set(snapshot), {"balance", "available", "at"})

    def test_account_events_without_run_audit_are_ignored(self) -> None:
        session = LiveGridSession(config("rb2601", "SHFE"), simnow_confirmed=True)
        adapter = CtpLiveGridAdapter([session], {}, [RecordingAudit()])
        adapter._on_account(
            SimpleNamespace(
                data=SimpleNamespace(accountid="SimNow8888", balance=1_000_000.0, available=900_000.0)
            )
        )
        self.assertEqual(adapter.sessions[0].state.value, "WAITING_FOR_CONTRACT")

    def test_position_query_send_refusal_retries_with_backoff_until_sent(self) -> None:
        class FlowControlledGateway:
            def __init__(self) -> None:
                self.query_count = 0

            def query_position(self) -> int | None:
                self.query_count += 1
                return None if self.query_count < 30 else 77

        adapter, sessions, _ = make_adapter(("rb2601", "SHFE"))
        session = sessions[0]
        clock = [1000.0]
        adapter._clock = lambda: clock[0]
        adapter.main_engine.gateway = FlowControlledGateway()

        adapter._on_contract(contract_event("rb2601", "SHFE"))
        self.assertEqual(adapter.main_engine.gateway.query_count, 1)
        self.assertEqual(session.state.value, "WAITING_FOR_ZERO_POSITION")

        # 首次被拒后：同刻与未满退避间隔的重发都被抑制（1s→2s→4s→固定5s）
        adapter._on_timer(SimpleNamespace(data=None))
        self.assertEqual(adapter.main_engine.gateway.query_count, 1)
        clock[0] += 1.01
        adapter._on_timer(SimpleNamespace(data=None))
        self.assertEqual(adapter.main_engine.gateway.query_count, 2)
        clock[0] += 1.5
        adapter._on_timer(SimpleNamespace(data=None))
        self.assertEqual(adapter.main_engine.gateway.query_count, 2)
        clock[0] += 0.6
        adapter._on_timer(SimpleNamespace(data=None))
        self.assertEqual(adapter.main_engine.gateway.query_count, 3)
        clock[0] += 4.01
        adapter._on_timer(SimpleNamespace(data=None))
        self.assertEqual(adapter.main_engine.gateway.query_count, 4)

        while adapter.main_engine.gateway.query_count < 29:
            clock[0] += 5.01
            adapter._on_timer(SimpleNamespace(data=None))
        clock[0] += 5.01
        adapter._on_timer(SimpleNamespace(data=None))
        self.assertEqual(adapter.main_engine.gateway.query_count, 30)
        adapter._on_position_query_complete(position_result(77))
        self.assertEqual(session.state.value, "WAITING_FOR_STABLE_QUOTE")

    def test_position_query_send_refusal_fails_after_exhausted_retries(self) -> None:
        from live_grid.ctp_adapter import POSITION_QUERY_MAX_ATTEMPTS

        class DeadGateway:
            def __init__(self) -> None:
                self.query_count = 0
                self.last_query_send_refusal = None

            def query_position(self) -> None:
                self.query_count += 1
                self.last_query_send_refusal = "ReqQryInvestorPosition 返回 -3"
                return None

        adapter, sessions, audits = make_adapter(("rb2601", "SHFE"))
        session = sessions[0]
        clock = [1000.0]
        adapter._clock = lambda: clock[0]
        adapter.main_engine.gateway = DeadGateway()

        adapter._on_contract(contract_event("rb2601", "SHFE"))
        self.assertEqual(session.state.value, "WAITING_FOR_ZERO_POSITION")
        for _ in range(POSITION_QUERY_MAX_ATTEMPTS - 2):
            clock[0] += 5.01
            adapter._on_timer(SimpleNamespace(data=None))
        self.assertEqual(session.state.value, "WAITING_FOR_ZERO_POSITION")
        clock[0] += 5.01
        adapter._on_timer(SimpleNamespace(data=None))
        self.assertEqual(session.state.value, "RISK_HOLD")
        self.assertEqual(adapter.main_engine.gateway.query_count, POSITION_QUERY_MAX_ATTEMPTS)
        refusals = [
            event for event, _, _, _, _, _ in audits[0].events
            if isinstance(event, PositionQueryCompleteEvent) and event.error_id == 1
        ]
        self.assertTrue(refusals)
        self.assertIn("ReqQryInvestorPosition 返回 -3", refusals[-1].error_msg)

    def test_send_refused_message_appends_detail_only_when_present(self) -> None:
        self.assertEqual(CtpLiveGridAdapter._send_refused_message("CTP 持仓查询请求未发送", None), "CTP 持仓查询请求未发送")
        detail = CtpLiveGridAdapter._send_refused_message("CTP 持仓查询请求未发送", "ReqQryInvestorPosition 返回 -3")
        self.assertEqual(detail, "CTP 持仓查询请求未发送（ReqQryInvestorPosition 返回 -3）")

    def test_close_does_not_hold_lock_while_engine_closes(self) -> None:
        adapter, _, audits = make_adapter(("rb2601", "SHFE"))

        class LockProbingEngine(FakeMainEngine):
            def __init__(self) -> None:
                super().__init__()
                self.lock_free_during_close = None

            def close(self) -> None:
                acquired = adapter._lock.acquire(timeout=1)
                self.lock_free_during_close = acquired
                if acquired:
                    adapter._lock.release()

        engine = LockProbingEngine()
        adapter.main_engine = engine
        adapter.close()
        self.assertTrue(engine.lock_free_during_close)
        self.assertIsNone(adapter.main_engine)
        self.assertFalse(all(audit.closed for audit in audits))

    def test_project_gateway_path_and_position_completion_contract_are_available(self) -> None:
        package_path = verify_project_gateway(Path(__file__).resolve().parents[1])
        self.assertEqual(package_path.name, "vnpy_ctp")
        self.assertTrue(package_path.joinpath("gateway", "position_query.py").is_file())
        self.assertEqual(EVENT_POSITION_QUERY_COMPLETE, "ePositionQueryComplete")
        result = PositionQueryComplete(request_id=11, positions=())
        self.assertEqual(result.request_id, 11)
        self.assertEqual(result.positions, ())

    def test_native_extensions_resolve_from_project_build_when_present(self) -> None:
        spec = importlib.util.find_spec("vnpy_ctp.api.vnctpmd")
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertIn("/vendor/vnpy_ctp/build/", spec.origin)

    def test_empty_and_nonempty_position_callbacks_publish_correlated_completion(self) -> None:
        class Gateway:
            def __init__(self) -> None:
                self.positions = []
                self.events = []

            def on_position(self, position) -> None:
                self.positions.append(position)

            def on_event(self, event_type, data) -> None:
                self.events.append((event_type, data))

        gateway = Gateway()
        api = CtpTdApi.__new__(CtpTdApi)
        api.gateway = gateway
        api.gateway_name = "CTP"
        api.position_queries = {}
        api.positions = {}
        api.onRspQryInvestorPosition({}, {"ErrorID": 0, "ErrorMsg": ""}, 11, True)
        self.assertEqual(gateway.events[-1][0], EVENT_POSITION_QUERY_COMPLETE)
        self.assertEqual(gateway.events[-1][1].request_id, 11)
        self.assertEqual(gateway.events[-1][1].positions, ())

        symbol_contract_map["rb2601"] = SimpleNamespace(exchange=Exchange.SHFE, size=1)
        api.positions = {}
        api.onRspQryInvestorPosition(
            {
                "InstrumentID": "rb2601",
                "PosiDirection": THOST_FTDC_PD_Long,
                "YdPosition": 0,
                "TodayPosition": 1,
                "Position": 1,
                "PositionCost": 100,
                "PositionProfit": 0,
                "LongFrozen": 0,
                "ShortFrozen": 0,
            },
            {"ErrorID": 0, "ErrorMsg": ""},
            12,
            True,
        )
        self.assertEqual(gateway.events[-1][1].request_id, 12)
        self.assertEqual(gateway.events[-1][1].positions[0].volume, 1)
        symbol_contract_map.clear()

    def test_interleaved_position_queries_keep_results_isolated(self) -> None:
        class Gateway:
            def __init__(self) -> None:
                self.events = []

            def on_position(self, position) -> None:
                pass

            def on_event(self, event_type, data) -> None:
                self.events.append(data)

        gateway = Gateway()
        api = CtpTdApi.__new__(CtpTdApi)
        api.gateway = gateway
        api.gateway_name = "CTP"
        api.position_queries = {}
        symbol_contract_map["rb2601"] = SimpleNamespace(exchange=Exchange.SHFE, size=1)
        row = {
            "InstrumentID": "rb2601",
            "PosiDirection": THOST_FTDC_PD_Long,
            "YdPosition": 0,
            "TodayPosition": 1,
            "Position": 1,
            "PositionCost": 100,
            "PositionProfit": 0,
            "LongFrozen": 0,
            "ShortFrozen": 0,
        }
        api.onRspQryInvestorPosition(row, {"ErrorID": 0, "ErrorMsg": ""}, 21, False)
        api.onRspQryInvestorPosition({}, {"ErrorID": 0, "ErrorMsg": ""}, 22, True)
        api.onRspQryInvestorPosition({}, {"ErrorID": 0, "ErrorMsg": ""}, 21, True)
        self.assertEqual(gateway.events[-2].request_id, 22)
        self.assertEqual(gateway.events[-2].positions, ())
        self.assertEqual(gateway.events[-1].request_id, 21)
        self.assertEqual(gateway.events[-1].positions[0].volume, 1)
        symbol_contract_map.clear()


if __name__ == "__main__":
    unittest.main()
