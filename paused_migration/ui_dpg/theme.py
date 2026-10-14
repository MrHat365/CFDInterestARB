"""全局 DearPyGui 主题：把旧 QSS 调色板 token 映射为 ImGui 主题色。

配色 token 值与 mt5_trader/ui/theme.py 的 COLORS 完全一致（只读参考，不改旧包）。

映射总表（mvThemeCat_Core 除非标注 mvThemeCat_Plots）：

  QSS 选择器 / 语义                 DearPyGui 常量                      源 token
  ---------------------------------------------------------------------------------------------
  QWidget background               mvThemeCol_WindowBg                 BG_BASE
  面板/卡片 QLabel#KpiCard          mvThemeCol_ChildBg                  BG_PANEL
  QMenu/QComboBox 弹层             mvThemeCol_PopupBg                  BG_RAISED (alpha≈0.97)
  模态遮罩                          mvThemeCol_ModalWindowDimBg         黑半透明
  窗口标题栏                        mvThemeCol_TitleBg/TitleBgActive    BG_PANEL / BG_RAISED
  * color                          mvThemeCol_Text                     TXT_PRIMARY
  QPushButton:disabled             mvThemeCol_TextDisabled             #5a6272
  表格选区 selection-color         mvThemeCol_TextSelectedBg           #24344d
  QLineEdit/QComboBox:普通         mvThemeCol_FrameBg                  BG_RAISED
  QLineEdit:hover                  mvThemeCol_FrameBgHovered           #242c38
  按下/激活                        mvThemeCol_FrameBgActive            #1a2029
  QPushButton:普通                 mvThemeCol_Button                   BG_RAISED
  QPushButton:hover                mvThemeCol_ButtonHovered            #242c38
  按下                             mvThemeCol_ButtonActive             ACCENT
  QListWidget::{hover,selected}    mvThemeCol_HeaderHovered/Header     #1c2736 / #24344d
  QHeaderView::section             mvThemeCol_TableHeaderBg            BG_RAISED
  表格底色                         mvThemeCol_TableRowBg               BG_TABLE
  斑马纹 alternate-background      mvThemeCol_TableRowBgAlt            ZEBRA
  表格网格线 gridline(强/弱)        mvThemeCol_TableBorderStrong/Light  BORDER / GRID
  QSplitter/常规边框               mvThemeCol_Border / Separator       BORDER
  键盘导航高亮                     mvThemeCol_NavCursor                ACCENT
  滚动条                           mvThemeCol_ScrollbarBg/Grab(+Hover) BG_BASE / #333c4d / #414c60
  拖拽手柄                          mvThemeCol_ResizeGrip(Hovered)      BORDER / ACCENT
  滑杆抓取                          mvThemeCol_SliderGrab(-Active)      ACCENT
  QToolTip                         mvThemeCol_PopupBg (共用)           BG_RAISED
  ---- 图表（批2 预映射；DPG 2.3.1 用独立 mvPlotCol_* + category=mvThemeCat_Plots）----
  PlotLines                        mvPlotCol_Line                      ACCENT
  轴文字                            mvPlotCol_AxisText                  TXT_MUTED
  绘图区底                          mvPlotCol_FrameBg + mvPlotCol_PlotBg BG_TABLE
  图例底                            mvPlotCol_LegendBg                  BG_PANEL
  直方图/填充                        mvPlotCol_Fill                      UP
  轴网格/边框                        mvPlotCol_AxisGrid / PlotBorder     GRID / BORDER
  ---- 品牌变体（bind_item_theme 单绑到具体按钮）----
  QPushButton#primary              mvThemeCol_Button(-Hovered/-Active) ACCENT/#6199f3/#3f6fd1 + Text 白
  QPushButton#danger               mvThemeCol_Button(-Hovered/-Active) DOWN/#ef555e/#8f1f2a + Text 白
  ---- 版式（mvStyleVar）----
  控件内边距 QSS padding 5px 14px   mvStyleVar_FramePadding             (8, 6)
  圆角 4px                         mvStyleVar_FrameRounding            4
  表格单元格内边距                 mvStyleVar_CellPadding              (10, 6)
  控件间距                         mvStyleVar_ItemSpacing              (8, 8)
  窗口内边距                       mvStyleVar_WindowPadding            (10, 10)
"""

from __future__ import annotations

import dearpygui.dearpygui as dpg

