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

Mac 上的 `vnpy_ctp` 需要从源码构建：

```bash
git clone https://github.com/vnpy/vnpy_ctp.git ../vnpy_ctp
python -m pip install ../vnpy_ctp
```

如果你的上级目录已经有 `vnpy_ctp`，跳过 `git clone`，只执行安装命令即可。构建失败时，以 [vnpy_ctp 官方 README](https://github.com/vnpy/vnpy_ctp) 的 Mac 安装说明为准。

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

本阶段没有加入策略、自动下单、风控、订单持久化和广发实盘配置。等 SimNow 的登录、行情、资金和持仓链路稳定后，再增加一手模拟委托和撤单测试；切换到广发时只替换同一组 `CTP_*` 配置。
