"""Small localhost-only control service for the SimNow trading entrypoint."""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from live_grid.activity import ActivityIdentity, ActivityLock, default_activity_lock_path
from live_grid.config import MultiContractConfig, StrategyConfigError
from live_grid.console_projection import AuditProjector
from run import SETTING_ENV_BY_PROFILE


ALLOWED_HOSTS = frozenset({"127.0.0.1:8765", "localhost:8765"})
ALLOWED_ORIGINS = frozenset({"http://127.0.0.1:8765", "http://localhost:8765"})
_STRATEGY_NAME = re.compile(r"^strategy[A-Za-z0-9_.-]*\.json$")
_MAX_REQUEST_BODY = 1_000_000
_REQUIRED_ENV_KEYS = ("user_id", "password", "broker_id", "trade_front", "market_front", "app_id", "auth_code")


class ConsoleInputError(ValueError):
    """Raised for a rejected console request."""


@dataclass(frozen=True)
class PreviewConfirmation:
    token: str
    strategy_name: str
    environment: str
    market_data_mode: str
    strategy_hash: str


def environment_status(environment: str) -> dict[str, Any]:
    try:
        names = SETTING_ENV_BY_PROFILE[environment]
    except KeyError as exc:
        raise ConsoleInputError(f"不支持的仿真环境: {environment}") from exc
    missing = [names[key] for key in _REQUIRED_ENV_KEYS if not os.environ.get(names[key], "").strip()]
    return {"ready": not missing, "missing": missing}


def trading_python() -> str:
    """Prefer the virtualenv interpreter over a macOS framework re-exec path."""
    roots: list[str] = []
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        roots.append(sys.prefix)
    virtual_env = os.environ.get("VIRTUAL_ENV", "").strip()
    if virtual_env:
        roots.append(virtual_env)
    for root in roots:
        candidate = Path(root) / "bin" / "python"
        if candidate.is_file():
            return str(candidate)
    return sys.executable


