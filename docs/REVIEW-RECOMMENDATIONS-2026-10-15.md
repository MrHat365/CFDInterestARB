# 第六轮：独立复审、首次实际执行基线与修改建议

日期：2026-10-15（Asia/Taipei）。范围：前五轮修复后的当前工作树（含第五轮性能专项）。

## 0. 本轮性质与边界

- **本轮只交付审查结论与修改建议，没有修改任何业务源码。** 原因：审查期间（14:36–14:52）另一个代理正在同一工作树修改 `worker.py`、`chart.py`、`series_data.py`、`event_queue.py` 等文件（第五轮性能专项）。并发写同一文件会互相覆盖，因此本轮把修复方案写成可直接实施的规格，等工作树不再被并发修改后再落地。
- **约束：用户确认 macOS 不能运行本项目，本轮只审查代码逻辑。** 在收到该确认之前，本轮已在项目 `.venv`（Python 3.14.6）安装 pytest/ruff/mypy/numpy/psutil，并对纯逻辑测试、ruff、mypy 各执行了一次；结果如实记录在 A 节，仅作参考。收到确认后已停止一切执行：PySide6/pyqtgraph 安装已中止（未装上），Qt/图表测试没有运行，`.mypy_cache` 已删除。没有启动应用或 worker，没有安装 MetaTrader5，没有连接任何账户、终端或 broker。
- 这些测试使用注入的伪 MT5 API。它们通过只说明代码契约自洽，**不等于** Windows/真实 broker 行为正确。本文结论以代码逻辑审查为主。

## A. 实际执行基线

| 验证项目 | 执行结果 | 证据 / 限制 |
| --- | --- | --- |
| 单元 + 集成测试（非 Qt） | **158 passed，0 failed，0 skipped** | `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests -q -p no:cacheprovider --ignore=tests/test_chart_ui.py`，2.3s |
| Qt/图表测试 `test_chart_ui.py` | **未运行** | 按用户约束停止；PySide6 未安装 |
| Lint（ruff，项目配置 E4/E7/E9/F） | **28 errors** | 9×E741、7×E702、6×F401、5×F841、1×E401；另有 `strategy.py:120` 的 noqa 格式警告 |
| 类型检查（mypy，项目配置） | **90 errors / 12 files** | 绝大多数为惰性初始化属性的 Optional 标注缺失、Windows 专用模块（winreg/msvcrt/ctypes.windll）在 macOS 上无属性；未发现指向运行期缺陷的类型错误（`_SpreadTaker.partial` 位于已禁用的旧算法路径） |
| 生产构建（PyInstaller） | 未执行 | spec 面向 Windows，macOS 无法产出等价制品 |
| 应用启动 / MT5 连接 | 未执行 | 无 MT5；需 Windows 演示账户 |
| 缺陷复现探针 | 已执行 | 见 B 节 P0-1、P1-2；脚本位于仓库外 `/tmp/cfdprobe/probe_clock.py`，直接调用仓库代码 |

### A.1 Lint 中值得修的条目

不是风格问题、建议立即清理的：`worker.py:653 fetched_at`、`worker.py:1005 tp_adj`、`console.py:2109 color`、`accounts_view.py:237 rows` 四个未使用变量（死代码，说明逻辑曾被修改但未收尾）；`console.py` 中 `_fmt_value` 定义了两次（161 行与 485 行，后者覆盖前者）。

## B. 问题清单（按优先级）

“已复现”表示用仓库代码实际执行得到错误结果；“代码确认”表示调用链静态确认；“待 Windows 验证”表示需真实终端证据。

### P0-1 broker 服务器时间被当作 UTC，导致实时图表、行情新鲜度、实盘与自动交易在多数 broker 上失效

