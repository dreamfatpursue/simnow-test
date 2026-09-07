# SimNow CTP Replay

This context records the shared language for applying a grid quoting algorithm to SimNow CTP orders while keeping historical replay results separate from live order state.

## Language

**仿真连接环境**:
An explicitly selected service boundary for one run: `first` is the regular SimNow service, `7x24` is the SimNow API-test service without settlement, and `guangfa` is the separate Guangfa simulation service. Selecting one does not transfer state or imply automatic switching; the trading console never exposes a production environment.
_Avoid_: 前置地址本身、自动故障切换、环境间共享状态、生产交易环境

**历史行情联调许可**:
An explicit, per-run opt-in available only for the `7x24` environment that permits replayed exchange timestamps to pass the market-time gate while preserving the normal order, position, closing, and audit safeguards. It is off by default and remains visibly distinct from normal market-data mode throughout preview and execution.
_Avoid_: 自动开启、普通实时行情模式、跨运行沿用许可、放宽订单与持仓安全边界

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

**价差窗口收口**:
The closing sequence after the first fill in a round: the opposite quote rests for the configured window (`closing_wait_seconds`, default 1; 0 means no window). If it fills within the window, the round ends with the spread collected and no flatten; otherwise the net position is flattened with the bounded FAK close and the opposite quote is cancelled after the flatten reaches terminal state. Both branches end only after every order is terminal and the reconciled net position is zero; a closing failure fails the session and never resumes quoting with an open position.
_Avoid_: 立即撤单收口、平仓前撤对侧、跳过对账查仓、带遗留委托或仓位结束一轮

**交易时段窗口**:
Each contract must provide an ordered `quote_windows` list of local `start`/`end` times. Five seconds before every window end, the session safely cancels all active opening quotes; between windows it stays paused, and after the final window's closing sequence it ends normally. A descending clock time denotes a cross-midnight next-day window.
_Avoid_: 午休或夜盘切换遗留挂单、只配置单一全局收盘时刻、按行情恢复后才撤旧单

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
A credential-free document containing one entry per target contract with its identity, quantity, grid, timing, and safety parameters. Each contract owns its values independently; omitting an optional value uses that parameter's default, never another contract's setting.
_Avoid_: 凭证文件、硬编码策略参数、每合约一份参数文件、全局策略参数覆盖

**策略文件预览**:
A normalized view of one existing credential-free strategy file before launch. While no trading run is active, the console may edit that file's parameters, validate the complete result, atomically save it back to the same source file, and create a new preview; contract identity, credentials, file creation, rename, and save-as remain unavailable. Confirmation applies only while the selected file's effective hash, environment, and market-data mode remain unchanged.
_Avoid_: 运行草稿、未校验的局部保存、另存为、修改文件后沿用旧预览、运行中热更新

**多合约运行**:
One SimNow test run quoting one or more target contracts, each driven by an independently configured session with its own state, limits, and completed-round count. The run ends only after every contract's session reaches a terminal state.
_Avoid_: 共享网格状态、跨合约对冲、任一合约终态即结束

**交易控制台**:
The operator-facing workspace for selecting a strategy, starting a simulation run, observing its live state, and deliberately requesting whole-run safe termination. It does not provide discretionary orders, individual-order cancellation, single-contract intervention, production-environment selection, or credential editing. A control request is not proof that an order was cancelled, a position was closed, or the run reached a terminal state; those outcomes still require authoritative CTP callbacks and reconciliation.
_Avoid_: 只读运行监控面板、手工下单终端、单合约干预、把按钮受理当作 CTP 执行成功

**交易启动确认**:
A per-run operator approval granted only after reviewing the selected simulation environment and the complete effective strategy, including its hash, contracts, quantities, quote windows, and stopping limits. It authorizes that exact preview once and has no clock-based expiry; configuration changes, use, or control-service restart invalidate it.
_Avoid_: 单击直接启动、跨运行持续解锁、修改配置后沿用旧确认、无变化也按时间过期

**活动运行**:
The single multi-contract run whose trading process currently holds the activity lock. A stop request, one contract reaching a terminal state, or loss of the operator view does not make the run inactive; process exit releases the lock and ends its active status even when no complete safety summary exists.
_Avoid_: 已请求停止的运行自动结束、任一合约结束即释放新运行、操作界面关闭即运行结束、进程退出后仍视为活动运行

**单活动运行准入**:
The rule that a confirmed start is rejected only while another trading process is still active. When one exists, the console returns that run and switches to its live view; after it exits, a new confirmed run may start even if the previous run lacked a complete safety summary. The first version does not preserve a post-exit verification gate or prove account continuity across runs.
_Avoid_: 同时启动两个交易进程、用过期 PID 判断运行中、把上一运行摘要作为下一运行门槛

