"""DearPyGui 版 GUI 迁移（批1）：骨架 + 账户管理窗口。

- state-first：所有数据/后台线程持有在 state.DpgState，UI 只读渲染、写入走状态方法。
- event_bridge.DpgEventBridge：把 EventEngine 后台线程事件与本地线程事件
  统一排入 queue.Queue，主循环每帧 drain 到 DPG 主线程。
- 本包替代旧 PySide6 UI（mt5_trader/ui/，批1 仅只读参考；批3 删除）。
"""

from __future__ import annotations
