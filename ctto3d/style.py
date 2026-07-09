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
  窗宽窗位滑块（WindowLevelSlider）有独立的配色方案，
  其样式在 QSS 中以 #WindowLevelSlider 选择器定义
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
    # 滑块把手
    "HANDLE": "#000000",               # 滑块把手填充色
    # 窗宽窗位滑块专用配色
    "WL_ACCENT": "#2563EB",            # 窗宽窗位滑块强调色（蓝色）
    "WL_TRACK": "#2A2F36",             # 窗宽窗位滑块轨道色
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
    # 窗宽窗位滑块专用配色
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
QFrame#HeaderAccent {{ background: {ACCENT}; border-radius: 3px; }}
QFrame#HeaderDivider {{ background: {BORDER}; }}
QLabel#Title {{
    font-size: 13pt;
    font-weight: 700;
    color: {TEXT};
}}
QLabel#Subtitle {{
    font-size: 9pt;
    color: {MUTED};
}}

/* Side panel */
QFrame#Panel {{
    background: {PANEL};
    border-right: 1px solid {BORDER};
}}
QLabel#SectionTitle {{
    font-size: 8pt;
    font-weight: 700;
    color: {MUTED};
    text-transform: uppercase;
    letter-spacing: 1px;
}}
QLabel#Status {{
    color: {MUTED};
    font-size: 9pt;
}}

/* Buttons */
QPushButton {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 9px 12px;
    text-align: left;
}}
QPushButton:hover {{ border-color: {ACCENT}; background: {SURFACE_HOVER}; }}
QPushButton:pressed {{ background: {PRESSED}; }}
QPushButton:disabled {{ color: {DISABLED}; background: {INPUT_DISABLED_BG}; border-color: {INPUT_DISABLED_BORDER}; }}

QPushButton#Primary {{
    background: {ACCENT};
    color: white;
    border: none;
    font-weight: 600;
    text-align: center;
}}
QPushButton#Primary:hover {{ background: {ACCENT_DARK}; }}
QPushButton#Primary:disabled {{ background: {PRIMARY_DISABLED_BG}; color: {PRIMARY_DISABLED_TEXT}; }}

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
/* 中间 3D 视图：常驻深灰外框（未导入 CT 时也显示，作为布局骨架），单击选中时金色外框 */
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
/* 放大切片浮层：作为当前焦点视图，外框用金色 */
QFrame#ExpandedSliceOverlay {{
    background: {BG};
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

/* Slider */
QSlider::groove:horizontal {{
    height: 5px; background: {BORDER}; border-radius: 3px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT}; border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: {HANDLE}; border: 2px solid {ACCENT};
    width: 15px; height: 15px; margin: -6px 0; border-radius: 9px;
}}

QSlider#WindowLevelSlider::groove:horizontal {{
    height: 7px; background: {WL_TRACK}; border-radius: 4px;
}}
QSlider#WindowLevelSlider::sub-page:horizontal {{
    background: {WL_ACCENT}; border-radius: 4px;
}}
QSlider#WindowLevelSlider::handle:horizontal {{
    background: {HANDLE}; border: 2px solid {WL_ACCENT};
    width: 16px; height: 16px; margin: -6px 0; border-radius: 9px;
}}
QSlider#WindowLevelSlider::groove:vertical {{
    width: 7px; background: {WL_TRACK}; border-radius: 4px;
}}
QSlider#WindowLevelSlider::add-page:vertical {{
    background: {WL_ACCENT}; border-radius: 4px;
}}
QSlider#WindowLevelSlider::sub-page:vertical {{
    background: {WL_TRACK}; border-radius: 4px;
}}
QSlider#WindowLevelSlider::handle:vertical {{
    background: {HANDLE}; border: 2px solid {WL_ACCENT};
    width: 16px; height: 16px; margin: 0 -6px; border-radius: 9px;
}}

QSlider#SliceNavSlider::groove:horizontal {{
    height: 5px; background: {WL_TRACK}; border-radius: 3px;
}}
QSlider#SliceNavSlider::sub-page:horizontal {{
    background: {ACCENT}; border-radius: 3px;
}}
QSlider#SliceNavSlider::handle:horizontal {{
    background: {HANDLE}; border: 2px solid {ACCENT};
    width: 14px; height: 14px; margin: -5px 0; border-radius: 8px;
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

/* Context menus (slice right-click) + top-menu dropdowns */
QMenu {{ background: {SURFACE}; border: 1px solid {BORDER}; color: {TEXT}; padding: 4px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT}; color: white; }}
QMenu::item:disabled {{ color: {DISABLED}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

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
/* Combo box + spin boxes (needle params, Z 比例, 功率/时间/倍率) */
QComboBox, QDoubleSpinBox, QSpinBox {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 8px;
    color: {TEXT};
}}
QComboBox:hover, QDoubleSpinBox:hover, QSpinBox:hover {{ border-color: {ACCENT}; }}
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
    subcontrol-origin: border; subcontrol-position: top right;
    width: 18px; background: {SURFACE_HOVER};
    border-left: 1px solid {BORDER}; border-top-right-radius: 6px;
}}
QDoubleSpinBox::down-button, QSpinBox::down-button {{
    subcontrol-origin: border; subcontrol-position: bottom right;
    width: 18px; background: {SURFACE_HOVER};
    border-left: 1px solid {BORDER}; border-bottom-right-radius: 6px;
}}
QDoubleSpinBox::up-button:hover, QSpinBox::up-button:hover,
QDoubleSpinBox::down-button:hover, QSpinBox::down-button:hover {{ background: {STEP_HOVER}; }}
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
