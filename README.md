# 广发期货程序化交易：SimNow CTP 联调

这个项目先做第一阶段的只读联调：

```text
CTP 交易/行情前置
        ↓
    vn.py + vnpy_ctp
        ↓
    登录、合约、资金、持仓、Tick
```

`run.py` 不包含自动下单、撤单或策略逻辑，避免还没验证连接和状态同步就产生委托。

## 1. 创建 Python 环境

当前 Mac 是 Apple Silicon，Python 3.13 可以使用：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

项目已经把带持仓查询完成契约的 `vnpy_ctp` 放在 `vendor/vnpy_ctp`。安装会构建项目管理的 CTP 原生扩展；构建失败时，先确认本机已具备 C++ 编译器、Meson 和 Ninja。

## 2. 填写 SimNow 参数

```bash
cp .env.example .env
```

编辑 `.env`，填入 SimNow 当前页面给出的账号、密码、BrokerID、产品名称/AppID、授权编码和第一套前置。第一套继续使用 `CTP_TRADE_FRONT`、`CTP_MARKET_FRONT`；7×24 API 测试环境使用 `CTP_7X24_TRADE_FRONT`、`CTP_7X24_MARKET_FRONT`。两套环境共用账号类凭证，但连接环境必须在每次启动时人工选择，系统不会自动切换。

加载配置并先做本地检查：

```bash
set -a
source .env
set +a
python run.py --check --env first
```

`CTP_SYMBOL` 可以先留空。要测试行情时，从 SimNow 当前返回的有效合约中填写，例如：

```text
CTP_SYMBOL=当前有效合约代码
CTP_EXCHANGE=SHFE
```

合约代码和交易所必须以当前合约查询结果为准。

## 3. 启动只读联调

```bash
python run.py --env first
```

重点观察：

- 交易前置连接、认证和登录日志；
- 合约查询完成；
- 资金和持仓事件；
- 指定合约的买一、卖一和最新价 Tick。

指定 `CTP_SYMBOL`/`CTP_EXCHANGE` 时，诊断快照会把 `target(contract=Y,tick=Y)` 与
`contracts=Y` 分开显示：前者表示目标合约已到达并收到行情，后者表示全量合约列表已经结束；
SimNow 的全量列表可能明显晚于目标合约。交易侧查询拒绝会打印 `交易接口报错`，不会再静默等待。

按 `Ctrl+C` 退出。这个入口不会调用 `send_order` 或 `cancel_order`。

## 当前边界

当前实现面向 SimNow 多合约（每合约独立会话与收口）、单进程、无凭证落盘的受控联调；不包含生产交易、广发实盘配置、数据库持久化、跨进程连续托管或跨合约对冲。启动和重连会先查询目标合约当日委托/成交/持仓：遗留开仓单自动撤销，遗留平仓单、非零仓或未知状态进入 `RISK_HOLD`，不会静默退出。

完整的启动方式、状态机、CTP 回报/动作链路、报价与收口规则、审计字段、排查顺序和交接验收步骤见：[单合约报撤联调运行与代码逻辑说明](docs/live-grid-runtime.md)。

## 单合约报撤联调

普通连接入口仍然只读。需要下单能力时使用独立入口，并准备不含凭证的策略配置：

```bash
cp strategy.example.json strategy.json
# 把 contracts 条目改成当前 SimNow 有效合约，并为每条填写断流阈值与 quote_windows（v2 多合约格式）
python run_live_grid.py --config strategy.json --env first
```

该命令只显示标准化配置和 SHA-256 哈希，不会连接或下单。确认配置和目标合约无误后，只需显式确认当前连接是 SimNow：

```bash
python run_live_grid.py \
  --config strategy.json \
  --env first \
  --confirm-simnow
```

收盘后可手工切换到 7×24 API 测试环境：

```bash
python run.py --check --env 7x24
python run_live_grid.py --config strategy.json --env 7x24 --confirm-simnow
```

7×24 如果使用历史化行情做报撤联调，必须显式增加危险开关；普通模式会拒绝历史交易所时间行情：

```bash
python run_live_grid.py --config strategy.json --env 7x24 --confirm-simnow --allow-replay-market-data
```

7×24 不提供结算服务。每次报撤启动仍会针对所选环境查询目标合约净仓，非零仓时拒绝开仓；不要根据第一套的仓位或结算状态推断 7×24 的结果。SHA-256 仍会用于预览展示、审计记录和复盘识别，但不参与下单授权。修改策略配置会产生新的哈希，不需要额外提供旧哈希或新哈希参数。

CTP 凭证仍只从 `.env` 读取，策略配置和测试审计目录不得放入凭证。每次运行会在 `audit/` 下创建独立目录，记录无凭证事件和最终安全摘要。首次使用应先执行远价无成交后 `Ctrl+C` 的人工验收，再进行受控首次成交验收。