# 调色板 Token：与 mt5_trader/ui/theme.py 同值，语义 key 相同。
COLORS: dict[str, str] = {
    "BG_BASE": "#0e1116",
    "BG_PANEL": "#151a21",
    "BG_TABLE": "#12161d",
    "BG_RAISED": "#1c222c",
    "BORDER": "#2a3140",
    "TXT_PRIMARY": "#dde3ec",
    "TXT_MUTED": "#8b93a3",
    "UP": "#16c784",
    "DOWN": "#ea3943",
    "WARN": "#f0b90b",
    "ACCENT": "#4d8df0",
    "GRID": "#202836",
    "ZEBRA": "#171c25",
    # QSS 里写死的补充色（对比高亮/失效）
    "HOVER": "#242c38",
    "PRESSED": "#1a2029",
    "SELECTION": "#24344d",
    "TEXT_DISABLED": "#5a6272",
    "STALE": "#e05661",           # 失效终端红（旧 accounts_view 同色）
    "PRIMARY_HOVER": "#6199f3",
    "PRIMARY_ACTIVE": "#3f6fd1",
    "DANGER_HOVER": "#ef555e",
    "DANGER_ACTIVE": "#8f1f2a",
    "SCROLL_GRAB": "#333c4d",
    "SCROLL_GRAB_HOVER": "#414c60",
    "ROW_HOVER": "#1c2736",
}


def C(name: str) -> list[int]:
    """token -> [r,g,b]。供 add_text(color=…) 等非 plot 场景快捷引用。"""
    return rgba(name, 255)


def RGBA(name: str, alpha: int = 255) -> list[int]:
    """同 rgba 的更明确别名（series color）。"""
    return rgba(name, alpha)


def rgba(name: str, alpha: int = 255) -> list[int]:
    """'#rrggbb' token -> [r, g, b, alpha]。"""
    hexstr = COLORS[name].lstrip("#")
    return [int(hexstr[0:2], 16), int(hexstr[2:4], 16), int(hexstr[4:6], 16), alpha]


def _core_map() -> list[tuple[int, str, int]]:
    """(DearPyGui 色常量, token, alpha)。顺序即上面映射总表的 Core 部分。"""
    return [
        (dpg.mvThemeCol_WindowBg, "BG_BASE", 255),
        (dpg.mvThemeCol_ChildBg, "BG_PANEL", 255),
        (dpg.mvThemeCol_PopupBg, "BG_RAISED", 247),
        (dpg.mvThemeCol_ModalWindowDimBg, "BG_BASE", 140),
        (dpg.mvThemeCol_TitleBg, "BG_PANEL", 255),
        (dpg.mvThemeCol_TitleBgActive, "BG_RAISED", 255),
        (dpg.mvThemeCol_TitleBgCollapsed, "BG_PANEL", 255),
        (dpg.mvThemeCol_Text, "TXT_PRIMARY", 255),
        (dpg.mvThemeCol_TextDisabled, "TEXT_DISABLED", 255),
        (dpg.mvThemeCol_TextSelectedBg, "SELECTION", 255),
        (dpg.mvThemeCol_FrameBg, "BG_RAISED", 255),
        (dpg.mvThemeCol_FrameBgHovered, "HOVER", 255),
        (dpg.mvThemeCol_FrameBgActive, "PRESSED", 255),
        (dpg.mvThemeCol_Button, "BG_RAISED", 255),
        (dpg.mvThemeCol_ButtonHovered, "HOVER", 255),
        (dpg.mvThemeCol_ButtonActive, "ACCENT", 255),
        (dpg.mvThemeCol_HeaderHovered, "ROW_HOVER", 255),
        (dpg.mvThemeCol_Header, "SELECTION", 255),
        (dpg.mvThemeCol_HeaderActive, "SELECTION", 255),
        (dpg.mvThemeCol_TableHeaderBg, "BG_RAISED", 255),
        (dpg.mvThemeCol_TableRowBg, "BG_TABLE", 255),
        (dpg.mvThemeCol_TableRowBgAlt, "ZEBRA", 255),
        (dpg.mvThemeCol_TableBorderStrong, "BORDER", 255),
        (dpg.mvThemeCol_TableBorderLight, "GRID", 255),
        (dpg.mvThemeCol_Border, "BORDER", 255),
        (dpg.mvThemeCol_Separator, "BORDER", 255),
        (dpg.mvThemeCol_NavCursor, "ACCENT", 255),
        (dpg.mvThemeCol_ScrollbarBg, "BG_BASE", 255),
        (dpg.mvThemeCol_ScrollbarGrab, "SCROLL_GRAB", 255),
        (dpg.mvThemeCol_ScrollbarGrabHovered, "SCROLL_GRAB_HOVER", 255),
        (dpg.mvThemeCol_ScrollbarGrabActive, "ACCENT", 255),
        (dpg.mvThemeCol_ResizeGrip, "BORDER", 255),
        (dpg.mvThemeCol_ResizeGripHovered, "ACCENT", 255),
        (dpg.mvThemeCol_SliderGrab, "ACCENT", 255),
        (dpg.mvThemeCol_SliderGrabActive, "ACCENT", 255),
    ]


