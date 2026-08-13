import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace

from live_grid.ctp_adapter import verify_project_gateway
from vnpy.trader.constant import Exchange
from vnpy_ctp.api import THOST_FTDC_PD_Long
from vnpy_ctp.gateway.ctp_gateway import CtpTdApi, symbol_contract_map
from vnpy_ctp.gateway.position_query import EVENT_POSITION_QUERY_COMPLETE, PositionQueryComplete


class CtpAdapterTests(unittest.TestCase):
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
