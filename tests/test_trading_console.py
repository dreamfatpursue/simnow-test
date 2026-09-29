import http.client
import json
import os
import signal
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from live_grid.account_recheck import read_latest_recheck
from live_grid.activity import ActivityIdentity, ActivityLock
from live_grid.config import StrategyConfigError
from trading_console import ConsoleInputError, ConsoleState, TradingConsoleServer


def strategy_document(*, stable_market_seconds: float = 2) -> dict:
    return {
        "version": 2,
        "contracts": [
            {
                "stable_market_seconds": stable_market_seconds,
                "symbol": "rb2601",
                "exchange": "SHFE",
                "target_lots": 1,
                "max_tick_age_seconds": 60,
                "quote_windows": [{"start": "00:00", "end": "23:59"}],
            }
        ],
    }


def write_strategy(root: str | Path, name: str = "strategy.json", **kwargs) -> Path:
    path = Path(root) / name
    path.write_text(json.dumps(strategy_document(**kwargs)), encoding="utf-8")
    return path


def ready_environment():
    return patch.dict(
        os.environ,
        {
            "CTP_USER_ID": "sim-user",
            "CTP_PASSWORD": "sim-password",
            "CTP_BROKER_ID": "sim-broker",
            "CTP_TRADE_FRONT": "tcp://127.0.0.1:1",
            "CTP_MARKET_FRONT": "tcp://127.0.0.1:2",
            "CTP_APP_ID": "sim-app",
            "CTP_AUTH_CODE": "sim-auth",
        },
        clear=False,
    )


