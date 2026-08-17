# Spec：SimNow 单合约实时网格报撤联调

Status: ready-for-agent

## Problem Statement

当前项目只能对 SimNow CTP 做只读登录、合约、资金、持仓和 Tick 联调，无法在受控条件下验证程序化网格的真实委托生命周期。离线回放器中的“可观察行情穿价即模拟成交”不能用于真实 CTP 委托：它既不能证明成交，也不能证明撤单已生效。

操作者需要一个单合约的 SimNow 报撤测试器。它应复用 W/D/S 网格报价和重定锚算法，但以 CTP 的委托、成交和关联的持仓查询完成事件为唯一事实来源。测试必须能在首次任意成交后安全收口，避免继续扩大敞口或遗留活动委托，并留下完整、无凭证的审计证据。

## Solution

提供一个独立于只读连接命令的“单合约报撤联调”入口。操作者以不含 CTP 凭证的策略配置指定策略目标合约、手数、W/D/S 与安全参数；只有显式确认 SimNow 环境后，该入口才可能提交委托。策略 SHA-256 继续用于预览、审计和复盘识别。

实时网格会话收到 CTP 合约、行情、委托、成交、持仓查询完成和时钟事件，产生提交委托、撤单和查询持仓动作。它等待目标合约元数据、关联的零仓查询结果和连续两秒的稳定有效行情，随后提交一对双向被动限价开仓单。行情异常或价差保护失败时撤单暂停；持续越出价带时严格按“撤旧单、等旧单终态、再挂新单”的顺序重定锚。合约、委托、成交事件同时携带交易所报给的合约乘数、报单时间与成交时间，随审计落盘供离线报告复盘，不参与状态机决策；网关轮询的资金快照以无凭证形式落入 run 级审计，供报告推算真实净盈亏与手续费。

任一目标委托首次部分成交或全部成交即进入价差窗口收口：对侧报价继续挂满 `closing_wait_seconds`（默认 1 秒，0 为无窗口）。窗口内对侧成交则价差落袋、撤残余委托并对账结束；窗口超时则按本轮净仓以受限 FAK 平仓，平仓终态后撤对侧，再对账收尾。人工中断走相同的撤单、查仓、必要时 FAK 路径。失败不掩盖残余仓位或活动委托。

## User Stories

