"""
3D 体渲染视图 + 三向正交切片视图模块

============================================================
模块功能
============================================================
本模块是应用核心的可视化组件，负责所有医学影像的 3D/2D 显示。

主要类：
  VolumeViewer           — 主 3D 体渲染视图 + 消融针 + 消融范围显示
  SliceSlider            — 支持点击跳转的切片导航滑块
  DefaultMarkedSlider    — 带默认值标记的滑块（窗宽窗位用）
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

import math
import os

import vtk
from PySide6 import QtCore, QtGui, QtWidgets
from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor

from . import presets, segmentation

# 3D 视口背景色（纯黑，与医院阅片环境保持一致）
BG_TOP = (0.0, 0.0, 0.0)
BG_BOTTOM = (0.0, 0.0, 0.0)
# 切片方向 → VTK 轴索引的映射
SLICE_AXIS = {"sagittal": 0, "coronal": 1, "axial": 2}

# 参考坐标系（十字光标）配色
#   每个世界轴一个颜色：X=红, Y=绿, Z=蓝（医学/工程惯例）
#   鼠标悬停中心/拖动时整组变为高亮绿，原点球用中性白
CROSSHAIR_AXIS_COLORS = {
    0: (0.96, 0.28, 0.28),   # X 红
    1: (0.96, 0.86, 0.20),   # Y 黄（避免与"可移动"高亮绿撞色）
    2: (0.34, 0.56, 0.98),   # Z 蓝
}
CROSSHAIR_HOVER_COLOR = (0.20, 0.95, 0.45)   # 悬停中心/拖动时的高亮绿
CROSSHAIR_ORIGIN_COLOR = (0.92, 0.92, 0.96)


class _StayOpenMenu(QtWidgets.QMenu):
    """点击带 keepOpen 属性的项时不关闭菜单，便于连续勾选。

    用于 3D 右键的"分割部位"子菜单：连续点不同器官的显示/隐藏，
    不必每点一次就重新右键。其它(不带 keepOpen 的)项仍正常点击即关闭。
    """

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

    def _on_toggle(self, on):
        self.slider.setEnabled(on)
        self.pct.setEnabled(on)
        self._on_visible(self._name, on)

    def _on_slide(self, value):
        self.pct.setText("%d%%" % value)
        self._on_opacity(self._name, value / 100.0)

    def set_visible_silent(self, on):
        """供"全部显示/隐藏"批量刷新行内复选框状态，不触发回调。"""
        self.check.blockSignals(True)
        self.check.setChecked(on)
        self.check.blockSignals(False)
        self.slider.setEnabled(on)
        self.pct.setEnabled(on)


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
        self._main_layout.addWidget(self.view3d_frame, 1)

        self.render_window = self.vtk_widget.GetRenderWindow()
        self.renderer = vtk.vtkRenderer()
        self.renderer.SetBackground(*BG_BOTTOM)
        self.renderer.SetBackground2(*BG_TOP)
        self.renderer.GradientBackgroundOn()
        self.render_window.AddRenderer(self.renderer)
        # 平行(正交)投影：避免透视下平行线汇聚——这样无论参考十字拖到哪里，
        # 它的轴向都与左下角方向标记完全一致；对手术规划也更准(无近大远小畸变)。
        self.renderer.GetActiveCamera().ParallelProjectionOn()

        self.interactor = self.render_window.GetInteractor()
        self.interactor.SetInteractorStyle(vtk.vtkInteractorStyleTrackballCamera())

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

        self.slice_panel = OrthogonalSlicesPanel(self)
        self.slice_panel.setFixedWidth(300)
        # 未导入 CT 时也保持三个切片框可见，作为界面布局骨架
        self.slice_panel.setVisible(True)
        self.slice_panel.expandRequested.connect(self._show_expanded_slice)
        self.slice_panel.crosshairMoved.connect(self._on_slice_crosshair_moved)
        self.slice_panel.viewActivated.connect(self._set_active_view)
        self._main_layout.addWidget(self.slice_panel)

        self.expanded_slice = ExpandedSliceOverlay(self)
        self.expanded_slice.setVisible(False)
        self.expanded_slice.collapsed.connect(self._on_expanded_slice_collapsed)
        self.expanded_slice.crosshairMoved.connect(self._on_slice_crosshair_moved)
        self._main_layout.addWidget(self.expanded_slice, 1)

    def initialize(self):
        """初始化 VTK 交互器（必须在窗口首次显示后调用）。"""
        self.interactor.Initialize()
        if self.slice_panel.isVisible():
            self.slice_panel.initialize()

    def eventFilter(self, obj, event):
        if obj is self.vtk_widget:
            et = event.type()
            if et == QtCore.QEvent.Type.MouseButtonPress:
                # 单击 3D 视图 → 选中高亮（清除切片高亮）；不拦截事件，旋转照常
                self._set_active_view("3d")
                if event.button() == QtCore.Qt.MouseButton.RightButton:
                    self._show_3d_context_menu(event.globalPosition().toPoint())
                    return True
            elif et == QtCore.QEvent.Type.MouseButtonDblClick:
                # 双击 3D 视图 → 全屏 / 还原（与切片放大效果一致）
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
        """
        self.view3d_frame.setProperty("active", target == "3d")
        self._repolish(self.view3d_frame)
        for view in self.slice_panel.views:
            view.set_active(view is target)

    def _toggle_view3d_fullscreen(self):
        """双击 3D 视图在"全屏(隐藏右侧切片栏)"与正常布局间切换。"""
        if self.image is None:
            return
        self._view3d_fullscreen = not self._view3d_fullscreen
        # 全屏时隐藏切片栏，3D 视图(stretch=1)自动铺满整个查看区
        self.view3d_frame.setProperty("fullscreen", self._view3d_fullscreen)
        self._repolish(self.view3d_frame)
        self.slice_panel.setVisible(not self._view3d_fullscreen)
        self._layout_slice_overlays()
        self.render()

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
        self.image = image
        self.info = info
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

        self.mapper = vtk.vtkSmartVolumeMapper()
        self.mapper.SetInputData(image)
        self.mapper.SetBlendModeToComposite()

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

        self.volume = vtk.vtkVolume()
        self.volume.SetMapper(self.mapper)
        self.volume.SetProperty(self.vol_property)
        self.renderer.AddVolume(self.volume)

        self._add_orientation_marker()
        self.renderer.AddViewProp(self._needle_angle_actor)
        self._update_needle_angle_overlay()
        self._ablation_zone = None
        self._zone_actor.SetVisibility(False)
        self.renderer.AddActor(self._zone_actor)

        # Everything visible at full opacity by default; the user adjusts layers
        # via the 3D right-click "组织" menu.
        self._init_tissue_model(info["modality"])
        self._apply_tissue_model()
        # Reference crosshair starts at the volume centre by default.
        dims = image.GetDimensions()
        self._crosshair_ijk = (
            (dims[0] - 1) * 0.5, (dims[1] - 1) * 0.5, (dims[2] - 1) * 0.5)
        self._rebuild_crosshair_actors()
        self.reset_view()
        self.expanded_slice.setVisible(False)
        self.view3d_frame.setVisible(True)
        self.vtk_widget.setVisible(True)
        self.slice_panel.set_volume(image, info)
        self._push_segmentations_to_slices()
        self._push_crosshair_to_slices()
        self._view3d_fullscreen = False
        self.view3d_frame.setProperty("fullscreen", False)
        self._repolish(self.view3d_frame)
        self.slice_panel.setVisible(True)
        self._refresh_slice_points()
        self._layout_slice_overlays()
        if self.isVisible():
            self.slice_panel.initialize()

    def apply_tissues(self, tissue_states, opacity_scale=None):
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

    def _apply_tissue_model(self):
        states = {
            n: self._tissue_opacity.get(n, 1.0)
            for n in self._tissue_order
            if self._tissue_visible.get(n, True)
        }
        self.apply_tissues(states, self._opacity_scale)

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

    def _refresh_slice_points(self):
        points = self.point_annotations()
        needle = self.ablation_needle()
        self.slice_panel.set_points(points)
        self.expanded_slice.set_points(points)
        self.slice_panel.set_needle(needle)
        self.expanded_slice.set_needle(needle)

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
    # 修改外观：CROSSHAIR_AXIS_COLORS / CROSSHAIR_HOVER_COLOR / CROSSHAIR_ORIGIN_COLOR
    # ============================================================
    def crosshair_ijk(self):
        return self._crosshair_ijk

    def set_crosshair_ijk(self, ijk, from_slice=False):
        """更新参考坐标系位置（ijk），重建 3D 十字并同步到三向切片。"""
        if self.image is None or ijk is None:
            return
        self._crosshair_ijk = self._clamp_ijk(ijk)
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

    def _crosshair_payload(self):
        if self.image is None or self._crosshair_ijk is None:
            return None
        return {
            "ijk": tuple(self._crosshair_ijk),
            "world": self._ijk_to_world(self._crosshair_ijk),
            "visible": self._crosshair_visible,
        }

    def _push_crosshair_to_slices(self):
        payload = self._crosshair_payload()
        self.slice_panel.set_crosshair(payload)
        if self.expanded_slice.isVisible():
            self.expanded_slice.set_crosshair(payload)

    def _crosshair_dimensions(self):
        """返回 (half_length, line_radius, origin_radius)（世界单位）。"""
        bounds = self.image.GetBounds()
        extents = [abs(bounds[1] - bounds[0]),
                   abs(bounds[3] - bounds[2]),
                   abs(bounds[5] - bounds[4])]
        diagonal = math.sqrt(sum(v * v for v in extents)) or 1.0
        half_length = diagonal * 0.75          # 总长约 1.5 倍体对角线 → 远超屏幕
        line_radius = max(0.22, diagonal * 0.0008)   # 细线
        origin_radius = max(0.8, diagonal * 0.006)   # 原点小一点
        return half_length, line_radius, origin_radius

    def _rebuild_crosshair_actors(self):
        for actor in self._crosshair_actors:
            self.renderer.RemoveActor(actor)
        self._crosshair_actors = []
        if self.image is None or self._crosshair_ijk is None or not self._crosshair_visible:
            return

        center = self._ijk_to_world(self._crosshair_ijk)
        half, radius, _ = self._crosshair_dimensions()
        labels = ("X", "Y", "Z")
        for axis in range(3):
            d = [0.0, 0.0, 0.0]
            d[axis] = half
            p0 = tuple(center[i] - d[i] for i in range(3))
            p1 = tuple(center[i] + d[i] for i in range(3))
            self._crosshair_actors.append(
                self._make_crosshair_line_actor(
                    p0, p1, radius, CROSSHAIR_AXIS_COLORS[axis]))
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

    def _make_crosshair_line_actor(self, p0, p1, radius, color):
        line = vtk.vtkLineSource()
        line.SetPoint1(*p0)
        line.SetPoint2(*p1)
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(line.GetOutputPort())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetLineWidth(1.0)        # screen-space pixels; does not scale with zoom
        prop.SetAmbient(1.0)
        prop.SetDiffuse(0.0)
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
        half, _, _ = self._crosshair_dimensions()
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

    def _push_segmentations_to_slices(self):
        segmentations = self.slice_segmentations()
        self.slice_panel.set_segmentations(segmentations)
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
            cast = vtk.vtkImageCast()
            cast.SetInputConnection(voi.GetOutputPort())
        else:
            cast = vtk.vtkImageCast()
            cast.SetInputData(mask_image)
        # uint8 0/1 can't hold blurred values — must be float or the blur
        # rounds straight back to 0/1 and nothing is gained.
        cast.SetOutputScalarTypeToFloat()

        blur = vtk.vtkImageGaussianSmooth()
        blur.SetInputConnection(cast.GetOutputPort())
        blur.SetDimensionality(3)
        # Standard deviation in voxel units. Equal voxels means more physical
        # smoothing along the thick-Z axis, which is exactly what kills the
        # inter-slice staircase on CT with large slice spacing.
        blur.SetStandardDeviations(1.6, 1.6, 1.6)
        blur.SetRadiusFactors(1.5, 1.5, 1.5)

        surface = vtk.vtkFlyingEdges3D()
        surface.SetInputConnection(blur.GetOutputPort())
        surface.SetValue(0, iso)
        surface.ComputeNormalsOff()  # recomputed after mesh smoothing below

        smooth = vtk.vtkWindowedSincPolyDataFilter()
        smooth.SetInputConnection(surface.GetOutputPort())
        smooth.SetNumberOfIterations(20)
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
    # ============================================================
    def has_ablation_needle(self):
        return self._ablation_needle is not None

    def ablation_needle(self):
        if self._ablation_needle is None:
            return None
        item = dict(self._ablation_needle)
        item["radius"] = self._needle_visual_radius()
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
            return self.connect_planning_points()
        self.renderer.ResetCameraClippingRange()
        self.render()
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
        return True

    def clear_planning_points(self):
        """清除入针点/消融点规划标记(不主动删除已生成的针道)。"""
        self._planning_points = {"entry": None, "tip": None}
        self._remove_planning_markers()
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
        # 颜色与大小都与针道两端一致：入针点绿色(=针入口球)、消融点红色(=针尖球)。
        base = self._needle_visual_radius()
        if kind == "entry":
            color, scale = (0.20, 0.85, 0.30), 1.15
        else:
            color, scale = (1.0, 0.18, 0.05), 1.65
        marker = self._make_sphere_actor(world, base * scale, color)
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
            radius * 1.65,
            (1.0, 0.18, 0.05),     # 针头/消融点：红色
        )
        entry = self._make_sphere_actor(
            needle["entry_world"],
            radius * 1.15,
            (0.20, 0.85, 0.30),    # 入针点：绿色
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

    def _make_sphere_actor(self, center, radius, color):
        source = vtk.vtkSphereSource()
        source.SetCenter(*center)
        source.SetRadius(radius)
        source.SetThetaResolution(24)
        source.SetPhiResolution(14)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(source.GetOutputPort())

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(*color)
        actor.GetProperty().SetAmbient(0.65)
        actor.GetProperty().SetDiffuse(0.35)
        actor.GetProperty().SetSpecular(0.25)
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
        # 针的可见半径直接由"消融针仿真"的直径决定(直径/2)，让 3D 针、切片针
        # 和入针点/消融点标记的粗细与直径设置保持一致；夹在可见范围内避免过细/过粗。
        diameter = self._ablation_params["diameter_mm"]
        return max(0.5, min(4.0, diameter * 0.5))

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

    def _layout_slice_overlays(self):
        panel_w = min(320, max(260, self.width() // 4))
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

    def _show_expanded_slice(self, source_view):
        if self.image is None:
            return
        # Build the overlay's scene while it is still hidden. After AI
        # segmentation the mask-pipeline rebuild takes a noticeable moment;
        # swapping visibility first left a half-built overlay (stale frame
        # from the previous expansion) flashing on screen until the rebuild
        # finished.
        self.expanded_slice.show_source(source_view)
        # Hide the whole 3D frame (not just the GL widget) so the overlay gets
        # the full viewer width — an empty frame would keep its stretch space.
        self.view3d_frame.setVisible(False)
        self.slice_panel.setVisible(False)
        self._update_camera_window_center(render=True)
        self.expanded_slice.setVisible(True)
        self._layout_slice_overlays()
        if self.isVisible():
            self.expanded_slice.initialize()
        self.expanded_slice.refresh()

    def _on_expanded_slice_collapsed(self):
        if self.image is not None:
            self.expanded_slice.setVisible(False)
            self.view3d_frame.setVisible(True)
            self.slice_panel.setVisible(True)
            self._layout_slice_overlays()
            self._update_camera_window_center(render=True)
            self.render()

    def reset_view(self):
        """重置相机到固定的前视视角（anterior view）。
        
        绝对相机姿态：
          - 视角上方 = 病人头侧（Z 轴正方向）
          - 视线方向 = 从前向后（Y 轴正方向）
          - 相机位置 = 病人前方
        
        ResetCamera 仅调整距离以完整框住体数据，保持方向不变。
        每次按下都会得到完全相同的视角。
        修改默认视角方向：修改 SetViewUp 和 SetPosition。
        """
        cam = self.renderer.GetActiveCamera()
        cam.SetViewUp(0, 0, 1)        # patient superior (head) points up
        cam.SetFocalPoint(0, 0, 0)
        cam.SetPosition(0, -1, 0)     # camera in front; view direction = +Y
        if self.image is not None:
            self.renderer.ResetCamera(self.image.GetBounds())
        else:
            self.renderer.ResetCamera()
        self._update_camera_window_center()
        self.renderer.ResetCameraClippingRange()
        self._widen_clipping_for_crosshair()
        self.render()

    def render(self):
        self.render_window.Render()

    def _add_orientation_marker(self):
        """左下角方向标记：X/Y/Z 三向箭头坐标轴，随主相机同步旋转。

        配色与参考坐标系一致：X 红 / Y 黄 / Z 蓝（Y 用黄，避免与可移动
        高亮的绿色撞色）。每个轴末端是小箭头，用于判断当前观察方位。
        """
        axes = vtk.vtkAxesActor()
        axes.SetTotalLength(1, 1, 1)
        axes.SetShaftTypeToLine()
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
            shafts[axis].SetLineWidth(2.0)
            tips[axis].SetColor(*color)
            captions[axis].GetCaptionTextProperty().SetColor(*color)
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


class DefaultMarkedSlider(QtWidgets.QSlider):
    """Fixed-range slider with a visible marker for the default value."""

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self.setObjectName("WindowLevelSlider")
        self._default_value = 50
        self.setRange(0, 100)
        self.setTickInterval(10)
        self.setSingleStep(1)
        self.setPageStep(5)
        self.setTracking(True)
        if orientation == QtCore.Qt.Orientation.Horizontal:
            self.setTickPosition(QtWidgets.QSlider.TickPosition.TicksBelow)
        else:
            self.setTickPosition(QtWidgets.QSlider.TickPosition.TicksLeft)

    def set_default_value(self, value):
        self._default_value = int(max(self.minimum(), min(self.maximum(), value)))
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        opt = QtWidgets.QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QtWidgets.QStyle.ComplexControl.CC_Slider,
            opt,
            QtWidgets.QStyle.SubControl.SC_SliderGroove,
            self,
        )
        if groove.isNull():
            return

        span = groove.width() if self.orientation() == QtCore.Qt.Orientation.Horizontal else groove.height()
        if span <= 0:
            return
        pos = QtWidgets.QStyle.sliderPositionFromValue(
            self.minimum(),
            self.maximum(),
            self._default_value,
            span,
            self.invertedAppearance(),
        )

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        pen = QtGui.QPen(QtGui.QColor(120, 132, 145, 150), 1)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        if self.orientation() == QtCore.Qt.Orientation.Horizontal:
            x = groove.x() + pos
            y = groove.center().y()
            painter.drawLine(x, y - 5, x, y + 5)
        else:
            x = groove.center().x()
            y = groove.y() + pos
            painter.drawLine(x - 5, y, x + 5, y)


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


class SliceView(QtWidgets.QFrame):
    """单个二维正交切片视图，底层使用 vtkImageViewer2。
    
    支持三种方向：
      - axial    (轴状位/横断面)：SetSliceOrientationToXY
      - coronal  (冠状位/额面)：SetSliceOrientationToXZ
      - sagittal (矢状位)：SetSliceOrientationToYZ
    
    交互功能：
      - 鼠标进入：显示与黑色底色相反的白色十字光标
      - 滚轮：切换切片层
      - 左键拖动：调整窗宽窗位
      - 双击：放大到 ExpandedSliceOverlay
    
    信号：
      doubleClicked       — 双击事件
      sliceChanged(int)   — 切片索引改变
      contextRequested    — 右键菜单请求（全局坐标, 局部坐标）
      windowLevelChanged  — 窗宽窗位改变
    """

    doubleClicked = QtCore.Signal()
    sliceChanged = QtCore.Signal(int)
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
        self._zoom_dragging = False
        self._zoom_drag_start_pos = QtCore.QPointF()
        self._zoom_drag_start_scale = 1.0
        # 参考坐标系（十字光标）状态
        self._crosshair = None              # payload: {ijk, world, visible}
        self._crosshair_actors = []
        self._crosshair_hover = False
        self._crosshair_dragging = False
        self._crosshair_grab_offset = None  # 抓取点与中心的世界偏移(拖动保持相对,不跳)

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
        # Mouse tracking on → no-button moves are delivered, so the crosshair
        # can highlight green on hover (Qt otherwise only sends moves while a
        # button is held).
        self.vtk_widget.setMouseTracking(True)
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

    def title(self):
        return self._title

    def initialize(self):
        self.interactor.Initialize()

    def set_volume(self, image, info):
        self.image = image
        self.info = info
        self._zone_poly = None
        self.image_viewer.SetInputData(image)
        self.image_viewer.GetImageActor().SetVisibility(True)
        getattr(self.image_viewer, self.ORIENTATION_METHODS[self.orientation])()
        self._configure_window_level()
        self._min_slice = int(self.image_viewer.GetSliceMin())
        self._max_slice = int(self.image_viewer.GetSliceMax())
        self.set_slice((self._min_slice + self._max_slice) // 2)
        self._reset_slice_camera()
        self._rebuild_segmentation_slice_actors()
        self._refresh_point_actors()
        self._refresh_needle_actors()
        self._refresh_zone_actors()
        self._refresh_crosshair_actors()
        self.image_viewer.Render()
        self._emit_window_level_changed()

    def _configure_window_level(self):
        lo, hi = self.info.get("scalar_range", (0.0, 255.0))
        if self.info.get("modality") == "CT":
            self.image_viewer.SetColorWindow(1400.0)
            self.image_viewer.SetColorLevel(200.0)
        else:
            window = max(1.0, float(hi) - float(lo))
            self.image_viewer.SetColorWindow(window)
            self.image_viewer.SetColorLevel((float(lo) + float(hi)) / 2.0)

    def window_level(self):
        return (
            float(self.image_viewer.GetColorWindow()),
            float(self.image_viewer.GetColorLevel()),
        )

    def set_window_level(self, window=None, level=None):
        if self.image is None:
            return
        current_window, current_level = self.window_level()
        if window is None:
            window = current_window
        if level is None:
            level = current_level
        self.image_viewer.SetColorWindow(max(1.0, float(window)))
        self.image_viewer.SetColorLevel(float(level))
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

    def set_points(self, points):
        self._points = [dict(point) for point in points]
        self._refresh_point_actors()
        if self.image is not None:
            self.image_viewer.Render()

    def set_needle(self, needle):
        self._needle = dict(needle) if needle is not None else None
        self._refresh_needle_actors()
        if self.image is not None:
            self.image_viewer.Render()

    def set_ablation_zone(self, polydata):
        self._zone_poly = polydata
        self._refresh_zone_actors()
        if self.image is not None:
            self.image_viewer.Render()

    def set_segmentations(self, segmentations):
        self._segmentations = [dict(item) for item in segmentations]
        self._rebuild_segmentation_slice_actors()
        self._refresh_point_actors()
        self._refresh_needle_actors()
        self._refresh_zone_actors()
        self._refresh_crosshair_actors()
        if self.image is not None:
            self.image_viewer.Render()

    def set_crosshair(self, payload):
        self._crosshair = dict(payload) if payload else None
        self._refresh_crosshair_actors()
        if self.image is not None:
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

    def set_slice(self, index):
        if self.image is None:
            return
        index = max(self._min_slice, min(self._max_slice, int(index)))
        self._slice = index
        self._refresh_segmentation_slice_actors()
        self._refresh_point_actors()
        self._refresh_needle_actors()
        self._refresh_zone_actors()
        self._refresh_crosshair_actors()
        self.image_viewer.SetSlice(index)
        self.image_viewer.Render()
        self.sliceChanged.emit(index)

    def step_slice(self, delta):
        self.set_slice(self._slice + int(delta))

    def refresh(self):
        if self.image is None:
            return
        self._reset_slice_camera()
        self._refresh_segmentation_slice_actors()
        self._refresh_point_actors()
        self._refresh_needle_actors()
        self._refresh_zone_actors()
        self._refresh_crosshair_actors()
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
        for actor in self._crosshair_actors:
            renderer.RemoveActor(actor)
        self._crosshair_actors = []
        if (self.image is None or not self._crosshair
                or not self._crosshair.get("visible", True)):
            return

        # 线宽恒定不变；悬停中心/拖动时只整组高亮变绿，不加粗
        active = self._crosshair_hover or self._crosshair_dragging
        width = 1.0
        for draw_axis, p0, p1 in self._crosshair_line_endpoints():
            color = CROSSHAIR_HOVER_COLOR if active else CROSSHAIR_AXIS_COLORS[draw_axis]
            actor = self._make_crosshair_slice_line(p0, p1, color, width)
            renderer.AddActor(actor)
            self._crosshair_actors.append(actor)

    def _make_crosshair_slice_line(self, p0, p1, color, width_px):
        """切片十字线：用屏幕像素宽度的扁线（而非世界半径管），
        这样在小预览窗里也不会因为缩放而细到看不见。"""
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
        return actor

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

    def _update_crosshair_hover(self, widget_pos):
        hover = self._crosshair_hit_test(widget_pos)
        if hover == self._crosshair_hover:
            return
        self._crosshair_hover = hover
        self.vtk_widget.setCursor(
            QtCore.Qt.CursorShape.SizeAllCursor if hover
            else QtCore.Qt.CursorShape.ArrowCursor)
        self._refresh_crosshair_actors()
        self.image_viewer.Render()

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

    def _refresh_needle_actors(self):
        renderer = self.image_viewer.GetRenderer()
        for actor in self._needle_actors:
            renderer.RemoveActor(actor)
        self._needle_actors = []

        if self.image is None or self._needle is None:
            return

        radius = self._needle.get("radius", 2.0)
        projected_entry, projected_tip = self._project_needle_to_slice(
            self._needle["entry_world"],
            self._needle["tip_world"],
        )
        projected_active, projected_active_tip = self._project_needle_to_slice(
            self._needle["active_start_world"],
            self._needle["tip_world"],
        )

        path = self._make_slice_line_actor(
            projected_entry,
            projected_tip,
            radius * 0.55,
            (0.20, 0.85, 0.30),    # 针道：绿色，呼应入针点
            0.85,
        )
        active = self._make_slice_line_actor(
            projected_active,
            projected_active_tip,
            radius * 0.75,
            (1.0, 0.82, 0.10),
            1.0,
        )
        for actor in (path, active):
            renderer.AddActor(actor)
            self._needle_actors.append(actor)

        cross = self._needle_slice_crossing()
        if cross is not None:
            marker = self._make_slice_sphere_actor(
                cross,
                radius * 1.35,
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

    def _project_needle_to_slice(self, p0, p1):
        axis = SLICE_AXIS[self.orientation]
        plane = self._slice_world_position(axis)
        q0 = list(p0)
        q1 = list(p1)
        q0[axis] = plane
        q1[axis] = plane
        return self._offset_slice_point(q0), self._offset_slice_point(q1)

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

    def _make_slice_sphere_actor(self, center, radius, color):
        source = vtk.vtkSphereSource()
        source.SetCenter(*center)
        source.SetRadius(radius)
        source.SetThetaResolution(22)
        source.SetPhiResolution(12)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(source.GetOutputPort())

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(*color)
        actor.GetProperty().SetAmbient(1.0)
        actor.GetProperty().SetDiffuse(0.0)
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
            if event.type() == QtCore.QEvent.Type.Enter:
                # 鼠标进入切片图：断言白色十字光标(VTK 渲染时可能把它重置回箭头)。
                self.vtk_widget.setCursor(_slice_crosshair_cursor())
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
                if not left_down and not middle_down:   # 无按键移动 → 悬停高亮检测
                    self._update_crosshair_hover(event.position())
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
        if self.image is not None:
            QtCore.QTimer.singleShot(0, self._fit_camera)

    def _fit_camera(self):
        if self.image is None:
            return
        self._reset_slice_camera()
        self.image_viewer.Render()


class ExpandedSliceOverlay(QtWidgets.QFrame):
    """放大切片浮层：双击侧边栏切片视图时展开的全尺寸切片查看。
    
    不创建顶级弹窗，而是覆盖在 3D 视图上方的 QFrame 浮层。
    包含：
      - 全尺寸 SliceView
      - 垂直窗位滑块 + 水平窗宽滑块
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
        self.source_view = None
        self._loading = False
        self._syncing_window_level = False
        self._wl_default_window = 1400.0
        self._wl_default_level = 200.0
        self._wl_window_min = 350.0
        self._wl_window_max = 4200.0
        self._wl_level_span = 1400.0

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
        self.slice_view.windowLevelChanged.connect(self._sync_window_level_controls)
        self.slice_view.crosshairMoved.connect(self.crosshairMoved)

        image_row = QtWidgets.QHBoxLayout()
        image_row.setSpacing(8)
        level_box = QtWidgets.QVBoxLayout()
        level_box.setSpacing(6)
        level_label = QtWidgets.QLabel("窗位")
        level_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.level_slider = DefaultMarkedSlider(QtCore.Qt.Orientation.Vertical)
        self.level_slider.setMinimumHeight(220)
        self.level_slider.valueChanged.connect(self._on_level_slider)
        level_box.addWidget(level_label)
        level_box.addWidget(self.level_slider, 1)
        image_row.addLayout(level_box)
        image_row.addWidget(self.slice_view, 1)
        layout.addLayout(image_row, 1)

        window_controls = QtWidgets.QHBoxLayout()
        window_controls.setSpacing(8)
        window_controls.addWidget(QtWidgets.QLabel("窗宽"))
        self.window_slider = DefaultMarkedSlider(QtCore.Qt.Orientation.Horizontal)
        self.window_slider.valueChanged.connect(self._on_window_slider)
        window_controls.addWidget(self.window_slider, 1)
        layout.addLayout(window_controls)

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(8)
        controls.addWidget(QtWidgets.QLabel("切片"))
        self.slider = SliceSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider.valueChanged.connect(self.slice_view.set_slice)
        controls.addWidget(self.slider, 1)
        layout.addLayout(controls)

    def initialize(self):
        self.slice_view.initialize()

    def show_source(self, source_view):
        self.source_view = source_view
        self.title.setText(source_view.title())
        self.slice_view.orientation = source_view.orientation
        viewer = self.parent()
        segmentations = (
            viewer.slice_segmentations()
            if viewer is not None and hasattr(viewer, "slice_segmentations")
            else []
        )
        self._loading = True
        # Seed the masks before set_volume so its rebuild already uses the
        # current segmentations — calling set_segmentations() afterwards
        # rebuilt every mask pipeline a second time.
        self.slice_view._segmentations = [dict(item) for item in segmentations]
        self.slice_view.set_volume(source_view.image, source_view.info)
        lo, hi = self.slice_view.slice_range()
        self.slider.setRange(lo, hi)
        self._configure_window_level_sliders()
        self._loading = False
        self.slice_view.set_slice(source_view.current_slice())
        if viewer is not None and hasattr(viewer, "point_annotations"):
            self.slice_view.set_points(viewer.point_annotations())
        if viewer is not None and hasattr(viewer, "ablation_needle"):
            self.slice_view.set_needle(viewer.ablation_needle())
        if viewer is not None and hasattr(viewer, "ablation_zone_polydata"):
            self.slice_view.set_ablation_zone(viewer.ablation_zone_polydata())
        if viewer is not None and hasattr(viewer, "_crosshair_payload"):
            self.slice_view.set_crosshair(viewer._crosshair_payload())
        # Visibility is owned by _show_expanded_slice: the overlay must only
        # become visible after the scene above is fully built.

    def sync_segmentations_from_parent(self):
        viewer = self.parent()
        if viewer is not None and hasattr(viewer, "slice_segmentations"):
            self.slice_view.set_segmentations(viewer.slice_segmentations())

    def refresh(self):
        if self.isVisible():
            self.slice_view.refresh()

    def set_points(self, points):
        self.slice_view.set_points(points)

    def set_needle(self, needle):
        self.slice_view.set_needle(needle)

    def set_ablation_zone(self, polydata):
        self.slice_view.set_ablation_zone(polydata)

    def set_segmentations(self, segmentations):
        self.slice_view.set_segmentations(segmentations)

    def set_crosshair(self, payload):
        self.slice_view.set_crosshair(payload)

    def collapse(self):
        if self.source_view is not None and self.slice_view.image is not None:
            self.source_view.set_slice(self.slice_view.current_slice())
        self.setVisible(False)
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
            self.source_view.set_slice(index)

    def _configure_window_level_sliders(self):
        if self.slice_view.image is None:
            return
        lo, hi = self.slice_view.info.get("scalar_range", (0.0, 255.0))
        span = max(1.0, float(hi) - float(lo))
        window, level = self.slice_view.window_level()
        self._syncing_window_level = True
        self._wl_default_window = max(1.0, window)
        self._wl_default_level = level
        self._wl_window_min = max(1.0, self._wl_default_window * 0.25)
        self._wl_window_max = max(self._wl_default_window * 3.0, self._wl_default_window + 1.0)
        self._wl_level_span = max(self._wl_default_window, span * 0.5, 100.0)
        for slider in (self.window_slider, self.level_slider):
            slider.setRange(0, 100)
            slider.set_default_value(50)
            slider.setValue(50)
        self._syncing_window_level = False

    def _sync_window_level_controls(self, window, level):
        if self._syncing_window_level:
            return
        self._syncing_window_level = True
        self.window_slider.setValue(self._slider_from_window(window))
        self.level_slider.setValue(self._slider_from_level(level))
        self._syncing_window_level = False

    def _on_window_slider(self, value):
        if self._syncing_window_level or self.slice_view.image is None:
            return
        self.slice_view.set_window_level(window=self._window_from_slider(value))

    def _on_level_slider(self, value):
        if self._syncing_window_level or self.slice_view.image is None:
            return
        self.slice_view.set_window_level(level=self._level_from_slider(value))

    def _window_from_slider(self, value):
        value = max(0.0, min(100.0, float(value)))
        if value <= 50.0:
            t = value / 50.0
            return self._wl_window_min + (self._wl_default_window - self._wl_window_min) * t
        t = (value - 50.0) / 50.0
        return self._wl_default_window + (self._wl_window_max - self._wl_default_window) * t

    def _level_from_slider(self, value):
        value = max(0.0, min(100.0, float(value)))
        return self._wl_default_level + ((value - 50.0) / 50.0) * self._wl_level_span

    def _slider_from_window(self, window):
        window = max(1.0, float(window))
        if window <= self._wl_default_window:
            denom = max(1.0, self._wl_default_window - self._wl_window_min)
            value = 50.0 * (window - self._wl_window_min) / denom
        else:
            denom = max(1.0, self._wl_window_max - self._wl_default_window)
            value = 50.0 + 50.0 * (window - self._wl_default_window) / denom
        return int(round(max(0.0, min(100.0, value))))

    def _slider_from_level(self, level):
        denom = max(1.0, self._wl_level_span)
        value = 50.0 + 50.0 * (float(level) - self._wl_default_level) / denom
        return int(round(max(0.0, min(100.0, value))))

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

    def set_volume(self, image, info):
        self.image = image
        self.info = info
        for view in self.views:
            view.set_volume(image, info)

    def set_points(self, points):
        for view in self.views:
            view.set_points(points)

    def set_needle(self, needle):
        for view in self.views:
            view.set_needle(needle)

    def set_ablation_zone(self, polydata):
        for view in self.views:
            view.set_ablation_zone(polydata)

    def set_segmentations(self, segmentations):
        for view in self.views:
            view.set_segmentations(segmentations)

    def set_crosshair(self, payload):
        for view in self.views:
            view.set_crosshair(payload)

    def refresh(self):
        for view in self.views:
            view.refresh()
