import unittest
from types import SimpleNamespace

from vnpy.trader.constant import Exchange, Status
from vnpy_ctp.api import (
    THOST_FTDC_D_Buy,
    THOST_FTDC_OF_Open,
    THOST_FTDC_OPT_LimitPrice,
    THOST_FTDC_OST_Unknown,
    THOST_FTDC_TC_GFD,
    THOST_FTDC_VC_AV,
)
from vnpy_ctp.gateway.ctp_gateway import CtpTdApi, symbol_contract_map


class RecordingGateway:
    gateway_name = "CTP"

    def __init__(self) -> None:
        self.errors = []
        self.logs = []
        self.orders = []

    def write_error(self, message, error) -> None:
        self.errors.append((message, error))

    def write_log(self, message) -> None:
        self.logs.append(message)

    def on_order(self, order) -> None:
        self.orders.append(order)


class CtpGatewayCallbackTests(unittest.TestCase):
    def test_trade_api_reports_generic_request_error(self) -> None:
        gateway = RecordingGateway()
        api = CtpTdApi(gateway)

        api.onRspError({"ErrorID": 7, "ErrorMsg": "请求被拒绝"}, 12, True)

        self.assertEqual(gateway.errors, [("交易接口报错", {"ErrorID": 7, "ErrorMsg": "请求被拒绝"})])

    def test_empty_instrument_error_does_not_raise_or_mark_query_complete(self) -> None:
        gateway = RecordingGateway()
        api = CtpTdApi(gateway)

        api.onRspQryInstrument(
            {},
            {"ErrorID": 42, "ErrorMsg": "查询失败"},
            13,
            True,
        )

        self.assertFalse(api.contract_inited)
        self.assertEqual(gateway.errors, [("合约查询失败", {"ErrorID": 42, "ErrorMsg": "查询失败"})])

    def test_empty_successful_instrument_tail_can_complete(self) -> None:
        gateway = RecordingGateway()
        api = CtpTdApi(gateway)

        api.onRspQryInstrument({}, {"ErrorID": 0, "ErrorMsg": ""}, 14, True)

        self.assertTrue(api.contract_inited)
        self.assertIn("合约信息查询成功", gateway.logs)

    def test_ctp_unknown_order_status_is_transient_submitting(self) -> None:
        gateway = RecordingGateway()
        api = CtpTdApi(gateway)
        api.contract_inited = True
        previous_contract = symbol_contract_map.get("AP610")
        symbol_contract_map["AP610"] = SimpleNamespace(exchange=Exchange.CZCE)

        try:
            api.onRtnOrder(
                {
                    "InstrumentID": "AP610",
                    "FrontID": 1,
                    "SessionID": 2,
                    "OrderRef": "7",
                    "OrderStatus": THOST_FTDC_OST_Unknown,
                    "InsertDate": "20260825",
                    "InsertTime": "09:30:00",
                    "OrderPriceType": THOST_FTDC_OPT_LimitPrice,
                    "TimeCondition": THOST_FTDC_TC_GFD,
                    "VolumeCondition": THOST_FTDC_VC_AV,
                    "Direction": THOST_FTDC_D_Buy,
                    "CombOffsetFlag": THOST_FTDC_OF_Open,
                    "LimitPrice": 7533,
                    "VolumeTotalOriginal": 1,
                    "VolumeTraded": 0,
                    "OrderSysID": "sys-7",
                }
            )
        finally:
            if previous_contract is None:
                symbol_contract_map.pop("AP610", None)
            else:
                symbol_contract_map["AP610"] = previous_contract

        self.assertEqual(len(gateway.orders), 1)
        self.assertEqual(gateway.orders[0].status, Status.SUBMITTING)
        self.assertFalse(gateway.orders[0].ctp_status_unknown)


if __name__ == "__main__":
    unittest.main()
