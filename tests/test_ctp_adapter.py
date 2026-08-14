import importlib.util
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from live_grid.ctp_adapter import CtpLiveGridAdapter, verify_project_gateway
from live_grid.config import StrategyConfig
from live_grid.session import ClockEvent, LiveGridSession, PositionQueryCompleteEvent, TradeEvent
from vnpy.trader.constant import Exchange
from vnpy_ctp.api import THOST_FTDC_PD_Long
from vnpy_ctp.gateway.ctp_gateway import CtpTdApi, symbol_contract_map
from vnpy_ctp.gateway.position_query import EVENT_POSITION_QUERY_COMPLETE, PositionQueryComplete


def config(symbol: str = "rb2601", exchange: str = "SHFE", **overrides) -> StrategyConfig:
    doc: dict = {"version": 1, "symbol": symbol, "exchange": exchange, "target_lots": 1}
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


def contract_event(symbol: str, exchange: str, pricetick: float = 1.0) -> SimpleNamespace:
    return SimpleNamespace(
        data=SimpleNamespace(
            symbol=symbol,
            exchange=SimpleNamespace(value=exchange),
            pricetick=pricetick,
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

    def record(self, event, actions, state, at, state_before=None) -> None:
        self.events.append((event, actions, state, at, state_before))

    def close(self) -> None:
        self.closed = True


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

        now = time.monotonic()
        for session in sessions:
            for action in session.handle(ClockEvent(now + 2.1)):
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
        self.assertEqual(rb.state.value, "CLOSING_CANCELS")
        self.assertEqual(ap.state.value, "QUOTING")
        self.assertEqual(
            sorted(request.symbol for request, _ in engine.cancelled),
            ["rb2601", "rb2601"],
        )

    def test_position_query_fan_out_rejects_whole_run_when_any_target_nonzero(self) -> None:
        adapter, sessions, audits = make_adapter(("rb2601", "SHFE"), ("AP610", "CZCE"), ("hc2601", "SHFE"))
        engine = adapter.main_engine
        rb, ap, hc = sessions

        adapter._on_contract(contract_event("rb2601", "SHFE"))
        adapter._on_contract(contract_event("AP610", "CZCE"))
        adapter._on_position_query_complete(
            position_result(41, (held_position("AP610", "CZCE", "多", 1),))
        )

        self.assertEqual(rb.state.value, "FINISHED")
        self.assertEqual(ap.state.value, "FAILED")
        self.assertEqual(ap.failure_reason, "nonzero_startup_position")
        self.assertEqual(hc.state.value, "FAILED")
        self.assertEqual(hc.failure_reason, "interrupted_before_zero_position")
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
        self.assertEqual(rb.state.value, "QUOTING")
        self.assertEqual(ap.state.value, "QUOTING")
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

        adapter._on_contract(contract_event("rb2601", "SHFE"))
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
        self.assertEqual(len(engine.cancelled), 2)

    def test_position_query_send_refusal_retries_on_timer_until_sent(self) -> None:
        class FlowControlledGateway:
            def __init__(self) -> None:
                self.query_count = 0

            def query_position(self) -> int | None:
                self.query_count += 1
                return None if self.query_count < 30 else 77

        adapter, sessions, _ = make_adapter(("rb2601", "SHFE"))
        session = sessions[0]
        adapter.main_engine.gateway = FlowControlledGateway()

        adapter._on_contract(contract_event("rb2601", "SHFE"))
        self.assertEqual(adapter.main_engine.gateway.query_count, 1)
        self.assertEqual(session.state.value, "WAITING_FOR_ZERO_POSITION")

        for _ in range(28):
            adapter._on_timer(SimpleNamespace(data=None))
        self.assertEqual(adapter.main_engine.gateway.query_count, 29)
        self.assertEqual(session.state.value, "WAITING_FOR_ZERO_POSITION")

        adapter._on_timer(SimpleNamespace(data=None))
        self.assertEqual(adapter.main_engine.gateway.query_count, 30)
        adapter._on_position_query_complete(position_result(77))
        self.assertEqual(session.state.value, "WAITING_FOR_STABLE_QUOTE")

    def test_position_query_send_refusal_fails_after_exhausted_retries(self) -> None:
        from live_grid.ctp_adapter import POSITION_QUERY_MAX_ATTEMPTS

        class DeadGateway:
            def __init__(self) -> None:
                self.query_count = 0

            def query_position(self) -> None:
                self.query_count += 1
                return None

        adapter, sessions, _ = make_adapter(("rb2601", "SHFE"))
        session = sessions[0]
        adapter.main_engine.gateway = DeadGateway()

        adapter._on_contract(contract_event("rb2601", "SHFE"))
        self.assertEqual(session.state.value, "WAITING_FOR_ZERO_POSITION")
        for _ in range(POSITION_QUERY_MAX_ATTEMPTS - 2):
            adapter._on_timer(SimpleNamespace(data=None))
        self.assertEqual(session.state.value, "WAITING_FOR_ZERO_POSITION")
        adapter._on_timer(SimpleNamespace(data=None))
        self.assertEqual(session.state.value, "FAILED")
        self.assertEqual(adapter.main_engine.gateway.query_count, POSITION_QUERY_MAX_ATTEMPTS)

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
        self.assertTrue(all(audit.closed for audit in audits))

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
