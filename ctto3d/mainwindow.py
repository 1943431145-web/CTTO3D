"""
主窗口模块 — 应用控制面板和主界面

============================================================
模块功能
============================================================
本模块定义了应用程序的主窗口 UI，包含以下结构：

页面布局（从上到下）：
  ┌──────────────────────────────────────────────────┐
  │  Header（标题栏）：应用名 + 文件/视图菜单        │
  ├──────────────┬───────────────────────────────────┤
  │ 侧边面板     │  3D 体渲染视图 + 三向切片预览    │
  │ (330px 宽)  │  (VolumeViewer)                  │
  │              │                                  │
  │  1. 组织     │                                  │
  │     (勾选    │                                  │
  │     显示)    │                                  │
  │  2. 显示效果 │                                  │
  │     (透明/   │                                  │
  │     不透明,  │                                  │
  │     不透明度,│                                  │
  │     Z比例)   │                                  │
  │  3. 消融针   │                                  │
  │     仿真     │                                  │
  │  4. 消融     │                                  │
  │     仿真     │                                  │
  └──────────────┴───────────────────────────────────┘
  │  状态栏                                         │
  └──────────────────────────────────────────────────┘

主要类：
  MainWindow — 主窗口，包含所有 UI 逻辑和数据交互
  （组织层的显示/透明度已迁移到 3D 视图右键菜单「组织」，由 VolumeViewer 维护）

修改方法：
  - 面板宽度：修改 _build_panel() 中 scroll.setFixedWidth(330) 的值
  - 标题文字：修改 _build_header() 中 title/subtitle 的 setText()
  - 添加新的面板分区：参照 _build_panel() 中现有分区的模式添加
  - 组织列表：直接随内容自适应高度，全部显示（无内层滚动条）
  - 菜单文字和图标：修改 _build_menubar() 中的 addAction() 文字
============================================================
"""

import logging
import os
import shutil
import sys
import tempfile

from PySide6 import QtCore, QtGui, QtWidgets

from . import ablation, loader, segmentation, style
from .viewer import VolumeViewer

log = logging.getLogger(__name__)

# 不透明/透明模式预设对应的总量不透明度系数
# 不透明模式：总量 × 1.0（使用滑块设置的值）
# 透明模式 / 分割完成：直接拉到滑块最低值（最透），方便透视内部结构与分割
OPAQUE_SCALE = 1.0
# 不透明度滑块上的参考刻度位置（约 15%）：透视分割时的推荐透明度
OPACITY_MARK_VALUE = 15


class MarkedSlider(QtWidgets.QSlider):
    """普通(青色)样式的水平滑块，额外在某个值处画一条参考刻度线。"""

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self._mark_value = None

    def set_mark_value(self, value):
        self._mark_value = None if value is None else int(value)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._mark_value is None:
            return
        opt = QtWidgets.QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QtWidgets.QStyle.ComplexControl.CC_Slider,
            opt,
            QtWidgets.QStyle.SubControl.SC_SliderGroove,
            self,
        )
        if groove.isNull() or groove.width() <= 0:
            return
        pos = QtWidgets.QStyle.sliderPositionFromValue(
            self.minimum(), self.maximum(), self._mark_value,
            groove.width(), self.invertedAppearance(),
        )
        x = groove.x() + pos
        y = groove.center().y()
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor(150, 162, 175, 200), 1))
        painter.drawLine(x, y - 6, x, y + 6)
        painter.end()


