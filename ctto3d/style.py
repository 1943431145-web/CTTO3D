"""
临床主题系统：深色/浅色双主题，Qt 样式表 + 调色板 + 运行时切换

============================================================
模块功能
============================================================
本模块定义了应用的完整 UI 视觉风格，包括：

1. 深色主题（dark）和浅色主题（light）两套颜色方案
2. 基于 QSS（Qt Style Sheets）的完整 UI 样式模板
3. Fusion 样式引擎的 QPalette 适配
4. 自定义箭头图标渲染（用于下拉框和微调框的 ▲▼）
5. 主题持久化（通过 QSettings 记住用户选择）
6. 运行时主题切换（通过 apply_theme() 函数）

注意事项：
  - VTK 图像画布（3D 体渲染和 2D CT 切片）在两种主题下都保持黑色背景，
    与 3D Slicer 和医院阅片终端保持一致
  - 只有外围 UI 控件（面板/按钮/菜单等）跟随主题变化
  - 标题栏颜色通过 Windows DWM API 设置沉浸式深色模式（Win10 1909+）

============================================================
如何自定义主题样式？
============================================================

【改颜色变量】
  在 DARK 或 LIGHT 字典中修改对应的颜色值（十六进制如 "#RRGGBB"）：
    BG          — 主背景色
    PANEL       — 侧边面板背景
    SURFACE     — 控件面颜色（按钮/输入框等）
    TEXT        — 主文本颜色
    MUTED       — 次要/辅助文本颜色
    ACCENT      — 品牌强调色（按钮选中/链接/焦点等）
    BORDER      — 边框颜色
    DISABLED    — 禁用状态文字颜色
    HANDLE      — 滑块把手颜色
    ...（更多颜色见 DARK/LIGHT 字典）

【改品牌色】
  修改模块开头的 ACCENT 变量（十六进制颜色，同时影响两个主题）：
    ACCENT = "#0E9F9B"          # 蓝绿色（teal）
    ACCENT_DARK = "#0B807D"     # 深一点的变体（悬停时使用）

【改字体】
  修改 _base_stylesheet() 中的 font-family 行：
    * { font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif; }
  修改字体大小：
    * { font-size: 10pt; }  ← 全局基准字体大小

【改圆角大小】
  搜索 QSS 中的 "border-radius: Xpx" 字段，X 越大圆角越明显
  典型位置：按钮 8px、面板 8px、滑块把手 9px

【改控件高度/边距】
  修改对应 QSS 选择器的 padding 值：
    QPushButton { padding: 9px 12px; }  ← 按钮内边距（上下 左右）
    数字越大控件越高

【添加新控件样式】
  在 _base_stylesheet() 或 _input_stylesheet() 中添加新的 QSS 规则块

【改滑块颜色】
  通用滑块：
    QSlider::groove  — 轨道颜色
    QSlider::sub-page — 已填充部分的颜色（accent 色）
    QSlider::handle  — 把手的边框和背景色
  切片导航滑块（SliceNavSlider）有独立的配色方案，
  其样式在 QSS 中以 #SliceNavSlider 选择器定义
============================================================
"""

import os
import tempfile

# ============================================================
# 品牌色 — 两个主题共用
# ============================================================
# 修改品牌色只需改这两个变量
ACCENT = "#0E9F9B"        # 蓝绿色（teal）主品牌色
ACCENT_DARK = "#0B807D"   # 深蓝绿色变体（按钮悬停时使用）


# ============================================================
# 深色主题颜色表
# ============================================================
# 每个键会在 QSS 模板中被替换为对应的颜色值
# 添加新颜色：在此字典中添加键值对，然后在 _base_stylesheet() 中解包使用
DARK = {
    "ACCENT": ACCENT,
    "ACCENT_DARK": ACCENT_DARK,
    # 背景色
    "BG": "#000000",                    # 主窗口背景（纯黑）
    "PANEL": "#000000",                 # 侧边面板背景（纯黑）
    # 控件面
    "SURFACE": "#141414",               # 控件表面（深灰黑）
    "SURFACE_HOVER": "#1E1E1E",        # 控件悬停（稍亮）
    "PRESSED": "#0E2A29",              # 按钮按下（带品牌色深度）
    # 边框/文本
    "BORDER": "#2C2C2C",               # 边框色（深灰）
    "TEXT": "#EAECEE",                 # 主文本（近白）
    "MUTED": "#8B95A0",                # 次要文本（灰色）
    "DISABLED": "#565E66",             # 禁用文本
    # 禁用状态
    "INPUT_DISABLED_BG": "#0C0C0C",    # 禁用输入框背景
    "INPUT_DISABLED_BORDER": "#1C1C1C",# 禁用输入框边框
    "PRIMARY_DISABLED_BG": "#15403F",  # 禁用主按钮背景（品牌色弱化版）
    "PRIMARY_DISABLED_TEXT": "#7FB3B1",# 禁用主按钮文字
    # 微调框步进器悬停
    "STEP_HOVER": "#303030",
    # 滚动条
    "SCROLL_HANDLE": "#2E2E2E",        # 滚动条滑块
    "SCROLL_HANDLE_HOVER": "#3A3A3A",  # 滚动条滑块悬停
    # 滑块把手：深色主题用浅色实心圆，避免叠在强调色轨道上只剩一圈边框
    "HANDLE": "#F3F6F8",
    # 切片导航滑块专用配色
    "WL_ACCENT": "#2563EB",            # 切片滑块强调色（蓝色）
    "WL_TRACK": "#2A2F36",             # 切片滑块轨道色
    # 元信息
    "scheme": "dark",
}


# ============================================================
# 浅色主题颜色表
# ============================================================
# 结构同 DARK 字典，值改为浅色方案
LIGHT = {
    "ACCENT": ACCENT,
    "ACCENT_DARK": ACCENT_DARK,
    # 背景色
    "BG": "#EDF0F3",                   # 主窗口背景（浅灰蓝）
    "PANEL": "#FFFFFF",                # 侧边面板背景（纯白）
    # 控件面
    "SURFACE": "#EDF0F3",             # 控件表面（浅灰蓝）
    "SURFACE_HOVER": "#E0E5EA",       # 控件悬停（稍深）
    "PRESSED": "#D3EAE9",             # 按钮按下（品牌色浅色调）
    # 边框/文本
    "BORDER": "#D2D8DE",              # 边框色（浅灰）
    "TEXT": "#1C2329",                # 主文本（深灰黑）
    "MUTED": "#5E6772",               # 次要文本（中灰）
    "DISABLED": "#A9B1BA",            # 禁用文本
    # 禁用状态
    "INPUT_DISABLED_BG": "#F2F4F6",
    "INPUT_DISABLED_BORDER": "#E4E7EA",
    "PRIMARY_DISABLED_BG": "#9FD4D2",
    "PRIMARY_DISABLED_TEXT": "#F4FBFA",
    # 微调框步进器悬停
    "STEP_HOVER": "#DCE2E7",
    # 滚动条
    "SCROLL_HANDLE": "#C4CAD1",
    "SCROLL_HANDLE_HOVER": "#AAB2BB",
    # 滑块把手
    "HANDLE": "#FFFFFF",              # 白色把手
    # 切片导航滑块专用配色
    "WL_ACCENT": "#2563EB",
    "WL_TRACK": "#CDD3D9",
    # 元信息
    "scheme": "light",
}

# 主题名称到颜色字典的映射
THEMES = {"dark": DARK, "light": LIGHT}
DEFAULT_THEME = "dark"  # 默认主题（首次启动或无法读取设置时使用）

# 图像画布背景 — 两种主题下都保持黑色，与 3D Slicer 阅片环境一致
CANVAS_BG = (0.0, 0.0, 0.0)