**安全失败终态**:
The exact contract-session state `FAILED`, reached only after the state machine knows there are no active orders and the final net position is zero. It communicates that the requested trading behavior failed but the session is safe to close; it is not an unknown-risk state.
_Avoid_: 进程异常退出、残仓、未知委托、把所有失败都标成风险托管

**进程异常退出**:
The run-level fact that the trading process ended before every contract recorded a safe terminal state. It preserves each contract's last known exact state and risk facts rather than rewriting them as `FAILED`; in the first version it warns the operator but does not block a later confirmed run.
_Avoid_: `SessionState.FAILED`、伪造合约终态、把最后已知事实冒充当前账户事实

**运行态势**:
The live operational view of an active run: connection and market freshness, each contract's session state, quote and order lifecycle, fills, reconciled position, stopping progress, and risk conditions. It deliberately excludes profit and loss; economic results belong to the completed-run report.
_Avoid_: 实时盈亏看板、原始日志滚屏、最终运行报告

**运行阶段**:
The operator-facing Chinese summary of exact session states. A contract keeps its detailed label, while the whole run uses five fixed groups: startup preparation, trading run, safe closing, risk, and terminal. Waiting for a stable market, pausing between quote windows, and waiting for the opposite fill all remain part of the trading run; only cancellation, reconciliation, and flattening are safe closing. The grouping is display-only and never drives trading behavior.
_Avoid_: 第二套状态机、只显示英文枚举、用中文文案猜测归属、把 `CLOSING_WAIT` 当作安全收口

**合约独立收口**:
The rule that a contract's first-fill closing, failure, or timeout stops and flattens only that contract's session. An operator interrupt is broadcast to every session, which each run their own closing sequence.
_Avoid_: 任一成交全停、跨合约收口链路

**人工结束收口**:
The whole-run termination path requested by either a command-line interrupt or the trading console's safe-stop control. It broadcasts to every contract session, cancels every test order, waits for terminal order callbacks, reconciles each position, and uses the same bounded FAK close if a fill occurred; the request is not complete until every session reaches a safe terminal outcome. Its availability depends on identifying the active trading process, not on the freshness of the console's displayed market or audit projection.
_Avoid_: 直接退出、单合约停止、遗留活动委托、把停止按钮受理当作收口完成、因页面数据过期拒绝停止

**零仓启动门槛**:
The test may submit its first quote only after CTP position data confirms the target contract has zero net position. Any nonzero position on any target contract rejects the whole run without sending an order.
_Avoid_: 管理既有仓位、带仓启动、剔除持仓合约后部分启动

**首轮报撤限额**:
The configured maximum number of quote submissions and cancellation requests in a rolling minute for one contract's session (default 60), configured and counted per contract independently. Reaching it pauses that session's quoting and never bypasses a necessary safety cancellation.
_Avoid_: 无上限报撤、把风险撤单计入静默失败、跨合约共享限额池

**合约元数据门槛**:
The target contract's CTP metadata, including its positive price tick, must be received before the test may submit its first quote.
_Avoid_: 写死最小变动价位、缺少合约元数据仍下单

**策略目标合约**:
A symbol and exchange named as one entry in the strategy configuration's contract list and used by that contract's order-management session. CTP credentials and the read-only connector's subscription settings do not select it.
_Avoid_: 从环境变量隐式继承下单合约、只读订阅合约

**盘口保护**:
The session may quote only when valid bid, ask, and last prices exist and its `W + D` is strictly greater than its configured protection multiple (default 2) times the observed bid-ask spread in ticks. An invalid or too-wide book cancels that contract's test quotes and pauses quoting.
_Avoid_: 薄盘口继续挂单、忽略无效行情

**轮内成交触发**:
The first CTP-reported partial or complete fill for either quote of the current round. It starts that round's spread-window closing while the opposite quote stays resting.
_Avoid_: 等待全额成交、成交即撤销对侧报价

**撤单终态上限**:
The session waits at most its configured cancellation timeout (default ten seconds) after entering the closing sequence for every test order to reach a terminal CTP status. On timeout it queries the target position, closes any net position with bounded FAK, and ends as a failure.
_Avoid_: 无限等待撤单回报、超时后恢复报价

**当日平仓偏移**:
The close offset chosen automatically for a position opened by this test today: close-today for SHFE and INE, ordinary close for other supported exchanges. A rejection ends the test rather than trying another offset.
_Avoid_: 手工填写偏移、拒单后猜测性重试

