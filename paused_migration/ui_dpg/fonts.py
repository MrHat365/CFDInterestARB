"""字体加载：DearPyGui 默认字体不含中文字形，必须注册系统字体否则全部缺字。

按旧 QSS 字体栈 "Microsoft YaHei", "Segoe UI" 对应微软雅黑：
- msyh.ttc     常规 14px，设为全局字体；
- msyhbd.ttc   加粗变体，供标题/强调文本（bind_item_font 到具体文本项）。
"""

from __future__ import annotations

import os
import sys

import dearpygui.dearpygui as dpg

_MAIN_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
)
_BOLD_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyhbd.ttf",
    r"C:\Windows\Fonts\simhei.ttf",
)


def setup_fonts(size: int = 14) -> dict[str, int | str]:
    """创建字体并绑定全局字体；返回 {"main": tag, "bold": tag}。"""
    main_path = next((p for p in _MAIN_CANDIDATES if os.path.exists(p)), None)
    bold_path = next((p for p in _BOLD_CANDIDATES if os.path.exists(p)), None)
    if main_path is None:
        # 字体缺失只警告不中断（DPG 默认字体可显示 ASCII，中文缺字）。
        print("[ui_dpg.fonts] 未找到 Microsoft YaHei 等系统字体，中文将缺字", file=sys.stderr)
        return {"main": 0, "bold": 0}

    with dpg.font_registry():
        main = dpg.add_font(main_path, size)
        dpg.bind_font(main)
        # 加粗变体与主字号一致，标题项用 dpg.bind_item_font(item, bold) 换用。
        bold = dpg.add_font(bold_path or main_path, size)
    return {"main": main, "bold": bold}