def _base_stylesheet(c):
    """
    生成 QSS 基础样式表（不包含输入控件的箭头图标部分）。

    这是整个应用 UI 外观的核心定义。QSS 语法与 CSS 类似，
    通过对象名（#name）和类型选择器来定义各控件的视觉样式。
    
    参数:
      c: 颜色字典（DARK 或 LIGHT），其中每个键对应一个颜色变量

    修改样式的方法：
      - 修改颜色：编辑 DARK/LIGHT 字典中对应的颜色值
      - 修改字体：修改 "* { font-family: ... }" 和 "* { font-size: ... }"
      - 修改圆角：修改各选择器中的 "border-radius: Xpx"
      - 修改控件的内边距：修改 "padding: Xpx Ypx"
      - 修改通用按钮样式：编辑 "QPushButton {{ ... }}" 块
      - 修改主按钮（带品牌色）样式：编辑 "QPushButton#Primary {{ ... }}" 块
      - 修改分段按钮（Segment）样式：编辑 "QPushButton#Segment {{ ... }}" 块
      - 修改滑块轨道/把手：编辑 "QSlider::groove" 和 "QSlider::handle" 相关块
      - 修改菜单栏/右键菜单：编辑 "QMenuBar" 和 "QMenu" 相关块
      - 修改滚动条：编辑 "QScrollBar" 相关块
    """
    # 从颜色字典中解包所有颜色变量为局部变量，以便在 QSS 模板中使用 {变量名} 替换
    ACCENT = c["ACCENT"]; ACCENT_DARK = c["ACCENT_DARK"]
    BG = c["BG"]; PANEL = c["PANEL"]; SURFACE = c["SURFACE"]
    SURFACE_HOVER = c["SURFACE_HOVER"]; PRESSED = c["PRESSED"]; BORDER = c["BORDER"]
    TEXT = c["TEXT"]; MUTED = c["MUTED"]; DISABLED = c["DISABLED"]
    INPUT_DISABLED_BG = c["INPUT_DISABLED_BG"]; INPUT_DISABLED_BORDER = c["INPUT_DISABLED_BORDER"]
    PRIMARY_DISABLED_BG = c["PRIMARY_DISABLED_BG"]; PRIMARY_DISABLED_TEXT = c["PRIMARY_DISABLED_TEXT"]
    SCROLL_HANDLE = c["SCROLL_HANDLE"]; SCROLL_HANDLE_HOVER = c["SCROLL_HANDLE_HOVER"]
    HANDLE = c["HANDLE"]; WL_ACCENT = c["WL_ACCENT"]; WL_TRACK = c["WL_TRACK"]
    # 微波消融面板的通道强调色（浅色主题用更深的同系色保证可读性）
    # 旁温=青绿（水冷）/ 杆温=琥珀（发热）/ 时间=天蓝 / 功率=黄，红色专属报警
    if c["scheme"] == "dark":
        MWA_TEMP = "#2DD4BF"; MWA_ROD = "#FBBF24"
        MWA_TIME = "#38BDF8"; MWA_POWER = "#FACC15"
        MWA_OK = "#4ADE80"; MWA_ALARM = "#F87171"
        # 深海军蓝仪器面板：弹窗卡/通道卡/控件面各自的渐变与描边
        MWA_CARD_BG = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                       "stop:0 #111A28, stop:1 #0B111C)")
        MWA_CARD_BORDER = "#26344A"
        MWA_CH_BG = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                     "stop:0 #17212F, stop:1 #121A26)")
        MWA_CH_BORDER = "#2A3850"
        MWA_SURFACE = "#1D2939"; MWA_SURFACE_BORDER = "#31415C"
        MWA_SURFACE_HOVER = "#253449"
        MWA_DIVIDER = "#26344A"
        # 设备连接面板与消融仪共用同一套海军蓝仪器材质。
        LINK_CARD_BG = MWA_CARD_BG
        LINK_CARD_BORDER = MWA_CARD_BORDER
        LINK_SECTION_BG = MWA_CH_BG
        LINK_SECTION_BORDER = MWA_CH_BORDER
        LINK_ROW_ON_BG = "rgba(74, 222, 128, 12)"
        LINK_SELECTED_BG = MWA_SURFACE_HOVER
        LINK_FIELD_BG = MWA_SURFACE
        LINK_FIELD_BORDER = MWA_SURFACE_BORDER
        LINK_FIELD_OFF_BG = "#111A26"
        LINK_INPUT_BG = "#0A101A"
        LINK_DIVIDER = MWA_DIVIDER
        LINK_GHOST_HOVER = MWA_SURFACE_HOVER
        LINK_LOG_BG = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                       "stop:0 #0A101A, stop:1 #04070D)")
        LINK_LOG_BORDER = "#263248"
        LINK_LOG_TEXT = "#C6D2DB"
        LINK_OK = "#4ADE80"
        LINK_DANGER = "#F87171"
        LINK_DANGER_BORDER = "rgba(248, 113, 113, 120)"
        LINK_DANGER_HOVER = "rgba(248, 113, 113, 26)"
        LINK_ACCENT_BORDER = "rgba(14, 159, 155, 150)"
        LINK_TAG_BG = "rgba(255, 255, 255, 16)"
        LINK_TAG_TEXT = "#8B95A0"
    else:
        MWA_TEMP = "#0F766E"; MWA_ROD = "#B45309"
        MWA_TIME = "#0369A1"; MWA_POWER = "#A16207"
        MWA_OK = "#15803D"; MWA_ALARM = "#DC2626"
        MWA_CARD_BG = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                       "stop:0 #FFFFFF, stop:1 #F4F7FA)")
        MWA_CARD_BORDER = "#DCE3EB"
        MWA_CH_BG = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                     "stop:0 #FBFCFE, stop:1 #F1F5F9)")
        MWA_CH_BORDER = "#DEE5ED"
        MWA_SURFACE = "#EDF1F6"; MWA_SURFACE_BORDER = "#D7DFE9"
        MWA_SURFACE_HOVER = "#E2E9F0"
        MWA_DIVIDER = "#E3E9F0"
        # 浅色主题同样复用消融仪的卡面、控件面与描边。
        LINK_CARD_BG = MWA_CARD_BG
        LINK_CARD_BORDER = MWA_CARD_BORDER
        LINK_SECTION_BG = MWA_CH_BG
        LINK_SECTION_BORDER = MWA_CH_BORDER
        LINK_ROW_ON_BG = "rgba(21, 128, 61, 10)"
        LINK_SELECTED_BG = MWA_SURFACE_HOVER
        LINK_FIELD_BG = MWA_SURFACE
        LINK_FIELD_BORDER = MWA_SURFACE_BORDER
        LINK_FIELD_OFF_BG = "#E7EDF3"
        LINK_INPUT_BG = "#F7FAFC"
        LINK_DIVIDER = MWA_DIVIDER
        LINK_GHOST_HOVER = MWA_SURFACE_HOVER
        LINK_LOG_BG = ("qlineargradient(x1:0, y1:0, x2:0, y2:1, "
                       "stop:0 #0A101A, stop:1 #04070D)")
        LINK_LOG_BORDER = "#263248"
        LINK_LOG_TEXT = "#C6D2DB"
        LINK_OK = "#15803D"
        LINK_DANGER = "#DC2626"
        LINK_DANGER_BORDER = "rgba(220, 38, 38, 110)"
        LINK_DANGER_HOVER = "rgba(220, 38, 38, 22)"
        LINK_ACCENT_BORDER = "rgba(14, 159, 155, 150)"
        LINK_TAG_BG = "rgba(28, 35, 41, 12)"
        LINK_TAG_TEXT = "#5E6772"
    return f"""
* {{
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 10pt;
    color: {TEXT};
}}
QMainWindow, QWidget#Root {{
    background: {BG};
}}

/* Header (the app's top title band) */
QFrame#Header {{
    background: {BG};
    border-bottom: 1px solid {BORDER};
}}
QLabel#HeaderLogo {{
    background: transparent;
    border: none;
}}
QFrame#HeaderAccent {{ background: {ACCENT}; border-radius: 3px; }}
QFrame#HeaderDivider {{ background: {BORDER}; }}
QPushButton#HeaderExit, QPushButton#HeaderMwa, QPushButton#HeaderTool, QToolButton#HeaderTool {{
    background: transparent;
    border: none;
    border-radius: 8px;
    padding: 0px;
    margin: 0px;
    text-align: center;
}}
QPushButton#HeaderTool:hover, QToolButton#HeaderTool:hover {{ background: {SURFACE_HOVER}; }}
QPushButton#HeaderTool:pressed, QToolButton#HeaderTool:pressed {{ background: {PRESSED}; }}
QToolButton#HeaderTool::menu-indicator {{ image: none; width: 0; height: 0; }}
QPushButton#HeaderMwa:hover {{ background: rgba(245, 158, 11, 38); }}
QPushButton#HeaderMwa:pressed {{ background: rgba(245, 158, 11, 68); }}
QPushButton#HeaderExit:hover {{ background: rgba(251, 113, 133, 38); }}
QPushButton#HeaderExit:pressed {{ background: rgba(251, 113, 133, 68); }}
QLabel#Title {{
    font-size: 13pt;
    font-weight: 700;
    color: {TEXT};
}}
QLabel#Subtitle {{
    font-size: 9pt;
    color: {MUTED};
}}


/* 退出确认 / 消融控制 / 串口连接等居中毛玻璃弹层 */
QWidget#ExitConfirmOverlay, QWidget#PlanningAlertOverlay,
QWidget#MwaControlOverlay, QWidget#SerialControlOverlay {{ background: #0F172A; }}
QFrame#ExitDialogCard {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 16px;
}}
QFrame#MwaDialogCard {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 16px;
}}
/* 设备连接弹窗与消融仪共用一套设计系统：链路通道卡复用 MwaChannel /
   MwaIconChip 等组件（kind 取 ser1/ser2/vna），其余 Link* 规则见本文件
   末尾「设备连接面板」一节。 */
QScrollArea#SerialDialogScroll {{
    background: transparent;
    border: none;
}}
QScrollArea#SerialDialogScroll > QWidget > QWidget {{
    background: transparent;
}}
QLabel#ExitDialogTitle {{
    color: {TEXT};
    font-size: 16pt;
    font-weight: 700;
}}
QLabel#MwaDialogTitle {{
    color: {TEXT};
    font-size: 17pt;
    font-weight: 700;
}}
QPushButton#OverlayCollapse {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px 14px;
    font-weight: 600;
    font-size: 11.5pt;
    text-align: center;
}}
QPushButton#OverlayCollapse:hover {{
    background: {SURFACE_HOVER};
    border-color: {ACCENT};
}}
QLabel#ExitDialogQuestion {{
    color: {MUTED};
    font-size: 11pt;
}}
QPushButton#ExitConfirm, QPushButton#ExitCancel {{
    text-align: center;
    border-radius: 8px;
    padding: 8px 18px;
    font-weight: 600;
}}
QPushButton#ExitConfirm {{
    background: {ACCENT};
    color: white;
    border: 1px solid {ACCENT};
}}
QPushButton#ExitCancel {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
}}

/* 内嵌导入浏览器：固定覆盖内容区，所有元素跟随应用主题。 */
QWidget#EmbeddedFileDialogOverlay {{
    background: transparent;
}}
QFrame#EmbeddedFileDialogCard {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
QLabel#EmbeddedFileDialogTitle {{
    color: {TEXT};
    font-size: 15pt;
    font-weight: 700;
    padding-left: 4px;
}}
QPushButton#EmbeddedFileDialogClose {{
    background: transparent;
    color: {MUTED};
    border: none;
    border-radius: 6px;
    padding: 0;
    font-size: 18pt;
    font-weight: 400;
    /* 覆盖全局 QPushButton 的 text-align:left，否则 × 贴边而按下高亮框居中 */
    text-align: center;
}}
QPushButton#EmbeddedFileDialogClose:pressed {{
    background: {PRESSED};
    color: {TEXT};
}}
QLabel#RestrictedFileType {{
    color: {MUTED};
    background: transparent;
    font-weight: 600;
}}
QLineEdit#RestrictedCurrentPath,
QLineEdit#RestrictedSelectionPath {{
    background: {BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 9px;
}}
QFrame#RestrictedBrowserToolbar {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QToolButton#RestrictedNavButton {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 0;
}}
QToolButton#RestrictedNavButton:pressed {{
    background: {PRESSED};
    border-color: {ACCENT};
}}
QToolButton#RestrictedNavButton:disabled {{
    background: {INPUT_DISABLED_BG};
    color: {DISABLED};
    border-color: {INPUT_DISABLED_BORDER};
}}
QTreeView#RestrictedFileView {{
    background: {SURFACE};
    alternate-background-color: {BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    outline: none;
    selection-background-color: {ACCENT};
    selection-color: white;
}}
QTreeView#RestrictedFileView::item {{
    min-height: 28px;
    padding: 2px 5px;
}}
QTreeView#RestrictedFileView QHeaderView::section {{
    background: {BG};
    color: {MUTED};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 6px 8px;
    font-weight: 600;
}}
QFrame#RestrictedEmptyState {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QLabel#RestrictedEmptyTitle {{
    color: {TEXT};
    background: transparent;
    border: none;
    font-size: 13pt;
    font-weight: 700;
}}
QLabel#RestrictedEmptyDetail {{
    color: {MUTED};
    background: transparent;
    border: none;
    font-size: 9.5pt;
}}
QPushButton#EmbeddedFileDialogAccept {{
    background: {ACCENT};
    color: white;
    border: 1px solid {ACCENT};
    border-radius: 7px;
    padding: 0;
    text-align: center;
    font-weight: 600;
}}
QPushButton#EmbeddedFileDialogCancel {{
    background: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 0;
    text-align: center;
    font-weight: 600;
}}
/* Side panel */
QFrame#Panel {{
    background: {PANEL};
    border-right: 1px solid {BORDER};
}}
QFrame#RightPanel {{
    background: {PANEL};
    border-left: 1px solid {BORDER};
}}
QLabel#SectionTitle {{
    font-size: 11pt;
    font-weight: 700;
    color: #2563EB;
    padding-top: 4px;
    padding-bottom: 2px;
}}
QLabel#Status {{
    color: {MUTED};
    font-size: 9pt;
}}

/* 左侧影像数据暂存区：导入后由用户确认，才加载到中央视图。 */
QFrame#DatasetLibraryCard {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 9px;
}}
QLabel#DatasetCount {{
    color: {MUTED};
    background: transparent;
    font-size: 9pt;
    font-weight: 600;
}}
QLabel#DatasetInfo {{
    color: {MUTED};
    background: transparent;
    border: none;
    font-size: 8.5pt;
}}
QListWidget#DatasetList {{
    background: {BG};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    outline: none;
    padding: 2px;
}}
QListWidget#DatasetList::item {{
    color: {TEXT};
    background: transparent;
    border: none;
    border-bottom: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 6px;
}}
QListWidget#DatasetList::item:hover {{
    background: transparent;
}}
QListWidget#DatasetList::item:selected {{
    background: {ACCENT};
    color: white;
}}

/* Buttons */
QPushButton {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 9px 12px;
    text-align: left;
}}
QPushButton:pressed {{ background: {PRESSED}; }}
QPushButton:disabled {{ color: {DISABLED}; background: {INPUT_DISABLED_BG}; border-color: {INPUT_DISABLED_BORDER}; }}

QPushButton#Primary {{
    background: {ACCENT};
    color: white;
    border: none;
    font-weight: 600;
    text-align: center;
}}
QPushButton#Primary:disabled {{ background: {PRIMARY_DISABLED_BG}; color: {PRIMARY_DISABLED_TEXT}; }}

/* 影像数据：移除按钮，红色文字提示危险操作 */
QPushButton#DatasetRemove {{
    color: #EF4444;
    font-weight: 600;
    text-align: center;
}}
QPushButton#DatasetRemove:disabled {{
    color: {DISABLED};
}}

/* 顶部串口弹出面板使用紧凑按钮，避免套用侧栏的大尺寸按钮。 */
QPushButton#Compact {{
    padding: 5px 10px;
    text-align: center;
    border-radius: 6px;
}}
QPushButton#PrimaryCompact {{
    background: {ACCENT};
    color: white;
    border: none;
    padding: 6px 12px;
    font-weight: 600;
    text-align: center;
    border-radius: 6px;
}}
QPushButton#PrimaryCompact:disabled {{
    background: {PRIMARY_DISABLED_BG}; color: {PRIMARY_DISABLED_TEXT};
}}

/* Segmented toggles (不透明/透明, 显示针, ...) */
QPushButton#Segment {{
    text-align: center;
    border-radius: 8px;
    padding: 8px 10px;
}}
QPushButton#Segment:checked {{
    background: {ACCENT};
    color: white;
    border: none;
    font-weight: 600;
}}

/* Organ list */
QListWidget {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
    outline: none;
}}
QListWidget::item {{
    padding: 8px 10px;
    border-radius: 6px;
}}
QListWidget::item:hover {{ background: {SURFACE_HOVER}; }}
QListWidget::item:selected {{
    background: {ACCENT};
    color: white;
    font-weight: 600;
}}

/* Tissue layer panel */
QScrollArea {{ background: transparent; border: none; }}
QFrame#TissueBox {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QFrame#TissueBox QCheckBox {{ spacing: 6px; }}

/* Orthogonal slice previews — the dark image tiles stay black in both themes */
QFrame#SlicePanel {{
    background: {BG};
    border: 1px solid {BORDER};
    border-radius: 0px;
}}
/* Slice tiles are always black; a fixed dim hairline reads as a fine border
   in both themes (1px is the thinnest a solid QSS border can render). */
QFrame#SliceView {{
    background: #000000;
    border: 1px solid #232323;
    border-radius: 0px;
}}
/* 单击选中的切片视图：金色细边框高亮 */
QFrame#SliceView[active="true"] {{
    border: 1px solid #E3B341;
    border-radius: 0px;
}}
/* 中间 3D 视图：常驻深灰外框；仅已加载 CT 且选中时才显示金色外框 */
QFrame#View3DFrame {{
    border: 1px solid #3A3A3A;
    border-radius: 0px;
}}
QFrame#View3DFrame[active="true"] {{
    border: 1px solid #E3B341;
    border-radius: 0px;
}}
/* 双击放到最大（全屏）时给 3D 视图加外框；选中态仍为金色，宽度一致避免跳动 */
QFrame#View3DFrame[fullscreen="true"] {{
    border: 2px solid #3A3A3A;
}}
QFrame#View3DFrame[fullscreen="true"][active="true"] {{
    border: 2px solid #E3B341;
}}
QLabel#SliceTitle {{
    color: #E5EDF3;
    font-size: 12px;
    font-weight: 600;
}}
/* 放大切片浮层：有体数据且作为焦点时才用金色外框 */
QFrame#ExpandedSliceOverlay {{
    background: {BG};
    border: 1px solid #3A3A3A;
    border-radius: 0px;
}}
QFrame#ExpandedSliceOverlay[active="true"] {{
    border: 1px solid #E3B341;
    border-radius: 0px;
}}
QLabel#DialogTitle {{
    font-size: 15px;
    font-weight: 700;
    color: {TEXT};
}}
QLabel#SliceCounter {{
    color: {MUTED};
    min-width: 72px;
}}
QPushButton#OverlayClose {{
    padding: 6px 12px;
    text-align: center;
    border-radius: 6px;
}}

/* Slider — 左右留白，把手完整显示为实心圆 */
QSlider {{
    min-height: 24px;
    padding: 0 11px;
}}
QSlider:vertical {{
    min-width: 24px;
    padding: 11px 0;
}}
QSlider::groove:horizontal {{
    height: 8px;
    background: {BORDER};
    border-radius: 4px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 4px;
}}
QSlider::add-page:horizontal {{
    background: {BORDER};
    border-radius: 4px;
}}
QSlider::handle:horizontal {{
    background: {HANDLE};
    border: 2px solid {ACCENT};
    width: 16px;
    height: 16px;
    margin: -6px 0;
    border-radius: 10px;
}}
QSlider::groove:vertical {{
    width: 8px;
    background: {BORDER};
    border-radius: 4px;
}}
QSlider::add-page:vertical {{
    background: {ACCENT};
    border-radius: 4px;
}}
QSlider::sub-page:vertical {{
    background: {BORDER};
    border-radius: 4px;
}}
QSlider::handle:vertical {{
    background: {HANDLE};
    border: 2px solid {ACCENT};
    width: 16px;
    height: 16px;
    margin: 0 -6px;
    border-radius: 10px;
}}

QSlider#SliceNavSlider {{
    padding: 0 11px;
}}
QSlider#SliceNavSlider::groove:horizontal {{
    height: 8px; background: {WL_TRACK}; border-radius: 4px;
}}
QSlider#SliceNavSlider::sub-page:horizontal {{
    background: {ACCENT}; border-radius: 4px;
}}
QSlider#SliceNavSlider::add-page:horizontal {{
    background: {WL_TRACK}; border-radius: 4px;
}}
QSlider#SliceNavSlider::handle:horizontal {{
    background: {HANDLE}; border: 2px solid {ACCENT};
    width: 16px; height: 16px; margin: -6px 0; border-radius: 10px;
}}

QCheckBox {{ spacing: 8px; }}
QCheckBox:disabled {{ color: {DISABLED}; }}

/* Scrollbars */
QScrollBar:vertical {{ background: {BG}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {SCROLL_HANDLE}; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {SCROLL_HANDLE_HOVER}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QScrollBar:horizontal {{ background: {BG}; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {SCROLL_HANDLE}; border-radius: 5px; min-width: 28px; }}
QScrollBar::handle:horizontal:hover {{ background: {SCROLL_HANDLE_HOVER}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}

/* Status bar */
QStatusBar {{ background: {BG}; color: {MUTED}; border-top: 1px solid {BORDER}; }}
QStatusBar::item {{ border: none; }}

/* VSCode-style top menu bar (文件 / 视图) sitting in the header */
QMenuBar {{
    background: transparent;
    color: {TEXT};
    border: none;
    padding: 0;
}}
QMenuBar::item {{
    background: transparent;
    padding: 5px 11px;
    margin: 0 1px;
    border-radius: 6px;
}}
QMenuBar::item:selected {{ background: {SURFACE_HOVER}; }}
QMenuBar::item:pressed {{ background: {PRESSED}; color: {TEXT}; }}

/* 顶部串口菜单内嵌的小型通信面板。 */
QLabel#SerialTitle {{ font-size: 11pt; font-weight: 700; color: {TEXT}; }}
QLabel#SerialSection {{ font-size: 9pt; font-weight: 600; color: {MUTED}; }}
QLabel#SerialStatusDot {{ background: #64748B; border-radius: 4px; }}
QLabel#SerialStatusDot[connected="true"] {{ background: #22C55E; }}
QFrame#SerialSeparator {{ background: {BORDER}; border: none; max-height: 1px; }}
/* 收发日志：底板见 QFrame#LinkLogHolder，这里只管文字。
   等宽字体 + 跟随主题的前景色（浅色主题下不再是一块黑板）。 */
QPlainTextEdit#SerialRxLog {{
    background: transparent;
    color: #C9D6E8;
    border: none;
    border-radius: 0px;
    padding: 2px 4px;
    font-family: "Consolas", "Cascadia Mono", "Microsoft YaHei UI", monospace;
    font-size: 9pt;
    selection-background-color: {ACCENT};
    selection-color: white;
}}
/* ---- 微波消融仪：深海军蓝仪器面板 + 四通道深色屏显。 ---- */
QFrame#MwaDialogCard {{
    background: {MWA_CARD_BG};
    border: 1px solid {MWA_CARD_BORDER};
    border-radius: 18px;
}}
QFrame#MwaTitleAccent {{
    background: {ACCENT};
    border-radius: 2px;
}}
QLabel#MwaDialogSubtitle {{
    color: {MUTED};
    font-size: 8pt;
    font-weight: 600;
    background: transparent;
    border: none;
}}
QPushButton#OverlayCollapse {{
    background: {MWA_SURFACE};
    color: {TEXT};
    border: 1px solid {MWA_SURFACE_BORDER};
    border-radius: 8px;
    padding: 4px 14px;
    font-weight: 600;
    font-size: 11.5pt;
    text-align: center;
}}
QPushButton#OverlayCollapse:hover {{
    background: {MWA_SURFACE_HOVER};
    border-color: {ACCENT};
}}
QFrame#MwaDeviceScreen {{
    background: transparent;
    border: none;
}}
QFrame#MwaStatusChip {{
    background: {MWA_SURFACE};
    border: 1px solid {MWA_SURFACE_BORDER};
    border-radius: 14px;
}}
QFrame#MwaStatusChip[online="true"] {{
    background: rgba(34, 197, 94, 22);
    border-color: rgba(34, 197, 94, 90);
}}
QLabel#MwaLink {{
    color: {MUTED};
    font-size: 10.5pt;
    font-weight: 600;
    background: transparent;
    border: none;
}}
QLabel#MwaLink[online="true"] {{
    color: #22C55E;
}}
QFrame#MwaChannel {{
    background: {MWA_CH_BG};
    border: 1px solid {MWA_CH_BORDER};
    border-radius: 14px;
}}
QFrame#MwaChannelHeader,
QFrame#MwaChannelFooter,
QFrame#MwaFooterLine,
QFrame#MwaStepGroup {{
    background: transparent;
    border: none;
}}
QFrame#MwaSectionDivider {{
    background: {MWA_DIVIDER};
    border: none;
}}
/* 通道图标徽章：每通道一个低饱和度的彩色底 */
QFrame#MwaIconChip {{
    border: none;
    border-radius: 9px;
}}
QFrame#MwaIconChip[kind="temp"]  {{ background: rgba(45, 212, 191, 34); }}
QFrame#MwaIconChip[kind="rod"]   {{ background: rgba(251, 191, 36, 34); }}
QFrame#MwaIconChip[kind="time"]  {{ background: rgba(56, 189, 248, 34); }}
QFrame#MwaIconChip[kind="power"] {{ background: rgba(250, 204, 21, 34); }}
/* 设备连接面板的链路通道卡复用同一套通道色：ser1=青绿 ser2=天蓝 vna=琥珀 */
QFrame#MwaIconChip[kind="ser1"] {{ background: rgba(45, 212, 191, 34); }}
QFrame#MwaIconChip[kind="ser2"] {{ background: rgba(56, 189, 248, 34); }}
QFrame#MwaIconChip[kind="vna"] {{ background: rgba(251, 191, 36, 34); }}
QFrame#MwaChannel[kind="ser1"][connected="true"] {{ border-color: rgba(45, 212, 191, 120); }}
QFrame#MwaChannel[kind="ser2"][connected="true"] {{ border-color: rgba(56, 189, 248, 120); }}
QFrame#MwaChannel[kind="vna"][connected="true"] {{ border-color: rgba(251, 191, 36, 120); }}
QLabel#MwaChannelTitle {{
    color: {TEXT};
    font-size: 12pt;
    font-weight: 700;
    background: transparent;
    border: none;
}}
QLabel#MwaUnitBadge {{
    font-size: 8.5pt;
    font-weight: 700;
    border-radius: 9px;
    padding: 2px 9px;
    background: transparent;
    border: none;
}}
QLabel#MwaUnitBadge[kind="temp"]  {{ color: {MWA_TEMP};  background: rgba(45, 212, 191, 22); }}
QLabel#MwaUnitBadge[kind="rod"]   {{ color: {MWA_ROD};   background: rgba(251, 191, 36, 22); }}
QLabel#MwaUnitBadge[kind="time"]  {{ color: {MWA_TIME};  background: rgba(56, 189, 248, 22); }}
QLabel#MwaUnitBadge[kind="power"] {{ color: {MWA_POWER}; background: rgba(250, 204, 21, 22); }}
QLabel#MwaUnitBadge[kind="ser1"] {{ color: {MWA_TEMP}; background: rgba(45, 212, 191, 22); }}
QLabel#MwaUnitBadge[kind="ser2"] {{ color: {MWA_TIME}; background: rgba(56, 189, 248, 22); }}
QLabel#MwaUnitBadge[kind="vna"] {{ color: {MWA_ROD};  background: rgba(251, 191, 36, 22); }}
/* 屏显插槽：两种主题都保持深色玻璃质感，底边一条通道色 LED 灯带 */
QFrame#MwaValueHolder {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 #0A101A, stop:1 #04070D);
    border: 1px solid #263248;
    border-bottom: 2px solid #31405C;
    border-radius: 10px;
}}
QFrame#MwaValueHolder[kind="temp"]  {{ border-bottom-color: rgba(45, 212, 191, 150); }}
QFrame#MwaValueHolder[kind="rod"]   {{ border-bottom-color: rgba(251, 191, 36, 150); }}
QFrame#MwaValueHolder[kind="time"]  {{ border-bottom-color: rgba(56, 189, 248, 150); }}
QFrame#MwaValueHolder[kind="power"] {{ border-bottom-color: rgba(250, 204, 21, 150); }}
QFrame#MwaValueHolder[alarm="true"] {{ border-bottom-color: rgba(248, 113, 113, 190); }}
/* 监护仪风格大号读数：深色屏上按通道着色，报警时转红 */
QLabel#MwaValue {{
    font-family: "Bahnschrift", "Segoe UI Variable Display", "Segoe UI";
    font-size: 46pt;
    font-weight: 600;
    background: transparent;
    border: none;
}}
QLabel#MwaValue[compact="true"] {{
    font-size: 27pt;
}}
QLabel#MwaValue[kind="temp"]  {{ color: #2DD4BF; }}
QLabel#MwaValue[kind="rod"]   {{ color: #FBBF24; }}
QLabel#MwaValue[kind="time"]  {{ color: #38BDF8; }}
QLabel#MwaValue[kind="power"] {{ color: #FACC15; }}
QLabel#MwaValue[alarm="true"] {{ color: #F87171; }}
QLabel#MwaFooterLabel {{
    color: {MUTED};
    font-size: 8.5pt;
    font-weight: 600;
    background: transparent;
    border: none;
}}
QLabel#MwaFooterValue {{
    color: {TEXT};
    font-size: 11pt;
    font-weight: 700;
    background: transparent;
    border: none;
}}
/* 通道状态胶囊：正常绿 / 报警红 */
QLabel#MwaState {{
    color: {MWA_OK};
    background: rgba(34, 197, 94, 26);
    border: 1px solid rgba(34, 197, 94, 64);
    border-radius: 10px;
    padding: 2px 10px;
    font-size: 8.5pt;
    font-weight: 700;
}}
QLabel#MwaState[alarm="true"] {{
    color: {MWA_ALARM};
    background: rgba(239, 68, 68, 26);
    border-color: rgba(239, 68, 68, 70);
}}
QPushButton#MwaStep {{
    min-height: 40px;
    border-radius: 10px;
    padding: 0;
    text-align: center;
    font-family: "Segoe UI", "Microsoft YaHei UI";
    font-weight: 600;
    font-size: 15pt;
    background: {MWA_SURFACE};
    border: 1px solid {MWA_SURFACE_BORDER};
    color: {TEXT};
}}
QPushButton#MwaStep:hover {{
    background: {MWA_SURFACE_HOVER};
    border-color: {ACCENT};
    color: {ACCENT};
}}
QPushButton#MwaStep:pressed {{
    background: {PRESSED};
}}
QPushButton#MwaStep:disabled {{
    color: {DISABLED};
    background: transparent;
    border-color: {MWA_DIVIDER};
}}

/* 底部动作按钮：冷却=天蓝语义，微波=红色语义，激活态填充渐变 */
QPushButton#MwaAction {{
    min-height: 54px;
    border-radius: 12px;
    padding: 4px 16px;
    font-size: 12.5pt;
    font-weight: 700;
    background: {MWA_SURFACE};
    border: 1px solid {MWA_SURFACE_BORDER};
    color: {TEXT};
    text-align: center;
}}
QPushButton#MwaAction[kind="cool"] {{ border-color: rgba(56, 189, 248, 120); }}
QPushButton#MwaAction[kind="cool"]:hover {{
    background: rgba(56, 189, 248, 26);
    border-color: #38BDF8;
}}
QPushButton#MwaAction[kind="cool"][active="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 #0EA5E9, stop:1 #0470A8);
    border: 1px solid #0EA5E9;
    color: white;
}}
QPushButton#MwaAction[kind="mw"] {{ border-color: rgba(248, 113, 113, 120); }}
QPushButton#MwaAction[kind="mw"]:hover {{
    background: rgba(248, 113, 113, 26);
    border-color: #F87171;
}}
QPushButton#MwaAction[kind="mw"][active="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 #EF4444, stop:1 #B91C1C);
    border: 1px solid #EF4444;
    color: white;
}}
/* 设备连接：紧凑动作钮；连接=品牌色描边，断开=红色激活态 */
QPushButton#MwaAction[compact="true"] {{
    min-height: 40px;
    font-size: 11pt;
    border-radius: 10px;
    padding: 2px 12px;
}}
QPushButton#MwaAction[kind="link"] {{
    border-color: rgba(56, 189, 248, 120);
}}
QPushButton#MwaAction[kind="link"]:hover {{
    background: rgba(56, 189, 248, 26);
    border-color: #38BDF8;
}}
QPushButton#MwaAction[kind="link"][active="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 #EF4444, stop:1 #B91C1C);
    border: 1px solid #EF4444;
    color: white;
}}
QPushButton#MwaAction:disabled {{
    color: {DISABLED};
    background: transparent;
    border-color: {MWA_DIVIDER};
}}
/* Context menus (slice right-click) + top-menu dropdowns */
QMenu {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 12px;
    color: {TEXT};
    padding: 6px;
}}
QMenu::item {{
    padding: 7px 18px;
    border-radius: 8px;
    margin: 1px 2px;
}}
QMenu::item:selected {{ background: {ACCENT}; color: white; }}
QMenu::item:disabled {{ color: {DISABLED}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 5px 10px; }}
QMenu::indicator {{ width: 14px; height: 14px; }}
/* 切片窗宽窗位菜单：更紧凑，避免贴边溢出 */
QMenu#SliceWindowLevelMenu {{
    padding: 4px;
    border-radius: 10px;
    font-size: 9pt;
}}
QMenu#SliceWindowLevelMenu::item {{
    padding: 4px 10px;
    border-radius: 6px;
    margin: 0px 1px;
}}
QMenu#SliceWindowLevelMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 3px 8px;
}}
QWidget#WindowLevelRow {{
    background: transparent;
}}
/* ============================================================
   设备连接面板 — 与微波消融仪共用海军蓝仪器设计系统
   ============================================================ */
QWidget#SerialPopup {{
    background: transparent;
    border-radius: 0px;
}}
QFrame#LinkDialogCard {{
    background: {LINK_CARD_BG};
    border: 1px solid {LINK_CARD_BORDER};
    border-radius: 18px;
}}
QFrame#LinkTitleAccent {{
    background: {ACCENT};
    border-radius: 1px;
}}
QLabel#LinkDialogTitle {{
    color: {TEXT};
    font-size: 17pt;
    font-weight: 700;
    background: transparent;
    border: none;
}}
QLabel#LinkDialogSubtitle {{
    color: {MUTED};
    font-size: 8pt;
    font-weight: 600;
    background: transparent;
    border: none;
}}
/* 标题右侧「x / 3 已连接」胶囊 */
QFrame#LinkStatusChip {{
    background: {LINK_FIELD_BG};
    border: 1px solid {LINK_FIELD_BORDER};
    border-radius: 13px;
}}
QFrame#LinkStatusChip[online="true"] {{
    background: {LINK_FIELD_BG};
    border-color: {LINK_FIELD_BORDER};
}}
QLabel#LinkSummary {{
    color: {MUTED};
    font-size: 9.5pt;
    font-weight: 600;
    background: transparent;
    border: none;
}}
QLabel#LinkSummary[online="true"] {{ color: {TEXT}; }}
/* 两个分区容器：设备链路 / 串口监视 */
QFrame#LinkSection, QFrame#LinkMonitor {{
    background: {LINK_SECTION_BG};
    border: 1px solid {LINK_SECTION_BORDER};
    border-radius: 14px;
}}
QLabel#LinkSectionTitle {{
    color: {TEXT};
    font-size: 10.5pt;
    font-weight: 700;
    background: transparent;
    border: none;
}}
QLabel#LinkSectionCaption {{
    color: {MUTED};
    font-size: 8pt;
    font-weight: 600;
    background: transparent;
    border: none;
}}
QLabel#LinkFieldLabel {{
    color: {MUTED};
    font-size: 8pt;
    font-weight: 600;
    background: transparent;
    border: none;
}}
/* 状态角标：「未连接」是待机不是报警，所以走灰阶，红色留给真正的故障 */
QLabel#LinkState {{
    color: {LINK_OK};
    font-size: 8pt;
    font-weight: 700;
    background: rgba(34, 197, 94, 18);
    border: 1px solid rgba(34, 197, 94, 65);
    border-radius: 9px;
    padding: 2px 7px;
}}
QLabel#LinkState[alarm="true"] {{
    color: {MUTED};
    background: {LINK_TAG_BG};
    border-color: {LINK_FIELD_BORDER};
}}
QLabel#LinkStatus {{
    color: {MUTED};
    font-size: 8.5pt;
    background: transparent;
    border: none;
}}
QLabel#LinkStatus[connected="true"] {{ color: {TEXT}; }}
/* 串口错误：红色只留给真正的故障（连接失败 / 资源丢失等） */
QLabel#LinkStatus[error="true"] {{ color: {LINK_DANGER}; }}
/* 链路通道卡 / 监视区内的输入控件 */
QFrame#MwaChannel QComboBox, QFrame#MwaChannel QLineEdit,
QFrame#LinkMonitor QLineEdit {{
    background: {LINK_INPUT_BG};
    border: 1px solid {LINK_FIELD_BORDER};
    border-radius: 7px;
    padding: 3px 8px;
    min-height: 27px;
    color: {TEXT};
}}
QFrame#MwaChannel QComboBox:hover, QFrame#MwaChannel QLineEdit:hover,
QFrame#LinkMonitor QLineEdit:hover {{
    border-color: {ACCENT};
}}
QFrame#MwaChannel QComboBox:disabled, QFrame#MwaChannel QLineEdit:disabled,
QFrame#LinkMonitor QLineEdit:disabled {{
    color: {DISABLED};
    background: {LINK_FIELD_OFF_BG};
    border-color: {LINK_DIVIDER};
}}
QFrame#LinkMonitor QCheckBox {{
    color: {MUTED};
    font-size: 9pt;
    background: transparent;
}}
/* 次级动作：刷新端口 / 清空 */
QPushButton#LinkGhost {{
    background: transparent;
    border: 1px solid {LINK_FIELD_BORDER};
    border-radius: 7px;
    padding: 2px 11px;
    color: {MUTED};
    font-size: 9pt;
    font-weight: 600;
    text-align: center;
}}
QPushButton#LinkGhost:hover {{
    background: {LINK_GHOST_HOVER};
    border-color: {LINK_FIELD_BORDER};
    color: {TEXT};
}}
QPushButton#LinkGhost:disabled {{
    color: {DISABLED};
    border-color: {LINK_DIVIDER};
}}
/* 连接 / 断开：一律描边，不用实心渐变 —— 实心红在医疗界面里等同报警 */
QPushButton#LinkAction {{
    background: {MWA_SURFACE};
    border: 1px solid {LINK_ACCENT_BORDER};
    border-radius: 10px;
    padding: 4px 10px;
    color: {ACCENT};
    font-size: 10pt;
    font-weight: 700;
    text-align: center;
}}
QPushButton#LinkAction:hover {{
    background: {MWA_SURFACE_HOVER};
    border-color: {ACCENT};
}}
QFrame#MwaChannel[kind="ser1"] QPushButton#LinkAction {{
    color: {MWA_TEMP}; border-color: rgba(45, 212, 191, 120);
}}
QFrame#MwaChannel[kind="ser2"] QPushButton#LinkAction {{
    color: {MWA_TIME}; border-color: rgba(56, 189, 248, 120);
}}
QFrame#MwaChannel[kind="vna"] QPushButton#LinkAction {{
    color: {MWA_ROD}; border-color: rgba(251, 191, 36, 120);
}}
QFrame#MwaChannel QPushButton#LinkAction[active="true"] {{
    background: transparent;
    border-color: {LINK_FIELD_BORDER};
    color: {TEXT};
}}
QFrame#MwaChannel QPushButton#LinkAction[active="true"]:hover {{
    background: {LINK_DANGER_HOVER};
    border-color: {LINK_DANGER};
}}
QPushButton#LinkAction:disabled {{
    color: {DISABLED};
    background: transparent;
    border-color: {LINK_DIVIDER};
}}
/* 收发日志底板：与消融仪屏显同款的深色玻璃控制台（两种主题都保持深色） */
QFrame#LinkLogHolder {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 #0A101A, stop:1 #04070D);
    border: 1px solid #263248;
    border-bottom: 2px solid #31405C;
    border-radius: 10px;
}}
/* RX 活动指示灯：收到数据时绿灯闪烁（active 由代码瞬时置位后复位） */
QLabel#SerialRxLed {{
    background: {LINK_FIELD_BORDER};
    border-radius: 4px;
}}
QLabel#SerialRxLed[active="true"] {{
    background: {LINK_OK};
}}
/* RX 接收计数徽章 */
QLabel#SerialRxBadge {{
    color: {MUTED};
    font-size: 8pt;
    font-weight: 600;
    background: {LINK_FIELD_BG};
    border: 1px solid {LINK_FIELD_BORDER};
    border-radius: 9px;
    padding: 1px 8px;
}}
/* TX 活动指示灯：发送数据时蓝色闪烁 */
QLabel#SerialTxLed {{
    background: {LINK_FIELD_BORDER};
    border-radius: 4px;
}}
QLabel#SerialTxLed[active="true"] {{
    background: #38BDF8;
}}
/* TX 发送计数徽章 */
QLabel#SerialTxBadge {{
    color: {MUTED};
    font-size: 8pt;
    font-weight: 600;
    background: {LINK_FIELD_BG};
    border: 1px solid {LINK_FIELD_BORDER};
    border-radius: 9px;
    padding: 1px 8px;
}}
/* 「收起」按钮在本弹层里改用中性灰，不跟着消融仪的海军蓝 */
QWidget#SerialControlOverlay QPushButton#OverlayCollapse {{
    background: {MWA_SURFACE};
    color: {TEXT};
    border: 1px solid {MWA_SURFACE_BORDER};
    border-radius: 8px;
    padding: 4px 14px;
    font-size: 10.5pt;
    font-weight: 600;
}}
QWidget#SerialControlOverlay QPushButton#OverlayCollapse:hover {{
    background: {MWA_SURFACE_HOVER};
    border-color: {ACCENT};
    color: {ACCENT};
}}

QToolTip {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER};
    padding: 6px 8px; border-radius: 6px;
}}
"""