**目标委托手数**:
The positive per-side quantity in the strategy configuration's contract entry. No separate absolute cap applies; its actual value is highlighted before submission and recorded for the run, and any first partial fill still triggers closing.
_Avoid_: 隐式固定手数、成交后继续加仓、未展示的配置风险

**逻辑委托**:
One order instruction submitted to CTP together with its lifecycle through a terminal state. Repeated status callbacks still describe the same logical order; an order remains part of the audit view when it is cancelled, rejected, or never filled.
_Avoid_: 把每条委托回报算作一笔新挂单、只统计成交委托、把委托等同于成交

**运行委托成交报告**:
A manually generated offline HTML view of one complete terminal audit run, covering every target contract, every logical order, its related fills, the effective strategy, round-level gross PnL, and run-level balance-delta net PnL with implied fees. Both successful and failed terminal runs are valid, while active or crashed runs without a complete run summary are rejected. It never assigns unreliable net PnL to an individual order. It requires the run's structured causal trace and rejects older runs that lack it rather than inferring or degrading; it never aggregates unrelated runs or participates in live order submission.
_Avoid_: 交易日聚合报告、单合约事件摘录、策略运行时自动报表、旧日志推导或降级报告

**审计品种身份**:
The product code, exact contract symbol, exchange, contract multiplier, and minimum price tick recorded by one audit run. A human-readable Chinese product name is not part of the identity when the audit did not record it.
_Avoid_: 报告自行补充品种名称、只写中文简称、用当前合约信息覆盖历史审计事实

**运行参数**:
The complete effective strategy and its run-level hash retained by one audit run, including each contract's own grid, safety, timing, quantity, and stopping limits. It is loaded once when the confirmed process starts and remains the historical configuration actually used by that run, not the source file's later contents or current defaults.
_Avoid_: 当前策略文件、代码默认值、实际委托价格、运行中热更新参数

**委托参数**:
The actual instruction submitted for one logical order: its purpose, side, offset, order type, limit price, and quantity. It is distinct from both the run parameters that produced it and the repeated CTP callbacks that report its lifecycle.
_Avoid_: W/D/S 等运行参数、委托状态回报、成交结果

**委托生命周期摘要**:
One consolidated row for a logical order showing its actual parameters, deduplicated status path, terminal result, and related fills. Repeated callbacks are evidence for the row, not separate orders in the main report.
_Avoid_: 每条回报一行、只展示最终状态、把成交回报并入委托状态

**委托因果详情**:
The expandable explanation beneath a logical order that connects each meaningful lifecycle change to its triggering event, contemporaneous market data, applicable price calculation, state transition, and resulting submit, cancel, or replace action. It explains only causes supported by the run's audit facts.
_Avoid_: 原始 JSON 堆叠、根据结果猜原因、脱离当时行情解释撤单

**审计因果轨迹**:
A structured list attached to an audit event containing stable reason codes, affected logical orders, contemporaneous inputs, calculation operands, and decision results. It is emitted for state changes, actions, order-status changes, and fills; no-op market and clock events remain raw facts without explanatory noise. It contains no localized prose and is sufficient for an offline report to explain the decision without replaying the state machine.
_Avoid_: 中文说明直接落盘、报告器反推原因、只记录前后状态不记录决策输入

**运行因果时间线**:
The contract-level chronological view of causal trace entries that explain session changes not owned by a single logical order, such as startup position validation, market qualification, round reconciliation, termination, and failure. Order-specific detail remains with the logical order and is linked by its client identity rather than duplicated.
_Avoid_: 把运行级变化塞入最近委托、重复逐笔委托详情、只展示委托而丢失会话状态

**测试审计目录**:
The per-run directory holding one subdirectory per contract with its credential-free effective configuration, CTP market/order/trade events, and final safety summary, plus a run-level summary of every contract's position, active orders, and failures.
_Avoid_: 仅终端输出、无法还原的测试结果、多合约事件混写单一文件

**策略哈希身份**:
The SHA-256 identity of the effective strategy configuration, used for preview, audit, and replay identification. It is not a submission gate; order-capable entry requires only explicit SimNow confirmation.
_Avoid_: 把哈希当作下单门禁、只记录终端输出不保留策略身份

**稳定行情启动**:
After the SimNow launch confirmation, the test submits its first or resumed quote only after at least two valid, book-protected ticks, consecutive ticks no more than that contract's required `max_tick_age_seconds` apart, the latest tick still within that age at submit time, and the window lasting `stable_market_seconds` (default 2). Any miss restarts the next two-second wait. While quoting, exceeding the same threshold cancels the active opening quotes and requires this gate again.
_Avoid_: 启动即挂单、单条瞬时行情触发、用过期 Tick 下单

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
