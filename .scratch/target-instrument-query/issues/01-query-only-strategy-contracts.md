# 01 — 报撤入口只查询策略目标合约

**What to build:** SimNow 全市场 `ReqQryInstrument({})` 会把期权列表推数分钟，占住 CTP 唯一查询通道，导致启动查委托/持仓发不出去。报撤入口改为只查策略 JSON 中的目标合约；只读 `run.py` 仍查全市场。

**Blocked by:** None

**Status:** ready-for-agent

- [x] 网关在设置了 `查询合约` 时按 `InstrumentID+ExchangeID` 逐个查询，全部完成后才标记 `contract_inited`。
- [x] 未设置时保持空过滤的全市场查询。
- [x] 适配层把会话目标写成 `查询合约` 交给网关。
- [x] 过滤查询后，未知合约的委托/成交回报跳过，避免 KeyError。