def _render_arrow_png(path, direction, color_hex, size=22):
    """绘制一个小三角形箭头图标并保存为 PNG 文件。
    
    用于微调框（QSpinBox）和下拉框（QComboBox）的 ▲▼ 箭头。
    箭头方向 "up" 对应 ▲，"down" 对应 ▼。
    
    参数：
      path      — 保存路径
      direction — "up"（上箭头）或 "down"（下箭头）
      color_hex — 箭头颜色（十六进制如 "#EAECEE"）
      size      — 图标像素尺寸（默认 22×22）
    
    修改方法：
      箭头大小：调整 size 参数
      箭头粗细：调整 m = size * 0.30 中的 0.30（越大箭头越细）
    """
    from PySide6 import QtCore, QtGui

    pix = QtGui.QPixmap(size, size)
    pix.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pix)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QtCore.Qt.PenStyle.NoPen)
    painter.setBrush(QtGui.QColor(color_hex))
    m = size * 0.30
    tri = QtGui.QPolygonF()
    if direction == "up":
        tri.append(QtCore.QPointF(m, size - m))
        tri.append(QtCore.QPointF(size - m, size - m))
        tri.append(QtCore.QPointF(size / 2.0, m))
    else:
        tri.append(QtCore.QPointF(m, m))
        tri.append(QtCore.QPointF(size - m, m))
        tri.append(QtCore.QPointF(size / 2.0, size - m))
    painter.drawPolygon(tri)
    painter.end()
    pix.save(path, "PNG")


