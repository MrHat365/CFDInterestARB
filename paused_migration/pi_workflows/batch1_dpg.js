// 迁移批1：DPG 骨架 + 账户管理窗口（与我批0 worker 改动文件零交集）
const results = await runs.all([
  {
    key: "dpg-skeleton",
    agent: "worker",
    task: `项目: C:\\Users\\52764\\Desktop\\ARBInterest 的 MT5 交易工具。
【背景】正在把整套 GUI 从 PySide6 迁移到 DearPyGui 2.3.1（已安装、cp314 可用）。决策:
- 用户决定"原生 C GPU K线 + 价差线双画面叠加"（下批做，本批不做图）；
- 旧 Qt UI 本批【只读参考、不得修改/删除】（批3 才删除）；
- worker/事件引擎/spreads/weights 算法核心与 UI 无关，且同时有另一代理在改 mt5_trader/worker.py 与 mt5_trader/account_session.py 与 mt5_trader/constants.py ——【你绝对不得修改这些文件！】
【你独占的新文件】ground: mt5_trader/ui_dpg/ 目录（新建包）:
    mt5_trader/ui_dpg/__init__.py
    mt5_trader/ui_dpg/theme.py        # 名字与旧包不冲突（旧的是 mt5_trader/ui/）
    mt5_trader/ui_dpg/fonts.py
    mt5_trader/ui_dpg/state.py
    mt5_trader/ui_dpg/accounts_window.py
    mt5_trader/ui_dpg/event_bridge.py
    app_dpg.py（项目根，入口）
【先只读参考】mt5_trader/ui/theme.py 里的 COLORS token 值、mt5_trader/ui/accounts_view.py（布局/行为语义）、mt5_trader/ui/main_window.py、mt5_trader/ui/account_dialog.py（添加账户对话框字段: login(账户号)/password(密码)/server(服务器)/terminal_id(绑定终端，下拉来自 store.terminals()，最后一项"(自动/默认终端)"）与 terminal_path)、mt5_trader/accounts.py（AccountsStore API: all/add/remove/terminals/rebind_terminal）、mt5_trader/database.py、mt5_trader/event_engine.py（Event/EventEngine）、app.py（原 main() 启动序列）。
【DearPyGui 要点】(2.3.1)
1) dpg.create_context() 先于一切；字体: with dpg.font_registry(): dpg.add_font("C:/Windows/Fonts/msyh.ttc", 14) 设为全局 font —— 必须，否则中文全部缺字。另外加 bold 变体供标题。
2) 全局主题: with dpg.theme(theme tag) + dpg.theme_color(DpgTheme.CATEGORY_*)… 对映 COLORS:
   BG #0e1116 / PANEL #151a21 / TABLE #12161d / RAISED #1c222c / BORDER #2a3140 / TXT #dde3ec / MUTED #8b93a3 / UP #16c784 / DOWN #ea3943 / WARN #f0b90b / ACCENT #4d8df0 / ZEBRA #171c25。
   dpg.theme_color 有 matplotlib color loads (mThemeColor) 具体名: dpg.mvThemeCat_Core / dpg.mvThemeCat_Plots —— 按钮 FrameBgHovered、TableHeaderBackground、PlotAxisText 等逐项映射；写出清晰映射表。
3) 数据持有【核心思想】: DPG 无 Qt 那种 widget 状态，必须 state-first:
   state.py 定义 DpgState 单例，持 accounts 列表/scan线程等，UI 拿数据渲染；写入走 state 方法，UI 更新走 dpg.configure_item/set_value。
4) 事件桥 event_bridge.py: 把 EventEngine 后台线程投递到 DPG 主线程 —— 用 dpg.set_frame_callback(dpg.generate_uuid(), callback) 每帧从 queue.Queue 排空 eSessionStatus 等事件（DRY 原则：一个 drain 函数，逐事件 dispatch 到注册的 handler 表）。
5) 主循环 app_dpg.py:
   main(): create_context -> fonts -> theme -> state -> accounts window -> dpg.create_viewport(title="MT5 Light Trader", width=1150, height=620)
   while dpg.is_dearpygui_running(): bridge.drain(); dpg.render_dearpygui_frame()
   事件引擎线程 daemon；退出时安全 stop（dpg.set_exit_callback 里 bond close）。
6) 账户管理窗口 accounts_window.py（对照旧版布局）:
   工具栏按钮: [添加账户] [编辑] [删除] [扫描MT5客户端(后台 TerminalScanThread 逻辑照搬,用 threading.Thread+queue)] [打开控制台(本批先占位 disabled, tooltip"批2")]
   表: dpg.add_table(tag, resizable=True, borders_inner/outer=True, row_bg/striped=True...) + add_table_column(各列 init_width_or_weight) 顺序: 登录号|服务器|绑定终端(路径)|数据目录|连接状态|操作
   交互: 行点击选中（is_item_hovered 判定 per-cell）; 双击=打开控制台（本批 disabled）；操作列: 按钮 per row（dpg.add_button, small）: 控制台(禁用)/编辑/删除 —— 删除走 confirm 模态(dpg.add_window modal)。
   连接状态列颜色: 已连接 #16c784 / 未连接 #ea3943，用 state 里 session 最新状态（状态下 bridge 更新）。
   扫描进度: 状态栏 label + 每发现条目------
7) 添加账户对话框(dpg child/modal): 字段同旧: 登录号(QSpinBox→数字输入), 密码(密码字段), 服务器, 绑定终端下拉(from store.terminals()，末项"(自动/默认终端)")；OK 入库 store.add。
   编辑=同窗预填；删除=confirm。
8) TerminalScanThread: 照搬 mt5_trader/terminals.py 的 scan/iter_deep_scan 逻辑封装成 threading.Thread + queue 后台，通过 bridge 回填 state（发现即扫描 found 处理 → store.sync_terminals 调用）。
【验证】
1) python -m py_compile 所有新文件(不含 import errors 无法可测的除外)。
2) 临时脚本（跑完删除）: offscreen? DPG 无 offscreen —— 用真实渲染起一个 app_dpg 主窗口 3 秒并自动退出（用 after 定时 close: dpg.set_exit_callback / frame_callback + 计数），断言: 主窗口存在、表列头存在、无异常；如无 GPU 则 catch 并打印 dpg 平台错误（本项目机器有 GPU）。
3) 单独起 app_dpg.py 真实渲染检查日志（stderr 无 Traceback）。
【报告】文件清单、主题映射表、TABLE 排序实现策略说明（你也许用 dpg.add_table_column(...) 无 sort 参数，需要解释你如何自实现排序 + 如需要可调用 dpg API 建议清单）、冒烟输出。不得修改: mt5_trader/{worker.py,account_session.py,constants.py,console*,spreads.py,weights.py,strategy.py,opportunities.py,accounts.py,database.py,event_engine.py,terminals.py,mt5_trader/ui/**} 以及任何旧文件；app.py 保留不动，新建 app_dpg.py。`
  }
]);
return results.map((r, i) => ({ run: ["dpg-skeleton"][i], ok: r.ok, output: (r.output || "").slice(0, 1500) }));
