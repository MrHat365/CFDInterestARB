# CFDInterestARB — MT5 价差套利 + 套息桌面平台

连接 MetaTrader 5 终端的**价差套利 / 隔夜套息**交易桌面工具（PySide6 + pyqtgraph + MetaTrader5 API）。

> 核心思路：把 USD 中性化的多腿组合做成一个**合成价差**，方向上"卖实际腿、买合成腿"，用价差的统计偏离（kσ 网格分层）进行小单进场；组合的**隔夜利息差**（swap）才是主要收益来源，价差收敛是入场信号。

---

## 目录

- [核心功能](#核心功能)
- [系统架构](#系统架构)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [使用流程（先后顺序）](#使用流程先后顺序)
- [价差公式与口径](#价差公式与口径)
- [策略引擎](#策略引擎run_signal)
- [价差算法执行](#价差算法执行vnpy-spreadtakeralgo-语义)
- [图表（v3）](#图表v3固定窗口折线)
- [进程与线程模型](#进程与线程模型)
- [事件总线](#事件总线)
- [数据与持久化](#数据与持久化)
- [测试](#测试)
- [监控与运维指标](#监控与运维指标)
- [安全与实盘注意事项](#安全与实盘注意事项)
- [提交历史（里程碑）](#提交历史里程碑)

---

## 核心功能

| 模块 | 能力 |
|---|---|
| 账户管理 | MT5 终端扫描（注册表/文件系统）、账户-终端绑定、一键打开控制台；**唯一连接路线 = 用户先在 MT5 客户端手动登录 → 绑定该终端 → worker 纯 attach**（绝不拉起/重启/写凭据） |
| 行情接入 | 每账户一个 worker 子进程，attach 到已登录终端；ticks 0.2s 轮询、账户/持仓/挂单 1s 快照；断线检测（连续 3 次无数据）→ 30s 自动重连；UI↔worker 显式心跳（5s ping / 15s 无回包判死） |
| 隔夜利息 | 全市场 swap 指纹比对（60s 周期，变化才推送）；`swap_to_usd_per_day` 将 9 种 swap_mode 统一换算为 USD/每天 |
| 机会发现 | 品种多重图上枚举 2~5 腿闭环；USD 中性非负权重（最小奇异向量，5% 残余容差）；每组合给出"净息 USD/天"评级 |
| 图表 | **确定性固定窗口折线**（M1 合成收盘）：近1小时/8/24小时/3天/7天/全部 一键切换；均值/±kσ 可拖阈值线/层间网格线/1y·6m 极值线；Y 轴定点小数（**全站无科学计数法**） |
| 策略 | Z-score 统计（H1 全窗先验 + 实时滚动窗口两段口径）；kσ 网格分层小单；每层手数 = USD 中性权重 × 统一每层名义；方向独立计数 + 最大持仓约束 |
| 下单 | vnpy `SpreadTakerAlgo` 语义：**主动腿先吃单（IOC）→ 成交回调立即对冲被动腿**；任一腿被拒 → 立即上报（critical 弹窗）+ 30s 停滞告警 + 5s 后裸腿复核（positions 对比 targets）；REQUOTE/PRICE_OFF 类瞬态失败自动重报价重试（≤2 次）；订单在独立线程执行（不阻塞请求主循环） |
| 屏幕 | 交易机会 / 规则·隔夜利息合并表 / 策略控制台（图表 + 持仓/成交/信号/日志四子页）；QAbstractTableModel 数据/视图分离；toast 限流合并 |

---

## 系统架构

```
┌────────── 主进程（PySide6 Qt UI）──────────────────────────────┐
│ app.py: Database(SQLite) + EventEngine(单分发线程, 有界队列)   │
│ MainWindow ─ AccountsView ─ AccountConsole × N（每账户一窗）   │
│   · EventBridge：EventEngine(后台线程) → Qt Signal(主线程)     │
│   · 数据/视图分离：QAbstractTableModel（MiniTable 系列）        │
│   · 重计算全部后台化：机会生成(generate) 后台线程+信号桥回填    │
│ AccountSession：subprocess 拉起 worker 子进程                   │
│   + 127.0.0.1 TCP JSON-lines 双向管道（req 队列/rep 读线程）    │
│   + 心跳（5s ping / 15s 死检）+ stderr drain（防子进程写满死锁）│
└──────────────┬────────────────────────────────────────────────┘
               │ JSON-lines over TCP（每账户一条连接）
┌──────────────┴──────── 子进程 worker（每账户独立）─────────────┐
│ worker.py _Worker:                                             │
│   主循环线程: 请求分发（subscribe/rules/deals/历史/stop/ping）  │
│   轮询线程:   ticks(0.2s) + 快照(1s) + 断线检测/自动重连        │
│   swap线程:   全市场 swap 指纹比对(周期可配)                    │
│   history线程: M1(14400)+H1(9600) 拉取（latest-wins 队列）      │
│   order线程:   订单/价差算法（串行发单 + REQUOTE 重试）         │
│   订单执行器 _SpreadTaker: active→hedge→done 状态机+裸腿审计    │
│   所有 mt5.* 调用统一串行于一把 RLock（API 非线程安全）          │
└────────────────────────────────────────────────────────────────┘
```

依赖方向（自外向内）：`ui → services → strategy/spreads/opportunities/weights → database → constants`。UI 层不 import worker；策略/公式层不依赖 UI（可独立测试）。

## 目录结构

```
app.py                     主入口（启动 Database/EventEngine/MainWindow）
requirements.txt           依赖清单（见下方"快速开始"）
mt5_trader/
  __init__.py
  constants.py             EVENT_* 事件名、swap_mode 名称、MAGIC 等
  database.py              SQLite（WAL）账户/终端/swap快照/成交持久化
  accounts.py              AccountsStore（账户 CRUD）
  terminals.py             MT5 终端发现（注册表+文件系统）与实例管理
  event_engine.py          事件总线（单分发线程、有界队列、丢最旧计数）
  account_session.py       账户会话：subprocess worker + TCP 管道 + 心跳
  worker_pipe.py           worker 子进程入口（python -m mt5_trader.worker_pipe <port>）
  worker.py                _Worker 主逻辑（行情/换仓/历史/算法/审计）
  spreads.py               价差腿/组合、乘法合成价、目标腿、手数换算
  weights.py               USD 中性权重求解（SVD/投影迭代）、swap→USD/天
  opportunities.py         多腿闭环枚举 → 交易机会生成
  strategy.py              StrategyRunner / SpreadSeries（先验+滚动统计）/ SignalEngine（kσ 网格）
  services/db_service.py   EVENT_SWAPS → swap_snapshots 增量入库
  ui/
    main_window.py         主窗口
    accounts_view.py       账户管理（TerminalScanThread 后台扫描）
    account_dialog.py      新增/编辑账户
    console.py             AccountConsole（策略控制台主界面 + 四子页）
    chart.py               SpreadChart v3（固定窗口实时折线）
    kpi.py                 KPI 卡片 / 状态片 / 横幅
    notify.py              Toast 通知（同类合并 + 数量上限）
    theme.py               暗色主题（国际惯例：绿涨 #16c784 / 红跌 #ea3943 / 灰零 #8b93a3）
    base.py                EventBridge（事件总线→Qt Signal）
tests/                     pytest 61 用例（spreads/strategy/opportunities/db）+ 无 pytest 兜底 runner
mt5_trader/mt5_trader.db   SQLite 数据文件（gitignore，不入库）
paused_migration/          DearPyGui 迁移尝试的归档（已弃用，仅存档）
```

## 快速开始

### 环境要求

- Windows 10/11（MetaTrader5 Python API 仅 Windows）
- Python 3.12+（开发环境 3.14.2）
- MetaTrader 5 终端（已安装并可登录目标账户）

### 安装与启动

```powershell
pip install -r requirements.txt
python app.py
```

依赖（全部轻量，无 pandas/heavy 框架）：

```
MetaTrader5>=5.0.4529    # 官方 Python API（仅 Windows）
PySide6>=6.6             # Qt 绑定（GUI）
numpy>=1.26              # 数值/向量化
pyqtgraph>=0.13.7        # 图表
psutil>=5.9              # 进程/终端发现辅助
```

### 连接流程（顺序敏感，务必按序）

1. **先**在 MT5 客户端手动登录目标账户；
2. 在「账户管理」里登记账户并**绑定**到该终端路径（扫描或手动）；
3. 打开控制台 → worker **attach** 到已登录终端 → 校验 login 匹配；
   （若终端当前登录的不是该账户，worker 会持续等待人工切换，永不拉起/重启终端）

连接成功后：swap 首轮数据到达（约间隔 60s 更新）→ **自动生成机会并载入首个最优组合** → M1 价差折线自动出现 → 可点「开始监测」。

## 使用流程（先后顺序）

```
启动 app.py → 账户管理页 → （先在 MT5 客户端登录）→ 绑定终端 → 打开控制台
→ 连接（attach，等待 worker 上线）
→ 规则·隔夜利息页：开启自动检测（swap 全表入库）
→ 自动载入组合（或去【交易机会】页手动挑选）
→ 【策略控制台】：统计先验（H1）→ M1 折线 → 阈值/极值线 → （开始就绪）
→ 点「开始监测」→ kσ 网格分层信号（纸面模式默认只记录不下单）
```

数据的**顺序保证**（实现级）：

1. 历史事件**按组合归属校验**（`(symbol, side, weight)` 签名）——旧组合晚到的历史直接丢弃；
2. 历史请求节流**按组合分别 60s**——切新组合立即放行、同组合免重复；
3. 机会计算在后台线程完成后**不再覆盖用户已手动选择的组合**；
4. 历史数据的处理顺序固定：**统计先验（H1）→ 折线数据（M1）→ 阈值线 → 极值线 → 就绪**（避免空图先蹦阈值线）。

## 价差公式与口径

**乘法合成（外汇交叉盘口径）**：

```
合成买入 USDJPY (Ask) = USDCHF Ask × CHFJPY Ask
价差 = 实际卖出 USDJPY (Bid) − 合成买入 USDJPY (Ask)
```

推广到任意腿数（对数线性实现，幂取 ±1 按腿方向）：

```
合成价 = exp( Σ s_i · log(侧价_i) )      [s_i=+1 买腿(取 ask)，s_i=−1 卖腿(取 bid)]
价差  = 目标腿 Bid − 合成价               [目标腿 = 卖出方向 |权重| 最大腿]
```

要点：

- **合成价与手数解耦**：定价幂是三角交叉面的定量（±1），手数比例走 `SpreadLeg.multiplier`（来自 USD 中性权重，最大腿归一）；
- 历史序列 = 各腿 H1/M1 **bid 基 close** + 每腿当前点差近似 ask 侧；
- 图表主视觉 = **M1 合成收盘折线**；统计先验（mean/std/n）用 H1 全窗（≥1 年）；1y/6m 极值线只作参考注记。

## 策略引擎（Runner/Signal）

- `SpreadSeries`：环形序列最大 200k 点；`stats()` 滚动窗 mean/std/z（numpy 向量化）；`extremes()` 1y/6m 长窗极值（60s TTL 缓存）；
- 两段口径：live 点 <10 且有先验 → 用 H1 先验 mean/std；≥10 点 → 改用滚动窗口；
- `SignalEngine`：上行 z 超过 `mean + (k + i·grid_step)·std` → 卖出方向第 i+1 层；下行对称（买入进场）；同一方向最多 `max_positions` 层，非同向独立计数；
- 纸面模式（默认勾选）：只记录信号/分层，不下单；实盘模式每层触发即通过价差算法下单。

## 价差算法执行（vnpy SpreadTakerAlgo 语义）

```
请求 {action: spread_algo, legs:[{symbol, side, volume}]}
  → 订单线程（独立，串行发单）
  → 主动腿吃单（IOC 市价，重报价类失败自动重试≤2 次）
     ├─ 成交 → 事件 active → 立即对冲其余腿（hedge）
     │         全部腿成交 → done → 5s 后裸腿复核（positions_get 对比 targets）
     └─ 被拒（结构性错误）→ 即时 failed + UI critical 弹窗（防裸腿，绝不自动重复下单）
stalled：任一阶段 30s 无回执 → 告警 + UI 提示人工介入
audit_ok   / audit_mismatch：裸腿复核结果（差异 = 步长/2 容差）
订单在专用线程执行：order_send 网络等待不再阻塞主请求循环（下单延迟从秒级降到毫秒级）
```

## 图表（v3 固定窗口折线）

**设计立场：确定性 > 自由度**（历史多版问题的根源是 pyqtgraph 有状态自动机之间的互搏）。

- 数据：单一 numpy 平行缓冲（`_buf_t/_buf_v`），只追加；
- 视图：**显式量度**——每次重绘 `setXRange(窗口)` + `setYRange(窗口内数据±8%)`；
- **不用 autoRange / setLimits / pan / zoom**（鼠标禁用），任何时刻重绘函数都是同一纯函数；
- 窗口选择：图表左上角下拉框（近1小时/8小时/24小时/3天/7天/全部），默认近 8 小时；
- 重绘：250ms 合并定时器 + 数据替换/窗口切换立即；250ms 频次下 14k 点切片 setData 几毫秒；
- 十字光标：只读、40ms 节流、读数钉在视口右上角；
- Y 轴：定点小数控轴 `_FixedFmtAxis`（小数位由刻度间隔自动推导，**全站禁科学计数法**）；
- 阈值线可拖动（注：拖动调 k 与 spin 双向同步）。

## 进程与线程模型

| 线程/进程 | 职责 | 通信 |
|---|---|---|
| 主进程 EventEngine | 事件分发（有界队列 5000，满丢最旧+计数） | 内存事件总线 |
| AccountSession req/rep 线程×N | JSON-lines 管道读写 | TCP + queue.Queue |
| 心跳线程×N | 5s ping / 15s 死检 | EVENT_STATUS 告警 |
| worker 主循环 | 请求分发（deserialize → dispatch） | req_q / rep_q |
| worker 轮询 | ticks 0.2s + 快照 1s（持 `_mt5_lock`） | rep_q |
| worker swap | 60s 全市场比对 | rep_q |
| worker history | M1/H1 拉取（latest-wins） | rep_q |
| worker order | 订单/算法串行发单 | rep_q |
| UI 侧后台线程 | 终端扫描 / 机会计算 / session stop | Qt Signal（`_CallInMain`）跨线程回主线程 |

退出链：console.close → bridge 反注册 → 后台 stop 线程（8s join+terminate）→ `app.py` atexit 兜底 join `stop-*` 线程 → EventEngine stop → DB close。

## 事件总线

| 事件 | 载荷要点 | 方向 |
|---|---|---|
| `eSessionStatus` | {ok,msg} | worker→UI |
| `eAccount/ePosition/eOrder/eTick/eDeal/eSymbols` | 行情与账户快照 | worker→UI |
| `eRules/eSwaps` | 合约规则/swap 全表+变化集+fx_rates | worker→UI |
| `eSpreadHistory` | {datasets:{m1,h1:{t,v}}, legs} | worker→UI |
| `eSpreadAlgo` | {algoid, stage, msg}（active/hedge/done/failed/stalled/audit_ok/audit_mismatch） | worker→UI |
| `eOrderResult` | {ok,msg,request}（含重试计数文本） | worker→UI |
| `eSymbolSubscribe/eOrderRequest/eDealRequest/eAlgoRequest/eLog` | 请求/日志 | UI→worker / 任意 |
| （未入 constants）`ePong` | 心跳应答（worker 内本地常量并与 account_session 对齐） | worker→UI |

## 数据与持久化

SQLite（WAL 模式，`busy_timeout=3000`，单连接+全局锁，全部方法签名稳定）：

- `terminals`：扫描/绑定终端路径
- `accounts`：账户凭据（**仅本地 DB**，从不起终端）
- `swap_snapshots`：隔夜利息快照（变化才写）
- `trades` / `spread_points`：纸面/实盘记录与价差点位缓存

## 测试

```powershell
python -m pytest tests -q        # 61 用例（纯逻辑层：spreads/strategy/opportunities/db）
python tests/run_smoke.py        # 无 pytest 环境的兜底 runner（退出码 0=全过）
```

关键公式都有断言（乘法合成、目标腿选择、kσ 网格触发、USD 中性闭环、DB 读写）；UI 层另有离线冒烟脚本（`QT_QPA_PLATFORM=offscreen`）覆盖"连接→自动载入→历史→live"全链路。

## 监控与运维指标

- UI 响应：机会计算后台化（UI 线程 0ms，原 442ms）；tick 路径纯 numpy 计算；
- 行情到界面：<200ms（0.2s 轮询 + 事件分发 + Signal 桥）；
- 重绘：250ms 合并节流，14k 点切片 setData ~2-3ms；
- 内存：序列 200k 上限、信号 600 行上限、事件队列 5000 上限、rep 管道 2048 上限（均满即丢最旧+计数）；
- 线程数恒定：5 worker 线程 + 会话 3 线程，无泄漏增长；
- 长稳立项：心跳（断连即告警）、stderr drain（防子进程死锁）、断线自动重连（30s 周期）。

## 安全与实盘注意事项

- **先纸面后实盘**：纸面模式勾选（默认）只记录不下单；切实盘前先小手数验证；
- 账户密码仅存本地 SQLite；仓库**不含**任何凭据/账户数据；
- `paused_migration/` 为已弃用的 DearPyGui 迁移归档（保留供未来参考），与运行无关；
- 价差套利本身**存在残余敞口**（USD 中性 ≠ 全货币中性）；实盘前请对手数、点差与阈值做小规模验证；
- 本项目为个人研究工具，不构成投资建议。

## 提交历史（里程碑）

```
03ed6f5  baseline: 阶段0诊断后的优化前快照
76bdb62  第一批并行子任务（worker后台历史/熔断/会话健壮/策略向量化/console后台化/db WAL/tests 底座）
c7e8c3e  第二批（订单独立线程+REQUOTE重试+心跳 / 表格 Model 化 / chart 缓冲复用+notify 限流 / 退出加固+EE 有界）
c931aa6  图表纯折线方案（K 线退役）+ Y 轴定点小数（根除科学计数法）
3192a8c  图表时序修复（历史归属校验/分组合节流/用户选择优先/视图跟随右缘）
19599f1  图表 v3 系统性重写（固定窗口/显式坐标/零交互状态机）← 当前
```
