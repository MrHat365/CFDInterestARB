# CFDInterestARB — MT5 价差与套息桌面工具

Python / PySide6 / pyqtgraph 桌面工具，连接 Windows MetaTrader 5 终端，展示账户、隔夜利息、外汇闭环交易机会与合成价差。图表默认纸面模式，后台实盘任务具有独立的入场/出场开关，默认均暂停。

**当前审计修改尚未经过 Windows 运行验收。** 2026-10-15 按用户要求只进行了静态审查、代码修复和测试源码补充，没有启动应用、MT5 或执行测试。详见 [审计报告](docs/AUDIT-2026-10-15.md) 、[第二轮修复报告](docs/FOLLOWUP-2026-10-15.md) 和 [Windows 验证清单](docs/WINDOWS-VALIDATION.md)。

## 当前能力与边界

- 账户/终端绑定与扫描；应用级会话注册表；每账户独立 worker。
- 连接身份以 login+server 校验，终端须已由用户启动并登录；程序不提供自动登录、密码登录或终端重启功能。
- 连接状态、订阅确认与报价新鲜度分别处理；旧会话事件和旧历史请求不能更新当前视图。
- 合成价差折线：M1历史、H1统计先验、实时更新、窗口选择、缩放/平移、恢复跟随、十字光标、可拖阈值线。
- 纸面网格与信号记录；手动受管开平仓；最多32个配对后台任务，各自固定买卖方向、目标层数、入场/出场条件及独立开关。**仅支持对冲账户中的FOREX合约，代码尚未通过运行验收。** 持仓层数来自实际成交账本，共享账户总手数和保证金预算。
- 原始账户持仓/成交查询；swap变更和实际执行回执写入SQLite。
- 策略归属以开仓成交的position_id及当前position.identifier核验；不会平人工仓。重启恢复账本，不恢复自动监测意图。未知委托只能根据明确历史证据恢复，不能凭“查不到”重发。

## 环境与启动（仅 Windows）

