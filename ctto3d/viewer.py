"""
3D 体渲染视图 + 三向正交切片视图模块

============================================================
模块功能
============================================================
本模块是应用核心的可视化组件，负责所有医学影像的 3D/2D 显示。

主要类：
  VolumeViewer           — 主 3D 体渲染视图 + 消融针 + 消融范围显示
  SliceSlider            — 支持点击跳转的切片导航滑块
  SliceView              — 单个 2D 正交切片视图（轴状/冠状/矢状位之一）
  ExpandedSliceOverlay   — 放大切片浮层（双击切片视图时展开）
  OrthogonalSlicesPanel  — 右侧三向切片预览面板容器

VTK 渲染管线：
  体渲染：vtkImageData → vtkSmartVolumeMapper → vtkVolume → vtkRenderer
  管道自动选择：小数据 GPU 光线投射，大数据 CPU 光线投射
  传递函数由 presets.build_composite() 动态合成

修改方法：
  - 3D 背景色：修改 BG_TOP 和 BG_BOTTOM 元组（RGB 0~1）
  - 切片视图背景色：修改 SliceView 中的 SetBackground() 参数
  - 消融针颜色：修改 _rebuild_ablation_actors() 中各 make_xxx_actor 的 color 参数
  - 消融区颜色：修改 _make_zone_actor() 中的 SetColor/SetOpacity 参数
  - 标注点颜色：修改 _make_slice_point_actor() 中的 SetColor 参数
  - 重置视角方向：修改 reset_view() 中的 SetViewUp 和 SetPosition
  - 方向标记位置：修改 _add_orientation_marker() 中的 SetViewport 参数
  - 消融针角度叠加文字：修改 _make_needle_angle_actor() 中的字体大小和位置
============================================================
"""

import logging
import math
import os

import numpy as np
import vtk
from PySide6 import QtCore, QtGui, QtWidgets
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor

from . import needle_planning, presets, segmentation, style

log = logging.getLogger(__name__)

# 3D 视口背景色（纯黑，与医院阅片环境保持一致）
BG_TOP = (0.0, 0.0, 0.0)
BG_BOTTOM = (0.0, 0.0, 0.0)
# 切片方向 → VTK 轴索引的映射
SLICE_AXIS = {"sagittal": 0, "coronal": 1, "axial": 2}

# 切片右键菜单里的典型 CT 窗宽/窗位（HU），取放射科常用档位。
# 只对 CT 数据有意义，其它模态的切片菜单里不列出。
CT_WINDOW_PRESETS = (
    ("腹部软组织", 400.0, 50.0),
    ("肝脏", 150.0, 60.0),
    ("纵隔", 350.0, 50.0),
    ("肺", 1500.0, -600.0),
    ("骨", 1800.0, 400.0),
    ("脑", 80.0, 40.0),
)

# 参考坐标系（十字光标）配色
#   每个世界轴一个颜色：X=红, Y=绿, Z=蓝（医学/工程惯例）
#   拖动中心时整组变为高亮绿，原点球用中性白
CROSSHAIR_AXIS_COLORS = {
    0: (0.96, 0.28, 0.28),   # X 红
    1: (0.96, 0.86, 0.20),   # Y 黄（避免与"可移动"高亮绿撞色）
    2: (0.34, 0.56, 0.98),   # Z 蓝
}
CROSSHAIR_DRAG_COLOR = (0.20, 0.95, 0.45)   # 按下并拖动中心时的高亮绿
CROSSHAIR_ORIGIN_COLOR = (0.92, 0.92, 0.96)
# 3D 参考坐标轴屏幕线宽（像素）；不随相机缩放变化
CROSSHAIR_3D_LINE_WIDTH = 2.2


