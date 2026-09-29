#!/usr/bin/env python3
"""Connect to a CTP environment and print account/market events.

This first slice is deliberately read-only: it never sends or cancels orders.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Any


GATEWAY_NAME = "CTP"
SETTING_ENV_BY_PROFILE = {
    "first": {
        "user_id": "CTP_USER_ID",
        "password": "CTP_PASSWORD",
        "broker_id": "CTP_BROKER_ID",
        "trade_front": "CTP_TRADE_FRONT",
        "market_front": "CTP_MARKET_FRONT",
        "app_id": "CTP_APP_ID",
        "auth_code": "CTP_AUTH_CODE",
        "product_info": "CTP_PRODUCT_INFO",
    },
    "7x24": {
        "user_id": "CTP_USER_ID",
        "password": "CTP_PASSWORD",
        "broker_id": "CTP_BROKER_ID",
        "trade_front": "CTP_7X24_TRADE_FRONT",
        "market_front": "CTP_7X24_MARKET_FRONT",
        "app_id": "CTP_APP_ID",
        "auth_code": "CTP_AUTH_CODE",
        "product_info": "CTP_PRODUCT_INFO",
    },
    "guangfa": {
        "user_id": "CTP_GUANGFA_USER_ID",
        "password": "CTP_GUANGFA_PASSWORD",
        "broker_id": "CTP_GUANGFA_BROKER_ID",
        "trade_front": "CTP_GUANGFA_TRADE_FRONT",
        "market_front": "CTP_GUANGFA_MARKET_FRONT",
        "app_id": "CTP_GUANGFA_APP_ID",
        "auth_code": "CTP_GUANGFA_AUTH_CODE",
        "product_info": "CTP_GUANGFA_PRODUCT_INFO",
    },
}
_DIAGNOSTIC_MARKERS = {
    "交易服务器连接成功": "td_front_connected",
    "交易服务器连接断开": "td_front_disconnected",
    "交易服务器授权验证成功": "td_authenticated",
    "交易服务器授权验证失败": "td_authentication_failed",
    "交易服务器登录成功": "td_logged_in",
    "交易服务器登录失败": "td_login_failed",
    "行情服务器连接成功": "md_front_connected",
    "行情服务器连接断开": "md_front_disconnected",
    "行情服务器登录成功": "md_logged_in",
    "行情服务器登录失败": "md_login_failed",
    "合约信息查询成功": "contracts_queried",
}


@dataclass(frozen=True)
class Settings:
    environment: str
    user_id: str
    password: str
    broker_id: str
    trade_front: str
    market_front: str
    app_id: str
    auth_code: str
    product_info: str
    symbol: str
    exchange: str

    def gateway_setting(self) -> dict[str, str]:
        return {
            "用户名": self.user_id,
            "密码": self.password,
            "经纪商代码": self.broker_id,
            "交易服务器": self.trade_front,
            "行情服务器": self.market_front,
            "产品名称": self.app_id,
            "授权编码": self.auth_code,
            "产品信息": self.product_info,
        }

    @property
    def vt_symbol(self) -> str:
        return f"{self.symbol}.{self.exchange}" if self.symbol else ""


def load_settings(environment: str = "first") -> Settings:
    try:
        names = SETTING_ENV_BY_PROFILE[environment]
    except KeyError as exc:
        raise ValueError(f"不支持的 CTP 环境: {environment}") from exc

    missing = [
        names[key]
        for key in ("user_id", "password", "broker_id", "trade_front", "market_front", "app_id", "auth_code")
        if not os.environ.get(names[key], "").strip()
    ]
    if missing:
        raise ValueError("缺少环境变量: " + ", ".join(missing))

    return Settings(
        environment=environment,
        user_id=os.environ[names["user_id"]].strip(),
        password=os.environ[names["password"]],
        broker_id=os.environ[names["broker_id"]].strip(),
        trade_front=os.environ[names["trade_front"]].strip(),
        market_front=os.environ[names["market_front"]].strip(),
        app_id=os.environ[names["app_id"]].strip(),
        auth_code=os.environ[names["auth_code"]].strip(),
        product_info=os.getenv(names["product_info"], "").strip(),
        symbol=os.getenv("CTP_SYMBOL", "").strip(),
        exchange=os.getenv("CTP_EXCHANGE", "SHFE").strip().upper(),
    )


def _diagnostic_snapshot(diagnostics: dict[str, bool]) -> str:
    def mark(name: str) -> str:
        return "Y" if diagnostics[name] else "N"

    def optional_mark(name: str) -> str:
        return "Y" if diagnostics.get(name, False) else "N"

    return (
        f"TD(front={mark('td_front_connected')},auth={mark('td_authenticated')},"
        f"login={mark('td_logged_in')}) "
        f"MD(front={mark('md_front_connected')},login={mark('md_logged_in')}) "
        f"contracts={mark('contracts_queried')} "
        f"target(contract={optional_mark('target_contract_seen')},"
        f"tick={optional_mark('target_tick_seen')})"
    )


def install_handlers(event_engine: Any, main_engine: Any, settings: Settings) -> dict[str, bool]:
    """Print the events needed for the first connection acceptance check."""

    from vnpy.trader.event import (
        EVENT_ACCOUNT,
        EVENT_CONTRACT,
        EVENT_LOG,
        EVENT_POSITION,
        EVENT_TICK,
    )

    subscribed = False
    last_tick_printed = 0.0
    diagnostics = {
        **{name: False for name in _DIAGNOSTIC_MARKERS.values()},
        "target_contract_seen": False,
        "target_tick_seen": False,
    }

    def on_log(event: Any) -> None:
        message = str(getattr(event.data, "msg", event.data))
        print(f"[日志] {message}", flush=True)
        for marker, name in _DIAGNOSTIC_MARKERS.items():
            if marker in message:
                diagnostics[name] = True
                print(f"[诊断] {name}: {message}", flush=True)
                break

    def on_account(event: Any) -> None:
        account = event.data
        print(
            "[资金] "
            f"账户={account.accountid} balance={account.balance} "
            f"available={account.available} frozen={account.frozen}",
            flush=True,
        )

    def on_position(event: Any) -> None:
        position = event.data
        if settings.symbol and position.symbol != settings.symbol:
            return
        print(
            "[持仓] "
            f"{position.vt_positionid} volume={position.volume} "
            f"yd={position.yd_volume} price={position.price} pnl={position.pnl}",
            flush=True,
        )

    def on_contract(event: Any) -> None:
        nonlocal subscribed
        contract = event.data
        if not settings.symbol:
            return
        exchange = getattr(contract.exchange, "value", str(contract.exchange))
        if contract.symbol != settings.symbol or exchange != settings.exchange:
            return

        diagnostics["target_contract_seen"] = True
        print(f"[诊断] target_contract_seen: {contract.vt_symbol}", flush=True)
        print(
            "[合约] "
            f"{contract.vt_symbol} name={contract.name} size={contract.size} "
            f"pricetick={contract.pricetick}",
            flush=True,
        )
        if not subscribed:
            from vnpy.trader.constant import Exchange
            from vnpy.trader.object import SubscribeRequest

            main_engine.subscribe(
                SubscribeRequest(
                    symbol=settings.symbol,
                    exchange=Exchange(settings.exchange),
                ),
                GATEWAY_NAME,
            )
            subscribed = True
            print(f"[行情] 已订阅 {settings.vt_symbol}", flush=True)

    def on_tick(event: Any) -> None:
        nonlocal last_tick_printed
        tick = event.data
        if settings.vt_symbol and tick.vt_symbol != settings.vt_symbol:
            return
        if settings.vt_symbol and not diagnostics["target_tick_seen"]:
            diagnostics["target_tick_seen"] = True
            print(f"[诊断] target_tick_seen: {tick.vt_symbol}", flush=True)
        now = time.monotonic()
        if now - last_tick_printed < 1:
            return
        last_tick_printed = now
        print(
            "[Tick] "
            f"{tick.vt_symbol} last={tick.last_price} "
            f"bid1={tick.bid_price_1}/{tick.bid_volume_1} "
            f"ask1={tick.ask_price_1}/{tick.ask_volume_1} "
            f"time={tick.datetime}",
            flush=True,
        )

    for event_type, handler in (
        (EVENT_LOG, on_log),
        (EVENT_ACCOUNT, on_account),
        (EVENT_POSITION, on_position),
        (EVENT_CONTRACT, on_contract),
        (EVENT_TICK, on_tick),
    ):
        event_engine.register(event_type, handler)
    return diagnostics


def connect(settings: Settings) -> int:
    try:
        from live_grid.ctp_native import activate_ctp_native_libs

        native_variant = activate_ctp_native_libs(settings.environment)
    except (FileNotFoundError, ImportError, OSError, RuntimeError, ValueError) as exc:
        print(f"CTP 原生库选择或加载失败: {exc}", file=sys.stderr)
        return 3

    try:
        from vnpy.event import EventEngine
        from vnpy.trader.constant import Exchange
        from vnpy.trader.engine import MainEngine
        from vnpy_ctp import CtpGateway
    except (ImportError, ModuleNotFoundError, OSError) as exc:
        print(
            "依赖未安装或 CTP 原生库加载失败。请先按 README 安装 vnpy 和 vnpy_ctp。\n"
            f"原始错误: {exc}",
            file=sys.stderr,
        )
        return 3

    if settings.symbol:
        try:
            Exchange(settings.exchange)
        except ValueError:
            print(f"配置错误: CTP_EXCHANGE 不是有效交易所: {settings.exchange}", file=sys.stderr)
            return 2

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_gateway(CtpGateway)
    diagnostics = install_handlers(event_engine, main_engine, settings)

    print(
        f"[连接] environment={settings.environment} ctp_native={native_variant} "
        f"broker={settings.broker_id} user={settings.user_id} "
        f"trade={settings.trade_front} market={settings.market_front}",
        flush=True,
    )
    main_engine.connect(settings.gateway_setting(), GATEWAY_NAME)
    print("[连接] 已发起连接；等待认证、登录、合约、资金和持仓事件。", flush=True)

    started_at = time.monotonic()
    last_checkpoint = 0
    try:
        while True:
            time.sleep(1)
            elapsed = int(time.monotonic() - started_at)
            if elapsed >= 30 and elapsed // 30 > last_checkpoint:
                last_checkpoint = elapsed // 30
                print(
                    f"[诊断] 等待 {last_checkpoint * 30}s: {_diagnostic_snapshot(diagnostics)}",
                    flush=True,
                )
    except KeyboardInterrupt:
        print(f"[诊断] 退出前最终状态: {_diagnostic_snapshot(diagnostics)}", flush=True)
        print("\n[退出] 正在关闭 CTP 连接。", flush=True)
    finally:
        main_engine.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CTP read-only connection check")
    parser.add_argument("--check", action="store_true", help="只校验配置，不连接")
    parser.add_argument("--env", choices=SETTING_ENV_BY_PROFILE, default="first", help="CTP 连接环境")
    args = parser.parse_args()

    try:
        settings = load_settings(args.env)
    except ValueError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    if args.check:
        target = settings.vt_symbol or "未设置（只测试登录/账户/持仓）"
        print(
            f"配置有效: environment={settings.environment} broker={settings.broker_id} user={settings.user_id} "
            f"symbol={target}"
        )
        return 0

    return connect(settings)


if __name__ == "__main__":
    raise SystemExit(main())
