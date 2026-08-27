import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import run_live_grid
from live_grid.config import MultiContractConfig
from live_grid.ctp_adapter import CtpLiveGridAdapter


def multi_strategy_doc() -> dict:
    return {
        "version": 2,
        "stable_market_seconds": 0.05,
        "max_round_trips": 1,
        "contracts": [
            {
                "symbol": "rb2601", "exchange": "SHFE", "target_lots": 1,
                "max_tick_age_seconds": 60,
                "quote_windows": [{"start": "00:00", "end": "23:59"}],
            },
            {
                "symbol": "AP610", "exchange": "CZCE", "target_lots": 1,
                "max_tick_age_seconds": 60,
                "quote_windows": [{"start": "00:00", "end": "23:59"}],
            },
        ],
    }


class FakeGateway:
    def __init__(self) -> None:
        self.query_count = 0

    def query_position(self) -> int:
        self.query_count += 1
        return 41


class FakeEngine:
    def __init__(self) -> None:
        self.gateway = FakeGateway()
        self.sent = []
        self.cancelled = []

    def get_gateway(self, name):
        return self.gateway

    def subscribe(self, request, name) -> None:
        pass

    def send_order(self, request, name) -> str:
        self.sent.append(request)
        return f"CTP.{len(self.sent)}"

    def cancel_order(self, request, name) -> None:
        self.cancelled.append(request)

    def close(self) -> None:
        pass


