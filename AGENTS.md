## 项目速览

广发期货程序化交易的 SimNow CTP 联调项目（不面向实盘生产）。两个入口：`run.py` 是只读连接联调（登录、合约、资金、持仓、Tick），绝不调用下单/撤单；`run_live_grid.py` 是受控报撤测试，多合约并发、每合约独立会话与收口。CTP 凭证只从 `.env` 读取，策略配置与审计目录不得包含凭证。

Agent 不需要通读代码：先用下表定位模块，再按"文档路由"选择要精读的文档。

### 代码地图

| 模块 | 责任 |
| --- | --- |
| `run.py` | 只读 CTP 连接入口 |
| `run_live_grid.py` | 报撤测试入口：预览/确认、审计目录、启动门槛、Ctrl+C 收口 |
| `report.py` | 离线报告工具：交易日成交明细与单 run 委托成交报告（HTML） |
| `live_grid/config.py` | 策略配置校验、规范化与 SHA-256 哈希（拒绝凭证字段） |
| `live_grid/session.py` | 与 CTP 无关的确定性状态机：报价、撤换、价差窗口收口、受限 FAK、最终摘要 |
| `live_grid/ctp_adapter.py` | vn.py/CTP 回报与状态机动作之间的薄适配层 |
| `live_grid/ctp_native.py` | 按 `--env` 切换 SimNow/广发 CTP 原生库（导入扩展前） |
| `live_grid/audit.py` | 每次运行的无凭证审计写入（`audit/<run>/`） |
| `vendor/vnpy_ctp` | 项目内可追踪的 CTP 依赖（含持仓查询完成事件补丁，勿改 site-packages） |

### 文档路由

| 要了解什么 | 读哪里 |
| --- | --- |
| 领域术语的精确定义与常见误解 | [CONTEXT.md](CONTEXT.md) |
| 运行方式、状态机、CTP 回报/动作链路、报价与收口规则、审计字段、排查顺序 | [docs/live-grid-runtime.md](docs/live-grid-runtime.md) |
| 人工验收阶段与通过判据 | [docs/live-grid-acceptance.md](docs/live-grid-acceptance.md) |
| 关键架构决策及理由 | [docs/adr/](docs/adr/) |
| 功能规格与验收计划 | `.scratch/<feature>/PRD.md` |

### 常用命令

```bash
source .venv/bin/activate
pip install -r requirements.txt

python -m unittest discover -s tests -q      # 全部离线测试，无需真实 CTP 连接
python -m compileall -q live_grid run.py run_live_grid.py report.py   # 发布前语法检查
python run.py --check --env first     # 只读入口的本地配置检查
python run.py --env first             # 只读连接联调（7x24 为 API 测试环境）
cp strategy.example.json strategy.json                        # 报撤前准备无凭证策略配置
python run_live_grid.py --config strategy.json --env first    # 仅预览标准化配置与哈希
python run_live_grid.py --config strategy.json --env first \
  --confirm-simnow                                           # 显式确认后才具备下单能力
```

## Agent skills

### Issue tracker

Issues are tracked as local Markdown files under `.scratch/<feature>/`; external pull requests are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Uses the default canonical labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Uses a single-context layout with root `CONTEXT.md` and `docs/adr/`. See `docs/agents/domain.md`.