class _StayOpenMenu(QtWidgets.QMenu):
    """点击带 keepOpen 属性的项时不关闭菜单，便于连续勾选。

    用于 3D 右键的"分割部位"子菜单：连续点不同器官的显示/隐藏，
    不必每点一次就重新右键。其它(不带 keepOpen 的)项仍正常点击即关闭。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style.style_rounded_menu(self)

    def mouseReleaseEvent(self, event):
        action = self.activeAction()
        if (action is not None and action.isEnabled()
                and action.property("keepOpen")):
            action.trigger()   # 触发对应槽（切换可见性 + 更新勾选标记）
            return             # 不调用 super → 菜单保持打开
        super().mouseReleaseEvent(event)


class _LayerMenuRow(QtWidgets.QWidget):
    """右键菜单里的一行图层控件：色块 + 复选框(名称) + 透明度滑块 + 百分比。

    通过 QWidgetAction 嵌入 QMenu 使用：在行内点复选框/拖滑块时菜单不关闭，
    可以连续调整多个图层(组织层 / 分割器官)。每行独立控制显示和透明度。
    """

    def __init__(self, name, label, color, visible, opacity,
                 on_visible, on_opacity, parent=None):
        super().__init__(parent)
        self._name = name
        self._on_visible = on_visible
        self._on_opacity = on_opacity

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 10, 2)
        lay.setSpacing(7)

        swatch = QtWidgets.QLabel()
        swatch.setFixedSize(12, 12)
        r, g, b = (max(0, min(255, int(float(c) * 255))) for c in color[:3])
        swatch.setStyleSheet(
            "background:rgb(%d,%d,%d); border-radius:3px;"
            "border:1px solid rgba(0,0,0,0.25);" % (r, g, b))
        lay.addWidget(swatch)

        self.check = QtWidgets.QCheckBox(label)
        self.check.setChecked(visible)
        self.check.setMinimumWidth(104)
        self.check.toggled.connect(self._on_toggle)
        lay.addWidget(self.check)

        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setValue(int(round(float(opacity) * 100)))
        self.slider.setFixedWidth(110)
        self.slider.setEnabled(visible)
        self.slider.valueChanged.connect(self._on_slide)
        lay.addWidget(self.slider)

        self.pct = QtWidgets.QLabel("%d%%" % int(round(float(opacity) * 100)))
        self.pct.setFixedWidth(38)
        self.pct.setEnabled(visible)
        self.pct.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight
                              | QtCore.Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.pct)

        # 拖动滑块时 valueChanged 每像素一报，而回调会重建传递函数并渲染
        # 整个 3D 视图——按 ~25fps 合批，释放滑块时立即落最终值。
        self._pending_opacity = None
        self._opacity_flush = QtCore.QTimer(self)
        self._opacity_flush.setSingleShot(True)
        self._opacity_flush.setInterval(40)
        self._opacity_flush.timeout.connect(self._flush_opacity)
        self.slider.sliderReleased.connect(self._flush_opacity)

    def _on_toggle(self, on):
        self.slider.setEnabled(on)
        self.pct.setEnabled(on)
        self._on_visible(self._name, on)

    def _on_slide(self, value):
        self.pct.setText("%d%%" % value)
        self._pending_opacity = value / 100.0
        self._opacity_flush.start()

    def _flush_opacity(self):
        if self._pending_opacity is None:
            return
        value, self._pending_opacity = self._pending_opacity, None
        self._on_opacity(self._name, value)

    def set_visible_silent(self, on):
        """供"全部显示/隐藏"批量刷新行内复选框状态，不触发回调。"""
        self.check.blockSignals(True)
        self.check.setChecked(on)
        self.check.blockSignals(False)
        self.slider.setEnabled(on)
        self.pct.setEnabled(on)


class _WindowLevelRow(QtWidgets.QWidget):
    """切片右键菜单里的一行窗值控件：名称 + 滑块 + 数值。

    通过 QWidgetAction 嵌入 QMenu 使用：拖滑块时菜单不关闭，可以边拖边看
    切片变化。窗宽、窗位各占一行。
    """

    def __init__(self, label, minimum, maximum, value, on_change, parent=None):
        super().__init__(parent)
        self._on_change = on_change
        self.setObjectName("WindowLevelRow")

        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(6, 1, 8, 1)
        lay.setSpacing(5)

        name = QtWidgets.QLabel(label)
        name.setFixedWidth(28)
        lay.addWidget(name)

        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.setRange(int(round(minimum)), int(round(maximum)))
        self.slider.setValue(self._clamped(value))
        self.slider.setFixedWidth(118)
        self.slider.valueChanged.connect(self._on_slide)
        lay.addWidget(self.slider)

        self.value_label = QtWidgets.QLabel(str(self.slider.value()))
        self.value_label.setFixedWidth(40)
        self.value_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight
                                      | QtCore.Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.value_label)

        # 同 _LayerMenuRow：窗值拖动每像素一报，合批到 ~33fps，释放时立即生效。
        self._pending_value = None
        self._wl_flush = QtCore.QTimer(self)
        self._wl_flush.setSingleShot(True)
        self._wl_flush.setInterval(30)
        self._wl_flush.timeout.connect(self._flush_change)
        self.slider.sliderReleased.connect(self._flush_change)

    def _clamped(self, value):
        return int(round(max(self.slider.minimum(),
                             min(self.slider.maximum(), float(value)))))

    def _on_slide(self, value):
        self.value_label.setText(str(int(value)))
        self._pending_value = float(value)
        self._wl_flush.start()

    def _flush_change(self):
        if self._pending_value is None:
            return
        value, self._pending_value = self._pending_value, None
        self._on_change(value)

    def set_value_silent(self, value):
        """预设/默认档改变窗值后同步滑块，不回调（避免来回打架）。"""
        self.slider.blockSignals(True)
        self.slider.setValue(self._clamped(value))
        self.slider.blockSignals(False)
        self.value_label.setText(str(self.slider.value()))


class VolumeViewer(QtWidgets.QWidget):
    """三维体渲染主视图 + 消融针/消融区/标注点管理。
    
    功能概览：
      - 3D 体渲染（支持 GPU/CPU 混合光线投射）
      - 多组织合成传递函数（通过 presets 模块动态构建）
      - 三向正交切片面板（轴状位/冠状位/矢状位预览）
      - 放大切片浮层
      - 消融针三维模型（针杆 + 活性端 + 针尖球 + 入针点球）
      - 消融区椭球体（随仿真时间动态生长）
      - 标注点（3D 球体 + 2D 切片投影）
      - 方向标记（坐标系指示器）
      - 消融针进针角度 HUD 叠加文字
      - 截图导出和网格（STL/OBJ/PLY）导出
    
    信号：
      ablationNeedleChanged(bool)
        消融针创建/更新时发出 True，清除时发出 False。
        用于让主窗口的「显示针」按钮与实际状态保持同步。
    """
    ablationNeedleChanged = QtCore.Signal(bool)
    # 入针点/消融点被放置、更新或清除时发出。主窗口据此刷新规划状态行与
    # 三步指示条——放置入口不止左面板按钮（切片右键、推荐结果回填等），
    # 状态不能只依赖按钮回调自己刷新。
    planningChanged = QtCore.Signal()
    # 3D 视图交互（旋转/平移/缩放）结束时发出；主窗口用它在空闲时段
    # 刷新弹层毛玻璃背景缓存，保证下次打开弹窗时背景是当前视角。
    interactionEnded = QtCore.Signal()
    # 新体数据接管后的分帧首帧渲染（3D + 三向切片）全部完成时发出。
    # 主窗口据此在画面稳定后预抓弹层背景，避免抓到未完成的黑帧。
    initialRendersFinished = QtCore.Signal()
    # Keep the complete projected CT volume visible while using almost all of
    # the available viewport.  Unlike a fixed zoom, this adapts to both the
    # scan proportions and the current 3D panel aspect ratio.
    DEFAULT_VIEW_MARGIN = 1.03

    def __init__(self, parent=None):
        """
        初始化 VolumeViewer：创建 VTK 渲染管线、消融针/区管线、
        切片面板、放大浮层等全套组件。
        """
        super().__init__(parent)
        self._main_layout = QtWidgets.QHBoxLayout(self)
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self._main_layout.setSpacing(0)

        # 3D 视图外包一层 QFrame，用于单击选中时显示金色外框（与切片视图统一）
        self.view3d_frame = QtWidgets.QFrame(self)
        self.view3d_frame.setObjectName("View3DFrame")
        self.view3d_frame.setProperty("active", False)
        self.view3d_frame.setProperty("fullscreen", False)
        _view3d_layout = QtWidgets.QVBoxLayout(self.view3d_frame)
        _view3d_layout.setContentsMargins(0, 0, 0, 0)
        _view3d_layout.setSpacing(0)
        self._view3d_fullscreen = False

        self.vtk_widget = QVTKRenderWindowInteractor(self.view3d_frame)
        self.vtk_widget.installEventFilter(self)
        _view3d_layout.addWidget(self.vtk_widget)
        # During a fullscreen resize, keep the last completed VTK frame visible
        # above the native render widget.  Qt can then present the new layout
        # immediately instead of waiting for a large synchronous volume render.
        # 覆盖层的父控件是 viewer 而非 view3d_frame：它必须能在 frame 改变
        # 几何之前就先铺到目标矩形，否则 GL 原生窗口 resize 的瞬间 DWM 会
        # 把旧帧拉伸铺满新窗口，内容先被拉宽再跳回截图，形成闪变。
        self._view3d_transition_overlay = QtWidgets.QLabel(self)
        self._view3d_transition_overlay.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignCenter)
        self._view3d_transition_overlay.setStyleSheet("background: #000000;")
        self._view3d_transition_overlay.setScaledContents(False)
        self._view3d_transition_overlay.setAttribute(
            QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._view3d_transition_overlay.setVisible(False)
        self._view3d_transition_image = QtGui.QImage()
        self._view3d_transition_opacity = QtWidgets.QGraphicsOpacityEffect(
            self._view3d_transition_overlay)
        self._view3d_transition_overlay.setGraphicsEffect(
            self._view3d_transition_opacity)
        self._view3d_transition_fade = QtCore.QPropertyAnimation(
            self._view3d_transition_opacity, b"opacity", self)
        self._view3d_transition_fade.setDuration(140)
        self._view3d_transition_fade.setEasingCurve(
            QtCore.QEasingCurve.Type.OutCubic)
        self._view3d_transition_fade.finished.connect(
            self._complete_view3d_transition_overlay)
        # 布局只管理一个轻量占位槽；真正的原生 QVTK frame 作为同级覆盖层
        # 跟随槽位。若直接把 frame 放在 layout 中再手动全屏，Qt 会不断把它
        # 拉回 cell，导致 1542↔2204 尺寸振荡和多次昂贵 Render()。
        self._view3d_slot = QtWidgets.QWidget(self)
        self._view3d_slot.setObjectName("View3DSlot")
        self._view3d_slot.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        self._view3d_slot.installEventFilter(self)
        self._main_layout.addWidget(self._view3d_slot, 1)

        self.render_window = self.vtk_widget.GetRenderWindow()
        self.renderer = vtk.vtkRenderer()
        self.renderer.SetBackground(*BG_BOTTOM)
        self.renderer.SetBackground2(*BG_TOP)
        self.renderer.GradientBackgroundOn()
        # 多重采样 + FXAA：坐标轴/针道等线框边缘更清晰，减轻锯齿发糊
        try:
            self.render_window.SetMultiSamples(8)
        except Exception:
            pass
        try:
            self.renderer.UseFXAAOn()
        except Exception:
            pass
        self.render_window.AddRenderer(self.renderer)
        # 平行(正交)投影：避免透视下平行线汇聚——这样无论参考十字拖到哪里，
        # 它的轴向都与左下角方向标记完全一致；对手术规划也更准(无近大远小畸变)。
        self.renderer.GetActiveCamera().ParallelProjectionOn()

        self.interactor = self.render_window.GetInteractor()
        self.interactor.SetInteractorStyle(vtk.vtkInteractorStyleTrackballCamera())
        # 交互结束事件：用于空闲时段刷新弹层背景缓存（见 interactionEnded）
        self.interactor.AddObserver("EndInteractionEvent",
                                    self._on_vtk_end_interaction)

        # A maximised volume viewport contains several times as many pixels.
        # Render the resize as a fast interactive frame first, then restore
        # still-image quality after the Qt layout has settled. Repeated toggles
        # restart one timer instead of queuing multiple expensive renders.
        self._view3d_refine_timer = QtCore.QTimer(self)
        self._view3d_refine_timer.setSingleShot(True)
        # Leave enough time for the low-resolution frame to reach the screen
        # before the expensive sharp render starts on the UI thread.
        self._view3d_refine_timer.setInterval(280)
        self._view3d_refine_timer.timeout.connect(
            self._finish_view3d_resize_render)
        # When collapsing from fullscreen the viewport shrinks, so the
        # low-resolution present frame is already sharp enough on the smaller
        # viewport. The expensive full-quality render is deferred to this
        # silent timer so it does not freeze the UI during the collapse motion
        # the user is actively watching. Any new toggle/load cancels it.
        self._view3d_silent_refine_timer = QtCore.QTimer(self)
        self._view3d_silent_refine_timer.setSingleShot(True)
        self._view3d_silent_refine_timer.setInterval(450)
        self._view3d_silent_refine_timer.timeout.connect(
            self._silent_refine_render)
        self._view3d_present_timer = QtCore.QTimer(self)
        self._view3d_present_timer.setSingleShot(True)
        self._view3d_present_timer.setInterval(25)
        self._view3d_present_timer.timeout.connect(
            self._present_view3d_after_resize)
        self._slice_resume_timer = QtCore.QTimer(self)
        self._slice_resume_timer.setSingleShot(True)
        self._slice_resume_timer.setInterval(25)
        self._slice_resume_timer.timeout.connect(
            self._resume_next_slice_after_fullscreen)
        self._pending_slice_resumes = []
        self._view3d_resize_quality = None
        self._view3d_fullscreen_transition = False
        self._view3d_prerendered_reveal = False
        self._view3d_lazy_pending = False
        # 几何同步合并调度标志 + 过渡图缩放缓存键（(cacheKey, 目标高度)）。
        # 没有它们，全屏期间 LayoutRequest → setPixmap → LayoutRequest 会
        # 形成每帧重复缩放大图的反馈循环，把过渡动画拖出明显掉帧。
        self._view3d_geometry_pending = False
        self._view3d_overlay_pixmap_key = None
        self._view3d_lazy_restore_timer = QtCore.QTimer(self)
        self._view3d_lazy_restore_timer.setSingleShot(True)
        self._view3d_lazy_restore_timer.setInterval(250)
        self._view3d_lazy_restore_timer.timeout.connect(
            lambda: self._finish_view3d_resize_render(render=False))
        # 新体数据接管后把 3D 和三张切片拆到不同事件循环帧绘制，避免
        # “加载显示”最后一步一次性同步执行十余次 Render()。
        self._initial_render_timer = QtCore.QTimer(self)
        self._initial_render_timer.setSingleShot(True)
        self._initial_render_timer.setInterval(0)
        self._initial_render_timer.timeout.connect(
            self._render_next_initial_view)
        self._pending_initial_renders = []

        # Volume rendering pipeline (built lazily once data is loaded).
        self.image = None
        self.info = None
        self.mapper = None
        self.volume = None
        self.vol_property = None
        self._tissue_states = {}
        self._opacity_scale = 1.0
        # 组织模型(从左侧面板迁移到 3D 右键菜单)：每种组织层的显示开关和透明度系数。
        self._tissue_order = []
        self._tissue_visible = {}
        self._tissue_opacity = {}
        self._orientation = None
        self._base_spacing = None
        self._z_factor = 1.0
        self._point_annotations = []
        self._next_point_id = 1
        self._segmentation_actors = {}
        self._segmentation_masks = {}
        self._segmentation_opacity = 1.0
        self._segmentation_update_depth = 0
        self._segmentation_update_dirty = False
        self._ablation_needle = None
        self._ablation_actors = []
        self._ablation_params = {
            "shaft_mm": 150.0,
            "active_mm": 5.0,
            "diameter_mm": 1.6,
        }
        # 入针点/消融点规划：把坐标轴(参考十字)中心作为放置点，两点齐备后
        # 自动连成针道(复用消融针可视化：入针点=针入口，消融点=针尖)
        self._planning_points = {"entry": None, "tip": None}
        self._planning_actors = {"entry": None, "tip": None}
        self._cjk_font = self._find_cjk_font()
        self._needle_angle_actor = self._make_needle_angle_actor()
        # 消融针传感器(IMU)实时读数 HUD：文字显示温度、姿态角和磁方位，
        # 立体针跟着实时姿态转动，绿色箭头示意三轴磁场方向。
        self._imu_temp_actor = self._make_imu_temp_actor()
        self._imu_needle_assembly = None
        self._imu_needle_renderer = self._make_imu_needle_renderer()
        # HUD 角上再放一套人体坐标轴，和左下角方向标记同源；两个叠加视口的
        # 相机每帧都对齐到主相机，所以坐标轴和立体针跟着人体一起转。
        self._imu_axes_actor = None
        self._imu_magnetic_actor = None
        self._imu_axes_renderer = self._make_imu_axes_renderer()
        self.render_window.AddObserver(
            vtk.vtkCommand.StartEvent, self._sync_imu_hud_cameras)
        self._imu_readout = None

        # Ablation-zone (growing ellipsoid) pipeline — built once, re-shaped per
        # animation frame via its transform; shared with the slice cross-sections.
        self._ablation_zone = None
        self._zone_sphere = vtk.vtkSphereSource()
        self._zone_sphere.SetRadius(1.0)
        self._zone_sphere.SetThetaResolution(48)
        self._zone_sphere.SetPhiResolution(32)
        self._zone_transform = vtk.vtkTransform()
        self._zone_tpf = vtk.vtkTransformPolyDataFilter()
        self._zone_tpf.SetInputConnection(self._zone_sphere.GetOutputPort())
        self._zone_tpf.SetTransform(self._zone_transform)
        self._zone_actor = self._make_zone_actor()

        # 参考坐标系（十字光标）：3D 长十字 + 小原点，三向切片同步显示并可拖动
        self._crosshair_ijk = None
        self._crosshair_visible = True
        self._crosshair_actors = []
        # 十字联动合帧：拖动/快速滚层时鼠标事件远快于渲染帧率，
        # 每次事件都跑完整联动（3D 体渲染 + 三切片换层）会让界面卡顿。
        # 位置立即更新（读取方拿到的是最新值），昂贵的跨视图同步按帧合批。
        self._crosshair_update_pending = False
        self._crosshair_flush_timer = QtCore.QTimer(self)
        self._crosshair_flush_timer.setSingleShot(True)
        self._crosshair_flush_timer.setInterval(16)
        self._crosshair_flush_timer.timeout.connect(self._flush_crosshair_update)

        self.slice_panel = OrthogonalSlicesPanel(self)
        self.slice_panel.setFixedWidth(300)
        # 未导入 CT 时也保持三个切片框可见，作为界面布局骨架
        self.slice_panel.setVisible(True)
        self.slice_panel.expandRequested.connect(self._show_expanded_slice)
        self.slice_panel.crosshairMoved.connect(self._on_slice_crosshair_moved)
        self.slice_panel.viewActivated.connect(self._set_active_view)
        for view in self.slice_panel.views:
            view.sliceNavigated.connect(
                lambda view=view: self._on_slice_navigated(view))
        self._main_layout.addWidget(self.slice_panel)

        # Cached slice overlays hide the three independent VTK repaints when
        # returning from 3D fullscreen. All overlays are released together, so
        # the user sees one continuous transition instead of three visible
        # rendering steps.
        self._slice_transition_images = [QtGui.QImage() for _ in self.slice_panel.views]
        self._slice_transition_overlays = []
        self._slice_transition_effects = []
        self._slice_transition_fade = QtCore.QParallelAnimationGroup(self)
        for view in self.slice_panel.views:
            overlay = QtWidgets.QLabel(view)
            overlay.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            overlay.setStyleSheet("background: #000000;")
            overlay.setAttribute(
                QtCore.Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            effect = QtWidgets.QGraphicsOpacityEffect(overlay)
            overlay.setGraphicsEffect(effect)
            overlay.hide()
            animation = QtCore.QPropertyAnimation(effect, b"opacity", self)
            animation.setDuration(140)
            animation.setStartValue(1.0)
            animation.setEndValue(0.0)
            animation.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
            self._slice_transition_fade.addAnimation(animation)
            self._slice_transition_overlays.append(overlay)
            self._slice_transition_effects.append(effect)
        self._slice_transition_fade.finished.connect(
            self._complete_slice_transition_overlays)
        self._slice_overlay_release_timer = QtCore.QTimer(self)
        self._slice_overlay_release_timer.setSingleShot(True)
        self._slice_overlay_release_timer.setInterval(25)
        self._slice_overlay_release_timer.timeout.connect(
            self._release_collapse_transition)

        self.expanded_slice = ExpandedSliceOverlay(self)
        self.expanded_slice.setVisible(False)
        self.expanded_slice.collapsed.connect(self._on_expanded_slice_collapsed)
        self.expanded_slice.crosshairMoved.connect(self._on_slice_crosshair_moved)
        self.expanded_slice.slice_view.sliceNavigated.connect(
            lambda: self._on_slice_navigated(self.expanded_slice.slice_view))
        self._main_layout.addWidget(self.expanded_slice, 1)

    def initialize(self):
        """初始化全部 VTK 交互器（必须在窗口首次显示后一次性调用）。"""
        if getattr(self, "_initialized_all", False):
            return
        self._initialized_all = True
        self.interactor.Initialize()
        if self.slice_panel.isVisible():
            self.slice_panel.initialize()

    def eventFilter(self, obj, event):
        if obj is getattr(self, "_view3d_slot", None):
            if (event.type() in (
                    QtCore.QEvent.Type.Move,
                    QtCore.QEvent.Type.Resize,
                    QtCore.QEvent.Type.Show,
                ) and not self._view3d_fullscreen):
                self._schedule_view3d_geometry()
            return super().eventFilter(obj, event)
        if obj is self.vtk_widget:
            et = event.type()
            if self._view3d_lazy_pending and (
                    et == QtCore.QEvent.Type.Wheel
                    or (et == QtCore.QEvent.Type.MouseMove
                        and bool(event.buttons()))):
                self._activate_lazy_view3d()
            if et == QtCore.QEvent.Type.MouseButtonPress:
                # 未加载体数据时不出现金色选中框
                if self.image is not None:
                    self._set_active_view("3d")
                if event.button() == QtCore.Qt.MouseButton.RightButton:
                    if self.image is None:
                        return True
                    self._show_3d_context_menu(event.globalPosition().toPoint())
                    return True
            if et == QtCore.QEvent.Type.MouseButtonDblClick:
                # 双击 3D 视图 → 全屏 / 还原（与切片双击放大一致；
                # 未加载体数据或过渡动画中由 _toggle 内部忽略）
                self._toggle_view3d_fullscreen()
                return True
        return super().eventFilter(obj, event)

    def _repolish(self, widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _set_active_view(self, target):
        """统一的选中高亮：整个界面同一时刻只有一个金色外框。

        target 为字符串 "3d"（中间 3D 视图）或某个 SliceView 实例。
        未加载 CT 时不显示任何金色框。
        """
        if self.image is None:
            self._clear_active_view_highlight()
            return
        self.view3d_frame.setProperty("active", target == "3d")
        self._repolish(self.view3d_frame)
        for view in self.slice_panel.views:
            view.set_active(view is target)

    def _clear_active_view_highlight(self):
        """清除 3D / 切片的金色选中框。"""
        self.view3d_frame.setProperty("active", False)
        self._repolish(self.view3d_frame)
        for view in self.slice_panel.views:
            view.set_active(False)

    def _toggle_view3d_fullscreen(self):
        """双击 3D 视图在"全屏(隐藏右侧切片栏)"与正常布局间切换。"""
        if self.image is None or self._view3d_fullscreen_transition:
            return
        # 切片放大浮层展开期间 3D frame 是隐藏的：真实鼠标事件到不了它，
        # 但程序化触发（如定位取景）仍可能发生；此时进入全屏会得到一个
        # 被浮层盖住的全屏布局，收回顺序不可预期，直接拒绝。
        if getattr(self, "expanded_slice", None) is not None \
                and self.expanded_slice.isVisible():
            return
        self._view3d_present_timer.stop()
        self._view3d_refine_timer.stop()
        self._view3d_silent_refine_timer.stop()
        self._slice_resume_timer.stop()
        self._slice_overlay_release_timer.stop()
        self._slice_transition_fade.stop()
        self._complete_slice_transition_overlays()
        self._pending_slice_resumes = []
        reuse_lazy_frame = (
            self._view3d_lazy_pending
            and not self._view3d_transition_image.isNull())
        self._view3d_lazy_pending = False
        self._view3d_lazy_restore_timer.stop()
        self._finish_view3d_resize_render(render=False)
        if not self._view3d_fullscreen:
            # 放大方向：先渲染后显示。先用旧视图盖住 GL（旧矩形、无黑边），
            # 再离屏渲染出全屏尺寸的最终帧——期间画面保持旧视图静止；
            # 成功后几何切换全程被最终帧覆盖，不存在拉伸/黑边/内容迟到。
            self._show_view3d_transition_overlay(grab=True)
            final_image = self._prerender_view3d_frame(
                self.rect().width(), self.rect().height())
            if final_image is not None:
                self._enter_view3d_fullscreen_prerendered(final_image)
                return
        else:
            # 收回方向：同样先渲染后显示。离屏渲染小视口最终帧后，覆盖层
            # 在隐藏状态下换好目标尺寸与内容再"全新显示"——原生窗口不
            # 经历 resize，系统不会缩放旧内容，收回瞬间没有弹跳。
            self._show_view3d_transition_overlay(grab=True)
            normal = self._normal_view3d_geometry()
            final_image = self._prerender_view3d_frame(
                normal.width(), normal.height())
            if final_image is not None:
                self._exit_view3d_fullscreen_prerendered(final_image)
                return
        # 预渲染失败（离屏上下文不可用等）：退回"渲染期间系统拉伸占位"
        # 的原路径（覆盖层已显示旧视图，下面 grab 无害）。
        self._show_view3d_transition_overlay(grab=not reuse_lazy_frame)
        # 只有收回才进入粗采样模式：首个小视口帧用 ISD=6 换取更快的呈现，
        # present 后立即还原画质。放大方向不再降采样——旧的"粗帧 + 280ms
        # 后精修"画质阶梯会让画面先糊一下再变清晰，正好被看成"放大后模
        # 糊了一下"；渲染等待期间过渡占位图本来就盖着，粗帧没有收益，
        # 首个全屏帧直接按正常画质渲染。（此刻 _view3d_fullscreen 尚未翻
        # 转，True 即"即将收回"。）
        if self._view3d_fullscreen:
            self._begin_view3d_resize_render()
        # 右侧切片栏始终保持可见、保持原尺寸，只由 3D frame 临时覆盖。
        # 因此三个 QVTK 切片窗口完全不 resize，也不会在收回时各自触发
        # fit_timer + paintEvent 的同步渲染风暴。
        self._view3d_fullscreen_transition = True
        # QVTK 的 paintEvent 无条件调用 interactor.Render()，Qt 的
        # setUpdatesEnabled(False) 仍挡不住 native resize 后的 paint；VTK
        # 自己的 render gate 才能保证 resize 期间真正不进入体渲染。
        self.interactor.EnableRenderOff()
        self.vtk_widget.setUpdatesEnabled(False)
        # 不能禁用后再启用整个 self：Qt 会顺带把显式冻结的 QVTK 子控件
        # 重新启用，resize paint 就会抢在 25ms present timer 前同步 Render。
        self._view3d_fullscreen = not self._view3d_fullscreen
        self.view3d_frame.setProperty("fullscreen", self._view3d_fullscreen)
        self._repolish(self.view3d_frame)
        # 收回：先把覆盖层缩到目标矩形再改 frame 几何，GL 旧帧被系统拉伸
        # 的瞬间不外露。放大：覆盖层保持在旧矩形（中心 1:1 截图盖住解剖，
        # 不变形），改几何后露出的两侧由系统对旧帧的拉伸即时填充——两侧
        # 零黑屏、零"迟到"，等真实帧渲染完成后由
        # _reveal_view3d_transition_over_frames 平滑校正两侧比例。
        if not self._view3d_fullscreen:
            self._view3d_transition_overlay.setGeometry(
                self._normal_view3d_geometry())
        self._apply_view3d_fullscreen_geometry()
        if not self._view3d_fullscreen:
            self._sync_view3d_transition_overlay()
        self.update()
        self._view3d_present_timer.start()

    def _normal_view3d_geometry(self):
        """真正 3D frame 的普通位置由布局中的轻量占位槽唯一决定。"""
        rect = self._view3d_slot.geometry()
        return QtCore.QRect(rect.x(), rect.y(), max(1, rect.width()),
                            max(1, rect.height()))

    def _schedule_view3d_geometry(self):
        """把多个来源的几何同步请求合并到同一事件循环周期执行一次。"""
        if self._view3d_geometry_pending:
            return
        self._view3d_geometry_pending = True
        QtCore.QTimer.singleShot(0, self._run_scheduled_view3d_geometry)

    def _run_scheduled_view3d_geometry(self):
        self._view3d_geometry_pending = False
        self._apply_view3d_fullscreen_geometry()

    def _apply_view3d_fullscreen_geometry(self):
        if self._view3d_fullscreen:
            self.view3d_frame.setGeometry(self.rect())
            self.view3d_frame.raise_()
        else:
            self.view3d_frame.setGeometry(self._normal_view3d_geometry())
            # 槽位在 frame 之后创建，且因 GL 子窗口被 Qt 连带原生化，
            # 天生叠在 frame 之上；不抬升 frame 的话 3D 画面会被槽的
            # 原生表面完全盖住（黑屏且收不到鼠标事件）。
            self.view3d_frame.raise_()
            self.slice_panel.raise_()
        self._sync_view3d_transition_overlay()

    def _show_view3d_transition_overlay(self, grab=True):
        """Show the last completed 3D frame without triggering a new render."""
        self._view3d_reveal_generation = (
            getattr(self, "_view3d_reveal_generation", 0) + 1)
        self._view3d_prerendered_reveal = False
        self._view3d_transition_fade.stop()
        self._view3d_transition_opacity.setOpacity(1.0)
        if grab:
            # Do not use vtkWindowToImageFilter here. Reading a fullscreen
            # OpenGL buffer forces a GPU/CPU synchronization and was itself a
            # major source of the collapse pause. QScreen copies the presented
            # window image through the compositor without asking VTK to render.
            image = self._grab_presented_widget_image(self.vtk_widget)
            if image.isNull():
                self._view3d_transition_image = QtGui.QImage()
                self._view3d_transition_overlay.clear()
            else:
                self._view3d_transition_image = image
        elif self._view3d_transition_image.isNull():
            self._view3d_transition_overlay.clear()
        self._sync_view3d_transition_overlay()
        self._view3d_transition_overlay.show()
        self._view3d_transition_overlay.raise_()

    @staticmethod
    def _grab_presented_widget_image(widget):
        image = QtGui.QImage()
        screen = widget.screen() or QtGui.QGuiApplication.primaryScreen()
        if screen is not None and widget.isVisible():
            try:
                captured = screen.grabWindow(int(widget.winId()))
                if not captured.isNull():
                    image = captured.toImage()
            except Exception:
                pass
        return image

    def _cache_slice_transition_images(self):
        self._slice_transition_images = [
            self._grab_presented_widget_image(view.vtk_widget)
            for view in self.slice_panel.views
        ]

    def _show_slice_transition_overlays(self):
        self._slice_transition_fade.stop()
        for index, overlay in enumerate(self._slice_transition_overlays):
            self._slice_transition_effects[index].setOpacity(1.0)
            self._sync_slice_transition_overlay(index)
            overlay.show()
            overlay.raise_()

    def _sync_slice_transition_overlay(self, index):
        view = self.slice_panel.views[index]
        overlay = self._slice_transition_overlays[index]
        overlay.setGeometry(view.vtk_widget.geometry())
        image = self._slice_transition_images[index]
        if image.isNull():
            overlay.clear()
            return
        dpr = max(1.0, float(self.devicePixelRatioF()))
        target_height = max(1, int(round(overlay.height() * dpr)))
        pixmap = QtGui.QPixmap.fromImage(image).scaledToHeight(
            target_height,
            QtCore.Qt.TransformationMode.FastTransformation,
        )
        pixmap.setDevicePixelRatio(dpr)
        overlay.setPixmap(pixmap)

    def _fade_slice_transition_overlays(self):
        if not any(not overlay.isHidden()
                   for overlay in self._slice_transition_overlays):
            return
        self._slice_transition_fade.stop()
        self._slice_transition_fade.start()

    def _complete_slice_transition_overlays(self):
        for index, overlay in enumerate(self._slice_transition_overlays):
            overlay.hide()
            overlay.clear()
            self._slice_transition_effects[index].setOpacity(1.0)

    def _release_collapse_transition(self):
        # The 3D overlay fade is already started in
        # _present_view3d_after_resize() right after the settled frame, so the
        # main view transitions in parallel with slice repaints. Here we only
        # release the slice overlays once every hidden VTK view has repainted.
        self._fade_slice_transition_overlays()

    def _sync_view3d_transition_overlay(self):
        if (getattr(self, "_view3d_fullscreen_transition", False)
                and self._view3d_fullscreen):
            # 放大等待期：覆盖层保持在旧位置只盖中心，两侧露出的系统拉伸
            # 旧帧就是占位（零黑屏）；几何同步会把覆盖层铺满、制造黑边，
            # present 之后由 _reveal_view3d_transition_over_frames 接管。
            return
        # 覆盖层是 viewer 的子控件（见创建处说明），frame.geometry() 直接
        # 就是它在本控件坐标系里的目标矩形。
        target_rect = self.view3d_frame.geometry()
        if self._view3d_transition_overlay.geometry() != target_rect:
            self._view3d_transition_overlay.setGeometry(target_rect)
        if not self._view3d_transition_image.isNull():
            # 占位策略（多轮实测迭代后的结论，勿再尝试"合成两侧内容"）：
            #   旧截图按高度 1:1 居中——中心与最终帧逐像素一致（不变形），
            #   两侧留纯黑（标签背景色与 3D 背景同为纯黑）。真实帧就绪后
            #   由 _present_view3d_after_resize 发起 140ms 交叉淡出：中心
            #   两帧内容相同、淡出不可见，只表现为两侧从黑渐显出真实解剖。
            #   拉伸会变形、单列平铺会出竖纹（拖影）、镜像模糊会复制出
            #   第二份结构（重影），都不如"黑 + 淡出"干净。
            # 收回方向：旧图比目标宽 → 同样按高度等比缩放，QLabel 居中
            # 裁掉两侧（模型不缩小，真实帧出现时不会回弹）。
            dpr = max(1.0, float(self.devicePixelRatioF()))
            overlay = self._view3d_transition_overlay
            target_h = max(1, int(round(overlay.height() * dpr)))
            image = self._view3d_transition_image
            # setPixmap 会触发 updateGeometry → LayoutRequest，而 event()
            # 对 LayoutRequest 又会调度几何同步；不做键值缓存的话这里会
            # 形成"每帧重复缩放整幅过渡图"的反馈循环。
            key = (image.cacheKey(), target_h)
            if key != self._view3d_overlay_pixmap_key:
                self._view3d_overlay_pixmap_key = key
                pixmap = QtGui.QPixmap.fromImage(image).scaledToHeight(
                    target_h,
                    QtCore.Qt.TransformationMode.SmoothTransformation)
                pixmap.setDevicePixelRatio(dpr)
                overlay.setPixmap(pixmap)
        if self._view3d_transition_overlay.isVisible():
            self._view3d_transition_overlay.raise_()

    def _fade_view3d_transition_overlay(self):
        if self._view3d_transition_overlay.isHidden():
            return
        self._view3d_transition_fade.stop()
        self._view3d_transition_fade.setStartValue(1.0)
        self._view3d_transition_fade.setEndValue(0.0)
        self._view3d_transition_fade.start()

    def _reveal_view3d_transition_over_frames(self):
        """逐帧把覆盖层内容混合为真实画面后隐藏（真实像素"淡出"）。

        覆盖层为盖住原生 GL 子窗口被 Qt 连带原生化，QGraphicsOpacityEffect
        对原生窗口不生效——透明度动画只会空转。这里改用真像素：present 已
        把最终画面渲染进 GL 帧缓冲，用 vtkWindowToImageFilter 直接从帧缓冲
        读回（与屏幕遮挡无关）；基准帧则抓取"当前屏幕实际所见"（放大等待
        期 = 中心 1:1 截图 + 两侧系统拉伸的旧帧，无黑屏）。覆盖层铺满后从
        当前所见逐帧混合到真实画面——每一帧都与上一帧连续，隐藏瞬间上下
        内容一致。读回的 GPU 同步开销发生在两侧已有拉伸占位的等待期内，
        用户看到的是静态画面被平滑校正，而不是内容迟到。
        """
        overlay = self._view3d_transition_overlay
        if overlay.isHidden():
            self._complete_view3d_transition_overlay()
            return
        try:
            reader = vtk.vtkWindowToImageFilter()
            reader.SetInput(self.render_window)
            reader.SetScale(1)
            reader.Update()
            data = reader.GetOutput()
            from vtk.util import numpy_support
            fx, fy, _fz = data.GetDimensions()
            arr = numpy_support.vtk_to_numpy(
                data.GetPointData().GetScalars()).reshape(fy, fx, 3)
            final_image = QtGui.QImage(
                np.ascontiguousarray(arr[::-1]).data, fx, fy, fx * 3,
                QtGui.QImage.Format_RGB888).copy()
        except Exception:
            log.exception("读回过渡终帧失败，直接整帧替换")
            self._complete_view3d_transition_overlay()
            return
        if final_image.isNull():
            self._complete_view3d_transition_overlay()
            return

        dpr = max(1.0, float(self.devicePixelRatioF()))
        frame = self.view3d_frame
        target_w = max(1, int(round(frame.width() * dpr)))
        target_h = max(1, int(round(frame.height() * dpr)))
        final = QtGui.QPixmap.fromImage(final_image).scaled(
            target_w, target_h,
            QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation)
        # 基准帧 = 当前屏幕实际呈现（含覆盖层与两侧拉伸旧帧）；抓不到时
        # 退回"旧截图 + 黑边"的字母框基准（两侧会有一段黑，但仍平滑）。
        base_image = self._grab_presented_widget_image(self.vtk_widget)
        if not base_image.isNull():
            base = QtGui.QPixmap.fromImage(base_image).scaled(
                target_w, target_h,
                QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation)
        else:
            base = QtGui.QPixmap(target_w, target_h)
            base.fill(QtCore.Qt.GlobalColor.black)
            current = overlay.pixmap()
            if not current.isNull():
                current = QtGui.QPixmap(current)
                current.setDevicePixelRatio(1.0)
                painter = QtGui.QPainter(base)
                painter.drawPixmap(
                    (target_w - current.width()) // 2, 0, current)
                painter.end()

        generation = getattr(self, "_view3d_reveal_generation", 0)
        # 覆盖层铺满目标矩形并立即换上第一帧混合结果（同一更新周期生效，
        # 不出现黑边中间帧）。
        overlay.setGeometry(frame.geometry())
        self._blend_view3d_overlay_steps(
            base, final, target_w, target_h, dpr, generation, hide_when_done=True)

    def _blend_view3d_overlay_steps(self, base, final, target_w, target_h, dpr,
                                    generation, hide_when_done):
        """驱动覆盖层内容分步混合到最终画面（真实像素"淡出"的步进部分）。

        hide_when_done=False 时（预渲染路径）混合完保持显示最终帧，由
        present 在在屏渲染完成后隐藏——覆盖层与屏上内容一致，隐藏无跳变。
        """
        overlay = self._view3d_transition_overlay

        def apply_step(index):
            if generation != getattr(self, "_view3d_reveal_generation", 0) \
                    or overlay.isHidden():
                return
            blended = QtGui.QPixmap(target_w, target_h)
            painter = QtGui.QPainter(blended)
            painter.drawPixmap(0, 0, base)
            painter.setOpacity((index + 1) / float(self._REVEAL_STEPS))
            painter.drawPixmap(0, 0, final)
            painter.end()
            blended.setDevicePixelRatio(dpr)
            overlay.setPixmap(blended)
            if index + 1 < self._REVEAL_STEPS:
                QtCore.QTimer.singleShot(
                    self._REVEAL_STEP_MS, lambda: apply_step(index + 1))
            elif hide_when_done:
                self._complete_view3d_transition_overlay()

        apply_step(0)

    _REVEAL_STEPS = 6
    _REVEAL_STEP_MS = 18

    def _prerender_view3d_frame(self, width, height):
        """离屏渲染目标尺寸的最终帧并读回（失败返回 None）。

        在几何切换之前执行，画面仍由覆盖层显示旧视图，用户只感知到短暂
        静止。读回走 GL 帧缓冲，与屏幕遮挡无关。
        """
        if self.image is None or width < 2 or height < 2:
            return None
        window = self.render_window
        old_size = (int(window.GetSize()[0]), int(window.GetSize()[1]))
        try:
            window.SetOffScreenRendering(True)
            window.SetSize(int(width), int(height))
            window.Render()
            reader = vtk.vtkWindowToImageFilter()
            reader.SetInput(window)
            reader.SetScale(1)
            reader.Update()
            data = reader.GetOutput()
            from vtk.util import numpy_support
            fx, fy, _fz = data.GetDimensions()
            arr = numpy_support.vtk_to_numpy(
                data.GetPointData().GetScalars()).reshape(fy, fx, 3)
            return QtGui.QImage(
                np.ascontiguousarray(arr[::-1]).data, fx, fy, fx * 3,
                QtGui.QImage.Format_RGB888).copy()
        except Exception:
            log.exception("离屏预渲染最终帧失败")
            return None
        finally:
            try:
                window.SetSize(*old_size)
                window.SetOffScreenRendering(False)
            except Exception:
                pass

    def _enter_view3d_fullscreen_prerendered(self, final_image):
        """用预渲染的最终帧完成放大切换：全程无拉伸、无黑边（除渐显）。"""
        overlay = self._view3d_transition_overlay
        frame = self.view3d_frame
        self._view3d_fullscreen_transition = True
        self.interactor.EnableRenderOff()
        self.vtk_widget.setUpdatesEnabled(False)
        self._view3d_fullscreen = True
        self.view3d_frame.setProperty("fullscreen", True)
        self._repolish(frame)
        # 覆盖层先于帧几何铺满目标矩形并换上混合第一帧：系统对 GL 旧帧的
        # 拉伸从未暴露。
        target_rect = self.rect()
        overlay.setGeometry(target_rect)
        self._apply_view3d_fullscreen_geometry()
        dpr = max(1.0, float(self.devicePixelRatioF()))
        target_w = max(1, int(round(target_rect.width() * dpr)))
        target_h = max(1, int(round(target_rect.height() * dpr)))
        final = QtGui.QPixmap.fromImage(final_image).scaled(
            target_w, target_h,
            QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation)
        base = QtGui.QPixmap(target_w, target_h)
        base.fill(QtCore.Qt.GlobalColor.black)
        current = overlay.pixmap()
        if not current.isNull():
            current = QtGui.QPixmap(current)
            current.setDevicePixelRatio(1.0)
            painter = QtGui.QPainter(base)
            painter.drawPixmap((target_w - current.width()) // 2, 0, current)
            painter.end()
        generation = getattr(self, "_view3d_reveal_generation", 0)
        self._view3d_prerendered_reveal = True
        self._blend_view3d_overlay_steps(
            base, final, target_w, target_h, dpr, generation,
            hide_when_done=False)
        self.update()
        self._view3d_present_timer.start()

    def _exit_view3d_fullscreen_prerendered(self, final_image):
        """用预渲染的小视口最终帧完成收回：覆盖层全新显示，零缩放帧。

        覆盖层是原生窗口——直接 resize 的话，系统在它重绘前会把旧内容
        缩放一帧（收回时的"弹一下"）。因此在隐藏状态下换好目标矩形与
        最终内容，再全新显示：原生窗口首次出现不经过 resize。随后帧几何
        收回、present 在屏渲染完成后隐藏覆盖层（内容一致，零跳变）。
        """
        overlay = self._view3d_transition_overlay
        target_rect = self._normal_view3d_geometry()
        dpr = max(1.0, float(self.devicePixelRatioF()))
        target_w = max(1, int(round(target_rect.width() * dpr)))
        target_h = max(1, int(round(target_rect.height() * dpr)))
        final = QtGui.QPixmap.fromImage(final_image).scaled(
            target_w, target_h,
            QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation)
        final.setDevicePixelRatio(dpr)

        overlay.hide()
        self._view3d_transition_image = final_image
        self._view3d_overlay_pixmap_key = None
        overlay.setGeometry(target_rect)
        overlay.setPixmap(final)

        self._view3d_fullscreen_transition = True
        self.interactor.EnableRenderOff()
        self.vtk_widget.setUpdatesEnabled(False)
        self._view3d_fullscreen = False
        self.view3d_frame.setProperty("fullscreen", False)
        self._repolish(self.view3d_frame)
        self._apply_view3d_fullscreen_geometry()   # frame 收回 + 抬升 frame/切片栏
        overlay.show()
        overlay.raise_()
        self._view3d_prerendered_reveal = True
        self.update()
        self._view3d_present_timer.start()

    def _complete_view3d_transition_overlay(self):
        self._view3d_transition_overlay.hide()
        self._view3d_transition_overlay.clear()
        self._view3d_transition_image = QtGui.QImage()
        self._view3d_overlay_pixmap_key = None
        self._view3d_transition_opacity.setOpacity(1.0)

    def _present_view3d_after_resize(self):
        """Render the settled 3D frame while the cached transition stays visible."""
        quality = self._view3d_resize_quality or {}
        if (self._view3d_fullscreen_transition
                and quality.get("collapsing", False)):
            # 双击收回：视口已回到小尺寸，ISD=6 的粗采样帧在小视口上的开销
            # 与一次普通交互帧相同（大 CT 上约 150~200ms 的一次性等待）。
            # 立即渲染换上真画面——旧方案完全不渲染、进入“静态截图 + 等
            # 用户拖动才唤醒”的懒状态，而悬停/单击都不会触发唤醒，收回后
            # 的 3D 视图看起来就像界面卡死。
            self._view3d_fullscreen_transition = False
            self.vtk_widget.setUpdatesEnabled(True)
            self.interactor.EnableRenderOn()
            try:
                if self.image is not None and self.isVisible():
                    self.render_window.Render()
            finally:
                # 还原映射器质量但不追加渲染：小视口上这一帧已足够清晰，
                # 下次自然交互会按正常画质渲染。
                self._finish_view3d_resize_render(render=False)
                # 过渡图是全屏截图缩到小视口的版本，与新帧同源但清晰度不
                # 同；渲染完成后直接整帧替换，不做半透明交叉淡化。
                self._complete_view3d_transition_overlay()
            return
        self.vtk_widget.setUpdatesEnabled(True)
        self.interactor.EnableRenderOn()
        try:
            if self.image is not None and self.isVisible():
                self.render_window.Render()
        finally:
            # Re-enable the panel shell after the settled 3D frame, then restore
            # its three GL widgets one per event-loop turn. This prevents four
            # VTK renders from landing in the same collapse frame.
            if self._view3d_fullscreen_transition:
                # 纯 3D 覆盖式切换：切片从未隐藏/缩放，不需要恢复或重绘。
                self._view3d_fullscreen_transition = False
                if getattr(self, "_view3d_prerendered_reveal", False):
                    # 预渲染路径：覆盖层早已显示最终帧（离屏预渲染产物），
                    # 在屏渲染完成后直接隐藏——上下内容一致，零跳变。
                    self._view3d_prerendered_reveal = False
                    self._complete_view3d_transition_overlay()
                else:
                    # 常规路径：从帧缓冲读回最终帧，从"当前所见"逐帧混合
                    # （原生窗口不支持透明特效，只能用真实像素）。
                    self._reveal_view3d_transition_over_frames()
            else:
                # 此分支只服务“放大切片→收回”的旧布局切换路径。
                self.slice_panel.setUpdatesEnabled(True)
                if self._view3d_fullscreen:
                    for view in self.slice_panel.views:
                        view.vtk_widget.setUpdatesEnabled(True)
                    self._fade_view3d_transition_overlay()
                else:
                    self._fade_view3d_transition_overlay()
                    self._pending_slice_resumes = list(self.slice_panel.views)
                    for view in self._pending_slice_resumes:
                        if hasattr(view, "_fit_timer"):
                            view._fit_timer.stop()
                    self._resume_next_slice_after_fullscreen()
        if self._view3d_resize_quality is not None:
            self._view3d_refine_timer.start()

    def _activate_lazy_view3d(self):
        """用户或程序真正需要新帧时，再唤醒收回后的 QVTK 画布。"""
        if not self._view3d_lazy_pending:
            return
        self._view3d_lazy_pending = False
        self.vtk_widget.setUpdatesEnabled(True)
        self.interactor.EnableRenderOn()
        self._begin_view3d_resize_render()
        self._view3d_lazy_restore_timer.start()
        self._fade_view3d_transition_overlay()

    def _resume_next_slice_after_fullscreen(self):
        if not self._pending_slice_resumes:
            return
        view = self._pending_slice_resumes.pop(0)
        widget = view.vtk_widget
        # Fit the camera without rendering, then let the single paint below
        # produce the only render for this slice. Previously _fit_timer (24ms)
        # fired a separate Render() right beside this resume paint (25ms),
        # doubling every slice render during the collapse transition.
        if hasattr(view, "_fit_timer"):
            view._fit_timer.stop()
        if hasattr(view, "_fit_camera"):
            view._fit_camera(render=False)
        widget.setUpdatesEnabled(True)
        widget.update()
        if self._pending_slice_resumes:
            self._slice_resume_timer.start()
        else:
            self._slice_overlay_release_timer.start()

    def _begin_view3d_resize_render(self):
        """Temporarily favour responsiveness while the 3D viewport resizes."""
        if self.mapper is None:
            return

        # Save normal-quality settings only once. If another toggle happens
        # before refinement, keep fast mode active and just restart the timer.
        if self._view3d_resize_quality is None:
            if isinstance(self.mapper, vtk.vtkGPUVolumeRayCastMapper):
                self._view3d_resize_quality = {
                    "mapper_type": "gpu",
                    "auto_adjust": self.mapper.GetAutoAdjustSampleDistances(),
                    "image_sample_distance":
                        self.mapper.GetImageSampleDistance(),
                    "render_rate": self.render_window.GetDesiredUpdateRate(),
                }
            else:
                self._view3d_resize_quality = {
                    "mapper_type": "smart",
                    "low_res_mode": self.mapper.GetLowResMode(),
                    "interactive_rate":
                        self.mapper.GetInteractiveUpdateRate(),
                    "render_rate": self.render_window.GetDesiredUpdateRate(),
                }

        self._view3d_refine_timer.stop()
        self._view3d_silent_refine_timer.stop()
        self._view3d_lazy_restore_timer.stop()
        # _view3d_fullscreen has not been toggled yet here, so True means the
        # viewport is about to shrink (collapse). On collapse a coarser present
        # frame stays sharp on the smaller viewport and the silent refine
        # restores full quality later — roughly halving the present render time.
        collapsing = self._view3d_fullscreen
        # Record the direction so _finish_view3d_resize_render (which runs after
        # the toggle, when _view3d_fullscreen has flipped) can tell collapse
        # apart from the slice-overlay collapse path that shares this method.
        self._view3d_resize_quality["collapsing"] = collapsing
        if self._view3d_resize_quality["mapper_type"] == "gpu":
            self.mapper.AutoAdjustSampleDistancesOff()
            self.mapper.SetImageSampleDistance(6.0 if collapsing else 3.5)
        else:
            self.mapper.SetLowResMode(self.mapper.LowResModeResample)
            self.mapper.SetInteractiveUpdateRate(30.0)
        self.render_window.SetDesiredUpdateRate(30.0)

    def _finish_view3d_resize_render(self, render=True):
        """Restore mapper settings and issue one settled, sharp render."""
        quality = self._view3d_resize_quality
        if quality is None:
            return

        self._view3d_resize_quality = None
        if self.mapper is not None:
            if quality["mapper_type"] == "gpu":
                self.mapper.SetImageSampleDistance(
                    quality["image_sample_distance"])
                self.mapper.SetAutoAdjustSampleDistances(
                    quality["auto_adjust"])
            else:
                self.mapper.SetLowResMode(quality["low_res_mode"])
                self.mapper.SetInteractiveUpdateRate(
                    quality["interactive_rate"])
        self.render_window.SetDesiredUpdateRate(quality["render_rate"])

        if render and self.image is not None and self.isVisible():
            if quality.get("collapsing", False):
                # 收回后的视口更小，过渡帧已足够清晰。不要在 700ms 后再偷偷
                # 同步 Render 一次（旧方案只是把卡顿延后）；映射器质量已经
                # 恢复，下一次用户旋转/缩放产生的自然渲染会自动升级画质。
                return
            else:
                self.render()

    def _silent_refine_render(self):
        """Deferred full-quality render after a collapse transition.

        By the time this fires the user has finished watching the collapse
        motion, so the synchronous volume render no longer reads as a
        perceptible freeze during the interaction.
        """
        if self.image is not None and self.isVisible():
            self.render()

    def _schedule_initial_view_renders(self):
        """把首次 3D/三向切片绘制拆帧，加载完成后界面可立即响应。"""
        self._initial_render_timer.stop()
        self._pending_initial_renders = []
        self._view3d_lazy_pending = False
        if self.image is None or not self.isVisible():
            return
        # Staging the four views already keeps the UI responsive. Render the
        # first visible 3D frame at settled quality instead of flashing a coarse
        # frame and refining it several hundred milliseconds later.
        self._finish_view3d_resize_render(render=False)
        self._pending_initial_renders = [None] + list(self.slice_panel.views)
        self._initial_render_timer.start()

    def _render_next_initial_view(self):
        if self.image is None or not self._pending_initial_renders:
            return
        target = self._pending_initial_renders.pop(0)
        try:
            if target is None:
                self.render()
            elif target.isVisible():
                target._fit_camera(force=True, render=True)
        finally:
            if self._pending_initial_renders:
                self._initial_render_timer.start()
            else:
                self._finish_view3d_resize_render(render=False)
                # 全部首帧已同步渲染完成；画面呈现到屏幕还需一个合成周期，
                # 由主窗口侧延迟后再预抓弹层背景。
                self.initialRendersFinished.emit()

    # ============================================================
    # 体数据加载与渲染管线
    # ============================================================

    def set_volume(self, image, info):
        """设置三维体数据并重建整个渲染管线。
        
        每加载一次新数据调用一次，会：
          1. 清除旧数据（标注点、消融针、消融区）
          2. 重建 vtkSmartVolumeMapper + vtkVolume 渲染管线
          3. 设置传递函数的体素间距校准
          4. 添加方向标记
          5. 默认以全部组织+全透明显示
        
        参数：
          image — vtkImageData 三维体数据
          info  — 字典，含 modality/scalar_range/dimensions
        """
        old_image = self.image
        old_masks = [
            item.get("mask") for item in self._segmentation_masks.values()
            if item.get("mask") is not None
        ]
        # A pending refinement must not target the mapper that is about to be
        # replaced by this newly loaded volume.
        self._view3d_present_timer.stop()
        self._view3d_refine_timer.stop()
        self._view3d_silent_refine_timer.stop()
        self._view3d_lazy_restore_timer.stop()
        self._slice_resume_timer.stop()
        self._slice_overlay_release_timer.stop()
        self._slice_transition_fade.stop()
        self._pending_slice_resumes = []
        self._initial_render_timer.stop()
        self._pending_initial_renders = []
        self._view3d_transition_fade.stop()
        self.interactor.EnableRenderOn()
        self.vtk_widget.setUpdatesEnabled(True)
        self.slice_panel.setUpdatesEnabled(True)
        for view in self.slice_panel.views:
            view.vtk_widget.setUpdatesEnabled(True)
        self._complete_slice_transition_overlays()
        self._slice_transition_images = [
            QtGui.QImage() for _ in self.slice_panel.views]
        self._view3d_transition_overlay.hide()
        self._view3d_transition_overlay.clear()
        self._view3d_transition_image = QtGui.QImage()
        self._view3d_transition_opacity.setOpacity(1.0)
        self._finish_view3d_resize_render(render=False)
        # 先切断隐藏的放大切片对旧 vtkImageData 的引用；否则用户曾经放大过
        # 切片后再加载病例，旧体数据会被这个不可见 viewer 长期保留。
        self.expanded_slice.clear_volume()
        self.slice_panel.set_segmentations([], render=False)
        self.image = image
        self.info = info
        self._clear_active_view_highlight()
        self._base_spacing = image.GetSpacing()
        self._z_factor = 1.0
        self._point_annotations = []
        self._next_point_id = 1
        self._segmentation_actors = {}
        self._segmentation_masks = {}
        self._ablation_needle = None
        self._ablation_actors = []
        self._planning_points = {"entry": None, "tip": None}
        self._planning_actors = {"entry": None, "tip": None}
        self.renderer.RemoveAllViewProps()

        self.vol_property = vtk.vtkVolumeProperty()
        self.vol_property.SetInterpolationTypeToLinear()
        self.vol_property.SetAmbient(0.30)
        self.vol_property.SetDiffuse(0.70)
        self.vol_property.SetSpecular(0.30)
        self.vol_property.SetSpecularPower(12)
        # Interpret transfer-function opacities per voxel (as Slicer does), so
        # opacity doesn't over-accumulate through thick tissue and wash out.
        sx, sy, sz = image.GetSpacing()
        self.vol_property.SetScalarOpacityUnitDistance((sx + sy + sz) / 3.0)

        # Prefer the explicit GPU mapper when supported. Unlike the adaptive
        # wrapper it exposes ImageSampleDistance, allowing fullscreen resize
        # frames to reduce screen-space work immediately (before the first
        # large frame has already blocked). Keep SmartVolumeMapper as the
        # software/driver compatibility fallback.
        gpu_mapper = vtk.vtkGPUVolumeRayCastMapper()
        gpu_mapper.SetInputData(image)
        gpu_mapper.SetBlendModeToComposite()
        if gpu_mapper.IsRenderSupported(self.render_window, self.vol_property):
            # A 1.5x screen-space sample distance reduces the persistent
            # fullscreen ray count while remaining visually close to native
            # application's normal viewing distance.  It also prevents the
            # delayed refinement frame from freezing the UI again.
            gpu_mapper.AutoAdjustSampleDistancesOff()
            gpu_mapper.SetImageSampleDistance(1.5)
            self.mapper = gpu_mapper
        else:
            self.mapper = vtk.vtkSmartVolumeMapper()
            self.mapper.SetInputData(image)
            self.mapper.SetBlendModeToComposite()

        self.volume = vtk.vtkVolume()
        self.volume.SetMapper(self.mapper)
        self.volume.SetProperty(self.vol_property)
        self.renderer.AddVolume(self.volume)

        self._add_orientation_marker()
        self.renderer.AddViewProp(self._needle_angle_actor)
        self._update_needle_angle_overlay()
        self.renderer.AddViewProp(self._imu_temp_actor)
        self._refresh_imu_readout_overlay()
        self._ablation_zone = None
        self._zone_actor.SetVisibility(False)
        self.renderer.AddActor(self._zone_actor)

        # Everything visible at full opacity by default; the user adjusts layers
        # via the 3D right-click "组织" menu.
        self._init_tissue_model(info["modality"])
        self._apply_tissue_model(render=False)
        # Reference crosshair starts at the volume centre by default.
        dims = image.GetDimensions()
        self._crosshair_ijk = (
            (dims[0] - 1) * 0.5, (dims[1] - 1) * 0.5, (dims[2] - 1) * 0.5)
        self._rebuild_crosshair_actors()
        self.expanded_slice.setVisible(False)
        self._view3d_slot.setVisible(True)
        self.view3d_frame.setVisible(True)
        self.vtk_widget.setVisible(True)
        self.slice_panel.set_volume(image, info, render=False)
        self._push_segmentations_to_slices(render=False)
        self._push_crosshair_to_slices(render=False)
        self._view3d_fullscreen = False
        self.view3d_frame.setProperty("fullscreen", False)
        self._repolish(self.view3d_frame)
        self.slice_panel.setVisible(True)
        self._refresh_slice_points(render=False)
        self._layout_slice_overlays()
        # Reset only after the slice column has reached its final width.  The
        # renderer aspect ratio used by ResetCamera is then the one the user
        # actually sees, so the volume no longer becomes smaller after load.
        self._main_layout.activate()
        self._apply_view3d_fullscreen_geometry()
        for view in self.slice_panel.views:
            view._fit_timer.stop()
        self.reset_view(render=False)
        if self.isVisible():
            self.slice_panel.initialize()
        self._schedule_initial_view_renders()
        # 所有 mapper/viewer 已经接到新数据，可以主动断开旧数组。对于
        # numpy/VTK 零拷贝体数据，这一步会立刻释放旧的大块 numpy owner，
        # 避免连续切换病例时工作集按病例数累积。
        for old_data in [old_image] + old_masks:
            if old_data is not None and old_data is not image:
                try:
                    old_data.GetPointData().SetScalars(None)
                except Exception:
                    pass

    def apply_tissues(self, tissue_states, opacity_scale=None, render=True):
        """合成可见组织层并更新体渲染传递函数。
        
        此方法将用户当前勾选的组织层合并，调用 presets.build_composite()
        生成颜色和透明度传递函数，然后应用到 VTK Volume 上触发重渲染。
        
        参数：
          tissue_states — {组织名: 不透明度系数} 或组织名列表
          opacity_scale — 总量的不透明度缩放（不透明 1.0 / 透明 0.30）
        """
        if self.vol_property is None:
            return
        if opacity_scale is not None:
            self._opacity_scale = opacity_scale
        self._tissue_states = (dict(tissue_states) if isinstance(tissue_states, dict)
                               else {n: 1.0 for n in tissue_states})

        color_tf, opacity_tf, light, shade = presets.build_composite(
            self._tissue_states, self.info["modality"], self.info["scalar_range"],
            self._opacity_scale)
        self.vol_property.SetColor(color_tf)
        self.vol_property.SetScalarOpacity(opacity_tf)
        self.vol_property.SetShade(shade)
        self.vol_property.SetAmbient(light["ambient"])
        self.vol_property.SetDiffuse(light["diffuse"])
        self.vol_property.SetSpecular(light["specular"])
        self.vol_property.SetSpecularPower(light["power"])
        if render:
            self.render()

    def set_opacity_scale(self, scale):
        self._opacity_scale = scale
        self.apply_tissues(self._tissue_states, scale)

    # ---- 组织模型(3D 右键"组织"菜单) -----------------------------------
    # 旧版左侧"组织"面板迁移到这里：每种组织层有显示开关和透明度系数，
    # 合成时只把"可见"的组织按各自系数喂给传递函数。
    def _init_tissue_model(self, modality):
        names = list(presets.tissue_names(modality))
        self._tissue_order = names
        self._tissue_visible = {n: True for n in names}
        self._tissue_opacity = {n: 1.0 for n in names}

    def _apply_tissue_model(self, render=True):
        states = {
            n: self._tissue_opacity.get(n, 1.0)
            for n in self._tissue_order
            if self._tissue_visible.get(n, True)
        }
        self.apply_tissues(states, self._opacity_scale, render=render)

    def _tissue_color(self, name):
        modality = (self.info or {}).get("modality")
        return presets.tissue_color(name, modality)

    def set_tissue_visible(self, name, visible):
        if name in self._tissue_visible:
            self._tissue_visible[name] = bool(visible)
            self._apply_tissue_model()

    def set_all_tissues_visible(self, visible):
        for n in self._tissue_order:
            self._tissue_visible[n] = bool(visible)
        self._apply_tissue_model()

    def show_only_tissues(self, keep_names):
        """只显示 keep_names 中的组织层，其余全部隐藏。

        用于肺结节观察等场景（分割完成后只保留肺和骨，避免其余组织
        遮挡病灶）。3D 视图右键「组织」菜单的动态勾选状态会同步，
        用户可随时手动恢复任意层。返回是否有层实际发生了切换。
        """
        keep = set(keep_names)
        changed = False
        for name in self._tissue_order:
            target = name in keep
            if self._tissue_visible.get(name, True) != target:
                self._tissue_visible[name] = target
                changed = True
        if changed:
            self._apply_tissue_model()
        return changed

    def set_tissue_opacity(self, name, value):
        if name in self._tissue_opacity:
            self._tissue_opacity[name] = max(0.0, min(1.0, float(value)))
            if self._tissue_visible.get(name, True):
                self._apply_tissue_model()

    def set_volume_visible(self, visible):
        if self.volume is not None:
            self.volume.SetVisibility(bool(visible))
            self.render()

    def set_z_spacing_factor(self, factor):
        """拉伸/压缩体数据 Z 方向以修正层间距比例。
        
        用于图片序列（无 DICOM 元数据）中调节模型的"高矮胖瘦"：
          factor > 1.0 → 模型沿 Z 拉长
          factor < 1.0 → 模型沿 Z 压扁
        DICOM 数据本身含有真实层间距，通常只需保持 factor = 1.0。
        修改后自动更新标注点位置、消融针位置和所有切片视图。
        
        参数：
          factor — Z 方向缩放因子（0.1 ~ 10.0）
        """
        if self.image is None or self._base_spacing is None:
            return
        self._z_factor = factor
        sx, sy, sz = self._base_spacing
        sz *= factor
        self.image.SetSpacing(sx, sy, sz)
        self.image.Modified()
        if self.vol_property is not None:
            self.vol_property.SetScalarOpacityUnitDistance((sx + sy + sz) / 3.0)
        for item in self._segmentation_masks.values():
            mask = item.get("mask")
            if mask is not None:
                mask.SetSpacing(sx, sy, sz)
                mask.Modified()
        self._update_point_actor_positions()
        self._update_ablation_needle_positions()
        self._rebuild_crosshair_actors()
        self._push_crosshair_to_slices()
        self._queue_segmentation_update()
        self.reset_view()
        self.slice_panel.refresh()
        self.expanded_slice.refresh()

    def _show_3d_context_menu(self, global_pos):
        menu = QtWidgets.QMenu(self)
        style.style_rounded_menu(menu)
        reset_action = menu.addAction("重置视角")
        reset_action.triggered.connect(lambda *_: self.reset_view())
        menu.addSeparator()

        # 组织 / 分割部位：每行 = 复选框 + 透明度滑块，嵌入菜单后可连续调整，
        # 在行外(空白处)点才会关闭菜单。
        tissue_menu = _StayOpenMenu("组织", menu)
        menu.addMenu(tissue_menu)
        self._build_tissue_menu(tissue_menu)
        menu.addSeparator()

        seg_menu = _StayOpenMenu("分割部位", menu)
        menu.addMenu(seg_menu)
        self._build_segmentation_menu(seg_menu)

        # 子菜单是后加入的，再刷一遍圆角属性
        style.style_rounded_menu(menu)
        menu.exec(global_pos)

    def _build_layer_menu(self, layer_menu, entries, on_visible, on_opacity,
                          on_all, empty_text):
        """通用图层菜单：全部显示/隐藏 + 每行(复选框+透明度滑块)。

        entries — [(name, label, color, visible, opacity), ...]
        on_visible(name, bool) / on_opacity(name, 0~1) — 单行回调
        on_all(bool, rows) — 全部显示/隐藏回调(需同步刷新行内复选框)
        """
        if not entries:
            empty = layer_menu.addAction(empty_text)
            empty.setEnabled(False)
            return

        rows = []
        show_all = layer_menu.addAction("全部显示")
        hide_all = layer_menu.addAction("全部隐藏")
        for act in (show_all, hide_all):
            act.setProperty("keepOpen", True)   # 点完不关闭，可继续操作
        show_all.triggered.connect(lambda *_: on_all(True, rows))
        hide_all.triggered.connect(lambda *_: on_all(False, rows))
        layer_menu.addSeparator()

        for name, label, color, visible, opacity in entries:
            row = _LayerMenuRow(name, label, color, visible, opacity,
                                on_visible, on_opacity)
            action = QtWidgets.QWidgetAction(layer_menu)
            action.setDefaultWidget(row)
            layer_menu.addAction(action)
            rows.append(row)

    def _build_tissue_menu(self, tissue_menu):
        """组织菜单(替代旧的左侧"组织"面板)：每层显示开关 + 透明度滑块。"""
        entries = [] if (self.image is None) else [
            (name, name, self._tissue_color(name),
             self._tissue_visible.get(name, True),
             self._tissue_opacity.get(name, 1.0))
            for name in self._tissue_order
        ]
        self._build_layer_menu(
            tissue_menu, entries,
            self.set_tissue_visible, self.set_tissue_opacity,
            self._all_tissues_from_menu, "尚未加载数据")

    def _build_segmentation_menu(self, seg_menu):
        """分割部位菜单：每个器官独立的显示开关 + 透明度滑块。"""
        entries = [
            (name, segmentation.segment_display_name(name),
             item.get("color", (0.15, 0.85, 0.72)),
             self._segmentation_visible(name),
             self.segmentation_opacity(name))
            for name, item in self._segmentation_masks.items()
        ]
        self._build_layer_menu(
            seg_menu, entries,
            self.set_segmentation_visible, self.set_segmentation_opacity,
            self._all_segments_from_menu, "暂无分割结果")

    def _all_tissues_from_menu(self, visible, rows):
        self.set_all_tissues_visible(visible)
        for row in rows:
            row.set_visible_silent(visible)

    def _all_segments_from_menu(self, visible, rows):
        self.set_all_segmentations_visible(visible)
        for row in rows:
            row.set_visible_silent(visible)

    def _segmentation_color_icon(self, color):
        pixmap = QtGui.QPixmap(18, 18)
        pixmap.fill(QtCore.Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        rgb = [max(0, min(255, int(float(component) * 255))) for component in color[:3]]
        painter.setPen(QtGui.QPen(QtGui.QColor(30, 38, 46, 180), 1))
        painter.setBrush(QtGui.QColor(rgb[0], rgb[1], rgb[2]))
        painter.drawRoundedRect(QtCore.QRectF(3, 3, 12, 12), 2, 2)
        painter.end()
        return QtGui.QIcon(pixmap)

    # ---- point annotations ---------------------------------------------
    def point_annotations(self):
        return [
            {
                "id": point["id"],
                "ijk": point["ijk"],
                "world": point["world"],
                "radius": self._point_radius(),
            }
            for point in self._point_annotations
        ]

    def add_point(self, ijk):
        if self.image is None:
            return None

        ijk = self._clamp_ijk(ijk)
        world = self._ijk_to_world(ijk)
        source = vtk.vtkSphereSource()
        source.SetCenter(*world)
        source.SetRadius(self._point_radius())
        source.SetThetaResolution(28)
        source.SetPhiResolution(16)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(source.GetOutputPort())

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(1.0, 0.0, 0.0)
        actor.GetProperty().SetAmbient(0.9)
        actor.GetProperty().SetDiffuse(0.25)
        actor.GetProperty().SetSpecular(0.25)
        actor.GetProperty().SetSpecularPower(14)

        point = {
            "id": self._next_point_id,
            "ijk": ijk,
            "world": world,
            "source": source,
            "actor": actor,
        }
        self._next_point_id += 1
        self._point_annotations.append(point)
        self.renderer.AddActor(actor)
        self.renderer.ResetCameraClippingRange()
        self._refresh_slice_points()
        self.render()
        return point["id"]

    def nearest_point_id(self, ijk, orientation):
        point = self._nearest_point(ijk, orientation)
        return point["id"] if point is not None else None

    def delete_point_near(self, ijk, orientation):
        point = self._nearest_point(ijk, orientation)
        if point is None:
            return False

        self.renderer.RemoveActor(point["actor"])
        self._point_annotations = [
            item for item in self._point_annotations
            if item["id"] != point["id"]
        ]
        self._refresh_slice_points()
        self.render()
        return True

    def _nearest_point(self, ijk, orientation):
        if self.image is None or orientation not in SLICE_AXIS:
            return None

        ijk = self._clamp_ijk(ijk)
        axis = SLICE_AXIS[orientation]
        plane_axes = [i for i in range(3) if i != axis]
        click_world = self._ijk_to_world(ijk)
        threshold = self._point_radius() * 4.0
        best = None
        best_dist = None

        for point in self._point_annotations:
            if abs(point["ijk"][axis] - ijk[axis]) > 0.5:
                continue
            dist = math.sqrt(sum(
                (point["world"][i] - click_world[i]) ** 2
                for i in plane_axes
            ))
            if dist <= threshold and (best_dist is None or dist < best_dist):
                best = point
                best_dist = dist
        return best

    def _refresh_slice_points(self, render=True):
        points = self.point_annotations()
        needle = self.ablation_needle()
        markers = self.planning_markers()
        self.slice_panel.set_points(points, render=render)
        self.expanded_slice.set_points(points)
        self.slice_panel.set_needle(needle, render=render)
        self.expanded_slice.set_needle(needle)
        self.slice_panel.set_planning_markers(markers, render=render)
        self.expanded_slice.set_planning_markers(markers)

    def planning_markers(self):
        """切片上要画的入针点/消融点标记。

        有针道时取针的两端（重置针位/改参数后不会与针脱节），否则取已放置
        的规划点。颜色与 3D 一致：入针点绿色不透明、消融点红色半透明。
        """
        if self.image is None:
            return []
        if self._ablation_needle is not None:
            source = {
                "entry": self._ablation_needle["entry_ijk"],
                "tip": self._ablation_needle["tip_ijk"],
            }
        else:
            source = self._planning_points
        # 切片上的标记按图像尺度取半径（与标注点同一套），不用 3D 针的毫米半径，
        # 否则在 512² 的片子上只有一两个像素。
        base = self._point_radius()
        markers = []
        for kind, scale, color, opacity in (
                ("entry", 1.0, (0.20, 0.85, 0.30), 1.0),
                ("tip", 1.25, (1.0, 0.18, 0.05), 0.55)):
            ijk = source.get(kind)
            if ijk is None:
                continue
            markers.append({
                "kind": kind,
                "ijk": tuple(ijk),
                "world": self._ijk_to_world(ijk),
                "radius": base * scale,
                "color": color,
                "opacity": opacity,
            })
        return markers

    def _update_point_actor_positions(self):
        if self.image is None:
            return
        radius = self._point_radius()
        for point in self._point_annotations:
            world = self._ijk_to_world(point["ijk"])
            point["world"] = world
            point["source"].SetCenter(*world)
            point["source"].SetRadius(radius)
            point["source"].Modified()
        self._refresh_slice_points()

    def _point_radius(self):
        if self.image is None:
            return 4.0
        bounds = self.image.GetBounds()
        extents = [
            abs(bounds[1] - bounds[0]),
            abs(bounds[3] - bounds[2]),
            abs(bounds[5] - bounds[4]),
        ]
        spacing = [abs(value) for value in self.image.GetSpacing()]
        diagonal = math.sqrt(sum(value * value for value in extents))
        scale_radius = diagonal * 0.008 if diagonal > 0 else 4.0
        voxel_radius = max(spacing) * 1.8 if spacing else 2.0
        return max(2.0, min(8.0, max(scale_radius, voxel_radius)))

    # ============================================================
    # 参考坐标系（十字光标）
    #   - 3D：很长的三轴十字 + 小原点球，UseBounds 关闭以免撑大相机取景
    #   - 切片：每个切片画两条平面内长线，可拖动，所有视图同步
    # 修改外观：CROSSHAIR_AXIS_COLORS / CROSSHAIR_DRAG_COLOR / CROSSHAIR_ORIGIN_COLOR
    # ============================================================
    def crosshair_ijk(self):
        return self._crosshair_ijk

    def set_crosshair_ijk(self, ijk, from_slice=False):
        """更新参考坐标系位置（ijk）。

        位置立即生效；跨视图联动（3D 十字重建 + 三切片滚动 + 渲染）合批到
        每帧最多一次，由 _flush_crosshair_update 执行——见 __init__ 里的
        _crosshair_flush_timer 说明。需要立即看到画面时调 _flush_crosshair_update()。
        """
        if self.image is None or ijk is None:
            return
        self._crosshair_ijk = self._clamp_ijk(ijk)
        self._crosshair_update_pending = True
        if not self._crosshair_flush_timer.isActive():
            self._crosshair_flush_timer.start()

    def _flush_crosshair_update(self):
        """把最近一次十字位置一次性同步到 3D 与三向切片（含放大浮层）。"""
        if not self._crosshair_update_pending:
            return
        self._crosshair_update_pending = False
        self._rebuild_crosshair_actors()
        self._push_crosshair_to_slices()
        self._sync_slices_to_crosshair()
        self.render()

    def _sync_slices_to_crosshair(self):
        """把每个切片视图滚动到十字所在的层，使交点始终落在显示的切片上。

        拖动十字时，被拖动的视图法向坐标不变（其切片不动），另外两个视图
        会跟随滚动到十字的新位置——交点所在的切片永远是当前显示的那一层。
        """
        if self.image is None or self._crosshair_ijk is None:
            return
        ijk = self._crosshair_ijk
        for view in self.slice_panel.views:
            idx = int(round(ijk[SLICE_AXIS[view.orientation]]))
            if idx != view.current_slice():
                view.set_slice(idx)
        if self.expanded_slice.isVisible():
            ev = self.expanded_slice.slice_view
            idx = int(round(ijk[SLICE_AXIS[ev.orientation]]))
            if idx != ev.current_slice():
                ev.set_slice(idx)

    def _on_slice_crosshair_moved(self, ijk):
        """切片视图里拖动十字时的回调：更新位置并同步回所有视图。"""
        self.set_crosshair_ijk(ijk, from_slice=True)

    def _on_slice_navigated(self, view):
        """切片滚轮/滑块换层：沿该切片法向移动 3D 坐标轴中心。"""
        if (self.image is None or self._crosshair_ijk is None
                or view is None or view.orientation not in SLICE_AXIS):
            return
        axis = SLICE_AXIS[view.orientation]
        ijk = list(self._crosshair_ijk)
        new_val = float(view.current_slice())
        if abs(ijk[axis] - new_val) < 1e-6:
            return
        ijk[axis] = new_val
        self.set_crosshair_ijk(ijk, from_slice=True)

    def _crosshair_payload(self):
        if self.image is None or self._crosshair_ijk is None:
            return None
        return {
            "ijk": tuple(self._crosshair_ijk),
            "world": self._ijk_to_world(self._crosshair_ijk),
            "visible": self._crosshair_visible,
        }

    def _push_crosshair_to_slices(self, render=True):
        payload = self._crosshair_payload()
        self.slice_panel.set_crosshair(payload, render=render)
        if self.expanded_slice.isVisible():
            self.expanded_slice.set_crosshair(payload)

    def _crosshair_dimensions(self):
        """返回参考十字半长（世界单位）；线宽见 CROSSHAIR_3D_LINE_WIDTH（屏幕像素）。"""
        bounds = self.image.GetBounds()
        extents = [abs(bounds[1] - bounds[0]),
                   abs(bounds[3] - bounds[2]),
                   abs(bounds[5] - bounds[4])]
        diagonal = math.sqrt(sum(v * v for v in extents)) or 1.0
        half_length = diagonal * 0.75          # 总长约 1.5 倍体对角线 → 远超屏幕
        return half_length

    def _rebuild_crosshair_actors(self):
        for actor in self._crosshair_actors:
            self.renderer.RemoveActor(actor)
        self._crosshair_actors = []
        if self.image is None or self._crosshair_ijk is None or not self._crosshair_visible:
            return

        center = self._ijk_to_world(self._crosshair_ijk)
        half = self._crosshair_dimensions()
        labels = ("X", "Y", "Z")
        for axis in range(3):
            d = [0.0, 0.0, 0.0]
            d[axis] = half
            p0 = tuple(center[i] - d[i] for i in range(3))
            p1 = tuple(center[i] + d[i] for i in range(3))
            self._crosshair_actors.append(
                self._make_crosshair_line_actor(
                    p0, p1, CROSSHAIR_AXIS_COLORS[axis]))
            # X/Y/Z 标签放在正方向末端外侧一点
            tip = tuple(center[i] + d[i] * 1.03 for i in range(3))
            self._crosshair_actors.append(
                self._make_crosshair_label_actor(
                    labels[axis], tip, CROSSHAIR_AXIS_COLORS[axis]))
        for actor in self._crosshair_actors:
            self.renderer.AddActor(actor)
        # The reference cross extends well past the volume; widen the clipping
        # range so its far arms aren't depth-clipped in the default view.
        self._widen_clipping_for_crosshair()

    def _make_crosshair_line_actor(self, p0, p1, color):
        """屏幕像素线宽的管状线：放大/缩小粗细不变，不跟着体数据缩放。"""
        line = vtk.vtkLineSource()
        line.SetPoint1(*p0)
        line.SetPoint2(*p1)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(line.GetOutputPort())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetLineWidth(float(CROSSHAIR_3D_LINE_WIDTH))
        prop.SetAmbient(0.95)
        prop.SetDiffuse(0.25)
        prop.SetSpecular(0.15)
        prop.SetSpecularPower(12)
        try:
            # OpenGL 把像素线宽画成管状，边缘更干净，且仍按屏幕像素计宽
            prop.RenderLinesAsTubesOn()
        except Exception:
            pass
        actor.SetPickable(False)
        actor.SetUseBounds(False)   # keep the long arms out of ResetCamera
        return actor

    def _make_crosshair_origin_actor(self, center, radius):
        source = vtk.vtkSphereSource()
        source.SetCenter(*center)
        source.SetRadius(radius)
        source.SetThetaResolution(20)
        source.SetPhiResolution(12)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(source.GetOutputPort())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(*CROSSHAIR_ORIGIN_COLOR)
        actor.GetProperty().SetAmbient(0.95)
        actor.GetProperty().SetDiffuse(0.05)
        actor.SetPickable(False)
        actor.SetUseBounds(False)
        return actor

    def _make_crosshair_label_actor(self, text, position, color):
        """轴末端的 X/Y/Z 文字：billboard 始终面向相机、字号恒定。"""
        actor = vtk.vtkBillboardTextActor3D()
        actor.SetInput(text)
        actor.SetPosition(*position)
        tp = actor.GetTextProperty()
        tp.SetFontSize(15)
        tp.SetColor(*color)
        tp.SetJustificationToCentered()
        tp.SetVerticalJustificationToCentered()
        tp.BoldOn()
        actor.SetPickable(False)
        try:
            actor.SetUseBounds(False)   # 标签在远端，别让它撑大相机取景
        except Exception:
            pass
        return actor

    def _widen_clipping_for_crosshair(self):
        if self.image is None:
            return
        cam = self.renderer.GetActiveCamera()
        half = self._crosshair_dimensions()
        near, far = cam.GetClippingRange()
        # Push the far plane out and pull the near plane in (clamped > 0) by the
        # crosshair half-length so the whole cross stays inside the frustum.
        cam.SetClippingRange(max(near * 0.1, 0.01), far + half * 2.5)

    def _ijk_to_world(self, ijk):
        origin = self.image.GetOrigin()
        spacing = self.image.GetSpacing()
        return tuple(origin[i] + float(ijk[i]) * spacing[i] for i in range(3))

    def _clamp_ijk(self, ijk):
        dims = self.image.GetDimensions()
        values = []
        for i in range(3):
            upper = max(0, dims[i] - 1)
            values.append(max(0.0, min(float(ijk[i]), float(upper))))
        return tuple(values)

    # ---- segmentation overlays -----------------------------------------
    def begin_segmentation_update(self):
        self._segmentation_update_depth += 1

    def end_segmentation_update(self):
        self._segmentation_update_depth = max(0, self._segmentation_update_depth - 1)
        if self._segmentation_update_depth == 0 and self._segmentation_update_dirty:
            self._segmentation_update_dirty = False
            self._push_segmentations_to_slices()
            self.renderer.ResetCameraClippingRange()
            self.render()

    def _queue_segmentation_update(self):
        if self._segmentation_update_depth > 0:
            self._segmentation_update_dirty = True
            return
        self._push_segmentations_to_slices()
        self.renderer.ResetCameraClippingRange()
        self.render()

    def clear_segmentations(self):
        for actor in self._segmentation_actors.values():
            self.renderer.RemoveActor(actor)
        self._segmentation_actors = {}
        self._segmentation_masks = {}
        self._queue_segmentation_update()

    def add_segmentation_mask(self, name, mask_image, color, opacity=0.96):
        """Add a binary segmentation mask as a solid 3D surface."""
        if self.image is None or mask_image is None:
            return False
        if mask_image.GetDimensions() != self.image.GetDimensions():
            return False

        scalar_range = mask_image.GetScalarRange()
        if scalar_range[1] <= 0.0:
            return False

        if name in self._segmentation_actors:
            self.renderer.RemoveActor(self._segmentation_actors[name])

        base_opacity = float(opacity)
        effective_opacity = base_opacity * self._segmentation_opacity
        # Voxel bounding box of the organ, computed once here. The slice views
        # use it to skip slices the organ isn't on and to crop per-slice
        # geometry extraction; the 3D surface builder uses it to crop the
        # blur/contour to the organ's footprint (big speed win).
        bounds = self._compute_mask_bounds(mask_image)
        iso = scalar_range[1] * 0.5
        actor = self._make_segmentation_actor(
            mask_image, color, effective_opacity, bounds=bounds, iso=iso)
        self.renderer.AddActor(actor)
        self._segmentation_actors[name] = actor
        self._segmentation_masks[name] = {
            "name": name,
            "mask": mask_image,
            "color": tuple(color),
            "opacity": effective_opacity,
            "base_opacity": base_opacity,
            "visible": True,
            "bounds": bounds,
        }
        self._queue_segmentation_update()
        return True

    @staticmethod
    def _compute_mask_bounds(mask_image):
        """Return the (i0,i1,j0,j1,k0,k1) voxel bounds of the mask's nonzero
        region, or None if empty/unavailable. Used so slice overlays only do
        work on slices the organ actually occupies."""
        try:
            import numpy as np
            from vtk.util import numpy_support
        except Exception:
            return None
        scalars = mask_image.GetPointData().GetScalars()
        if scalars is None:
            return None
        nx, ny, nz = mask_image.GetDimensions()
        if nx == 0 or ny == 0 or nz == 0:
            return None
        arr = numpy_support.vtk_to_numpy(scalars)
        if arr.size != nx * ny * nz:
            return None
        arr = arr.reshape(nz, ny, nx)  # VTK point order: x fastest, then y, z
        # Two full passes over the volume; everything else is cheap 2D work.
        zy = np.any(arr, axis=2)       # (z, y)
        yx = np.any(arr, axis=0)       # (y, x)
        zany = np.any(zy, axis=1)
        if not zany.any():
            return None
        yany = np.any(yx, axis=1)
        xany = np.any(yx, axis=0)
        zi = np.nonzero(zany)[0]
        yi = np.nonzero(yany)[0]
        xi = np.nonzero(xany)[0]
        ext = mask_image.GetExtent()
        return (
            ext[0] + int(xi[0]), ext[0] + int(xi[-1]),
            ext[2] + int(yi[0]), ext[2] + int(yi[-1]),
            ext[4] + int(zi[0]), ext[4] + int(zi[-1]),
        )

    def segmentation_names(self):
        return list(self._segmentation_actors.keys())

    def slice_segmentations(self):
        return [
            dict(item)
            for item in self._segmentation_masks.values()
            if item.get("visible", True)
        ]

    def segmentation_opacity(self, name):
        """返回某个分割器官当前的不透明度(0~1)；每个器官独立。"""
        item = self._segmentation_masks.get(name)
        return float(item.get("opacity", 1.0)) if item else 1.0

    def set_segmentation_opacity(self, name, opacity):
        """设置单个分割器官的不透明度(每个器官各自一个)。"""
        item = self._segmentation_masks.get(name)
        if item is None:
            return
        opacity = max(0.0, min(1.0, float(opacity)))
        item["opacity"] = opacity
        item["base_opacity"] = opacity
        actor = self._segmentation_actors.get(name)
        if actor is not None:
            actor.GetProperty().SetOpacity(opacity)
        self._queue_segmentation_update()

    def _segmentation_visible(self, name):
        item = self._segmentation_masks.get(name)
        if item is None:
            return False
        return bool(item.get("visible", True))

    def set_segmentation_visible(self, name, visible):
        item = self._segmentation_masks.get(name)
        actor = self._segmentation_actors.get(name)
        if item is None or actor is None:
            return
        item["visible"] = bool(visible)
        actor.SetVisibility(bool(visible))
        self._queue_segmentation_update()

    def set_all_segmentations_visible(self, visible):
        self.begin_segmentation_update()
        try:
            for name in list(self._segmentation_masks.keys()):
                self.set_segmentation_visible(name, visible)
        finally:
            self.end_segmentation_update()

    def _push_segmentations_to_slices(self, render=True):
        segmentations = self.slice_segmentations()
        self.slice_panel.set_segmentations(segmentations, render=render)
        if self.expanded_slice.isVisible():
            self.expanded_slice.set_segmentations(segmentations)

    def _make_segmentation_actor(self, mask_image, color, opacity, bounds=None, iso=0.5):
        """Build a smooth 3D surface from a binary organ mask.

        Contouring a binary 0/1 mask directly puts the iso-surface on voxel
        faces, so organs come out blocky/stair-stepped. Instead we cast the
        mask to float and Gaussian-blur it into a continuous scalar field —
        marching cubes then follows a smooth boundary between voxels rather
        than their faces. A windowed-sinc pass polishes the mesh and
        vtkPolyDataNormals recomputes shading normals on the final geometry.

        The whole chain is cropped to the organ's padded bounding box, so the
        blur/contour touch a small sub-volume instead of the full 512x512xN —
        for most organs this is actually faster than the old full-volume
        FlyingEdges while producing a far better surface.

        Tuning knobs:
          - 平滑程度：vtkImageGaussianSmooth 的 SetStandardDeviations(越大越圆滑)
          - 网格平滑：vtkWindowedSincPolyDataFilter 的迭代次数 / PassBand
        """
        # Crop to the organ footprint (+pad so the Gaussian tail closes the
        # surface cleanly at the edges).
        span = None
        if bounds is not None:
            ext = list(mask_image.GetExtent())
            pad = 4
            voi = vtk.vtkExtractVOI()
            voi.SetInputData(mask_image)
            voi.SetVOI(
                max(ext[0], int(bounds[0]) - pad), min(ext[1], int(bounds[1]) + pad),
                max(ext[2], int(bounds[2]) - pad), min(ext[3], int(bounds[3]) + pad),
                max(ext[4], int(bounds[4]) - pad), min(ext[5], int(bounds[5]) + pad),
            )
            span = min(
                int(bounds[1]) - int(bounds[0]) + 1 + 2 * pad,
                int(bounds[3]) - int(bounds[2]) + 1 + 2 * pad,
                int(bounds[5]) - int(bounds[4]) + 1 + 2 * pad,
            )
            cast = vtk.vtkImageCast()
            cast.SetInputConnection(voi.GetOutputPort())
        else:
            cast = vtk.vtkImageCast()
            cast.SetInputData(mask_image)
            span = min(mask_image.GetDimensions())
        # uint8 0/1 can't hold blurred values — must be float or the blur
        # rounds straight back to 0/1 and nothing is gained.
        cast.SetOutputScalarTypeToFloat()

        blur = vtk.vtkImageGaussianSmooth()
        blur.SetInputConnection(cast.GetOutputPort())
        blur.SetDimensionality(3)
        # Standard deviation in voxel units. Equal voxels means more physical
        # smoothing along the thick-Z axis, which is exactly what kills the
        # inter-slice staircase on CT with large slice spacing.
        # 小目标保护：固定 1.6σ 会把直径只有几个体素的小结节峰值抹到等值面
        # 以下，3D 视图里表现为结节模糊甚至消失。σ 按包围盒最小跨度自适应，
        # 大器官（跨度 ≥ 24 体素）仍保持 1.6σ 的圆滑效果。
        sigma = 1.6 if span >= 24 else max(0.5, span * 0.08)
        blur.SetStandardDeviations(sigma, sigma, sigma)
        blur.SetRadiusFactors(1.5, 1.5, 1.5)

        surface = vtk.vtkFlyingEdges3D()
        surface.SetInputConnection(blur.GetOutputPort())
        surface.SetValue(0, iso)
        surface.ComputeNormalsOff()  # recomputed after mesh smoothing below

        smooth = vtk.vtkWindowedSincPolyDataFilter()
        smooth.SetInputConnection(surface.GetOutputPort())
        # 小结节网格顶点少，按跨度收缩迭代次数，避免把小肿块磨成圆球
        smooth.SetNumberOfIterations(20 if span >= 32 else max(2, int(span) // 2))
        smooth.SetPassBand(0.1)
        smooth.NonManifoldSmoothingOn()
        smooth.NormalizeCoordinatesOn()
        smooth.BoundarySmoothingOff()

        normals = vtk.vtkPolyDataNormals()
        normals.SetInputConnection(smooth.GetOutputPort())
        normals.SetFeatureAngle(60.0)
        normals.SplittingOff()
        normals.ConsistencyOn()

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(normals.GetOutputPort())
        mapper.ScalarVisibilityOff()

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.SetPickable(False)
        prop = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetOpacity(opacity)
        prop.SetAmbient(0.28)
        prop.SetDiffuse(0.72)
        prop.SetSpecular(0.28)
        prop.SetSpecularPower(18)
        prop.SetInterpolationToPhong()
        return actor

    # ============================================================
    # 消融针规划（三维模型 + 切片投影 + 角度叠加文字）
    # 修改消融针外观：
    #   - 针杆颜色：_make_tube_actor(..., (0.78, 0.82, 0.86)) 的 RGB
    #   - 活性端颜色：_make_tube_actor(..., (1.0, 0.80, 0.10)) 的 RGB
    #   - 针尖颜色：_make_sphere_actor(..., (1.0, 0.18, 0.05)) 的 RGB
    #   - 入针点颜色：_make_sphere_actor(..., (0.15, 0.85, 1.0)) 的 RGB
    #   - 两端球的大小/透明度：_ENTRY_* / _TIP_* 四个常量
    # ============================================================
    # 入针点/消融点球：半径 = 针的可见半径 × 下面的倍数。
    # 消融点球半透明，好让球心下面的组织和消融范围椭球透出来；入针点在体表，
    # 不会挡住什么，保持不透明便于一眼定位。
    _ENTRY_SPHERE_SCALE = 3.5
    _ENTRY_SPHERE_OPACITY = 1.0
    _TIP_SPHERE_SCALE = 4.5
    _TIP_SPHERE_OPACITY = 0.45

    def has_ablation_needle(self):
        return self._ablation_needle is not None

    def ablation_needle(self):
        if self._ablation_needle is None:
            return None
        item = dict(self._ablation_needle)
        item["radius"] = self._needle_visual_radius()
        # 切片上针道交点的最小可见尺寸：真实针径在 512² 的片子上只有一两个
        # 像素，按图像尺度给个下限（比入针点/消融点标记小一圈）。
        item["marker_radius"] = self._point_radius() * 0.4
        return item

    def set_ablation_needle_params(self, shaft_mm=None, active_mm=None, diameter_mm=None):
        if shaft_mm is not None:
            self._ablation_params["shaft_mm"] = max(5.0, float(shaft_mm))
        if active_mm is not None:
            self._ablation_params["active_mm"] = max(1.0, float(active_mm))
        if diameter_mm is not None:
            self._ablation_params["diameter_mm"] = max(0.2, float(diameter_mm))
        if self._ablation_needle is not None:
            self.set_ablation_needle(
                self._ablation_needle["entry_ijk"],
                self._ablation_needle["tip_ijk"],
            )

    def reset_ablation_needle(self):
        if self.image is None:
            return
        entry_ijk, tip_ijk = self._default_ablation_needle_points()
        self.set_ablation_needle(entry_ijk, tip_ijk)

    def set_ablation_entry(self, ijk):
        if self.image is None:
            return
        entry_ijk = self._clamp_ijk(ijk)
        if self._ablation_needle is None:
            tip_ijk = self._tip_from_entry(entry_ijk)
        else:
            tip_ijk = self._ablation_needle["tip_ijk"]
        self.set_ablation_needle(entry_ijk, tip_ijk)

    def set_ablation_tip(self, ijk):
        if self.image is None:
            return
        tip_ijk = self._clamp_ijk(ijk)
        if self._ablation_needle is None:
            entry_ijk = self._entry_from_tip(tip_ijk)
        else:
            entry_ijk = self._ablation_needle["entry_ijk"]
        self.set_ablation_needle(entry_ijk, tip_ijk)

    def set_ablation_needle(self, entry_ijk, tip_ijk):
        if self.image is None:
            return
        entry_ijk = self._clamp_ijk(entry_ijk)
        tip_ijk = self._clamp_ijk(tip_ijk)
        entry_world = self._ijk_to_world(entry_ijk)
        tip_world = self._ijk_to_world(tip_ijk)
        if self._distance(entry_world, tip_world) < 1.0:
            tip_ijk = self._tip_from_entry(entry_ijk)
            tip_world = self._ijk_to_world(tip_ijk)

        self._ablation_needle = {
            "entry_ijk": entry_ijk,
            "tip_ijk": tip_ijk,
            "entry_world": entry_world,
            "tip_world": tip_world,
            "active_start_world": self._active_start_world(entry_world, tip_world),
            "shaft_mm": self._ablation_params["shaft_mm"],
            "active_mm": self._ablation_params["active_mm"],
            "diameter_mm": self._ablation_params["diameter_mm"],
        }
        self._rebuild_ablation_actors()
        self._update_needle_angle_overlay()
        if self._ablation_zone is not None:
            # Keep a displayed zone glued to the (moved) needle/active tip.
            self.set_ablation_zone(
                self._ablation_zone["half_long"], self._ablation_zone["half_short"])
        self._refresh_slice_points()
        self.renderer.ResetCameraClippingRange()
        self.render()
        self.ablationNeedleChanged.emit(True)

    def clear_ablation_needle(self):
        for actor in self._ablation_actors:
            self.renderer.RemoveActor(actor)
        self._ablation_actors = []
        self._ablation_needle = None
        self.clear_ablation_zone()
        self._update_needle_angle_overlay()
        self._refresh_slice_points()
        self.render()
        self.ablationNeedleChanged.emit(False)

    def _update_ablation_needle_positions(self):
        if self._ablation_needle is None:
            return
        self.set_ablation_needle(
            self._ablation_needle["entry_ijk"],
            self._ablation_needle["tip_ijk"],
        )

    # ---- 入针点 / 消融点规划 -------------------------------------------
    # 工作流：用户把"参考坐标轴(十字光标)"中心移动到目标处，分别放置入针点
    # 和消融点；两点都放好后自动连成针道——复用消融针可视化，入针点=针入口、
    # 消融点=针尖，set_ablation_needle 画出二者之间的连线(针道)。
    def planning_points(self):
        """返回当前规划点 {'entry': ijk 或 None, 'tip': ijk 或 None}。"""
        return {
            kind: (None if value is None else tuple(value))
            for kind, value in self._planning_points.items()
        }

    def _planning_ct_array(self):
        """Return a zero-copy Z/Y/X NumPy view of the current CT volume."""
        if (self.image is None or not self.info
                or str(self.info.get("modality", "")).upper() != "CT"):
            return None
        scalars = self.image.GetPointData().GetScalars()
        if scalars is None or scalars.GetNumberOfComponents() != 1:
            return None
        try:
            from vtk.util import numpy_support

            dims = self.image.GetDimensions()
            return numpy_support.vtk_to_numpy(scalars).reshape(
                dims[2], dims[1], dims[0])
        except Exception:
            log.exception("无法读取 CT 体素，针道骨骼检测不可用。")
            return None

    def evaluate_planning_point(self, ijk):
        """Check whether a proposed entry/tip point is inside or near bone."""
        volume = self._planning_ct_array()
        if volume is None:
            return {
                "available": False,
                "reason": "当前影像不是可进行 HU 骨骼判定的单通道 CT。",
            }
        point = self._clamp_ijk(ijk)
        return needle_planning.analyze_bone_path(
            volume, self.image.GetSpacing(), point, point,
            needle_radius_mm=self._ablation_params["diameter_mm"] * 0.5,
            max_length_mm=self._ablation_params["shaft_mm"],
        )

    def evaluate_needle_path(self, entry_ijk, tip_ijk):
        """Return length, bone collision, and bone-clearance data for a path."""
        volume = self._planning_ct_array()
        if volume is None:
            return {
                "available": False,
                "reason": "当前影像不是可进行 HU 骨骼判定的单通道 CT。",
            }
        return needle_planning.analyze_bone_path(
            volume, self.image.GetSpacing(),
            self._clamp_ijk(entry_ijk), self._clamp_ijk(tip_ijk),
            needle_radius_mm=self._ablation_params["diameter_mm"] * 0.5,
            max_length_mm=self._ablation_params["shaft_mm"],
        )

    def current_needle_bone_analysis(self):
        if self._ablation_needle is None:
            return None
        return self.evaluate_needle_path(
            self._ablation_needle["entry_ijk"],
            self._ablation_needle["tip_ijk"])

    def recommend_planning_entry(self, tip_ijk=None):
        """Find the best sampled, reachable, bone-free skin entry for a tip."""
        volume = self._planning_ct_array()
        if volume is None:
            return None
        target = tip_ijk
        if target is None:
            target = self._planning_points.get("tip")
        if target is None:
            return None
        return needle_planning.recommend_entry_point(
            volume, self.image.GetSpacing(), self._clamp_ijk(target),
            max_length_mm=self._ablation_params["shaft_mm"],
            needle_radius_mm=self._ablation_params["diameter_mm"] * 0.5,
        )

    def planning_entry_search_args(self, tip_ijk=None):
        """在调用线程上一次性捕获后台搜索所需的只读输入。

        搜索耗时随 CT 规模增长，应放到工作线程执行；本方法把体数据视图、
        间距、目标点和针型参数打包返回，避免工作线程再回头触碰 VTK 状态。
        """
        volume = self._planning_ct_array()
        if volume is None:
            return None
        target = tip_ijk
        if target is None:
            target = self._planning_points.get("tip")
        if target is None:
            return None
        return {
            "volume": volume,
            "spacing": tuple(float(s) for s in self.image.GetSpacing()),
            "target_ijk": tuple(self._clamp_ijk(target)),
            "max_length_mm": float(self._ablation_params["shaft_mm"]),
            "needle_radius_mm": float(self._ablation_params["diameter_mm"]) * 0.5,
        }

    def set_planning_point(self, kind, ijk):
        """在 ijk 处放置/更新一个规划点。

        参数：
          kind — 'entry'(入针点) 或 'tip'(消融点)
          ijk  — 放置位置（通常取坐标轴中心 crosshair_ijk()）

        两点齐备时自动连成针道。返回 True 表示已连接，否则 False。
        """
        if self.image is None or kind not in ("entry", "tip"):
            return False
        self._planning_points[kind] = self._clamp_ijk(ijk)
        self._rebuild_planning_marker(kind)
        if (self._planning_points["entry"] is not None
                and self._planning_points["tip"] is not None):
            connected = self.connect_planning_points()
            # 连成针道之后再发信号：刷新方才能读到针道长度/角度等派生量。
            self.planningChanged.emit()
            return connected
        self._refresh_slice_points()
        self.renderer.ResetCameraClippingRange()
        self.render()
        self.planningChanged.emit()
        return False

    def connect_planning_points(self):
        """把已放置的入针点和消融点连成针道(消融针)。两点缺一不可。"""
        entry = self._planning_points["entry"]
        tip = self._planning_points["tip"]
        if entry is None or tip is None:
            return False
        # 针道复用消融针：入针点→针入口，消融点→针尖，画出连线 + 两端球。
        self.set_ablation_needle(entry, tip)
        # 针道已带入针点/针尖球，移除临时规划标记球避免重复。
        self._remove_planning_markers()
        # 两点齐备(通常是最后放入针点)后，坐标轴跳到消融点：三向切片跟着滚到
        # 靶点那一层，接着就能直接看消融范围，不用再手动把十字拖回去。
        self.set_crosshair_ijk(tip)
        return True

    def clear_planning_points(self):
        """清除入针点/消融点规划标记(不主动删除已生成的针道)。"""
        self._planning_points = {"entry": None, "tip": None}
        self._remove_planning_markers()
        self._refresh_slice_points()
        self.planningChanged.emit()
        self.render()

    def _remove_planning_markers(self):
        for kind in ("entry", "tip"):
            actor = self._planning_actors.get(kind)
            if actor is not None:
                self.renderer.RemoveActor(actor)
            self._planning_actors[kind] = None

    def _rebuild_planning_marker(self, kind):
        actor = self._planning_actors.get(kind)
        if actor is not None:
            self.renderer.RemoveActor(actor)
            self._planning_actors[kind] = None
        ijk = self._planning_points.get(kind)
        if ijk is None:
            return
        world = self._ijk_to_world(ijk)
        # 颜色、大小、透明度都与针道两端一致：入针点绿色不透明(=针入口球)、
        # 消融点红色半透明(=针尖球)。
        base = self._needle_visual_radius()
        if kind == "entry":
            color = (0.20, 0.85, 0.30)
            scale, opacity = self._ENTRY_SPHERE_SCALE, self._ENTRY_SPHERE_OPACITY
        else:
            color = (1.0, 0.18, 0.05)
            scale, opacity = self._TIP_SPHERE_SCALE, self._TIP_SPHERE_OPACITY
        marker = self._make_sphere_actor(world, base * scale, color, opacity)
        self.renderer.AddActor(marker)
        self._planning_actors[kind] = marker

    # ---- needle angle overlay ------------------------------------------
    def _find_cjk_font(self):
        """Locate a CJK-capable TrueType font for the 3D-view text overlay.

        VTK's bundled font is Latin-only, so without a real CJK face the Chinese
        labels render as blanks. Returns None if none is found (Latin fallback).
        """
        for path in (
            r"C:\Windows\Fonts\msyh.ttc",     # Microsoft YaHei (matches the UI)
            r"C:\Windows\Fonts\msyh.ttf",
            r"C:\Windows\Fonts\simhei.ttf",   # SimHei
            r"C:\Windows\Fonts\simsun.ttc",   # SimSun
        ):
            if os.path.exists(path):
                return path
        return None

    def _make_needle_angle_actor(self):
        """Top-left HUD that reads out the needle's axis angles in the 3D view."""
        actor = vtk.vtkTextActor()
        actor.SetTextScaleModeToNone()
        actor.SetVisibility(False)
        coord = actor.GetPositionCoordinate()
        coord.SetCoordinateSystemToNormalizedViewport()
        coord.SetValue(0.022, 0.972)
        prop = actor.GetTextProperty()
        prop.SetFontSize(17)
        prop.SetColor(0.86, 0.94, 0.98)
        prop.SetLineSpacing(1.3)
        prop.SetJustificationToLeft()
        prop.SetVerticalJustificationToTop()
        prop.ShadowOn()
        prop.SetShadowOffset(1, -1)
        if self._cjk_font:
            prop.SetFontFamily(vtk.VTK_FONT_FILE)
            prop.SetFontFile(self._cjk_font)
        else:
            prop.SetFontFamilyToArial()
        return actor

    def _needle_axis_angles(self):
        """Direction angles (deg) of the entry→tip needle vs. the X/Y/Z axes.

        Standard direction angles of the rendered needle vector, so they match
        what you see against the orientation-axes marker (cos²x+cos²y+cos²z == 1).
        Range 0–180°; entry→tip is the direction the needle advances.
        """
        if self._ablation_needle is None:
            return None
        p0 = self._ablation_needle["entry_world"]
        p1 = self._ablation_needle["tip_world"]
        vec = [p1[i] - p0[i] for i in range(3)]
        length = math.sqrt(sum(c * c for c in vec))
        if length < 1e-9:
            return None
        return tuple(
            math.degrees(math.acos(max(-1.0, min(1.0, vec[i] / length))))
            for i in range(3)
        )

    # ---- 定位取景与规划核对单（mainwindow 的结节定位卡片/报告导出使用）----

    def needle_axis_angles(self):
        """针道方向与世界 X/Y/Z 轴的夹角（度）；未连成针道时 None。"""
        return self._needle_axis_angles()

    def needle_path_length_mm(self):
        """入针点到消融点的针道长度（mm）；未连成针道时 None。"""
        if self._ablation_needle is None:
            return None
        p0 = self._ablation_needle["entry_world"]
        p1 = self._ablation_needle["tip_world"]
        return math.sqrt(sum((p1[i] - p0[i]) ** 2 for i in range(3)))

    def planning_world_points(self):
        """规划点（入针点/消融点）的世界坐标 mm；未放置的为 None。"""
        output = {}
        for kind in ("entry", "tip"):
            ijk = self._planning_points.get(kind)
            output[kind] = None if ijk is None else tuple(
                float(v) for v in self._ijk_to_world(ijk))
        return output

    def ablation_params(self):
        """当前消融针几何参数（mm）的副本。"""
        return dict(self._ablation_params)

    def ablation_zone_info(self):
        """当前消融椭球半轴与体积；未放置消融区时 None。"""
        zone = self._ablation_zone
        if not zone:
            return None
        half_long = float(zone["half_long"])
        half_short = float(zone["half_short"])
        return {
            "half_long_mm": half_long,
            "half_short_mm": half_short,
            # 绕针长轴的旋转椭球：V = 4/3·π·a·b²
            "volume_ml": 4.0 / 3.0 * math.pi * half_long * half_short ** 2 / 1000.0,
        }

    def nodule_depth_mm(self, ijk):
        """结节质心沿腹侧方向到体表的距离（mm）；射线不可解时 None。

        用与自动推荐入针点同一套 HU 体壁检测，只在单条射线上采样，
        单次调用毫秒级，可逐结节调用。
        """
        if self.image is None or ijk is None:
            return None
        args = self.planning_entry_search_args(tip_ijk=ijk)
        if args is None:
            return None
        anterior, _view_up = self._anatomical_camera_axes(self.info)
        try:
            entry = needle_planning.find_body_entry_on_ray(
                args["volume"], args["spacing"], args["target_ijk"],
                anterior, max_length_mm=max(250.0, args["max_length_mm"]))
        except Exception:
            return None
        if not entry:
            return None
        return float(entry["path_length_mm"])

    def focus_camera_on_ijk(self, ijk, direction_world=None):
        """把 3D 相机转到从指定一侧观察 ijk（保持当前距离，不渲染）。

        direction_world 是"从病灶指向相机"的方向，默认取患者腹侧（与
        reset_view 同源），即从前方看病灶；传入 normalize(entry-tip)
        可从入针侧预演进针视角。渲染由随后的十字联动 flush/调用方完成。
        """
        if self.image is None or ijk is None:
            return False
        world = self._ijk_to_world(self._clamp_ijk(ijk))
        anterior, view_up = self._anatomical_camera_axes(self.info)
        direction = anterior if direction_world is None else tuple(
            float(v) for v in direction_world)
        norm = math.sqrt(sum(v * v for v in direction))
        if norm < 1.0e-9:
            return False
        direction = tuple(v / norm for v in direction)
        cam = self.renderer.GetActiveCamera()
        distance = max(float(cam.GetDistance()), 1.0)
        cam.SetFocalPoint(*world)
        cam.SetPosition(*(world[i] + direction[i] * distance for i in range(3)))
        # 视角上方固定为头侧，并投影到成像平面内（与 reset_view 同一约束）。
        along = sum(view_up[i] * direction[i] for i in range(3))
        up = tuple(view_up[i] - along * direction[i] for i in range(3))
        up_norm = math.sqrt(sum(v * v for v in up))
        if up_norm > 1.0e-6:
            cam.SetViewUp(*(v / up_norm for v in up))
        cam.OrthogonalizeViewUp()
        self.renderer.ResetCameraClippingRange()
        self._widen_clipping_for_crosshair()
        return True

    def enter_view3d_fullscreen(self):
        """定位取景用：未处于全屏时进入 3D 全屏；已全屏/过渡中则不动。"""
        if (self.image is not None and not self._view3d_fullscreen
                and not self._view3d_fullscreen_transition):
            self._toggle_view3d_fullscreen()
            return True
        return False

    def capture_report_pngs(self):
        """抓取 3D 与三向切片当前画面，返回 {名称: PNG 字节}。

        用 vtkWindowToImageFilter 从各渲染窗口自己的帧缓冲读回，与屏幕
        遮挡无关（3D 全屏盖住切片栏时也能抓到切片画面）。单次调用四次
        GPU 读回，仅用于导出核对单一类低频操作。
        """
        from vtk.util import numpy_support

        output = {}
        targets = [("view3d", self.render_window)]
        for view in self.slice_panel.views:
            window = view.image_viewer.GetRenderWindow()
            if window is not None:
                targets.append((view.orientation, window))
        for name, window in targets:
            try:
                window.Render()
                reader = vtk.vtkWindowToImageFilter()
                reader.SetInput(window)
                reader.SetScale(1)
                reader.Update()
                writer = vtk.vtkPNGWriter()
                writer.SetWriteToMemory(True)
                writer.SetInputConnection(reader.GetOutputPort())
                writer.Write()
                data = numpy_support.vtk_to_numpy(writer.GetResult()).tobytes()
                if data:
                    output[str(name)] = data
            except Exception:
                continue        # 单个视图失败不应让整份核对单导不出来
        return output

    def _needle_angle_text(self, ax, ay, az):
        if self._cjk_font:
            return ("消融针进针角度\n"
                    "X    %3.0f°\n"
                    "Y    %3.0f°\n"
                    "Z    %3.0f°" % (ax, ay, az))
        return ("Needle angle\n"
                "X   %3.0f°\n"
                "Y   %3.0f°\n"
                "Z   %3.0f°" % (ax, ay, az))

    def _update_needle_angle_overlay(self):
        actor = getattr(self, "_needle_angle_actor", None)
        if actor is None:
            return
        angles = self._needle_axis_angles()
        if angles is None:
            actor.SetVisibility(False)
            return
        actor.SetInput(self._needle_angle_text(*angles))
        actor.SetVisibility(True)

    # ---- needle IMU sensor readout overlay -----------------------------
    # 消融针上的陀螺仪(IMU)通过串口持续上报温度与姿态角。右上角 HUD 同时给出
    # 温度和三个姿态角的数字，下面再配一支“立体的带手柄消融针”跟着实时姿态
    # 转动：数字看准确值，模型看大致朝向。
    # 针旁边配一套人体坐标轴(与左下角方向标记同源)，两者的相机每帧都对齐到主
    # 相机——转动 3D 视图时坐标轴和针一起跟着人体转，针的姿态可以直接对轴读。
    #
    # 调整外观：
    #   - 针的零件配色：_IMU_NEEDLE_MESHES（模型本身来自 STEP 图纸，改形状要重新导出）
    #   - 立体针位置与大小：_IMU_NEEDLE_VIEWPORT（改高度记得给上方文字留行）
    #   - 取景余量：_IMU_NEEDLE_RADIUS（_IMU_NEEDLE_CAM_POS 只是首帧方位）
    #   - 坐标轴位置/大小：_IMU_AXES_VIEWPORT / _IMU_AXES_RADIUS
    #   - 偏航漂移时关掉 yaw：_IMU_NEEDLE_USE_YAW
    #   - 读数字号/颜色：_make_imu_temp_actor()；文案与位数：_imu_readout_text()

    # 尺寸取自装配体 STEP 图纸(针尾部水管装配体.STEP)，只保留针体本身：
    #   针杆  Ø1.6 x 168mm  (不锈钢钢管1.6x168)   手柄 Ø16.5mm (绝缘衬套DO-16/刺头C16x5)
    #   针杆刻度环间距 10mm                        全长约 208mm
    # 实物尾部还接同轴电缆(Ø0.8x192)和针尾部水管，HUD 上不画。
    #
    # 针的三维模型直接来自厂家的装配体图纸(针尾部水管装配体.STEP)，用 OpenCascade
    # 离线镶嵌成网格后存为 assets/needle_*.stl，运行时只是加载，不需要 CAD 依赖。
    # 只保留针体本身：手柄壳/封口盖、Ø1.6x168 针杆、刺头C16x5+绝缘衬套DO-16、
    # 尾部同轴连接器；同轴电缆(Ø0.8x192)、针尾部水管和管路接头都已剔除。
    # 导出时做了工程"断裂视图"处理，方便在小窗里看清：手柄/连接器/刺头完全按图纸
    # 原样(未变形)，只把中间那段**等直径**针杆截去一节把针尖拉近，并把针杆+刺头
    # 径向放粗 2 倍(真实 Ø1.6 在 HUD 上只有约 2px)。归一化为以中点为原点、针尖朝 +X。
    # (STL 文件名, RGB)
    _IMU_NEEDLE_MESHES = (
        ("needle_handle", (216, 220, 228)),     # 手柄壳前端B + 手柄封口盖
        ("needle_shaft", (120, 127, 138)),      # 不锈钢钢管 Ø1.6x168
        ("needle_tip", (188, 195, 205)),        # 刺头C16x5 + 绝缘衬套DO-16
        ("needle_conn", (208, 172, 66)),        # 同轴连接器铜件(金色)
        ("needle_conn_ins", (228, 230, 234)),   # 同轴连接器 PTFE 绝缘
    )
    _IMU_NEEDLE_RADIUS = 5.9            # 取景半径：断裂视图后模型半长 5.25，留 ~12% 余量
    _IMU_NEEDLE_CAM_POS = (4.0, 7.0, 22.0)  # 首帧观察方位；随后由主相机接管
    # 立体针视口（归一化窗口坐标）：上方给姿态角与磁方位读数留出空间
    _IMU_NEEDLE_VIEWPORT = (0.795, 0.500, 0.988, 0.780)
    # 姿态 HUD 左下角的人体坐标轴小视口与取景半径
    _IMU_AXES_VIEWPORT = (0.784, 0.523, 0.878, 0.645)
    _IMU_AXES_RADIUS = 1.42
    # 若上游退化为六轴解算，偏航(yaw)会缓慢漂移；可把这里改成 False，仅显示
    # 有重力校正的俯仰+横滚。九轴数据正常时保持 True 显示完整姿态。
    _IMU_NEEDLE_USE_YAW = True
    # 遥测刷新死区：温度(°C)/俯仰/横滚/偏航(°)及磁场(µT)的变化都小于阈值时
    # 不重渲染。遥测约 20 帧/秒，探针静止时若无死区，每帧都会全量体渲染。
    _IMU_READOUT_EPS = (0.05, 0.05, 0.05, 0.05)
    _IMU_MAGNETIC_EPS = 0.5

    def _make_imu_temp_actor(self):
        """右上角 HUD：消融针传感器读数（温度 + 俯仰/横滚/偏航）。"""
        actor = vtk.vtkTextActor()
        actor.SetTextScaleModeToNone()
        actor.SetVisibility(False)
        coord = actor.GetPositionCoordinate()
        coord.SetCoordinateSystemToNormalizedViewport()
        coord.SetValue(0.978, 0.972)            # 右上角；文字右对齐贴边
        prop = actor.GetTextProperty()
        prop.SetFontSize(17)
        prop.SetColor(1.0, 0.80, 0.36)          # 暖色，区别于进针角度（冷色）
        prop.SetLineSpacing(1.3)
        prop.SetJustificationToRight()
        prop.SetVerticalJustificationToTop()
        prop.ShadowOn()
        prop.SetShadowOffset(1, -1)
        if self._cjk_font:
            prop.SetFontFamily(vtk.VTK_FONT_FILE)
            prop.SetFontFile(self._cjk_font)
        else:
            prop.SetFontFamilyToArial()
        return actor

    @staticmethod
    def _make_imu_magnetic_actor():
        """Create the green 3D arrow used to indicate the magnetic vector."""
        source = vtk.vtkArrowSource()
        source.SetShaftRadius(0.045)
        source.SetTipRadius(0.12)
        source.SetTipLength(0.30)
        source.SetShaftResolution(20)
        source.SetTipResolution(24)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(source.GetOutputPort())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.SetScale(1.15)
        actor.SetVisibility(False)
        actor.SetPickable(False)
        prop = actor.GetProperty()
        prop.SetColor(0.20, 1.0, 0.48)
        prop.SetAmbient(0.75)
        prop.SetDiffuse(0.45)
        prop.SetSpecular(0.25)
        return actor

    def _make_imu_needle_part(self, stl_path, rgb):
        """加载一个针体零件网格(STL)，并给上金属质感。"""
        reader = vtk.vtkSTLReader()
        reader.SetFileName(stl_path)
        # 按特征角重算法线：STL 只有面法线，直接渲染圆柱面会有明显棱块感
        normals = vtk.vtkPolyDataNormals()
        normals.SetInputConnection(reader.GetOutputPort())
        normals.SetFeatureAngle(45)
        normals.SplittingOn()
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(normals.GetOutputPort())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
        prop.SetAmbient(0.28)
        prop.SetDiffuse(0.75)
        prop.SetSpecular(0.55)
        prop.SetSpecularPower(45)
        return actor

    def _make_imu_needle_renderer(self):
        """右上角叠加视口：真三维的带手柄消融针，跟随 IMU 姿态转动。

        单独用一个 layer=1 的 renderer 画：背景透明地叠在主场景之上。针是厂家
        STEP 图纸镶嵌出来的真实网格(有光照和透视)，不是二维贴图也不是近似圆柱。
        相机方位由 _sync_imu_hud_cameras 与主相机对齐：转动 3D 视图时针跟着
        人体一起转，针的姿态可以直接对着旁边的人体坐标轴读。
        """
        asset_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
        assembly = vtk.vtkAssembly()
        loaded = 0
        for name, rgb in self._IMU_NEEDLE_MESHES:
            path = os.path.join(asset_dir, name + ".stl")
            if not os.path.exists(path):
                log.warning("消融针模型缺失，跳过：%s", path)
                continue
            assembly.AddPart(self._make_imu_needle_part(path, rgb))
            loaded += 1
        if not loaded:
            # 模型都不在就别显示了，免得空转；温度文字不受影响。
            log.warning("消融针三维模型未加载，姿态示意图将不显示。")
        assembly.SetVisibility(False)
        self._imu_needle_assembly = assembly

        renderer = vtk.vtkRenderer()
        renderer.SetLayer(1)                # 叠加层：不清背景，透明盖在主场景上
        renderer.InteractiveOff()
        renderer.SetViewport(*self._IMU_NEEDLE_VIEWPORT)   # 3D 视图右上角
        renderer.AddViewProp(assembly)
        camera = renderer.GetActiveCamera()
        camera.SetPosition(*self._IMU_NEEDLE_CAM_POS)
        camera.SetFocalPoint(0.0, 0.0, 0.0)
        camera.SetViewUp(0.0, 1.0, 0.0)
        light = vtk.vtkLight()
        light.SetLightTypeToCameraLight()
        light.SetPosition(-0.3, 0.5, 1.0)
        light.SetFocalPoint(0.0, 0.0, 0.0)
        light.SetIntensity(1.15)
        renderer.AddLight(light)

        window = self.render_window
        window.SetNumberOfLayers(max(2, window.GetNumberOfLayers()))
        window.AddRenderer(renderer)
        return renderer

    def _make_imu_axes_renderer(self):
        """姿态 HUD 角上的人体坐标轴：和左下角方向标记同一套 X/Y/Z。

        单独一个 layer=1 的小视口，相机方位同样由 _sync_imu_hud_cameras 与
        主相机对齐——所以它和左下角的人体坐标轴永远指向同一个方向，旁边那
        支立体针也就是在同一个坐标系里转。
        """
        axes = self._make_body_axes_actor()
        axes.SetVisibility(False)
        self._imu_axes_actor = axes
        magnetic = self._make_imu_magnetic_actor()
        self._imu_magnetic_actor = magnetic

        renderer = vtk.vtkRenderer()
        renderer.SetLayer(1)                # 叠加层：不清背景，透明盖在主场景上
        renderer.InteractiveOff()
        renderer.SetViewport(*self._IMU_AXES_VIEWPORT)
        renderer.AddViewProp(axes)
        renderer.AddViewProp(magnetic)
        camera = renderer.GetActiveCamera()
        # 与主视图一致的平行投影：轴向不受近大远小影响
        camera.ParallelProjectionOn()
        camera.SetFocalPoint(0.0, 0.0, 0.0)

        window = self.render_window
        window.SetNumberOfLayers(max(2, window.GetNumberOfLayers()))
        window.AddRenderer(renderer)
        return renderer

    def _sync_imu_hud_cameras(self, caller=None, event=None):
        """把姿态 HUD 的两个视口对齐到主相机方位（每帧渲染前调用）。

        HUD 里的坐标轴因此与左下角人体坐标轴指向完全一致，转动 3D 视图时
        坐标轴和立体针一起跟着人体转。

        取景用固定包围球，姿态/视角怎么转都不出框、也不会忽远忽近。注意
        ResetCamera 取的是包围盒"对角线"，直接传 ±R 会按半径 R*√3 取景、
        模型会缩得很小；这里先把盒子除以 √3，才等价于按半径 R 的球取景。
        """
        main_camera = self.renderer.GetActiveCamera()
        direction = main_camera.GetDirectionOfProjection()
        view_up = main_camera.GetViewUp()
        for renderer, radius in (
                (getattr(self, "_imu_needle_renderer", None),
                 self._IMU_NEEDLE_RADIUS),
                (getattr(self, "_imu_axes_renderer", None),
                 self._IMU_AXES_RADIUS)):
            if renderer is None:
                continue
            camera = renderer.GetActiveCamera()
            camera.SetFocalPoint(0.0, 0.0, 0.0)
            camera.SetPosition(-direction[0], -direction[1], -direction[2])
            camera.SetViewUp(*view_up)
            half = radius / math.sqrt(3.0)
            renderer.ResetCamera(-half, half, -half, half, -half, half)

    def _orient_imu_needle(self, pitch, roll, yaw):
        """把 IMU 姿态角映射成针的三维朝向。

        针体沿 +X：俯仰=绕 Z(针尖上下)、偏航=绕 Y(针尖左右)、横滚=绕针自身轴 X
        (靠手柄上的防滑纹能看出来)。
        """
        assembly = self._imu_needle_assembly
        if assembly is None:
            return
        transform = vtk.vtkTransform()
        transform.PostMultiply()
        transform.RotateX(roll)
        transform.RotateZ(pitch)
        if self._IMU_NEEDLE_USE_YAW:
            transform.RotateY(-yaw)
        assembly.SetUserTransform(transform)
        self._sync_imu_hud_cameras()

    def _orient_magnetic_arrow(self, magnetic):
        """Point the HUD arrow along the normalized body-frame magnetic vector."""
        actor = self._imu_magnetic_actor
        if actor is None:
            return False
        vector = tuple(float(value) for value in magnetic)
        length = math.sqrt(sum(value * value for value in vector))
        if length < 1.0e-9:
            actor.SetVisibility(False)
            return False
        direction = tuple(value / length for value in vector)

        # vtkArrowSource points along +X. Rotate +X onto the measured vector.
        dot = max(-1.0, min(1.0, direction[0]))
        angle = math.degrees(math.acos(dot))
        axis = (0.0, -direction[2], direction[1])
        axis_length = math.sqrt(sum(value * value for value in axis))
        if axis_length < 1.0e-9:
            axis = (0.0, 0.0, 1.0)
        else:
            axis = tuple(value / axis_length for value in axis)
        transform = vtk.vtkTransform()
        transform.RotateWXYZ(angle, *axis)
        actor.SetUserTransform(transform)
        actor.SetVisibility(True)
        return True

    @staticmethod
    def _magnetic_heading(magnetic, pitch, roll):
        """Tilt-compensated magnetic heading; no true-north correction."""
        if magnetic is None:
            return None
        mag_x, mag_y, mag_z = (float(value) for value in magnetic)
        if mag_x * mag_x + mag_y * mag_y + mag_z * mag_z < 1.0e-12:
            return None
        pitch = math.radians(float(pitch))
        roll = math.radians(float(roll))
        sr, cr = math.sin(roll), math.cos(roll)
        sp, cp = math.sin(pitch), math.cos(pitch)
        level_x = mag_x * cp + mag_y * sr * sp + mag_z * cr * sp
        level_y = mag_y * cr - mag_z * sr
        if level_x * level_x + level_y * level_y < 1.0e-12:
            return None
        return math.degrees(math.atan2(-level_y, level_x)) % 360.0

    def _imu_readout_text(self, temp_c, pitch, roll, yaw, magnetic=None):
        """右上角 HUD 文字：温度、三个姿态角与可选磁方位。

        立体针只表达"大概朝哪"，具体多少度还是看数字更准，所以两者都给。
        """
        heading = self._magnetic_heading(magnetic, pitch, roll)
        magnitude = None
        if magnetic is not None:
            magnitude = math.sqrt(sum(float(value) ** 2 for value in magnetic))
        if self._cjk_font:
            text = ("消融针传感器\n"
                    "温度 %.1f°C\n"
                    "俯仰 %+.1f°\n"
                    "横滚 %+.1f°\n"
                    "偏航 %+.1f°" % (temp_c, pitch, roll, yaw))
            if heading is not None:
                text += "\n磁场 %.1f μT\n磁方位 %.1f°" % (magnitude, heading)
            return text
        text = ("Needle sensor\n"
                "Temp  %.1f°C\n"
                "Pitch %+.1f°\n"
                "Roll  %+.1f°\n"
                "Yaw   %+.1f°" % (temp_c, pitch, roll, yaw))
        if heading is not None:
            text += "\nField %.1f uT\nMag heading %.1f°" % (magnitude, heading)
        return text

    def _refresh_imu_readout_overlay(self):
        """根据缓存的最新遥测刷新读数文字与立体针姿态（不主动触发渲染）。

        没导入数据时整块 HUD 都不显示：3D 图像还没出来就先飘一支针和一套
        坐标轴在黑屏上，既没有参照也容易让人以为是残留。数据加载后
        set_volume 会再调一次本函数，那时缓存的遥测会立刻补上。
        """
        temp_actor = getattr(self, "_imu_temp_actor", None)
        assembly = getattr(self, "_imu_needle_assembly", None)
        axes_actor = getattr(self, "_imu_axes_actor", None)
        magnetic_actor = getattr(self, "_imu_magnetic_actor", None)
        data = getattr(self, "_imu_readout", None)
        if not data or self.image is None:
            if temp_actor is not None:
                temp_actor.SetVisibility(False)
            if assembly is not None:
                assembly.SetVisibility(False)
            if axes_actor is not None:
                axes_actor.SetVisibility(False)
            if magnetic_actor is not None:
                magnetic_actor.SetVisibility(False)
            return
        temp_c, pitch, roll, yaw, magnetic = data
        if temp_actor is not None:
            temp_actor.SetInput(
                self._imu_readout_text(temp_c, pitch, roll, yaw, magnetic))
            temp_actor.SetVisibility(True)
        if assembly is not None:
            self._orient_imu_needle(pitch, roll, yaw)
            assembly.SetVisibility(True)
        # 坐标轴和针一起出现/消失：没有姿态数据时整块 HUD 都不显示
        if axes_actor is not None:
            axes_actor.SetVisibility(True)
        if magnetic_actor is not None:
            if magnetic is None:
                magnetic_actor.SetVisibility(False)
            else:
                self._orient_magnetic_arrow(magnetic)

    def set_imu_sensor_readout(self, temp_c, pitch, roll, yaw, magnetic=None):
        """更新消融针传感器实时读数并刷新显示。

        由主窗口在解析到 IMU 串口遥测后调用。姿态只驱动右上角立体针旋转；
        可选三轴磁场驱动绿色方向箭头。这里不做任何位置积分或平移。

        遥测约 20 帧/秒，而一次 render() 是全量体渲染：读数总是缓存并同步
        到 HUD actor（纯属性更新，很便宜），但只在数值越过死区、且已加载
        体数据（HUD 仅在有数据时可见，见 _refresh_imu_readout_overlay）
        时才真正重渲染。
        """
        magnetic_value = None
        if magnetic is not None:
            magnetic_value = tuple(float(value) for value in magnetic)
            if len(magnetic_value) != 3:
                raise ValueError("magnetic must contain exactly three axes")
        # Only the orientation HUD is updated.  Deliberately do not modify the
        # planned needle entry/tip or integrate a translation from sensor data.
        readout = (
            float(temp_c), float(pitch), float(roll), float(yaw), magnetic_value)
        changed = self._imu_readout_changed(readout)
        self._imu_readout = readout
        self._refresh_imu_readout_overlay()
        if changed and self.image is not None:
            self.render()

    def _imu_readout_changed(self, readout):
        """新遥测是否越过刷新死区（首次收到读数视为变化）。

        读数元组 = (温度, 俯仰, 横滚, 偏航, 磁场或 None)；前四项按标量阈值
        比较，磁场按每轴阈值比较，出现/消失本身也视为变化。
        """
        previous = self._imu_readout
        if previous is None:
            return True
        for old, new, eps in zip(previous, readout, self._IMU_READOUT_EPS):
            if abs(new - old) > eps:
                return True
        old_magnetic, new_magnetic = previous[4], readout[4]
        if (old_magnetic is None) != (new_magnetic is None):
            return True
        if old_magnetic is not None and any(
                abs(new - old) > self._IMU_MAGNETIC_EPS
                for old, new in zip(old_magnetic, new_magnetic)):
            return True
        return False

    def clear_imu_sensor_readout(self):
        """清除消融针传感器读数（例如串口断开时）。"""
        # HUD 可见 ⟺ 已缓存读数且已加载体数据；两者任一不满足时本来就
        # 不可见，清理纯属属性操作，无需为它渲染一帧。
        hud_was_shown = self._imu_readout is not None and self.image is not None
        self._imu_readout = None
        self._refresh_imu_readout_overlay()
        if hud_was_shown:
            self.render()

    # ============================================================
    # 消融范围椭球体
    # 消融区是沿消融针方向生长的一个半透明橙红色椭球体
    # 修改外观：_make_zone_actor() 中的 SetColor/SetOpacity
    # ============================================================
    def has_ablation_zone(self):
        return self._ablation_zone is not None

    def ablation_zone_polydata(self):
        return self._ablation_zone["polydata"] if self._ablation_zone else None

    def set_ablation_zone(self, half_long_mm, half_short_mm):
        """Place/resize the coagulation ellipsoid on the needle's active tip.

        Half-axes are in mm; ``half_long`` runs along the needle. Needs a needle.
        """
        if self.image is None or self._ablation_needle is None:
            return
        needle = self._ablation_needle
        center = tuple(
            (needle["active_start_world"][i] + needle["tip_world"][i]) * 0.5
            for i in range(3)
        )
        axis = self._needle_axis()
        half_long = max(0.5, float(half_long_mm))
        half_short = max(0.5, float(half_short_mm))

        self._zone_transform.SetMatrix(
            self._zone_matrix(center, axis, half_long, half_short))
        self._zone_transform.Modified()
        self._zone_tpf.Update()

        self._ablation_zone = {
            "half_long": half_long,
            "half_short": half_short,
            "polydata": self._zone_tpf.GetOutput(),
        }
        self._zone_actor.SetVisibility(True)
        self._push_zone_to_slices()
        self.render()

    def clear_ablation_zone(self):
        self._ablation_zone = None
        self._zone_actor.SetVisibility(False)
        self._push_zone_to_slices()
        self.render()

    def _push_zone_to_slices(self):
        poly = self._ablation_zone["polydata"] if self._ablation_zone else None
        self.slice_panel.set_ablation_zone(poly)
        self.expanded_slice.set_ablation_zone(poly)

    def _needle_axis(self):
        p0 = self._ablation_needle["entry_world"]
        p1 = self._ablation_needle["tip_world"]
        vec = [p1[i] - p0[i] for i in range(3)]
        length = math.sqrt(sum(c * c for c in vec))
        if length < 1e-9:
            return (0.0, 1.0, 0.0)
        return tuple(c / length for c in vec)

    def _make_zone_actor(self):
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(self._zone_tpf.GetOutputPort())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.SetVisibility(False)
        actor.SetPickable(False)
        prop = actor.GetProperty()
        prop.SetColor(1.0, 0.30, 0.10)
        prop.SetOpacity(0.32)
        prop.SetAmbient(0.55)
        prop.SetDiffuse(0.45)
        prop.SetSpecular(0.10)
        return actor

    @staticmethod
    def _zone_matrix(center, axis, half_long, half_short):
        """4x4 mapping the unit sphere to an oriented ellipsoid (long axis=needle)."""
        x = list(axis)
        ref = (0.0, 0.0, 1.0) if abs(x[2]) < 0.9 else (1.0, 0.0, 0.0)
        y = [
            ref[1] * x[2] - ref[2] * x[1],
            ref[2] * x[0] - ref[0] * x[2],
            ref[0] * x[1] - ref[1] * x[0],
        ]
        ylen = math.sqrt(sum(c * c for c in y)) or 1.0
        y = [c / ylen for c in y]
        z = [
            x[1] * y[2] - x[2] * y[1],
            x[2] * y[0] - x[0] * y[2],
            x[0] * y[1] - x[1] * y[0],
        ]
        m = vtk.vtkMatrix4x4()
        m.Identity()
        for row in range(3):
            m.SetElement(row, 0, x[row] * half_long)
            m.SetElement(row, 1, y[row] * half_short)
            m.SetElement(row, 2, z[row] * half_short)
            m.SetElement(row, 3, center[row])
        return m

    def _rebuild_ablation_actors(self):
        for actor in self._ablation_actors:
            self.renderer.RemoveActor(actor)
        self._ablation_actors = []

        if self._ablation_needle is None:
            return

        needle = self._ablation_needle
        radius = self._needle_visual_radius()
        shaft = self._make_tube_actor(
            needle["entry_world"],
            needle["tip_world"],
            radius,
            (0.78, 0.82, 0.86),
        )
        active = self._make_tube_actor(
            needle["active_start_world"],
            needle["tip_world"],
            radius * 1.25,
            (1.0, 0.80, 0.10),
        )
        tip = self._make_sphere_actor(
            needle["tip_world"],
            radius * self._TIP_SPHERE_SCALE,
            (1.0, 0.18, 0.05),     # 针头/消融点：红色，半透明
            self._TIP_SPHERE_OPACITY,
        )
        entry = self._make_sphere_actor(
            needle["entry_world"],
            radius * self._ENTRY_SPHERE_SCALE,
            (0.20, 0.85, 0.30),    # 入针点：绿色，不透明
            self._ENTRY_SPHERE_OPACITY,
        )

        for actor in (shaft, active, tip, entry):
            self.renderer.AddActor(actor)
            self._ablation_actors.append(actor)

    def _make_tube_actor(self, p0, p1, radius, color):
        line = vtk.vtkLineSource()
        line.SetPoint1(*p0)
        line.SetPoint2(*p1)

        tube = vtk.vtkTubeFilter()
        tube.SetInputConnection(line.GetOutputPort())
        tube.SetRadius(radius)
        tube.SetNumberOfSides(24)
        tube.CappingOn()

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(tube.GetOutputPort())

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(*color)
        actor.GetProperty().SetAmbient(0.45)
        actor.GetProperty().SetDiffuse(0.55)
        actor.GetProperty().SetSpecular(0.35)
        actor.GetProperty().SetSpecularPower(18)
        return actor

    def _make_sphere_actor(self, center, radius, color, opacity=1.0):
        source = vtk.vtkSphereSource()
        source.SetCenter(*center)
        source.SetRadius(radius)
        # 球变大后棱面会看出来，分辨率跟着提一档
        source.SetThetaResolution(36)
        source.SetPhiResolution(22)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(source.GetOutputPort())

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetAmbient(0.65)
        prop.SetDiffuse(0.35)
        prop.SetSpecular(0.25)
        if opacity < 1.0:
            prop.SetOpacity(float(opacity))
        return actor

    def _default_ablation_needle_points(self):
        bounds = self.image.GetBounds()
        spacing = [abs(v) for v in self.image.GetSpacing()]
        center = (
            (bounds[0] + bounds[1]) * 0.5,
            (bounds[2] + bounds[3]) * 0.5,
            (bounds[4] + bounds[5]) * 0.5,
        )
        entry_world = (
            center[0],
            bounds[2] + max(spacing[1], 1.0) * 2.0,
            center[2],
        )
        tip_world = center
        return self._world_to_ijk(entry_world), self._world_to_ijk(tip_world)

    def _entry_from_tip(self, tip_ijk):
        tip_world = self._ijk_to_world(tip_ijk)
        shaft = self._ablation_params["shaft_mm"]
        entry_world = (
            tip_world[0],
            tip_world[1] - shaft,
            tip_world[2],
        )
        return self._world_to_ijk(self._clamp_world(entry_world))

    def _tip_from_entry(self, entry_ijk):
        entry_world = self._ijk_to_world(entry_ijk)
        shaft = self._ablation_params["shaft_mm"]
        tip_world = (
            entry_world[0],
            entry_world[1] + shaft,
            entry_world[2],
        )
        return self._world_to_ijk(self._clamp_world(tip_world))

    def _active_start_world(self, entry_world, tip_world):
        length = self._distance(entry_world, tip_world)
        if length <= 0:
            return entry_world
        active_len = min(self._ablation_params["active_mm"], length)
        axis = tuple((tip_world[i] - entry_world[i]) / length for i in range(3))
        return tuple(tip_world[i] - axis[i] * active_len for i in range(3))

    def _needle_visual_radius(self):
        # 针的可见半径以直径为基础，但夹在 2.0~5.0mm 的可见区间。真实
        # 直径(默认 1.6mm → 半径 0.8mm)在全身视角下只有约 4 像素粗，
        # 针道和两端标记几乎不可见；按手术规划软件的惯例放大到视觉可辨
        # 的粗细，同时仍随直径设置增减（3D 针、切片针道、入针点/消融点
        # 标记共用此半径）。
        diameter = self._ablation_params["diameter_mm"]
        return max(2.0, min(5.0, diameter * 0.75))

    def _world_to_ijk(self, world):
        origin = self.image.GetOrigin()
        spacing = self.image.GetSpacing()
        values = []
        for i in range(3):
            values.append((float(world[i]) - origin[i]) / spacing[i])
        return self._clamp_ijk(values)

    def _clamp_world(self, world):
        bounds = self.image.GetBounds()
        return (
            max(bounds[0], min(float(world[0]), bounds[1])),
            max(bounds[2], min(float(world[1]), bounds[3])),
            max(bounds[4], min(float(world[2]), bounds[5])),
        )

    def _distance(self, p0, p1):
        return math.sqrt(sum((p1[i] - p0[i]) ** 2 for i in range(3)))

    # ---- view helpers ---------------------------------------------------
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_slice_overlays()
        if self._view3d_fullscreen:
            # view3d_frame 仍由主 layout 持有；父窗口 resize/layout request
            # 可能先把它放回普通 cell，这里立即重新覆盖整个 viewer。
            self._apply_view3d_fullscreen_geometry()

    def event(self, event):
        result = super().event(event)
        if (event.type() == QtCore.QEvent.Type.LayoutRequest
                and getattr(self, "_view3d_fullscreen", False)):
            # repolish 可能在当前调用栈结束后投递 LayoutRequest；等 layout
            # 先完成，再把仍由它管理的 3D frame 提回覆盖层位置。
            self._schedule_view3d_geometry()
        return result

    def _layout_slice_overlays(self):
        # 参照临床工作站布局，三向切片列约占视图区 30%；在大屏上
        # 允许扩展到 500 px，让三张切片完整可辨，同时 3D 仍占主体。
        panel_w = min(500, max(320, int(round(self.width() * 0.30))))
        self.slice_panel.setFixedWidth(panel_w)
        self._update_camera_window_center(render=True)

    def _target_camera_window_center(self):
        """No camera offset: the slice panel is laid out beside the 3D view."""
        return 0.0, 0.0

    def _update_camera_window_center(self, render=False):
        cam = self.renderer.GetActiveCamera()
        target = self._target_camera_window_center()
        current = cam.GetWindowCenter()
        if (
            abs(current[0] - target[0]) < 0.001
            and abs(current[1] - target[1]) < 0.001
        ):
            return

        cam.SetWindowCenter(*target)
        if render and self.image is not None:
            self.renderer.ResetCameraClippingRange()
            self.render()

    def _on_vtk_end_interaction(self, _obj, _event):
        """VTK 3D 交互（旋转/平移/缩放）结束回调：转发为 Qt 信号。"""
        self.interactionEnded.emit()

    def _show_expanded_slice(self, source_view):
        if self.image is None:
            return
        # 若上一次收回的分帧渲染还没跑完，先停掉并恢复控件状态，
        # 避免放大期间残留的定时器对着隐藏的 3D/切片做无用渲染。
        self._view3d_present_timer.stop()
        self._view3d_refine_timer.stop()
        self._view3d_silent_refine_timer.stop()
        self._slice_resume_timer.stop()
        self._slice_overlay_release_timer.stop()
        self._pending_slice_resumes = []
        self.vtk_widget.setUpdatesEnabled(True)
        self.slice_panel.setUpdatesEnabled(True)
        for view in self.slice_panel.views:
            view.vtk_widget.setUpdatesEnabled(True)
        # 隐藏 3D/切片前先抓当前画面缓存下来，收回时用它们做过渡帧，
        # 避免收回瞬间出现黑框、再逐个补画的跳变。
        self._view3d_transition_fade.stop()
        if not (self._view3d_lazy_pending
                and not self._view3d_transition_image.isNull()):
            self._view3d_transition_image = self._grab_presented_widget_image(
                self.vtk_widget)
        self._view3d_lazy_pending = False
        self._view3d_lazy_restore_timer.stop()
        self._finish_view3d_resize_render(render=False)
        self._cache_slice_transition_images()
        # Build the overlay's scene while it is still hidden. After AI
        # segmentation the mask-pipeline rebuild takes a noticeable moment;
        # swapping visibility first left a half-built overlay (stale frame
        # from the previous expansion) flashing on screen until the rebuild
        # finished.
        self.expanded_slice.show_source(source_view)
        self.setUpdatesEnabled(False)
        try:
            # Hide the whole 3D frame (not just the GL widget) so the overlay
            # gets the full viewer width without painting intermediate sizes.
            self.view3d_frame.setVisible(False)
            self._view3d_slot.setVisible(False)
            self.slice_panel.setVisible(False)
            self._update_camera_window_center(render=False)
            self.expanded_slice.setVisible(True)
            self._layout_slice_overlays()
            self._main_layout.activate()
            if self.isVisible():
                self.expanded_slice.initialize()
            # 只更新最终尺寸对应的相机；重新启用 Qt 绘制后由一次 paint
            # 完成真正渲染，避免这里 Render 后 paint 又 Render 一遍。
            self.expanded_slice.slice_view._fit_camera(
                force=True, render=False)
        finally:
            self.setUpdatesEnabled(True)
        self.update()

    def _on_expanded_slice_collapsed(self):
        if self.image is None:
            return
        # 与 3D 全屏收回同一套机制：先冻结所有 GL 控件再恢复布局，
        # 放大时缓存的 3D/切片画面作为过渡帧盖在原位（不露黑框），
        # 之后 3D 渲染一帧低成本画面，三个切片按事件循环逐个恢复，
        # 过渡帧最后统一淡出。否则收回瞬间 4 次 VTK 渲染同帧执行，
        # 全压在 UI 线程上，造成非常明显的卡顿。
        self._view3d_present_timer.stop()
        self._view3d_refine_timer.stop()
        self._view3d_silent_refine_timer.stop()
        self._slice_resume_timer.stop()
        self._slice_overlay_release_timer.stop()
        self._slice_transition_fade.stop()
        self._complete_slice_transition_overlays()
        self._pending_slice_resumes = []
        self._show_view3d_transition_overlay(grab=False)
        # Keep the cached frame above the VTK widget until a settled-quality
        # render is complete; the user never sees a coarse intermediate frame.
        self._finish_view3d_resize_render(render=False)
        self.vtk_widget.setUpdatesEnabled(False)
        self.slice_panel.setUpdatesEnabled(False)
        for view in self.slice_panel.views:
            view.vtk_widget.setUpdatesEnabled(False)
        self.setUpdatesEnabled(False)
        try:
            self.expanded_slice.setVisible(False)
            self._view3d_slot.setVisible(True)
            self.view3d_frame.setVisible(True)
            self.slice_panel.setVisible(True)
            self._layout_slice_overlays()
            self._update_camera_window_center(render=False)
            self._main_layout.activate()
            self._apply_view3d_fullscreen_geometry()
            self._show_slice_transition_overlays()
        finally:
            self.setUpdatesEnabled(True)
        self._sync_view3d_transition_overlay()
        self.update()
        self._view3d_present_timer.start()

    @staticmethod
    def _anatomical_camera_axes(info):
        """把 DICOM 患者前方/头侧方向换算到轴对齐的体数据坐标。"""
        fallback = ((0.0, -1.0, 0.0), (0.0, 0.0, 1.0))
        try:
            orientation = tuple(float(value) for value in (
                info or {}).get("image_orientation_patient", ()))
        except (TypeError, ValueError):
            return fallback
        if len(orientation) != 6:
            return fallback

        def normalized(vector):
            length = math.sqrt(sum(value * value for value in vector))
            if length <= 1.0e-8:
                return None
            return tuple(value / length for value in vector)

        axis_i = normalized(orientation[:3])
        raw_j = orientation[3:]
        if axis_i is None:
            return fallback
        # 对扫描仪写入的近似方向余弦做一次正交化，避免舍入误差让 ViewUp
        # 带入细小的视线分量，长期旋转后出现画面倾斜。
        projection = sum(raw_j[i] * axis_i[i] for i in range(3))
        axis_j = normalized(tuple(
            raw_j[i] - projection * axis_i[i] for i in range(3)))
        if axis_j is None:
            return fallback
        axis_k = normalized((
            axis_i[1] * axis_j[2] - axis_i[2] * axis_j[1],
            axis_i[2] * axis_j[0] - axis_i[0] * axis_j[2],
            axis_i[0] * axis_j[1] - axis_i[1] * axis_j[0],
        ))
        if axis_k is None:
            return fallback

        basis = (axis_i, axis_j, axis_k)

        def patient_to_volume(vector):
            return tuple(sum(vector[i] * axis[i] for i in range(3))
                         for axis in basis)

        # DICOM 使用 LPS 患者坐标：-Y 是前方，+Z 是头侧。
        camera_position = normalized(patient_to_volume((0.0, -1.0, 0.0)))
        view_up = normalized(patient_to_volume((0.0, 0.0, 1.0)))
        if camera_position is None or view_up is None:
            return fallback
        # ViewUp 必须严格位于成像平面内。
        along_view = sum(
            view_up[i] * camera_position[i] for i in range(3))
        view_up = normalized(tuple(
            view_up[i] - along_view * camera_position[i] for i in range(3)))
        if view_up is None:
            return fallback
        return camera_position, view_up

    def reset_view(self, render=True):
        """重置相机到固定的前视视角（anterior view）。
        
        绝对相机姿态：
          - 视角上方 = 病人头侧（Z 轴正方向）
          - 视线方向 = 从前向后（Y 轴正方向）
          - 相机位置 = 病人前方
        
        ResetCamera 仅调整距离以完整框住体数据，保持方向不变。
        每次按下都会得到完全相同的视角。
        修改默认视角方向：修改 SetViewUp 和 SetPosition。
        """
        camera_position, view_up = self._anatomical_camera_axes(self.info)
        cam = self.renderer.GetActiveCamera()
        cam.SetViewUp(*view_up)        # patient superior (head) points up
        cam.SetFocalPoint(0, 0, 0)
        cam.SetPosition(*camera_position)
        if self.image is not None:
            self.renderer.ResetCamera(self.image.GetBounds())
        else:
            self.renderer.ResetCamera()
        if cam.GetParallelProjection() and self.image is not None:
            bounds = self.image.GetBounds()
            projected_width = max(0.001, float(bounds[1] - bounds[0]))
            projected_height = max(0.001, float(bounds[5] - bounds[4]))
            viewport_width = max(1, self.vtk_widget.width())
            viewport_height = max(1, self.vtk_widget.height())
            viewport_aspect = viewport_width / float(viewport_height)
            # ParallelScale is half of the visible vertical world span.  Fit
            # whichever projected axis is limiting, then add a small margin so
            # no edge is clipped by borders or floating-point rounding.
            fit_half_height = max(
                projected_height * 0.5,
                projected_width * 0.5 / viewport_aspect,
            )
            cam.SetParallelScale(
                fit_half_height * self.DEFAULT_VIEW_MARGIN)
        self._update_camera_window_center()
        self.renderer.ResetCameraClippingRange()
        self._widen_clipping_for_crosshair()
        if render:
            self.render()

    def render(self):
        self._activate_lazy_view3d()
        # 放大切片期间 3D frame 是隐藏的；此时十字联动/透明度调节等触发的
        # 体渲染完全不可见，纯属浪费（大体数据一次要几十到上百毫秒）。
        # 收回时 _present_view3d_after_resize 总会补一帧最新画面。
        if not self.vtk_widget.isVisible():
            return
        self.render_window.Render()

    def render_all_views(self):
        """刷新 3D 与所有可见切片的 VTK 帧（抓屏/模糊前调用）。"""
        for _widget, render_window in self._iter_visible_gl_targets():
            try:
                render_window.Render()
            except Exception:
                pass

    def _iter_visible_gl_targets(self):
        """可见的 VTK/OpenGL 控件及其渲染窗口（退出模糊抓屏用）。"""
        if self.vtk_widget.isVisible():
            yield self.vtk_widget, self.render_window
        if self.slice_panel.isVisible():
            for view in self.slice_panel.views:
                if view.isVisible() and view.vtk_widget.isVisible():
                    yield view.vtk_widget, view.render_window
        if self.expanded_slice.isVisible():
            sv = getattr(self.expanded_slice, "slice_view", None)
            if sv is not None and sv.vtk_widget.isVisible():
                yield sv.vtk_widget, sv.render_window

    @staticmethod
    def _image_mostly_black(image, threshold=12.0):
        if image is None or image.isNull():
            return True
        probe = image.scaled(
            12, 12,
            QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
            QtCore.Qt.TransformationMode.FastTransformation,
        )
        total = 0.0
        count = max(1, probe.width() * probe.height())
        for y in range(probe.height()):
            for x in range(probe.width()):
                c = QtGui.QColor(probe.pixel(x, y))
                total += (c.red() + c.green() + c.blue()) / 3.0
        return (total / count) < threshold

    @staticmethod
    def _render_window_to_qimage(render_window, front_buffer=False, rerender=True):
        """从 VTK 帧缓冲读出画面（与保存截图同一路径）。

        rerender=False 时不触发 Render，避免退出确认抓屏时屏幕闪一下。
        """
        if render_window is None:
            return QtGui.QImage()
        try:
            if rerender:
                render_window.Render()
            w2i = vtk.vtkWindowToImageFilter()
            w2i.SetInput(render_window)
            w2i.SetScale(1)
            if rerender:
                w2i.ShouldRerenderOn()
            else:
                w2i.ShouldRerenderOff()
            if front_buffer:
                w2i.ReadFrontBufferOn()
            else:
                w2i.ReadFrontBufferOff()
            w2i.Update()
            data = w2i.GetOutput()
            width, height, _ = data.GetDimensions()
            if width <= 0 or height <= 0:
                return QtGui.QImage()
            ncomp = int(data.GetNumberOfScalarComponents())
            if ncomp < 3:
                return QtGui.QImage()
            import numpy as np
            from vtk.util import numpy_support
            arr = numpy_support.vtk_to_numpy(data.GetPointData().GetScalars())
            arr = arr.reshape(height, width, ncomp)
            arr = np.ascontiguousarray(np.flipud(arr))
            if ncomp >= 4:
                qimg = QtGui.QImage(
                    arr.data, width, height, 4 * width,
                    QtGui.QImage.Format.Format_RGBA8888,
                ).copy()
            else:
                qimg = QtGui.QImage(
                    arr.data, width, height, 3 * width,
                    QtGui.QImage.Format.Format_RGB888,
                ).copy()
            return qimg
        except Exception:
            return QtGui.QImage()

    def capture_gl_layers(self, host_window):
        """抓取各 VTK 视口画面，返回 [(逻辑坐标 QRect, QImage), ...]。

        优先读已有前缓冲且不重新 Render，减少退出确认时的闪屏。
        """
        layers = []
        if host_window is None:
            return layers
        for widget, render_window in self._iter_visible_gl_targets():
            gl = self._render_window_to_qimage(
                render_window, front_buffer=True, rerender=False)
            if gl.isNull():
                gl = self._render_window_to_qimage(
                    render_window, front_buffer=False, rerender=False)
            if gl.isNull():
                gl = self._render_window_to_qimage(
                    render_window, front_buffer=False, rerender=True)
            if gl.isNull():
                continue
            origin = widget.mapTo(host_window, QtCore.QPoint(0, 0))
            rect = QtCore.QRect(origin, widget.size())
            if rect.width() > 0 and rect.height() > 0:
                layers.append((rect, gl))
        return layers

    def _make_body_axes_actor(self):
        """人体坐标轴造型：X/Y/Z 三向箭头，末端小箭头用于判断观察方位。

        配色与参考坐标系一致：X 红 / Y 黄 / Z 蓝（Y 用黄，避免与可移动
        高亮的绿色撞色）。左下角方向标记和消融针姿态 HUD 共用这一套造型，
        两处必须长得一样、指向也一样。
        """
        axes = vtk.vtkAxesActor()
        axes.SetTotalLength(1, 1, 1)
        axes.SetShaftTypeToCylinder()
        axes.SetCylinderRadius(0.03)
        axes.SetNormalizedShaftLength(0.78, 0.78, 0.78)
        axes.SetNormalizedTipLength(0.18, 0.18, 0.18)
        axes.SetSphereRadius(0.08)
        axes.SetSphereResolution(12)
        shafts = (axes.GetXAxisShaftProperty(),
                  axes.GetYAxisShaftProperty(),
                  axes.GetZAxisShaftProperty())
        tips = (axes.GetXAxisTipProperty(),
                axes.GetYAxisTipProperty(),
                axes.GetZAxisTipProperty())
        captions = (axes.GetXAxisCaptionActor2D(),
                    axes.GetYAxisCaptionActor2D(),
                    axes.GetZAxisCaptionActor2D())
        for axis in range(3):
            color = CROSSHAIR_AXIS_COLORS[axis]
            shafts[axis].SetColor(*color)
            shafts[axis].SetAmbient(0.9)
            shafts[axis].SetDiffuse(0.35)
            tips[axis].SetColor(*color)
            captions[axis].GetCaptionTextProperty().SetColor(*color)
        return axes

    def _add_orientation_marker(self):
        """左下角方向标记：X/Y/Z 三向箭头坐标轴，随主相机同步旋转。"""
        axes = self._make_body_axes_actor()
        if self._orientation is not None:
            # 重新加载数据时复用已有 widget，避免叠加出多个标记
            self._orientation.SetOrientationMarker(axes)
            return
        marker = vtk.vtkOrientationMarkerWidget()
        marker.SetOrientationMarker(axes)
        marker.SetInteractor(self.interactor)
        marker.SetViewport(0.0, 0.0, 0.18, 0.22)
        marker.EnabledOn()
        marker.InteractiveOff()
        self._orientation = marker  # keep a reference alive

    # ============================================================
    # 导出功能
    # ============================================================
    def save_screenshot(self, path):
        """保存当前 3D 渲染窗口截图（PNG 格式，2× 超采样抗锯齿）。"""
        w2i = vtk.vtkWindowToImageFilter()
        w2i.SetInput(self.render_window)
        w2i.SetScale(2)  # 2× 超采样
        w2i.ReadFrontBufferOff()
        w2i.Update()
        writer = vtk.vtkPNGWriter()
        writer.SetFileName(path)
        writer.SetInputConnection(w2i.GetOutputPort())
        writer.Write()

    def export_mesh(self, path):
        """导出三维网格模型（STL/OBJ/PLY）。
        
        流程：
          1. 计算等值面阈值（取当前勾选的最致密组织 iso 值）
          2. FlyingEdges 算法提取等值面
          3. WindowedSinc 平滑处理（15次迭代）
          4. 根据文件扩展名选择写入器并保存
        
        参数：
          path — 保存路径（.stl / .obj / .ply）
        
        返回：
          使用的等值面阈值
        
        异常：
          RuntimeError — 未加载体数据
        """
        if self.image is None:
            raise RuntimeError("No volume loaded.")
        iso = presets.composite_threshold(
            self._tissue_states, self.info["modality"], self.info["scalar_range"])

        surface = vtk.vtkFlyingEdges3D()
        surface.SetInputData(self.image)
        surface.SetValue(0, iso)
        surface.ComputeNormalsOn()

        smooth = vtk.vtkWindowedSincPolyDataFilter()
        smooth.SetInputConnection(surface.GetOutputPort())
        smooth.SetNumberOfIterations(15)
        smooth.SetPassBand(0.1)
        smooth.NonManifoldSmoothingOn()
        smooth.NormalizeCoordinatesOn()
        smooth.Update()

        lower = path.lower()
        if lower.endswith(".obj"):
            writer = vtk.vtkOBJWriter()
        elif lower.endswith(".ply"):
            writer = vtk.vtkPLYWriter()
        else:
            writer = vtk.vtkSTLWriter()
            writer.SetFileTypeToBinary()
        writer.SetFileName(path)
        writer.SetInputConnection(smooth.GetOutputPort())
        writer.Write()
        return iso


