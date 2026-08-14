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


def config() -> StrategyConfig:
    return StrategyConfig.from_mapping(
        {
            "version": 1,
            "symbol": "rb2601",
            "exchange": "SHFE",
            "target_lots": 1,
        }
    )


class RecordingAudit:
    def __init__(self) -> None:
        self.events = []

    def record(self, event, actions, state, at, state_before=None) -> None:
        self.events.append((event, actions, state, at, state_before))

    def close(self) -> None:
        pass


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
    def test_adapter_translates_ctp_events_and_actions_at_live_boundary(self) -> None:
        strategy = config()
        session = LiveGridSession(strategy, simnow_confirmed=True, hash_prefix=strategy.sha256[:8])
        audit = RecordingAudit()
        adapter = CtpLiveGridAdapter(session, {}, audit)
        engine = FakeMainEngine()
        adapter.main_engine = engine

        contract = SimpleNamespace(
            symbol="rb2601",
            exchange=SimpleNamespace(value="SHFE"),
            pricetick=1.0,
        )
        adapter._on_contract(SimpleNamespace(data=contract))
        self.assertEqual(session.state.value, "WAITING_FOR_ZERO_POSITION")
        self.assertEqual(engine.gateway.query_count, 1)
        self.assertEqual(engine.subscriptions[0][0].symbol, "rb2601")

        adapter._on_position_query_complete(
            SimpleNamespace(
                data=SimpleNamespace(request_id=41, positions=(), error_id=0, error_msg="")
            )
        )
        self.assertEqual(session.state.value, "WAITING_FOR_STABLE_QUOTE")
        self.assertIsInstance(audit.events[-1][0], PositionQueryCompleteEvent)
        self.assertEqual(audit.events[-1][0].request_id, "position-1")

        now = time.monotonic()
        tick = SimpleNamespace(
            symbol="rb2601",
            exchange=SimpleNamespace(value="SHFE"),
            last_price=100,
            bid_price_1=99,
            ask_price_1=101,
        )
        adapter._on_tick(SimpleNamespace(data=tick))
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