需要 Python 3.12+、已安装且已登录的 MT5 终端。MetaTrader5 Python API 不支持本地 macOS。依赖仍采用原仓库版本范围，本次没有更换图表库或升级生产依赖。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python app.py
```

使用流程：手动登录 MT5 → 在账户管理中添加账户并绑定终端 → 打开控制台 → 等待账户连接和swap快照 → 选择组合 → 加载历史 → 开始纸面监测。

账户窗口不再收集新密码，也不向 worker传递密码；编辑时可勾选删除此账户的旧密码。旧数据库字段保留兼容，未自动批量清除用户数据。编辑已有账户时登录号/服务器不变，修改终端绑定前需要先断开会话。

## 生命周期

- `app.py` 创建 `SessionRegistry`，按 `login@server` 管理会话；同账户重复连接重用，同终端不允许两个活跃账户worker竞争。
- `AccountSession.start()` 在后台建立经认证的TCP连接，每次会话使用独立generation。合法状态为 disconnected / connecting / connected / reconnecting / stopping / failed。
- 关闭控制台只关闭视图并停止监测；连接继续由应用管理。重新打开从会话快照恢复显示。
- “断开会话”停止worker但保留视图订阅，清理结束后可重连。断线或行情过期会暂停监测，恢复后由用户重新开始。
- 退出主窗口停止所有会话，等待在途算法、排空持久化事件，再关闭SQLite。强制退出的未知委托结果必须在终端核对。
- 应用重启恢复账户/终端配置，不恢复“已连接”标志，也不自动恢复交易意图。

心跳确认IPC存活；账户状态确认终端连接身份；数据可用还需各腿报价有效、时龄不超过15秒、腿间时间差不超过5秒。时间阈值是工程默认，需结合Windows校钟、broker和交易时段验证。

## 数据与图表

典型三角示例：

```text
合成买 USDJPY = USDCHF Ask × CHFJPY Ask
价差 = USDJPY Bid − 合成买 USDJPY
```

定价幂和手数权重分开。历史使用每腿bid close并用当前点差近似ask，不能当成真实历史可成交报价。H1统计覆盖取决于终端实际可用历史，不保证加载固定年份。

`series_data.py` 负责有限值校验、排序、去重、同刻替换、容量上限；历史请求通过generation/request_id/legs校验后进入runner和chart。M1折线缓冲最多200,000点，容量固定；无效旧live点不会覆盖最新点。相同组合刷新历史时保留新于历史末尾的live尾部。

pyqtgraph实例生命周期与widget一致，250ms合并绘制；鼠标手动缩放/平移暂停自动跟随，点击“跟随最新”或切窗口恢复。参考线更新复用对象。时间轴/十字光标显示UTC。旧K线接口仅兼容占位，当前产品是折线图。

## 受管实盘与恢复

手动实盘操作先设置风险上限、取消图表纸面模式并核验策略账本。自动交易使用独立的“后台自动交易”页：从当前组合新建任务，设置实际手数、目标层数、价差/成本/净息门槛、出场净利润及可选的价差/时间/亏损条件；保存后分别启用入场和出场。旧图表Z-score网格仅用于纸面观察，不再触发实盘。详见[后台自动交易说明](docs/AUTOMATION-2026-10-15.md)。

自动任务不依赖控制台窗口；关闭窗口或切换组合不会停止它。图表纸面按钮及Escape不控制后台实盘任务。需要停止时使用任务开关或“全部暂停”；断开账户/退出程序停止执行，重启后恢复配置但不恢复执行开关。失效报价等待刷新，账户/成交异常暂停；未知结果不自动重试。

发单路径：账户身份/对冲模式核验 → 真实层数/账户总手数/保证金预算 → 每腿IOC与broker order_check → 步长取整后的逐币种残余检查 → 提交持久订单意图 → broker调用 → 持久回执 → position_id归属核验。每腿开仓前的整个篮子使用FOREX合约大小、当前买卖侧报价检查残余，各币种不得超过最大单腿流的5%。这不是损失或回撤上限。

主动腿部分成交后按实际比例缩放被动腿并再次检查取整残余；对冲不足/未知回执会阻断继续开仓。实盘层数来自确认的账本批次，不从信号条数推断。平仓只按当前策略方向的已核验持仓逐ticket执行，部分平仓后暂停，可核验后继续平剩余归属仓位。

“核验策略账本”执行只读恢复。“平策略持仓…”可选择重启前留下的策略方向，无须重新生成完全相同的机会。未知发单结果只有在唯一comment、magic、品种、方向、终态历史订单和成交量都吻合时才能恢复；broker修改comment或历史缺失时保持阻断。绝不自动重发或自动接管人工仓位。

`managed_requests` / `managed_batches` 保存请求去重、订单意图、回执与批次归属；`automation_tasks`保存任务配置与开关，每次实际变更使用FULL同步。worker订单线程在生命周期内持有账户执行文件锁，阻止同库同账户的第二个后台调度者；进程崩溃后锁自动释放。不同数据库副本不共享此锁，不能并行操作同一账户。

旧的无归属order/spread_algo IPC请求已拒绝，避免绕过新账本和风控；内部旧算法辅助代码仍保留供迁移对照。历史未入账的仓位不自动认领。源码运行和exe运行必须使用同一份完整数据库，不能只迁移accounts表。

## 模块

```text
app.py                       应用根与确定的退出顺序
mt5_trader/
  session_registry.py        应用级会话所有权、账户/终端隔离
  connection_state.py        不可变连接状态和合法迁移
  account_session.py         子进程/认证TCP/心跳/请求与事件代次
  worker_pipe.py             worker传输与独立stop/ping处理
  worker.py                  MT5连接、行情、历史、订单、成交复核
  event_queue.py             有界队列：快照替换、关键事件背压
  event_engine.py            事件分发与线程安全订阅
  series_data.py             时序规范化、有界缓冲、报价新鲜度
  managed_trading.py         持久意图/归属账本、受管开平仓、恢复与预算
  automated_trading.py       多配对后台任务、经济门槛、独立入场/出场控制
  strategy.py                H1先验/live统计、图表与纸面网格信号
  spreads.py                 组合定价与手数换算
  opportunities.py / weights.py  闭环机会、中性权重、swap换算
  database.py / accounts.py   SQLite与账户配置
  services/db_service.py     swap与执行回执持久化
  ui/                        Qt账户视图、控制台、图表、通知
