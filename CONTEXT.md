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

**单次成交收口**:
The mandatory sequence after the first confirmed fill in single-contract testing: cancel every remaining quote, reconcile CTP order and position callbacks, flatten the net position, then stop the run.
_Avoid_: 持续报价、人工兜底持仓

**报撤测试入口**:
A dedicated command for the single-contract SimNow test. It can submit orders only when started with an explicit SimNow confirmation flag; the ordinary connection command remains read-only.
_Avoid_: 在只读入口中隐藏下单模式、无确认启动

**受限 FAK 收口**:
The selected closing method for the single-contract SimNow test: after order reconciliation, submit an executable FAK close and reprice for at most three seconds and ten adverse ticks. Stop with the remaining position explicit if it cannot complete.
_Avoid_: 无限追价、静默忽略未平仓位、市价收口

**策略配置**:
A versionable JSON document containing the grid and safety parameters for one test run, excluding CTP credentials. The effective configuration is retained with that run's audit log.
_Avoid_: 凭证文件、硬编码策略参数

**人工结束收口**:
The termination path for a no-time-limit test: on an operator interrupt, cancel every test order, wait for terminal order callbacks, reconcile the position, and use the same bounded FAK close if a fill occurred.
_Avoid_: 直接退出、遗留活动委托

**零仓启动门槛**:
The test may submit its first quote only after CTP position data confirms the target contract has zero net position. Any nonzero position rejects the run without sending an order.
_Avoid_: 管理既有仓位、带仓启动

**首轮报撤限额**:
The maximum number of quote submissions and cancellation requests in a rolling minute for the first SimNow test: 60. Reaching it pauses quoting and never bypasses a necessary safety cancellation.
_Avoid_: 无上限报撤、把风险撤单计入静默失败

**合约元数据门槛**:
The target contract's CTP metadata, including its positive price tick, must be received before the test may submit its first quote.
_Avoid_: 写死最小变动价位、缺少合约元数据仍下单

**策略目标合约**:
The symbol and exchange named exclusively in the strategy configuration and used by the order-management test. CTP credentials and the read-only connector's subscription settings do not select it.
_Avoid_: 从环境变量隐式继承下单合约、只读订阅合约

**盘口保护**:
The first test may quote only when valid bid, ask, and last prices exist and `W + D` is strictly greater than twice the observed bid-ask spread in ticks. An invalid or too-wide book cancels test quotes and pauses quoting.
_Avoid_: 薄盘口继续挂单、忽略无效行情

**首次成交触发**:
The first CTP-reported partial or complete fill for either target quote. It immediately starts the single-fill closing sequence; remaining quantity and the opposite quote cannot continue trading.
_Avoid_: 等待全额成交、保留另一侧报价

**撤单终态上限**:
The test waits at most ten seconds after entering the closing sequence for every test order to reach a terminal CTP status. On timeout it queries the target position, closes any net position with bounded FAK, and ends as a failure.
_Avoid_: 无限等待撤单回报、超时后恢复报价

**当日平仓偏移**:
The close offset chosen automatically for a position opened by this test today: close-today for SHFE and INE, ordinary close for other supported exchanges. A rejection ends the test rather than trying another offset.
_Avoid_: 手工填写偏移、拒单后猜测性重试

**目标委托手数**:
The positive per-side quantity in the strategy configuration. No separate absolute cap applies; its actual value is highlighted before submission and recorded for the run, and any first partial fill still triggers closing.
_Avoid_: 隐式固定手数、成交后继续加仓、未展示的配置风险

**测试审计目录**:
The per-run directory containing the effective credential-free strategy configuration, CTP market/order/trade events, and a final safety summary of position, active orders, and failures.
_Avoid_: 仅终端输出、无法还原的测试结果

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

**持仓查询关联号**:
The gateway's increasing CTP request identifier returned with each position-query completion event. A safety gate accepts only the result for the query it initiated.
_Avoid_: 最近一次结果、后台轮询结果猜测性复用
