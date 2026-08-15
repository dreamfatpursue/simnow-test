# SimNow CTP Replay

This context records the shared language for applying a grid quoting algorithm to SimNow CTP orders while keeping historical replay results separate from live order state.

## Language

**SimNow 行情快照**:
A timestamped top-of-book and last-price observation received from SimNow for one contract. It drives live quoting decisions but does not prove an order was filled.
_Avoid_: 交易所成交回报

**回放合约组**:
The target, hedge, and fair-reference contracts captured together for one replay run.
_Avoid_: 单合约行情、任意合约集合

**模拟成交**:
A replay conclusion derived from observable snapshots and configured assumptions; it is not a CTP order acknowledgement or an exchange fill.
_Avoid_: 真实成交、成交回报

**实盘状态机**:
The order-management state machine that submits, cancels, replaces, hedges, and exits SimNow orders using CTP order and trade callbacks as the source of truth.
_Avoid_: 回放成交假设、仅凭行情判定成交

**受限完整链路**:
One target contract and one hedge contract traded at the minimum allowed size, with hard limits on order actions, open exposure, and session duration. A confirmed target fill triggers automatic hedge and exit.
_Avoid_: 仅报撤测试、无上限自动交易

**单合约报撤联调**:
A first SimNow test phase that submits, cancels, replaces, and observes fills for one contract only. It deliberately does not submit an automatic hedge order.
_Avoid_: 受限完整链路、最终策略运行

**单轮成交收口**:
The mandatory sequence after any confirmed fill within a round: cancel every remaining quote, reconcile CTP order and position callbacks, flatten the net position, then either resume quoting for the next round or stop per the stop conditions. A closing or flatten failure fails the session; it never resumes quoting with an open position.
_Avoid_: 持续持仓、带仓重挂、跨轮合并对账

**收盘停止**:
The configured local-clock time (`session_end_time`, HH:MM) at which each contract's session runs the same closing sequence as an operator interrupt and ends normally. The deadline is the next occurrence of that time within 24 hours, so cross-midnight night-session ends (e.g. 01:00) are supported.
_Avoid_: 无限挂单、依赖交易所日历、按当日零点截断

**往返轮数上限**:
The maximum completed fill-and-flatten rounds per session (`max_round_trips`, default 10). Reaching it ends the session normally after the current round completes; it does not interrupt an in-flight closing.
_Avoid_: 无上限轮次、中途打断收口

**迟到成交触发**:
A fill reported for an order that was already cancel-requested, arriving while the session is between rounds or re-quoting. It must immediately start a new closing sequence for that contract; it is never ignored as stale.
_Avoid_: 忽略撤单竞速失败的成交、静默记账不平仓

**报撤测试入口**:
A dedicated command for the single-contract SimNow test. It can submit orders only when started with an explicit SimNow confirmation flag; the ordinary connection command remains read-only.
_Avoid_: 在只读入口中隐藏下单模式、无确认启动

**受限 FAK 收口**:
The selected closing method for the single-contract SimNow test: after order reconciliation, submit an executable FAK close and reprice for at most three seconds and ten adverse ticks. Stop with the remaining position explicit if it cannot complete.
_Avoid_: 无限追价、静默忽略未平仓位、市价收口

**策略配置**:
A versionable JSON document containing the grid and safety parameters shared by every target contract, plus one entry per contract with its symbol, exchange, and per-side lots. It excludes CTP credentials. The effective configuration is retained with that run's audit log.
_Avoid_: 凭证文件、硬编码策略参数、每合约一份参数文件

**多合约运行**:
One SimNow test run quoting several target contracts concurrently, each driven by an independent single-contract session. The run ends only after every contract's session reaches a terminal state.
_Avoid_: 共享网格状态、跨合约对冲、任一合约终态即结束

**合约独立收口**:
The rule that a contract's first-fill closing, failure, or timeout stops and flattens only that contract's session. An operator interrupt is broadcast to every session, which each run their own closing sequence.
_Avoid_: 任一成交全停、跨合约收口链路

**人工结束收口**:
The termination path for a no-time-limit test: on an operator interrupt, cancel every test order, wait for terminal order callbacks, reconcile the position, and use the same bounded FAK close if a fill occurred.
_Avoid_: 直接退出、遗留活动委托

**零仓启动门槛**:
The test may submit its first quote only after CTP position data confirms the target contract has zero net position. Any nonzero position on any target contract rejects the whole run without sending an order.
_Avoid_: 管理既有仓位、带仓启动、剔除持仓合约后部分启动