def ensure_arrow_assets(c):
    """为当前主题生成上下三角形箭头图标（▲▼）。
    
    生成 4 种图标：
      - up / down（正常状态，使用 TEXT 颜色）
      - up_dim / down_dim（禁用状态，使用 DISABLED 颜色）
    
    图标保存在 ctto3d/assets/ 目录（可写时）或系统临时目录（回退）。
    文件名按主题命名空间隔离（如 arrow_up_dark.png），确保深浅主题各自独立。
    
    返回：
      含路径的字典 {key: path, ...}，路径中反斜杠已转为正斜杠（Qt url() 要求）
      或 None（生成失败时，主题回退到系统原生箭头）
    """
    try:
        scheme = c["scheme"]
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
        try:
            os.makedirs(out_dir, exist_ok=True)
            if not os.access(out_dir, os.W_OK):
                raise OSError
        except OSError:
            out_dir = os.path.join(tempfile.gettempdir(), "ctto3d_assets")
            os.makedirs(out_dir, exist_ok=True)

        spec = {
            "up": ("up", c["TEXT"]),
            "down": ("down", c["TEXT"]),
            "up_dim": ("up", c["DISABLED"]),
            "down_dim": ("down", c["DISABLED"]),
        }
        paths = {}
        for key, (direction, color) in spec.items():
            p = os.path.join(out_dir, "arrow_%s_%s.png" % (key, scheme))
            # 已有同名文件则复用，避免每次启动都重绘 PNG
            if not os.path.isfile(p):
                _render_arrow_png(p, direction, color)
            paths[key] = p.replace("\\", "/")
        return paths
    except Exception:
        return None