class ConsoleState:
    """In-memory confirmation state and the process boundary for one console."""

    def __init__(
        self,
        project_root: str | Path | None = None,
        *,
        entrypoint: str | Path | None = None,
        activity_lock_path: str | Path | None = None,
        page_path: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root or Path(__file__).resolve().parent).resolve()
        self.entrypoint = Path(entrypoint or self.project_root / "run_live_grid.py").resolve()
        self.activity_lock_path = Path(activity_lock_path or default_activity_lock_path(self.project_root))
        self.page_path = Path(page_path or self.project_root / "trading_console" / "index.html").resolve()
        self.token = secrets.token_urlsafe(24)
        self._confirmation: PreviewConfirmation | None = None
        self._launching_process: Any | None = None
        self._projectors: dict[str, AuditProjector] = {}
        self._stop_requested: set[str] = set()
        self._start_guard = threading.Lock()

    def _safe_strategy_path(self, strategy_name: str) -> Path:
        if (
            not isinstance(strategy_name, str)
            or Path(strategy_name).name != strategy_name
            or _STRATEGY_NAME.fullmatch(strategy_name) is None
        ):
            raise ConsoleInputError("策略文件名不在允许范围内")
        candidate = self.project_root / strategy_name
        if candidate.is_symlink():
            raise ConsoleInputError("策略文件不得是符号链接")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise ConsoleInputError(f"策略文件不可读: {strategy_name}") from exc
        if not candidate.is_file() or resolved.parent != self.project_root:
            raise ConsoleInputError("策略文件不在项目根目录")
        return resolved

    @staticmethod
    def _contract_names(config: MultiContractConfig) -> list[str]:
        return [f"{item.effective['symbol']}@{item.effective['exchange']}" for item in config.contracts]

    def list_strategies(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for candidate in sorted(self.project_root.glob("strategy*.json"), key=lambda item: item.name):
            if candidate.is_symlink() or _STRATEGY_NAME.fullmatch(candidate.name) is None:
                continue
            try:
                path = self._safe_strategy_path(candidate.name)
                config = MultiContractConfig.from_json_file(path)
            except (ConsoleInputError, StrategyConfigError) as exc:
                result.append({"name": candidate.name, "valid": False, "error": str(exc)})
                continue
            result.append(
                {
                    "name": candidate.name,
                    "valid": True,
                    "sha256": config.sha256,
                    "contracts": self._contract_names(config),
                }
            )
        return result

    def preview(self, strategy_name: str, environment: str, allow_replay_market_data: bool = False) -> dict[str, Any]:
        if environment not in SETTING_ENV_BY_PROFILE:
            raise ConsoleInputError(f"不支持的仿真环境: {environment}")
        if not isinstance(allow_replay_market_data, bool):
            raise ConsoleInputError("历史行情许可必须是布尔值")
        if allow_replay_market_data and environment != "7x24":
            raise ConsoleInputError("历史行情许可仅允许 7x24 环境")

        path = self._safe_strategy_path(strategy_name)
        try:
            config = MultiContractConfig.from_json_file(path)
        except StrategyConfigError:
            raise
        mode = "replay_override" if allow_replay_market_data else "normal"
        confirmation = PreviewConfirmation(
            token=secrets.token_urlsafe(24),
            strategy_name=strategy_name,
            environment=environment,
            market_data_mode=mode,
            strategy_hash=config.sha256,
        )
        with self._start_guard:
            self._confirmation = confirmation
        return {
            "strategy": strategy_name,
            "environment": environment,
            "market_data_mode": mode,
            "effective": config.effective,
            "sha256": config.sha256,
            "contracts": self._contract_names(config),
            "environment_status": environment_status(environment),
            "confirmation": confirmation.token,
        }

    def _launching_status(self) -> dict[str, Any] | None:
        process = self._launching_process
        if process is None:
            return None
        if process.poll() is None:
            return {"status": "starting", "pid": process.pid}
        self._launching_process = None
        return None

    def start(self, confirmation_token: str) -> dict[str, Any]:
        with self._start_guard:
            confirmation = self._confirmation
            if confirmation is None or confirmation.token != confirmation_token:
                raise ConsoleInputError("启动确认已失效，请重新预览策略")

            path = self._safe_strategy_path(confirmation.strategy_name)
            try:
                config = MultiContractConfig.from_json_file(path)
            except StrategyConfigError:
                raise
            if config.sha256 != confirmation.strategy_hash:
                raise ConsoleInputError("策略文件已变化，请重新预览")

            active = ActivityLock.read_active(self.activity_lock_path)
            if active is not None:
                self._confirmation = None
                return {"status": "already_started", "message": "已有活动运行", "run": active.as_dict()}

            launching = self._launching_status()
            if launching is not None:
                self._confirmation = None
                return launching

            env_status = environment_status(confirmation.environment)
            if not env_status["ready"]:
                raise ConsoleInputError("仿真环境缺少必要变量: " + ", ".join(env_status["missing"]))

            command = [
                trading_python(),
                str(self.entrypoint),
                "--config",
                str(path),
                "--env",
                confirmation.environment,
                "--confirm-simnow",
                "--audit-dir",
                str(self.project_root / "audit"),
            ]
            if confirmation.market_data_mode == "replay_override":
                command.append("--allow-replay-market-data")

            log_path = self.project_root / "audit" / ".console-run.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with log_path.open("a", encoding="utf-8") as log_handle:
                    process = subprocess.Popen(
                        command,
                        cwd=self.project_root,
                        stdin=subprocess.DEVNULL,
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
            except OSError as exc:
                raise ConsoleInputError(f"交易进程启动失败: {exc}") from exc
            self._confirmation = None
            self._launching_process = process
            return {"status": "starting", "pid": process.pid, "message": "正在启动进程"}

    def current_activity(self) -> ActivityIdentity | None:
        return ActivityLock.read_active(self.activity_lock_path)

    @staticmethod
    def _stop_marker(identity: ActivityIdentity) -> Path:
        return Path(identity.audit_dir) / ".console-stop-requested"

    def _stop_was_requested(self, identity: ActivityIdentity) -> bool:
        return identity.run_id in self._stop_requested or self._stop_marker(identity).exists()

    def stop(self, run_id: str) -> dict[str, Any]:
        """Send one SIGINT to the process holding the matching activity lock."""
        with self._start_guard:
            active = self.current_activity()
            if active is None:
                raise ConsoleInputError("当前没有活动运行")
            if not isinstance(run_id, str) or run_id != active.run_id:
                raise ConsoleInputError("运行身份已变化，请刷新当前运行页面")
            if self._stop_was_requested(active):
                return {
                    "status": "stopping",
                    "already_requested": True,
                    "message": "安全停止已经请求，继续等待收口",
                    "run": active.as_dict(),
                }
            if active.pid <= 0 or active.pid == os.getpid():
                raise ConsoleInputError("活动运行进程身份无效")
            try:
                os.kill(active.pid, signal.SIGINT)
            except ProcessLookupError as exc:
                raise ConsoleInputError("活动运行进程已退出，请刷新当前运行页面") from exc
            except PermissionError as exc:
                raise ConsoleInputError("没有权限请求该活动运行安全停止") from exc
            except OSError as exc:
                raise ConsoleInputError(f"安全停止请求失败: {exc}") from exc
            self._stop_requested.add(active.run_id)
            try:
                marker = self._stop_marker(active)
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch(exist_ok=True)
            except OSError:
                # 进程内集合仍保证本次控制服务生命周期内的幂等性；信号已发出。
                pass
            return {
                "status": "stopping",
                "already_requested": False,
                "message": "安全停止请求已发送，正在等待撤单、对账和必要的 FAK 平仓",
                "run": active.as_dict(),
            }

    def current_overview(self) -> dict[str, Any]:
        active = self.current_activity()
        if active is not None:
            status = "active"
            identity = active
        else:
            launching = self._launching_status()
            if launching is not None:
                return launching
            identity = ActivityLock.read_record(self.activity_lock_path)
            if identity is None:
                return {"status": "idle"}
            status = "terminal" if (Path(identity.audit_dir) / "summary.json").exists() else "process_abnormal_exit"
        projector = self._projectors.setdefault(identity.run_id, AuditProjector(identity))
        snapshot = projector.refresh(status=status)
        if status == "active":
            snapshot["stop_requested"] = self._stop_was_requested(identity)
        elif status == "process_abnormal_exit":
            snapshot["run_risk"] = {
                "key": f"{identity.run_id}:process_abnormal_exit",
                "reason": "process_abnormal_exit",
            }
        return snapshot


class TradingConsoleHandler(BaseHTTPRequestHandler):
    server_version = "SimNowTradingConsole/1.0"

    @property
    def console_state(self) -> ConsoleState:
        return self.server.console_state  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _host_allowed(self) -> bool:
        if self.headers.get("Host") in ALLOWED_HOSTS:
            return True
        self._send_error_json(403, "仅允许本机控制台地址")
        return False

    def _mutation_allowed(self) -> bool:
        if not self._host_allowed():
            return False
        if self.headers.get("Origin") not in ALLOWED_ORIGINS:
            self._send_error_json(403, "请求来源不是本机控制台")
            return False
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            self._send_error_json(415, "状态变更请求必须使用 JSON")
            return False
        if self.headers.get("X-Console-Token") != self.console_state.token:
            self._send_error_json(403, "控制台会话令牌无效")
            return False
        return True

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > _MAX_REQUEST_BODY:
            self._send_error_json(413, "请求体大小无效")
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(400, "请求体不是有效 JSON")
            return None
        if not isinstance(payload, dict):
            self._send_error_json(400, "请求体必须是 JSON 对象")
            return None
        return payload

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._host_allowed():
            return
        path = urlsplit(self.path).path
        if path == "/":
            try:
                body = self.console_state.page_path.read_bytes()
            except OSError:
                self._send_error_json(500, "控制台页面不可用")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/session":
            self._send_json(200, {"token": self.console_state.token})
            return
        if path == "/api/strategies":
            try:
                strategies = self.console_state.list_strategies()
            except OSError as exc:
                self._send_error_json(500, f"策略列表不可用: {exc}")
                return
            self._send_json(200, {"strategies": strategies})
            return
        if path == "/api/run/current":
            self._send_json(200, self.console_state.current_overview())
            return
        self._send_error_json(404, "接口不存在")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if not self._mutation_allowed():
            return
        payload = self._read_json()
        if payload is None:
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/preview":
                result = self.console_state.preview(
                    payload.get("strategy"),
                    payload.get("environment"),
                    payload.get("allow_replay_market_data", False),
                )
                self._send_json(200, result)
                return
            if path == "/api/run/start":
                result = self.console_state.start(payload.get("confirmation"))
                self._send_json(202 if result["status"] == "starting" else 200, result)
                return
            if path == "/api/run/stop":
                result = self.console_state.stop(payload.get("run_id"))
                self._send_json(202, result)
                return
        except StrategyConfigError as exc:
            self._send_error_json(422, str(exc))
            return
        except ConsoleInputError as exc:
            self._send_error_json(409, str(exc))
            return
        self._send_error_json(404, "接口不存在")

    def log_message(self, format: str, *args: Any) -> None:
        return


class TradingConsoleServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], console_state: ConsoleState) -> None:
        self.console_state = console_state
        super().__init__(server_address, TradingConsoleHandler)
