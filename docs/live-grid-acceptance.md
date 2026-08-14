# SimNow 实盘验收记录

本文是报撤联调功能真实 SimNow 验收的执行台账，对应 [`live-grid-runtime.md`](live-grid-runtime.md) 第 14 节的验收顺序。每个阶段标注"已执行"或"pending"，附审计目录和判据核对结果。缺陷复盘保留根因和修复提交，作为后续维护的先例。

## 阶段 A：本地无凭证验证 — 已执行（2026-08-14）

```text
.venv/bin/python -m unittest discover -s tests -q   # 44 tests OK
.venv/bin/python -m compileall -q live_grid run_live_grid.py tests vendor/vnpy_ctp/vnpy_ctp
```

## 阶段 B：只读 SimNow 联调 — 已执行（2026-08-14）

`python run.py`：登录、认证、结算确认、资金、合约、AP610 行情订阅全部正常；确认无任何委托动作。实测 SimNow 合约查询成功需 30 秒～2 分钟。

## 阶段 C：远价无成交报撤验收 — 已执行（2026-08-14）

### 运行记录

| 审计目录 | 结果 | 说明 |
| --- | --- | --- |
| `audit/ap610-preview/…` | PREVIEW | 预览核对 effective 配置与哈希 |
| `audit/ap610-live*`（上午 3 次） | FAILED `interrupted_before_zero_position` | 合约回报等待不足即 Ctrl+C，非程序缺陷 |
| `audit/20260814T063230…` | FAILED `startup_position_query_failed` | 缺陷 1：查仓发送被拒即判失败（修复前） |
| `audit/20260814T063717…` | FAILED `startup_position_query_failed` | 缺陷 1 第一版修复（重试 3 次）预算不足 |
| `audit/20260814T064414…` | **FINISHED** | **阶段 C 通过**，细节见下 |

### 通过判据核对（`audit/20260814T064414.827392Z-c9d3b96f60/summary.json`）

| 判据 | 要求 | 实际 |
| --- | --- | --- |
| `terminal_state` | `FINISHED` | `FINISHED` |
| `startup_position_result` | `zero` | `zero`（重试约 2 分钟后送达） |
| 首次报价 | 双向各 1 手、远价 | BUY 1@7789 / SELL 1@7869（现价约 7829，W+D=40） |
| 观察期 | 无成交、订单驻留 | 4 分钟 `NOTTRADED`，无重定锚 |
| 收口 | 中断后撤单全部终态 | 两笔 `CANCELLED`，`cancellation_terminal=true` |
| `closing_position_request_id` | 与本次关联 | `position-2` 匹配 |
| `final_net_position` / `active_order_count` | `0` / `0` | `0` / `0` |

### 缺陷复盘

**缺陷 1：查仓发送被拒即判查询失败。** 目标合约回报在合约查询响应流的中间到达，CTP 单在途查询限制使查仓发送被拒；适配层原把"发送被拒"当成"查询失败"立即 FAILED。修复：按定时器每秒重试、最多 60 次的有界预算（`live_grid/ctp_adapter.py`，提交 `c321bcd`）。教训：SimNow 启动链路等待 2~3 分钟属正常。

**缺陷 2：终态后进程不退出。** `close()` 持 adapter 锁调用 `MainEngine.close()`，`EventEngine.stop()` join 的工作线程正阻塞在同一把锁的回调上，互等死锁；faulthandler 线程栈实证。修复：锁内换手 `main_engine`、锁外关闭引擎、再关审计（同提交 `c321bcd`）。修复后验证：收口到 `FINISHED` 后进程自行退出。

## 阶段 D：受控首次成交验收 — pending

目标：在操作者在场、有人工接管安排的前提下，让首次真实成交触发完整的单次收口链路（停止报价 → 撤单 → 关联查仓 → 受限 FAK 平仓 → `FINISHED`）。建议 2026-08-17（周一）日盘执行。

### 准备

1. 前置：阶段 C 已通过（上表）；AP610 净仓为 0（收口已确认，可用只读 `run.py` 复核）。
2. 操作者在场，日盘时段（苹果无夜盘）；准备可登录同一 SimNow 账号的手动下单客户端（如快期），作为 FAK 失败后人工平仓的兜底——本项目没有手动下单入口。
3. 使用 `strategy-stage-d.json`：`w_ticks=2, d_ticks=3`（报价距锚点 5 tick，几个分钟内大概率成交；W+D=5 > 2×spread 要求价差 ≤2 tick，AP610 通常满足），其余参数与阶段 C 一致。
4. 先预览核对 effective 配置与哈希。

### 执行与观察

1. `--confirm-simnow` 启动，耐心等待启动链路（合约回报 + 查仓重试，2~3 分钟正常）。
2. 挂单后等待首次成交，实时观察：
   - 第一笔成交（哪怕部分成交）是否**立即**停止新增报价并进入 `CLOSING_CANCELS`；
   - 对侧剩余挂单是否全部撤销并收到 CTP 终态；
   - closing 查仓的 request id 是否关联本次（`position-N` 递增）；
   - FAK 是否按实际净仓、相反方向、CZCE 用 `CLOSE` 发出，价格从对手一档起、不越 `flatten_adverse_ticks`；
   - `终态=` 打印后进程是否自行退出（缺陷 2 修复的运行时验证点）。
3. `Ctrl+C` 仅用于提前结束等待成交；中断走与成交相同的收口路径。

### 通过判据（`summary.json` 逐项核对）

```text
terminal_state = FINISHED
first_fill 非空（记录成交价与数量）
cancellation_terminal = true
closing_position_request_id 与本次关联
final_net_position = 0
active_order_count = 0
flatten_attempts 至少 1 次，方向与净仓相反、offset 为 CLOSE
state_transitions 含 QUOTING → CLOSING_CANCELS → CLOSING_RECONCILE → FLATTENING → FINISHED
```

### 失败处置

- `flatten_rejected` / `flatten_timeout` / 残仓非零：按运行文档第 15 节"平仓没有结束"顺序排查，人工接管平仓，保留审计目录；
- 任何失败都不删 audit、不重启进程、不改 offset 来"清理"收口；
- 保留独立审计目录，不用终端输出代替证据。
