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

编辑 `.env`，把 SimNow 当前页面给出的账号、密码、BrokerID、交易前置、行情前置、产品名称/AppID、授权编码填进去。不要复制旧教程里的前置地址。

加载配置并先做本地检查：

```bash
set -a
source .env
set +a
python run.py --check
```

`CTP_SYMBOL` 可以先留空。要测试行情时，从 SimNow 当前返回的有效合约中填写，例如：

```text
CTP_SYMBOL=当前有效合约代码
CTP_EXCHANGE=SHFE
```

合约代码和交易所必须以当前合约查询结果为准。

## 3. 启动只读联调

```bash
python run.py
```

重点观察：

- 交易前置连接、认证和登录日志；
- 合约查询完成；
- 资金和持仓事件；
- 指定合约的买一、卖一和最新价 Tick。

按 `Ctrl+C` 退出。这个入口不会调用 `send_order` 或 `cancel_order`。

## 当前边界

当前实现只面向 SimNow 单合约、单进程、无凭证落盘的受控联调；不包含生产交易、广发实盘配置、回放撮合、数据库持久化或多合约组合。

## 单合约报撤联调

普通连接入口仍然只读。需要下单能力时使用独立入口，并准备不含凭证的策略配置：

```bash
cp strategy.example.json strategy.json
# 把 symbol 改成当前 SimNow 合约查询返回的有效合约
python run_live_grid.py --config strategy.json
```

该命令只显示标准化配置和 SHA-256 哈希，不会连接或下单。确认无误后，必须同时提供 SimNow 确认和至少八位匹配的策略哈希前缀：

```bash
python run_live_grid.py \
  --config strategy.json \
  --confirm-simnow \
  --confirm-hash <策略哈希前八位或更长前缀>
```

CTP 凭证仍只从 `.env` 读取，策略配置和测试审计目录不得放入凭证。每次运行会在 `audit/` 下创建独立目录，记录无凭证事件和最终安全摘要。首次使用应先执行远价无成交后 `Ctrl+C` 的人工验收，再进行受控首次成交验收。
