#!/usr/bin/env python3
"""Read-only first-env probe: list active-month metal contracts and check book spread."""

from __future__ import annotations

import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "vendor", "vnpy_ctp"))

from run import GATEWAY_NAME, load_settings  # noqa: E402

CANDIDATE_PREFIXES = ("cu", "al", "zn", "pb", "au", "ag")
SUBSCRIBE_SYMBOL = sys.argv[1] if len(sys.argv) > 1 else ""


def main() -> int:
    settings = load_settings("first")

    from vnpy.event import EventEngine
    from vnpy.trader.constant import Exchange
    from vnpy.trader.event import EVENT_CONTRACT, EVENT_LOG, EVENT_TICK
    from vnpy.trader.engine import MainEngine
    from vnpy.trader.object import SubscribeRequest
    from vnpy_ctp import CtpGateway

    contracts: dict[str, tuple[str, float, float]] = {}
    logs: list[str] = []
    ticks: list = []
    subscribed = False
    pricetick = None

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_gateway(CtpGateway, GATEWAY_NAME)

    def on_contract(event):
        nonlocal subscribed, pricetick
        c = event.data
        if c.symbol[:2] in CANDIDATE_PREFIXES and c.symbol[2:].isdigit():
            contracts[c.symbol] = (c.exchange.value, c.pricetick, c.size)
        if SUBSCRIBE_SYMBOL and c.symbol == SUBSCRIBE_SYMBOL and not subscribed:
            subscribed = True
            pricetick = c.pricetick
            main_engine.subscribe(
                SubscribeRequest(symbol=SUBSCRIBE_SYMBOL, exchange=Exchange(c.exchange.value)),
                GATEWAY_NAME,
            )
            print(f"[订阅] {SUBSCRIBE_SYMBOL} pricetick={c.pricetick} size={c.size}", flush=True)

    def on_log(event):
        logs.append(str(getattr(event.data, "msg", event.data)))

    def on_tick(event):
        t = event.data
        if t.symbol == SUBSCRIBE_SYMBOL:
            ticks.append(t)

    event_engine.register(EVENT_CONTRACT, on_contract)
    event_engine.register(EVENT_LOG, on_log)
    event_engine.register(EVENT_TICK, on_tick)

    print(f"[连接] first trade={settings.trade_front}", flush=True)
    main_engine.connect(settings.gateway_setting(), GATEWAY_NAME)
    for _ in range(35):
        time.sleep(1)
    main_engine.close()

    print("\n=== 关键日志 ===")
    for msg in logs:
        if any(k in msg for k in ("登录", "授权", "连接", "查询", "失败", "错误")):
            print(f"  {msg}")

    print("\n=== 候选品种近月合约 ===")
    by_prefix: dict[str, list[str]] = {}
    for symbol, (exch, pt, size) in contracts.items():
        by_prefix.setdefault(symbol[:2], []).append(symbol)
    for prefix in sorted(by_prefix):
        months = sorted(by_prefix[prefix])
        print(f"  {prefix}: {', '.join(months[:6])}")

    if SUBSCRIBE_SYMBOL:
        spreads = Counter(
            round((t.ask_price_1 - t.bid_price_1) / (pricetick or 1), 1) for t in ticks
        )
        print(f"\n{SUBSCRIBE_SYMBOL} 收到 {len(ticks)} 个 Tick, 价差分布(tick): {dict(spreads)}")
        if ticks:
            last = ticks[-1]
            print(
                f"最新: last={last.last_price} bid={last.bid_price_1}/{last.bid_volume_1} "
                f"ask={last.ask_price_1}/{last.ask_volume_1} time={last.datetime}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