```

源码SQLite沿用 `mt5_trader/mt5_trader.db`；冻结exe使用 `%LOCALAPPDATA%/CFDInterestARB/mt5_trader.db`，开启WAL。首次部署须在关闭应用后迁移完整数据库（含受管账本）；不得同时运行旧库和副本。扫描不会删除失效终端绑定。`trades` 包含 paper/signal/execution 记录；只有execution来自worker回执，仍需看ok/result，不能将信号当成交。数据库、虚拟环境与本地归档不入库。

## 测试与验证

已有测试与新增测试均保留。测试函数数量及静态证据见[静态检查记录](docs/static-validation.txt)，**不是通过数**。新测试覆盖状态/IPC、数据与策略、注入MT5 API、真实Qt组件、后台多任务调度与成本门槛；本次没有运行。

Windows准备验证环境后：

```powershell
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python -m pytest tests -q
.\.venv\Scripts\python -m ruff check app.py mt5_trader tests
.\.venv\Scripts\python -m mypy --ignore-missing-imports app.py mt5_trader
```

已新增pyproject检查配置和CFDInterestARB.spec，但未运行任何质量工具或生产构建；源码语法通过不等于类型检查、Lint、GUI或构建通过。旧 `tests/run_smoke.py` 仅覆盖原四个纯逻辑测试模块，不代表新增回归已执行。

## Windows打包（配置已写，未执行）

```powershell
.\.venv\Scripts\python -m pip install -r requirements-build.txt
.\.venv\Scripts\python -m PyInstaller --clean --noconfirm CFDInterestARB.spec
```

采用onedir分发，保留console型子进程的stdin/stderr以供认证IPC。冻结exe以 `--worker <port>` 进入worker，不递归启动GUI。数据库必须位于用户可写目录，不能放入打包临时目录。

依赖仍未锁定：须先在Windows运行测试与打包验收，再保存实际 `pip freeze`、用 `pip-licenses --with-urls --with-license-file` 生成第三方许可清单，并审阅Qt及全部随包许可证。此处没有伪造经验证的版本锁或许可证结论。

## 性能与后台稳定性专项（源码已改，运行未验收）

保留现有UI和第三方依赖，图表内部改用按屏幕时间分桶保留真实峰谷/首尾点的显示数据，33ms合并绘制请求；快照入队立即合并、账本索引只扫描活动批次、任务表复用单元格。worker新增执行进度健康检查、发送阻塞监视和有时限的回执排空。33ms不是端到端延迟承诺；全部行为仍待Windows验证。

完整候选库比较、替换门槛、剩余锁竞争/服务化边界与72小时验收方案见[性能专项报告](docs/PERFORMANCE-2026-10-15.md)。当前后台指关闭账户控制台后继续执行；退出整个主程序仍停止worker，尚不是独立Windows服务。任务进程异常后不会自动恢复交易开关或重发未知委托。

### 第六轮修复：时间、统计与后台会话

最新审查的源码处理结果见 [REVIEW-FIXES-2026-10-15.md](docs/REVIEW-FIXES-2026-10-15.md)。保留原界面与 pyqtgraph；滚动统计窗口单位现为分钟，采用已收盘 M1 与 H1 先验平滑混合。后台事件快照以索引合并，心跳优先于用户请求；失败 worker 可在账户页清理后重连。

账户“报价时间基准”默认 **UTC**。只有核实 broker 的 API tick 和历史 epoch 存在同一非标准偏移后，才配置如 `UTC+03:00` 或正确的 IANA 时区；终端图表显示的服务器时区不等于 API 必须换算。软件不根据旧报价自动猜时区。市场报价最大年龄默认 60 秒（可设 5–120 秒），与跨腿本轮读取最长 1 秒分别检查。IANA 夏令时需要声明的 `tzdata` 依赖，冻结打包已配置携带数据。

按用户确认保留：账户任何挂单阻止新增仓位；断线导致任务暂停后，恢复连接也须人工分别启用入场/出场。清理 worker 后须先核对在途委托。所有本轮回归仅编写源码，未运行；Python 3.12 的手动 CI 配置也未触发，不能据此认定已经通过实盘或长期性能验收。
