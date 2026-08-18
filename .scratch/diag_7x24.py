#!/usr/bin/env python3
"""Read-only 7x24 diagnostic: contract list contents and AP610 tick flow."""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vendor", "vnpy_ctp"))

from run import GATEWAY_NAME, load_settings  # noqa: E402


def main() -> int:
    settings = load_settings("7x24")

    from vnpy.event import EventEngine
    from vnpy.trader.constant import Exchange
    from vnpy.trader.event import EVENT_CONTRACT, EVENT_LOG, EVENT_TICK
    from vnpy.trader.engine import MainEngine
    from vnpy.trader.object import SubscribeRequest
    from vnpy_ctp import CtpGateway

    contracts: dict[str, list[str]] = {}
    logs: list[str] = []
    ticks: list[str] = []
    subscribed = False

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_gateway(CtpGateway, GATEWAY_NAME)

    def on_contract(event):
        nonlocal subscribed
        contract = event.data
        if contract.symbol.startswith("AP"):
            contracts.setdefault(contract.symbol, []).append(contract.exchange.value)
        if contract.symbol == "AP610" and not subscribed:
            subscribed = True
            main_engine.subscribe(
                SubscribeRequest(symbol="AP610", exchange=Exchange("CZCE")), GATEWAY_NAME
            )
            print(f"[订阅] AP610 已订阅 (pricetick={contract.pricetick}, size={contract.size})", flush=True)

    def on_log(event):
        msg = str(getattr(event.data, "msg", event.data))
        logs.append(msg)

    def on_tick(event):
        tick = event.data
        if tick.symbol == "AP610":
            ticks.append(
                f"last={tick.last_price} bid1={tick.bid_price_1}/{tick.bid_volume_1} "
                f"ask1={tick.ask_price_1}/{tick.ask_volume_1} time={tick.datetime}"
            )

    event_engine.register(EVENT_CONTRACT, on_contract)
    event_engine.register(EVENT_LOG, on_log)
    event_engine.register(EVENT_TICK, on_tick)

    print(f"[连接] 7x24 trade={settings.trade_front} market={settings.market_front}", flush=True)
    main_engine.connect(settings.gateway_setting(), GATEWAY_NAME)

    for _ in range(45):
        time.sleep(1)
    main_engine.close()

    print("\n=== 关键日志 ===")
    for msg in logs:
        if any(k in msg for k in ("登录", "授权", "连接", "查询", "失败", "错误")):
            print(f"  {msg}")
    print("\n=== AP 系合约（7x24 返回） ===")
    for symbol in sorted(contracts):
        print(f"  {symbol}@{','.join(contracts[symbol])}")
    print(f"\nAP610 存在: {'AP610' in contracts}")
    print(f"AP610 Tick 数量: {len(ticks)}")
    for line in ticks[-5:]:
        print(f"  [Tick] {line}")
    if not ticks:
        print("  （45 秒内未收到任何 AP610 行情）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