**首轮报撤限额**:
The maximum number of quote submissions and cancellation requests in a rolling minute for one contract's session: 60, counted per contract independently. Reaching it pauses that session's quoting and never bypasses a necessary safety cancellation.
_Avoid_: 无上限报撤、把风险撤单计入静默失败、跨合约共享限额池

**合约元数据门槛**:
The target contract's CTP metadata, including its positive price tick, must be received before the test may submit its first quote.
_Avoid_: 写死最小变动价位、缺少合约元数据仍下单

**策略目标合约**:
A symbol and exchange named as one entry in the strategy configuration's contract list and used by that contract's order-management session. CTP credentials and the read-only connector's subscription settings do not select it.
_Avoid_: 从环境变量隐式继承下单合约、只读订阅合约

**盘口保护**:
The first test may quote only when valid bid, ask, and last prices exist and `W + D` is strictly greater than twice the observed bid-ask spread in ticks. An invalid or too-wide book cancels test quotes and pauses quoting.
_Avoid_: 薄盘口继续挂单、忽略无效行情

**轮内成交触发**:
The first CTP-reported partial or complete fill for either quote of the current round. It immediately starts that round's closing sequence; remaining quantity and the opposite quote cannot continue trading.
_Avoid_: 等待全额成交、保留另一侧报价

**撤单终态上限**:
The test waits at most ten seconds after entering the closing sequence for every test order to reach a terminal CTP status. On timeout it queries the target position, closes any net position with bounded FAK, and ends as a failure.
_Avoid_: 无限等待撤单回报、超时后恢复报价

**当日平仓偏移**:
The close offset chosen automatically for a position opened by this test today: close-today for SHFE and INE, ordinary close for other supported exchanges. A rejection ends the test rather than trying another offset.
_Avoid_: 手工填写偏移、拒单后猜测性重试

**目标委托手数**:
The positive per-side quantity in the strategy configuration's contract entry. No separate absolute cap applies; its actual value is highlighted before submission and recorded for the run, and any first partial fill still triggers closing.
_Avoid_: 隐式固定手数、成交后继续加仓、未展示的配置风险

**测试审计目录**:
The per-run directory holding one subdirectory per contract with its credential-free effective configuration, CTP market/order/trade events, and final safety summary, plus a run-level summary of every contract's position, active orders, and failures.
_Avoid_: 仅终端输出、无法还原的测试结果、多合约事件混写单一文件

**策略哈希身份**:
The SHA-256 identity of the effective strategy configuration, used for preview, audit, and replay identification. It is not a submission gate; order-capable entry requires only explicit SimNow confirmation.
_Avoid_: 把哈希当作下单门禁、只记录终端输出不保留策略身份

**稳定行情启动**:
After the SimNow launch confirmation, the test submits its first quote only after the target's bid, ask, and last price remain valid and pass book protection for two consecutive seconds.
_Avoid_: 启动即挂单、单个瞬时行情触发

**持仓查询完成事件**:
The gateway's general event containing the completed query's summarized positions, including an empty result. It is the authoritative zero-position signal required before the test can submit its first quote.
_Avoid_: 未收到持仓事件即视为零仓、超时猜测零仓

**项目内 CTP 依赖**:
The checked-in editable `vnpy_ctp` source used by this project at runtime. Gateway safety patches are versioned with the live-grid test rather than applied to an unmanaged virtual-environment copy.
_Avoid_: 临时修改 site-packages、被忽略的未加载源码副本

**交易日成交明细**:
A per-trading-day HTML report where each record is one round's filled order lifecycle—its submission, its fill, and the flatten that closed it. The trading day follows the exchange trading-day calendar (CTP `TradeDate`), so night-session trades roll into the next trading day. Cancelled quote attempts of the same round are not part of the record; it is a view of audit facts, never a second live data path.
_Avoid_: 策略运行时产出报表、在线合并当日文件、逐笔委托流水、按本地自然日切分

**资金差净盈亏**:
The real net profit or loss for one run or trading day, derived from the account balance sampled before the first order submission and after the last flatten reaches terminal state. The gap between it and the sum of gross round PnL is the implied commission.
_Avoid_: 从成交回报取手续费、逐轮资金差归因

**持仓查询关联号**:
The gateway's increasing CTP request identifier returned with each position-query completion event. A safety gate accepts only the result for the query it initiated.
_Avoid_: 最近一次结果、后台轮询结果猜测性复用