class TradingConsoleStateTests(unittest.TestCase):
    def test_save_permanently_replaces_previewed_strategy_and_renews_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = write_strategy(root)
            state = ConsoleState(root)
            before = state.preview("strategy.json", "first")
            edited = json.loads(json.dumps(before["effective"]))
            edited["contracts"][0].update(w_ticks=24, d_ticks=8, s_ticks=6, max_round_trips=3)

            saved = state.save_strategy(
                "strategy.json", before["sha256"], edited, "first"
            )

            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted, saved["effective"])
            self.assertEqual(saved["effective"]["contracts"][0]["w_ticks"], 24)
            self.assertEqual(saved["effective"]["contracts"][0]["max_round_trips"], 3)
            self.assertNotEqual(saved["sha256"], before["sha256"])
            self.assertNotEqual(saved["confirmation"], before["confirmation"])

            with ready_environment(), patch("trading_console.subprocess.Popen", return_value=SimpleNamespace(pid=9876, poll=lambda: None)) as popen:
                state.start(saved["confirmation"])
            self.assertIn(str(path.resolve()), popen.call_args.args[0])

    def test_save_rejects_invalid_or_changed_source_without_overwriting_file(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = write_strategy(root)
            state = ConsoleState(root)
            preview = state.preview("strategy.json", "first")
            invalid = json.loads(json.dumps(preview["effective"]))
            invalid["contracts"][0].update(w_ticks=2, s_ticks=3)
            before = path.read_text(encoding="utf-8")

            with self.assertRaisesRegex(StrategyConfigError, "s_ticks 不得大于 w_ticks"):
                state.save_strategy("strategy.json", preview["sha256"], invalid, "first")
            self.assertEqual(path.read_text(encoding="utf-8"), before)

            write_strategy(root, stable_market_seconds=7)
            with self.assertRaisesRegex(ConsoleInputError, "文件已变化"):
                state.save_strategy("strategy.json", preview["sha256"], preview["effective"], "first")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["contracts"][0]["stable_market_seconds"], 7)

    def test_save_rejects_contract_identity_changes_and_active_runs(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            write_strategy(root)
            lock_path = Path(root) / "activity.lock"
            state = ConsoleState(root, activity_lock_path=lock_path)
            preview = state.preview("strategy.json", "first")
            changed_identity = json.loads(json.dumps(preview["effective"]))
            changed_identity["contracts"][0]["symbol"] = "rb2610"
            with self.assertRaisesRegex(ConsoleInputError, "合约标识"):
                state.save_strategy("strategy.json", preview["sha256"], changed_identity, "first")

            held, _ = ActivityLock.try_acquire(
                lock_path,
                ActivityIdentity(
                    run_id="existing-run", pid=1234, started_at="2026-09-04T01:02:03+00:00",
                    environment="first", market_data_mode="normal", strategy_hash="hash",
                    contracts=("rb2601@SHFE",), audit_dir="audit/existing-run",
                ),
            )
            try:
                with self.assertRaisesRegex(ConsoleInputError, "已有活动运行"):
                    state.save_strategy("strategy.json", preview["sha256"], preview["effective"], "first")
            finally:
                held.release()

    def test_list_and_preview_are_read_only_and_hide_environment_values(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            write_strategy(root)
            Path(root, "strategy-bad.json").write_text("{\"version\": 2}", encoding="utf-8")
            outside = Path(root).parent / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            Path(root, "strategy-link.json").symlink_to(outside)

            state = ConsoleState(root)
            items = state.list_strategies()
            self.assertEqual([item["name"] for item in items], ["strategy-bad.json", "strategy.json"])
            self.assertFalse(next(item for item in items if item["name"] == "strategy-bad.json")["valid"])

            with patch.dict(os.environ, {}, clear=True):
                preview = state.preview("strategy.json", "first")
            self.assertEqual(preview["sha256"], items[1]["sha256"])
            self.assertEqual(preview["contracts"], ["rb2601@SHFE"])
            self.assertFalse(preview["environment_status"]["ready"])
            self.assertIn("CTP_USER_ID", preview["environment_status"]["missing"])
            self.assertNotIn("sim-password", json.dumps(preview))
            self.assertNotIn("sim-auth", json.dumps(preview))
            self.assertFalse(Path(root, "audit").exists())

    def test_start_rechecks_hash_consumes_confirmation_and_passes_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config_path = write_strategy(root)
            state = ConsoleState(root)
            with ready_environment():
                preview = state.preview("strategy.json", "first")
                process = SimpleNamespace(pid=9876, poll=lambda: None)
                with patch("trading_console.subprocess.Popen", return_value=process) as popen:
                    result = state.start(preview["confirmation"])

            self.assertEqual(result, {"status": "starting", "pid": 9876, "message": "正在启动进程"})
            command = popen.call_args.args[0]
            self.assertIn(str(config_path.resolve()), command)
            self.assertIn("--confirm-simnow", command)
            self.assertNotIn(preview["effective"], command)
            with self.assertRaisesRegex(ConsoleInputError, "已失效"):
                state.start(preview["confirmation"])

    def test_start_launches_venv_python_instead_of_macos_framework_executable(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            write_strategy(root)
            venv = Path(root) / ".venv"
            venv_python = venv / "bin" / "python"
            venv_python.parent.mkdir(parents=True)
            venv_python.write_text("", encoding="utf-8")
            framework = "/opt/homebrew/Python.app/Contents/MacOS/Python"
            state = ConsoleState(root)
            process = SimpleNamespace(pid=4242, poll=lambda: None)
            with ready_environment():
                preview = state.preview("strategy.json", "first")
                with (
                    patch.dict(os.environ, {"VIRTUAL_ENV": str(venv)}),
                    patch("trading_console.sys.executable", framework),
                    patch("trading_console.sys.prefix", "/opt/homebrew/opt/python"),
                    patch("trading_console.sys.base_prefix", "/opt/homebrew/opt/python"),
                    patch("trading_console.subprocess.Popen", return_value=process) as popen,
                ):
                    state.start(preview["confirmation"])
            command = popen.call_args.args[0]
            self.assertEqual(command[0], str(venv_python))
            self.assertNotEqual(command[0], framework)

    def test_changed_source_requires_a_new_preview(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            doc = strategy_document()
            doc["contracts"].append({**doc["contracts"][0], "symbol": "rb2610", "w_ticks": 30, "max_round_trips": 3})
            path = Path(root, "strategy.json")
            path.write_text(json.dumps(doc), encoding="utf-8")
            state = ConsoleState(root)
            with ready_environment():
                preview = state.preview("strategy.json", "first")
                self.assertEqual([c["w_ticks"] for c in preview["effective"]["contracts"]], [20, 30])
                self.assertEqual([c["max_round_trips"] for c in preview["effective"]["contracts"]], [10, 3])
                doc["contracts"][1]["w_ticks"] = 31
                path.write_text(json.dumps(doc), encoding="utf-8")
                with patch("trading_console.subprocess.Popen") as popen:
                    with self.assertRaisesRegex(ConsoleInputError, "文件已变化"):
                        state.start(preview["confirmation"])
                    popen.assert_not_called()

    def test_console_restart_invalidates_in_memory_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            write_strategy(root)
            with ready_environment():
                preview = ConsoleState(root).preview("strategy.json", "first")
                with self.assertRaisesRegex(ConsoleInputError, "已失效"):
                    ConsoleState(root).start(preview["confirmation"])

    def test_existing_activity_is_returned_without_spawning_or_reading_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            write_strategy(root)
            lock_path = Path(root) / "activity.lock"
            held, _ = ActivityLock.try_acquire(
                lock_path,
                ActivityIdentity(
                    run_id="existing-run",
                    pid=1234,
                    started_at="2026-09-04T01:02:03+00:00",
                    environment="first",
                    market_data_mode="normal",
                    strategy_hash="existing-hash",
                    contracts=("rb2601@SHFE",),
                    audit_dir="audit/existing-run",
                ),
            )
            state = ConsoleState(root, activity_lock_path=lock_path)
            with ready_environment():
                preview = state.preview("strategy.json", "first")
                with patch("trading_console.subprocess.Popen") as popen:
                    result = state.start(preview["confirmation"])
            held.release()

            self.assertEqual(result["status"], "already_started")
            self.assertEqual(result["run"]["run_id"], "existing-run")
            popen.assert_not_called()

    def test_recheck_spawns_a_separate_read_only_module_and_blocks_start(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            write_strategy(root)
            audit_dir = Path(root) / "audit" / "existing-run"
            audit_dir.mkdir(parents=True)
            lock_path = Path(root) / "activity.lock"
            identity = ActivityIdentity(
                run_id="existing-run", pid=1234, started_at="2026-09-04T01:02:03+00:00",
                environment="first", market_data_mode="normal", strategy_hash="existing-hash",
                contracts=("rb2601@SHFE",), audit_dir=str(audit_dir),
            )
            held, _ = ActivityLock.try_acquire(lock_path, identity)
            held.release()
            state = ConsoleState(root, activity_lock_path=lock_path)
            checking = SimpleNamespace(pid=3456, poll=lambda: None)
            with patch("trading_console.subprocess.Popen", return_value=checking) as popen:
                result = state.recheck("existing-run")
            command = popen.call_args.args[0]
            self.assertEqual(result["status"], "checking")
            self.assertEqual(command[1:3], ["-m", "live_grid.account_recheck"])
            self.assertNotIn("--confirm-simnow", command)
            self.assertNotIn("send_order", " ".join(command))
            self.assertEqual(read_latest_recheck(audit_dir, "existing-run")["status"], "checking")

            with ready_environment():
                preview = state.preview("strategy.json", "first")
                with self.assertRaisesRegex(ConsoleInputError, "账户核对中"):
                    state.start(preview["confirmation"])

    def test_stop_sends_one_signal_only_to_matching_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            audit_dir = Path(root) / "audit" / "existing-run"
            audit_dir.mkdir(parents=True)
            lock_path = Path(root) / "activity.lock"
            identity = ActivityIdentity(
                run_id="existing-run",
                pid=4321,
                started_at="2026-09-04T01:02:03+00:00",
                environment="first",
                market_data_mode="normal",
                strategy_hash="existing-hash",
                contracts=("rb2601@SHFE",),
                audit_dir=str(audit_dir),
            )
            held, _ = ActivityLock.try_acquire(lock_path, identity)
            state = ConsoleState(root, activity_lock_path=lock_path)
            try:
                with patch("trading_console.os.kill") as kill:
                    first = state.stop("existing-run")
                    second = state.stop("existing-run")
                kill.assert_called_once_with(4321, signal.SIGINT)
                self.assertFalse(first["already_requested"])
                self.assertTrue(second["already_requested"])
                self.assertTrue(state.current_overview()["stop_requested"])
                with self.assertRaisesRegex(ConsoleInputError, "运行身份已变化"):
                    state.stop("other-run")
            finally:
                held.release()

            with self.assertRaisesRegex(ConsoleInputError, "没有活动运行"):
                state.stop("existing-run")


class TradingConsoleHttpTests(unittest.TestCase):
    def test_mutating_api_requires_local_host_origin_and_session_token(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            write_strategy(root)
            state = ConsoleState(root)
            server = TradingConsoleServer(("127.0.0.1", 0), state)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = server.server_port
                connection = http.client.HTTPConnection("127.0.0.1", port)
                connection.request("GET", "/api/session", headers={"Host": "127.0.0.1:8765"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                token = json.loads(response.read())["token"]

                connection.request("GET", "/api/run/current", headers={"Host": "127.0.0.1:8765"})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["status"], "idle")

                body = json.dumps({"strategy": "strategy.json", "environment": "first"})
                headers = {
                    "Host": "127.0.0.1:8765",
                    "Origin": "http://127.0.0.1:8765",
                    "Content-Type": "application/json",
                    "X-Console-Token": token,
                }
                connection.request("POST", "/api/preview", body=body, headers=headers)
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                preview = json.loads(response.read())
                self.assertEqual(preview["contracts"], ["rb2601@SHFE"])

                edited = json.loads(json.dumps(preview["effective"]))
                edited["contracts"][0]["w_ticks"] = 24
                connection.request(
                    "POST",
                    "/api/strategy/save",
                    body=json.dumps({"strategy": "strategy.json", "environment": "first", "expected_sha256": preview["sha256"], "effective": edited}),
                    headers=headers,
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["effective"]["contracts"][0]["w_ticks"], 24)

                connection.request("POST", "/api/preview", body=body, headers={**headers, "Origin": "http://evil.example"})
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()

                connection.request("GET", "/api/strategies", headers={"Host": "evil.example"})
                response = connection.getresponse()
                self.assertEqual(response.status, 403)
                response.read()

                connection.request(
                    "POST",
                    "/api/run/stop",
                    body=json.dumps({"run_id": "missing-run"}),
                    headers=headers,
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 409)
                self.assertIn("没有活动运行", json.loads(response.read())["error"])
                with patch.object(state, "flatten", return_value={"status": "requested"}) as flatten:
                    connection.request("POST", "/api/run/flatten", body=json.dumps({"run_id": "run-test", "contract": "rb2601@SHFE"}), headers=headers)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 202)
                    self.assertEqual(json.loads(response.read())["status"], "requested")
                    flatten.assert_called_once_with("run-test", "rb2601@SHFE")
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
