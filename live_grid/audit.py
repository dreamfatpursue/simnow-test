"""Credential-free per-run audit artifacts."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .config import MultiContractConfig, StrategyConfig


class AuditError(ValueError):
    """Raised when an audit artifact cannot be written safely."""


AUDIT_SCHEMA_VERSION = 3
SUPPORTED_AUDIT_SCHEMA_VERSIONS = frozenset({2, AUDIT_SCHEMA_VERSION})

_QUERY_ITEM_FIELDS = {
    "OrderQueryCompleteEvent": "orders",
    "TradeQueryCompleteEvent": "trades",
}
_TRADE_QUERY_FIELDS = frozenset(
    {
        "InstrumentID", "ExchangeID", "ExchangeInstID", "TradeID", "OrderSysID",
        "OrderRef", "TradeDate", "TradeTime", "TradingDay", "Direction",
        "OffsetFlag", "HedgeFlag", "Price", "Volume", "TradeType",
        "PriceSource", "SequenceNo", "SettlementID",
    }
)


class AuditEventDecoder:
    """Restore v3 query snapshot references while retaining one snapshot per event kind."""

    def __init__(self, *, restore_query_items: bool = True) -> None:
        self._restore_query_items = restore_query_items
        self._snapshots: dict[str, tuple[int, int, list[Any] | None, Any, bool]] = {}
        self._last_snapshot_id = 0

    def decode(self, record: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(record, dict):
            raise AuditError("审计事件记录必须是 JSON 对象")
        event = record.get("event")
        if not isinstance(event, dict):
            if any(key in record for key in ("query_snapshot_id", "query_snapshot_ref", "query_snapshot_count")):
                raise AuditError("查询审计快照标记缺少事件")
            return record
        event_type = event.get("type")
        field = _QUERY_ITEM_FIELDS.get(event_type)
        if field is None:
            if any(key in record for key in ("query_snapshot_id", "query_snapshot_ref", "query_snapshot_count")):
                raise AuditError("查询审计快照标记出现在非查询事件中")
            return record
        data = event.get("data")
        if not isinstance(data, dict):
            raise AuditError("查询审计事件缺少 data 对象")

        has_reference = "query_snapshot_ref" in record
        has_snapshot_id = "query_snapshot_id" in record
        has_snapshot_count = "query_snapshot_count" in record
        if has_reference:
            reference = record["query_snapshot_ref"]
            if has_snapshot_id or not has_snapshot_count or type(reference) is not int:
                raise AuditError("查询审计快照引用格式无效")
            snapshot = self._snapshots.get(event_type)
            if snapshot is None or snapshot[0] != reference:
                raise AuditError("查询审计快照引用无效")
            count = record["query_snapshot_count"]
            if (
                field in data
                or "trace" in record
                or type(count) is not int
                or count != snapshot[1]
            ):
                raise AuditError("查询审计快照引用内容无效")
            decoded = dict(record)
            decoded_event = dict(event)
            decoded_data = dict(data)
            if self._restore_query_items:
                decoded_data[field] = snapshot[2]
            decoded_event["data"] = decoded_data
            decoded["event"] = decoded_event
            if snapshot[4]:
                decoded["trace"] = snapshot[3]
            else:
                decoded.pop("trace", None)
            decoded.pop("query_snapshot_ref", None)
            decoded.pop("query_snapshot_count", None)
            return decoded

        if has_snapshot_id:
            snapshot_id = record["query_snapshot_id"]
            items = data.get(field)
            if (
                type(snapshot_id) is not int
                or not isinstance(items, list)
                or not items
                or data.get("error_id")
                or has_snapshot_count
                or snapshot_id <= self._last_snapshot_id
            ):
                raise AuditError("完整查询快照内容无效")
            self._last_snapshot_id = snapshot_id
            self._snapshots[event_type] = (
                snapshot_id,
                len(items),
                items if self._restore_query_items else None,
                record.get("trace"),
                "trace" in record,
            )
            decoded = dict(record)
            decoded.pop("query_snapshot_id", None)
            if not self._restore_query_items:
                decoded_event = dict(event)
                decoded_data = dict(data)
                decoded_data.pop(field, None)
                decoded_event["data"] = decoded_data
                decoded["event"] = decoded_event
            return decoded

        if has_snapshot_count:
            raise AuditError("查询审计快照计数缺少引用")
        self._snapshots.pop(event_type, None)
        return record


_FORBIDDEN_KEYS = {
    "user_id",
    "username",
    "password",
    "broker_id",
    "trade_front",
    "market_front",
    "app_id",
    "auth_code",
    "accountid",
    "account_id",
    "产品名称",
    "用户名",
    "密码",
    "经纪商代码",
    "产品信息",
    "授权编码",
    "交易服务器",
    "行情服务器",
    "资金账号",
    "CTP_USER_ID",
    "CTP_PASSWORD",
    "CTP_BROKER_ID",
    "CTP_TRADE_FRONT",
    "CTP_MARKET_FRONT",
    "CTP_APP_ID",
    "CTP_AUTH_CODE",
    "CTP_PRODUCT_INFO",
}


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:10]


def _run_name() -> str:
    return new_run_id()


def _write_json_file(directory: Path, name: str, value: Any) -> None:
    AuditWriter._assert_safe(value)
    directory.joinpath(name).write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_json_line(handle: Any, value: Any) -> None:
    AuditWriter._assert_safe(value)
    handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    handle.flush()


class AuditWriter:
    """Write one isolated JSON audit directory for a session."""

    def __init__(
        self,
        config: StrategyConfig,
        root: str | Path = "audit",
        *,
        directory: str | Path | None = None,
    ) -> None:
        if directory is None:
            root_path = Path(root)
            root_path.mkdir(parents=True, exist_ok=True)
            directory = root_path / _run_name()
            Path(directory).mkdir()
        else:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=False)
        self.directory = directory
        self._events = self.directory.joinpath("events.jsonl").open("w", encoding="utf-8")
        self._closed = False
        self._query_snapshot_sequence = 0
        self._query_snapshots: dict[str, tuple[int, list[Any], Any, bool]] = {}
        _write_json_file(
            self.directory,
            "effective_strategy.json",
            {
                "audit_schema_version": AUDIT_SCHEMA_VERSION,
                "effective": config.effective,
                "sha256": config.sha256,
            },
        )

    def record(
        self,
        event: object,
        actions: list[object],
        state: str,
        at: float,
        state_before: str | None = None,
        trace: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None,
    ) -> None:
        self._ensure_open()
        record = {
            "at": at,
            "event": self._serialize(event),
            "actions": [self._serialize(action) for action in actions],
            "state_before": state_before or state,
            "state_after": state,
        }
        if trace:
            record["trace"] = self._serialize(trace)
        self._record_query_snapshot(record)
        self._write_line(record)

    def finish(self, summary: dict[str, Any]) -> Path:
        self._ensure_open()
        _write_json_file(self.directory, "summary.json", summary)
        self._events.close()
        self._closed = True
        return self.directory

    def close(self) -> None:
        if not self._closed:
            self._events.close()
            self._closed = True

    def _write_line(self, value: Any) -> None:
        _write_json_line(self._events, value)

    def _record_query_snapshot(self, record: dict[str, Any]) -> None:
        event = record["event"]
        event_type = event.get("type")
        field = _QUERY_ITEM_FIELDS.get(event_type)
        if field is None:
            return
        data = event.get("data")
        if not isinstance(data, dict):
            raise AuditError("查询审计事件缺少 data 对象")
        items = data.get(field)
        if event_type == "TradeQueryCompleteEvent" and isinstance(items, list):
            items = [
                {key: value for key, value in trade.items() if key in _TRADE_QUERY_FIELDS}
                for trade in items
                if isinstance(trade, dict)
            ]
            if len(items) != len(data.get(field, [])):
                raise AuditError("成交查询回报包含不支持的数据项")
            data[field] = items

        if data.get("error_id") or not isinstance(items, list) or not items:
            self._query_snapshots.pop(event_type, None)
            return

        has_trace = "trace" in record
        trace = record.get("trace")
        previous = self._query_snapshots.get(event_type)
        if (
            previous is not None
            and previous[1] == items
            and previous[2] == trace
            and previous[3] == has_trace
        ):
            data.pop(field)
            record.pop("trace", None)
            record["query_snapshot_ref"] = previous[0]
            record["query_snapshot_count"] = len(items)
            return

        self._query_snapshot_sequence += 1
        snapshot_id = self._query_snapshot_sequence
        record["query_snapshot_id"] = snapshot_id
        self._query_snapshots[event_type] = (snapshot_id, items, trace, has_trace)

    def _ensure_open(self) -> None:
        if self._closed:
            raise AuditError("审计目录已经关闭")

    @classmethod
    def _assert_safe(cls, value: Any) -> None:
        if is_dataclass(value):
            cls._assert_safe(asdict(value))
        elif isinstance(value, dict):
            forbidden = _FORBIDDEN_KEYS.intersection(value)
            if forbidden:
                raise AuditError("审计内容不得包含凭证字段: " + ", ".join(sorted(forbidden)))
            for item in value.values():
                cls._assert_safe(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                cls._assert_safe(item)
        elif hasattr(value, "__dict__") and not isinstance(value, type):
            cls._assert_safe(vars(value))

    @staticmethod
    def _serialize(value: Any) -> Any:
        if is_dataclass(value):
            return {"type": type(value).__name__, "data": AuditWriter._serialize(asdict(value))}
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, dict):
            return {key: AuditWriter._serialize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [AuditWriter._serialize(item) for item in value]
        if hasattr(value, "__dict__") and not isinstance(value, type):
            return {"type": type(value).__name__, "data": AuditWriter._serialize(vars(value))}
        return value


class MultiContractAuditWriter:
    """One run directory with per-contract audit subdirectories and a run-level summary."""

    def __init__(
        self,
        config: MultiContractConfig,
        root: str | Path = "audit",
        *,
        run_id: str | None = None,
    ) -> None:
        root_path = Path(root)
        root_path.mkdir(parents=True, exist_ok=True)
        self.directory = root_path / (run_id or _run_name())
        self.directory.mkdir()
        self.writers = [
            AuditWriter(
                contract_config,
                directory=self.directory / f"{contract_config.effective['symbol']}@{contract_config.effective['exchange']}",
            )
            for contract_config in config.contracts
        ]
        self._closed = False
        self._account_events = self.directory.joinpath("account.jsonl").open("w", encoding="utf-8")
        _write_json_file(
            self.directory,
            "effective_strategy.json",
            {
                "audit_schema_version": AUDIT_SCHEMA_VERSION,
                "effective": config.effective,
                "sha256": config.sha256,
            },
        )

    def record_account(self, *, balance: float, available: float, at: float) -> None:
        """Append one credential-free account balance snapshot for this run."""
        if self._closed:
            raise AuditError("审计目录已经关闭")
        _write_json_line(self._account_events, {"at": at, "balance": balance, "available": available})

    def finish(self, run_summary: dict[str, Any]) -> Path:
        if self._closed:
            raise AuditError("审计目录已经关闭")
        for writer in self.writers:
            writer.close()
        _write_json_file(self.directory, "summary.json", run_summary)
        self._account_events.close()
        self._closed = True
        return self.directory

    def close(self) -> None:
        if not self._closed:
            for writer in self.writers:
                writer.close()
            self._account_events.close()
            self._closed = True
