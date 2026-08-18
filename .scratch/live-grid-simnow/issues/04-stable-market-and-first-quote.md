# 04 — 稳定行情门槛与首轮 W/D/S 报价

**What to build:** 目标行情快照连续两秒有效、盘口顺序正常且通过严格盘口保护后，系统依据 CTP `pricetick` 和最新有效 LastPrice 计算 tick 对齐的 W/D/S 价格，并提交一对被动买卖开仓报价。

**Blocked by:** 03 — 合约元数据与关联零仓启动门槛

**Status:** ready-for-agent

- [ ] 缺少有效 bid、ask 或 last、价格非有限或非正、盘口倒挂时，不提交首轮报价。
- [ ] 盘口保护严格满足 `W + D > 2 × bid-ask spread in ticks` 后，行情才可进入稳定计时。
- [ ] 有效且受保护的行情必须连续稳定两秒，单个瞬时快照不能触发开仓。
- [ ] 买卖报价均来自最新有效 LastPrice、W/D/S 和 CTP 价格跳动，并全部按 `pricetick` 对齐。
- [ ] 首轮只提交目标合约的一对被动开仓报价，行情穿过限价但没有 CTP 成交回报时不改变成交状态。