def _input_stylesheet(c, arrows):
    """生成输入控件专用样式表（下拉框 + 微调框 + 它们的箭头）。
    
    若无箭头资源（arrows=None），则跳过箭头规则，使用系统 Fusion 默认箭头样式。
    
    修改方法：
      - 输入框圆角：修改 "border-radius: 6px"
      - 输入框内边距：修改 "padding: 4px 8px"
      - 上下按钮宽度：修改 "width: 18px"
      - 上下按钮颜色：修改 background 值
    """
    if not arrows:
        return ""
    SURFACE = c["SURFACE"]; SURFACE_HOVER = c["SURFACE_HOVER"]; BORDER = c["BORDER"]
    TEXT = c["TEXT"]; ACCENT = c["ACCENT"]; STEP_HOVER = c["STEP_HOVER"]
    return f"""
/* Combo box + spin boxes + line edits (needle params, serial, Z 比例, 功率/时间/倍率) */
QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 8px;
    color: {TEXT};
}}
QComboBox:hover, QDoubleSpinBox:hover, QSpinBox:hover, QLineEdit:hover {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    color: {TEXT};
    selection-background-color: {ACCENT};
    selection-color: white;
    outline: none;
}}

/* Up/down steppers — give them a visible button face and clear triangles */
QDoubleSpinBox::up-button, QSpinBox::up-button {{
    subcontrol-origin: padding; subcontrol-position: top right;
    width: 18px; background: {SURFACE_HOVER};
    border-left: 1px solid {BORDER}; border-top-right-radius: 6px;
}}
QDoubleSpinBox::down-button, QSpinBox::down-button {{
    subcontrol-origin: padding; subcontrol-position: bottom right;
    width: 18px; background: {SURFACE_HOVER};
    border-left: 1px solid {BORDER}; border-bottom-right-radius: 6px;
}}
QDoubleSpinBox::up-button:pressed, QSpinBox::up-button:pressed,
QDoubleSpinBox::down-button:pressed, QSpinBox::down-button:pressed {{ background: {ACCENT}; }}
QDoubleSpinBox::up-arrow, QSpinBox::up-arrow {{ image: url({arrows['up']}); width: 9px; height: 9px; }}
QDoubleSpinBox::down-arrow, QSpinBox::down-arrow {{ image: url({arrows['down']}); width: 9px; height: 9px; }}
QDoubleSpinBox::up-arrow:disabled, QSpinBox::up-arrow:disabled {{ image: url({arrows['up_dim']}); }}
QDoubleSpinBox::down-arrow:disabled, QSpinBox::down-arrow:disabled {{ image: url({arrows['down_dim']}); }}
QComboBox::drop-down {{
    subcontrol-origin: padding; subcontrol-position: center right;
    width: 20px; border-left: 1px solid {BORDER};
}}
QComboBox::down-arrow {{ image: url({arrows['down']}); width: 10px; height: 10px; }}
QComboBox::down-arrow:disabled {{ image: url({arrows['down_dim']}); }}
"""