1. As a SimNow 操作者, I want the ordinary connection command to remain read-only, so that checking connectivity can never accidentally submit an order.
2. As a SimNow 操作者, I want a dedicated 报撤测试入口, so that order-capable behavior is visibly separated from read-only diagnostics.
3. As a SimNow 操作者, I want CTP credentials to remain in environment configuration while strategy parameters live in a separate credential-free configuration, so that audit artifacts never expose secrets.
4. As a SimNow 操作者, I want the strategy target contract to come only from the strategy configuration, so that changing a read-only subscription cannot change the trading target.
5. As a SimNow 操作者, I want to see the effective target contract, exchange, hand count, W/D/S, rate limit, and all closing limits before any order can be sent, so that a configuration mistake is visible before trading.
6. As a SimNow 操作者, I want to explicitly confirm the SimNow environment and retain the exact effective strategy hash in the preview and audit, so that each run's configuration identity is clear without making the hash a second order gate.
7. As a SimNow 操作者, I want the test to refuse to quote until CTP has returned the target contract metadata and a positive price tick, so that every price is aligned to real contract metadata rather than a hard-coded assumption.
8. As a SimNow 操作者, I want the test to refuse to quote until a specific CTP position query proves the strategy target contract has zero net position, so that it never manages or closes an existing position.
9. As a SimNow 操作者, I want an explicit empty position-query completion result, so that zero position is not inferred merely because no position callback arrived.
10. As a SimNow 操作者, I want the position-query result to carry the request identifier that initiated it, so that delayed background polling cannot authorize a new quote or closing action.
11. As a SimNow 操作者, I want the first quote to wait for two seconds of valid target行情快照, so that a transient or incomplete quote cannot immediately open two orders.
12. As a SimNow 操作者, I want bid, ask, and last price validation plus book protection before quoting, so that a thin, invalid, or abnormally wide market pauses the test instead of placing near-market passive orders.
13. As a SimNow 操作者, I want W/D/S prices to be derived from the latest valid LastPrice and the CTP price tick, so that the live grid uses the agreed algorithm without price-rounding drift.
14. As a SimNow 操作者, I want re-anchoring only after the LastPrice remains outside the current band for the configured confirmation period, so that normal quote noise does not cause needless report/cancel churn.
15. As a SimNow 操作者, I want replacement quoting to wait for terminal CTP callbacks for old orders, so that old and replacement quotes cannot overlap and double exposure.
16. As a SimNow 操作者, I want an ordinary rolling action limit of 60 quote submissions or cancellation requests per minute, so that a volatile market cannot cause uncontrolled report/cancel traffic.
17. As a SimNow 操作者, I want required safety cancellations to be attempted even if the ordinary action limit has been reached, so that a rate guard cannot silently preserve unwanted active orders.
18. As a SimNow 操作者, I want CTP order and trade callbacks—not行情快照—to decide whether an order exists, is active, is cancelled, or has filled, so that live state never reuses replay-only simulated-fill logic.
19. As a SimNow 操作者, I want the first partial fill to be treated the same as the first full fill, so that a residual order or opposite quote cannot continue increasing the position.
20. As a SimNow 操作者, I want all remaining test orders cancelled after first fill, so that the test has at most one controlled opening episode.
21. As a SimNow 操作者, I want the test to wait at most ten seconds for closing-sequence cancellations to reach terminal status, so that it cannot wait indefinitely without making the outstanding risk visible.
22. As a SimNow 操作者, I want a fresh, request-correlated position query before flattening, so that the close quantity always reflects confirmed net position after any late fill.
23. As a SimNow 操作者, I want a long net position closed by selling and a short net position closed by buying, with the correct exchange close offset, so that the CTP close request matches the position created by this test today.
24. As a SimNow 操作者, I want FAK flattening to begin from an executable opposite quote and be bounded to three seconds and ten adverse ticks, so that automatic closing has a clear worst-price envelope rather than unlimited chasing.
25. As a SimNow 操作者, I want a rejected, timed-out, or incomplete flattening attempt to stop the test and expose its residual state, so that no unbounded retry or guessed close offset can hide an unsafe outcome.
26. As a SimNow 操作者, I want Ctrl+C to initiate the same cancel, reconcile, and bounded-close sequence, so that manual termination does not leave live test orders behind.
27. As a SimNow 操作者, I want every run to write an isolated 测试审计目录 containing the effective credential-free configuration, time-ordered CTP events, and a final safety summary, so that I can investigate every outcome from evidence.
28. As a SimNow 操作者, I want the final summary to state final net position, active order count, first-fill details, cancellation result, flattening attempts, terminal state, and failure reason, so that a pass cannot be confused with an incomplete close.
29. As a maintainer, I want the CTP gateway safety extension to be a tracked editable project dependency, so that the position-query completion contract is reproducible rather than an undocumented local site-packages edit.
30. As a maintainer, I want deterministic tests at the live-grid-session boundary, so that the most safety-critical behavior can be verified without credentials, native CTP connectivity, or a live SimNow session.

## Implementation Decisions

- Preserve the existing ordinary CTP connection command as read-only. The order-capable behavior is a separate dedicated entry point and must never be enabled through a hidden mode of the read-only command.

- The first release is a **单合约报撤联调**, not a hedge strategy or replay engine. It has one 策略目标合约 and submits a buy and sell passive opening quote for that contract only.

- The strategy configuration is a versionable JSON document with no credentials. It contains the target symbol and exchange, positive integer 目标委托手数, W/D/S, book-protection multiple, confirmation durations, normal action limit, cancellation-terminal timeout, and bounded-FAK limits. The effective normalized configuration is the audit source of truth.

- A launch is order-capable only after the explicit SimNow flag is provided. The SHA-256 hash of the canonical effective credential-free strategy configuration remains in the preview, audit, and final summary as the strategy identity; it does not authorize or block submission.

- The real-time behavior is one highest-level test seam: a live-grid session consumes normalized contract, tick, order, trade, position-query-complete, clock, and interrupt events; it emits submit-order, cancel-order, position-query, audit, and terminal-status actions. The CTP entry point is a thin adapter around this session.