class MainWindow(QtWidgets.QMainWindow):
    """应用程序主窗口。
    
    负责：
      - 构建完整 UI 布局（标题栏 + 侧面板 + 3D视图 + 状态栏）
      - 响应用户操作（加载数据、调整组织层、切换主题、消融针规划、仿真控制）
      - 协调 VolumeViewer（3D 体渲染）和侧面板控制之间的数据流
    
    信号连接架构：
      用户操作 → MainWindow 方法 → VolumeViewer 方法 → VTK 渲染更新
    """

    def __init__(self):
        """
        初始化主窗口：构建 UI 布局、创建 VolumeViewer、设置初始状态。
        
        修改窗口默认行为的方法：
          - 窗口标题：setWindowTitle() 中的文字
          - 窗口初始大小：self.resize(1280, 820) 中的宽高
          - 默认主题：style.load_theme() 返回值
        """
        super().__init__()
        self.setWindowTitle("消融手术规划系统")
        self.resize(1280, 820)
        self._theme = style.load_theme()  # applied app-wide in main(); mirrored in the 主题 menu
        self._seg_thread = None
        self._seg_worker = None
        self._seg_temp_dir = None
        self._seg_cancelled = False
        self._seg_running = False
        self._seg_download_thread = None
        self._seg_download_worker = None
        self._seg_download_running = False

        root = QtWidgets.QWidget(objectName="Root")
        self.setCentralWidget(root)
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())

        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_panel())

        self.viewer = VolumeViewer()
        self.viewer.ablationNeedleChanged.connect(self._on_viewer_needle_changed)
        body.addWidget(self.viewer, 1)
        outer.addLayout(body, 1)

        # Loaded-volume info lives permanently at the right of the status bar
        # (it used to be a label under the old 加载数据 panel section).
        self.status = QtWidgets.QLabel("尚未加载数据", objectName="Status")
        self.statusBar().addPermanentWidget(self.status)
        self.statusBar().showMessage("就绪 — 通过顶部「文件」菜单加载 DICOM 文件夹、图片序列、ZIP 压缩包,或试用演示体模。")
        self._set_controls_enabled(False)

    # ============================================================
    # UI 构建方法
    # 修改 UI 布局/样式时，关注以下区域：
    #   _build_header()   — 顶部标题栏（品牌色条+标题+菜单）
    #   _build_menubar()  — 菜单栏（文件/视图/主题）
    #   _build_panel()    — 右侧控制面板（所有参数控件）
    # ============================================================
    def _build_header(self):
        """构建顶部标题栏：品牌色条 + 应用标题/副标题 + 右侧菜单栏。
        
        样式通过 QSS 中 #Header、#HeaderAccent、#Title、#Subtitle 选择器控制。
        修改标题文字：修改 title.setText() 和 subtitle.setText()
        """
        header = QtWidgets.QFrame(objectName="Header")
        header.setFixedHeight(64)
        lay = QtWidgets.QHBoxLayout(header)
        lay.setContentsMargins(20, 8, 16, 8)
        lay.setSpacing(12)

        # Brand anchored on the left: teal accent bar + stacked title/subtitle.
        accent = QtWidgets.QFrame(objectName="HeaderAccent")
        accent.setFixedSize(6, 36)
        lay.addWidget(accent, 0, QtCore.Qt.AlignVCenter)

        titles = QtWidgets.QVBoxLayout()
        titles.setSpacing(0)
        title = QtWidgets.QLabel("消融手术规划系统", objectName="Title")
        subtitle = QtWidgets.QLabel(
            "面向临床阅片的切片三维重建", objectName="Subtitle")
        for w in (title, subtitle):
            w.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
            titles.addWidget(w)
        lay.addLayout(titles)

        # …menu (文件 / 视图) anchored on the far right, balancing the brand.
        lay.addStretch(1)
        lay.addWidget(self._build_menubar(), 0, QtCore.Qt.AlignVCenter)
        return header

    def _build_menubar(self):
        """构建顶部菜单栏（替代旧版面板中的「加载数据」区域）。
        
        菜单结构：
          文件(&F)
            📁 打开 DICOM 文件夹
            🖼 打开图片序列
            🗜 导入 ZIP 压缩包
            ─────────
            🧪 加载演示体模
            ─────────
            退出
          视图(&V)
            ↺ 重置视角
            📸 保存截图
            ─────────
            导出三维模型 (STL)…
            ─────────
            主题
              深色(黑)
              浅色(白)
        
        修改方法：
          - 添加菜单项：调用 menu.addAction("文字") 并连接 triggered 信号
          - 修改图标：修改加在文字前的 emoji 符号
          - 添加快捷键：在文字中添加 &字母（如 "文件(&F)" → Alt+F）
        """
        self.menu_bar = bar = QtWidgets.QMenuBar()
        bar.setNativeMenuBar(False)  # 强制在窗口内显示（macOS 下重要）
        bar.setSizePolicy(QtWidgets.QSizePolicy.Maximum,
                          QtWidgets.QSizePolicy.Preferred)

        # ---- 文件菜单：数据加载入口 ----
        self.file_menu = file_menu = bar.addMenu("文件(&F)")
        act_dicom = file_menu.addAction("📁  打开 DICOM 文件夹")
        act_stack = file_menu.addAction("🖼  打开图片序列")
        act_zip = file_menu.addAction("🗜  导入 ZIP 压缩包")
        file_menu.addSeparator()
        act_demo = file_menu.addAction("🧪  加载演示体模")
        file_menu.addSeparator()
        act_quit = file_menu.addAction("退出")
        act_dicom.triggered.connect(self._open_dicom)
        act_stack.triggered.connect(self._open_stack)
        act_zip.triggered.connect(self._open_zip)
        act_demo.triggered.connect(self._open_demo)
        act_quit.triggered.connect(self.close)

        # ---- 视图菜单：视角和导出（加载体数据前禁用）----
        self.view_menu = view_menu = bar.addMenu("视图(&V)")
        self.act_reset = view_menu.addAction("↺  重置视角")
        self.act_shot = view_menu.addAction("📸  保存截图")
        view_menu.addSeparator()
        self.act_mesh = view_menu.addAction("导出三维模型 (STL)…")
        self.act_reset.triggered.connect(lambda: self.viewer.reset_view())
        self.act_shot.triggered.connect(self._save_screenshot)
        self.act_mesh.triggered.connect(self._export_mesh)

        # ---- 主题子菜单：运行时切换深色/浅色 ----
        view_menu.addSeparator()
        theme_menu = view_menu.addMenu("主题")
        self.theme_group = QtGui.QActionGroup(self)
        self.theme_group.setExclusive(True)  # 互斥选择
        self.act_theme_dark = theme_menu.addAction("深色(黑)")
        self.act_theme_light = theme_menu.addAction("浅色(白)")
        for act, name in ((self.act_theme_dark, "dark"), (self.act_theme_light, "light")):
            act.setCheckable(True)
            act.setChecked(self._theme == name)
            self.theme_group.addAction(act)
            act.triggered.connect(lambda _checked=False, n=name: self._set_theme(n))

        return bar

    def _set_theme(self, name):
        """在运行时切换整个应用的主题（深色↔浅色）。
        
        执行步骤：
          1. 更新内部状态
          2. 调用 style.apply_theme() 重建样式表/调色板/箭头图标
          3. 保存主题选择到 QSettings（下次启动自动恢复）
          4. 更新 Windows 原生标题栏颜色
          5. 同步菜单勾选状态
        """
        self._theme = name
        log.info("切换主题 -> %s", name)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            style.apply_theme(app, name)
        style.save_theme(name)
        self._apply_titlebar()  # 重绘原生标题栏颜色
        # 保持菜单项的勾选状态与实际一致（程序化调用时）
        act = self.act_theme_dark if name == "dark" else self.act_theme_light
        if not act.isChecked():
            act.setChecked(True)

    def _build_panel(self):
        """构建右侧控制面板（可滚动的参数控制区域）。
        
        面板分区结构（从上到下）：
          1 · 组织（勾选显示 · 单独调透明度）
            └ 组织列表 + 全选/全不选按钮
          2 · 显示效果
            ├ 不透明 / 透明 切换
            ├ 不透明度 滑块
            └ Z 比例 微调框
          3 · 消融针仿真
            ├ 消融针型号 下拉框
            ├ 直径 / 活性端 / 针杆 微调框
            └ 显示针 / 重置针位 / 清除 按钮
          4 · 消融仿真
            ├ 功率 / 时间 / 时间倍率 控件
            ├ 进度条
            ├ 状态标签
            └ 开始仿真 / 暂停 / 停止 按钮
        
        修改方法：
          - 面板宽度：修改 scroll.setFixedWidth(330)
          - 添加新分区：参照现有 _section() 的分区模式
          - 修改标题：修改各 _section() 调用中的文本
          - 组织列表：直接随内容自适应高度，全部显示（无内层滚动条）
        """
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("PanelScroll")
        scroll.setFixedWidth(330)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        panel = QtWidgets.QFrame(objectName="Panel")
        panel.setMinimumWidth(312)
        scroll.setWidget(panel)
        lay = QtWidgets.QVBoxLayout(panel)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(10)

        # 组织层已迁移到 3D 视图的右键菜单「组织」中(显示/隐藏 + 逐层透明度)。

        # 1 - Appearance
        lay.addWidget(self._section("1 · 显示效果"))
        seg = QtWidgets.QHBoxLayout()
        seg.setSpacing(8)
        self.btn_opaque = QtWidgets.QPushButton("不透明", objectName="Segment", checkable=True)
        self.btn_transparent = QtWidgets.QPushButton("透明", objectName="Segment", checkable=True)
        self.mode_group = QtWidgets.QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_group.addButton(self.btn_opaque)
        self.mode_group.addButton(self.btn_transparent)
        self.btn_opaque.setChecked(True)
        self.btn_opaque.clicked.connect(lambda: self._set_mode(OPAQUE_SCALE))
        # 透明 → 直接拉到最低（最透），方便透视内部/分割
        self.btn_transparent.clicked.connect(lambda: self._set_mode(self._min_opacity_scale()))
        seg.addWidget(self.btn_opaque)
        seg.addWidget(self.btn_transparent)
        lay.addLayout(seg)

        op_row = QtWidgets.QHBoxLayout()
        op_row.addWidget(QtWidgets.QLabel("不透明度"))
        self.opacity_slider = MarkedSlider(QtCore.Qt.Horizontal)
        self.opacity_slider.setRange(5, 100)
        self.opacity_slider.setValue(int(OPAQUE_SCALE * 100))
        self.opacity_slider.set_mark_value(OPACITY_MARK_VALUE)   # 15% 参考刻度
        self.opacity_slider.valueChanged.connect(self._on_slider)
        op_row.addWidget(self.opacity_slider, 1)
        lay.addLayout(op_row)

        z_row = QtWidgets.QHBoxLayout()
        z_lbl = QtWidgets.QLabel("Z 比例")
        z_lbl.setToolTip("校正层间距比例:模型显得太长就调小、太扁就调大。\nDICOM 通常保持 1.0(已含真实层厚)。")
        z_row.addWidget(z_lbl)
        self.z_spin = QtWidgets.QDoubleSpinBox()
        self.z_spin.setRange(0.1, 10.0)
        self.z_spin.setSingleStep(0.1)
        self.z_spin.setValue(1.0)
        self.z_spin.setToolTip(z_lbl.toolTip())
        self.z_spin.valueChanged.connect(self._on_z_spacing_changed)
        z_row.addWidget(self.z_spin, 1)
        lay.addLayout(z_row)

        lay.addSpacing(6)

        # AI - TotalSegmentator organ segmentation
        lay.addWidget(self._section("AI · 自动器官分割"))
        self.seg_preset = QtWidgets.QComboBox()
        self.seg_preset.addItems(segmentation.preset_names())
        lay.addWidget(self.seg_preset)

        seg_opts = QtWidgets.QHBoxLayout()
        seg_opts.setSpacing(8)
        self.seg_fast = QtWidgets.QCheckBox("快速模式")
        self.seg_fast.setChecked(True)
        self.seg_fast.setToolTip(
            "勾选后使用 TotalSegmentator 3mm 快速模型。"
            "取消勾选会尝试 1.5mm 标准模型,更细但更慢、更占内存。")
        self.seg_lowmem = QtWidgets.QCheckBox("低内存推理")
        self.seg_lowmem.setChecked(True)
        self.seg_lowmem.setToolTip(
            "启用 TotalSegmentator --force_split,会慢一些,但能降低大体积 CT 的内存压力。")
        seg_opts.addWidget(self.seg_fast)
        seg_opts.addWidget(self.seg_lowmem)
        seg_opts.addStretch(1)
        lay.addLayout(seg_opts)

        seg_device_row = QtWidgets.QHBoxLayout()
        seg_device_row.setSpacing(8)
        seg_device_row.addWidget(QtWidgets.QLabel("设备"))
        self.seg_device = QtWidgets.QComboBox()
        cuda_name = segmentation.cuda_device_name()
        if cuda_name:
            self.seg_device.addItem("GPU · %s" % cuda_name, "gpu")
        else:
            self.seg_device.addItem("GPU（尝试使用 CUDA）", "gpu")
        self.seg_device.addItem("CPU", "cpu")
        self.seg_device.setToolTip(
            "GPU 会使用显卡推理,通常更快并减少 CPU 内存压力。"
            "如果运行时报 CUDA/显存错误,再切回 CPU。")
        seg_device_row.addWidget(self.seg_device, 1)
        lay.addLayout(seg_device_row)

        seg_actions = QtWidgets.QHBoxLayout()
        seg_actions.setSpacing(8)
        self.btn_seg_run = QtWidgets.QPushButton("自动分割", objectName="Primary")
        self.btn_seg_download = QtWidgets.QPushButton("下载模型", objectName="Segment")
        self.btn_seg_run.clicked.connect(self._run_totalsegmentator)
        self.btn_seg_download.clicked.connect(self._download_totalseg_weights)
        seg_actions.addWidget(self.btn_seg_run)
        seg_actions.addWidget(self.btn_seg_download)
        lay.addLayout(seg_actions)

        seg_view_actions = QtWidgets.QHBoxLayout()
        seg_view_actions.setSpacing(8)
        self.btn_seg_focus = QtWidgets.QPushButton("突出显示", objectName="Segment")
        self.btn_seg_clear = QtWidgets.QPushButton("清除分割", objectName="Segment")
        self.btn_seg_focus.clicked.connect(self._highlight_segmentations)
        self.btn_seg_clear.clicked.connect(self._clear_segmentations)
        seg_view_actions.addWidget(self.btn_seg_focus)
        seg_view_actions.addWidget(self.btn_seg_clear)
        lay.addLayout(seg_view_actions)

        self.seg_status = QtWidgets.QLabel(
            "调用 TotalSegmentator 生成器官 mask,并叠加到 3D 视图。", objectName="Status")
        self.seg_status.setWordWrap(True)
        lay.addWidget(self.seg_status)
        self.seg_result = QtWidgets.QLabel("", objectName="Status")
        self.seg_result.setTextFormat(QtCore.Qt.RichText)
        self.seg_result.setWordWrap(True)
        lay.addWidget(self.seg_result)

        lay.addSpacing(6)

        # 针道规划 - 以坐标轴(参考十字)中心放置入针点/消融点，自动连成针道
        lay.addWidget(self._section("针道规划 · 入针点/消融点(坐标轴定位)"))
        plan_hint = QtWidgets.QLabel(
            "拖动切片中的参考坐标轴到目标位置,再放置入针点/消融点;"
            "两点都放好后自动连成针道。", objectName="Status")
        plan_hint.setWordWrap(True)
        lay.addWidget(plan_hint)

        plan_row = QtWidgets.QHBoxLayout()
        plan_row.setSpacing(8)
        self.btn_plan_entry = QtWidgets.QPushButton("放置入针点", objectName="Segment")
        self.btn_plan_tip = QtWidgets.QPushButton("放置消融点", objectName="Segment")
        self.btn_plan_clear = QtWidgets.QPushButton("清除针道", objectName="Segment")
        self.btn_plan_entry.clicked.connect(self._place_entry_point)
        self.btn_plan_tip.clicked.connect(self._place_ablation_point)
        self.btn_plan_clear.clicked.connect(self._clear_planning_points)
        plan_row.addWidget(self.btn_plan_entry)
        plan_row.addWidget(self.btn_plan_tip)
        plan_row.addWidget(self.btn_plan_clear)
        lay.addLayout(plan_row)

        self.plan_status = QtWidgets.QLabel(
            "入针点:未放置   消融点:未放置", objectName="Status")
        self.plan_status.setWordWrap(True)
        lay.addWidget(self.plan_status)

        lay.addSpacing(6)

        # 2 - Ablation needle planning
        lay.addWidget(self._section("2 · 消融针仿真"))
        self.needle_preset = QtWidgets.QComboBox()
        self.needle_preset.addItems(ablation.preset_names())
        self.needle_preset.currentTextChanged.connect(self._on_needle_preset_changed)
        lay.addWidget(self.needle_preset)

        needle_grid = QtWidgets.QGridLayout()
        needle_grid.setHorizontalSpacing(8)
        needle_grid.setVerticalSpacing(6)
        self.needle_diameter = QtWidgets.QDoubleSpinBox()
        self.needle_diameter.setRange(0.2, 5.0)
        self.needle_diameter.setSingleStep(0.1)
        self.needle_diameter.setSuffix(" mm")
        self.needle_active = QtWidgets.QDoubleSpinBox()
        self.needle_active.setRange(1.0, 80.0)
        self.needle_active.setSingleStep(1.0)
        self.needle_active.setSuffix(" mm")
        self.needle_shaft = QtWidgets.QDoubleSpinBox()
        self.needle_shaft.setRange(10.0, 400.0)
        self.needle_shaft.setSingleStep(5.0)
        self.needle_shaft.setSuffix(" mm")
        for spin in (self.needle_diameter, self.needle_active, self.needle_shaft):
            spin.valueChanged.connect(self._on_needle_params_changed)
        needle_grid.addWidget(QtWidgets.QLabel("直径"), 0, 0)
        needle_grid.addWidget(self.needle_diameter, 0, 1)
        needle_grid.addWidget(QtWidgets.QLabel("活性端"), 1, 0)
        needle_grid.addWidget(self.needle_active, 1, 1)
        needle_grid.addWidget(QtWidgets.QLabel("针杆"), 2, 0)
        needle_grid.addWidget(self.needle_shaft, 2, 1)
        lay.addLayout(needle_grid)

        needle_row = QtWidgets.QHBoxLayout()
        needle_row.setSpacing(8)
        self.btn_needle_show = QtWidgets.QPushButton("显示针", objectName="Segment", checkable=True)
        self.btn_needle_reset = QtWidgets.QPushButton("重置针位", objectName="Segment")
        self.btn_needle_clear = QtWidgets.QPushButton("清除", objectName="Segment")
        self.btn_needle_show.toggled.connect(self._toggle_ablation_needle)
        self.btn_needle_reset.clicked.connect(self._reset_ablation_needle)
        self.btn_needle_clear.clicked.connect(self._clear_ablation_needle)
        needle_row.addWidget(self.btn_needle_show)
        needle_row.addWidget(self.btn_needle_reset)
        needle_row.addWidget(self.btn_needle_clear)
        lay.addLayout(needle_row)
        self._on_needle_preset_changed(self.needle_preset.currentText())

        # 3 - Ablation simulation (growing coagulation zone on the needle tip)
        lay.addWidget(self._section("3 · 消融仿真"))
        sim_grid = QtWidgets.QGridLayout()
        sim_grid.setHorizontalSpacing(8)
        sim_grid.setVerticalSpacing(6)
        self.sim_power = QtWidgets.QDoubleSpinBox()
        self.sim_power.setRange(5.0, 200.0)
        self.sim_power.setSingleStep(5.0)
        self.sim_power.setSuffix(" W")
        self.sim_power.setValue(30.0)
        self.sim_time = QtWidgets.QDoubleSpinBox()
        self.sim_time.setRange(30.0, 1800.0)
        self.sim_time.setSingleStep(30.0)
        self.sim_time.setSuffix(" s")
        self.sim_time.setValue(300.0)
        self.sim_speed = QtWidgets.QComboBox()
        self.sim_speed.addItems(["1×", "2×", "5×", "10×", "20×", "60×"])
        self.sim_speed.setCurrentText("20×")
        sim_grid.addWidget(QtWidgets.QLabel("功率"), 0, 0)
        sim_grid.addWidget(self.sim_power, 0, 1)
        sim_grid.addWidget(QtWidgets.QLabel("时间"), 1, 0)
        sim_grid.addWidget(self.sim_time, 1, 1)
        sim_grid.addWidget(QtWidgets.QLabel("时间倍率"), 2, 0)
        sim_grid.addWidget(self.sim_speed, 2, 1)
        lay.addLayout(sim_grid)

        self.sim_progress = QtWidgets.QProgressBar()
        self.sim_progress.setRange(0, 100)
        self.sim_progress.setValue(0)
        self.sim_progress.setTextVisible(False)
        self.sim_progress.setFixedHeight(6)
        lay.addWidget(self.sim_progress)

        self.sim_status = QtWidgets.QLabel("未开始仿真。", objectName="Status")
        self.sim_status.setWordWrap(True)
        lay.addWidget(self.sim_status)

        sim_row = QtWidgets.QHBoxLayout()
        sim_row.setSpacing(8)
        self.btn_sim_start = QtWidgets.QPushButton("开始仿真", objectName="Segment")
        self.btn_sim_pause = QtWidgets.QPushButton("暂停", objectName="Segment")
        self.btn_sim_stop = QtWidgets.QPushButton("停止", objectName="Segment")
        self.btn_sim_start.clicked.connect(self._start_simulation)
        self.btn_sim_pause.clicked.connect(self._toggle_pause_simulation)
        self.btn_sim_stop.clicked.connect(self._stop_simulation)
        self.btn_sim_pause.setEnabled(False)
        self.btn_sim_stop.setEnabled(False)
        sim_row.addWidget(self.btn_sim_start)
        sim_row.addWidget(self.btn_sim_pause)
        sim_row.addWidget(self.btn_sim_stop)
        lay.addLayout(sim_row)

        self._sim_timer = QtCore.QTimer(self)
        self._sim_timer.setInterval(40)
        self._sim_timer.timeout.connect(self._on_sim_tick)
        self._sim_running = False
        self._sim_paused = False
        self._sim_elapsed = 0.0
        self._sim_duration = 300.0
        self._sim_power_w = 30.0

        lay.addStretch(1)

        return scroll

    def _section(self, text):
        """创建一个分区标题标签，样式由 QSS #SectionTitle 控制。
        修改分区标题样式：编辑 style.py 中 QLabel#SectionTitle 的 QSS 规则。
        """
        return QtWidgets.QLabel(text, objectName="SectionTitle")

    # ============================================================
    # 数据加载
    # _load() 是核心通用加载方法，四个菜单项最终都调用它
    # ============================================================

    def _open_dicom(self):
        """打开 DICOM 文件夹的菜单响应。"""
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择 DICOM 序列文件夹")
        if d:
            self._load(lambda: loader.load_dicom_series(d), os.path.basename(d) or d)

    def _open_stack(self):
        """打开图片序列文件夹的菜单响应。"""
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择图片序列文件夹")
        if d:
            self._load(lambda: loader.load_image_stack(d), os.path.basename(d) or d)

    def _open_zip(self):
        """导入 ZIP 压缩包的菜单响应。"""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择 ZIP 压缩包", "", "ZIP 压缩包 (*.zip)")
        if path:
            self._load(lambda: loader.load_zip(path), os.path.basename(path))

    def _open_demo(self):
        """加载演示体模的菜单响应。"""
        self._load(loader.make_demo_phantom, "演示体模")

    def _load(self, fn, label):
        """通用数据加载方法（被四个菜单项复用）。
        
        工作流程：
          1. 显示等待光标，提示正在加载
          2. 执行传入的加载函数 fn()
          3. 将加载结果传递给 VolumeViewer
          4. 重建组织列表（根据数据模态）
          5. 更新状态栏显示体素信息
          6. 启用控制面板
        
        参数：
          fn    — 无参可调用对象，返回 (vtkImageData, info_dict)
          label — 用于日志和 UI 提示的标签（如文件名）
        """
        log.info("开始加载:%s", label)
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        self.statusBar().showMessage("正在加载 %s…" % label)
        QtWidgets.QApplication.processEvents()
        try:
            image, info = fn()
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            log.exception("加载失败:%s", label)
            QtWidgets.QMessageBox.critical(self, "无法加载数据", str(exc))
            self.statusBar().showMessage("加载失败。")
            return

        self._cancel_segmentation()
        self.viewer.set_volume(image, info)  # also clears any ablation zone; rebuilds 组织 model
        self._update_plan_status()  # set_volume cleared 入针点/消融点
        self._stop_simulation()  # reset sim UI to idle for the new volume
        if self.btn_needle_show.isChecked():
            self._on_needle_params_changed()
            self.viewer.reset_ablation_needle()
        self.z_spin.blockSignals(True)
        self.z_spin.setValue(1.0)
        self.z_spin.blockSignals(False)
        QtWidgets.QApplication.restoreOverrideCursor()

        modality_cn = {"CT": "CT", "MR": "MR", "IMAGE": "图片序列"}.get(
            info["modality"], info["modality"])
        dx, dy, dz = info["dimensions"]
        lo, hi = info["scalar_range"]
        self.status.setText(
            "%s · %s · %d×%d×%d 体素 · 数值范围 %d…%d"
            % (label, modality_cn, dx, dy, dz, lo, hi))
        self._set_controls_enabled(True)
        self.statusBar().showMessage("已加载 %s。" % label)
        log.info("已加载 %s(%s,%d×%d×%d 体素,范围 %d…%d)",
                 label, modality_cn, dx, dy, dz, lo, hi)

    # ============================================================
    # 显示效果控制
    # 组织层的显示/隐藏与逐层透明度已迁移到 3D 视图右键菜单「组织」，
    # 由 VolumeViewer 自己维护组织模型（见 viewer._build_tissue_menu）。
    # ============================================================

    def _min_opacity_scale(self):
        """不透明度滑块允许的最低系数（最透）。"""
        return self.opacity_slider.minimum() / 100.0

    def _set_mode(self, scale):
        """设置不透明/透明模式，更新滑块并同步 VolumeViewer。
        同时更新分段按钮的选中状态。
        """
        self.viewer.set_volume_visible(True)
        self.btn_opaque.setChecked(scale >= 0.75)
        self.btn_transparent.setChecked(scale < 0.75)
        self.opacity_slider.blockSignals(True)
        self.opacity_slider.setValue(int(scale * 100))
        self.opacity_slider.blockSignals(False)
        self.viewer.set_opacity_scale(scale)

    def _on_slider(self, value):
        """不透明度滑块拖动回调：将滑块值转为 0~1 系数，
        并自动更新不透明/透明按钮的选中状态（≥75% 为不透明）。
        """
        scale = value / 100.0
        self.btn_opaque.setChecked(scale >= 0.75)
        self.btn_transparent.setChecked(scale < 0.75)
        self.viewer.set_opacity_scale(scale)

    def _on_z_spacing_changed(self, value):
        """Z 比例微调框改变时，更新体数据的 Z 方向缩放。
        用于修正图片序列无元数据时的层间距问题。
        """
        self.viewer.set_z_spacing_factor(value)

    # ============================================================
    # 消融针规划
    # ============================================================

    def _on_needle_preset_changed(self, name):
        """消融针型号切换时，加载预设参数到面板控件。
        
        更新内容：
          - 直径、活性端长度、针杆长度（微调框）
          - 建议功率（仅仿真未运行时更新，避免中途改变参数）
        """
        data = ablation.preset_by_name(name)
        for spin, key in (
            (self.needle_diameter, "diameter_mm"),
            (self.needle_active, "active_mm"),
            (self.needle_shaft, "shaft_mm"),
        ):
            spin.blockSignals(True)
            spin.setValue(data[key])
            spin.blockSignals(False)
        # 仿真未运行时才更新建议功率
        if hasattr(self, "sim_power") and not self._sim_running:
            self.sim_power.setValue(data.get("power_w", self.sim_power.value()))
        self._on_needle_params_changed()

    def _on_needle_params_changed(self, *_):
        """消融针参数（直径/活性端/针杆长度）改变时更新 3D 视图中的针模型。"""
        if not hasattr(self, "viewer"):
            return
        self.viewer.set_ablation_needle_params(
            shaft_mm=self.needle_shaft.value(),
            active_mm=self.needle_active.value(),
            diameter_mm=self.needle_diameter.value(),
        )
        if self.btn_needle_show.isChecked() and not self.viewer.has_ablation_needle():
            self.viewer.reset_ablation_needle()

    def _toggle_ablation_needle(self, checked):
        """显示针按钮切换：显示或清除消融针三维模型。"""
        if not hasattr(self, "viewer"):
            return
        if checked:
            self._on_needle_params_changed()
            if not self.viewer.has_ablation_needle():
                self.viewer.reset_ablation_needle()
        else:
            self.viewer.clear_ablation_needle()

    def _reset_ablation_needle(self):
        """重置消融针到体数据中心默认位置。"""
        self.btn_needle_show.setChecked(True)
        self._on_needle_params_changed()
        self.viewer.reset_ablation_needle()
        self.statusBar().showMessage("消融针已重置到体数据中心路径。")

    def _clear_ablation_needle(self):
        """清除消融针（包括消融范围）。"""
        self.btn_needle_show.setChecked(False)
        self.viewer.clear_ablation_needle()
        self.statusBar().showMessage("消融针已清除。")

    # ============================================================
    # 针道规划（入针点 / 消融点）
    # 把"参考坐标轴(十字光标)"中心作为放置位置；两点放好后自动连成针道
    # ============================================================

    def _place_entry_point(self):
        """在坐标轴中心放置入针点。"""
        self._place_planning_point("entry", "入针点")

    def _place_ablation_point(self):
        """在坐标轴中心放置消融点。"""
        self._place_planning_point("tip", "消融点")

    def _place_planning_point(self, kind, label):
        """通用放置：取坐标轴中心 ijk 放置规划点，两点齐备时自动连成针道。"""
        if not hasattr(self, "viewer") or self.viewer.image is None:
            self.statusBar().showMessage("请先加载数据,再放置%s。" % label)
            return
        ijk = self.viewer.crosshair_ijk()
        if ijk is None:
            self.statusBar().showMessage("坐标轴中心不可用,无法放置%s。" % label)
            return
        connected = self.viewer.set_planning_point(kind, ijk)
        self._update_plan_status()
        if not connected:
            self.statusBar().showMessage(
                "%s已放置于坐标轴中心。移动坐标轴后放置另一个点即可自动连接。" % label)
            return
        pts = self.viewer.planning_points()
        entry, tip = pts["entry"], pts["tip"]
        if entry is not None and tip is not None and self._same_voxel(entry, tip):
            self.statusBar().showMessage(
                "%s已放置,但入针点与消融点重合;请移动坐标轴拉开距离后重放。" % label)
        else:
            self.statusBar().showMessage(
                "%s已放置,入针点与消融点已自动连成针道。" % label)

    @staticmethod
    def _same_voxel(a, b):
        """两个 ijk 是否落在同一体素（用于提醒入针点/消融点重合）。"""
        return all(round(a[i]) == round(b[i]) for i in range(3))

    def _clear_planning_points(self):
        """清除入针点/消融点及其连成的针道。"""
        if not hasattr(self, "viewer"):
            return
        self.viewer.clear_planning_points()
        self.viewer.clear_ablation_needle()
        self._update_plan_status()
        self.statusBar().showMessage("针道(入针点/消融点)已清除。")

    def _update_plan_status(self):
        """刷新入针点/消融点的放置状态文本。"""
        if not hasattr(self, "viewer") or not hasattr(self, "plan_status"):
            return
        pts = self.viewer.planning_points()

        def fmt(ijk):
            if ijk is None:
                return "未放置"
            return "(%d, %d, %d)" % (round(ijk[0]), round(ijk[1]), round(ijk[2]))

        self.plan_status.setText(
            "入针点:%s   消融点:%s" % (fmt(pts["entry"]), fmt(pts["tip"])))

    def _on_viewer_needle_changed(self, has_needle):
        """VolumeViewer 的消融针状态变化时的回调。
        
        保持「显示针」按钮与 3D 视图中消融针的实际存在状态同步。
        如果用户从切片视图右键菜单放置了消融针，按钮也会亮起。
        如果消融针被清除且仿真正在运行，则同时停止仿真。
        """
        if not has_needle and getattr(self, "_sim_running", False):
            self._stop_simulation()  # 消融范围锚定在针上，针没了就停止仿真
        if self.btn_needle_show.isChecked() == has_needle:
            return
        self.btn_needle_show.blockSignals(True)
        self.btn_needle_show.setChecked(has_needle)
        self.btn_needle_show.blockSignals(False)

    # ============================================================
    # 消融仿真控制
    # 仿真使用 Qt QTimer 以 40ms 为间隔驱动生长动画
    # ============================================================

    def _current_speed_multiplier(self):
        """获取当前时间倍率（如 "20×" → 20.0）。"""
        try:
            return float(self.sim_speed.currentText().rstrip("×"))
        except ValueError:
            return 1.0

    def _start_simulation(self):
        """开始消融仿真。
        
        执行前后检查：
          - 确保消融针已放置（消融范围锚定在针活性端）
          - 锁定功率和时间输入（仿真中不可修改）
          - 启动 40ms 的 QTimer 循环
        """
        if not hasattr(self, "viewer"):
            return
        # 确保消融针存在（范围锚定在针的活性端上）
        if not self.viewer.has_ablation_needle():
            self.btn_needle_show.setChecked(True)
            if not self.viewer.has_ablation_needle():
                self.viewer.reset_ablation_needle()
        if not self.viewer.has_ablation_needle():
            self.statusBar().showMessage("请先放置消融针,再开始消融仿真。")
            return

        self._sim_power_w = self.sim_power.value()
        self._sim_duration = self.sim_time.value()
        self._sim_elapsed = 0.0
        self._sim_running = True
        self._sim_paused = False

        self.btn_sim_pause.setText("暂停")
        self.btn_sim_start.setEnabled(False)
        self.btn_sim_pause.setEnabled(True)
        self.btn_sim_stop.setEnabled(True)
        # 仿真期间锁定功率和时间（防止中途修改）
        for w in (self.sim_power, self.sim_time):
            w.setEnabled(False)

        self._apply_zone_at_elapsed()
        self._sim_timer.start()
        self.statusBar().showMessage(
            "消融仿真开始(%.0f W,%.0f s)。" % (self._sim_power_w, self._sim_duration))
        log.info("消融仿真开始:%.0f W,%.0f s", self._sim_power_w, self._sim_duration)

    def _on_sim_tick(self):
        """仿真定时器每次触发时调用（每 40ms）。
        
        计算经过时间 = 定时间隔 × 倍率，累加到 _sim_elapsed。
        到达 _sim_duration 时自动结束。
        """
        if not self._sim_running or self._sim_paused:
            return
        dt = self._sim_timer.interval() / 1000.0
        self._sim_elapsed += dt * self._current_speed_multiplier()
        done = self._sim_elapsed >= self._sim_duration
        if done:
            self._sim_elapsed = self._sim_duration
        self._apply_zone_at_elapsed()
        if done:
            self._finish_simulation()

    def _apply_zone_at_elapsed(self):
        """根据当前消融时间计算并更新消融椭球体的大小。
        
        通过 ablation.ablation_zone_half_axes_mm() 计算当前时间点的
        长短半轴，然后传给 VolumeViewer.set_ablation_zone() 更新 3D 显示。
        同时更新进度条和状态文本。
        """
        half_long, half_short = ablation.ablation_zone_half_axes_mm(
            self._sim_power_w, self.needle_active.value(), self._sim_elapsed)
        self.viewer.set_ablation_zone(half_long, half_short)
        pct = 0 if self._sim_duration <= 0 else int(
            100.0 * self._sim_elapsed / self._sim_duration)
        self.sim_progress.setValue(max(0, min(100, pct)))
        self.sim_status.setText(
            "t = %.0f / %.0f s · 短径 %.0f mm · 长径 %.0f mm"
            % (self._sim_elapsed, self._sim_duration,
               2.0 * half_short, 2.0 * half_long))

    def _toggle_pause_simulation(self):
        """暂停/继续消融仿真。"""
        if not self._sim_running:
            return
        self._sim_paused = not self._sim_paused
        self.btn_sim_pause.setText("继续" if self._sim_paused else "暂停")
        self.statusBar().showMessage(
            "消融仿真已暂停。" if self._sim_paused else "消融仿真继续。")

    def _finish_simulation(self):
        """消融仿真自然完成（时间到达）时的处理。
        停止定时器，恢复按钮状态，保留最后的消融范围显示。
        """
        self._sim_timer.stop()
        self._sim_running = False
        self._sim_paused = False
        self.btn_sim_start.setEnabled(True)
        self.btn_sim_pause.setEnabled(False)
        self.btn_sim_pause.setText("暂停")
        self.btn_sim_stop.setEnabled(True)  # 保持启用让用户可清除最终范围
        for w in (self.sim_power, self.sim_time):
            w.setEnabled(True)
        self.statusBar().showMessage("消融仿真完成。最终消融范围已显示,点击“停止”可清除。")
        log.info("消融仿真完成(%.0f s)", self._sim_duration)

    def _stop_simulation(self):
        """强制停止消融仿真并清除消融范围显示。
        
        适用于：用户手动停止、加载新数据、消融针被清除等场景。
        """
        self._sim_timer.stop()
        self._sim_running = False
        self._sim_paused = False
        self.viewer.clear_ablation_zone()
        self.btn_sim_start.setEnabled(True)
        self.btn_sim_pause.setEnabled(False)
        self.btn_sim_pause.setText("暂停")
        self.btn_sim_stop.setEnabled(False)
        for w in (self.sim_power, self.sim_time):
            w.setEnabled(True)
        self.sim_progress.setValue(0)
        self.sim_status.setText("未开始仿真。")
        self.statusBar().showMessage("消融仿真已停止,消融范围已清除。")

    # ============================================================
    # TotalSegmentator 自动器官分割
    # ============================================================

    def _current_segmentation_preset(self):
        preset = segmentation.preset_by_name(self.seg_preset.currentText())
        if not self.seg_fast.isChecked():
            preset["fast"] = False
            preset["fastest"] = False
        preset["force_split"] = self.seg_lowmem.isChecked()
        preset["device"] = self.seg_device.currentData() or "cpu"
        return preset

    def _download_totalseg_weights(self):
        if self._seg_running:
            self.statusBar().showMessage("TotalSegmentator 正在分割,请等待完成后再下载模型。")
            return
        if self._seg_download_running:
            self.statusBar().showMessage("模型权重正在下载,请等待完成。")
            return
        if segmentation.find_totalsegmentator() is None \
                and segmentation.find_totalseg_download_weights() is None:
            QtWidgets.QMessageBox.information(
                self, "需要安装 TotalSegmentator",
                segmentation.TOTAL_SEGMENTATOR_INSTALL_HINT)
            return

        preset = self._current_segmentation_preset()
        tasks = segmentation.download_tasks_for_preset(preset)
        label = segmentation.download_tasks_display_name(tasks)
        self.seg_status.setText("准备下载/检查 %s..." % label)
        self.seg_result.setText("模型缓存目录:<br>%s" % segmentation.weights_cache_dir_hint())
        self.statusBar().showMessage("正在准备下载 TotalSegmentator 模型权重...")

        self._set_seg_download_running(True)
        self._seg_download_thread = QtCore.QThread(self)
        self._seg_download_worker = segmentation.TotalSegmentatorDownloadWorker(preset)
        self._seg_download_worker.moveToThread(self._seg_download_thread)

        self._seg_download_thread.started.connect(self._seg_download_worker.run)
        self._seg_download_worker.progress.connect(self._on_seg_download_progress)
        self._seg_download_worker.finished.connect(self._on_seg_download_finished)
        self._seg_download_worker.failed.connect(self._on_seg_download_failed)
        self._seg_download_worker.finished.connect(self._seg_download_thread.quit)
        self._seg_download_worker.failed.connect(self._seg_download_thread.quit)
        self._seg_download_worker.finished.connect(self._seg_download_worker.deleteLater)
        self._seg_download_worker.failed.connect(self._seg_download_worker.deleteLater)
        self._seg_download_thread.finished.connect(self._on_seg_download_thread_finished)
        self._seg_download_thread.finished.connect(self._seg_download_thread.deleteLater)
        self._seg_download_thread.start()

    def _on_seg_download_progress(self, text):
        text = segmentation.clean_progress_text(text)
        if not text:
            return
        short = text if len(text) <= 120 else text[:117] + "..."
        self.seg_status.setText(short)
        self.statusBar().showMessage(short)
        log.info("TotalSegmentator 权重下载: %s", text)

    def _on_seg_download_finished(self, task):
        label = segmentation.download_task_display_name(task)
        self.seg_status.setText("%s 已下载/已确认,可以直接运行自动分割。" % label)
        self.seg_result.setText("模型缓存目录:<br>%s" % segmentation.weights_cache_dir_hint())
        self.statusBar().showMessage("TotalSegmentator 模型权重已准备好。")

    def _on_seg_download_failed(self, message):
        self.seg_status.setText("模型权重下载失败。")
        QtWidgets.QMessageBox.warning(self, "TotalSegmentator 模型下载失败", message)
        self.statusBar().showMessage("TotalSegmentator 模型权重下载失败。")

    def _on_seg_download_thread_finished(self):
        self._set_seg_download_running(False)
        self._seg_download_thread = None
        self._seg_download_worker = None

    def _run_totalsegmentator(self):
        if not hasattr(self, "viewer") or self.viewer.image is None:
            self.statusBar().showMessage("请先加载 CT 数据,再运行自动器官分割。")
            return
        modality = (self.viewer.info or {}).get("modality")
        if modality != "CT":
            QtWidgets.QMessageBox.information(
                self, "仅支持 CT 数据",
                "自动器官分割依赖 CT 的 HU 值,当前数据模态为 %s。\n"
                "请加载 CT(DICOM)后再运行分割;图片序列/MR 会得到错误结果。"
                % (modality or "未知"))
            self.statusBar().showMessage("自动分割已取消:当前数据不是 CT。")
            return
        if self._seg_download_running:
            self.statusBar().showMessage("模型权重正在下载,请等待完成后再分割。")
            return
        if self._seg_thread is not None and self._seg_thread.isRunning():
            self.statusBar().showMessage("TotalSegmentator 正在运行,请等待完成。")
            return
        if segmentation.find_totalsegmentator() is None:
            QtWidgets.QMessageBox.information(
                self, "需要安装 TotalSegmentator",
                segmentation.TOTAL_SEGMENTATOR_INSTALL_HINT)
            return

        self._seg_cancelled = False
        self._cleanup_seg_temp_dir()
        self._seg_temp_dir = tempfile.mkdtemp(prefix="ctto3d_totalseg_")
        input_path = os.path.join(self._seg_temp_dir, "ct_input.nii")
        output_dir = os.path.join(self._seg_temp_dir, "segmentations")
        os.makedirs(output_dir, exist_ok=True)

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        self.seg_status.setText("正在导出临时 NIfTI...")
        self.statusBar().showMessage("正在准备 TotalSegmentator 输入数据...")
        QtWidgets.QApplication.processEvents()
        try:
            segmentation.write_nifti(self.viewer.image, input_path)
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            self._cleanup_seg_temp_dir()
            QtWidgets.QMessageBox.critical(self, "导出 NIfTI 失败", str(exc))
            return
        QtWidgets.QApplication.restoreOverrideCursor()

        self._set_segmentation_running(True)
        preset = self._current_segmentation_preset()
        self._seg_thread = QtCore.QThread(self)
        self._seg_worker = segmentation.TotalSegmentatorWorker(
            input_path, output_dir, preset)
        self._seg_worker.moveToThread(self._seg_thread)

        self._seg_thread.started.connect(self._seg_worker.run)
        self._seg_worker.progress.connect(self._on_segmentation_progress)
        self._seg_worker.finished.connect(self._on_segmentation_finished)
        self._seg_worker.failed.connect(self._on_segmentation_failed)
        self._seg_worker.finished.connect(self._seg_thread.quit)
        self._seg_worker.failed.connect(self._seg_thread.quit)
        self._seg_worker.finished.connect(self._seg_worker.deleteLater)
        self._seg_worker.failed.connect(self._seg_worker.deleteLater)
        self._seg_thread.finished.connect(self._on_segmentation_thread_finished)
        self._seg_thread.finished.connect(self._seg_thread.deleteLater)
        self._seg_thread.start()
        self.statusBar().showMessage("TotalSegmentator 已启动,首次运行可能需要下载模型。")

    def _on_segmentation_progress(self, text):
        text = segmentation.clean_progress_text(text)
        if not text:
            return
        short = text if len(text) <= 120 else text[:117] + "..."
        self.seg_status.setText(short)
        self.statusBar().showMessage(short)
        log.info("TotalSegmentator: %s", text)

    def _on_segmentation_finished(self, output_dir, expected_names):
        if self._seg_cancelled:
            self._cleanup_seg_temp_dir()
            return
        self.seg_status.setText("分割完成,正在载入 mask...")
        self.statusBar().showMessage("正在载入 TotalSegmentator 输出结果...")
        QtWidgets.QApplication.processEvents()
        try:
            loaded, skipped, loaded_items = self._load_segmentation_masks(output_dir, expected_names)
        except Exception as exc:
            log.exception("载入 TotalSegmentator 输出失败")
            QtWidgets.QMessageBox.critical(self, "载入分割失败", str(exc))
            self.seg_status.setText("分割完成,但结果载入失败。")
            return
        finally:
            self._cleanup_seg_temp_dir()

        loaded_names = [item["name"] for item in loaded_items]
        if loaded_names:
            self._highlight_segmentations()
            self.seg_result.setText(
                "已加载 %d 个肺部分割部位。<br>"
                "在 3D 视图右键可选择显示部位、查看颜色并调整透明度。" % loaded)
            log.info("TotalSegmentator 已载入器官分割: %s", ", ".join(
                "%s(%.1fml/%dvox)" % (
                    item["name"], item["volume_ml"], item["voxels"])
                for item in loaded_items))
        else:
            self.seg_result.setText("")
        self.seg_status.setText("已载入 %d 个器官分割%s。" % (
            loaded, "" if skipped == 0 else ",跳过 %d 个空/不匹配 mask" % skipped))
        self.statusBar().showMessage("TotalSegmentator 分割结果已叠加到 3D 视图。")

    def _on_segmentation_failed(self, message):
        self._cleanup_seg_temp_dir()
        if self._seg_cancelled:
            self.seg_status.setText("自动分割已取消。")
            self.statusBar().showMessage("TotalSegmentator 自动分割已取消。")
            return
        self.seg_status.setText("自动分割失败。")
        QtWidgets.QMessageBox.warning(self, "TotalSegmentator 失败", message)
        self.statusBar().showMessage("TotalSegmentator 自动分割失败。")

    def _on_segmentation_thread_finished(self):
        self._set_segmentation_running(False)
        self._seg_thread = None
        self._seg_worker = None

    def _load_segmentation_masks(self, output_dir, expected_names):
        files = segmentation.mask_files(output_dir, expected_names)
        if not files:
            raise RuntimeError("没有找到 TotalSegmentator 输出的 .nii/.nii.gz mask。")

        loaded = 0
        skipped = 0
        loaded_items = []
        self.viewer.begin_segmentation_update()
        try:
            self.viewer.clear_segmentations()
            total_files = len(files)
            for index, path in enumerate(files, start=1):
                name = path.name
                if name.endswith(".nii.gz"):
                    name = name[:-7]
                elif name.endswith(".nii"):
                    name = name[:-4]
                if index == 1 or index % 5 == 0 or index == total_files:
                    self.seg_status.setText("正在载入彩色器官 %d/%d: %s" % (
                        index, total_files, segmentation.segment_display_name(name)))
                    QtWidgets.QApplication.processEvents()
                mask = segmentation.read_nifti(path, self.viewer.image)
                color = segmentation.segment_color(name)
                opacity = self._segmentation_opacity(name)
                if self.viewer.add_segmentation_mask(name, mask, color, opacity):
                    loaded += 1
                    stats = segmentation.mask_statistics(mask)
                    loaded_items.append({
                        "name": name,
                        "voxels": stats["voxels"],
                        "volume_ml": stats["volume_ml"],
                    })
                else:
                    skipped += 1
        finally:
            self.viewer.end_segmentation_update()
        return loaded, skipped, loaded_items

    def _segmentation_opacity(self, name):
        if name.startswith("lung_") and name.endswith(("_left", "_right")):
            return 0.28
        if name in ("lung_airways_wall",):
            return 0.62
        if any(token in name for token in ("airways", "arteries", "veins", "trachea")):
            return 1.0
        return 0.96

    def _clear_segmentations(self):
        if hasattr(self, "viewer"):
            self.viewer.clear_segmentations()
            self.viewer.set_volume_visible(True)
        self.seg_status.setText("分割叠加已清除。")
        self.seg_result.setText("")
        self.statusBar().showMessage("分割叠加已清除。")

    def _highlight_segmentations(self):
        if not hasattr(self, "viewer") or not self.viewer.segmentation_names():
            self.statusBar().showMessage("当前没有可突出的分割叠加。")
            return
        self.viewer.set_volume_visible(True)
        # 分割完成后把整体不透明度拉到最低，最大限度透出彩色分割
        self._set_mode(self._min_opacity_scale())
        self.viewer.set_volume_visible(True)
        self.statusBar().showMessage("已保留原始 CT 并调淡,突出彩色肺部分割。")

    def _format_segmentation_result(self, loaded_items):
        rows = []
        max_rows = 36
        for item in loaded_items[:max_rows]:
            name = item["name"]
            color = segmentation.segment_color(name)
            rgb = tuple(max(0, min(255, int(c * 255))) for c in color)
            label = segmentation.segment_display_name(name)
            rows.append(
                '<span style="color:rgb(%d,%d,%d);">■</span> %s %.1f ml / %d 体素' %
                (rgb[0], rgb[1], rgb[2], label,
                 item["volume_ml"], item["voxels"]))
        if len(loaded_items) > max_rows:
            rows.append("... 还有 %d 个器官已加载到 3D/三视图中" % (
                len(loaded_items) - max_rows))
        return "本次分割:<br>" + "<br>".join(rows)

    def _cancel_segmentation(self):
        if self._seg_worker is not None and self._seg_thread is not None \
                and self._seg_thread.isRunning():
            self._seg_cancelled = True
            self._seg_worker.cancel()
            self.seg_status.setText("正在取消自动分割...")
        else:
            self._cleanup_seg_temp_dir()

    def _cancel_seg_download(self):
        if self._seg_download_worker is not None and self._seg_download_thread is not None \
                and self._seg_download_thread.isRunning():
            self._seg_download_worker.cancel()

    def _cleanup_seg_temp_dir(self):
        if self._seg_temp_dir and os.path.isdir(self._seg_temp_dir):
            shutil.rmtree(self._seg_temp_dir, ignore_errors=True)
        self._seg_temp_dir = None

    def _set_segmentation_running(self, running):
        self._seg_running = running
        self._update_segmentation_controls()

    def _set_seg_download_running(self, running):
        self._seg_download_running = running
        self._update_segmentation_controls()

    def _update_segmentation_controls(self):
        has_image = hasattr(self, "viewer") and self.viewer.image is not None
        idle = not self._seg_running and not self._seg_download_running
        self.seg_preset.setEnabled(idle)
        self.seg_fast.setEnabled(idle)
        self.seg_lowmem.setEnabled(idle)
        self.seg_device.setEnabled(idle)
        self.btn_seg_download.setEnabled(idle)
        self.btn_seg_run.setEnabled(idle and has_image)
        self.btn_seg_focus.setEnabled(idle and has_image)
        self.btn_seg_clear.setEnabled(idle and has_image)
        self.btn_seg_run.setText("分割中..." if self._seg_running else "自动分割")
        self.btn_seg_download.setText("下载中..." if self._seg_download_running else "下载模型")

    # ============================================================
    # 导出功能
    # ============================================================

    def _save_screenshot(self):
        """保存当前 3D 视图的截图（PNG 格式，2× 超采样抗锯齿）。"""
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "保存截图", "ctto3d_view.png", "PNG 图片 (*.png)")
        if path:
            self.viewer.save_screenshot(path)
            self.statusBar().showMessage("截图已保存:%s" % path)
            log.info("截图已保存:%s", path)

    def _export_mesh(self):
        """导出三维网格模型（STL/OBJ/PLY）。
        
        使用 FlyingEdges 算法提取等值面 + WindowedSinc 平滑处理，
        阈值由当前勾选的最致密组织的 iso 值决定。
        """
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出三维模型", "ctto3d_model.stl",
            "STL 模型 (*.stl);;OBJ 模型 (*.obj);;PLY 模型 (*.ply)")
        if not path:
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            iso = self.viewer.export_mesh(path)
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            log.exception("导出三维模型失败:%s", path)
            QtWidgets.QMessageBox.critical(self, "导出失败", str(exc))
            return
        QtWidgets.QApplication.restoreOverrideCursor()
        self.statusBar().showMessage("三维模型已保存(阈值=%d):%s" % (iso, path))
        log.info("三维模型已保存(阈值=%d):%s", iso, path)

    # ============================================================
    # 杂项
    # ============================================================

    def _set_controls_enabled(self, on):
        """批量启用/禁用所有数据相关的控件。
        
        在加载数据前（无数据时）禁用所有操作控件，
        加载成功后启用。控件列表需要保持与 UI 构建中的控件一致。
        若启用到禁用切换且仿真正在运行，则同时停止仿真。
        """
        for w in (self.btn_opaque, self.btn_transparent, self.opacity_slider,
                  self.z_spin, self.needle_preset, self.needle_diameter,
                  self.needle_active, self.needle_shaft, self.btn_needle_show,
                  self.btn_needle_reset, self.btn_needle_clear,
                  self.btn_plan_entry, self.btn_plan_tip, self.btn_plan_clear,
                  self.sim_power, self.sim_time, self.sim_speed, self.btn_sim_start,
                  self.act_reset, self.act_shot, self.act_mesh):
            w.setEnabled(on)
        self._update_segmentation_controls()
        if not on and getattr(self, "_sim_running", False):
            self._stop_simulation()

    def showEvent(self, event):
        """窗口首次显示时：应用标题栏颜色 + 初始化 VTK 交互器。"""
        super().showEvent(event)
        self._apply_titlebar()
        self.viewer.initialize()

    def closeEvent(self, event):
        self._cancel_segmentation()
        self._cancel_seg_download()
        super().closeEvent(event)

    def _apply_titlebar(self):
        """让 Windows 原生标题栏匹配当前主题的深色/浅色。
        
        使用 Windows DWM API 设置 DWMWA_USE_IMMERSIVE_DARK_MODE 属性。
        先尝试 Win11/Win10 1909+ 的属性 ID (20)，
        失败则回退到旧版属性 ID (19)。
        非 Windows 平台不执行任何操作。
        修改标题栏外观会影响到这里。
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = wintypes.HWND(int(self.winId()))
            value = ctypes.c_int(1 if self._theme == "dark" else 0)
            set_attr = ctypes.windll.dwmapi.DwmSetWindowAttribute
            # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (Win11 / Win10 1909+)
            # 19 = DWMWA_USE_IMMERSIVE_DARK_MODE (Win10 1809-)
            for attr in (20, 19):
                if set_attr(hwnd, ctypes.c_int(attr),
                            ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
        except Exception:
            pass