def _resolve(theme):
    """将主题名称或颜色字典统一解析为颜色字典。
    支持两种输入：字符串名（'dark'/'light'）或已解析的字典。
    未知名称会回退到默认主题（DEFAULT_THEME）。
    """
    if isinstance(theme, dict):
        return theme
    return THEMES.get(theme, THEMES[DEFAULT_THEME])


def build_stylesheet(theme=DEFAULT_THEME, arrows=None):
    """构造完整的应用样式表 = 基础样式 + 输入控件样式（含箭头图标）。
    
    参数：
      theme  — 主题名称或颜色字典
      arrows — 箭头图标路径字典（由 ensure_arrow_assets() 生成），None 时跳过箭头规则
    
    返回：
      完整的 QSS 字符串
    """
    c = _resolve(theme)
    return _base_stylesheet(c) + _input_stylesheet(c, arrows)


def palette(theme=DEFAULT_THEME):
    """生成与当前主题匹配的 Fusion QPalette。
    
    QPalette 覆盖了 QSS 样式表无法触及的系统控件颜色（如原生滚动条、
    下拉菜单、系统对话框等），同时对 Windows 11 的原生标题栏颜色也有影响。
    
    返回：
      QPalette 对象
    """
    from PySide6.QtGui import QColor, QPalette

    c = _resolve(theme)
    role = QPalette.ColorRole
    group = QPalette.ColorGroup
    pal = QPalette()
    window = QColor(c["BG"])
    surface = QColor(c["SURFACE"])
    text = QColor(c["TEXT"])

    pal.setColor(role.Window, window)
    pal.setColor(role.WindowText, text)
    pal.setColor(role.Base, surface)
    pal.setColor(role.AlternateBase, window)
    pal.setColor(role.ToolTipBase, surface)
    pal.setColor(role.ToolTipText, text)
    pal.setColor(role.Text, text)
    pal.setColor(role.Button, surface)
    pal.setColor(role.ButtonText, text)
    pal.setColor(role.BrightText, QColor("#FF6B6B"))
    pal.setColor(role.Link, QColor(c["ACCENT"]))
    pal.setColor(role.Highlight, QColor(c["ACCENT"]))
    pal.setColor(role.HighlightedText, QColor("#FFFFFF"))
    pal.setColor(role.PlaceholderText, QColor(c["MUTED"]))
    # 禁用状态下的文字颜色
    for r in (role.WindowText, role.Text, role.ButtonText):
        pal.setColor(group.Disabled, r, QColor(c["DISABLED"]))
    return pal