- The externally meaningful state sequence is:

  ```text
  PREVIEW
    → WAITING_FOR_CONTRACT
    → WAITING_FOR_ZERO_POSITION
    → WAITING_FOR_STABLE_QUOTE
    → QUOTING ↔ REPLACING
    → CLOSING_CANCELS
    → CLOSING_RECONCILE
    → FLATTENING
    → FINISHED or FAILED
  ```

  A state advances only on its required CTP callback or verified clock condition; a timer alone never fabricates an order acknowledgement, cancellation, fill, or zero position.

- Quote generation reuses the agreed W/D/S behavior. With CTP-derived `pricetick` and a tick-aligned LastPrice anchor, the diagnostic band is one W-width on each side and passive buy/sell limits are W+D ticks away from the anchor. A re-anchor occurs only after LastPrice remains outside the current band for one second; the new anchor moves in ten-tick steps sufficient to bring the band back around the current price.

- The first release defaults to W=20, D=20, S=10, re-anchor confirmation=1 second, stable-market recovery=2 seconds, and normal action limit=60 actions per rolling minute. Positive strategy-config values remain configurable; no separate absolute maximum is imposed on 目标委托手数.

- The CTP contract callback is the only source of `pricetick`. No first quote is allowed until the target contract has a positive price tick. All computed order prices must be tick-aligned.

- A valid target 行情快照 has finite positive bid, ask, and last prices with an ordered book. Before quoting or resuming, the market must remain valid and pass the 2-second stability gate. The book-protection condition is strictly `W + D > 2 × bid-ask spread in ticks`. An invalid or too-wide book cancels active test quotes and pauses new quoting until it becomes stable again.

- Live CTP callbacks, not replay assumptions, are authoritative. Order lifecycle state comes from CTP order callbacks; actual fill quantity comes from trade callbacks and confirmed order state. A market price crossing a limit is never treated as an execution.

- New replacement quotes must not be submitted until every old test quote scheduled for replacement has reached a terminal CTP status. The inherited one-second acknowledgement settings are observability thresholds only: they may produce an audit warning and hold the session in a safe non-quoting state, but they never advance the session without the actual callback or create a duplicate order.

- At the first observed partial or full opening fill, the session permanently disables new opening quotes for that run and begins 单次成交收口. It cancels all remaining test orders, accepts and records any late callbacks, and does not resume quoting.

- The closing sequence waits at most ten seconds for remaining test orders to reach terminal status. On timeout, it records the failure, issues a new request-correlated position query, and still attempts bounded closing for the actual confirmed net position. It never resumes quoting after this timeout.

- The project-managed editable CTP gateway publishes a general position-query-complete event after every completed position query, including an empty query. Its payload provides the CTP request identifier and the query's summarized positions. The gateway's position-query action returns or otherwise exposes the same request identifier. The session accepts only the completion event matching its own startup or closing query.

- The zero-position startup gate applies only to the 策略目标合约. Any nonzero net target position rejects the run without sending an opening order. Existing positions are never adopted, netted, or closed by this feature.

- Bounded flattening begins only after the closing reconciliation query. A long net position sends a sell close and a short net position sends a buy close. Positions opened by this test today use close-today for SHFE and INE, and ordinary close for other supported exchanges. A close rejection ends the run; the system must not guess another offset.

- A flattening attempt uses an executable FAK price from the current opposite top-of-book. For no more than three seconds, it may retry only for remaining confirmed position and may move at most ten ticks in the adverse direction from the initial executable close price. No valid quote, rejection, terminal timeout, or nonzero residual position produces `FAILED`, not a market order or unlimited price chase.

- Ctrl+C and equivalent process interruption enter the same closing sequence. They do not directly tear down the CTP connection while the session still has tracked active test orders or a confirmed nonzero position.

- Every run creates a unique 测试审计目录. It retains the effective credential-free strategy configuration, deterministic configuration hash, CTP contract/tick/order/trade/position-query-complete events, emitted actions, state transitions, and a final summary. Audit artifacts must not contain account credentials, passwords, authorization codes, or front addresses.

- The final summary distinguishes `FINISHED` from `FAILED` and includes target contract, exchange, hand count, configuration hash, startup position result, first fill, cancellation-terminal result, correlated closing query result, FAK attempts, final net position, active test orders, and any failure reason.

- The CTP gateway used at runtime becomes a project-managed editable dependency. Startup must verify that it loaded this project-managed copy; an unmanaged installed gateway is a hard failure for the order-capable entry point because it lacks the position-query-complete contract.

## Testing Decisions

