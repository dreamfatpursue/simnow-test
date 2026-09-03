import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from run import Settings, _diagnostic_snapshot, install_handlers, load_settings
from vnpy.trader.event import EVENT_CONTRACT, EVENT_TICK


class LoadSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environ = {
            "CTP_USER_ID": "simnow-user",
            "CTP_PASSWORD": "password",
            "CTP_BROKER_ID": "9999",
            "CTP_TRADE_FRONT": "tcp://first-trade",
            "CTP_MARKET_FRONT": "tcp://first-market",
            "CTP_7X24_TRADE_FRONT": "tcp://7x24-trade",
            "CTP_7X24_MARKET_FRONT": "tcp://7x24-market",
            "CTP_APP_ID": "simnow_client_test",
            "CTP_AUTH_CODE": "0000000000000000",
            "CTP_GUANGFA_USER_ID": "guangfa-user",
            "CTP_GUANGFA_PASSWORD": "guangfa-password",
            "CTP_GUANGFA_BROKER_ID": "2358",
            "CTP_GUANGFA_TRADE_FRONT": "tcp://guangfa-trade",
            "CTP_GUANGFA_MARKET_FRONT": "tcp://guangfa-market",
            "CTP_GUANGFA_APP_ID": "guangfa-app",
            "CTP_GUANGFA_AUTH_CODE": "guangfa-auth",
        }

    def test_selected_environment_uses_its_own_fronts(self) -> None:
        with patch.dict(os.environ, self.environ, clear=True):
            first = load_settings()
            continuous = load_settings("7x24")
            guangfa = load_settings("guangfa")

        self.assertEqual((first.environment, first.trade_front, first.market_front), ("first", "tcp://first-trade", "tcp://first-market"))
        self.assertEqual((continuous.environment, continuous.trade_front, continuous.market_front), ("7x24", "tcp://7x24-trade", "tcp://7x24-market"))
        self.assertEqual(
            (guangfa.environment, guangfa.user_id, guangfa.broker_id, guangfa.trade_front, guangfa.market_front),
            ("guangfa", "guangfa-user", "2358", "tcp://guangfa-trade", "tcp://guangfa-market"),
        )

    def test_7x24_never_falls_back_to_first_fronts(self) -> None:
        self.environ.pop("CTP_7X24_MARKET_FRONT")
        with patch.dict(os.environ, self.environ, clear=True):
            with self.assertRaisesRegex(ValueError, "CTP_7X24_MARKET_FRONT"):
                load_settings("7x24")

    def test_guangfa_never_falls_back_to_simnow_credentials(self) -> None:
        self.environ.pop("CTP_GUANGFA_AUTH_CODE")
        with patch.dict(os.environ, self.environ, clear=True):
            with self.assertRaisesRegex(ValueError, "CTP_GUANGFA_AUTH_CODE"):
                load_settings("guangfa")

    def test_diagnostic_snapshot_exposes_login_stage(self) -> None:
        diagnostics = {
            "td_front_connected": True,
            "td_front_disconnected": False,
            "td_authenticated": False,
            "td_authentication_failed": False,
            "td_logged_in": False,
            "td_login_failed": False,
            "md_front_connected": True,
            "md_front_disconnected": False,
            "md_logged_in": False,
            "md_login_failed": False,
            "contracts_queried": False,
        }
        self.assertEqual(
            _diagnostic_snapshot(diagnostics),
            "TD(front=Y,auth=N,login=N) MD(front=Y,login=N) contracts=N target(contract=N,tick=N)",
        )

    def test_read_only_diagnostics_distinguish_target_contract_and_tick(self) -> None:
        class RecordingEventEngine:
            def __init__(self) -> None:
                self.handlers = {}

            def register(self, event_type, handler) -> None:
                self.handlers[event_type] = handler

        class RecordingMainEngine:
            def __init__(self) -> None:
                self.subscriptions = []

            def subscribe(self, request, gateway_name) -> None:
                self.subscriptions.append((request, gateway_name))

        settings = Settings(
            environment="first",
            user_id="user",
            password="password",
            broker_id="9999",
            trade_front="tcp://trade",
            market_front="tcp://market",
            app_id="app",
            auth_code="auth",
            product_info="",
            symbol="AP610",
            exchange="CZCE",
        )
        event_engine = RecordingEventEngine()
        main_engine = RecordingMainEngine()
        diagnostics = install_handlers(event_engine, main_engine, settings)

        event_engine.handlers[EVENT_CONTRACT](SimpleNamespace(data=SimpleNamespace(
            symbol="AP610",
            exchange=SimpleNamespace(value="CZCE"),
            vt_symbol="AP610.CZCE",
            name="苹果",
            size=10,
            pricetick=1.0,
        )))
        self.assertTrue(diagnostics["target_contract_seen"])
        self.assertEqual(len(main_engine.subscriptions), 1)

        event_engine.handlers[EVENT_TICK](SimpleNamespace(data=SimpleNamespace(
            vt_symbol="AP610.CZCE",
            last_price=7424,
            bid_price_1=7424,
            bid_volume_1=1,
            ask_price_1=7425,
            ask_volume_1=1,
            datetime="2026-08-25T14:39:50+08:00",
        )))
        self.assertTrue(diagnostics["target_tick_seen"])


if __name__ == "__main__":
    unittest.main()