def dark_palette():
    """向后兼容的快捷函数 — 返回深色主题 QPalette。"""
    return palette("dark")


def apply_theme(app, theme):
    """将指定主题应用到整个应用程序。
    
    这是运行时切换主题的入口函数，依次执行：
      1. 设置 Fusion 样式引擎
      2. 应用匹配的 QPalette（影响系统级控件和标题栏颜色）
      3. 设置操作系统的颜色方案（Qt 6.5+，影响原生标题栏）
      4. 生成箭头图标并编译完整 QSS 样式表
      5. 将样式表应用到 QApplication
    
    参数：
      app   — QApplication 实例
      theme — 主题名称（"dark" 或 "light"），未知名称使用默认
    
    返回：
      解析后的颜色字典，调用者可重用其中颜色值
    """
    from PySide6 import QtCore

    name = theme if theme in THEMES else DEFAULT_THEME
    c = THEMES[name]
    app.setStyle("Fusion")
    app.setPalette(palette(c))
    # Qt 6.5+ 支持通过 ColorScheme 提示操作系统使用匹配的原生标题栏
    try:
        scheme = (QtCore.Qt.ColorScheme.Dark if c["scheme"] == "dark"
                  else QtCore.Qt.ColorScheme.Light)
        app.styleHints().setColorScheme(scheme)
    except (AttributeError, TypeError):
        pass
    # 重新生成箭头图标（颜色随主题变化）+ 应用样式表
    app.setStyleSheet(build_stylesheet(c, ensure_arrow_assets(c)))
    return c