- The preferred and only primary test seam is the live-grid session. Tests inject normalized external events and a monotonic clock, then assert emitted CTP action intents, terminal state, and audit-facing results. They do not assert private collections, CTP SDK internals, or incidental log formatting.

- The session tests must cover configuration validation and preview-only behavior: invalid values, invalid exchanges, non-positive hand counts, absent SimNow confirmation, confirmed launch without a hash parameter, and deterministic hash recording.

- The session tests must prove no opening order before all of these observable gates are satisfied: matching positive contract price tick, matching zero-position query completion, valid market data, passed book protection, and two seconds of stable valid行情快照.

- The session tests must prove that a nonzero target position, missing metadata, invalid book, too-wide book, or mismatched position-query request identifier produces no opening order.

- The quote tests must assert tick-aligned W/D/S prices, the strict book-protection inequality, one-second re-anchor confirmation, ten-tick re-anchor stepping, and two-second recovery after a pause.

- The replacement tests must prove that old orders are cancelled before new orders are submitted, that submitted or cancellation-requested old orders block replacement, and that acknowledgement-delay warnings never create a second live quote pair.

- The action-limit tests must prove ordinary quote actions pause at 60 per rolling minute while necessary safety cancellation actions still occur and are visible in the audit stream.

- The fill tests must inject both partial and full opening fills. Both must disable new quotes, cancel all remaining test orders, and move to closing; a tick crossing a limit without a CTP fill must not do so.

- The closing tests must cover late fills while cancellations are pending, terminal cancellation success, the ten-second cancellation timeout, and matching versus non-matching position-query-complete request identifiers. The flattening quantity must equal the final correlated net position rather than an opening-order expectation.

- The flattening tests must cover long and short directions, SHFE/INE close-today selection, other-exchange ordinary close selection, FAK execution price direction, partial close fills, three-second timeout, ten-tick adverse cap, missing executable quote, and close rejection. Any unresolved position must result in a visible failure state.

- The interruption tests must prove that an interrupt in quoting or replacing starts cancellation and reconciliation rather than directly terminating the session.

- The CTP gateway adapter tests must observe the public position-query-complete event: a query with positions and a query with no rows both publish a completion payload with the initiating request identifier and summarized result. These tests do not require a real CTP endpoint.

- The project currently has no application-level strategy test suite to reuse. The vendored CTP package's order/trade tests are prior art for the shape of CTP lifecycle callbacks only; project tests should remain deterministic and exercise the live-grid session seam rather than attempting native CTP integration.

- SimNow acceptance is intentionally manual and staged after local tests pass: first a far-away no-fill run followed by operator interrupt; then a controlled first-fill run; then deliberate failure observations for reject, cancellation timeout, and incomplete flattening. Each run is accepted only when its audit summary makes the final position and active-order state unambiguous.

## Out of Scope

- Historical tick collection, offline replay execution, fair-price calculation, candidate-event attribution, and HTML replay reports.
- Multi-contract fair-reference logic, automatic hedge submission, hedge ratio calculation, and hedged exit behavior.
- Production brokerage connectivity, 广发实盘 credentials, or any real-money trading change.
- Persistent order recovery across process crashes, strategy restart/adoption of existing positions, portfolio-level risk management, or multi-session scheduling.
- A user interface, web dashboard, database, or external notification channel.
- Automatically changing close offsets after a CTP rejection, market-price closing, unlimited adverse-price chasing, or hiding residual position state.
- Changing the existing read-only connection behavior or storing credentials in strategy configuration or audit artifacts.

## Further Notes

- This specification uses the established domain terms: SimNow 行情快照, 实盘状态机, 单合约报撤联调, 单次成交收口, 受限 FAK 收口, 零仓启动门槛, 测试审计目录, and 持仓查询关联号.

- The live behavior deliberately reuses only the W/D/S quotation and re-anchor logic from the prior replay design. The replay concept 模拟成交 remains prohibited in this implementation because the authoritative live evidence is a CTP order/trade callback.

- The project-level architecture decision requires the general position-query-complete event and a project-managed editable CTP dependency. This spec must remain aligned with that decision if implementation details evolve.

- The operator retains responsibility for choosing a current valid SimNow contract and an intentional `target_lots` value. The system displays both before confirmation, but the selected scope intentionally has no separate hard hand-count ceiling.
