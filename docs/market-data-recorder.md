# 独立行情采集器

`run_market_recorder.py` 使用独立进程直接加载项目的 MD 扩展，登录行情前置并批量保存原始行情。它不创建交易接口，不连接交易前置，不调用报单/撤单，不依赖交易控制台。当前支持本项目 macOS / CPython 3.13 的已编译绑定。

## 安装和本地检查

在项目根目录执行：

```bash
.venv/bin/python -m venv .venv-recorder
.venv-recorder/bin/python -m pip install -r requirements-recorder.txt
.venv-recorder/bin/python run_market_recorder.py --env 7x24 --prepare
.venv-recorder/bin/python run_market_recorder.py --env 7x24 --check
```

- 依赖安装只写 `.venv-recorder`。不升级 `.venv`，不重新编译交易绑定。
- `--prepare` 从 `vendor/vnpy_ctp/build/cp313` 及对应 `ctp_variants` 复制行情扩展和行情库，修正副本加载路径、签名，按内容哈希存放在 `.recorder-runtime/native`。不替换交易程序的 framework。
- `--check` 从 `.env` 检查所选环境的四项 MD 配置，并加载扩展验证 dyld 实际库路径；不创建 MD 对象或连接。没有 `--run` 时只预览配置。
- 缺少项目编译产物或 Python ABI 不匹配时应先完成项目的依赖构建；采集器不会偷偷重建或改变交易安装。
- macOS 使用系统 `install_name_tool`、`codesign`。其他平台当前明确拒绝运行，而不是尝试共享原生库。

凭证只从 `.env` 读取。支持 `KEY=value`、`export KEY=value`、单/双引号和行末注释，不执行 shell、不进行变量插值。只读取选择环境的 user/password/broker/market_front；不读取交易连接参数。

## 限时运行

```bash
.venv-recorder/bin/python run_market_recorder.py \
  --env 7x24 --config recorder.example.json \
  --run --duration 120 --startup-timeout 60
```

`--duration` 是连接开始后的总运行秒数，默认 120 秒，包含登录等待；`--startup-timeout` 默认 60 秒。到达启动超时时必须所有合约订阅成功并至少收到一条行情，否则该 run 失败。到达总时长正常收尾；`Ctrl+C` 只停止这个采集器。长期录制可显式给较大的 duration，例如 28800；不会自动常驻或注册后台任务。

环境必须明确选择：`first`、`7x24`、`guangfa`。采集配置中的 `contracts` 是独立订阅列表，编辑采集配置不会改变策略文件。示例为 IF2610/CFFEX；不自动订阅全部市场，不默认换月。

正常终态 `COMPLETED`；已知丢弃/断线后的正常收尾为 `COMPLETED_WITH_GAPS`，退出码仍为 0，分析时必须检查状态；启动、连接、写入或停止失败为 `FAILED`、非零退出码。终态中的 connected/logged_in 是关闭前最后观察结果，不代表进程结束后仍连接。

第一次在某个环境与其他会话并行使用前，需要在无挂单窗口验证前置的额外 MD 会话许可。此次 7x24 短时采集不等于已经验证与活跃 TD/MD 会话并行登录不会互相挤掉。

## 按文件大小滚动（2026-09-14 起）

配置使用 `"target_file_mib": 64`，原 `flush_rows` 已移除，旧配置会提示迁移。三份项目采集配置均已更新。文件按实际写出的压缩字节数滚动；内部约 4 MiB 未压缩数据组成一个 row group，写完一个组后检查目标大小。因此 64 MiB 是滚动目标，最终文件可多出一个数据组和 footer，不是逐字节硬上限。正常停止时，各交易日/交易所分区的不足大小尾文件也会完成，不为了凑大小遗漏数据。

`flush_seconds=10` 现在仅控制同步到该 run 的 `pending.sqlite3`，不会每 10 秒新建一个 Parquet。Writer 约每 1 MiB 待编码数据也会提前暂存，用来限制内存，不限制每个文件的行数。暂存使用 SQLite FULL 同步和 Arrow IPC 保存所有字段，Parquet 完成、同步并记录清单后才删除相应暂存；数据库页会回收。内部数据组达到约 4 MiB 才编码为 Parquet，避免把每次短时间刷新都变成一个小数据组。