def style_rounded_menu(menu):
    """让 QMenu 圆角在 Windows 上真正生效（含子菜单）。"""
    from PySide6 import QtCore

    if menu is None:
        return
    flags = (menu.windowFlags()
             | QtCore.Qt.WindowType.FramelessWindowHint
             | QtCore.Qt.WindowType.NoDropShadowWindowHint)
    menu.setWindowFlags(flags)
    menu.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
    for action in menu.actions():
        submenu = action.menu()
        if submenu is not None:
            style_rounded_menu(submenu)


def _settings():
    """获取 QSettings 实例，用于持久化用户偏好设置。
    
    使用组织名 "CTto3D" 和应用名 "CTto3D"，
    Windows 下存储在注册表 HKCU\\Software\\CTto3D\\CTto3D。
    """
    from PySide6 import QtCore
    return QtCore.QSettings("CTto3D", "CTto3D")


def load_theme():
    """读取用户上次选择的主题名称，默认为深色主题（"dark"）。
    
    从 QSettings 中读取 'theme' 键的值。
    若读取失败或值无效，返回默认主题。
    """
    try:
        value = _settings().value("theme", DEFAULT_THEME)
    except Exception:
        return DEFAULT_THEME
    return value if value in THEMES else DEFAULT_THEME


def save_theme(name):
    """将当前主题名称保存到 QSettings，下次启动时自动恢复。
    
    参数：
      name — "dark" 或 "light"
    """
    if name not in THEMES:
        return
    try:
        _settings().setValue("theme", name)
    except Exception:
        pass
