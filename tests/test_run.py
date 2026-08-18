import os
import unittest
from unittest.mock import patch

from run import _diagnostic_snapshot, load_settings


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
        }

    def test_selected_environment_uses_its_own_fronts(self) -> None:
        with patch.dict(os.environ, self.environ, clear=True):
            first = load_settings()
            continuous = load_settings("7x24")

        self.assertEqual((first.environment, first.trade_front, first.market_front), ("first", "tcp://first-trade", "tcp://first-market"))
        self.assertEqual((continuous.environment, continuous.trade_front, continuous.market_front), ("7x24", "tcp://7x24-trade", "tcp://7x24-market"))

    def test_7x24_never_falls_back_to_first_fronts(self) -> None:
        self.environ.pop("CTP_7X24_MARKET_FRONT")
        with patch.dict(os.environ, self.environ, clear=True):
            with self.assertRaisesRegex(ValueError, "CTP_7X24_MARKET_FRONT"):
                load_settings("7x24")

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
            "TD(front=Y,auth=N,login=N) MD(front=Y,login=N) contracts=N",
        )


if __name__ == "__main__":
    unittest.main()