def _plots_map() -> list[tuple[int, str, int]]:
    """批2 绘图区预映射（DPG 2.3.1：目标是 mvPlotCol_*，category=mvThemeCat_Plots）。"""
    return [
        (dpg.mvPlotCol_Line, "ACCENT", 255),
        (dpg.mvPlotCol_AxisText, "TXT_MUTED", 255),
        (dpg.mvPlotCol_FrameBg, "BG_TABLE", 255),
        (dpg.mvPlotCol_PlotBg, "BG_TABLE", 255),
        (dpg.mvPlotCol_LegendBg, "BG_PANEL", 247),
        (dpg.mvPlotCol_Fill, "UP", 255),
        (dpg.mvPlotCol_AxisGrid, "GRID", 255),
        (dpg.mvPlotCol_PlotBorder, "BORDER", 255),
    ]


def _add_colors(color_list: list[tuple[int, str, int]], category: int) -> None:
    for target, token, alpha in color_list:
        dpg.add_theme_color(target, rgba(token, alpha), category=category)


def _add_core_styles() -> None:
    """版式（保持旧 QSS 的 padding/圆角/间距观感）。"""
    category = dpg.mvThemeCat_Core
    dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 8.0, 6.0, category=category)
    dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4.0, -1.0, category=category)
    dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8.0, 8.0, category=category)
    dpg.add_theme_style(dpg.mvStyleVar_CellPadding, 10.0, 6.0, category=category)
    dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 10.0, 10.0, category=category)
    dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 4.0, -1.0, category=category)
    dpg.add_theme_style(dpg.mvStyleVar_ScrollbarRounding, 8.0, -1.0, category=category)
    dpg.add_theme_style(dpg.mvStyleVar_SelectableTextAlign, 0.0, 0.0, category=category)


def _button_variant(button: str, hovered: str, active: str) -> int | str:
    """QPushButton#primary / #danger 变体：只能单绑到具体按钮（bind_item_theme）。"""
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvButton):   # DPG 2.3.1: theme_component 无 category 参数
            dpg.add_theme_color(dpg.mvThemeCol_Button, rgba(button),
                                category=dpg.mvThemeCat_Core)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, rgba(hovered),
                                category=dpg.mvThemeCat_Core)
            dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, rgba(active),
                                category=dpg.mvThemeCat_Core)
            dpg.add_theme_color(dpg.mvThemeCol_Text, [255, 255, 255, 255],
                                category=dpg.mvThemeCat_Core)
    return theme


def _row_selected_variant() -> int | str:
    """表行选中高亮（QTableWidget selection-background-color #24344d）。单绑到 mvTableRow。"""
    with dpg.theme() as theme:
        with dpg.theme_component(dpg.mvTableRow):
            dpg.add_theme_color(dpg.mvThemeCol_TableRowBg, rgba("SELECTION"),
                                category=dpg.mvThemeCat_Core)
    return theme


def apply_theme() -> dict[str, int | str]:
    """构建并绑定全局主题；返回 {global/primary/danger/row_selected: theme_tag}。"""
    with dpg.theme() as global_theme:
        # Core：theme_component(0) = 全部控件类型
        with dpg.theme_component(0):
            _add_colors(_core_map(), dpg.mvThemeCat_Core)
            _add_core_styles()
        # Plots（批2 预映射）：目标是 mvPlotCol_*，category=mvThemeCat_Plots
        with dpg.theme_component(dpg.mvPlot):
            _add_colors(_plots_map(), dpg.mvThemeCat_Plots)
    dpg.bind_theme(global_theme)

    return {
        "global": global_theme,
        "primary": _button_variant("ACCENT", "PRIMARY_HOVER", "PRIMARY_ACTIVE"),
        "danger": _button_variant("DOWN", "DANGER_HOVER", "DANGER_ACTIVE"),
        "row_selected": _row_selected_variant(),
    }


def format_ts(text: str | None, prefix: str = "") -> str:
    """给顶栏最近入库时间的小字前缀（与 Qt 版语义一致）。"""
    return f"{prefix}{text or '-'}"


def series_item_theme(color_tag: str, kind: str = "line", alpha: int = 200) -> int:
    """给 plot series 绑定颜色的 item-theme（DPG2.3 series 无 color 形参）。

    :param color_tag: COLORS 的 key（如 UP/DOWN/ACCENT）
    :param kind: "line" | "candle"
    :return: theme tag
    """
    color = COLORS[color_tag]
    rgb = [int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16), alpha]
    with dpg.theme() as theme_tag:
        if kind == "line":
            with dpg.theme_component(dpg.mvLineSeries):
                dpg.add_theme_color(dpg.mvThemeCol_Line, rgb,
                                    category=dpg.mvThemeCat_Plots)
        elif kind == "candle":
            with dpg.theme_component(dpg.mvCandleSeries):
                dpg.add_theme_color(dpg.mvThemeCol_CandleBull, rgb,
                                    category=dpg.mvThemeCat_Plots)
                dpg.add_theme_color(dpg.mvThemeCol_CandleBear, rgb,
                                    category=dpg.mvThemeCat_Plots)
    return theme_tag
