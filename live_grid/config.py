"""Credential-free strategy configuration and launch confirmation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any, Mapping


class StrategyConfigError(ValueError):
    """Raised when a strategy configuration cannot be used safely."""


_EXCHANGES = {"CFFEX", "SHFE", "CZCE", "DCE", "INE", "GFEX"}
_CREDENTIAL_KEYS = {
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
_DEFAULTS: dict[str, int] = {
    "w_ticks": 20,
    "d_ticks": 20,
    "s_ticks": 10,
    "book_protection_multiple": 2,
    "reanchor_confirmation_seconds": 1,
    "stable_market_seconds": 2,
    "action_limit_per_minute": 60,
    "cancel_timeout_seconds": 10,
    "flatten_timeout_seconds": 3,
    "flatten_adverse_ticks": 10,
}
_REQUIRED = {"version", "symbol", "exchange", "target_lots"}
_POSITIVE_FIELDS = {
    "book_protection_multiple",
    "reanchor_confirmation_seconds",
    "stable_market_seconds",
    "action_limit_per_minute",
    "cancel_timeout_seconds",
    "flatten_timeout_seconds",
    "flatten_adverse_ticks",
}
_POSITIVE_INTEGER_FIELDS = {"target_lots", "w_ticks", "d_ticks", "s_ticks"}


@dataclass(frozen=True)
class StrategyConfig:
    """Canonical, credential-free settings for one test run."""

    effective: dict[str, Any]
    canonical_json: str
    sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StrategyConfig":
        credential_keys = _CREDENTIAL_KEYS.intersection(raw)
        if credential_keys:
            raise StrategyConfigError("策略配置不得包含凭证字段: " + ", ".join(sorted(credential_keys)))

        missing = _REQUIRED - raw.keys()
        if missing:
            raise StrategyConfigError("缺少策略字段: " + ", ".join(sorted(missing)))

        effective = dict(_DEFAULTS)
        effective.update(raw)
        effective["exchange"] = str(effective["exchange"]).upper()
        if effective["exchange"] not in _EXCHANGES:
            raise StrategyConfigError(f"exchange 不是有效交易所: {effective['exchange']}")
        if not isinstance(effective["symbol"], str) or not effective["symbol"].strip():
            raise StrategyConfigError("symbol 必须是非空字符串")
        effective["symbol"] = effective["symbol"].strip()
        for name in _POSITIVE_INTEGER_FIELDS:
            value = effective[name]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise StrategyConfigError(f"{name} 必须是正整数")
        for name in _POSITIVE_FIELDS:
            value = effective[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value <= 0
            ):
                raise StrategyConfigError(f"{name} 必须是正数")
        if (
            isinstance(effective["version"], bool)
            or not isinstance(effective["version"], int)
            or effective["version"] <= 0
        ):
            raise StrategyConfigError("version 必须是正整数")

        unknown = set(effective) - (_REQUIRED | set(_DEFAULTS))
        if unknown:
            raise StrategyConfigError("未知策略字段: " + ", ".join(sorted(unknown)))

        canonical_json = json.dumps(effective, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(
            effective=effective,
            canonical_json=canonical_json,
            sha256=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
        )

    @classmethod
    def from_json_file(cls, path: str | Path) -> "StrategyConfig":
        config_path = Path(path)
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StrategyConfigError(f"策略配置读取失败: {config_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise StrategyConfigError("策略配置根节点必须是 JSON 对象")
        return cls.from_mapping(raw)

    def can_submit(self, *, simnow_confirmed: bool, hash_prefix: str) -> bool:
        if not simnow_confirmed or not hash_prefix or len(hash_prefix) < 8:
            return False
        return self.sha256.startswith(hash_prefix.lower())