状态含义：`committed_total` 是已完成且可查询的 Parquet 行数；`pending_durable_total` 是已持久化暂存但尚未完成 Parquet 的行数；`unsaved_total` 是队列或内存中尚未持久化的行数。兼容保留的 `uncommitted_total` 包含后两者，不再表示全部都没写磁盘。正常停止后 pending/unsaved/uncommitted 均为零。

查询函数仍只读完成的 `.parquet`。运行中尚未达到大小的尾部在暂存库中，正常停止或滚动后才纳入历史查询。进程意外终止后可离线恢复（原失败状态不会被伪装成采集成功）：

```bash
.venv-recorder/bin/python run_market_recorder.py \
  --env guangfa --recover-run <原始run_id>
```

恢复不读凭证、不加载 CTP，只把暂存恢复为完成文件并写入 `recovery.json`；活动 Writer 会通过文件锁拒绝恢复。恢复可重复执行，不重复发布已经完成的文件。异常时遗留但未登记完成的 `.tmp` 不参与查询，暂存是恢复依据；原始队列/内存中尚未同步的数据无法恢复，不能由恢复成功推出网络上游完整。历史小文件不会被自动合并、改写或删除。

文件较大通常能减少列举、打开文件和解析 footer 的开销；Parquet 仍按列与数据组读取，并可用统计信息跳过无关数据组。收益取决于过滤条件、数据分布和缓存；单文件很大或统计范围过宽时仍可能读取较多数据。参考 [Apache Arrow Parquet 文档](https://arrow.apache.org/docs/python/parquet.html)。

## 数据和运行目录

### 广发连接排查记录（2026-09-14）

受限沙箱内的采集器可能始终没有连接回调。沙箱外 TCP 探测成功不能证明沙箱内采集进程拥有网络权限；应对采集命令本身申请网络执行权限后重试，不能据此判断前置停服或账户未授权。

`recorder-guangfa-metals-chemicals.json` 使用 SA/FG 大写以及 ag/jm/lc 小写合约代码。实测大写 AG/JM/LC 可收到订阅确认但没有 Tick，改为小写后全部 15 个合约收到 Tick。订阅确认不能替代实际行情验收。

```bash
.venv-recorder/bin/python run_market_recorder.py \
  --env guangfa --config recorder-guangfa-metals-chemicals.json \
  --run --duration 120 --startup-timeout 60
```

```text
ctp-data/
  raw_tick/source_id=7x24/trading_day=<CTP原始交易日>/exchange=CFFEX/
    part-<run_id>-000001.parquet
  runs/<run_id>/
    manifest.json       # 无凭证订阅配置、版本、原生库来源/实际路径/哈希
    instruments.json    # 合约身份；未查询的价位/乘数明确为 null
    status.json         # 采集统计、心跳、状态和错误
    supervisor.json     # 监督进程的结束结果
    parts.jsonl         # 完成文件、行数、接收时间/序号边界
    gaps.jsonl          # 连接/订阅事件、已知队列溢出范围
    quarantine.json     # 仅异常时：最后失败样本中的已知标量字段
    pending.sqlite3     # 持久化暂存和文件发布记录
    recovery.json       # 仅离线恢复时：恢复结果，不覆盖原始运行状态
.recorder-runtime/
  native/<hash>/        # 私有 MD 扩展、行情库和校验清单
  flow/<run_id>/        # CTP 自用文件；不属于可分享行情数据
```

数据、虚拟环境和私有运行目录均已加入 `.gitignore`。flow 的内容由 CTP 库决定，不能把它当无凭证成果分享。采集子进程工作目录也是 `.recorder-runtime`，避免原生库相对路径文件落入交易目录。

RAW 保存包装层提供的全部已知 CTP 字段，保留原始字段名、异常价格哨兵、非有限浮点值、零值、空日期及重复快照；额外有 source_id/run_id/connection_id/seq_no/recv_epoch_ns/recv_monotonic_ns。本地时钟定义是 **Python MD 回调入口**，不是网卡或 C++ 首次收到报文的时间。

没有生成交易所统一时间或成交增量；7x24 的 TradingDay/ActionDay 可以明显早于采集日期，不能用本机日期覆盖。分区中的空交易所可由明确订阅配置补充，但原始 ExchangeID 列不改；不合法日期/交易所进入 UNKNOWN，原值仍保留。

`instruments.json` 第一版只保存订阅身份，price_tick/volume_multiple 明确缺失，不以旧值冒充当日查询。现有交易审计中的 ContractEvent 可供另行离线导入；本采集器不为查询元数据而建立 TD 连接。

## 读取与完整性

```python
from pathlib import Path
from market_data_recorder import read_ticks

table = read_ticks(Path('ctp-data'), '7x24', '20260911', 'IF2610')
rows = table.sort_by([('run_id', 'ascending'), ('seq_no', 'ascending')]).to_pylist()
```

用 `.venv-recorder/bin/python` 执行。读取器只读 `.parquet`，忽略 `.tmp`，显式声明 Hive 分区日期为 string，避免日期列类型冲突。跨 run 的排序仅用于查看；不能把它当交易所跨连接的严格顺序。

运行中满足 `received_total = committed_total + pending_durable_total + unsaved_total + dropped_total`；正常收尾满足 `received_total = committed_total + dropped_total` 且 `uncommitted_total = 0`。队列满丢弃新来的录制样本，不等空位；gaps 中 first/last 序号是丢弃的外边界，区间内可能夹有成功保存的 Tick，count 是实际已知丢弃数。seq_no 在入队前递增。

同一毫秒允许多条快照，使用 `(source_id, run_id, seq_no)` 作为本地唯一身份。序号连续及 dropped_total=0 不能证明网络、CTP 原生队列或停机期间绝对无丢失。独立 MD 连接的样本不能冒充交易进程逐条实际收到的样本；交易原因仍以交易 run 审计为准。

## 资源和故障边界

- 默认有界队列 10000 条，按字节预算或 10 秒触发持久化暂存，64 MiB 目标滚动；单 Writer，ZSTD 等级 1。队列容量保护内存，不是文件行数上限。Python 入队是带锁的非阻塞容量操作，不承诺硬实时。
- 采集工作进程降低调度优先级。默认内存高水位 512 MiB、磁盘预留 max(10 GiB, 卷容量 10%)；每轮管理检查及每批写入检查资源。
- 持续 80% 队列积压、Writer 心跳过期、写入错误或资源越限停止采集，不动交易进程，不自动清理任何历史数据。
- 数据先写 `.tmp`，完成 footer、文件同步后改名，再同步目录并追加清单。完成文件即使清单失败仍保留，可从文件重建；不自动重写已提交批次。
- 已完成分片不是断电零丢失承诺。强杀可能损失队列和未同步内存；持久化暂存可以离线恢复。缺少有序 supervisor 终态或心跳过期时把 run 视为不完整。
- 停止预算默认 10 秒；MD 关闭和 Writer 分配此预算。外层监督进程处理 native/GIL/Writer 卡住的情况，只终止自己启动的采集子进程。
- 无法可靠写盘时不保证错误文件也能成功写入；必须结合退出码和最后心跳。异常数据保留已知字段隔离样本，未知字段仅计数、不输出其值；不把 schema 变化静默吞掉。

同机运行仍共享 CPU、磁盘和网络。离线结果不能证明真实挂单链路绝对零延迟影响；不满足实际延迟预算时，应使用单独机器采集。

## 验证

```bash
.venv-recorder/bin/python -m unittest discover -s tests -p test_market_data_recorder.py -v
.venv/bin/python tests/benchmark_market_recorder.py \
  --output .scratch/market-data-recorder/benchmark-results.json
.venv-recorder/bin/python -m compileall -q market_data_recorder.py run_market_recorder.py
```

专项测试使用假 MD 检查生命周期、断线/订阅/错误、监督进程超时，并注入慢盘、队列满、写入/清单失败和原始异常字段；原生加载测试只加载扩展，不连接。

离线基准使用真实 AuditWriter 磁盘 flush 与假报撤接口，覆盖报价、成交、FAK、撤单、持仓对账，对比关闭录制、正常录制、慢盘录制三组。它不包含网络、原生回调或事件队列延迟，不代表实盘完整延迟。

项目原有测试存在同进程导入原生模块后影响库切换测试的顺序依赖，按模块隔离执行可避免该问题；HTTP 测试需要允许监听本机临时端口。原交易虚拟环境会跳过采集专项模块，专项必须在独立环境另跑，不能只看总测试命令的退出码。