- **证据等级：已复现（以 +3h 偏移为输入）+ 外部证据；偏移是否存在需在目标 broker 上记录一次确认。**
- **外部证据**：MetaTrader5 Python API 的 `symbol_info_tick().time/time_msc` 与 `copy_rates_*()['time']` 是“broker 服务器时区写成的 epoch”，并非真实 UTC，与官方文档描述不符；MetaQuotes-Demo 实测比 PC UTC 快约 10,800 秒（GMT+3）。来源：[MQL5 论坛 517653](https://www.mql5.com/en/forum/517653)、[MQL5 论坛 518264](https://www.mql5.com/en/forum/518264)、[MQL5 论坛 375461](https://www.mql5.com/en/forum/375461)、[mt5-gateway issue #70](https://github.com/elitekaycy/mt5-gateway/issues/70)。
- **受影响位置**（同一根因）：
  - `worker.py:1259` `_tick()`：`-2 <= time.time() - tick.time <= 15`。GMT+2/+3 时差值为 −7200/−10800，**所有受管开平仓的预检查、残余检查和自动任务报价都返回 None**，表现为“交易规格或行情无效”“本地委托校验失败”。
  - `series_data.py` `quotes_ready()`：同样比较，**控制台永远不认为行情新鲜**，纸面监测无法产生实时点，`_execute_live`/手动开仓门槛不通过。
  - `automated_trading.py:263-264` `_quotes()`：QUOTE_AGE=2s 对 broker 时间比较，自动入场永不触发。
  - `strategy.py:333` `on_tick()` 用本地 `datetime.now()` 作为实时点时间，而历史 H1/M1 用 broker bar 时间（`worker.py:983`）。历史末点比本地 UTC 晚 2~3 小时，`SpreadSeries.append` 和 `SeriesBuffer.append` 都拒收“更早”的点。
- **复现结果**（GMT+3 输入）：图表追加实时点 `False`；新鲜 tick 的 `quotes_ready` 为 `False`；20 个 tick 后 runner 实时点数为 `0`，统计永久停在 H1 先验。对照组（UTC broker）实时点数 20，正常。
- **这正是“图表无法正常使用”的主要根因**：不是 pyqtgraph 的问题，是历史与实时两条数据流使用了两种时钟。
- **修复方案（单一时间契约，在 worker 边界完成换算）**：
  1. 新建纯函数模块 `broker_clock.py`：`BrokerClock.observe(server_ts, local_ts)` 用当前最新 tick 估计偏移 `round((server_ts - local_ts)/900)*900`；仅在残差 ≤ 60s 且 |偏移| ≤ 14h 时接受；持续重估以覆盖夏令时切换（本月 EEST→EET 即会从 +3 变 +2）。估计依据是测量值，若 broker 真是 UTC，偏移自然为 0，不改变现有行为。
  2. worker 内所有出站时间统一换算为真实 UTC epoch：tick 的 `time/time_msc`、历史 `rates.time`、`_tick()` 新鲜度判断。偏移随 `EVENT_STATUS`/`EVENT_ACCOUNT` 上报（`server_utc_offset`），UI 可显示“服务器时区 GMT+3”。
  3. 偏移未知（例如周末休市、无新鲜 tick）时 `clock_ready=False`：新鲜度判断失败关闭（保持当前的保守语义），历史可加载但标注“时区未校准”，校准后重拉历史。
  4. runner 的实时点时间改用该帧各腿 broker 时间换算后的最大值，不再用本地 `datetime.now()`，历史与实时共用同一时间轴。
- **测试**：GMT+0/+2/+3/+5:30 偏移估计；陈旧 tick（30/58 分钟）不被接受；DST 切换重估；历史（服务器时间）后追加实时点被接受；`quotes_ready`/`_tick`/`_quotes` 在 +3h 下对新鲜 tick 返回可用；周末无新鲜 tick 时失败关闭。
- **Windows 验收**：在演示账户上记录 `symbol_info_tick('EURUSD').time - time.time()`；实时点在加载历史后立即出现在图表右缘；自动任务在条件满足时能进入执行阶段。

### P1-1 自动交易的报价“新鲜度”模型把“价格没变”误判为“报价失效”

- **证据等级：代码确认。** 与 P0-1 独立，即使 broker 为 UTC 也存在。
- `automated_trading.py:106-107,264,272`：每腿最后一次价格变动距今 ≤ 2s，且各腿最后变动时间差 ≤ 0.5s，否则不估值（“跨腿报价不同步”）。
- 根因：`time_msc` 是“该品种最后一次报价变化”的时间，不是“报价是否有效”。CHFJPY、NZD 交叉盘等低活跃品种几秒不变是常态，三腿的最后变化时间彼此相差 0.5s 以内是小概率事件。结果是入场条件长期显示“等待同步行情”，**自动入场在大部分时间不可能触发**；退出的“净利润达标”分支同样受阻（风险退出不受影响）。
- 修复方案：区分两件事。
  - “本轮读取是否新鲜”：同一把 `_mt5_lock` 内对各腿读取 `symbol_info_tick`（已经满足），以本地单调时钟记录读取时刻，限制在 ≤ 1s。
  - “市场是否活着”：最后变化时间（经 P0-1 换算后）≤ 可配置上限（建议 60~120s）且终端 `connected`，以识别休市/断流。
  - 删除基于最后变化时间的跨腿 0.5s 偏差要求；如需防止跨腿不同步，应约束“读取时刻差”，不是“变化时刻差”。
- 测试：某腿 5s 未变化仍可入场；某腿 10 分钟未变化判为休市；读取间隔超过 1s 拒绝。

### P1-2 实时统计窗口在 10 个 tick 后从一年 H1 先验切换到“几秒钟的 tick”，σ 塌缩

- **证据等级：已复现（UTC 对照组）。** 影响纸面信号、图表 ±kσ 阈值线和 Z-score 显示；当前不直接触发实盘（自动任务使用绝对价差门槛）。
- `strategy.py` `SpreadSeries.stats()` 与 `StrategyRunner.on_tick()`：live 点满 10 个即改用 live 窗口（最多 500 点）。轮询周期 0.2s，10 点≈2 秒、500 点≈100 秒。复现：20 个恒定 tick 后 σ = 1e-12，而 H1 先验 σ = 0.020。任何微小价格变动都会给出极端 Z 值，阈值线收缩到当前价附近。
- 根因：混用了不同采样粒度（H1 先验、M1 图表、0.2s 轮询帧），统计口径没有统一时间粒度。
- 修复方案：实时帧先聚合为 M1 收盘值（与图表历史同粒度），统计在“M1 历史尾部 + 实时 M1”的连续序列上滚动计算（窗口以分钟为单位）；先验到实时的切换以“有效 M1 数 ≥ 窗口”判定；σ 设置相对先验的下限（例如 ≥ 先验 σ 的 10%）。
- 测试：恒定报价不使 σ 塌缩；先验→实时切换前后 Z 值连续；窗口按分钟计算与轮询频率无关。

### P2-1 worker 启动 120 秒“执行停滞”判定与“无限等待用户登录终端”相冲突

- **证据等级：代码确认（第五轮新增行为，现有测试 `test_health_startup_has_separate_deadline` 固化了它）。**
- `worker.py:256-334` `_connect()` 的文档承诺“账号不匹配则轮询等待，永不超时”；但 `worker_health.py` 在 `ready()` 之前若超过 120 秒即报告 `stalled=['initialize']`，`worker_pipe.py:71-75` 收到 ping 后停止 worker，父进程报“worker 执行停滞（initialize）”。
- 实际表现：先打开控制台、再去启动/登录 MT5 的用户，两分钟后会话失败，提示语指向“执行停滞”，误导排障。
- 修复方案：attach 等待循环每轮调用 `self._health.mark('initialize')`（等待是有进度的），使 120 秒只用于识别 `mt5.initialize` 本身卡死；`ready()` 已会替换进度字典，无需额外清理。UI 在等待登录期间显示“等待终端登录 login@server”。
- 测试：attach 循环 10 分钟仍健康；单次 initialize 超过阈值不推进则判停滞。

### P2-2 未绑定终端的账户互相触发“终端正被占用”

- **证据等级：代码确认。** `session_registry.py:26-30`：`realpath("")` 等于当前工作目录，两个未绑定终端的账户路径相等，第二个连接报“终端正被 X 使用”，真实原因（未绑定）被掩盖。
- 修复：路径为空时在 registry 直接拒绝并提示“请先绑定终端”，不进入冲突比较、不启动 worker。

### P2-3 `SessionRegistry.disconnect` 绕过会话锁操作私有状态

- **证据等级：代码确认。** `session_registry.py:43-44` 先 `session._transition(STOPPING)`，再在锁外 `session._cancel.set()`。两步之间 reader 线程若处理 worker 的 `EVENT_STATUS ok`，`_rep` 会尝试 STOPPING→CONNECTED，触发非法迁移 `ValueError`，被 `_reader` 的异常分支吞掉并结束读线程（当前碰巧无害，但非法迁移变成了控制流）。
- 修复：在 `AccountSession` 增加 `begin_stop()`，在 `self._lock` 内同时完成迁移与取消；registry 只调用公开方法。`_rep` 对 STOPPING 状态下的状态事件显式忽略。
- 测试：并发注入状态事件与 disconnect，断言无非法迁移、无读线程异常退出。

### P2-4 “失败但进程未退出”的会话没有出口

- **证据等级：代码确认。** `account_session.py:206-212`：进程未退出时置 FAILED 且不设置 `closed`；之后 `disconnect` 把它迁到 STOPPING，但 `_run` 线程已结束，`stop()` 只 join 一个已结束线程，`closed` 永不设置。registry 的 `connect` 一直返回这个会话，**该账户在应用重启前无法重连**。
- 修复：为该状态提供显式“回收”动作（再次 terminate/kill 并轮询 `proc.poll()`），成功后迁到 DISCONNECTED 并设置 `closed`；UI 提供“强制清理 worker”按钮并显示 PID，提醒在终端核对在途委托。
- 测试：模拟 `wait` 超时的进程，回收后可重新连接且只存在一个 worker。

### P2-5 账户上任何挂单都会阻止所有受管/自动开仓

- **证据等级：代码确认（产品规则问题）。** `managed_trading.py` `_preflight`：`orders_get()` 非空即拒绝。人工在其他品种挂一张限价单，或其他 EA 有挂单，本程序所有开仓都被阻断。
- 这是保守设计，但与“不干预人工仓位”的产品意图不一致，且 UI 没有给出明确原因。建议：仅当挂单涉及篮子品种或带本策略 magic 时阻断；其余挂单的潜在保证金占用计入预算（`order_calc_margin`）。若维持现规则，至少在任务面板显示“账户存在挂单（ticket 列表），自动入场已阻断”。需产品确认。

### P2-6 任一瞬时错误会暂停所有自动任务，断线恢复后必须逐个人工重新启用

- **证据等级：代码确认（设计取舍）。** `automated_trading.py` `pulse()` 外层异常（包括终端短暂断线时 `_account()` 抛出的身份/连接错误）对所有活跃任务调用 `_pause_error`。
- 建议区分：账本完整性/归属异常 → 暂停（现状正确）；连接中断、行情暂不可用 → 只跳过本轮并显示“等待恢复”，不清除执行开关。需产品确认是否接受“断线恢复后自动继续”。

### P2-7 可维护性：两个超大模块与遗留执行路径

- `ui/console.py` 2168 行、`worker.py` 1448 行，远超项目规范（≤800 行）。`console.py` 同时承担数据表、机会生成、图表编排、受管交易、日志和持仓；`worker.py` 中 `_SpreadTaker` 与 `_start_spread_algo/_on_algo_leg_result/_algo_*`（约 300 行）对应的 IPC 入口已全部禁用，仅作“迁移对照”。
- 建议：按标签页拆分 console（`console_data.py`、`console_strategy.py`、`console_managed.py`）；在下一次 Windows 验收通过后删除旧算法路径及其测试，避免死代码继续被误以为生效。

### P2-8 质量门槛尚未闭环

- ruff 28 项、mypy 90 项（见 A 节）。建议：先清零 ruff（多为机械修复，4 个未使用变量需确认是否遗漏逻辑）；mypy 对 Windows 专用模块使用 `sys.platform == "win32"` 分支或按模块 `ignore_errors`，对惰性属性补 `X | None` 标注，使 macOS/Linux CI 能得到“0 错误”的可用信号。
- 增加 CI：非 Qt 测试 + Qt offscreen 测试 + ruff + mypy，至少在 Linux 上运行；Windows runner 跑 PyInstaller 构建冒烟。

### P3 其他

- **P3-1 swap 快照时间口径不一致**：`worker.py:683` 存本地时间字符串，`db_service.py` 执行日志存 UTC ISO；且每个新 worker 首轮扫描会把全部品种当“变化”重新写入。建议统一 UTC ISO，并以数据库中的最后指纹去重。
- **P3-2 心跳 ping 走用户请求队列**：`account_session.py:168` 经 `_req_q`（容量 256），队列满时 ping 丢弃并每 5 秒记一条错误，20 秒后误判心跳超时。建议 writer 优先发送 ping（单独标志位，不入队）。
- **P3-3 `EventQueue.put` 每次线性扫描整队**寻找可合并快照，队列接近 5000 时每个 tick 做 5000 次 key 计算。当前规模可接受，若压测显示热点，改为 key→索引映射。
- **P3-4 Python 版本**：`pyproject.toml` 目标 3.12，本次在 3.14 下测试通过；Windows 发行版应锁定实际验证版本。

## C. 架构评估与建议

### C.1 连接状态管理

前几轮建立的结构是合理的，本轮不建议推倒重来：`SessionRegistry` 按 `login@server` 唯一持有会话；`ConnectionState` 不可变并有合法迁移表；事件带 `generation` 过滤迟到回调；连接状态、订阅确认、报价新鲜度三者已分开。158 个测试中的会话/传输用例实际通过。

剩余缺口集中在 P2-2/P2-3/P2-4（边界与回收）和 P0-1（“数据可用”判断依赖错误时钟）。修复后建议的状态语义：

```text
连接相位（session 持有）: disconnected → connecting → connected ⇄ reconnecting → stopping → disconnected
                                                   ↘ failed →（回收）→ disconnected
数据可用（console/automation 计算，非存储状态）:
  connected ∧ clock_ready ∧ 各腿本轮读取 ≤1s ∧ 各腿最后变化 ≤ 市场存活上限
```

不建议新增“data_ready”存储字段：它是由时钟、报价与连接派生的值，存成状态会产生第二个事实来源。

### C.2 图表

**结论：保留 pyqtgraph，修复数据契约。** 依据：

1. 本轮复现的图表核心故障（实时点不出现、阈值线塌缩）分别来自时钟不一致（P0-1）与统计粒度（P1-2），换库不能解决，而且迁移 Web 图表会引入 QtWebEngine、JS 桥和第二套生命周期。
2. 第五轮的屏幕分桶峰谷降采样已解决不等间隔数据的绘制量问题，替换门槛（Windows 实测仍卡顿）已在 `PERFORMANCE-2026-10-15.md` 中定义，本轮认同该门槛。
3. 需要补充的契约：图表接收的所有时间均为真实 UTC epoch（由 P0-1 在 worker 边界保证）；时间轴标注时区（可选显示 broker 时区，便于与 MT5 终端图表对照）；历史重拉时以换算后的时间合并实时尾部。

### C.3 数据契约（建议写入代码注释与测试）

| 字段 | 唯一来源 | 时间基准 | 消费者 |
| --- | --- | --- | --- |
| tick `time_msc`、rates `t` | worker（换算后） | 真实 UTC epoch | console、chart、runner、automation |
| 本轮读取时刻 | worker/automation | 本地单调时钟 | 仅新鲜度判断，不跨进程 |
| `server_utc_offset` | worker `BrokerClock` | 秒 | UI 显示、诊断 |
| 账本 `created`、执行日志 | worker / db_service | 真实 UTC | 持有期限、审计 |

## D. 建议实施顺序

1. **先协调工作树**：确认没有其他代理同时修改源码，并对当前工作树做一次提交或打快照（目前 27 个已修改文件 + 大量未跟踪文件，没有任何提交可回退）。
2. **P0-1 时间契约**：`broker_clock.py` + worker 出站换算 + runner 用 broker 帧时间 + 测试。先在 Windows 演示账户记录一次偏移，确认前提。
3. **P1-1、P1-2**：自动交易新鲜度模型与统计粒度，均为纯逻辑，可在 macOS 上完整测试。
4. **P2-1 至 P2-4**：会话边界与回收，补并发测试。
5. **P2-5、P2-6**：需产品决定规则后实施。
6. **质量门槛**：ruff 清零、mypy 可用化、CI。
7. **P2-7 拆分与删除遗留路径**：在 Windows 验收通过后进行，避免在未验证的基线上做大规模搬迁。

## E. 遗留风险与需要外部环境确认的事项

- 所有 MT5/broker 行为（时间偏移、成交回执、comment 保留、IOC、`position.identifier`、对冲账户模式）仍须在 Windows 演示账户验证，清单见 `WINDOWS-VALIDATION.md`；建议将 P0-1 的偏移记录加入该清单首项。
- PyInstaller 构建、Windows 文件锁、冻结程序 worker 入口未执行。
- 依赖未锁定，许可证清单未生成。
- 本轮未修改源码；上述问题在实施前均仍存在于当前工作树。

## F. 最终状态

- 已完成：对连接、事件、图表、策略统计、受管执行和自动交易路径的独立代码逻辑复审；定位图表与行情新鲜度的共同根因（broker 时钟）；给出带测试与验收标准的修复规格。收到“仅审查”确认之前跑过的纯逻辑测试（158 通过）、ruff、mypy 结果作为参考附在 A 节。
- 未完成：任何源码修复（因并发修改而有意延后）、Qt/图表测试、Windows/MT5 运行验证、构建。
- 项目**尚未达到**原始验收标准：P0-1 意味着在 GMT+2/+3 broker 上实时图表、新鲜度判断、手动受管开仓和自动交易实际上不可用；该结论需在目标 broker 上用一行日志确认偏移后即可坐实。