class SliceSlider(QtWidgets.QSlider):
    """A slider whose groove click jumps directly to the requested slice."""

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setObjectName("SliceNavSlider")
        self.setTracking(True)

    def _value_from_position(self, position):
        opt = QtWidgets.QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QtWidgets.QStyle.ComplexControl.CC_Slider,
            opt,
            QtWidgets.QStyle.SubControl.SC_SliderGroove,
            self,
        )
        if self.orientation() == QtCore.Qt.Orientation.Horizontal:
            span = max(1, groove.width())
            pos = int(position.x() - groove.x())
        else:
            span = max(1, groove.height())
            pos = int(groove.bottom() - position.y())
        return QtWidgets.QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), pos, span, self.invertedAppearance()
        )

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.setValue(self._value_from_position(event.position()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self.setValue(self._value_from_position(event.position()))
            event.accept()
            return
        super().mouseMoveEvent(event)


_SLICE_CURSOR = None


def _slice_crosshair_cursor():
    """构建切片视图悬停时使用的"反色"十字光标。

    用单色位图 + 掩码做反色(XOR)光标：十字落在白色底上显示为黑色，落在黑色底上
    显示为白色，始终与底色相反、清晰可见。
    位图(B)/掩码(M)组合：十字线处 B=1,M=0 → 反色(Windows 上为屏幕取反)，
    其余 B=0,M=0 → 透明。光标只构建一次并缓存复用。
    """
    global _SLICE_CURSOR
    if _SLICE_CURSOR is None:
        size = 25
        center = size // 2
        bitmap = QtGui.QBitmap(size, size)
        bitmap.fill(QtCore.Qt.GlobalColor.color0)   # 0：非十字区域
        painter = QtGui.QPainter(bitmap)
        painter.setPen(QtGui.QPen(QtCore.Qt.GlobalColor.color1, 1))  # 1：十字线
        painter.drawLine(center, 0, center, size - 1)
        painter.drawLine(0, center, size - 1, center)
        painter.end()
        mask = QtGui.QBitmap(size, size)
        mask.fill(QtCore.Qt.GlobalColor.color0)     # 掩码全 0 → 十字处反色、其余透明
        _SLICE_CURSOR = QtGui.QCursor(bitmap, mask, center, center)
    return _SLICE_CURSOR


class SliceScaleRuler:
    """在 VTK 前景层绘制物理比例尺、四边解剖方位标记，以及左下角坐标/灰度读数。"""

    # 与 SliceView._reset_slice_camera 的朝向一致（LPS 习惯）：
    #   轴状位：上A下P 左R右L
    #   冠状位：上S下I 左R右L
    #   矢状位：上S下I 左A右P
    ORIENTATION_MARKERS = {
        "axial": {"top": "A", "bottom": "P", "left": "R", "right": "L"},
        "coronal": {"top": "S", "bottom": "I", "left": "R", "right": "L"},
        "sagittal": {"top": "S", "bottom": "I", "left": "A", "right": "P"},
    }

    def __init__(self, slice_view):
        self._slice_view = slice_view
        self._points = vtk.vtkPoints()
        self._lines = vtk.vtkCellArray()
        self._polydata = vtk.vtkPolyData()
        self._polydata.SetPoints(self._points)
        self._polydata.SetLines(self._lines)

        coordinate = vtk.vtkCoordinate()
        coordinate.SetCoordinateSystemToDisplay()
        self._coordinate = coordinate
        self._mapper = vtk.vtkPolyDataMapper2D()
        self._mapper.SetInputData(self._polydata)
        self._mapper.SetTransformCoordinate(coordinate)

        self._shadow_actor = self._make_line_actor((0.0, 0.0, 0.0), 4.0, 0.82)
        self._line_actor = self._make_line_actor((0.96, 0.97, 0.98), 1.6, 1.0)
        self._horizontal_text = self._make_text_actor(12)
        self._vertical_text = self._make_text_actor(12)
        self._marker_top = self._make_text_actor(18)
        self._marker_bottom = self._make_text_actor(18)
        self._marker_left = self._make_text_actor(18)
        self._marker_right = self._make_text_actor(18)
        self._probe_text = self._make_text_actor(13)
        self._probe_text.GetTextProperty().SetColor(0.72, 0.74, 0.78)
        self._probe_text.GetTextProperty().SetJustificationToLeft()
        self._probe_text.GetTextProperty().SetVerticalJustificationToBottom()
        self._horizontal_text.GetTextProperty().SetJustificationToRight()
        self._vertical_text.GetTextProperty().SetJustificationToRight()
        self._marker_top.GetTextProperty().SetJustificationToCentered()
        self._marker_top.GetTextProperty().SetVerticalJustificationToTop()
        self._marker_bottom.GetTextProperty().SetJustificationToCentered()
        self._marker_bottom.GetTextProperty().SetVerticalJustificationToBottom()
        self._marker_left.GetTextProperty().SetJustificationToLeft()
        self._marker_left.GetTextProperty().SetVerticalJustificationToCentered()
        self._marker_right.GetTextProperty().SetJustificationToRight()
        self._marker_right.GetTextProperty().SetVerticalJustificationToCentered()

        renderer = slice_view.image_viewer.GetRenderer()
        renderer.AddActor2D(self._shadow_actor)
        renderer.AddActor2D(self._line_actor)
        renderer.AddActor2D(self._horizontal_text)
        renderer.AddActor2D(self._vertical_text)
        for actor in (
                self._marker_top, self._marker_bottom,
                self._marker_left, self._marker_right,
                self._probe_text):
            renderer.AddActor2D(actor)
        self.set_visible(False)

    def _make_line_actor(self, color, width, opacity):
        actor = vtk.vtkActor2D()
        actor.SetMapper(self._mapper)
        actor.SetPickable(False)
        prop = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetLineWidth(width)
        prop.SetOpacity(opacity)
        prop.SetDisplayLocationToForeground()
        return actor

    @staticmethod
    def _make_text_actor(font_size):
        actor = vtk.vtkTextActor()
        actor.SetPickable(False)
        actor.SetTextScaleModeToNone()
        prop = actor.GetTextProperty()
        prop.SetColor(0.96, 0.97, 0.98)
        prop.SetFontFamilyToArial()
        prop.SetFontSize(font_size)
        prop.BoldOn()
        prop.ShadowOn()
        prop.SetShadowOffset(1, -1)
        prop.SetVerticalJustificationToBottom()
        return actor

    @staticmethod
    def _nice_length(target_mm, maximum_mm):
        """返回最接近目标、且不会越出视图的友好刻度。"""
        target_mm = max(1e-6, float(target_mm))
        exponent = math.floor(math.log10(target_mm))
        candidates = []
        for power in range(exponent - 1, exponent + 3):
            base = 10.0 ** power
            candidates.extend((1.0 * base, 2.0 * base, 3.0 * base, 5.0 * base))
        valid = [value for value in candidates if value <= maximum_mm * 1.001]
        if not valid:
            return min(candidates)
        return min(valid, key=lambda value: abs(value - target_mm))

    @staticmethod
    def _format_length(length_mm):
        if length_mm >= 10.0:
            return "%g cm" % (length_mm / 10.0)
        return "%g mm" % length_mm

    def _scale_values(self, width, height):
        view = self._slice_view
        if view.image is None or height <= 0:
            return None
        camera = view.image_viewer.GetRenderer().GetActiveCamera()
        parallel_scale = float(camera.GetParallelScale())
        if parallel_scale <= 0:
            return None
        mm_per_pixel = (2.0 * parallel_scale) / max(1.0, float(height))
        # 比上一版更长：目标占短边约 68%，大视图中最多 420 px。
        target_pixels = max(
            90.0,
            min(420.0, float(width) * 0.68, float(height) * 0.68),
        )
        maximum_pixels = max(40.0, min(float(width), float(height)) - 50.0)
        length_mm = self._nice_length(
            mm_per_pixel * target_pixels,
            mm_per_pixel * maximum_pixels,
        )
        pixels = length_mm / mm_per_pixel
        return length_mm, pixels

    def _add_line(self, x0, y0, x1, y1):
        first = self._points.InsertNextPoint(float(x0), float(y0), 0.0)
        second = self._points.InsertNextPoint(float(x1), float(y1), 0.0)
        line = vtk.vtkLine()
        line.GetPointIds().SetId(0, first)
        line.GetPointIds().SetId(1, second)
        self._lines.InsertNextCell(line)

    def _add_bar(self, horizontal, start, end, fixed, subdivisions=10):
        if horizontal:
            self._add_line(start, fixed, end, fixed)
            span = end - start
            for index in range(subdivisions + 1):
                x = start + span * index / subdivisions
                tick = 7.0 if index in (0, subdivisions) else (
                    5.0 if index == subdivisions // 2 else 3.5)
                self._add_line(x, fixed, x, fixed + tick)
        else:
            self._add_line(fixed, start, fixed, end)
            span = end - start
            for index in range(subdivisions + 1):
                y = start + span * index / subdivisions
                tick = 7.0 if index in (0, subdivisions) else (
                    5.0 if index == subdivisions // 2 else 3.5)
                self._add_line(fixed, y, fixed - tick, y)

    def _marker_labels(self):
        orientation = getattr(self._slice_view, "orientation", "axial")
        return self.ORIENTATION_MARKERS.get(
            orientation, self.ORIENTATION_MARKERS["axial"])

    def set_visible(self, visible):
        visible = bool(visible)
        for actor in (
                self._shadow_actor, self._line_actor,
                self._horizontal_text, self._vertical_text,
                self._marker_top, self._marker_bottom,
                self._marker_left, self._marker_right):
            actor.SetVisibility(visible)

    def update_probe(self):
        """左下角：(i, j, k) - 灰度/HU。

        鼠标在切片图内时读取光标所在体素中心的值；光标移出切片图（或落在
        图像范围之外）后回落到参考坐标轴中心。
        """
        view = self._slice_view
        if view.image is None:
            self._probe_text.SetVisibility(False)
            return
        ijk = view.hover_probe_ijk()
        if ijk is None and view._crosshair:
            ijk = view._crosshair.get("ijk")
        if ijk is None:
            self._probe_text.SetVisibility(False)
            return
        width, height = view.render_window.GetSize()
        if width < 40 or height < 40:
            self._probe_text.SetVisibility(False)
            return
        coords = tuple(int(round(float(v))) for v in ijk[:3])
        value = self._sample_scalar(view.image, coords)
        if value is None:
            text = "(%d, %d, %d)" % coords
        else:
            if abs(value - round(value)) < 1e-3:
                value_text = "%d" % int(round(value))
            else:
                value_text = "%.1f" % float(value)
            text = "(%d, %d, %d) - %s" % (coords[0], coords[1], coords[2],
                                         value_text)
        self._probe_text.SetInput(text)
        self._probe_text.SetDisplayPosition(10, 12)
        self._probe_text.SetVisibility(True)

    @staticmethod
    def _sample_scalar(image, ijk):
        dims = image.GetDimensions()
        i = max(0, min(int(dims[0]) - 1, int(ijk[0])))
        j = max(0, min(int(dims[1]) - 1, int(ijk[1])))
        k = max(0, min(int(dims[2]) - 1, int(ijk[2])))
        try:
            return float(image.GetScalarComponentAsDouble(i, j, k, 0))
        except Exception:
            return None

    def update(self):
        width, height = self._slice_view.render_window.GetSize()
        values = self._scale_values(width, height)
        if values is None or width < 90 or height < 90:
            self.set_visible(False)
            self.update_probe()
            return
        length_mm, pixels = values
        label = self._format_length(length_mm)
        width = float(width)
        height = float(height)

        # VTK 显示坐标的原点在左下角，因此底尺直接放在 y=12。
        horizontal_y = 12.0
        horizontal_x0 = (width - pixels) / 2.0
        horizontal_x1 = horizontal_x0 + pixels
        vertical_x = width - 12.0
        vertical_y0 = (height - pixels) / 2.0
        vertical_y1 = vertical_y0 + pixels

        self._points.Reset()
        self._lines.Reset()
        self._add_bar(True, horizontal_x0, horizontal_x1, horizontal_y)
        self._add_bar(False, vertical_y0, vertical_y1, vertical_x)
        self._points.Modified()
        self._lines.Modified()
        self._polydata.Modified()

        self._horizontal_text.SetInput(label)
        self._horizontal_text.SetDisplayPosition(
            int(round(horizontal_x1)), 25)
        self._vertical_text.SetInput(label)
        self._vertical_text.SetDisplayPosition(
            int(round(vertical_x - 8.0)), int(round(vertical_y1 + 8.0)))

        markers = self._marker_labels()
        self._marker_top.SetInput(markers["top"])
        self._marker_bottom.SetInput(markers["bottom"])
        self._marker_left.SetInput(markers["left"])
        self._marker_right.SetInput(markers["right"])
        # 四边中点：避开底/右侧比例尺
        self._marker_top.SetDisplayPosition(
            int(round(width / 2.0)), int(round(height - 10.0)))
        self._marker_bottom.SetDisplayPosition(
            int(round(width / 2.0)), 38)
        self._marker_left.SetDisplayPosition(
            10, int(round(height / 2.0)))
        self._marker_right.SetDisplayPosition(
            int(round(vertical_x - 16.0)), int(round(height / 2.0)))
        self.set_visible(True)
        self.update_probe()


class SliceView(QtWidgets.QFrame):
    """单个二维正交切片视图，底层使用 vtkImageViewer2。
    
    支持三种方向：
      - axial    (轴状位/横断面)：SetSliceOrientationToXY
      - coronal  (冠状位/额面)：SetSliceOrientationToXZ
      - sagittal (矢状位)：SetSliceOrientationToYZ
    
    交互功能：
      - 鼠标进入：显示与黑色底色相反的白色十字光标
      - 滚轮：切换切片层
      - 左键拖动：缩放（按在参考十字中心上则是拖动十字）
      - 中键拖动：调整窗宽窗位
      - 右键：窗宽窗位菜单（典型档位 / 默认值 / 两条微调滑块）
      - 双击：放大到 ExpandedSliceOverlay
    
    信号：
      doubleClicked       — 双击事件
      sliceChanged(int)   — 切片索引改变
      sliceNavigated      — 用户滚轮/滑块主动换层（驱动 3D 坐标轴跟随）
      contextRequested    — 右键菜单请求（全局坐标, 局部坐标）
      windowLevelChanged  — 窗宽窗位改变
    """

    doubleClicked = QtCore.Signal()
    sliceChanged = QtCore.Signal(int)
    sliceNavigated = QtCore.Signal()
    contextRequested = QtCore.Signal(object, object)
    windowLevelChanged = QtCore.Signal(float, float)
    crosshairMoved = QtCore.Signal(object)   # 拖动参考十字时发出新的 ijk
    activated = QtCore.Signal()              # 单击本视图时发出（用于选中高亮）

    ORIENTATION_METHODS = {
        "axial": "SetSliceOrientationToXY",
        "coronal": "SetSliceOrientationToXZ",
        "sagittal": "SetSliceOrientationToYZ",
    }

    def __init__(self, title, orientation, parent=None, show_header=True):
        super().__init__(parent)
        if orientation not in self.ORIENTATION_METHODS:
            raise ValueError("Unknown slice orientation: %s" % orientation)

        self._title = title
        self.orientation = orientation
        self.image = None
        self.info = None
        self._min_slice = 0
        self._max_slice = 0
        self._slice = 0
        self._points = []
        self._point_actors = []
        self._needle = None
        self._needle_actors = []
        # 入针点/消融点在本切片上的截面标记（由 VolumeViewer 推送）
        self._planning_markers = []
        self._planning_marker_actors = []
        self._zone_poly = None
        self._zone_actors = []
        self._segmentations = []
        self._segmentation_slice_items = []
        self._zone_plane = vtk.vtkPlane()
        self._zone_cutter = vtk.vtkCutter()
        self._zone_cutter.SetCutFunction(self._zone_plane)
        self._zone_stripper = vtk.vtkStripper()
        self._zone_stripper.SetInputConnection(self._zone_cutter.GetOutputPort())
        self._zone_stripper.JoinContiguousSegmentsOn()
        self._zone_tri = vtk.vtkContourTriangulator()
        self._zone_tri.SetInputConnection(self._zone_stripper.GetOutputPort())
        self._wl_dragging = False
        self._wl_drag_start_pos = QtCore.QPointF()
        self._wl_drag_start_window = 1.0
        self._wl_drag_start_level = 0.0
        self._default_window_level = (1400.0, 200.0)   # set_volume 时按数据重算
        self._zoom_dragging = False
        self._zoom_drag_start_pos = QtCore.QPointF()
        self._zoom_drag_start_scale = 1.0
        # 参考坐标系（十字光标）状态
        self._crosshair = None              # payload: {ijk, world, visible}
        self._crosshair_actors = []
        self._crosshair_line_sources = []   # 与 _crosshair_actors 一一对应的 vtkLineSource
        self._crosshair_dragging = False
        self._crosshair_grab_offset = None  # 抓取点与中心的世界偏移(拖动保持相对,不跳)
        # 左下角读数的取值点：光标在图内时的控件坐标，移出后置 None
        self._hover_pos = None

        self.setObjectName("SliceView")
        self.setProperty("active", False)   # 选中高亮态（金色边框）

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        if show_header:
            title_label = QtWidgets.QLabel(title, objectName="SliceTitle")
            title_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(title_label)

        self.vtk_widget = QVTKRenderWindowInteractor(self)
        self.vtk_widget.setMinimumSize(120, 120)
        # 鼠标进入切片图时显示与黑色底色相反的白色十字光标(eventFilter 里在
        # Enter 事件时再断言一次，避免被 VTK 重置)。
        self.vtk_widget.setCursor(_slice_crosshair_cursor())
        self.vtk_widget.installEventFilter(self)
        layout.addWidget(self.vtk_widget, 1)

        self.render_window = self.vtk_widget.GetRenderWindow()
        self.interactor = self.render_window.GetInteractor()
        self.image_viewer = vtk.vtkImageViewer2()
        self.image_viewer.SetRenderWindow(self.render_window)
        self.image_viewer.SetupInteractor(self.interactor)
        self.interactor.SetInteractorStyle(vtk.vtkInteractorStyleImage())
        self.image_viewer.GetRenderer().SetBackground(0.0, 0.0, 0.0)
        # 面板在未导入数据时也会显示并触发绘制；窗宽窗位滤镜此时没有输入，
        # 渲染会报管线错误，所以先隐藏图像 actor，set_volume 时再恢复。
        self.image_viewer.GetImageActor().SetVisibility(False)
        self.scale_ruler = SliceScaleRuler(self)
        self._last_fit_size = None
        self._fit_timer = QtCore.QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(24)
        self._fit_timer.timeout.connect(self._fit_camera)

    def _update_scale_ruler(self):
        if not hasattr(self, "scale_ruler"):
            return
        self.scale_ruler.update()

    def _update_probe_readout(self):
        if not hasattr(self, "scale_ruler"):
            return
        self.scale_ruler.update_probe()

    def title(self):
        return self._title

    def initialize(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.interactor.Initialize()

    def set_volume(self, image, info, render=True):
        self.image = image
        self.info = info
        self._zone_poly = None
        self.image_viewer.SetInputData(image)
        self.image_viewer.GetImageActor().SetVisibility(True)
        getattr(self.image_viewer, self.ORIENTATION_METHODS[self.orientation])()
        self._configure_window_level()
        self._min_slice = int(self.image_viewer.GetSliceMin())
        self._max_slice = int(self.image_viewer.GetSliceMax())
        self.set_slice((self._min_slice + self._max_slice) // 2, render=False)
        self._reset_slice_camera()
        self._rebuild_segmentation_slice_actors()
        self._refresh_point_actors()
        self._refresh_needle_actors()
        self._refresh_planning_marker_actors()
        self._refresh_zone_actors()
        self._refresh_crosshair_actors()
        self._update_scale_ruler()
        if render:
            self.image_viewer.Render()
            self._last_fit_size = tuple(self.render_window.GetSize())
        self._emit_window_level_changed()

    def clear_volume(self):
        """释放隐藏 SliceView 对体数据和分割 mask 的强引用。"""
        self._fit_timer.stop()
        self.image = None
        self.info = None
        self._points = []
        self._needle = None
        self._planning_markers = []
        self._zone_poly = None
        self._segmentations = []
        self._crosshair = None
        self._last_fit_size = None
        self._rebuild_segmentation_slice_actors()
        self._refresh_point_actors()
        self._refresh_needle_actors()
        self._refresh_planning_marker_actors()
        self._refresh_zone_actors()
        self._refresh_crosshair_actors()
        self.image_viewer.GetImageActor().SetVisibility(False)
        # vtkImageViewer2 没有跨版本一致的 RemoveInputData API；切到一个
        # 空 image 即可让旧 vtkImageData 的引用计数归零。
        self.image_viewer.SetInputData(vtk.vtkImageData())
        self._update_scale_ruler()

    def _configure_window_level(self):
        lo, hi = self.info.get("scalar_range", (0.0, 255.0))
        if self.info.get("modality") == "CT":
            window, level = 1400.0, 200.0
        else:
            window = max(1.0, float(hi) - float(lo))
            level = (float(lo) + float(hi)) / 2.0
        # 记下本套数据的默认档，右键菜单的"默认窗宽窗位"据此恢复。
        self._default_window_level = (window, level)
        self.image_viewer.SetColorWindow(window)
        self.image_viewer.SetColorLevel(level)

    def default_window_level(self):
        return self._default_window_level

    def reset_window_level(self):
        """恢复到本套数据的默认窗宽/窗位。"""
        if self.image is None:
            return
        window, level = self._default_window_level
        self.set_window_level(window=window, level=level)

    def _window_level_matches(self, window, level):
        current_window, current_level = self.window_level()
        return (abs(current_window - float(window)) < 1.0
                and abs(current_level - float(level)) < 1.0)

    def show_window_level_menu(self, global_pos):
        """右键菜单：恢复默认 + 窗宽/窗位滑块；典型档位收在子菜单里，避免撑出屏幕。

        菜单用 _StayOpenMenu：点预设、拖滑块都不关闭，可以连着对比几档再
        微调；点菜单外或按 Esc 收起。
        """
        if self.image is None:
            return
        menu = _StayOpenMenu(self)
        menu.setObjectName("SliceWindowLevelMenu")
        window, level = self.window_level()
        preset_actions = []      # [(action, window, level)]
        rows = {}

        def apply(new_window, new_level, from_slider=False):
            self.set_window_level(window=new_window, level=new_level)
            current_window, current_level = self.window_level()
            for action, preset_window, preset_level in preset_actions:
                action.setChecked(
                    self._window_level_matches(preset_window, preset_level))
            if not from_slider:
                rows["window"].set_value_silent(current_window)
                rows["level"].set_value_silent(current_level)

        default_window, default_level = self._default_window_level
        default_action = menu.addAction(
            "默认  (%d / %d)"
            % (round(default_window), round(default_level)))
        default_action.setCheckable(True)
        default_action.setProperty("keepOpen", True)
        default_action.triggered.connect(
            lambda *_: apply(default_window, default_level))
        preset_actions.append((default_action, default_window, default_level))

        # 预设窗值是 HU 档位，只对 CT 有意义；收进子菜单，主菜单保持紧凑。
        if (self.info or {}).get("modality") == "CT":
            preset_menu = _StayOpenMenu("典型窗值", menu)
            preset_menu.setObjectName("SliceWindowLevelMenu")
            for label, preset_window, preset_level in CT_WINDOW_PRESETS:
                action = preset_menu.addAction(
                    "%s  %d/%d" % (label, int(preset_window), int(preset_level)))
                action.setCheckable(True)
                action.setProperty("keepOpen", True)
                action.triggered.connect(
                    lambda *_, w=preset_window, lv=preset_level: apply(w, lv))
                preset_actions.append((action, preset_window, preset_level))
            menu.addMenu(preset_menu)

        menu.addSeparator()

        # 滑块量程按数据自身灰度范围来，CT 大约就是 -1024~3071 HU。
        lo, hi = (self.info or {}).get("scalar_range", (0.0, 255.0))
        span = max(2.0, float(hi) - float(lo))
        rows["window"] = _WindowLevelRow(
            "窗宽", 1.0, span, window,
            lambda value: apply(value, self.window_level()[1], True))
        rows["level"] = _WindowLevelRow(
            "窗位", float(lo), float(hi), level,
            lambda value: apply(self.window_level()[0], value, True))
        for row in (rows["window"], rows["level"]):
            holder = QtWidgets.QWidgetAction(menu)
            holder.setDefaultWidget(row)
            menu.addAction(holder)

        for action, preset_window, preset_level in preset_actions:
            action.setChecked(
                self._window_level_matches(preset_window, preset_level))
        style.style_rounded_menu(menu)
        menu.exec(self._clamp_menu_pos(menu, global_pos))

    @staticmethod
    def _clamp_menu_pos(menu, global_pos):
        """把菜单锚点限制在可用屏幕内，避免贴边时整块溢出。"""
        menu.ensurePolished()
        size = menu.sizeHint()
        screen = QtGui.QGuiApplication.screenAt(global_pos)
        if screen is None:
            screen = QtGui.QGuiApplication.primaryScreen()
        if screen is None:
            return global_pos
        geo = screen.availableGeometry()
        x = min(max(global_pos.x(), geo.left()),
                geo.right() - max(1, size.width()) + 1)
        y = min(max(global_pos.y(), geo.top()),
                geo.bottom() - max(1, size.height()) + 1)
        return QtCore.QPoint(x, y)

    def window_level(self):
        return (
            float(self.image_viewer.GetColorWindow()),
            float(self.image_viewer.GetColorLevel()),
        )

    def set_window_level(self, window=None, level=None, render=True):
        if self.image is None:
            return
        current_window, current_level = self.window_level()
        if window is None:
            window = current_window
        if level is None:
            level = current_level
        self.image_viewer.SetColorWindow(max(1.0, float(window)))
        self.image_viewer.SetColorLevel(float(level))
        if render:
            self.image_viewer.Render()
        self._emit_window_level_changed()

    def _emit_window_level_changed(self):
        if self.image is None:
            return
        window, level = self.window_level()
        self.windowLevelChanged.emit(window, level)

    def _queue_window_level_sync(self):
        QtCore.QTimer.singleShot(0, self._emit_window_level_changed)

    def _begin_window_level_drag(self, pos):
        if self.image is None:
            return
        self._wl_dragging = True
        self._wl_drag_start_pos = QtCore.QPointF(pos)
        self._wl_drag_start_window, self._wl_drag_start_level = self.window_level()

    def _update_window_level_drag(self, pos):
        if not self._wl_dragging or self.image is None:
            return
        pos = QtCore.QPointF(pos)
        delta = pos - self._wl_drag_start_pos
        scale = max(1.0, self._wl_drag_start_window) / 250.0
        window = self._wl_drag_start_window + delta.x() * scale
        level = self._wl_drag_start_level - delta.y() * scale
        self.set_window_level(window=window, level=level)

    def _end_window_level_drag(self):
        self._wl_dragging = False

    def _begin_zoom_drag(self, pos):
        if self.image is None:
            return
        camera = self.image_viewer.GetRenderer().GetActiveCamera()
        self._zoom_dragging = True
        self._zoom_drag_start_pos = QtCore.QPointF(pos)
        self._zoom_drag_start_scale = max(0.01, float(camera.GetParallelScale()))

    def _update_zoom_drag(self, pos):
        if not self._zoom_dragging or self.image is None:
            return
        pos = QtCore.QPointF(pos)
        delta_y = pos.y() - self._zoom_drag_start_pos.y()
        camera = self.image_viewer.GetRenderer().GetActiveCamera()
        scale = self._zoom_drag_start_scale * math.exp(delta_y / 220.0)
        camera.SetParallelScale(max(0.01, min(self._max_parallel_scale() * 8.0, scale)))
        self._update_scale_ruler()
        self.image_viewer.Render()

    def _end_zoom_drag(self):
        self._zoom_dragging = False

    def _max_parallel_scale(self):
        if self.image is None:
            return 10000.0
        bounds = self.image.GetBounds()
        axis = SLICE_AXIS[self.orientation]
        axes = [i for i in range(3) if i != axis]
        spans = [abs(bounds[a * 2 + 1] - bounds[a * 2]) for a in axes]
        return max(1.0, max(spans) * 0.75)

    def set_points(self, points, render=True):
        self._points = [dict(point) for point in points]
        self._refresh_point_actors()
        if render and self.image is not None:
            self.image_viewer.Render()

    def set_needle(self, needle, render=True):
        self._needle = dict(needle) if needle is not None else None
        self._refresh_needle_actors()
        self._refresh_planning_marker_actors()
        if render and self.image is not None:
            self.image_viewer.Render()

    def set_planning_markers(self, markers, render=True):
        """入针点/消融点标记：[{world, radius, color, opacity}, ...]。"""
        self._planning_markers = [dict(item) for item in (markers or [])]
        self._refresh_planning_marker_actors()
        if render and self.image is not None:
            self.image_viewer.Render()

    def set_ablation_zone(self, polydata, render=True):
        self._zone_poly = polydata
        self._refresh_zone_actors()
        if render and self.image is not None:
            self.image_viewer.Render()

    def set_segmentations(self, segmentations, render=True):
        self._segmentations = [dict(item) for item in segmentations]
        self._rebuild_segmentation_slice_actors()
        self._refresh_point_actors()
        self._refresh_needle_actors()
        self._refresh_planning_marker_actors()
        self._refresh_zone_actors()
        self._refresh_crosshair_actors()
        if render and self.image is not None:
            self.image_viewer.Render()

    def set_crosshair(self, payload, render=True):
        self._crosshair = dict(payload) if payload else None
        self._refresh_crosshair_actors()
        self._update_probe_readout()
        if render and self.image is not None:
            self.image_viewer.Render()

    def set_active(self, active):
        """切换选中高亮（金色边框）。靠 QSS 的 [active] 属性选择器实现。"""
        active = bool(active)
        if self.property("active") == active:
            return
        self.setProperty("active", active)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def slice_range(self):
        return self._min_slice, self._max_slice

    def current_slice(self):
        return self._slice

    def set_slice(self, index, render=True, *, user=False):
        if self.image is None:
            return
        index = max(self._min_slice, min(self._max_slice, int(index)))
        changed = index != self._slice
        self._slice = index
        self._refresh_segmentation_slice_actors()
        self._refresh_point_actors()
        self._refresh_needle_actors()
        self._refresh_planning_marker_actors()
        self._refresh_zone_actors()
        self._refresh_crosshair_actors()
        self.image_viewer.SetSlice(index)
        self._update_probe_readout()
        if render:
            self.image_viewer.Render()
        self.sliceChanged.emit(index)
        if user and changed:
            self.sliceNavigated.emit()

    def step_slice(self, delta):
        self.set_slice(self._slice + int(delta), user=True)

    def refresh(self, render=True):
        if self.image is None:
            return
        self._reset_slice_camera()
        self._refresh_segmentation_slice_actors()
        self._refresh_point_actors()
        self._refresh_needle_actors()
        self._refresh_planning_marker_actors()
        self._refresh_zone_actors()
        self._refresh_crosshair_actors()
        if render:
            self.image_viewer.Render()

    def _reset_slice_camera(self):
        renderer = self.image_viewer.GetRenderer()
        camera = renderer.GetActiveCamera()
        # Absolute camera pose per orientation. This runs on every refresh and
        # resize, and the segmentation/needle overlays are offset toward the
        # camera at a different time — so the pose must be idempotent. The old
        # relative Azimuth(180) flipped the coronal view on every call, leaving
        # the overlays behind the opaque image plane (colors vanished until the
        # next slice change recomputed the offsets).
        camera.SetFocalPoint(0.0, 0.0, 0.0)
        if self.orientation == "axial":
            # 从下方(-Z)看：让 +X 落在屏幕右侧，与 3D 正视图(前视)左右一致；
            # viewUp 仍为 -Y，竖直方向不变。从上方看会左右镜像，与 3D 相反。
            camera.SetPosition(0.0, 0.0, -1.0)
            camera.SetViewUp(0.0, -1.0, 0.0)
        elif self.orientation == "coronal":
            # 从前方(-Y)看，与 3D 正视图同向：+X 落在屏幕右侧。
            # 原来从后方(+Y)看会左右镜像，与 3D 相反。
            camera.SetPosition(0.0, -1.0, 0.0)
            camera.SetViewUp(0.0, 0.0, 1.0)
        else:  # sagittal
            camera.SetPosition(1.0, 0.0, 0.0)
            camera.SetViewUp(0.0, 0.0, 1.0)
        renderer.ResetCamera()
        renderer.ResetCameraClippingRange()
        self._update_scale_ruler()

    def _clear_segmentation_slice_actors(self):
        renderer = self.image_viewer.GetRenderer()
        for item in self._segmentation_slice_items:
            renderer.RemoveActor(item["actor"])
        self._segmentation_slice_items = []

    def _rebuild_segmentation_slice_actors(self):
        self._clear_segmentation_slice_actors()
        if self.image is None:
            return
        renderer = self.image_viewer.GetRenderer()
        for item in self._segmentations:
            mask = item.get("mask")
            if mask is None or mask.GetDimensions() != self.image.GetDimensions():
                continue
            actor, refs = self._make_segmentation_slice_actor(
                mask,
                item.get("color", (0.15, 0.85, 0.72)),
                item.get("opacity", 0.96),
            )
            renderer.AddActor(actor)
            self._segmentation_slice_items.append(
                {"actor": actor, "refs": refs, "bounds": item.get("bounds")})
        self._refresh_segmentation_slice_actors()

    def _refresh_segmentation_slice_actors(self):
        if self.image is None:
            return
        img_extent = list(self.image.GetExtent())
        axis = SLICE_AXIS[self.orientation]
        slice_index = int(max(img_extent[axis * 2],
                              min(img_extent[axis * 2 + 1], self._slice)))
        camera = self.image_viewer.GetRenderer().GetActiveCamera()
        view_dir = camera.GetDirectionOfProjection()
        spacing = self.image.GetSpacing()
        offset = max(spacing) * 0.18
        for item in self._segmentation_slice_items:
            actor = item["actor"]
            bounds = item.get("bounds")
            # Organ absent from this slice -> hide it and do zero geometry work.
            # Most masks span only part of the volume, so on any given slice the
            # majority of overlays cost nothing.
            if bounds is not None and (
                slice_index < bounds[axis * 2] or slice_index > bounds[axis * 2 + 1]
            ):
                actor.SetVisibility(False)
                continue
            extent = list(img_extent)
            if bounds is not None:
                # Crop extraction to the organ's in-plane footprint instead of the
                # whole 512x512 slice, so the threshold/geometry filters touch a
                # few thousand cells rather than a quarter-million.
                for a in range(3):
                    extent[a * 2] = max(img_extent[a * 2], int(bounds[a * 2]))
                    extent[a * 2 + 1] = min(img_extent[a * 2 + 1], int(bounds[a * 2 + 1]))
            extent[axis * 2] = slice_index
            extent[axis * 2 + 1] = slice_index
            actor.SetVisibility(True)
            geo = item["refs"][0]
            geo.SetExtent(*extent)
            geo.Modified()
            item["refs"][-1].Update()
            actor.SetPosition(
                -view_dir[0] * offset,
                -view_dir[1] * offset,
                -view_dir[2] * offset,
            )

    def _make_segmentation_slice_actor(self, mask_image, color, opacity):
        geometry = vtk.vtkImageDataGeometryFilter()
        geometry.SetInputData(mask_image)

        threshold = vtk.vtkThreshold()
        threshold.SetInputConnection(geometry.GetOutputPort())
        threshold.SetUpperThreshold(0.5)
        threshold.SetThresholdFunction(vtk.vtkThreshold.THRESHOLD_UPPER)

        surface = vtk.vtkGeometryFilter()
        surface.SetInputConnection(threshold.GetOutputPort())

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(surface.GetOutputPort())
        mapper.ScalarVisibilityOff()

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.SetPickable(False)
        actor.SetUseBounds(False)
        prop = actor.GetProperty()
        prop.SetColor(float(color[0]), float(color[1]), float(color[2]))
        prop.SetOpacity(self._slice_segmentation_alpha(opacity))
        prop.SetAmbient(1.0)
        prop.SetDiffuse(0.0)
        return actor, (geometry, threshold, surface, mapper)

    def _slice_segmentation_alpha(self, opacity):
        if opacity >= 0.9:
            return 0.48
        return max(0.20, min(0.72, float(opacity)))

    # ---- 参考坐标系（十字光标） ----------------------------------------
    def _in_plane_axes(self):
        """当前切片平面内的两个世界轴索引（不含切片法向轴）。"""
        axis = SLICE_AXIS[self.orientation]
        return [i for i in range(3) if i != axis]

    def _refresh_crosshair_actors(self):
        renderer = self.image_viewer.GetRenderer()
        # 线宽恒定不变；只有按下并拖动中心时整组高亮变绿，不加粗。
        # 单纯移动鼠标不改变显示，避免未点击时产生误导性的选中反馈。
        active = self._crosshair_dragging
        width = 1.0
        if (self.image is None or not self._crosshair
                or not self._crosshair.get("visible", True)):
            lines = []
        else:
            lines = self._crosshair_line_endpoints()

        # 复用已有 actor：拖动/滚层是高频路径，正交切片恒为两条平面内线，
        # 数量不变时只挪端点、换颜色，不销毁重建 VTK 管线。
        if lines and len(lines) == len(self._crosshair_actors):
            for actor, source, (draw_axis, p0, p1) in zip(
                    self._crosshair_actors, self._crosshair_line_sources, lines):
                source.SetPoint1(*p0)
                source.SetPoint2(*p1)
                color = CROSSHAIR_DRAG_COLOR if active else CROSSHAIR_AXIS_COLORS[draw_axis]
                actor.GetProperty().SetColor(*color)
            return

        for actor in self._crosshair_actors:
            renderer.RemoveActor(actor)
        self._crosshair_actors = []
        self._crosshair_line_sources = []
        for draw_axis, p0, p1 in lines:
            color = CROSSHAIR_DRAG_COLOR if active else CROSSHAIR_AXIS_COLORS[draw_axis]
            actor, source = self._make_crosshair_slice_line(p0, p1, color, width)
            renderer.AddActor(actor)
            self._crosshair_actors.append(actor)
            self._crosshair_line_sources.append(source)

    def _make_crosshair_slice_line(self, p0, p1, color, width_px):
        """切片十字线：用屏幕像素宽度的扁线（而非世界半径管），
        这样在小预览窗里也不会因为缩放而细到看不见。返回 (actor, line)。"""
        line = vtk.vtkLineSource()
        line.SetPoint1(*p0)
        line.SetPoint2(*p1)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(line.GetOutputPort())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetLineWidth(float(width_px))   # 像素宽，随缩放保持可见
        prop.SetAmbient(1.0)
        prop.SetDiffuse(0.0)
        prop.SetOpacity(0.95)
        actor.SetPickable(False)
        actor.SetUseBounds(False)
        return actor, line

    def _crosshair_line_endpoints(self):
        """两条平面内长线 [(draw_axis, p0, p1), ...]（朝相机微抬，画在切片深度上）。"""
        bounds = self.image.GetBounds()
        axis = SLICE_AXIS[self.orientation]
        depth = self._slice_world_position(axis)
        world = list(self._ijk_to_world(self._crosshair["ijk"]))
        world[axis] = depth
        lines = []
        for draw_axis in self._in_plane_axes():
            lo = bounds[draw_axis * 2]
            hi = bounds[draw_axis * 2 + 1]
            # 限制在切片图像边界内，坐标轴不伸出切片之外
            p0 = list(world); p0[draw_axis] = lo
            p1 = list(world); p1[draw_axis] = hi
            lines.append((draw_axis,
                          self._offset_world_to_camera(p0),
                          self._offset_world_to_camera(p1)))
        return lines

    def _offset_world_to_camera(self, point):
        camera = self.image_viewer.GetRenderer().GetActiveCamera()
        view_dir = camera.GetDirectionOfProjection()
        spacing = self.image.GetSpacing()
        offset = max(abs(s) for s in spacing) * 0.3
        return tuple(float(point[i]) - view_dir[i] * offset for i in range(3))

    def _widget_to_display(self, widget_pos):
        widget_w = max(1, self.vtk_widget.width())
        widget_h = max(1, self.vtk_widget.height())
        render_w, render_h = self.render_window.GetSize()
        render_w = render_w or widget_w
        render_h = render_h or widget_h
        dx = float(widget_pos.x()) * render_w / float(widget_w)
        dy = (widget_h - float(widget_pos.y())) * render_h / float(widget_h)
        return dx, dy

    def _world_to_display(self, world):
        ren = self.image_viewer.GetRenderer()
        ren.SetWorldPoint(float(world[0]), float(world[1]), float(world[2]), 1.0)
        ren.WorldToDisplay()
        d = ren.GetDisplayPoint()
        return d[0], d[1]

    def _slice_pick_world(self, widget_pos):
        """把鼠标位置反投影到当前切片平面（不依赖图像 actor，拖出图像也有效）。"""
        if self.image is None:
            return None
        ren = self.image_viewer.GetRenderer()
        dx, dy = self._widget_to_display(widget_pos)
        axis = SLICE_AXIS[self.orientation]
        depth = self._slice_world_position(axis)
        ref = list(self._ijk_to_world(
            self._crosshair["ijk"] if self._crosshair else (0, 0, 0)))
        ref[axis] = depth
        ren.SetWorldPoint(ref[0], ref[1], ref[2], 1.0)
        ren.WorldToDisplay()
        z = ren.GetDisplayPoint()[2]
        ren.SetDisplayPoint(dx, dy, z)
        ren.DisplayToWorld()
        w = ren.GetWorldPoint()
        if w[3] == 0:
            return None
        world = [w[0] / w[3], w[1] / w[3], w[2] / w[3]]
        world[axis] = depth
        return world

    def hover_probe_ijk(self):
        """光标所在体素中心的 (i, j, k)；光标不在切片图内时返回 None。

        每次读数时重新反投影，这样换层/缩放/平移后无需鼠标移动也能跟上。
        """
        if self.image is None or self._hover_pos is None:
            return None
        world = self._slice_pick_world(self._hover_pos)
        if world is None:
            return None
        ijk = list(self._world_to_ijk(world))
        ijk[SLICE_AXIS[self.orientation]] = float(self._slice)
        dims = self.image.GetDimensions()
        voxel = []
        for axis in range(3):
            # 取最近的体素中心；落在图像之外就交给坐标轴中心读数。
            index = int(round(ijk[axis]))
            if index < 0 or index >= int(dims[axis]):
                return None
            voxel.append(index)
        return tuple(voxel)

    def _refresh_hover_probe(self, widget_pos):
        """更新光标取值点；体素没变就不重绘（鼠标移动很密集）。"""
        before = self.hover_probe_ijk()
        self._hover_pos = (
            QtCore.QPointF(widget_pos) if widget_pos is not None else None)
        if self.hover_probe_ijk() == before:
            return
        self._update_probe_readout()
        # 拖动中(十字/缩放/窗宽窗位)各自会重绘，这里不重复渲染。
        dragging = (self._crosshair_dragging or self._zoom_dragging
                    or self._wl_dragging)
        if self.image is not None and not dragging:
            self.image_viewer.Render()

    def _crosshair_hit_test(self, widget_pos):
        """鼠标是否落在十字中心（原点）附近——只有中心可拖动，线身不响应。"""
        if (self.image is None or not self._crosshair
                or not self._crosshair.get("visible", True)):
            return False
        px, py = self._widget_to_display(widget_pos)
        axis = SLICE_AXIS[self.orientation]
        world = list(self._ijk_to_world(self._crosshair["ijk"]))
        world[axis] = self._slice_world_position(axis)
        cx, cy = self._world_to_display(world)
        # 只有非常靠近中心(原点)才算命中，避免离得有点远点一下就把十字拽过去。
        tol = 7.0 * float(self.devicePixelRatioF() or 1.0)
        return math.hypot(px - cx, py - cy) <= tol

    def _begin_crosshair_drag(self, widget_pos):
        """抓取十字：只记录抓取点与中心的世界偏移，不移动。

        拖动时保持这个相对偏移，使十字跟随鼠标平移而不是一抓就跳到光标下。
        """
        self._crosshair_grab_offset = None
        if self.image is None or not self._crosshair:
            return
        world = self._slice_pick_world(widget_pos)
        if world is None:
            return
        center = self._ijk_to_world(self._crosshair["ijk"])
        self._crosshair_grab_offset = tuple(
            center[i] - world[i] for i in range(3))

    def _update_crosshair_drag(self, widget_pos):
        if self.image is None or not self._crosshair:
            return
        world = self._slice_pick_world(widget_pos)
        if world is None:
            return
        # 应用抓取偏移：十字保持与光标的初始相对位置，平移而非跳到光标下。
        offset = self._crosshair_grab_offset or (0.0, 0.0, 0.0)
        adjusted = [world[i] + offset[i] for i in range(3)]
        picked = list(self._world_to_ijk(adjusted))
        new = list(self._crosshair["ijk"])
        for a in self._in_plane_axes():   # 只改平面内两轴，保留切片深度轴
            new[a] = picked[a]
        self._crosshair["ijk"] = tuple(new)
        self._crosshair["world"] = self._ijk_to_world(new)
        self._refresh_crosshair_actors()
        self._update_probe_readout()
        self.image_viewer.Render()
        self.crosshairMoved.emit(tuple(new))

    def picked_ijk(self, widget_pos):
        if self.image is None:
            return None

        widget_w = max(1, self.vtk_widget.width())
        widget_h = max(1, self.vtk_widget.height())
        render_w, render_h = self.render_window.GetSize()
        render_w = render_w or widget_w
        render_h = render_h or widget_h
        display_x = float(widget_pos.x()) * render_w / float(widget_w)
        display_y = (widget_h - float(widget_pos.y())) * render_h / float(widget_h)

        picker = vtk.vtkCellPicker()
        picker.SetTolerance(0.005)
        picker.PickFromListOn()
        picker.AddPickList(self.image_viewer.GetImageActor())
        renderer = self.image_viewer.GetRenderer()
        if not picker.Pick(display_x, display_y, 0, renderer):
            return None

        ijk = list(self._world_to_ijk(picker.GetPickPosition()))
        ijk[SLICE_AXIS[self.orientation]] = float(self._slice)
        return self._clamp_ijk(ijk)

    def _refresh_point_actors(self):
        renderer = self.image_viewer.GetRenderer()
        for actor in self._point_actors:
            renderer.RemoveActor(actor)
        self._point_actors = []

        if self.image is None:
            return

        axis = SLICE_AXIS[self.orientation]
        for point in self._points:
            ijk = point["ijk"]
            if abs(float(ijk[axis]) - float(self._slice)) > 0.5:
                continue
            actor = self._make_slice_point_actor(ijk, point.get("radius", 4.0))
            renderer.AddActor(actor)
            self._point_actors.append(actor)

    def _make_slice_point_actor(self, ijk, radius):
        world = self._ijk_to_world(ijk)
        camera = self.image_viewer.GetRenderer().GetActiveCamera()
        view_dir = camera.GetDirectionOfProjection()
        center = tuple(world[i] - view_dir[i] * radius * 0.15 for i in range(3))

        source = vtk.vtkSphereSource()
        source.SetCenter(*center)
        source.SetRadius(radius * 1.1)
        source.SetThetaResolution(24)
        source.SetPhiResolution(12)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(source.GetOutputPort())

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(1.0, 0.0, 0.0)
        actor.GetProperty().SetAmbient(1.0)
        actor.GetProperty().SetDiffuse(0.0)
        actor.GetProperty().SetSpecular(0.1)
        actor.SetUseBounds(False)
        return actor

    def _refresh_planning_marker_actors(self):
        """画入针点/消融点：按球被当前切片截出的截面大小画。

        滚层经过标记时圆点由小变大再变小，滚出球外就消失——比"只在正好那
        一层显示"更容易看出自己离目标还有几层。
        """
        renderer = self.image_viewer.GetRenderer()
        for actor in self._planning_marker_actors:
            renderer.RemoveActor(actor)
        self._planning_marker_actors = []

        if self.image is None or not self._planning_markers:
            return

        axis = SLICE_AXIS[self.orientation]
        plane = self._slice_world_position(axis)
        for marker in self._planning_markers:
            world = marker.get("world")
            if world is None:
                continue
            radius = float(marker.get("radius", 4.0))
            distance = abs(float(world[axis]) - plane)
            if distance >= radius:
                continue                      # 这一层没切到这个球
            cut = math.sqrt(max(0.0, radius * radius - distance * distance))
            cut = max(cut, radius * 0.3)      # 边缘留个看得见的小点
            center = list(world)
            center[axis] = plane
            actor = self._make_slice_sphere_actor(
                self._offset_slice_point(center),
                cut,
                marker.get("color", (1.0, 0.18, 0.05)),
                float(marker.get("opacity", 1.0)),
            )
            renderer.AddActor(actor)
            self._planning_marker_actors.append(actor)

    def _refresh_needle_actors(self):
        renderer = self.image_viewer.GetRenderer()
        for actor in self._needle_actors:
            renderer.RemoveActor(actor)
        self._needle_actors = []

        if self.image is None or self._needle is None:
            return

        radius = self._needle.get("radius", 2.0)
        # 针道按实际路径画：只画真正落在这一层层厚里的那一段。针斜穿切片时
        # 这一段短到就是一个点(下面的交点标记)，只有针基本与切片平行时才是
        # 一条线——不再把整根针投影到每一层上。
        segments = (
            (self._needle_slab_segment(self._needle["entry_world"],
                                       self._needle["tip_world"]),
             radius * 0.55, (0.20, 0.85, 0.30), 0.85),   # 针道：绿色，呼应入针点
            (self._needle_slab_segment(self._needle["active_start_world"],
                                       self._needle["tip_world"]),
             radius * 0.75, (1.0, 0.82, 0.10), 1.0),     # 活性端：黄色
        )
        marker_radius = max(radius * 1.35,
                            float(self._needle.get("marker_radius", 0.0)))
        for segment, tube_radius, color, opacity in segments:
            if segment is None:
                continue
            p0, p1 = segment
            length = math.sqrt(sum((p1[i] - p0[i]) ** 2 for i in range(3)))
            if length < marker_radius * 2.0:
                continue        # 短到藏在交点标记里：只留那个点，避免叠画
            actor = self._make_slice_line_actor(
                self._offset_slice_point(p0),
                self._offset_slice_point(p1),
                tube_radius,
                color,
                opacity,
            )
            renderer.AddActor(actor)
            self._needle_actors.append(actor)

        cross = self._needle_slice_crossing()
        if cross is not None:
            # 针的真实截面在片子上只有一两个像素，按图像尺度给个下限才看得见。
            marker = self._make_slice_sphere_actor(
                cross,
                marker_radius,
                (1.0, 0.18, 0.05),
            )
            renderer.AddActor(marker)
            self._needle_actors.append(marker)

    def _refresh_zone_actors(self):
        """Draw the ablation ellipsoid's cross-section (fill + outline) on this slice."""
        renderer = self.image_viewer.GetRenderer()
        for actor in self._zone_actors:
            renderer.RemoveActor(actor)
        self._zone_actors = []

        if self.image is None or self._zone_poly is None:
            return

        axis = SLICE_AXIS[self.orientation]
        plane_pos = self._slice_world_position(axis)
        normal = [0.0, 0.0, 0.0]
        origin = [0.0, 0.0, 0.0]
        normal[axis] = 1.0
        origin[axis] = plane_pos
        self._zone_plane.SetOrigin(*origin)
        self._zone_plane.SetNormal(*normal)
        self._zone_cutter.SetInputData(self._zone_poly)
        self._zone_tri.Update()

        outline = self._zone_stripper.GetOutput()
        if outline.GetNumberOfPoints() == 0:
            return  # zone ellipsoid doesn't intersect this slice

        view_dir = self.image_viewer.GetRenderer().GetActiveCamera().GetDirectionOfProjection()
        fill = self._make_zone_slice_actor(
            self._zone_tri.GetOutput(), view_dir, 0.8,
            (1.0, 0.30, 0.10), 0.22, line_width=None)
        ring = self._make_zone_slice_actor(
            outline, view_dir, 1.2,
            (1.0, 0.55, 0.20), 0.95, line_width=2.0)
        for actor in (fill, ring):
            renderer.AddActor(actor)
            self._zone_actors.append(actor)

    def _make_zone_slice_actor(self, poly, view_dir, offset, color, opacity, line_width):
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(poly)
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetOpacity(opacity)
        prop.LightingOff()
        if line_width is not None:
            prop.SetLineWidth(line_width)
        actor.SetUseBounds(False)
        actor.SetPickable(False)
        # Nudge toward the camera so it sits on top of the image (no z-fighting).
        actor.SetPosition(-view_dir[0] * offset, -view_dir[1] * offset, -view_dir[2] * offset)
        return actor

    def _needle_slab_segment(self, p0, p1):
        """把针段裁到当前切片的层厚里，返回落在这一层的那一小段(压平到切片面)。

        针斜穿时裁出来的段很短(≈点)，与切片平行时才是完整的一条线；整段都
        不在这一层就返回 None。
        """
        axis = SLICE_AXIS[self.orientation]
        plane = self._slice_world_position(axis)
        half = abs(self.image.GetSpacing()[axis]) * 0.5 or 0.5
        a0, a1 = float(p0[axis]), float(p1[axis])
        denom = a1 - a0
        if abs(denom) < 1e-9:                    # 与切片平行
            if abs(a0 - plane) > half:
                return None
            lo, hi = 0.0, 1.0
        else:
            t0 = (plane - half - a0) / denom
            t1 = (plane + half - a0) / denom
            lo, hi = (t0, t1) if t0 <= t1 else (t1, t0)
            lo = max(0.0, lo)
            hi = min(1.0, hi)
            if hi < lo:
                return None
        q0 = [p0[i] + (p1[i] - p0[i]) * lo for i in range(3)]
        q1 = [p0[i] + (p1[i] - p0[i]) * hi for i in range(3)]
        q0[axis] = plane
        q1[axis] = plane
        return q0, q1

    def _needle_slice_crossing(self):
        axis = SLICE_AXIS[self.orientation]
        plane = self._slice_world_position(axis)
        p0 = self._needle["entry_world"]
        p1 = self._needle["tip_world"]
        denom = p1[axis] - p0[axis]
        spacing = abs(self.image.GetSpacing()[axis]) or 1.0
        if abs(denom) < 1e-6:
            if abs(p0[axis] - plane) <= spacing * 0.5:
                return self._offset_slice_point(p1)
            return None

        t = (plane - p0[axis]) / denom
        if t < 0.0 or t > 1.0:
            return None
        point = [p0[i] + (p1[i] - p0[i]) * t for i in range(3)]
        point[axis] = plane
        return self._offset_slice_point(point)

    def _make_slice_line_actor(self, p0, p1, radius, color, opacity):
        if math.sqrt(sum((p1[i] - p0[i]) ** 2 for i in range(3))) < 0.5:
            return self._make_slice_sphere_actor(p0, radius * 1.4, color)

        line = vtk.vtkLineSource()
        line.SetPoint1(*p0)
        line.SetPoint2(*p1)

        tube = vtk.vtkTubeFilter()
        tube.SetInputConnection(line.GetOutputPort())
        tube.SetRadius(radius)
        tube.SetNumberOfSides(16)
        tube.CappingOn()

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(tube.GetOutputPort())

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(*color)
        actor.GetProperty().SetOpacity(opacity)
        actor.GetProperty().SetAmbient(0.9)
        actor.GetProperty().SetDiffuse(0.1)
        actor.SetUseBounds(False)
        return actor

    def _make_slice_sphere_actor(self, center, radius, color, opacity=1.0):
        source = vtk.vtkSphereSource()
        source.SetCenter(*center)
        source.SetRadius(radius)
        source.SetThetaResolution(22)
        source.SetPhiResolution(12)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(source.GetOutputPort())

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetAmbient(1.0)
        prop.SetDiffuse(0.0)
        if opacity < 1.0:
            prop.SetOpacity(float(opacity))
        actor.SetUseBounds(False)
        return actor

    def _slice_world_position(self, axis):
        origin = self.image.GetOrigin()
        spacing = self.image.GetSpacing()
        return origin[axis] + float(self._slice) * spacing[axis]

    def _offset_slice_point(self, point):
        camera = self.image_viewer.GetRenderer().GetActiveCamera()
        view_dir = camera.GetDirectionOfProjection()
        radius = self._needle.get("radius", 2.0) if self._needle else 2.0
        return tuple(float(point[i]) - view_dir[i] * radius * 0.25 for i in range(3))

    def _world_to_ijk(self, world):
        origin = self.image.GetOrigin()
        spacing = self.image.GetSpacing()
        values = []
        for i in range(3):
            values.append((float(world[i]) - origin[i]) / spacing[i])
        return values

    def _ijk_to_world(self, ijk):
        origin = self.image.GetOrigin()
        spacing = self.image.GetSpacing()
        return tuple(origin[i] + float(ijk[i]) * spacing[i] for i in range(3))

    def _clamp_ijk(self, ijk):
        dims = self.image.GetDimensions()
        values = []
        for i in range(3):
            upper = max(0, dims[i] - 1)
            values.append(max(0.0, min(float(ijk[i]), float(upper))))
        return tuple(values)

    def eventFilter(self, obj, event):
        if obj is self.vtk_widget:
            if event.type() == QtCore.QEvent.Type.Resize:
                self._update_scale_ruler()
            if event.type() == QtCore.QEvent.Type.Enter:
                # 鼠标进入切片图：断言白色十字光标(VTK 渲染时可能把它重置回箭头)。
                self.vtk_widget.setCursor(_slice_crosshair_cursor())
                self._refresh_hover_probe(event.position())
            if event.type() == QtCore.QEvent.Type.Wheel:
                if self.image is not None:
                    delta = event.angleDelta().y()
                    if delta:
                        self.step_slice(1 if delta > 0 else -1)
                event.accept()
                return True
            if event.type() == QtCore.QEvent.Type.MouseButtonPress:
                if self.image is not None and event.button() in (
                    QtCore.Qt.MouseButton.LeftButton,
                    QtCore.Qt.MouseButton.MiddleButton,
                    QtCore.Qt.MouseButton.RightButton,
                ):
                    self.activated.emit()   # 单击选中本视图（金色高亮）
                if (
                    event.button() == QtCore.Qt.MouseButton.RightButton
                    and self.image is not None
                ):
                    self.contextRequested.emit(
                        event.globalPosition().toPoint(),
                        event.position().toPoint(),
                    )
                    self.show_window_level_menu(
                        event.globalPosition().toPoint())
                    event.accept()
                    return True
                if event.button() == QtCore.Qt.MouseButton.LeftButton:
                    # Drag the crosshair only when pressing right on its centre
                    # (small hot zone); otherwise left-drag zooms the slice.
                    if self._crosshair_hit_test(event.position()):
                        self._crosshair_dragging = True
                        # 只记录抓取偏移，不立即移动 → 抓取时不跳。
                        self._begin_crosshair_drag(event.position())
                        event.accept()
                        return True
                    self._begin_zoom_drag(event.position())
                    event.accept()
                    return True
                if event.button() == QtCore.Qt.MouseButton.MiddleButton:
                    self._begin_window_level_drag(event.position())
                    event.accept()
                    return True
            if event.type() == QtCore.QEvent.Type.MouseMove:
                self._refresh_hover_probe(event.position())
                left_down = bool(event.buttons() & QtCore.Qt.MouseButton.LeftButton)
                middle_down = bool(event.buttons() & QtCore.Qt.MouseButton.MiddleButton)
                if self._crosshair_dragging and left_down:
                    self._update_crosshair_drag(event.position())
                    event.accept()
                    return True
                if self._zoom_dragging and left_down:
                    self._update_zoom_drag(event.position())
                    event.accept()
                    return True
                if self._wl_dragging and middle_down:
                    self._update_window_level_drag(event.position())
                    event.accept()
                    return True
            if event.type() == QtCore.QEvent.Type.MouseButtonRelease:
                if event.button() == QtCore.Qt.MouseButton.LeftButton and self._crosshair_dragging:
                    self._update_crosshair_drag(event.position())
                    self._crosshair_dragging = False
                    self._refresh_crosshair_actors()
                    self.image_viewer.Render()
                    event.accept()
                    return True
                if event.button() == QtCore.Qt.MouseButton.LeftButton and self._zoom_dragging:
                    self._update_zoom_drag(event.position())
                    self._end_zoom_drag()
                    event.accept()
                    return True
                if event.button() == QtCore.Qt.MouseButton.MiddleButton and self._wl_dragging:
                    self._update_window_level_drag(event.position())
                    self._end_window_level_drag()
                    event.accept()
                    return True
            if event.type() == QtCore.QEvent.Type.Leave:
                # 鼠标离开切片图：左下角读数回到参考坐标轴中心。
                self._refresh_hover_probe(None)
            if event.type() in (
                QtCore.QEvent.Type.Leave,
                QtCore.QEvent.Type.FocusOut,
            ):
                self._crosshair_dragging = False
                self._end_zoom_drag()
                self._end_window_level_drag()
            if event.type() == QtCore.QEvent.Type.MouseButtonDblClick:
                if self.image is not None:
                    self.doubleClicked.emit()
                return True
        return super().eventFilter(obj, event)

    def mouseDoubleClickEvent(self, event):
        if self.image is not None:
            self.doubleClicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event):
        # Re-fit the slice after the widget reaches its final laid-out size.
        # Without this the camera keeps the parallel scale from an earlier
        # (smaller) size and the image looks zoomed-in / washed out / blank.
        super().resizeEvent(event)
        self._update_scale_ruler()
        if self.image is not None and hasattr(self, "_fit_timer"):
            # Restarting a single timer coalesces the resize burst generated by
            # fullscreen layout changes into one camera reset/render.
            self._fit_timer.start()

    def _fit_camera(self, force=False, render=True):
        if self.image is None or not self.vtk_widget.isVisible():
            return
        size = tuple(self.render_window.GetSize())
        if not force and size == self._last_fit_size:
            return
        self._reset_slice_camera()
        self._update_scale_ruler()
        if render:
            self.image_viewer.Render()
        self._last_fit_size = size


class ExpandedSliceOverlay(QtWidgets.QFrame):
    """放大切片浮层：双击侧边栏切片视图时展开的全尺寸切片查看。
    
    不创建顶级弹窗，而是覆盖在 3D 视图上方的 QFrame 浮层。
    包含：
      - 全尺寸 SliceView（窗宽/窗位走切片上的右键菜单）
      - 切片导航滑块（支持点击跳转）
      - 收起按钮 + 切片计数器
    
    收起时会同步当前切片位置回原 SliceView。
    
    信号：
      collapsed — 浮层收起时发出
    """

    collapsed = QtCore.Signal()
    crosshairMoved = QtCore.Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent, QtCore.Qt.WindowType.Widget)
        self.setObjectName("ExpandedSliceOverlay")
        self.setWindowFlag(QtCore.Qt.WindowType.Window, False)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setProperty("active", False)
        self.source_view = None
        self._loading = False
        self._loaded_image = None
        self._loaded_orientation = None
        self._loaded_segmentation_signature = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        self.title = QtWidgets.QLabel(objectName="DialogTitle")
        self.counter = QtWidgets.QLabel(objectName="SliceCounter")
        self.counter.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.close_btn = QtWidgets.QPushButton("收起", objectName="OverlayClose")
        self.close_btn.clicked.connect(self.collapse)
        header.addWidget(self.title)
        header.addStretch(1)
        header.addWidget(self.counter)
        header.addWidget(self.close_btn)
        layout.addLayout(header)

        self.slice_view = SliceView("", "axial", self, show_header=False)
        self.slice_view.doubleClicked.connect(self.collapse)
        self.slice_view.sliceChanged.connect(self._sync_from_view)
        self.slice_view.crosshairMoved.connect(self.crosshairMoved)
        self.slice_view.windowLevelChanged.connect(
            self._sync_window_level_to_source)

        # 窗宽/窗位不再占用边栏滑块，改由切片上的右键菜单调节。
        layout.addWidget(self.slice_view, 1)

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(QtWidgets.QLabel("切片"))
        self.slider = SliceSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.valueChanged.connect(self._on_slice_slider)
        controls.addWidget(self.slider, 1)
        layout.addLayout(controls)

    def initialize(self):
        self.slice_view.initialize()

    def _on_slice_slider(self, index):
        # 放大浮层滑块换层同样驱动 3D 坐标轴跟随
        if self._loading:
            self.slice_view.set_slice(index)
        else:
            self.slice_view.set_slice(index, user=True)

    def show_source(self, source_view):
        self.source_view = source_view
        self.title.setText(source_view.title())
        viewer = self.parent()
        segmentations = (
            viewer.slice_segmentations()
            if viewer is not None and hasattr(viewer, "slice_segmentations")
            else []
        )
        segmentation_signature = self._segmentation_signature(segmentations)
        volume_changed = (
            self._loaded_image is not source_view.image
            or self._loaded_orientation != source_view.orientation
        )
        self._loading = True
        self.slice_view.orientation = source_view.orientation
        if volume_changed:
            # Seed masks before set_volume so every pipeline is built once.
            self.slice_view._segmentations = [dict(item) for item in segmentations]
            self.slice_view.set_volume(
                source_view.image, source_view.info, render=False)
        elif segmentation_signature != self._loaded_segmentation_signature:
            self.slice_view.set_segmentations(segmentations, render=False)
        # The expanded SliceView is reused and set_volume applies its default
        # window/level. Always seed it from the selected source view so presets
        # and manual adjustments survive both first-time and later expansion.
        window, level = source_view.window_level()
        self.slice_view.set_window_level(
            window=window, level=level, render=False)

        self._loaded_image = source_view.image
        self._loaded_orientation = source_view.orientation
        self._loaded_segmentation_signature = segmentation_signature
        lo, hi = self.slice_view.slice_range()
        self.slider.setRange(lo, hi)
        self._loading = False
        self.slice_view.set_slice(source_view.current_slice(), render=False)
        if viewer is not None and hasattr(viewer, "point_annotations"):
            self.slice_view.set_points(viewer.point_annotations(), render=False)
        if viewer is not None and hasattr(viewer, "ablation_needle"):
            self.slice_view.set_needle(viewer.ablation_needle(), render=False)
        if viewer is not None and hasattr(viewer, "ablation_zone_polydata"):
            self.slice_view.set_ablation_zone(
                viewer.ablation_zone_polydata(), render=False)
        if viewer is not None and hasattr(viewer, "_crosshair_payload"):
            self.slice_view.set_crosshair(
                viewer._crosshair_payload(), render=False)
        # Visibility is owned by _show_expanded_slice: the overlay must only
        # become visible after the scene above is fully built.
        self._set_active(True)

    def _set_active(self, active):
        self.setProperty("active", bool(active))
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)
        self.update()

    @staticmethod
    def _segmentation_signature(segmentations):
        signature = []
        for item in segmentations:
            color = tuple(item.get("color", ()))
            bounds = item.get("bounds")
            signature.append((
                item.get("name"),
                id(item.get("mask")),
                color,
                float(item.get("opacity", 0.96)),
                tuple(bounds) if bounds is not None else None,
            ))
        return tuple(signature)

    def sync_segmentations_from_parent(self):
        viewer = self.parent()
        if viewer is not None and hasattr(viewer, "slice_segmentations"):
            self.set_segmentations(viewer.slice_segmentations())

    def refresh(self):
        if self.isVisible():
            self.slice_view.refresh()

    def clear_volume(self):
        self.setVisible(False)
        self._set_active(False)
        self.source_view = None
        self._loaded_image = None
        self._loaded_orientation = None
        self._loaded_segmentation_signature = None
        self.slice_view.clear_volume()

    def set_points(self, points):
        self.slice_view.set_points(points, render=self.isVisible())

    def set_needle(self, needle):
        self.slice_view.set_needle(needle, render=self.isVisible())

    def set_planning_markers(self, markers):
        self.slice_view.set_planning_markers(markers, render=self.isVisible())

    def set_ablation_zone(self, polydata):
        self.slice_view.set_ablation_zone(polydata, render=self.isVisible())

    def set_segmentations(self, segmentations):
        self._loaded_segmentation_signature = self._segmentation_signature(
            segmentations)
        self.slice_view.set_segmentations(
            segmentations, render=self.isVisible())

    def set_crosshair(self, payload):
        self.slice_view.set_crosshair(payload, render=self.isVisible())

    def collapse(self):
        if self.source_view is not None and self.slice_view.image is not None:
            # 不立即渲染：源切片此刻还隐藏着，收回后会按帧逐个恢复重画
            self.source_view.set_slice(
                self.slice_view.current_slice(), render=False)
        self.setVisible(False)
        self._set_active(False)
        self.collapsed.emit()

    def closeEvent(self, event):
        self.collapse()
        event.accept()

    def _sync_from_view(self, index):
        lo, hi = self.slice_view.slice_range()
        self.slider.blockSignals(True)
        self.slider.setValue(index)
        self.slider.blockSignals(False)
        self.counter.setText("%d / %d" % (index - lo + 1, hi - lo + 1))
        if self.source_view is not None and not self._loading:
            # 放大期间源切片是隐藏的，跳过它的同步渲染，收回时再重画
            self.source_view.set_slice(
                index, render=self.source_view.isVisible())

    def _sync_window_level_to_source(self, window, level):
        """Keep window/level changes made while expanded on the source slice."""
        if self._loading or self.source_view is None:
            return
        self.source_view.set_window_level(
            window=window,
            level=level,
            render=self.source_view.isVisible(),
        )


class OrthogonalSlicesPanel(QtWidgets.QFrame):
    """三向正交切片面板：包含轴状位、冠状位、矢状位三个 SliceView 预览。
    
    位于 3D 视图右侧（或左侧），每个 SliceView 占 1/3 高度。
    双击任一 SliceView 会触发 expandRequested 信号，打开放大浮层。
    
    信号：
      expandRequested(SliceView) — 双击某个切片视图时发出
    """

    expandRequested = QtCore.Signal(object)
    crosshairMoved = QtCore.Signal(object)
    viewActivated = QtCore.Signal(object)   # 某个切片被单击（上抛给 VolumeViewer 统一协调）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SlicePanel")
        self.image = None
        self.info = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.views = [
            SliceView("轴状位", "axial", self),
            SliceView("冠状位", "coronal", self),
            SliceView("矢状位", "sagittal", self),
        ]
        for view in self.views:
            view.doubleClicked.connect(lambda view=view: self.expandRequested.emit(view))
            view.crosshairMoved.connect(self.crosshairMoved)
            view.activated.connect(lambda view=view: self.viewActivated.emit(view))
            layout.addWidget(view, 1)

    def initialize(self):
        for view in self.views:
            view.initialize()

    def set_volume(self, image, info, render=True):
        self.image = image
        self.info = info
        for view in self.views:
            view.set_volume(image, info, render=render)

    def set_points(self, points, render=True):
        for view in self.views:
            view.set_points(points, render=render)

    def set_needle(self, needle, render=True):
        for view in self.views:
            view.set_needle(needle, render=render)

    def set_planning_markers(self, markers, render=True):
        for view in self.views:
            view.set_planning_markers(markers, render=render)

    def set_ablation_zone(self, polydata, render=True):
        for view in self.views:
            view.set_ablation_zone(polydata, render=render)

    def set_segmentations(self, segmentations, render=True):
        for view in self.views:
            view.set_segmentations(segmentations, render=render)

    def set_crosshair(self, payload, render=True):
        for view in self.views:
            view.set_crosshair(payload, render=render)

    def refresh(self):
        for view in self.views:
            view.refresh()