class ScriptedAdapter(CtpLiveGridAdapter):
    """Drive main() through quoting, an isolated first-fill closing, and a broadcast interrupt."""

    latest = None

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.rb_finished = False
        ScriptedAdapter.latest = self

    def start(self):
        self.main_engine = FakeEngine()
        threading.Thread(target=self._scenario, daemon=True).start()
        return self.main_engine

    def _tick(self, symbol, exchange):
        self._on_tick(
            SimpleNamespace(
                data=SimpleNamespace(
                    symbol=symbol,
                    exchange=SimpleNamespace(value=exchange),
                    last_price=100.0,
                    bid_price_1=99.0,
                    ask_price_1=101.0,
                    datetime=datetime.now(timezone.utc),
                    trading_day="20260824",
                    action_day="20260824",
                    update_millisec=0,
                    limit_up=200.0,
                    limit_down=0.1,
                )
            )
        )

    def _order(self, orderid, symbol, exchange, direction, status, volume, traded, price):
        self._on_order(
            SimpleNamespace(
                data=SimpleNamespace(
                    orderid=orderid,
                    reference=None,
                    symbol=symbol,
                    exchange=SimpleNamespace(value=exchange),
                    direction=SimpleNamespace(value=direction),
                    status=SimpleNamespace(value=status),
                    volume=volume,
                    traded=traded,
                    price=price,
                )
            )
        )

    def _trade(self, orderid, symbol, exchange, direction, volume, price, tradeid):
        self._on_trade(
            SimpleNamespace(
                data=SimpleNamespace(
                    orderid=orderid,
                    reference=None,
                    symbol=symbol,
                    exchange=SimpleNamespace(value=exchange),
                    direction=SimpleNamespace(value=direction),
                    volume=volume,
                    price=price,
                    tradeid=tradeid,
                )
            )
        )

    def _account(self, balance, available):
        self._on_account(
            SimpleNamespace(
                data=SimpleNamespace(accountid="SimNow8888", balance=balance, available=available)
            )
        )

    def _position_result(self, positions=()):
        self._on_position_query_complete(
            SimpleNamespace(
                data=SimpleNamespace(request_id=41, positions=positions, error_id=0, error_msg="")
            )
        )

    def _wait_for(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            threading.Event().wait(0.02)
        return False

    def _pump_until(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            self._tick("rb2601", "SHFE")
            self._tick("AP610", "CZCE")
            self._on_timer(SimpleNamespace(data=None))
            threading.Event().wait(0.03)
        return predicate()

    def _scenario(self) -> None:
        rb, ap = self.sessions
        engine = self.main_engine
        self._on_contract(
            SimpleNamespace(
                data=SimpleNamespace(
                    symbol="rb2601", exchange=SimpleNamespace(value="SHFE"), pricetick=1.0
                )
            )
        )
        self._on_contract(
            SimpleNamespace(
                data=SimpleNamespace(
                    symbol="AP610", exchange=SimpleNamespace(value="CZCE"), pricetick=1.0
                )
            )
        )
        self._position_result()
        self._account(1_000_000.0, 900_000.0)

        assert self._pump_until(
            lambda: rb.state.value == "QUOTE_PENDING"
            and ap.state.value == "QUOTE_PENDING"
            and len(engine.sent) >= 4
        )
        for index, request in enumerate(engine.sent, 1):
            self._order(
                str(index),
                request.symbol,
                "SHFE" if request.symbol == "rb2601" else "CZCE",
                request.direction.value,
                "未成交",
                1,
                0,
                request.price,
            )
        assert self._wait_for(lambda: rb.state.value == "QUOTING" and ap.state.value == "QUOTING")

        self._trade("1", "rb2601", "SHFE", "多", 1, 60.0, "trade-1")
        assert self._wait_for(lambda: rb.state.value == "CLOSING_WAIT")
        self._order("1", "rb2601", "SHFE", "多", "全部成交", 1, 1, 60.0)
        # 价差窗口超时：不撤对侧，直接按净仓 FAK 平仓（第 5 笔委托）。
        assert self._pump_until(lambda: len(engine.sent) >= 5)
        self._order("5", "rb2601", "SHFE", "空", "全部成交", 1, 1, 60.0)
        self._trade("5", "rb2601", "SHFE", "空", 1, 60.0, "trade-2")
        self._account(1_000_040.0, 899_940.0)
        # 平仓终态后撤对侧报价（委托 2），随后收尾对账净仓为零。
        assert self._wait_for(lambda: any(req.orderid == "2" for req in engine.cancelled))
        answered_queries = engine.gateway.query_count
        self._order("2", "rb2601", "SHFE", "空", "已撤销", 1, 0, 0)
        assert self._wait_for(lambda: engine.gateway.query_count > answered_queries)
        self._position_result()
        assert self._wait_for(lambda: rb.state.value in {"FINISHED", "FAILED"})
        self.rb_finished = True

        answered_cancels = set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and ap.state.value not in {"FINISHED", "FAILED"}:
            for req in list(engine.cancelled):
                if req.symbol == "AP610" and req.orderid not in answered_cancels:
                    answered_cancels.add(req.orderid)
                    self._order(
                        req.orderid,
                        "AP610",
                        "CZCE",
                        "多" if req.orderid == "3" else "空",
                        "已撤销",
                        1,
                        0,
                        0,
                    )
            if engine.gateway.query_count > answered_queries:
                answered_queries = engine.gateway.query_count
                self._position_result()
            threading.Event().wait(0.02)


def write_config(root: str, doc: dict) -> Path:
    config_path = Path(root) / "strategy.json"
    config_path.write_text(json.dumps(doc), encoding="utf-8")
    return config_path


class RunLiveGridTests(unittest.TestCase):
    def test_startup_failure_writes_complete_run_summary(self) -> None:
        with tempfile.TemporaryDirectory() as config_root, tempfile.TemporaryDirectory() as audit_root:
            config_path = write_config(config_root, multi_strategy_doc())
            argv = [
                "run_live_grid.py",
                "--config",
                str(config_path),
                "--env",
                "7x24",
                "--confirm-simnow",
                "--audit-dir",
                audit_root,
            ]
            with patch.object(sys, "argv", argv), patch.object(
                run_live_grid,
                "load_settings",
                side_effect=RuntimeError("missing credentials"),
            ) as load_settings:
                self.assertEqual(run_live_grid.main(), 3)
            load_settings.assert_called_once_with("7x24")

            run_directories = list(Path(audit_root).iterdir())
            self.assertEqual(len(run_directories), 1)
            run_directory = run_directories[0]
            run_summary = json.loads((run_directory / "summary.json").read_text())
            self.assertEqual(run_summary["environment"], "7x24")
            self.assertEqual(run_summary["failure_reason"], "missing credentials")
            self.assertEqual(run_summary["terminal_state"], "FAILED")
            self.assertEqual(
                run_summary["terminal_states"],
                {"rb2601@SHFE": "FAILED", "AP610@CZCE": "FAILED"},
            )
            self.assertEqual(run_summary["strategy_hash"], MultiContractConfig.from_mapping(multi_strategy_doc()).sha256)

            contract_directory = run_directory / "rb2601@SHFE"
            summary = json.loads((contract_directory / "summary.json").read_text())
            self.assertEqual(summary["environment"], "7x24")
            self.assertEqual(summary["terminal_state"], "FAILED")
            self.assertEqual(summary["failure_reason"], "missing credentials")
            for key in (
                "startup_position_result",
                "first_fill",
                "cancellation_terminal",
                "closing_position_request_id",
                "closing_position_result",
                "flatten_attempts",
                "final_net_position",
                "active_order_count",
                "active_orders",
            ):
                self.assertIn(key, summary)
            self.assertEqual(summary["active_order_count"], 0)
            self.assertEqual(summary["active_orders"], [])
            self.assertTrue((contract_directory / "events.jsonl").exists())

    def test_missing_confirmation_runs_preview_without_connecting(self) -> None:
        with tempfile.TemporaryDirectory() as config_root, tempfile.TemporaryDirectory() as audit_root:
            config_path = write_config(config_root, multi_strategy_doc())
            argv = [
                "run_live_grid.py",
                "--config",
                str(config_path),
                "--env",
                "7x24",
                "--audit-dir",
                audit_root,
            ]
            with patch.object(sys, "argv", argv), patch.object(
                run_live_grid,
                "load_settings",
                side_effect=AssertionError("预览模式不得加载凭证"),
            ) as load_settings:
                self.assertEqual(run_live_grid.main(), 0)
            load_settings.assert_not_called()

            run_directory = list(Path(audit_root).iterdir())[0]
            run_summary = json.loads((run_directory / "summary.json").read_text())
            self.assertEqual(run_summary["environment"], "7x24")
            self.assertEqual(
                run_summary["terminal_states"],
                {"rb2601@SHFE": "PREVIEW", "AP610@CZCE": "PREVIEW"},
            )
            self.assertEqual(run_summary["failure_reason"], "confirmation_required")
            for name in ("rb2601@SHFE", "AP610@CZCE"):
                summary = json.loads((run_directory / name / "summary.json").read_text())
                self.assertEqual(summary["environment"], "7x24")
                self.assertEqual(summary["terminal_state"], "PREVIEW")
                self.assertEqual(summary["failure_reason"], "confirmation_required")

    def test_legacy_single_contract_config_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as config_root, tempfile.TemporaryDirectory() as audit_root:
            config_path = write_config(
                config_root,
                {
                    "version": 1,
                    "symbol": "rb2601",
                    "exchange": "SHFE",
                    "target_lots": 1,
                },
            )
            argv = [
                "run_live_grid.py",
                "--config",
                str(config_path),
                "--confirm-simnow",
                "--audit-dir",
                audit_root,
            ]
            with patch.object(sys, "argv", argv):
                self.assertEqual(run_live_grid.main(), 2)
            self.assertEqual(list(Path(audit_root).iterdir()), [])

    def test_end_to_end_fake_gateway_run_closes_one_contract_and_interrupts_the_other(self) -> None:
        injected = {"done": False}

        def fake_sleep(seconds):
            adapter = ScriptedAdapter.latest
            if adapter is not None and adapter.rb_finished and not injected["done"]:
                injected["done"] = True
                raise KeyboardInterrupt()
            threading.Event().wait(seconds)

        with tempfile.TemporaryDirectory() as config_root, tempfile.TemporaryDirectory() as audit_root:
            config_path = write_config(config_root, multi_strategy_doc())
            argv = [
                "run_live_grid.py",
                "--config",
                str(config_path),
                "--confirm-simnow",
                "--audit-dir",
                audit_root,
            ]
            settings = SimpleNamespace(gateway_setting=lambda: {})
            with patch.object(sys, "argv", argv), patch.object(
                run_live_grid,
                "load_settings",
                return_value=settings,
            ), patch.object(
                run_live_grid,
                "CtpLiveGridAdapter",
                ScriptedAdapter,
            ), patch.object(
                run_live_grid.time,
                "sleep",
                fake_sleep,
            ):
                self.assertEqual(run_live_grid.main(), 0)

            run_directory = list(Path(audit_root).iterdir())[0]
            run_summary = json.loads((run_directory / "summary.json").read_text())
            self.assertEqual(run_summary["environment"], "first")
            self.assertEqual(
                run_summary["terminal_states"],
                {"rb2601@SHFE": "FINISHED", "AP610@CZCE": "FINISHED"},
            )
            self.assertTrue(run_summary["all_finished"])

            rb_summary = json.loads((run_directory / "rb2601@SHFE" / "summary.json").read_text())
            self.assertEqual(rb_summary["terminal_state"], "FINISHED")
            self.assertIsNotNone(rb_summary["first_fill"])
            self.assertEqual(rb_summary["final_net_position"], 0)
            self.assertEqual(len(rb_summary["flatten_attempts"]), 1)

            ap_summary = json.loads((run_directory / "AP610@CZCE" / "summary.json").read_text())
            self.assertEqual(ap_summary["terminal_state"], "FINISHED")
            self.assertIsNone(ap_summary["first_fill"])
            self.assertEqual(ap_summary["final_net_position"], 0)
            self.assertEqual(ap_summary["active_orders"], [])

            rb_events = (run_directory / "rb2601@SHFE" / "events.jsonl").read_text()
            ap_events = (run_directory / "AP610@CZCE" / "events.jsonl").read_text()
            self.assertGreater(len(rb_events.splitlines()), 0)
            self.assertGreater(len(ap_events.splitlines()), 0)
            self.assertNotIn("AP610", rb_events)
            self.assertNotIn("rb2601", ap_events)

            account_lines = (run_directory / "account.jsonl").read_text().splitlines()
            self.assertEqual(len(account_lines), 2)
            snapshots = [json.loads(line) for line in account_lines]
            self.assertEqual(snapshots[0]["balance"], 1_000_000.0)
            self.assertEqual(snapshots[1]["available"], 899_940.0)
            for snapshot in snapshots:
                self.assertEqual(set(snapshot), {"at", "balance", "available"})
            self.assertNotIn("SimNow8888", "\n".join(account_lines))


if __name__ == "__main__":
    unittest.main()
