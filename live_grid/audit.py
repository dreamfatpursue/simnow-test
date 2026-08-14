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


_FORBIDDEN_KEYS = {
    "user_id",
    "username",
    "password",
    "broker_id",
    "trade_front",
    "market_front",
    "app_id",
    "auth_code",
    "产品名称",
    "用户名",
    "密码",
    "经纪商代码",
    "产品信息",
    "授权编码",
    "交易服务器",
    "行情服务器",
    "CTP_USER_ID",
    "CTP_PASSWORD",
    "CTP_BROKER_ID",
    "CTP_TRADE_FRONT",
    "CTP_MARKET_FRONT",
    "CTP_APP_ID",
    "CTP_AUTH_CODE",
    "CTP_PRODUCT_INFO",
}


def _run_name() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:10]


def _write_json_file(directory: Path, name: str, value: Any) -> None:
    AuditWriter._assert_safe(value)
    directory.joinpath(name).write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


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
        _write_json_file(
            self.directory,
            "effective_strategy.json",
            {"effective": config.effective, "sha256": config.sha256},
        )

    def record(
        self,
        event: object,
        actions: list[object],
        state: str,
        at: float,
        state_before: str | None = None,
    ) -> None:
        self._ensure_open()
        self._write_line(
            {
                "at": at,
                "event": self._serialize(event),
                "actions": [self._serialize(action) for action in actions],
                "state_before": state_before or state,
                "state_after": state,
            }
        )

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
        self._assert_safe(value)
        self._events.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        self._events.flush()

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

    def __init__(self, config: MultiContractConfig, root: str | Path = "audit") -> None:
        root_path = Path(root)
        root_path.mkdir(parents=True, exist_ok=True)
        self.directory = root_path / _run_name()
        self.directory.mkdir()
        self.writers = [
            AuditWriter(
                contract_config,
                directory=self.directory / f"{contract_config.effective['symbol']}@{contract_config.effective['exchange']}",
            )
            for contract_config in config.contracts
        ]
        self._closed = False
        _write_json_file(
            self.directory,
            "effective_strategy.json",
            {"effective": config.effective, "sha256": config.sha256},
        )

    def finish(self, run_summary: dict[str, Any]) -> Path:
        if self._closed:
            raise AuditError("审计目录已经关闭")
        for writer in self.writers:
            writer.close()
        _write_json_file(self.directory, "summary.json", run_summary)
        self._closed = True
        return self.directory

    def close(self) -> None:
        if not self._closed:
            for writer in self.writers:
                writer.close()
            self._closed = True
