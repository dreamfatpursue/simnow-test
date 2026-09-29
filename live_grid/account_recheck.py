"""One-shot read-only CTP account reconciliation for a stopped console run."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from live_grid.activity import ActivityLock, OperationLock, default_activity_lock_path, default_operation_lock_path
from live_grid.ctp_native import activate_ctp_native_libs
from run import SETTING_ENV_BY_PROFILE, load_settings


RECHECK_VERSION = 1
RECHECK_TIMEOUT_SECONDS = 120
_TERMINAL_ORDER_STATUSES = frozenset({"0", "2", "4", "5", "ALLTRADED", "CANCELLED", "REJECTED"})
_ACTIVE_ORDER_STATUSES = frozenset({"1", "3", "NOTTRADED", "PARTTRADED", "SUBMITTING", "NOTTOUCHED", "TOUCHED"})


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def contract_key(symbol: str, exchange: str) -> str:
    return f"{symbol}@{exchange}"


def split_contract(value: str) -> tuple[str, str]:
    symbol, separator, exchange = value.partition("@")
    if not separator or not symbol or not exchange:
        raise ValueError(f"无效目标合约: {value}")
    return symbol, exchange


def _value(item: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(item, dict) and name in item:
            return item[name]
        if hasattr(item, name):
            return getattr(item, name)
    return default


def _exchange_value(value: Any) -> str:
    return str(getattr(value, "value", value or "")).upper()


def summarize_orders(orders: Iterable[Any], contracts: Iterable[str]) -> dict[str, dict[str, int]]:
    target = {split_contract(item): item for item in contracts}
    result = {key: {"active_orders": 0, "unknown_orders": 0} for key in target.values()}
    seen: set[str] = set()
    for item in orders:
        symbol = str(_value(item, "InstrumentID", "symbol", default=""))
        exchange = _exchange_value(_value(item, "ExchangeID", "exchange"))
        key = contract_key(symbol, exchange)
        if (symbol, exchange) not in target:
            continue
        order_id = str(_value(item, "OrderSysID", "orderid", "OrderRef", "order_ref", default=""))
        identity = f"{key}:{order_id or json.dumps(item, default=str, sort_keys=True)}"
        if identity in seen:
            continue
        seen.add(identity)
        status = str(_value(item, "OrderStatus", "status", default="")).upper()
        if status in _TERMINAL_ORDER_STATUSES:
            continue
        if status in _ACTIVE_ORDER_STATUSES:
            result[key]["active_orders"] += 1
        else:
            result[key]["unknown_orders"] += 1
    return result


def summarize_positions(positions: Iterable[Any], contracts: Iterable[str]) -> dict[str, dict[str, int]]:
    target = {split_contract(item): item for item in contracts}
    result = {key: {"long_position": 0, "short_position": 0} for key in target.values()}
    for item in positions:
        symbol = str(_value(item, "symbol", "InstrumentID", default=""))
        exchange = _exchange_value(_value(item, "exchange", "ExchangeID"))
        key = contract_key(symbol, exchange)
        if (symbol, exchange) not in target:
            continue
        direction = str(_value(item, "direction", "PosiDirection", default="")).upper()
        volume = int(_value(item, "volume", "Position", default=0) or 0)
        if direction in {"LONG", "多", "2"}:
            result[key]["long_position"] += volume
        elif direction in {"SHORT", "空", "3"}:
            result[key]["short_position"] += volume
    return result


def evaluate_recheck(
    contracts: Iterable[str],
    initial_orders: Iterable[Any],
    positions: Iterable[Any],
    final_orders: Iterable[Any],
) -> dict[str, Any]:
    """Apply the conservative pass rule to complete query responses only."""
    contract_list = tuple(contracts)
    before = summarize_orders(initial_orders, contract_list)
    after = summarize_orders(final_orders, contract_list)
    position_summary = summarize_positions(positions, contract_list)
    rows: list[dict[str, Any]] = []
    risk = False
    unstable = before != after
    for key in contract_list:
        order = after[key]
        position = position_summary[key]
        row = {"contract": key, **position, **order}
        rows.append(row)
        risk = risk or any(row[name] for name in ("long_position", "short_position", "active_orders", "unknown_orders"))
    if unstable:
        return {"status": "incomplete", "stage": "委托复核", "message": "核对期间目标委托发生变化，请重新核对", "contracts": rows}
    if risk:
        return {"status": "risk", "stage": "完成", "message": "发现目标合约持仓或遗留委托", "contracts": rows}
    return {"status": "passed", "stage": "完成", "message": "目标合约持仓和委托核对通过", "contracts": rows}


def recheck_directory(audit_dir: str | Path) -> Path:
    return Path(audit_dir) / "rechecks"


def latest_recheck_path(audit_dir: str | Path) -> Path:
    return recheck_directory(audit_dir) / "latest.json"


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_recheck(audit_dir: str | Path, record: dict[str, Any]) -> None:
    check_id = str(record["check_id"])
    directory = recheck_directory(audit_dir)
    _atomic_write_json(directory / f"{check_id}.json", record)
    _atomic_write_json(directory / "latest.json", record)


def read_latest_recheck(audit_dir: str | Path, run_id: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(latest_recheck_path(audit_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != RECHECK_VERSION or payload.get("run_id") != run_id:
        return None
    if payload.get("status") not in {"checking", "passed", "risk", "incomplete"}:
        return None
    if not isinstance(payload.get("contracts"), list):
        return None
    return payload


def new_recheck_record(identity: Any, context_id: str, check_id: str | None = None) -> dict[str, Any]:
    return {
        "version": RECHECK_VERSION,
        "check_id": check_id or f"{int(time.time() * 1000)}-{secrets.token_hex(5)}",
        "run_id": identity.run_id,
        "environment": identity.environment,
        "contracts": list(identity.contracts),
        "context_id": context_id,
        "status": "checking",
        "stage": "本地检查",
        "message": "正在准备账户核对",
        "started_at": utc_now(),
        "completed_at": None,
    }


class LiveRecheck:
    """Event-driven CTP query sequence. It intentionally has no trading actions."""

    def __init__(self, contracts: Iterable[str], timeout_seconds: float) -> None:
        self.contracts = tuple(contracts)
        self.targets = set(split_contract(value) for value in self.contracts)
        self.timeout_seconds = timeout_seconds
        self.stage = "连接认证"
        self.error: str | None = None
        self.contracts_seen: set[tuple[str, str]] = set()
        self.contracts_finished = False
        self.initial_orders: tuple[Any, ...] | None = None
        self.positions: tuple[Any, ...] | None = None
        self.final_orders: tuple[Any, ...] | None = None
        self._expected_request: int | None = None
        self._gateway: Any = None
        self._done = threading.Event()
        self._lock = threading.RLock()

    def bind_gateway(self, gateway: Any) -> None:
        self._gateway = gateway

    def _fail(self, stage: str, message: str) -> None:
        if self._done.is_set():
            return
        self.stage, self.error = stage, message[:300]
        self._done.set()

    def on_connection(self, event: Any) -> None:
        data = event.data
        if not getattr(data, "connected", False):
            with self._lock:
                self._fail(self.stage, f"交易连接断开：{getattr(data, 'reason', '') or '未提供原因'}")

    def on_log(self, event: Any) -> None:
        message = str(getattr(event.data, "msg", event.data))
        with self._lock:
            if "失败" in message:
                self._fail(self.stage, message)
            elif "合约信息查询成功" in message:
                self.contracts_finished = True
                self._start_initial_order_query()

    def on_contract(self, event: Any) -> None:
        item = event.data
        key = (str(getattr(item, "symbol", "")), _exchange_value(getattr(item, "exchange", "")))
        with self._lock:
            if key in self.targets:
                self.contracts_seen.add(key)

    def _query(self, kind: str) -> None:
        method = getattr(self._gateway, f"query_{kind}", None)
        request_id = method() if callable(method) else None
        if request_id is None:
            refusal = getattr(self._gateway, "last_query_send_refusal", None) or getattr(
                getattr(self._gateway, "td_api", None), "last_query_send_refusal", None
            )
            label = "委托" if kind == "order" else "持仓"
            self._fail(self.stage, f"CTP {label}查询请求未发送" + (f"（{refusal}）" if refusal else ""))
            return
        self._expected_request = request_id

    def _start_initial_order_query(self) -> None:
        if self._done.is_set() or not self.contracts_finished:
            return
        if self.contracts_seen != self.targets:
            self._fail("合约确认", "未取得全部目标合约信息")
            return
        self.stage = "委托初查"
        self._query("order")

    def on_order_complete(self, event: Any) -> None:
        result = event.data
        with self._lock:
            if self._done.is_set() or result.request_id != self._expected_request:
                return
            if getattr(result, "error_id", 0):
                self._fail(self.stage, getattr(result, "error_msg", "CTP 委托查询失败") or "CTP 委托查询失败")
            elif self.stage == "委托初查":
                self.initial_orders = tuple(getattr(result, "orders", ()))
                self.stage = "持仓查询"
                self._query("position")
            elif self.stage == "委托复核":
                self.final_orders = tuple(getattr(result, "orders", ()))
                self._done.set()

    def on_position_complete(self, event: Any) -> None:
        result = event.data
        with self._lock:
            if self._done.is_set() or result.request_id != self._expected_request:
                return
            if getattr(result, "error_id", 0):
                self._fail(self.stage, getattr(result, "error_msg", "CTP 持仓查询失败") or "CTP 持仓查询失败")
                return
            self.positions = tuple(getattr(result, "positions", ()))
            self.stage = "委托复核"
            self._query("order")

    def wait(self) -> dict[str, Any]:
        if not self._done.wait(self.timeout_seconds):
            with self._lock:
                self._fail(self.stage, "CTP 查询超时，当前账户状态仍未确认")
        if self.error:
            return {"status": "incomplete", "stage": self.stage, "message": self.error, "contracts": []}
        if self.initial_orders is None or self.positions is None or self.final_orders is None:
            return {"status": "incomplete", "stage": self.stage, "message": "未取得完整 CTP 查询结果", "contracts": []}
        return evaluate_recheck(self.contracts, self.initial_orders, self.positions, self.final_orders)


def run_live_recheck(record: dict[str, Any], project_root: Path, timeout_seconds: float) -> dict[str, Any]:
    """Connect, query order-position-order, then close. No order action is available here."""
    environment = str(record["environment"])
    contracts = tuple(str(item) for item in record["contracts"])
    if environment not in SETTING_ENV_BY_PROFILE:
        return {"status": "incomplete", "stage": "本地检查", "message": "运行环境不受支持", "contracts": []}
    try:
        activate_ctp_native_libs(environment, project_root=project_root)
        settings = load_settings(environment)
        from vnpy.event import EventEngine
        from vnpy.trader.event import EVENT_CONTRACT, EVENT_LOG
        from vnpy.trader.engine import MainEngine
        from vnpy_ctp import CtpGateway
        from vnpy_ctp.gateway.position_query import EVENT_CTP_CONNECTION, EVENT_CTP_ORDER_QUERY_COMPLETE, EVENT_POSITION_QUERY_COMPLETE
    except Exception as exc:
        return {"status": "incomplete", "stage": "本地检查", "message": str(exc)[:300], "contracts": []}

    engine = EventEngine()
    main_engine = MainEngine(engine)
    runner = LiveRecheck(contracts, timeout_seconds)
    try:
        main_engine.add_gateway(CtpGateway)
        gateway = main_engine.get_gateway("CTP")
        gateway.td_api.configure_instrument_queries(f"{symbol}.{exchange}" for symbol, exchange in map(split_contract, contracts))
        runner.bind_gateway(gateway)
        engine.register(EVENT_LOG, runner.on_log)
        engine.register(EVENT_CONTRACT, runner.on_contract)
        engine.register(EVENT_CTP_CONNECTION, runner.on_connection)
        engine.register(EVENT_CTP_ORDER_QUERY_COMPLETE, runner.on_order_complete)
        engine.register(EVENT_POSITION_QUERY_COMPLETE, runner.on_position_complete)
        main_engine.connect(settings.gateway_setting(), "CTP")
        return runner.wait()
    except Exception as exc:
        return {"status": "incomplete", "stage": runner.stage, "message": str(exc)[:300], "contracts": []}
    finally:
        main_engine.close()


def execute_recheck(project_root: Path, run_id: str, context_id: str, timeout_seconds: float = RECHECK_TIMEOUT_SECONDS) -> int:
    lock_path = default_activity_lock_path(project_root)
    identity = ActivityLock.read_record(lock_path)
    if identity is None or identity.run_id != run_id:
        return 2
    existing = read_latest_recheck(identity.audit_dir, run_id)
    record = (existing if existing and existing.get("context_id") == context_id and existing.get("status") == "checking"
              else new_recheck_record(identity, context_id))
    operation = OperationLock.try_acquire(default_operation_lock_path(project_root))
    if operation is None:
        record.update(status="incomplete", stage="本地检查", message="账户核对或启动操作正在进行", completed_at=utc_now(), contracts=[])
        write_recheck(identity.audit_dir, record)
        return 3
    try:
        result = ({"status": "incomplete", "stage": "本地检查", "message": "已有活动交易运行，不能执行账户核对", "contracts": []}
                  if ActivityLock.read_active(lock_path) is not None else run_live_recheck(record, project_root, timeout_seconds))
        record.update(result, completed_at=utc_now())
        write_recheck(identity.audit_dir, record)
        return 0
    finally:
        operation.release()


def main() -> int:
    parser = argparse.ArgumentParser(description="CTP read-only account recheck")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--context-id", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=RECHECK_TIMEOUT_SECONDS)
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds 必须为正数")
    return execute_recheck(Path(args.project_root).resolve(), args.run_id, args.context_id, args.timeout_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
