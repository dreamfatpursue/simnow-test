"""Credential-free strategy configuration and launch confirmation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
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
_DEFAULTS: dict[str, Any] = {
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
    "max_round_trips": 10,
    "closing_wait_seconds": 1,
    "quote_ack_timeout_seconds": 5,
}
_REQUIRED = {
    "version",
    "symbol",
    "exchange",
    "target_lots",
    "max_tick_age_seconds",
    "quote_windows",
}
_POSITIVE_FIELDS = {
    "book_protection_multiple",
    "reanchor_confirmation_seconds",
    "stable_market_seconds",
    "max_tick_age_seconds",
    "action_limit_per_minute",
    "cancel_timeout_seconds",
    "flatten_timeout_seconds",
    "flatten_adverse_ticks",
    "quote_ack_timeout_seconds",
}
_NON_NEGATIVE_FIELDS = {"closing_wait_seconds"}
_POSITIVE_INTEGER_FIELDS = {"target_lots", "w_ticks", "d_ticks", "s_ticks", "max_round_trips"}

_CLOCK_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _clock_minutes(value: Any, *, field: str) -> int:
    if not isinstance(value, str) or _CLOCK_TIME_PATTERN.fullmatch(value) is None:
        raise StrategyConfigError(f"{field} 必须是 HH:MM 格式: {value!r}")
    hour, minute = (int(part) for part in value.split(":"))
    return hour * 60 + minute


def _validate_quote_windows(effective: dict[str, Any]) -> None:
    windows = effective["quote_windows"]
    if not isinstance(windows, list) or not windows:
        raise StrategyConfigError("quote_windows 必须是非空数组")

    normalized: list[dict[str, str]] = []
    previous_start: int | None = None
    previous_end: int | None = None
    for index, window in enumerate(windows):
        if not isinstance(window, dict):
            raise StrategyConfigError(f"quote_windows[{index}] 必须是 JSON 对象")
        if set(window) != {"start", "end"}:
            unknown = set(window) - {"start", "end"}
            missing = {"start", "end"} - set(window)
            if missing:
                raise StrategyConfigError(
                    f"quote_windows[{index}] 缺少字段: " + ", ".join(sorted(missing))
                )
            raise StrategyConfigError(
                f"quote_windows[{index}] 未知字段: " + ", ".join(sorted(unknown))
            )
        start_text = window["start"]
        end_text = window["end"]
        start = _clock_minutes(start_text, field=f"quote_windows[{index}].start")
        end = _clock_minutes(end_text, field=f"quote_windows[{index}].end")
        if start == end:
            raise StrategyConfigError(f"quote_windows[{index}] start 与 end 不能相同")

        # 顺序下降表示跨午夜；同一交易日内的重叠窗口仍然拒绝。
        start_day = 0 if previous_start is None or start >= previous_start else 1
        start_abs = start + start_day * 1440
        if previous_end is not None and start_abs < previous_end:
            raise StrategyConfigError(f"quote_windows[{index}] 与前一窗口重叠或顺序无效")
        duration = (end - start) % 1440
        end_abs = start_abs + duration
        normalized.append({"start": start_text, "end": end_text})
        previous_start = start
        previous_end = end_abs
    effective["quote_windows"] = normalized


def _reject_credentials(raw: Mapping[str, Any]) -> None:
    credential_keys = _CREDENTIAL_KEYS.intersection(raw)
    if credential_keys:
        raise StrategyConfigError("策略配置不得包含凭证字段: " + ", ".join(sorted(credential_keys)))


def _require_positive_integers(effective: Mapping[str, Any], names: set[str]) -> None:
    for name in names:
        value = effective[name]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise StrategyConfigError(f"{name} 必须是正整数")


def _require_positive(effective: Mapping[str, Any], names: set[str]) -> None:
    for name in names:
        value = effective[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value <= 0
        ):
            raise StrategyConfigError(f"{name} 必须是正数")


def _require_non_negative(effective: Mapping[str, Any], names: set[str]) -> None:
    for name in names:
        value = effective[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value < 0
        ):
            raise StrategyConfigError(f"{name} 必须是非负数")


def _identity(effective: Mapping[str, Any]) -> tuple[str, str]:
    canonical_json = json.dumps(effective, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sha256 = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    return canonical_json, sha256


def _load_mapping(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StrategyConfigError(f"策略配置读取失败: {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise StrategyConfigError("策略配置根节点必须是 JSON 对象")
    return raw


@dataclass(frozen=True)
class StrategyConfig:
    """Canonical, credential-free settings for one test run."""

    effective: dict[str, Any]
    canonical_json: str
    sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StrategyConfig":
        _reject_credentials(raw)

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
        _require_positive_integers(effective, _POSITIVE_INTEGER_FIELDS)
        # 重锚按 s_ticks 步进移动锚点：步长超过带宽半宽 w_ticks 时，新带会把当前价甩在带外，
        # 会话陷入反复撤挂直到耗尽报撤限额。
        if effective["s_ticks"] > effective["w_ticks"]:
            raise StrategyConfigError(
                f"s_ticks 不得大于 w_ticks: {effective['s_ticks']} > {effective['w_ticks']}"
            )
        _require_positive(effective, _POSITIVE_FIELDS)
        _require_non_negative(effective, _NON_NEGATIVE_FIELDS)
        _validate_quote_windows(effective)
        if (
            isinstance(effective["version"], bool)
            or not isinstance(effective["version"], int)
            or effective["version"] <= 0
        ):
            raise StrategyConfigError("version 必须是正整数")

        unknown = set(effective) - (_REQUIRED | set(_DEFAULTS))
        if unknown:
            raise StrategyConfigError("未知策略字段: " + ", ".join(sorted(unknown)))

        canonical_json, sha256 = _identity(effective)
        return cls(
            effective=effective,
            canonical_json=canonical_json,
            sha256=sha256,
        )

    @classmethod
    def from_json_file(cls, path: str | Path) -> "StrategyConfig":
        return cls.from_mapping(_load_mapping(path))

    def can_submit(self, *, simnow_confirmed: bool) -> bool:
        return simnow_confirmed


_MULTI_ENTRY_REQUIRED = {
    "symbol",
    "exchange",
    "target_lots",
    "max_tick_age_seconds",
    "quote_windows",
}


@dataclass(frozen=True)
class MultiContractConfig:
    """Canonical, credential-free settings for one multi-contract run."""

    effective: dict[str, Any]
    canonical_json: str
    sha256: str
    contracts: tuple[StrategyConfig, ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "MultiContractConfig":
        _reject_credentials(raw)

        if "contracts" not in raw:
            if "symbol" in raw:
                raise StrategyConfigError(
                    "旧单合约格式已停用: 请将 symbol/exchange/target_lots 移入 contracts 数组, 并将 version 改为 2"
                )
            raise StrategyConfigError("缺少策略字段: contracts")
        version = raw["version"] if "version" in raw else None
        if isinstance(version, bool) or not isinstance(version, int) or version != 2:
            raise StrategyConfigError("version 必须是 2 (多合约格式)")

        entries = raw["contracts"]
        if not isinstance(entries, list) or not entries:
            raise StrategyConfigError("contracts 必须是非空数组")

        allowed = {"version", "contracts"} | set(_DEFAULTS)
        unknown = set(raw) - allowed
        if unknown:
            raise StrategyConfigError("未知策略字段: " + ", ".join(sorted(unknown)))

        common = dict(_DEFAULTS)
        common.update({name: raw[name] for name in _DEFAULTS if name in raw})
        _require_positive_integers(common, _POSITIVE_INTEGER_FIELDS - {"target_lots"})
        _require_positive(common, _POSITIVE_FIELDS - {"max_tick_age_seconds"})
        _require_non_negative(common, _NON_NEGATIVE_FIELDS)

        per_contract: list[StrategyConfig] = []
        seen: set[tuple[str, str]] = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise StrategyConfigError(f"contracts[{index}] 必须是 JSON 对象")
            missing = _MULTI_ENTRY_REQUIRED - entry.keys()
            if missing:
                raise StrategyConfigError(f"contracts[{index}] 缺少字段: " + ", ".join(sorted(missing)))
            unknown_keys = set(entry) - _MULTI_ENTRY_REQUIRED
            if unknown_keys:
                raise StrategyConfigError(f"contracts[{index}] 未知字段: " + ", ".join(sorted(unknown_keys)))
            try:
                config = StrategyConfig.from_mapping({"version": 2, **common, **entry})
            except StrategyConfigError as exc:
                raise StrategyConfigError(f"contracts[{index}]: {exc}") from exc
            key = (config.effective["symbol"], config.effective["exchange"])
            if key in seen:
                raise StrategyConfigError(f"contracts[{index}]: 重复合约: {key[0]}@{key[1]}")
            seen.add(key)
            per_contract.append(config)

        effective = {
            "version": 2,
            **common,
            "contracts": [
                {
                    "symbol": config.effective["symbol"],
                    "exchange": config.effective["exchange"],
                    "target_lots": config.effective["target_lots"],
                    "max_tick_age_seconds": config.effective["max_tick_age_seconds"],
                    "quote_windows": config.effective["quote_windows"],
                }
                for config in per_contract
            ],
        }
        canonical_json, sha256 = _identity(effective)
        # 每份逐合约配置的身份字段刻意替换为整份配置的运行级身份,
        # 其 canonical_json 不再是自身 effective 的序列化。
        contracts = tuple(
            replace(config, canonical_json=canonical_json, sha256=sha256) for config in per_contract
        )
        return cls(
            effective=effective,
            canonical_json=canonical_json,
            sha256=sha256,
            contracts=contracts,
        )

    @classmethod
    def from_json_file(cls, path: str | Path) -> "MultiContractConfig":
        return cls.from_mapping(_load_mapping(path))
