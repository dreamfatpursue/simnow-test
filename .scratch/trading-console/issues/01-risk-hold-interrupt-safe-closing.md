# 01 — 让 RISK_HOLD 中断进入不可逆安全收口

**What to build:** 当操作员在 `RISK_HOLD` 期间发起人工结束收口时，合约会记录不可撤回的停止意图，保持 CTP 连接并继续撤单和持仓对账；风险解除后只会安全结束，不会恢复报价。

**Blocked by:** None — can start immediately.

**Status:** done

- [x] `RISK_HOLD` 收到中断后，摘要与结构化审计都保留 `stop_reason=interrupted`，且收口所需的连接与对账不会被中断。
- [x] 当回报确认零活动委托和零净仓时，会话进入 `FINISHED`，并且后续行情不会令其回到可报价状态。
- [x] 离线状态机回归测试覆盖风险托管中断、风险恢复、零仓确认和不得续挂的完整路径。
