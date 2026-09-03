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
  │ (336px 宽)  │  (VolumeViewer，占满剩余空间)    │
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
  - 面板宽度：修改 _build_operation_panel() 中 scroll.setFixedWidth(336)
  - 标题文字：修改 _build_header() 中 title/subtitle 的 setText()
  - 添加新的面板分区：参照 _build_panel() 中现有分区的模式添加
  - 组织列表：直接随内容自适应高度，全部显示（无内层滚动条）
  - 菜单文字和图标：修改 _build_menubar() 中的 addAction() 文字
============================================================
"""

import logging
import math
import os
import re
import shutil
import sys
import tempfile
import threading
from datetime import datetime

from PySide6 import QtCore, QtGui, QtWidgets

from . import (ablation, loader, microwave_ablator, needle_planning,
               network_analyzer, planning_report, presets, rtx_telemetry,
               segmentation, serial_connection, style)
from .viewer import VolumeViewer

log = logging.getLogger(__name__)

# 不透明/透明模式预设对应的总量不透明度系数
# 不透明模式：总量 × 1.0（使用滑块设置的值）
# 透明模式 / 分割完成：直接拉到滑块最低值（最透），方便透视内部结构与分割
OPAQUE_SCALE = 1.0
# 不透明度滑块上的参考刻度位置（约 15%）：透视分割时的推荐透明度
OPACITY_MARK_VALUE = 15
# 导入浏览器的受限根目录。部署时可通过 CTTO3D_IMPORT_ROOT 环境变量指定；
# 默认只允许访问项目内的“CT_DATA”目录及其子目录。
IMPORT_ROOT_DIRECTORY = os.path.realpath(os.environ.get(
    "CTTO3D_IMPORT_ROOT",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "CT_DATA"),
))


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


def _scroll_parent_from_wheel(widget, event):
    """把输入控件上的滚轮动作转交给最近的外层滚动区域。"""
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, QtWidgets.QAbstractScrollArea):
            pixel_delta = event.pixelDelta()
            angle_delta = event.angleDelta()
            use_horizontal = (
                abs(pixel_delta.x()) > abs(pixel_delta.y())
                if not pixel_delta.isNull()
                else abs(angle_delta.x()) > abs(angle_delta.y()))
            bar = (parent.horizontalScrollBar() if use_horizontal
                   else parent.verticalScrollBar())
            delta = pixel_delta.x() if use_horizontal else pixel_delta.y()
            if not delta:
                angle = angle_delta.x() if use_horizontal else angle_delta.y()
                steps = angle / 120.0
                delta = steps * max(1, bar.singleStep()) * 3
            if delta and bar.maximum() > bar.minimum():
                bar.setValue(int(round(bar.value() - delta)))
                event.accept()
                return
        parent = parent.parentWidget()
    event.ignore()


class NoWheelDoubleSpinBox(QtWidgets.QDoubleSpinBox):
    """数值框不改值，并把滚轮事件继续交给外层滚动面板。"""

    def wheelEvent(self, event):
        _scroll_parent_from_wheel(self, event)


class NoWheelComboBox(QtWidgets.QComboBox):
    """下拉框不切换选项，并把滚轮事件继续交给外层滚动面板。"""

    def wheelEvent(self, event):
        _scroll_parent_from_wheel(self, event)


class FrostedCenterOverlay(QtWidgets.QWidget):
    """主窗口内部的全屏毛玻璃覆盖层：抓屏模糊 + 深色罩 + 卡片动画。

    动画与启动界面同风格：卡片从居中位置下方一点轻轻上浮浮现（快照平移，
    全程原尺寸清晰），卡片同步淡入（仅卡片本身透明度，不触发窗口级
    重合成，动画流畅）；收起时向下收回并淡出。
    每次打开时重新抓取当前界面，保证病例、分割和视角变化后背景不陈旧；
    单次显示和动画期间仍复用同一张模糊背景。
    """

    dismissed = QtCore.Signal()
    _BLUR_RADIUS = 10.0
    # 1/3 缩略图模糊：模糊像素数降为 1/2 方案的 2.25 倍快。放大回全屏时
    # 两处调用方都走 SmoothTransformation，浅色纯色区的像素格会被平滑插值
    # 抹掉；深色主题（默认）下更不可见。背景本身是全模糊的，缩略图放大后
    # 的轻微柔化符合毛玻璃观感。
    _BLUR_DOWNSCALE = 3
    _POP_MS = 340                # 从下方浮现动画时长（ms）
    _RETRACT_MS = 260            # 向下收回动画时长（ms）
    _CARD_RADIUS = 16            # 动画期间裁剪圆角（与 QSS border-radius 一致）

    @staticmethod
    def _slide_offset(height):
        """浮现滑行距离：屏幕高度的 1/6（下限 72px）。

        卡片从居中位置往下一点的地方浮现，而不是从屏幕底部长途滑入。
        """
        return max(72, height // 6)

    def __init__(self, parent=None, object_name="FrostedCenterOverlay",
                 card_width=410, card_height=None,
                 card_object_name="ExitDialogCard", card_radius=16):
        # 必须作为主窗口的原生子窗口存在，不能再创建顶层 Qt.Tool 窗口。
        # Windows 在全屏主窗口上激活一个非全屏 Tool 时会重新唤出系统任务栏，
        # 正是控制弹层底部露出任务栏的原因。原生子窗口既不会进入任务栏的
        # 顶层窗口管理，又能可靠盖住 QVTK 等原生子画布。
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self._host = parent
        self._card_radius = int(card_radius)
        self._card_width = int(card_width)
        self._card_height = (
            None if card_height is None else int(card_height))
        self._bg = QtGui.QPixmap()
        self._bg_full = QtGui.QPixmap()  # 整屏尺寸背景（逐帧 1:1 拷贝用）
        self._scrim = QtGui.QColor(15, 23, 42, 140)
        self._progress_anim = None  # QVariantAnimation：滑出进度（0→1 同时驱动位置与淡入）
        self._progress = 0.0        # 当前进度
        self._card_snapshot = None  # 动画期间绘制的卡片快照（QPixmap）
        self._card_live_rect = None # 快照对应的最终矩形（绘制定位用）
        self._dismissing = False
        self._theme = None
        self._bg_cache_key = None

        card = QtWidgets.QFrame(self, objectName=card_object_name)
        self._card = card
        card.setFixedWidth(self._card_width)
        self._card_lay = QtWidgets.QVBoxLayout(card)
        self._card_lay.setContentsMargins(30, 26, 30, 26)
        self._card_lay.setSpacing(14)
        self._populate_card(self._card_lay)
        self.hide()

    def _populate_card(self, card_lay):
        """子类填充卡片内容。"""

    def _sync_scrim(self):
        try:
            scheme = style.load_theme()
        except Exception:
            scheme = "dark"
        self._theme = scheme
        self._scrim = self._scrim_color(scheme)

    @staticmethod
    def _scrim_color(scheme):
        """所有居中覆盖层共用的主题遮罩颜色。"""
        return QtGui.QColor(
            15, 23, 42, 120 if scheme == "light" else 140)

    def _sync_geometry(self):
        host = self._host
        if host is None:
            return
        # 子窗口坐标属于 host，必须使用本地 rect；frameGeometry 是全局桌面
        # 坐标，套在子窗口上会产生位置偏移并再次留下未覆盖区域。
        geo = host.rect()
        if geo != self.geometry():
            # 宿主移动/缩放后，旧模糊背景快照失效，下次打开时重新抓取
            self._bg_cache_key = None
            self.setGeometry(geo)

    def _card_natural_size(self):
        self._card.setMinimumSize(0, 0)
        self._card.setMaximumSize(16777215, 16777215)
        # 桌面端使用设计尺寸；窗口较小时保留安全边距并自动收缩。
        target_width = min(self._card_width, max(1, self.width() - 64))
        self._card.setFixedWidth(target_width)
        self._card.adjustSize()
        natural_height = max(1, self._card.sizeHint().height())
        target_height = natural_height
        if self._card_height is not None:
            target_height = max(target_height, self._card_height)
        target_height = min(target_height, max(1, self.height() - 64))
        return QtCore.QSize(target_width, target_height)

    def _lock_card_geometry(self, target):
        """固定最终尺寸，避免布局在动画结束后把卡片缩回自然高度。"""
        self._card.setMinimumSize(target.size())
        self._card.setMaximumSize(target.size())
        self._card.setGeometry(target)

    def _card_target_rect(self):
        size = self._card_natural_size()
        x = (self.width() - size.width()) // 2
        y = (self.height() - size.height()) // 2
        return QtCore.QRect(x, y, size.width(), size.height())

    def _get_frosted_background(self, host):
        """抓屏模糊背景（在单次显示期间缓存）。

        整屏抓取+模糊是弹窗最贵的一步（Windows 上约 20~50ms）。弹窗打开期间
        背景保持静止，因此同一次显示期间按尺寸+主题复用；下一次打开会主动
        使缓存失效并重抓，避免病例切换、分割或相机变化后仍显示旧画面。
        同时预生成一张整屏尺寸的背景图：动画逐帧直接 1:1 拷贝，避免每帧
        全屏平滑拉伸（这也是动画卡顿的来源之一）。
        """
        key = (host.width(), host.height(), self._theme)
        if key != self._bg_cache_key:
            self._bg = self._capture_frosted(host)
            if not self._bg.isNull():
                self._bg_full = self._bg.scaled(
                    host.size(),
                    QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                    # 仅在缓存失效时执行一次，动画绘制仍是全尺寸 1:1 拷贝；
                    # 因而不会增加逐帧开销，也不会留下最近邻放大的网格。
                    QtCore.Qt.TransformationMode.SmoothTransformation)
            else:
                self._bg_full = QtGui.QPixmap()
            self._bg_cache_key = key
        return self._bg

    def prewarm_background(self):
        """空闲时预抓取模糊背景，让首次打开也没有抓屏停顿。"""
        host = self._host
        if host is None:
            return
        self._sync_scrim()
        self._sync_geometry()
        self._get_frosted_background(host)

    def _stop_progress_anim(self):
        if self._progress_anim is not None:
            self._progress_anim.stop()
            self._progress_anim.deleteLater()
            self._progress_anim = None

    def _run_progress_anim(self, start, end, ms, easing, on_finish=None):
        """滑出进度动画：进度 0→1 驱动卡片位置（上滑）与卡片淡入。

        与启动界面的过渡一致：OutCubic 上滑浮现 / InCubic 下滑淡出。
        淡入只在卡片快照绘制时用 painter 透明度实现——不碰窗口级
        setWindowOpacity（那会让全屏窗口变成分层窗口，逐帧 DWM 重合成
        整个 1080p 窗口，正是动画卡顿的根源）。
        """
        self._stop_progress_anim()
        anim = QtCore.QVariantAnimation(self)
        anim.setDuration(ms)
        anim.setStartValue(float(start))
        anim.setEndValue(float(end))
        anim.setEasingCurve(easing)
        anim.valueChanged.connect(self._on_progress_changed)
        if on_finish is not None:
            anim.finished.connect(on_finish)
        self._progress_anim = anim
        self._progress = float(start)
        self._on_progress_changed(self._progress)
        anim.start()

    def _on_progress_changed(self, value):
        self._progress = max(0.0, min(1.0, float(value)))
        self.update()

    def _grab_card_snapshot(self, target):
        """按最终尺寸渲染卡片快照，供动画期间绘制。

        滑动动画中快照全程原尺寸 1:1 绘制（只平移不缩放），文字始终清晰，
        卡片内容也不会在动画过程中被重新布局。
        """
        self._card.setMinimumSize(target.size())
        self._card.setMaximumSize(target.size())
        self._card.setGeometry(target)
        lay = self._card.layout()
        if lay is not None:
            lay.activate()
        self._card_snapshot = self._card.grab()
        self._card_live_rect = QtCore.QRect(target)
        self._card.hide()
        self.update()

    def _show_card_live(self):
        """动画结束：撤下快照，让真实卡片接管显示并锁定最终尺寸。"""
        self._card_snapshot = None
        self._card.show()
        self._card.raise_()
        self._lock_card_geometry(self._card_live_rect)
        self.update()
        # 动画期间卡片是隐藏的，焦点回到弹层；结束后把焦点交还给卡片按钮
        self._on_presented()

    def _play_card_slide(self):
        """从居中位置下方一点浮现：快照原尺寸平移 + 卡片淡入。"""
        self._stop_progress_anim()
        self._grab_card_snapshot(self._card_target_rect())
        self._run_progress_anim(
            0.0, 1.0, self._POP_MS,
            QtCore.QEasingCurve.Type.OutCubic,
            on_finish=self._show_card_live)

    @classmethod
    def _soft_blur(cls, image):
        """在缩略图上做模糊，返回的**仍是缩略图**。

        以前这里会把结果按全分辨率插值放大再返回，1920x1080 下光这一步就要几
        毫秒、4K 下更贵；而背景本来就是模糊的，直接让 paintEvent 拉伸绘制，
        肉眼看不出区别。
        """
        if image.isNull():
            return image
        pm = QtGui.QPixmap.fromImage(image)
        pm.setDevicePixelRatio(1.0)
        scene = QtWidgets.QGraphicsScene()
        scene.setSceneRect(0, 0, pm.width(), pm.height())
        item = scene.addPixmap(pm)
        effect = QtWidgets.QGraphicsBlurEffect()
        effect.setBlurRadius(cls._BLUR_RADIUS)
        # 背景只生成一次并缓存，优先质量可避免浅色区域的分块/条带伪影。
        effect.setBlurHints(QtWidgets.QGraphicsBlurEffect.BlurHint.QualityHint)
        item.setGraphicsEffect(effect)
        out_small = QtGui.QPixmap(pm.size())
        out_small.setDevicePixelRatio(1.0)
        out_small.fill(QtGui.QColor(15, 23, 42))
        painter = QtGui.QPainter(out_small)
        painter.drawPixmap(0, 0, pm)
        scene.render(painter, QtCore.QRectF(out_small.rect()), scene.sceneRect())
        painter.end()
        scene.clear()
        return out_small.toImage()

    @classmethod
    def _blur_captured_pixmap(cls, grabbed):
        """统一的截图降采样与毛玻璃处理，返回缩略分辨率 QPixmap。"""
        if grabbed.isNull():
            return QtGui.QPixmap()
        k = max(1, cls._BLUR_DOWNSCALE)
        small = grabbed.scaled(
            max(1, grabbed.width() // k), max(1, grabbed.height() // k),
            QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        image = small.toImage().convertToFormat(
            QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        frosted = cls._soft_blur(image)
        pixmap = QtGui.QPixmap.fromImage(frosted)
        pixmap.setDevicePixelRatio(1.0)
        return pixmap

    def _capture_frosted(self, host):
        screen = host.screen() or QtWidgets.QApplication.primaryScreen()
        geo = host.frameGeometry()
        grabbed = QtGui.QPixmap()
        if screen is not None:
            # 优先抓窗口本身(PrintWindow/PW_RENDERFULLCONTENT)：能拿到
            # DWM 合成后的画面，包括 VTK 的 GPU 表面；桌面 BitBlt 抓
            # OpenGL 区域可能得到黑块，且无多屏坐标换算问题。
            grabbed = screen.grabWindow(int(host.winId()))
            if grabbed.isNull():
                # 回退：抓桌面区域。grabWindow(0) 的 x/y 是该屏幕的本地
                # 坐标，而 frameGeometry() 返回虚拟桌面的全局坐标。副屏
                # （原点不在 (0,0)，如右侧竖屏 1920×1080+(-510)）上两者
                # 不一致：直接传全局坐标会抓到桌面其他区域（黑块/壁纸）。
                # 先换算成屏幕本地坐标再抓。
                origin = screen.geometry().topLeft()
                grabbed = screen.grabWindow(
                    0, geo.x() - origin.x(), geo.y() - origin.y(),
                    geo.width(), geo.height())
        if grabbed.isNull():
            grabbed = host.grab()
        if grabbed.isNull():
            return QtGui.QPixmap()

        # 先降采样再做格式转换/模糊：全分辨率转换是这里最贵的一步。
        pixmap = self._blur_captured_pixmap(grabbed)
        painter = QtGui.QPainter(pixmap)
        painter.fillRect(pixmap.rect(), self._scrim)
        painter.end()
        return pixmap

    def present(self):
        host = self._host
        if host is None:
            return
        self._stop_progress_anim()
        self._dismissing = False
        self._sync_scrim()
        self._sync_geometry()
        # 窗口尺寸和主题不变并不代表画面没有变化。病例加载、分割叠加、
        # 相机旋转和切片移动都不会改变旧 cache key，必须在每次弹出前重抓。
        self._bg_cache_key = None
        self._bg = self._get_frosted_background(host)
        self._card.hide()
        self.show()
        self.raise_()
        self.activateWindow()
        self.repaint()
        self._play_card_slide()
        self._on_presented()

    def _on_presented(self):
        """子类可覆盖：弹出后聚焦等。"""

    def dismiss(self):
        """收起弹层：卡片向下收回并淡出，再整体隐藏。

        收回动画用 InCubic 缓动（先慢后快），符合手机 App 收起手势的观感。
        """
        if self._dismissing:
            return
        self._stop_progress_anim()
        if not self.isVisible():
            self._finish_dismiss()
            return
        if self._card_snapshot is None:
            # 动画已结束、真实卡片在显示：先抓一张快照再滑回
            if not self._card.isVisible():
                self._finish_dismiss()
                return
            self._grab_card_snapshot(self._card_live_rect or self._card.geometry())
        self._dismissing = True
        self._run_progress_anim(
            self._progress, 0.0, self._RETRACT_MS,
            QtCore.QEasingCurve.Type.InCubic,
            on_finish=self._finish_dismiss)

    def _finish_dismiss(self):
        self._dismissing = False
        self._stop_progress_anim()
        self.hide()
        # _bg 保留给缓存：下次打开直接复用，不再重新抓屏
        self._card_snapshot = None
        self._card_live_rect = None
        self._progress = 0.0
        self._card.setMinimumSize(0, 0)
        self._card.setMaximumSize(16777215, 16777215)
        self._card.setFixedWidth(self._card_width)
        # 原生覆盖层隐藏后，底下的 VTK 原生表面不会随 Qt 重绘请求刷新，
        # 屏幕上会残留覆盖层最后一帧。强制刷新主窗口与 3D 视图。
        self._refresh_host_after_dismiss()
        self.dismissed.emit()

    def _refresh_host_after_dismiss(self):
        """覆盖层隐藏后强制宿主窗口与 3D 视图重绘，消除关闭残影。"""
        host = self._host
        if host is None:
            return
        host.update()
        viewer = getattr(host, "viewer", None)
        if viewer is not None:
            # 延迟到事件循环：先让 hide 完成合成，再触发 VTK 重渲染
            QtCore.QTimer.singleShot(0, viewer.render)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.isVisible():
            return
        if (self._progress_anim is not None
                and self._progress_anim.state() == QtCore.QAbstractAnimation.State.Running):
            return
        if self._card.isVisible():
            self._lock_card_geometry(self._card_target_rect())

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        if not self._bg_full.isNull():
            # 整屏尺寸背景：1:1 拷贝（动画逐帧时无平滑拉伸开销）
            painter.drawPixmap(0, 0, self._bg_full)
        elif not self._bg.isNull():
            # 回退：1/3 分辨率模糊图拉伸铺满
            painter.setRenderHint(
                QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(self.rect(), self._bg)
        else:
            painter.fillRect(self.rect(), QtGui.QColor(15, 23, 42, 220))
        # 动画期间画卡片快照：从居中位置下方一点浮现，原尺寸 1:1 绘制
        # （无缩放，文字清晰）。进度 p：卡片 y = 目标 y + (1-p)×滑距。
        # p=1 时与最终矩形完全重合，无缝切换真实卡片。
        # grab() 抓取的快照不带 QSS 圆角（四角是实心直角），因此绘制时
        # 用与 QSS 一致的 16px 圆角裁剪路径，保证动画全程圆角不倒角。
        # 淡入用 painter 透明度只作用于卡片本身，不触发窗口级重合成。
        if (self._card_snapshot is not None
                and self._card_live_rect is not None):
            p = max(0.0, min(1.0, self._progress))
            rect = self._card_live_rect
            offset = (1.0 - p) * self._slide_offset(self.height())
            dest = QtCore.QRectF(
                rect.x(), rect.y() + offset,
                rect.width(), rect.height())
            radius = min(self._card_radius,
                         dest.width() / 2.0, dest.height() / 2.0)
            path = QtGui.QPainterPath()
            path.addRoundedRect(dest, radius, radius)
            painter.save()
            painter.setRenderHint(
                QtGui.QPainter.RenderHint.Antialiasing, True)
            painter.setClipPath(path)
            painter.setOpacity(p)
            painter.drawPixmap(
                dest, self._card_snapshot,
                QtCore.QRectF(self._card_snapshot.rect()))
            painter.restore()

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Escape:
            self.dismiss()
            event.accept()
            return
        super().keyPressEvent(event)


class ExitConfirmOverlay(FrostedCenterOverlay):
    """退出确认层。"""

    confirmed = QtCore.Signal()
    cancelled = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(
            parent, object_name="ExitConfirmOverlay", card_width=410)

    def _populate_card(self, card_lay):
        title = QtWidgets.QLabel("退出软件", objectName="ExitDialogTitle")
        title.setAlignment(QtCore.Qt.AlignCenter)
        question = QtWidgets.QLabel("确认退出？", objectName="ExitDialogQuestion")
        question.setAlignment(QtCore.Qt.AlignCenter)
        card_lay.addWidget(title)
        card_lay.addWidget(question)
        card_lay.addSpacing(4)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(12)
        self.btn_confirm = QtWidgets.QPushButton("确认", objectName="ExitConfirm")
        self.btn_cancel = QtWidgets.QPushButton("取消", objectName="ExitCancel")
        self.btn_confirm.setMinimumHeight(40)
        self.btn_cancel.setMinimumHeight(40)
        self.btn_cancel.setDefault(True)
        self.btn_confirm.clicked.connect(self.confirmed.emit)
        self.btn_cancel.clicked.connect(self.cancelled.emit)
        buttons.addWidget(self.btn_confirm)
        buttons.addWidget(self.btn_cancel)
        card_lay.addLayout(buttons)

    def _on_presented(self):
        self.btn_cancel.setFocus(QtCore.Qt.PopupFocusReason)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Escape:
            self.cancelled.emit()
            event.accept()
            return
        super(FrostedCenterOverlay, self).keyPressEvent(event)


class PlanningAlertOverlay(FrostedCenterOverlay):
    """针道规划提醒层，与退出确认框复用同一套视觉和动画。"""

    accepted = QtCore.Signal()
    cancelled = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(
            parent, object_name="PlanningAlertOverlay", card_width=460)

    def _populate_card(self, card_lay):
        self.title = QtWidgets.QLabel(objectName="ExitDialogTitle")
        self.title.setAlignment(QtCore.Qt.AlignCenter)
        self.message = QtWidgets.QLabel(objectName="ExitDialogQuestion")
        self.message.setAlignment(QtCore.Qt.AlignCenter)
        self.message.setWordWrap(True)
        card_lay.addWidget(self.title)
        card_lay.addWidget(self.message)
        card_lay.addSpacing(4)

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(12)
        self.btn_confirm = QtWidgets.QPushButton(objectName="ExitConfirm")
        self.btn_cancel = QtWidgets.QPushButton(objectName="ExitCancel")
        self.btn_confirm.setMinimumHeight(40)
        self.btn_cancel.setMinimumHeight(40)
        self.btn_confirm.clicked.connect(self.accepted.emit)
        self.btn_cancel.clicked.connect(self.cancelled.emit)
        buttons.addWidget(self.btn_confirm)
        buttons.addWidget(self.btn_cancel)
        card_lay.addLayout(buttons)

    def configure(self, title, message, confirm_text="知道了", cancel_text=None):
        """更新提醒内容；不传 cancel_text 时显示单按钮提示框。"""
        self.title.setText(str(title))
        self.message.setText(str(message))
        self.btn_confirm.setText(str(confirm_text))
        self.btn_cancel.setVisible(cancel_text is not None)
        if cancel_text is not None:
            self.btn_cancel.setText(str(cancel_text))
            self.btn_cancel.setDefault(True)
            self.btn_confirm.setDefault(False)
        else:
            self.btn_cancel.setDefault(False)
            self.btn_confirm.setDefault(True)

    def _on_presented(self):
        button = self.btn_cancel if self.btn_cancel.isVisible() else self.btn_confirm
        button.setFocus(QtCore.Qt.PopupFocusReason)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key_Escape:
            self.cancelled.emit()
            event.accept()
            return
        super(FrostedCenterOverlay, self).keyPressEvent(event)


class MwaControlOverlay(FrostedCenterOverlay):
    """微波消融仪控制弹窗：从居中位置下方浮现，向下收回。"""

    def __init__(self, parent=None, content=None):
        self._content = content
        super().__init__(
            parent,
            object_name="MwaControlOverlay",
            card_width=1100,
            card_height=520,
            card_object_name="MwaDialogCard",
        )

    def _populate_card(self, card_lay):
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(12)
        accent = QtWidgets.QFrame(objectName="MwaTitleAccent")
        accent.setFixedSize(4, 38)
        header.addWidget(accent, 0, QtCore.Qt.AlignVCenter)
        title_col = QtWidgets.QVBoxLayout()
        title_col.setSpacing(1)
        title = QtWidgets.QLabel("微波消融仪主机", objectName="MwaDialogTitle")
        subtitle = QtWidgets.QLabel(
            "MICROWAVE ABLATION SYSTEM", objectName="MwaDialogSubtitle")
        # QSS 不支持 letter-spacing，用 QFont 拉开英文副标字距
        font = subtitle.font()
        font.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 2.0)
        subtitle.setFont(font)
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        header.addLayout(title_col, 1)
        status_chip = getattr(self._content, "mwa_status_chip", None)
        if status_chip is not None:
            header.addWidget(status_chip, 0, QtCore.Qt.AlignVCenter)
        self.btn_collapse = QtWidgets.QPushButton("收起", objectName="OverlayCollapse")
        self.btn_collapse.setFixedHeight(38)
        self.btn_collapse.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_collapse.clicked.connect(self.dismiss)
        header.addWidget(self.btn_collapse, 0, QtCore.Qt.AlignTop)
        card_lay.addLayout(header)
        if self._content is not None:
            self._content.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Expanding,
            )
            card_lay.addWidget(self._content)

    def _on_presented(self):
        self.btn_collapse.setFocus(QtCore.Qt.PopupFocusReason)


class _VnaSendEmitter(QtCore.QObject):
    """网分 SCPI 发送的跨线程回调桥：工作线程算完响应后用 Qt 信号回主线程。"""

    done = QtCore.Signal(str, object)


class _VnaConnectEmitter(QtCore.QObject):
    """网分连接的跨线程回调桥：工作线程完成阻塞连接后回主线程收尾。"""

    done = QtCore.Signal()


class _EntrySearchEmitter(QtCore.QObject):
    """入针点搜索的跨线程回调桥：后台线程算完推荐结果后回主线程落点。"""

    done = QtCore.Signal(object)


class LinkChannelMonitor(QtWidgets.QFrame):
    """单个链路通道的收发监视器：工具栏(RX/TX 计数灯) + 控制台 + 命令行。

    通用串口 / 微波消融仪 / 网络分析仪各配一个，收发互不串台：
      · RX 行蓝灰色，TX 行品牌青绿高亮（» 前缀），一眼可分；
      · 时间戳开关给每行加 [时:分:秒]；追加换行只影响串口发送；
      · 控制台两种主题都保持深色玻璃（终端惯例）。
    实际发送由外部完成：点击发送只发 send_requested 信号，外部调用
    note_tx() 记日志并清空输入，发送失败则什么都不写。
    """

    _RX_COLOR = "#C9D6E8"
    _TX_COLOR = "#5EEAD4"
    send_requested = QtCore.Signal(str)

    def __init__(self, parent=None, show_newline=True, show_hex_modes=False,
                 placeholder="输入指令后回车发送",
                 rx_label="RX", tx_label="TX"):
        super().__init__(parent, objectName="LinkMonitor")
        self.rx_chunks = 0
        self.tx_chunks = 0
        # 计数前缀：串口用 RX/TX 行话，网分改用 接收/发送。
        self._rx_label = rx_label
        self._tx_label = tx_label
        self._text_placeholder = placeholder
        self._text_newline_checked = True

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(12, 9, 12, 10)
        lay.setSpacing(7)

        # 工具栏：收发计数灯 + 选项 + 清空
        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(8)
        self.rx_led = QtWidgets.QLabel(objectName="SerialRxLed")
        self.rx_led.setFixedSize(8, 8)
        self.rx_led.setProperty("active", False)
        self.rx_led.setToolTip("收到数据时闪烁")
        self.rx_count = QtWidgets.QLabel(
            "%s 0" % rx_label, objectName="SerialRxBadge")
        self.tx_led = QtWidgets.QLabel(objectName="SerialTxLed")
        self.tx_led.setFixedSize(8, 8)
        self.tx_led.setProperty("active", False)
        self.tx_led.setToolTip("发送数据时闪烁")
        self.tx_count = QtWidgets.QLabel(
            "%s 0" % tx_label, objectName="SerialTxBadge")
        self.timestamp_chk = QtWidgets.QCheckBox("时间戳")
        self.timestamp_chk.setToolTip("在每条日志前加上 [时:分:秒]")
        self.rx_hex_chk = QtWidgets.QCheckBox("HEX 接收")
        self.rx_hex_chk.setToolTip("开启后按原始字节显示接收数据")
        self.tx_hex_chk = QtWidgets.QCheckBox("HEX 发送")
        self.tx_hex_chk.setToolTip("开启后把输入内容解析为十六进制字节")
        self.newline_chk = QtWidgets.QCheckBox("追加换行")
        self.newline_chk.setChecked(True)
        bar.addWidget(self.rx_led, 0, QtCore.Qt.AlignVCenter)
        bar.addWidget(self.rx_count, 0, QtCore.Qt.AlignVCenter)
        bar.addWidget(self.tx_led, 0, QtCore.Qt.AlignVCenter)
        bar.addWidget(self.tx_count, 0, QtCore.Qt.AlignVCenter)
        bar.addStretch(1)
        bar.addWidget(self.timestamp_chk, 0, QtCore.Qt.AlignVCenter)
        bar.addWidget(self.rx_hex_chk, 0, QtCore.Qt.AlignVCenter)
        bar.addWidget(self.tx_hex_chk, 0, QtCore.Qt.AlignVCenter)
        bar.addWidget(self.newline_chk, 0, QtCore.Qt.AlignVCenter)
        if not show_hex_modes:
            self.rx_hex_chk.hide()
            self.tx_hex_chk.hide()
        if not show_newline:
            self.newline_chk.hide()
        self.clear_btn = QtWidgets.QPushButton("清空", objectName="LinkGhost")
        self.clear_btn.setIcon(MainWindow._serial_icon("clear", "#7C8792"))
        self.clear_btn.setIconSize(QtCore.QSize(14, 14))
        self.clear_btn.setToolTip("清空收发窗口")
        self.clear_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.clear_btn.setMinimumHeight(26)
        self.clear_btn.clicked.connect(self.clear)
        bar.addWidget(self.clear_btn, 0, QtCore.Qt.AlignVCenter)
        lay.addLayout(bar)

        # 控制台
        holder = QtWidgets.QFrame(objectName="LinkLogHolder")
        holder_lay = QtWidgets.QVBoxLayout(holder)
        holder_lay.setContentsMargins(9, 6, 9, 6)
        self.rx_view = QtWidgets.QPlainTextEdit(objectName="SerialRxLog")
        self.rx_view.setReadOnly(True)
        self.rx_view.setUndoRedoEnabled(False)
        self.rx_view.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)
        self.rx_view.setMaximumBlockCount(2000)
        self.rx_view.setPlaceholderText("等待接收数据…")
        # 三路监视器同时展示时限制默认 sizeHint，避免弹窗被日志框撑成竖长形。
        self.rx_view.setMinimumHeight(82)
        self.rx_view.setMaximumHeight(96)
        holder_lay.addWidget(self.rx_view)
        lay.addWidget(holder, 1)

        # 命令行
        send_row = QtWidgets.QHBoxLayout()
        send_row.setContentsMargins(0, 0, 0, 0)
        send_row.setSpacing(8)
        self.send_edit = QtWidgets.QLineEdit()
        self.send_edit.setPlaceholderText(placeholder)
        self.send_edit.setMinimumHeight(30)
        self.send_edit.returnPressed.connect(self._on_send_clicked)
        self.tx_hex_chk.toggled.connect(self._sync_tx_input_mode)
        self.send_btn = QtWidgets.QPushButton("发送", objectName="PrimaryCompact")
        self.send_btn.setIcon(MainWindow._serial_icon("send"))
        self.send_btn.setIconSize(QtCore.QSize(14, 14))
        self.send_btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.send_btn.setFixedWidth(76)
        self.send_btn.setMinimumHeight(30)
        self.send_btn.clicked.connect(self._on_send_clicked)
        send_row.addWidget(self.send_edit, 1)
        send_row.addWidget(self.send_btn)
        lay.addLayout(send_row)

        # LED 闪烁：瞬时置位后单次定时器复位
        self._rx_led_timer = QtCore.QTimer(self)
        self._rx_led_timer.setSingleShot(True)
        self._rx_led_timer.setInterval(500)
        self._rx_led_timer.timeout.connect(lambda: self._led_off(self.rx_led))
        self._tx_led_timer = QtCore.QTimer(self)
        self._tx_led_timer.setSingleShot(True)
        self._tx_led_timer.setInterval(500)
        self._tx_led_timer.timeout.connect(lambda: self._led_off(self.tx_led))

    def _sync_tx_input_mode(self, enabled):
        if enabled:
            # Binary frames should be exact by default. Preserve the text-mode
            # preference so switching back restores the previous CRLF choice.
            self._text_newline_checked = self.newline_chk.isChecked()
            self.newline_chk.setChecked(False)
            self.send_edit.setPlaceholderText(
                "输入 HEX 字节，如 AA 13 00 FF 或 AA1300FF")
        else:
            self.newline_chk.setChecked(self._text_newline_checked)
            self.send_edit.setPlaceholderText(self._text_placeholder)

    def _on_send_clicked(self):
        text = self.send_edit.text()
        if not text:
            return
        self.send_requested.emit(text)

    def append_rx(self, text):
        """收到数据：接收计数 + 绿灯 + 蓝灰日志。"""
        self.rx_chunks += 1
        self.rx_count.setText("%s %d" % (self._rx_label, self.rx_chunks))
        self._led_on(self.rx_led, self._rx_led_timer)
        self._append(text, self._RX_COLOR)

    def append_info(self, text):
        """系统消息（连接/断开/错误）：不计收发数。"""
        self._append(text, self._RX_COLOR)

    def append_tx(self, text, clear_input=False):
        """记录已发送数据；协议通道可由底层发送信号统一调用。"""
        self.tx_chunks += 1
        self.tx_count.setText("%s %d" % (self._tx_label, self.tx_chunks))
        self._led_on(self.tx_led, self._tx_led_timer)
        self._append("» %s\n" % text, self._TX_COLOR)
        if clear_input:
            self.send_edit.clear()

    def note_tx(self, text):
        """发送成功后调用：记录发送行并清空输入。"""
        self.append_tx(text, clear_input=True)

    def _append(self, text, color_hex):
        if not text:
            return
        if self.timestamp_chk.isChecked():
            stamp = QtCore.QTime.currentTime().toString("[HH:mm:ss] ")
            text = "".join(
                (stamp + seg) if seg else seg
                for seg in text.splitlines(keepends=True))
        fmt = QtGui.QTextCharFormat()
        fmt.setForeground(QtGui.QColor(color_hex))
        cursor = self.rx_view.textCursor()
        cursor.movePosition(QtGui.QTextCursor.MoveOperation.End)
        cursor.insertText(text, fmt)
        self.rx_view.setTextCursor(cursor)
        self.rx_view.ensureCursorVisible()

    def _led_on(self, led, timer):
        if led.property("active") is not True:
            led.setProperty("active", True)
            led.style().unpolish(led)
            led.style().polish(led)
        timer.start()

    @staticmethod
    def _led_off(led):
        if led.property("active") is True:
            led.setProperty("active", False)
            led.style().unpolish(led)
            led.style().polish(led)

    def clear(self):
        self.rx_view.clear()
        self.rx_chunks = 0
        self.tx_chunks = 0
        self.rx_count.setText("%s 0" % self._rx_label)
        self.tx_count.setText("%s 0" % self._tx_label)
        self._led_off(self.rx_led)
        self._led_off(self.tx_led)

    def set_send_enabled(self, enabled):
        self.send_edit.setEnabled(bool(enabled))
        self.send_btn.setEnabled(bool(enabled))
        self.newline_chk.setEnabled(bool(enabled))
        self.tx_hex_chk.setEnabled(bool(enabled))


class SerialControlOverlay(FrostedCenterOverlay):
    """设备连接弹窗：沿用微波消融仪面板的海军蓝仪器设计系统。"""

    def __init__(self, parent=None, content=None):
        self._content = content
        super().__init__(
            parent,
            object_name="SerialControlOverlay",
            card_width=1240,
            card_height=760,
            card_object_name="LinkDialogCard",
            # 与 QSS 中 LinkDialogCard 的 border-radius(18px) 保持一致，
            # 避免动画快照圆角与静止态圆角不一致造成突变。
            card_radius=18,
        )

    def _populate_card(self, card_lay):
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(12)
        accent = QtWidgets.QFrame(objectName="LinkTitleAccent")
        accent.setFixedSize(4, 38)
        header.addWidget(accent, 0, QtCore.Qt.AlignVCenter)
        title_col = QtWidgets.QVBoxLayout()
        title_col.setSpacing(1)
        title = QtWidgets.QLabel("设备连接", objectName="LinkDialogTitle")
        subtitle = QtWidgets.QLabel(
            "SERIAL / NETWORK LINKS", objectName="LinkDialogSubtitle")
        font = subtitle.font()
        font.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 2.0)
        subtitle.setFont(font)
        title_col.addWidget(title)
        title_col.addWidget(subtitle)
        header.addLayout(title_col, 1)
        status_chip = getattr(self._content, "serial_status_chip", None)
        if status_chip is not None:
            header.addWidget(status_chip, 0, QtCore.Qt.AlignVCenter)
        self.btn_collapse = QtWidgets.QPushButton("收起", objectName="OverlayCollapse")
        self.btn_collapse.setFixedHeight(38)
        self.btn_collapse.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_collapse.clicked.connect(self.dismiss)
        header.addWidget(self.btn_collapse, 0, QtCore.Qt.AlignTop)
        card_lay.addLayout(header)
        if self._content is not None:
            self._content.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Expanding,
                QtWidgets.QSizePolicy.Policy.Expanding,
            )
            card_lay.addWidget(self._content, 1)

    def set_link_summary(self, connected_count, total=3):
        """刷新标题栏状态胶囊（复用消融仪 MwaStatusChip）。"""
        content = self._content
        if content is None:
            return
        chip = getattr(content, "serial_status_chip", None)
        label = getattr(content, "serial_link_label", None)
        dot = getattr(content, "serial_summary_dot", None)
        if label is None or chip is None:
            return
        count = max(0, min(int(total), int(connected_count)))
        online = count > 0
        label.setText("%d / %d 已连接" % (count, int(total)))
        for widget, prop, value in (
                (chip, "online", online),
                (label, "online", online),
                (dot, "connected", online)):
            if widget is None:
                continue
            if widget.property(prop) == value:
                continue
            widget.setProperty(prop, value)
            style = widget.style()
            if style is not None:
                style.unpolish(widget)
                style.polish(widget)

    def _on_presented(self):
        self.btn_collapse.setFocus(QtCore.Qt.PopupFocusReason)


class EmbeddedFileDialogOverlay(QtWidgets.QWidget):
    """嵌在主界面上的受限文件浏览器，只允许访问指定根目录及其子目录。"""

    # 与 QSS 中 EmbeddedFileDialogCard 的 border-radius 保持一致
    _CARD_RADIUS = 12

    def __init__(self, parent=None):
        # 必须是主窗口内部的原生子覆盖层。独立 Qt.Tool 在全屏/无边框窗口
        # 上激活时会触发 Windows 重新合成并短暂露出底部黑条或任务栏。
        super().__init__(parent)
        self.setObjectName("EmbeddedFileDialogOverlay")
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self._host = parent
        self._background = QtGui.QPixmap()
        self._bg_cache_key = None
        self._event_loop = None
        self._selected_path = ""
        self._directory_mode = False
        self._mixed_mode = False
        self._root_directory = ""
        self._current_directory = ""
        self._selected_candidate = ""
        self._history = []
        self._allowed_suffixes = ()
        self._card_anim = None
        self._card_target_pos = QtCore.QPoint()
        self._closing = False

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(0)
        outer.addStretch(1)

        card = QtWidgets.QFrame(objectName="EmbeddedFileDialogCard")
        self._card = card
        card.setMinimumSize(600, 380)
        card.setMaximumSize(820, 520)
        card.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding,
        )
        card_lay = QtWidgets.QVBoxLayout(card)
        card_lay.setContentsMargins(18, 14, 18, 18)
        card_lay.setSpacing(10)

        header = QtWidgets.QHBoxLayout()
        header.setSpacing(10)
        self.title_label = QtWidgets.QLabel(objectName="EmbeddedFileDialogTitle")
        self.title_label.setText("导入数据")
        header.addWidget(self.title_label)
        header.addStretch(1)
        close_btn = QtWidgets.QPushButton("×", objectName="EmbeddedFileDialogClose")
        close_btn.setFixedSize(34, 32)
        close_btn.setToolTip("取消")
        close_btn.clicked.connect(self._reject)
        header.addWidget(close_btn)
        card_lay.addLayout(header)

        toolbar = QtWidgets.QFrame(objectName="RestrictedBrowserToolbar")
        toolbar_lay = QtWidgets.QHBoxLayout(toolbar)
        toolbar_lay.setContentsMargins(8, 7, 8, 7)
        toolbar_lay.setSpacing(6)
        self.back_btn = self._nav_button(
            QtWidgets.QStyle.StandardPixmap.SP_ArrowBack, "返回")
        self.up_btn = self._nav_button(
            QtWidgets.QStyle.StandardPixmap.SP_ArrowUp,
            "上一级（不会超出限定目录）")
        self.root_btn = self._nav_button(
            QtWidgets.QStyle.StandardPixmap.SP_DirHomeIcon, "回到限定目录")
        self.back_btn.clicked.connect(self._go_back)
        self.up_btn.clicked.connect(self._go_up)
        self.root_btn.clicked.connect(lambda: self._navigate(self._root_directory))
        toolbar_lay.addWidget(self.back_btn)
        toolbar_lay.addWidget(self.up_btn)
        toolbar_lay.addWidget(self.root_btn)
        toolbar_lay.addSpacing(8)
        self.path_edit = QtWidgets.QLineEdit(objectName="RestrictedCurrentPath")
        self.path_edit.setReadOnly(True)
        self.path_edit.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        toolbar_lay.addWidget(self.path_edit, 1)
        card_lay.addWidget(toolbar)

        self.model = QtWidgets.QFileSystemModel(self)
        self.model.setReadOnly(True)
        self.model.setNameFilterDisables(False)
        self.model.directoryLoaded.connect(self._on_directory_loaded)
        self.browser = QtWidgets.QTreeView(objectName="RestrictedFileView")
        self.browser.setModel(self.model)
        self.browser.setRootIsDecorated(False)
        self.browser.setItemsExpandable(False)
        self.browser.setAllColumnsShowFocus(True)
        self.browser.setAlternatingRowColors(True)
        self.browser.setSortingEnabled(True)
        self.browser.sortByColumn(0, QtCore.Qt.SortOrder.AscendingOrder)
        self.browser.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.browser.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.browser.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.browser.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.NoContextMenu)
        self.browser.doubleClicked.connect(self._on_double_clicked)
        self.browser.selectionModel().currentChanged.connect(self._on_current_changed)
        self.browser.header().setStretchLastSection(True)
        self.browser.setColumnWidth(0, 330)
        self.browser.setColumnWidth(1, 110)
        self.browser.setColumnWidth(2, 150)

        self.browser_stack = QtWidgets.QStackedWidget()
        self.browser_stack.addWidget(self.browser)
        self.empty_page = QtWidgets.QFrame(objectName="RestrictedEmptyState")
        empty_lay = QtWidgets.QVBoxLayout(self.empty_page)
        empty_lay.setContentsMargins(24, 24, 24, 24)
        empty_lay.addStretch(1)
        empty_title = QtWidgets.QLabel(
            "当前目录为空", objectName="RestrictedEmptyTitle")
        empty_title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.empty_detail = QtWidgets.QLabel(objectName="RestrictedEmptyDetail")
        self.empty_detail.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.empty_detail.setWordWrap(True)
        empty_lay.addWidget(empty_title)
        empty_lay.addWidget(self.empty_detail)
        empty_lay.addStretch(1)
        self.browser_stack.addWidget(self.empty_page)
        card_lay.addWidget(self.browser_stack, 1)

        footer = QtWidgets.QHBoxLayout()
        footer.setSpacing(8)
        self.type_label = QtWidgets.QLabel(objectName="RestrictedFileType")
        footer.addWidget(self.type_label)
        self.selection_edit = QtWidgets.QLineEdit(objectName="RestrictedSelectionPath")
        self.selection_edit.setReadOnly(True)
        self.selection_edit.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        footer.addWidget(self.selection_edit, 1)
        self.cancel_btn = QtWidgets.QPushButton(
            "取消", objectName="EmbeddedFileDialogCancel")
        self.accept_btn = QtWidgets.QPushButton(
            "选择", objectName="EmbeddedFileDialogAccept")
        self.cancel_btn.setFixedSize(126, 42)
        self.accept_btn.setFixedSize(126, 42)
        self.cancel_btn.clicked.connect(self._reject)
        self.accept_btn.clicked.connect(self._accept)
        footer.addWidget(self.cancel_btn)
        footer.addWidget(self.accept_btn)
        card_lay.addLayout(footer)

        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card, 12)
        row.addStretch(1)
        outer.addLayout(row, 12)
        outer.addStretch(1)

        # 动画期间绘制卡片快照（与 FrostedCenterOverlay 同款方案）：
        # 不再使用 QGraphicsOpacityEffect——离屏 FBO 渲染与直接渲染的
        # 圆角不一致，动画结束瞬间圆角会突变。快照 + painter 透明度 +
        # clipPath 圆角裁剪保证动画全程与静止时圆角完全一致。
        self._card_snapshot = None
        self._card_live_rect = None
        self._card_progress = 0.0
        self.hide()

    def _nav_button(self, standard_icon, tooltip):
        button = QtWidgets.QToolButton(objectName="RestrictedNavButton")
        button.setIcon(self.style().standardIcon(standard_icon))
        button.setIconSize(QtCore.QSize(18, 18))
        button.setToolTip(tooltip)
        button.setFixedSize(38, 32)
        button.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonIconOnly)
        return button

    def _sync_geometry(self):
        """作为原生子覆盖层始终贴合主窗口本地矩形。"""
        host = self._host
        if host is None:
            return
        self.setGeometry(host.rect())

    @staticmethod
    def _current_theme():
        try:
            return style.load_theme()
        except Exception:
            return "dark"

    def _capture_background(self, force=False):
        """抓取主窗口并套用与 FrostedCenterOverlay 相同的毛玻璃背景。

        打开时 force=True 现场同步抓屏（实时，与消融仪/串口弹窗一致）；
        空闲路径（prewarm/关闭后刷新/视角交互后刷新）按 (宽,高,主题)
        缓存完整模糊背景，作为抓屏失败时的兜底。
        """
        host = self._host
        if host is None:
            self._background = QtGui.QPixmap()
            self._bg_cache_key = None
            return
        key = (host.width(), host.height(), self._current_theme())
        if not force and key == self._bg_cache_key:
            return
        screen = host.screen() or QtWidgets.QApplication.primaryScreen()
        geometry = host.frameGeometry()
        grabbed = QtGui.QPixmap()
        if screen is not None:
            # 优先抓窗口本身（PrintWindow）：VTK 的 GPU 表面也能拿到，
            # 避免桌面 BitBlt 抓到黑块；也无多屏坐标换算问题。
            grabbed = screen.grabWindow(int(host.winId()))
            if grabbed.isNull():
                # 回退：抓桌面区域，坐标需换算为屏幕本地坐标
                # （详见 FrostedCenterOverlay 的说明）。
                origin = screen.geometry().topLeft()
                grabbed = screen.grabWindow(
                    0, geometry.x() - origin.x(), geometry.y() - origin.y(),
                    geometry.width(), geometry.height())
        if grabbed.isNull():
            grabbed = host.grab()
        blurred = FrostedCenterOverlay._blur_captured_pixmap(grabbed)
        if not blurred.isNull():
            painter = QtGui.QPainter(blurred)
            painter.fillRect(
                blurred.rect(),
                FrostedCenterOverlay._scrim_color(self._current_theme()))
            painter.end()
        if not blurred.isNull() and blurred.size() != self.size():
            blurred = blurred.scaled(
                self.size(),
                QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                QtCore.Qt.TransformationMode.SmoothTransformation,
            )
        if blurred.isNull():
            # 抓屏/模糊失败时保留旧背景兜底，避免黑屏
            return
        self._background = blurred
        self._bg_cache_key = key

    def prewarm_background(self):
        """空闲时预抓毛玻璃背景，首次打开对话框也无需现场抓屏。"""
        if self._host is None:
            return
        self._sync_geometry()
        self._capture_background()

    def invalidate_background(self):
        """使背景缓存失效（关闭对话框后调用，配合延迟刷新）。"""
        self._bg_cache_key = None

    def _stop_card_animation(self):
        if self._card_anim is not None:
            self._card_anim.stop()
            self._card_anim.deleteLater()
            self._card_anim = None

    def _run_card_animation(self, start, end, duration, easing, finished):
        """驱动卡片动画进度（0→1 同时控制上浮位置与淡入透明度）。

        与 FrostedCenterOverlay 相同的快照绘制方案：动画只更新进度值，
        快照的实际绘制在 paintEvent 里完成，圆角全程由 clipPath 保证。
        """
        self._stop_card_animation()
        anim = QtCore.QVariantAnimation(self)
        anim.setDuration(duration)
        anim.setStartValue(float(start))
        anim.setEndValue(float(end))
        anim.setEasingCurve(easing)
        anim.valueChanged.connect(self._on_card_progress_changed)
        if finished is not None:
            anim.finished.connect(finished)
        self._card_progress = float(start)
        self._card_anim = anim
        anim.start()

    def _on_card_progress_changed(self, value):
        self._card_progress = max(0.0, min(1.0, float(value)))
        self.update()

    def _play_present_animation(self):
        self._stop_card_animation()
        layout = self.layout()
        if layout is not None:
            layout.activate()
        self._card_target_pos = QtCore.QPoint(self._card.pos())
        # 抓快照并隐藏真实卡片：动画期间由 paintEvent 绘制快照，
        # 快照包含 QSS 渲染结果，再叠加 clipPath 圆角保证与静止态一致。
        self._card_snapshot = self._card.grab()
        self._card_live_rect = QtCore.QRect(self._card.geometry())
        self._card.hide()
        self.update()
        self._run_card_animation(
            0.0, 1.0, FrostedCenterOverlay._POP_MS,
            QtCore.QEasingCurve.Type.OutCubic,
            self._finish_present_animation)

    def _finish_present_animation(self):
        self._stop_card_animation()
        self._card_snapshot = None
        self._card_live_rect = None
        self._card.move(self._card_target_pos)
        self._card.show()
        self._card.raise_()
        self._card_progress = 1.0
        self.update()
        if self.isVisible() and not self._closing:
            self.browser.setFocus(QtCore.Qt.FocusReason.PopupFocusReason)

    def _play_dismiss_animation(self):
        if self._closing:
            return
        self._closing = True
        if self._card_snapshot is None:
            if not self._card.isVisible():
                self._complete_finish()
                return
            self._card_snapshot = self._card.grab()
            self._card_live_rect = QtCore.QRect(self._card.geometry())
            self._card.hide()
            self.update()
        self._run_card_animation(
            self._card_progress, 0.0, FrostedCenterOverlay._RETRACT_MS,
            QtCore.QEasingCurve.Type.InCubic,
            self._complete_finish)

    def _complete_finish(self):
        self._stop_card_animation()
        self.hide()
        self._card_snapshot = None
        self._card_live_rect = None
        self._card.move(self._card_target_pos)
        self._card.show()
        self._card_progress = 0.0
        self._closing = False
        # 原生覆盖层隐藏后，底下的 VTK 原生表面不会自动重绘，屏幕上会
        # 残留文件对话框最后一帧；强制刷新主窗口与 3D 视图消除残影。
        host = self._host
        if host is not None:
            host.update()
            viewer = getattr(host, "viewer", None)
            if viewer is not None:
                QtCore.QTimer.singleShot(0, viewer.render)
        # 关闭后的空闲时段重抓毛玻璃背景：下次打开直接复用当前画面，
        # 抓屏开销不落在任何交互路径上。
        self.invalidate_background()
        QtCore.QTimer.singleShot(350, self._capture_background)
        if self._event_loop is not None and self._event_loop.isRunning():
            self._event_loop.quit()

    def choose(self, title, root_directory, directory=False, name_filter="",
               mixed=False):
        """显示内嵌浏览器并返回所选路径；取消时返回空字符串。

        mixed=True 时同时可选文件夹与 ZIP，用于统一导入入口。
        """
        if self._event_loop is not None:
            return ""
        self._selected_path = ""
        self._mixed_mode = bool(mixed)
        self._directory_mode = bool(directory) and not self._mixed_mode
        self._root_directory = os.path.realpath(os.path.abspath(root_directory))
        self._current_directory = ""
        self._selected_candidate = ""
        self._history = []
        if self._mixed_mode:
            self._allowed_suffixes = (".zip",)
        else:
            self._allowed_suffixes = (
                (".zip",) if "*.zip" in name_filter.lower() else ())
        self.title_label.setText(title)
        if self._mixed_mode:
            self.type_label.setText("文件夹 / ZIP：")
            self.accept_btn.setText("导入")
            self.empty_detail.setText(
                "没有可进入的子文件夹或 ZIP；仍可直接导入当前文件夹。")
        else:
            self.type_label.setText(
                "文件夹：" if self._directory_mode else "ZIP 文件：")
            self.accept_btn.setText(
                "选择此文件夹" if self._directory_mode else "导入")
            self.empty_detail.setText(
                "没有可进入的子文件夹，仍可直接选择当前文件夹。"
                if self._directory_mode else
                "没有子文件夹或 ZIP 压缩包。")

        filters = (QtCore.QDir.Filter.AllDirs
                   | QtCore.QDir.Filter.NoDotAndDotDot)
        if not self._directory_mode or self._mixed_mode:
            filters |= QtCore.QDir.Filter.Files
        self.model.setFilter(filters)
        self.model.setNameFilters(
            ["*.zip"] if self._allowed_suffixes else ["*"])
        if not os.path.isdir(self._root_directory):
            self.selection_edit.setText("限定根目录不存在")
            self.accept_btn.setEnabled(False)
            return ""
        self.model.setRootPath(self._root_directory)
        self._navigate(self._root_directory, record_history=False)

        self._sync_geometry()
        # 与消融仪/串口弹窗完全一致：打开时现场同步抓屏+模糊+压暗，
        # 背景实时且一步到位（无两段式升级，不会在对话框显示期间卡顿）。
        self._capture_background(force=True)
        self._event_loop = QtCore.QEventLoop(self)
        self.show()
        self.raise_()
        self.setFocus(QtCore.Qt.FocusReason.PopupFocusReason)
        self._play_present_animation()
        self._event_loop.exec()
        self._event_loop = None
        return self._selected_path

    def _is_within_root(self, path):
        if not path or not self._root_directory:
            return False
        try:
            real = os.path.realpath(os.path.abspath(path))
            return os.path.normcase(os.path.commonpath(
                [self._root_directory, real])) == os.path.normcase(
                    self._root_directory)
        except (OSError, ValueError):
            return False

    def _navigate(self, path, record_history=True):
        real = os.path.realpath(os.path.abspath(path))
        if not self._is_within_root(real) or not os.path.isdir(real):
            return False
        if (record_history and self._current_directory
                and real != self._current_directory):
            self._history.append(self._current_directory)
        self._current_directory = real
        self.browser.setRootIndex(self.model.index(real))
        self.browser.clearSelection()
        self.browser_stack.setCurrentWidget(self.browser)
        self.path_edit.setText(real)
        self.path_edit.setToolTip(real)
        can_accept_folder = self._directory_mode or self._mixed_mode
        self._selected_candidate = real if can_accept_folder else ""
        self.selection_edit.setText(self._selected_candidate)
        self.accept_btn.setEnabled(can_accept_folder)
        at_root = os.path.normcase(real) == os.path.normcase(self._root_directory)
        self.up_btn.setEnabled(not at_root)
        self.root_btn.setEnabled(not at_root)
        self.back_btn.setEnabled(bool(self._history))
        QtCore.QTimer.singleShot(0, lambda p=real: self._update_empty_state(p))
        return True

    def _on_directory_loaded(self, path):
        real = os.path.realpath(os.path.abspath(path))
        if os.path.normcase(real) == os.path.normcase(self._current_directory):
            self._update_empty_state(real)

    def _update_empty_state(self, path):
        if (not self._current_directory
                or os.path.normcase(os.path.realpath(path))
                != os.path.normcase(self._current_directory)):
            return
        index = self.model.index(self._current_directory)
        is_empty = not index.isValid() or self.model.rowCount(index) == 0
        self.browser_stack.setCurrentWidget(
            self.empty_page if is_empty else self.browser)

    def _go_back(self):
        while self._history:
            path = self._history.pop()
            if self._navigate(path, record_history=False):
                break
        self.back_btn.setEnabled(bool(self._history))

    def _go_up(self):
        if not self._current_directory:
            return
        parent = os.path.dirname(self._current_directory)
        if self._is_within_root(parent):
            self._navigate(parent)

    def _on_current_changed(self, current, _previous):
        path = self.model.filePath(current) if current.isValid() else ""
        if not self._is_within_root(path):
            path = ""
        selectable = self._is_selectable(path)
        self._selected_candidate = path if selectable else ""
        if (self._directory_mode or self._mixed_mode) and not path:
            self._selected_candidate = self._current_directory
        self.selection_edit.setText(self._selected_candidate)
        self.accept_btn.setEnabled(bool(self._selected_candidate))

    def _is_selectable(self, path):
        if not self._is_within_root(path):
            return False
        if self._mixed_mode:
            if os.path.isdir(path):
                return True
            return (os.path.isfile(path)
                    and path.lower().endswith(self._allowed_suffixes or (".zip",)))
        if self._directory_mode:
            return os.path.isdir(path)
        if not os.path.isfile(path):
            return False
        return (not self._allowed_suffixes
                or path.lower().endswith(self._allowed_suffixes))

    def _on_double_clicked(self, index):
        path = self.model.filePath(index)
        if os.path.isdir(path):
            self._navigate(path)
        elif self._is_selectable(path):
            self._selected_candidate = path
            self._accept()

    def _accept(self):
        candidate = self._selected_candidate
        if (self._directory_mode or self._mixed_mode) and not candidate:
            candidate = self._current_directory
        if self._is_selectable(candidate):
            self._finish(os.path.normpath(candidate))

    def _reject(self):
        self._finish("")

    def _finish(self, selected):
        if self._closing:
            return
        self._selected_path = selected
        self._play_dismiss_animation()

    def paintEvent(self, event):
        """绘制已完成模糊和压暗处理的实时主界面背景 + 动画期卡片快照。"""
        theme = getattr(self._host, "_theme", None)
        if theme not in ("dark", "light"):
            theme = style.load_theme()
        painter = QtGui.QPainter(self)
        if not self._background.isNull():
            painter.drawPixmap(self.rect(), self._background)
        else:
            painter.fillRect(
                self.rect(),
                QtGui.QColor(15, 23, 42) if theme == "dark"
                else QtGui.QColor(237, 240, 243),
            )
        # 动画期间绘制卡片快照：进度 p 控制上浮位置与淡入透明度。
        # 圆角用与 QSS 一致的 12px clipPath 裁剪，动画全程与静止态
        # 圆角完全一致，切回真实卡片时无突变。
        if self._card_snapshot is not None and self._card_live_rect is not None:
            p = max(0.0, min(1.0, self._card_progress))
            rect = self._card_live_rect
            offset = (1.0 - p) * FrostedCenterOverlay._slide_offset(self.height())
            dest = QtCore.QRectF(
                rect.x(), rect.y() + offset, rect.width(), rect.height())
            radius = min(self._CARD_RADIUS,
                         dest.width() / 2.0, dest.height() / 2.0)
            path = QtGui.QPainterPath()
            path.addRoundedRect(dest, radius, radius)
            painter.save()
            painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
            painter.setClipPath(path)
            painter.setOpacity(p)
            painter.drawPixmap(
                dest, self._card_snapshot,
                QtCore.QRectF(self._card_snapshot.rect()))
            painter.restore()
        painter.end()

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self._reject()
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        if self._event_loop is not None:
            event.ignore()
            self._reject()
            return
        super().closeEvent(event)


class DatasetLoadWorker(QtCore.QObject):
    """在后台线程中做磁盘读取/解码；VTK 渲染管线仍只在 GUI 线程创建。"""

    finished = QtCore.Signal(object, object)
    failed = QtCore.Signal(str)
    progressChanged = QtCore.Signal(int, str)

    def __init__(self, kind, path):
        super().__init__()
        self.kind = kind
        self.path = path
        self._last_percent = -1

    def _progress(self, fraction, message):
        percent = int(round(float(fraction) * 100.0))
        if percent != self._last_percent:
            self._last_percent = percent
            self.progressChanged.emit(percent, message)

    @QtCore.Slot()
    def run(self):
        try:
            if self.kind == "dicom":
                result = loader.load_dicom_series(
                    self.path, progress=self._progress)
            elif self.kind == "images":
                result = loader.load_image_stack(
                    self.path, progress=self._progress)
            else:
                result = loader.load_zip(
                    self.path, progress=self._progress)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        self.finished.emit(*result)


class _StepBadge(QtWidgets.QLabel):
    """步骤徽章：QLabel 保证文字在固定圆形内像素级居中（QPushButton 的
    盒模型受全局 padding 影响会让数字偏上），点击发 clicked 信号。"""

    clicked = QtCore.Signal()

    def mouseReleaseEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class PlanningStepsBar(QtWidgets.QWidget):
    """主流程步骤指示条：可点击的圆形数字徽章 + 文字。

    当前步金色实底、已完成绿色打勾、未到步灰描边（QSS 由 style.py 的
    StepBadge/StepText/StepArrow 规则驱动，明暗主题自动适配）。徽章可
    点击（stepClicked 信号），配合向导式翻页跳转。步骤多于 3 个时不画
    箭头（面板宽度放不下），序号本身已表达先后。
    """

    stepClicked = QtCore.Signal(int)

    def __init__(self, labels=None, tooltips=None, parent=None):
        super().__init__(parent)
        labels = tuple(labels) if labels else ("定位结节", "规划针道", "仿真并导出")
        self._labels = labels
        self._states = ("pending",) * len(labels)
        self._badges = []
        self._texts = []
        self._segments = []

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 2)
        outer.setSpacing(4)
        lay = QtWidgets.QHBoxLayout()
        lay.setSpacing(2)
        outer.addLayout(lay)

        for index, text in enumerate(labels):
            badge = _StepBadge(str(index + 1), objectName="StepBadge")
            badge.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            badge.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            badge.setMinimumSize(20, 20)
            badge.setMaximumSize(20, 20)
            if tooltips and index < len(tooltips):
                badge.setToolTip(tooltips[index])
            badge.clicked.connect(
                lambda i=index: self.stepClicked.emit(i))
            label = QtWidgets.QLabel(text, objectName="StepText")
            self._badges.append(badge)
            self._texts.append(label)
            lay.addWidget(badge)
            lay.addWidget(label)
            if index < len(labels) - 1:
                lay.addStretch(1)

        # 五段式进度条：完成绿 / 当前金 / 未到灰，与徽章状态同步
        segment_row = QtWidgets.QHBoxLayout()
        segment_row.setSpacing(3)
        for _index in range(len(labels)):
            segment = QtWidgets.QFrame(objectName="ProgressSegment")
            segment.setFixedHeight(4)
            self._segments.append(segment)
            segment_row.addWidget(segment, 1)
        outer.addLayout(segment_row)
        self.set_states(self._states)

    def states(self):
        return self._states

    def badges(self):
        return list(self._badges)

    def set_states(self, states):
        normalized = tuple(
            state if state in ("pending", "active", "done") else "pending"
            for state in states)
        # 步数与徽章数不一致时按徽章数截断/补齐，防御调用方
        self._states = (
            normalized + ("pending",) * len(self._badges))[:len(self._badges)]
        for index, state in enumerate(self._states):
            badge = self._badges[index]
            badge.setText("✓" if state == "done" else str(index + 1))
            for widget in (badge, self._texts[index]):
                widget.setProperty("state", state)
                widget.style().unpolish(widget)
                widget.style().polish(widget)
        for index, segment in enumerate(self._segments):
            state = self._states[index] if index < len(self._states) else "pending"
            segment.setProperty("state", state)
            segment.style().unpolish(segment)
            segment.style().polish(segment)


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
        self.setWindowFlags(
            QtCore.Qt.Window | QtCore.Qt.FramelessWindowHint)
        self.setWindowTitle("消融手术规划系统")
        self.resize(1280, 820)
        self._theme = style.load_theme()  # applied app-wide in main(); mirrored in the 主题 menu
        self._seg_thread = None
        self._seg_worker = None
        self._seg_temp_dir = None
        self._seg_cancelled = False
        self._seg_running = False
        self._seg_engine = None
        self._seg_download_thread = None
        self._seg_download_worker = None
        self._seg_download_running = False
        self._serial = serial_connection.SerialConnection(self)
        self._rtx_parser = rtx_telemetry.RtxDataStreamParser()
        # 通用串口按行缓冲：下位机(RF)持续发送的 IMU 温度/角度遥测行可能被
        # 拆分到多次 dataReceived 里，累积后按 \n 切分再逐行解析。
        self._serial_rx_buffer = ""
        # 当前 IMU 读数来源：通用串口或 HW100 协议链路。
        self._imu_source = None
        # MWA 控制区刷新节流：遥测帧周期 50ms，控制区只反映设备布尔状态，
        # 按状态签名变化刷新，另以 ~8Hz 兜底（见 _schedule_mwa_controls_refresh）。
        self._mwa_controls_clock = QtCore.QElapsedTimer()
        self._mwa_controls_clock.start()
        self._last_mwa_state_signature = None
        self._vna = network_analyzer.NetworkAnalyzerManager(self)
        self._mwa = microwave_ablator.MicrowaveAblationDevice(self)
        self._vna_reply = _VnaSendEmitter(self)
        self._vna_connect_done = _VnaConnectEmitter(self)
        # 连接进行中标志：connect() 是阻塞 socket + 探活，已移入工作线程，
        # 期间禁止重复发起/误判为未连接。
        self._vna_connecting = False
        # 入针点搜索进行中标志与结果回传桥（搜索在后台线程跑）。
        self._entry_search_done = _EntrySearchEmitter(self)
        self._entry_search_running = False
        self._exit_overlay = None
        self._planning_alert_overlay = None
        self._planning_alert_on_confirm = None
        self._planning_alert_pending_confirm = None
        self._mwa_overlay = None
        self._mwa_panel = None
        self._serial_overlay = None
        self._serial_panel = None
        self._file_dialog_overlay = None
        self._dataset_entries = {}
        self._active_dataset_key = None
        self._dataset_load_thread = None
        self._dataset_load_worker = None
        self._dataset_load_entry = None
        self._close_after_load = False
        self._allow_close = False

        root = QtWidgets.QWidget(objectName="Root")
        self.setCentralWidget(root)
        outer = QtWidgets.QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())

        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        # 所有操作集中在左侧一列；右侧完整留给 3D + 三向切片视图。
        body.addWidget(self._build_operation_panel())

        self.viewer = VolumeViewer()
        self.viewer.ablationNeedleChanged.connect(self._on_viewer_needle_changed)
        # 规划点从任何入口(按钮/切片右键/推荐回填)变化都刷新状态行与步骤条
        self.viewer.planningChanged.connect(self._update_plan_status)
        # 3D 视角交互结束后，空闲时段刷新文件对话框的毛玻璃背景缓存
        self.viewer.interactionEnded.connect(
            self._schedule_file_dialog_bg_refresh)
        # 首帧渲染完成后（画面稳定）预抓各弹层毛玻璃背景
        self.viewer.initialRendersFinished.connect(
            self._on_initial_renders_finished)
        self._file_dialog_bg_timer = QtCore.QTimer(self)
        self._file_dialog_bg_timer.setSingleShot(True)
        self._file_dialog_bg_timer.setInterval(900)
        self._file_dialog_bg_timer.timeout.connect(
            self._refresh_file_dialog_background)
        body.addWidget(self.viewer, 1)
        self._serial.statusChanged.connect(self._on_serial_status_changed)
        self._serial.dataReceived.connect(self._on_serial_data_received)
        self._serial.rawDataReceived.connect(self._on_serial_raw_data_received)
        self._serial.errorOccurred.connect(self._on_serial_error)
        self._vna.connection_status_changed.connect(self._on_vna_status_changed)
        self._vna.data_received.connect(self._on_vna_data_received)
        self._vna.error_occurred.connect(self._on_vna_error)
        self._vna_reply.done.connect(self._on_vna_reply)
        self._vna_connect_done.done.connect(self._on_vna_connect_finished)
        self._entry_search_done.done.connect(self._on_entry_search_finished)
        self._mwa.portStatusChanged.connect(self._on_mwa_port_status)
        self._mwa.connectionChanged.connect(self._on_mwa_connection)
        self._mwa.statusChanged.connect(self._on_mwa_status)
        self._mwa.telemetryUpdated.connect(self._on_mwa_telemetry)
        self._mwa.logMessage.connect(self._on_mwa_log)
        self._mwa.errorOccurred.connect(self._on_mwa_error)
        self._mwa.bytesSent.connect(self._on_mwa_bytes_sent)
        self._mwa.bytesReceived.connect(self._on_mwa_bytes_received)
        # 消融控制 / 串口面板提前创建（放进弹窗，不进左侧栏），以便状态回调可用
        self._mwa_panel = self._build_mwa_panel()
        self._serial_panel = self._build_serial_panel()
        self._refresh_device_ports()
        self._update_vna_controls()
        self._update_mwa_controls()
        outer.addLayout(body, 1)

        # Loaded-volume info lives permanently at the right of the status bar
        # (it used to be a label under the old 加载数据 panel section).
        self.status = QtWidgets.QLabel("尚未加载数据", objectName="Status")
        self.statusBar().addPermanentWidget(self.status)
        self.statusBar().showMessage(
            "就绪 — 通过右上角「导入」加载 DICOM / 图片序列 / ZIP，或试用演示体模。")
        self._set_controls_enabled(False)
        # 退出确认层改为首次退出时再创建
        self._exit_overlay = None
        self._viewer_init_started = False

    # ============================================================
    # UI 构建方法
    # 修改 UI 布局/样式时，关注以下区域：
    #   _build_header()   — 顶部标题栏（品牌色条+标题+菜单）
    #   _build_menubar()  — 菜单栏（文件/视图/主题）
    #   _build_panel()    — 右侧控制面板（所有参数控件）
    # ============================================================
    def _build_header(self):
        """构建顶部标题栏：品牌 Logo + 应用标题/副标题 + 右侧功能入口。
        
        样式通过 QSS 中 #Header、#HeaderLogo、#Title、#Subtitle 选择器控制。
        Logo 背景透明，随 #Header 的主题背景色一起变化。
        修改标题文字：修改 title.setText() 和 subtitle.setText()
        """
        header = QtWidgets.QFrame(objectName="Header")
        header.setFixedHeight(64)
        lay = QtWidgets.QHBoxLayout(header)
        lay.setContentsMargins(16, 8, 16, 8)
        lay.setSpacing(12)

        # 左上角品牌 Logo（透明底，随 Header 主题背景变化）
        logo_pm = self._load_header_logo(40)
        if not logo_pm.isNull():
            logo = QtWidgets.QLabel(objectName="HeaderLogo")
            logo.setFixedSize(44, 44)
            logo.setAlignment(QtCore.Qt.AlignCenter)
            logo.setPixmap(logo_pm)
            lay.addWidget(logo, 0, QtCore.Qt.AlignVCenter)
        else:
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

        # 顶部功能入口：导入 / 演示 / 视图动作 / 消融仪 / 串口 ｜ 退出
        lay.addStretch(1)
        lay.addWidget(self._build_header_tools(), 0, QtCore.Qt.AlignVCenter)
        divider = QtWidgets.QFrame(objectName="HeaderDivider")
        divider.setFixedSize(1, 32)
        lay.addWidget(divider, 0, QtCore.Qt.AlignVCenter)
        # 电源图标略大，与左侧工具组区分
        self.btn_exit = QtWidgets.QPushButton(objectName="HeaderExit")
        self.btn_exit.setFixedSize(46, 46)
        self.btn_exit.setIconSize(QtCore.QSize(28, 28))
        self.btn_exit.setIcon(self._header_icon("exit", size=28))
        self.btn_exit.setToolTip("退出")
        self.btn_exit.setFlat(True)
        self.btn_exit.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.btn_exit.setStyleSheet("text-align: center; padding: 0px;")
        self.btn_exit.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_exit.clicked.connect(self.close)
        lay.addWidget(self.btn_exit, 0, QtCore.Qt.AlignVCenter)
        return header

    def _build_header_tools(self):
        """右上角工具组：各功能单独成按钮（不再用下拉菜单）。"""
        tools = QtWidgets.QWidget(objectName="HeaderTools")
        row = QtWidgets.QHBoxLayout(tools)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)

        icon_px = 22
        btn_size = QtCore.QSize(36, 36)

        self.btn_file = self._make_header_action_button(
            "file", "导入影像（DICOM / 图片序列 / ZIP）",
            self._open_import, icon_px, btn_size)
        row.addWidget(self.btn_file)

        self.btn_demo = self._make_header_action_button(
            "demo", "加载演示体模", self._open_demo, icon_px, btn_size)
        row.addWidget(self.btn_demo)

        self.btn_reset = self._make_header_action_button(
            "reset", "重置视角",
            lambda: self.viewer.reset_view(), icon_px, btn_size)
        row.addWidget(self.btn_reset)

        self.btn_shot = self._make_header_action_button(
            "shot", "保存截图", self._save_screenshot, icon_px, btn_size)
        row.addWidget(self.btn_shot)

        self.btn_mesh = self._make_header_action_button(
            "mesh", "导出三维模型 (STL)", self._export_mesh, icon_px, btn_size)
        row.addWidget(self.btn_mesh)

        self.btn_theme = self._make_header_action_button(
            "theme", "切换主题", self._toggle_theme, icon_px, btn_size)
        row.addWidget(self.btn_theme)
        self._refresh_theme_button()

        self.btn_mwa = self._make_header_action_button(
            "mwa", "微波消融仪", self._show_mwa_overlay, icon_px, btn_size,
            object_name="HeaderMwa")
        row.addWidget(self.btn_mwa)

        self.btn_serial = self._make_header_action_button(
            "serial", "串口：未连接", self._show_serial_overlay, icon_px, btn_size)
        row.addWidget(self.btn_serial)

        # 兼容旧代码中对 menu_bar / 动作对象的引用
        self.menu_bar = tools
        self.act_reset = self.btn_reset
        self.act_shot = self.btn_shot
        self.act_mesh = self.btn_mesh
        return tools

    def _make_header_action_button(self, kind, tip, slot, icon_px, btn_size,
                                   object_name="HeaderTool"):
        """创建右上角独立图标按钮（无下拉）。"""
        btn = QtWidgets.QPushButton(objectName=object_name)
        btn.setFixedSize(btn_size)
        btn.setIconSize(QtCore.QSize(icon_px, icon_px))
        btn.setIcon(self._header_icon(kind, size=icon_px))
        btn.setToolTip(tip)
        btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        btn.setFlat(True)
        btn.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        # 覆盖全局 QPushButton 的 text-align:left，保证图标落在悬停方框正中
        btn.setStyleSheet("text-align: center; padding: 0px;")
        btn.clicked.connect(slot)
        return btn

    def _make_header_tool_button(self, kind, tip, menu, icon_px, btn_size):
        """兼容旧接口；现已改为独立按钮。"""
        return self._make_header_action_button(
            kind, tip, lambda: None, icon_px, btn_size)

    @staticmethod
    def _load_header_logo(height=40):
        """加载左上角 Logo（透明背景 PNG），按高度等比缩放。"""
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "assets", "app_logo_hq.png")
        if not os.path.isfile(path):
            return QtGui.QPixmap()
        pm = QtGui.QPixmap(path)
        if pm.isNull():
            return pm
        return pm.scaledToHeight(
            height,
            QtCore.Qt.TransformationMode.SmoothTransformation)

    @staticmethod
    def _header_icon(kind, active=False, size=28):
        """绘制清晰的彩色图标；size 为逻辑像素边长。"""
        size = max(16, int(size))
        scale = size / 22.0
        pixmap = QtGui.QPixmap(size, size)
        pixmap.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setPen(QtCore.Qt.NoPen)
        painter.scale(scale, scale)

        if kind == "file":
            # 暖黄色文件夹 + 高光页角。
            painter.setBrush(QtGui.QColor("#F7B731"))
            painter.drawRoundedRect(QtCore.QRectF(2.0, 6.0, 18.0, 13.0), 3.0, 3.0)
            painter.setBrush(QtGui.QColor("#FFD166"))
            painter.drawRoundedRect(QtCore.QRectF(3.0, 3.0, 8.5, 6.0), 2.0, 2.0)
            painter.setBrush(QtGui.QColor("#FFF0B3"))
            painter.drawRoundedRect(QtCore.QRectF(4.0, 9.0, 14.0, 2.2), 1.1, 1.1)
        elif kind == "demo":
            # 青绿色烧瓶：演示体模。
            painter.setBrush(QtGui.QColor("#2DD4BF"))
            flask = QtGui.QPainterPath()
            flask.moveTo(7.5, 3.0)
            flask.lineTo(14.5, 3.0)
            flask.lineTo(14.5, 8.0)
            flask.lineTo(18.0, 18.5)
            flask.lineTo(4.0, 18.5)
            flask.lineTo(7.5, 8.0)
            flask.closeSubpath()
            painter.drawPath(flask)
            painter.setBrush(QtGui.QColor("#99F6E4"))
            painter.drawEllipse(QtCore.QPointF(11.0, 14.5), 3.2, 2.2)
        elif kind == "reset":
            # 青色旋转箭头：重置视角。
            color = QtGui.QColor("#38BDF8")
            pen = QtGui.QPen(color, 2.2, QtCore.Qt.SolidLine,
                             QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawArc(QtCore.QRectF(4.0, 4.0, 14.0, 14.0),
                            40 * 16, 250 * 16)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(color)
            tip = QtGui.QPainterPath()
            tip.moveTo(15.8, 3.2)
            tip.lineTo(19.2, 8.2)
            tip.lineTo(13.6, 7.6)
            tip.closeSubpath()
            painter.drawPath(tip)
        elif kind == "shot":
            # 相机：截图。
            painter.setBrush(QtGui.QColor("#A78BFA"))
            painter.drawRoundedRect(QtCore.QRectF(2.5, 7.0, 17.0, 11.5), 2.5, 2.5)
            painter.setBrush(QtGui.QColor("#DDD6FE"))
            painter.drawRoundedRect(QtCore.QRectF(7.5, 4.2, 7.0, 3.2), 1.2, 1.2)
            painter.setBrush(QtGui.QColor("#5B21B6"))
            painter.drawEllipse(QtCore.QPointF(11.0, 12.6), 3.4, 3.4)
            painter.setBrush(QtGui.QColor("#EDE9FE"))
            painter.drawEllipse(QtCore.QPointF(11.0, 12.6), 1.5, 1.5)
        elif kind == "mesh":
            # 立体方块：导出 STL。
            painter.setBrush(QtGui.QColor("#34D399"))
            top = QtGui.QPainterPath()
            top.moveTo(11.0, 2.5)
            top.lineTo(19.0, 7.0)
            top.lineTo(11.0, 11.5)
            top.lineTo(3.0, 7.0)
            top.closeSubpath()
            painter.drawPath(top)
            painter.setBrush(QtGui.QColor("#059669"))
            left = QtGui.QPainterPath()
            left.moveTo(3.0, 7.0)
            left.lineTo(11.0, 11.5)
            left.lineTo(11.0, 19.5)
            left.lineTo(3.0, 15.0)
            left.closeSubpath()
            painter.drawPath(left)
            painter.setBrush(QtGui.QColor("#10B981"))
            right = QtGui.QPainterPath()
            right.moveTo(19.0, 7.0)
            right.lineTo(11.0, 11.5)
            right.lineTo(11.0, 19.5)
            right.lineTo(19.0, 15.0)
            right.closeSubpath()
            painter.drawPath(right)
        elif kind == "theme":
            # 深色主题显示月亮，浅色主题显示太阳（表示当前主题）。
            if active:
                painter.setBrush(QtGui.QColor("#FBBF24"))
                painter.drawEllipse(QtCore.QPointF(11.0, 11.0), 5.2, 5.2)
                painter.setBrush(QtGui.QColor("#FDE68A"))
                for angle in range(0, 360, 45):
                    rad = math.radians(angle)
                    cx = 11.0 + math.cos(rad) * 8.2
                    cy = 11.0 + math.sin(rad) * 8.2
                    painter.drawEllipse(QtCore.QPointF(cx, cy), 1.15, 1.15)
            else:
                painter.setBrush(QtGui.QColor("#93C5FD"))
                painter.drawEllipse(QtCore.QPointF(11.5, 11.0), 5.4, 5.4)
                painter.setBrush(QtGui.QColor("#1E293B"))
                painter.drawEllipse(QtCore.QPointF(14.2, 9.0), 4.4, 4.4)
        elif kind == "view":
            # 青蓝色眼睛，适合表达视角、截图和显示设置。
            eye = QtGui.QPainterPath()
            eye.moveTo(1.5, 11.0)
            eye.cubicTo(5.0, 4.5, 17.0, 4.5, 20.5, 11.0)
            eye.cubicTo(17.0, 17.5, 5.0, 17.5, 1.5, 11.0)
            painter.setBrush(QtGui.QColor("#38BDF8"))
            painter.drawPath(eye)
            painter.setBrush(QtGui.QColor("#0F766E"))
            painter.drawEllipse(QtCore.QPointF(11.0, 11.0), 4.2, 4.2)
            painter.setBrush(QtGui.QColor("#E6FFFB"))
            painter.drawEllipse(QtCore.QPointF(12.2, 9.8), 1.25, 1.25)
        elif kind == "serial":
            # 插头在连接后变为绿色，断开时保持蓝灰色。
            color = QtGui.QColor("#22C55E" if active else "#60A5FA")
            painter.setBrush(color)
            painter.drawRoundedRect(QtCore.QRectF(6.0, 7.0, 10.0, 8.0), 2.5, 2.5)
            painter.drawRoundedRect(QtCore.QRectF(8.0, 3.0, 2.2, 5.0), 1.0, 1.0)
            painter.drawRoundedRect(QtCore.QRectF(12.0, 3.0, 2.2, 5.0), 1.0, 1.0)
            pen = QtGui.QPen(color, 2.2, QtCore.Qt.SolidLine,
                             QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
            painter.setPen(pen)
            path = QtGui.QPainterPath(QtCore.QPointF(11.0, 15.0))
            path.cubicTo(11.0, 19.0, 16.5, 16.5, 18.5, 19.5)
            painter.drawPath(path)

        elif kind == "mwa":
            # 微波弧线 + 针尖，表示消融仪控制入口。
            color = QtGui.QColor("#F59E0B")
            pen = QtGui.QPen(color, 2.0, QtCore.Qt.SolidLine,
                             QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawArc(QtCore.QRectF(3.0, 4.0, 10.0, 10.0),
                            -40 * 16, 200 * 16)
            painter.drawArc(QtCore.QRectF(6.0, 6.5, 7.0, 7.0),
                            -40 * 16, 200 * 16)
            painter.setBrush(color)
            painter.setPen(QtCore.Qt.NoPen)
            tip = QtGui.QPainterPath()
            tip.moveTo(14.5, 8.0)
            tip.lineTo(19.5, 18.0)
            tip.lineTo(11.5, 15.5)
            tip.closeSubpath()
            painter.drawPath(tip)

        elif kind == "exit":
            # 珊瑚红电源图标，作为无系统标题栏时的明确退出入口。
            color = QtGui.QColor("#FB7185")
            pen = QtGui.QPen(color, 2.4, QtCore.Qt.SolidLine,
                             QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.drawArc(QtCore.QRectF(4.0, 4.0, 14.0, 14.0),
                            40 * 16, 280 * 16)
            painter.drawLine(QtCore.QPointF(11.0, 2.5),
                             QtCore.QPointF(11.0, 10.0))

        painter.end()
        return QtGui.QIcon(pixmap)


    def _build_menubar(self):
        """兼容入口：实际工具条由 _build_header_tools 构建。"""
        return self._build_header_tools()

    @staticmethod
    def _repolish_prop(widget, name, value):
        if widget is None:
            return
        if widget.property(name) == value:
            return
        widget.setProperty(name, value)
        style = widget.style()
        if style is not None:
            style.unpolish(widget)
            style.polish(widget)
        widget.update()

    def _sync_link_state(self, state_label, connected,
                         on_text="●  已连接", off_text="●  未连接"):
        """通道角标：复用消融仪 MwaState（正常绿 / 异常红）。"""
        if state_label is None:
            return
        connected = bool(connected)
        state_label.setText(on_text if connected else off_text)
        self._repolish_prop(state_label, "alarm", not connected)

    def _sync_device_connect_button(self, button, connected):
        if button is None:
            return
        connected = bool(connected)
        button.setText("断开" if connected else "连接")
        if button.objectName() in ("MwaAction", "LinkAction"):
            self._repolish_prop(button, "active", connected)
        else:
            self._repolish_prop(button, "connected", connected)

    def _refresh_device_summary(self):
        """标题栏汇总：通用串口 / 微波消融仪 / 网分三路连接数。"""
        if self._serial_overlay is None:
            return
        count = 0
        if self._serial.is_connected():
            count += 1
        if self._mwa.is_port_open():
            count += 1
        if self._vna.is_connected():
            count += 1
        self._serial_overlay.set_link_summary(count, 3)

    @staticmethod
    def _link_field_label(text, width):
        """设备参数标签；旧行式布局可指定宽度，卡片式布局传 0。"""
        label = QtWidgets.QLabel(text, objectName="LinkFieldLabel")
        if width > 0:
            label.setFixedWidth(width)
            label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        else:
            label.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        return label

    @staticmethod
    def _link_section_header(title, caption, trailing=()):
        """分区标题：中文标题 + 拉开字距的英文注脚 + 右侧次级动作。"""
        head = QtWidgets.QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(9)
        head.addWidget(
            QtWidgets.QLabel(title, objectName="LinkSectionTitle"),
            0, QtCore.Qt.AlignVCenter)
        caption_label = QtWidgets.QLabel(
            caption, objectName="LinkSectionCaption")
        font = caption_label.font()
        font.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 1.4)
        caption_label.setFont(font)
        head.addWidget(caption_label, 0, QtCore.Qt.AlignVCenter)
        head.addStretch(1)
        for widget in trailing:
            head.addWidget(widget, 0, QtCore.Qt.AlignVCenter)
        return head

    def _make_link_channel(self, kind, title, tag, status_text="",
                           icon=None, action=None):
        """设备链路通道卡：标题行(图标 + 名称 + 协议角标 + 状态胶囊) →
        参数网格 → 连接按钮 → 事件消息行。

        设计原则（参考 Arduino / VS Code 串口监视器与 nRF Connect 设备管理）：
          · 信息只出现一次：端口/IP 只出现在输入控件里，不再重复做大号读数屏；
          · 状态只出现一次：右上角胶囊表达「连接状态」，底部消息行表达
            「事件与错误」，红色留给真正的故障；
          · 动作按钮全宽，是卡片里唯一的实心视觉焦点。
        kind 取 ser1 / ser2 / vna，配色为 青绿 / 天蓝 / 琥珀 三通道色。
        """
        card = QtWidgets.QFrame(objectName="MwaChannel")
        card.setProperty("kind", kind)
        card.setProperty("connected", False)
        card_lay = QtWidgets.QVBoxLayout(card)
        card_lay.setContentsMargins(14, 13, 14, 12)
        card_lay.setSpacing(9)

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        chip = QtWidgets.QFrame(objectName="MwaIconChip")
        chip.setProperty("kind", kind)
        chip.setFixedSize(30, 30)
        chip_lay = QtWidgets.QVBoxLayout(chip)
        chip_lay.setContentsMargins(0, 0, 0, 0)
        chip_icon = QtWidgets.QLabel()
        if icon is not None and not icon.isNull():
            chip_icon.setPixmap(icon.pixmap(18, 18))
        chip_icon.setAlignment(QtCore.Qt.AlignCenter)
        chip_lay.addWidget(chip_icon)
        header.addWidget(chip, 0, QtCore.Qt.AlignVCenter)
        header.addWidget(
            QtWidgets.QLabel(title, objectName="MwaChannelTitle"),
            0, QtCore.Qt.AlignVCenter)
        badge = QtWidgets.QLabel(tag, objectName="MwaUnitBadge")
        badge.setProperty("kind", kind)
        font = badge.font()
        font.setLetterSpacing(QtGui.QFont.SpacingType.AbsoluteSpacing, 1.2)
        badge.setFont(font)
        header.addWidget(badge, 0, QtCore.Qt.AlignVCenter)
        header.addStretch(1)
        state = QtWidgets.QLabel("●  未连接", objectName="LinkState")
        state.setProperty("alarm", True)
        header.addWidget(state, 0, QtCore.Qt.AlignVCenter)
        card_lay.addLayout(header)

        controls = QtWidgets.QGridLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setHorizontalSpacing(8)
        controls.setVerticalSpacing(5)
        controls.setColumnStretch(0, 3)
        controls.setColumnStretch(1, 2)
        card_lay.addLayout(controls)

        if action is not None:
            card_lay.addWidget(action)

        status = QtWidgets.QLabel(status_text, objectName="LinkStatus")
        status.setProperty("connected", False)
        status.setProperty("error", False)
        status.setWordWrap(False)
        status.setMinimumHeight(14)
        status.setAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        card_lay.addWidget(status)
        return card, controls, status, state

    def _make_link_action(self, clicked):
        """链路动作钮：未连接=品牌色描边「连接」，已连接=告警色描边「断开」。

        旧版连接成功后整颗按钮变成红色实心渐变，而在医疗界面里红色实心等同
        报警；这里改成描边，把红色实心留给真正的故障。
        """
        button = QtWidgets.QPushButton("连接", objectName="LinkAction")
        button.setProperty("active", False)
        button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        button.setMinimumHeight(36)
        button.clicked.connect(clicked)
        return button

    def _make_link_ghost(self, text, icon_kind, tooltip, clicked):
        """分区标题右侧的次级动作（刷新端口 / 清空）。"""
        button = QtWidgets.QPushButton(text, objectName="LinkGhost")
        button.setIcon(self._serial_icon(icon_kind, "#7C8792"))
        button.setIconSize(QtCore.QSize(14, 14))
        button.setToolTip(tooltip)
        button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        button.setMinimumHeight(28)
        button.clicked.connect(clicked)
        return button

    def _build_serial_panel(self):
        """设备连接面板：通用串口 / 微波消融仪 / 网络分析仪三行，每行左侧是
        链路设置卡，右侧是该通道独立的收发监视窗，互不串台。"""
        popup = QtWidgets.QWidget(objectName="SerialPopup")
        lay = QtWidgets.QVBoxLayout(popup)
        lay.setContentsMargins(0, 2, 0, 0)
        lay.setSpacing(13)

        # 标题栏状态胶囊（由 SerialControlOverlay 挂到卡片头）
        status_chip = QtWidgets.QFrame(popup, objectName="LinkStatusChip")
        status_chip_lay = QtWidgets.QHBoxLayout(status_chip)
        status_chip_lay.setContentsMargins(11, 5, 12, 5)
        status_chip_lay.setSpacing(7)
        self.serial_summary_dot = QtWidgets.QLabel(objectName="SerialStatusDot")
        self.serial_summary_dot.setFixedSize(8, 8)
        self.serial_summary_dot.setProperty("connected", False)
        self.serial_link_label = QtWidgets.QLabel(
            "0 / 3 已连接", objectName="LinkSummary")
        status_chip_lay.addWidget(
            self.serial_summary_dot, 0, QtCore.Qt.AlignVCenter)
        status_chip_lay.addWidget(
            self.serial_link_label, 0, QtCore.Qt.AlignVCenter)
        status_chip.setProperty("online", False)
        self.serial_status_chip = status_chip
        popup.serial_status_chip = status_chip
        popup.serial_link_label = self.serial_link_label
        popup.serial_summary_dot = self.serial_summary_dot

        # ---- 链路区：三行，每行 = 链路设置卡 + 独立收发监视窗 ----
        # 通用串口走文本透传，微波消融仪走 PC↔HW100 二进制协议，网分走 TCP。
        # 两条串口链路共用一次系统枚举（_refresh_device_ports），
        # 所以「刷新端口」收敛成分区标题上的一颗按钮。
        self.btn_ports_refresh = self._make_link_ghost(
            "刷新端口", "refresh", "重新枚举系统串口",
            lambda _checked=False: self._refresh_device_ports())

        # 通道身份色：图标、状态胶囊、按钮描边保持同色。
        if style.load_theme() == "light":
            link_color = {
                "ser1": "#0F766E", "ser2": "#0369A1", "vna": "#B45309"}
        else:
            link_color = {
                "ser1": "#2DD4BF", "ser2": "#38BDF8", "vna": "#FBBF24"}

        lay.addLayout(self._link_section_header(
            "设备链路", "LINKS · MONITORS", (self.btn_ports_refresh,)))

        field = self._link_field_label

        # ---- 通用串口（文本透传；兼容 IMU 遥测行解析） ----
        self.btn_serial_connect = self._make_link_action(
            self._toggle_serial_connection)
        (self.ser_card, ser_grid, self.serial_status,
         self.serial_link_state) = self._make_link_channel(
            "ser1", "通用串口", "COM", "通用串口未连接。",
            icon=self._serial_icon("com", link_color["ser1"]),
            action=self.btn_serial_connect)
        self.ser_page = self.ser_card
        self.serial_port = NoWheelComboBox()
        self.serial_port.setMinimumContentsLength(5)
        self.serial_port.currentIndexChanged.connect(
            self._update_serial_controls)
        self.serial_baud = NoWheelComboBox()
        self.serial_baud.addItems(
            [str(rate) for rate in serial_connection.BAUD_RATES])
        self.serial_baud.setCurrentText("115200")
        ser_grid.addWidget(field("串口", 0), 0, 0)
        ser_grid.addWidget(field("波特率", 0), 0, 1)
        ser_grid.addWidget(self.serial_port, 1, 0)
        ser_grid.addWidget(self.serial_baud, 1, 1)
        self.mon_serial = LinkChannelMonitor(
            popup, show_hex_modes=True,
            placeholder="输入文本或开启 HEX 发送后输入字节")
        self.mon_serial.send_requested.connect(self._send_serial_text)
        self.mon_serial.rx_hex_chk.toggled.connect(
            self._on_serial_rx_mode_changed)
        lay.addWidget(self._link_row(self.ser_card, self.mon_serial), 1)

        # ---- 微波消融仪（固定 PC↔HW100 V1 二进制协议） ----
        self.btn_mwa_connect = self._make_link_action(
            self._toggle_mwa_connection)
        (self.mwa_card, mwa_grid, self.mwa_conn_status,
         self.mwa_link_state) = self._make_link_channel(
            "ser2", "微波消融仪", "HW100", "微波消融仪未连接。",
            icon=self._serial_icon("com", link_color["ser2"]),
            action=self.btn_mwa_connect)
        self.mwa_page = self.mwa_card
        self.mwa_port = NoWheelComboBox()
        self.mwa_port.setMinimumContentsLength(5)
        self.mwa_port.currentIndexChanged.connect(self._update_mwa_controls)
        self.mwa_baud = NoWheelComboBox()
        self.mwa_baud.addItem("115200")
        self.mwa_baud.setToolTip("PC↔HW100 V1 固定为 115200 / 8 / N / 1")
        mwa_grid.addWidget(field("串口", 0), 0, 0)
        mwa_grid.addWidget(field("波特率", 0), 0, 1)
        mwa_grid.addWidget(self.mwa_port, 1, 0)
        mwa_grid.addWidget(self.mwa_baud, 1, 1)
        self.mon_mwa = LinkChannelMonitor(
            popup, show_newline=False,
            placeholder="输入完整十六进制协议帧，如 AA 24 24 BB")
        self.mon_mwa.send_requested.connect(self._send_mwa_hex)
        lay.addWidget(self._link_row(self.mwa_card, self.mon_mwa), 1)

        # ---- 网络分析仪（TCP / SCPI） ----
        self.btn_vna_connect = self._make_link_action(
            self._toggle_vna_connection)
        (self.vna_card, vna_grid, self.vna_status,
         self.vna_link_state) = self._make_link_channel(
            "vna", "网络分析仪", "TCP", "网络分析仪未连接。",
            icon=self._serial_icon("vna", link_color["vna"]),
            action=self.btn_vna_connect)
        self.vna_page = self.vna_card
        self.vna_ip = QtWidgets.QLineEdit("192.168.1.3")
        self.vna_ip.setPlaceholderText("例如 192.168.1.3")
        self.vna_ip.returnPressed.connect(self._toggle_vna_connection)
        self.vna_port = QtWidgets.QLineEdit(str(network_analyzer.DEFAULT_PORT))
        self.vna_port.setPlaceholderText("5025")
        self.vna_port.returnPressed.connect(self._toggle_vna_connection)
        vna_grid.addWidget(field("IP 地址", 0), 0, 0)
        vna_grid.addWidget(field("端口", 0), 0, 1)
        vna_grid.addWidget(self.vna_ip, 1, 0)
        vna_grid.addWidget(self.vna_port, 1, 1)
        self.mon_vna = LinkChannelMonitor(
            popup, show_newline=False,
            placeholder="输入 SCPI 指令后回车发送，如 *IDN?",
            rx_label="接收", tx_label="发送")
        self.mon_vna.send_requested.connect(self._send_vna_text)
        lay.addWidget(self._link_row(self.vna_card, self.mon_vna), 1)

        return popup

    @staticmethod
    def _link_row(settings_card, monitor):
        """设备链路一行：约 30% 设置区 + 70% 监视区，左右严格等高。"""
        row = QtWidgets.QWidget(objectName="LinkRow")
        lay = QtWidgets.QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        row.setFixedHeight(198)
        settings_card.setFixedWidth(352)
        settings_card.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Fixed,
            QtWidgets.QSizePolicy.Policy.Expanding)
        monitor.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Expanding)
        lay.addWidget(settings_card, 0)
        lay.addWidget(monitor, 1)
        return row

    def _build_serial_menu(self, menu=None):
        """兼容旧入口：确保串口面板已创建。"""
        if self._serial_panel is None:
            self._serial_panel = self._build_serial_panel()
        return self._serial_panel

    def _set_theme(self, name):
        """在运行时切换整个应用的主题（深色↔浅色）。
        
        执行步骤：
          1. 更新内部状态
          2. 调用 style.apply_theme() 重建样式表/调色板/箭头图标
          3. 保存主题选择到 QSettings（下次启动自动恢复）
          4. 更新 Windows 原生标题栏颜色
          5. 刷新主题按钮图标
        """
        self._theme = name
        log.info("切换主题 -> %s", name)
        app = QtWidgets.QApplication.instance()
        if app is not None:
            style.apply_theme(app, name)
        style.save_theme(name)
        self._apply_titlebar()  # 重绘原生标题栏颜色
        self._refresh_theme_button()

    def _toggle_theme(self):
        self._set_theme("light" if self._theme == "dark" else "dark")

    def _refresh_theme_button(self):
        if not hasattr(self, "btn_theme"):
            return
        # active=True 表示当前是浅色（画太阳）；深色画月亮
        self.btn_theme.setIcon(
            self._header_icon("theme", active=self._theme == "light", size=22))
        self.btn_theme.setToolTip(
            "切换为深色主题" if self._theme == "light" else "切换为浅色主题")

    def _build_operation_panel(self):
        """左侧单一滚动操作面板（主流程向导；消融仿真在向导第⑤页内）。"""
        return self._build_panel()

    def _build_panel(self):
        """构建左侧控制面板（可滚动的参数控制区域）。
        
        面板分区结构（从上到下）：
          组织（勾选显示 · 单独调透明度）
            └ 组织列表 + 全选/全不选按钮
          显示效果
            ├ 不透明 / 透明 切换
            ├ 不透明度 滑块
            └ Z 比例 微调框
          消融针、消融仿真和设备控制继续排列在同一左侧滚动面板中。
        
        修改方法：
          - 面板宽度：修改 scroll.setFixedWidth(330)
          - 添加新分区：参照现有 _section() 的分区模式
          - 修改标题：修改各 _section() 调用中的文本
          - 组织列表：直接随内容自适应高度，全部显示（无内层滚动条）
        """
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("PanelScroll")
        scroll.setFixedWidth(376)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        panel = QtWidgets.QFrame(objectName="Panel")
        panel.setMinimumWidth(344)
        scroll.setWidget(panel)
        lay = QtWidgets.QVBoxLayout(panel)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(10)

        # 组织层已迁移到 3D 视图的右键菜单「组织」中(显示/隐藏 + 逐层透明度)。

        # ===== 主流程向导：整个左面板一步一页，底部前后翻页 =====
        # 加载影像 → 自动分割 → 定位结节 → 规划针道 → 导出核对单。
        # 每页只放当前步骤的操作，注意力不被其他功能区分散；完成当前步
        # 自动翻页，徽章/前后按钮随时可回看，不锁操作。
        core = QtWidgets.QFrame(objectName="CorePanel")
        core_lay = QtWidgets.QVBoxLayout(core)
        core_lay.setContentsMargins(12, 8, 12, 12)
        core_lay.setSpacing(8)
        core_lay.addWidget(QtWidgets.QLabel("消融规划主流程", objectName="CorePanelTitle"))
        self.plan_steps = PlanningStepsBar(
            ("加载", "分割", "定位", "针道", "导出"),
            tooltips=("加载影像", "自动分割", "定位结节", "规划针道", "导出核对单"))
        self.plan_steps.stepClicked.connect(self._show_plan_page)
        core_lay.addWidget(self.plan_steps)
        self._plan_report_exported = False
        self._plan_completed = 0
        self._plan_pages = QtWidgets.QStackedWidget()
        core_lay.addWidget(self._plan_pages, 1)

        # ---- 第①页 加载影像 ----
        page1 = QtWidgets.QWidget()
        p1 = QtWidgets.QVBoxLayout(page1)
        p1.setContentsMargins(0, 2, 0, 2)
        p1.setSpacing(10)
        p1.addWidget(self._page_header(
            1, "加载影像", "导入 DICOM / 图片序列 / ZIP，确认后加载到中央视图。"))

        # 影像数据暂存区：导入只登记到列表，用户确认后才加载到中央视图。
        dataset_header = QtWidgets.QHBoxLayout()
        dataset_header.addWidget(self._section("影像数据"))
        dataset_header.addStretch(1)
        self.dataset_count = QtWidgets.QLabel("0 项", objectName="DatasetCount")
        dataset_header.addWidget(self.dataset_count, 0, QtCore.Qt.AlignVCenter)
        p1.addLayout(dataset_header)

        dataset_card = QtWidgets.QFrame(objectName="DatasetLibraryCard")
        dataset_lay = QtWidgets.QVBoxLayout(dataset_card)
        dataset_lay.setContentsMargins(8, 8, 8, 8)
        dataset_lay.setSpacing(7)
        self.dataset_list = QtWidgets.QListWidget(objectName="DatasetList")
        self.dataset_list.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.dataset_list.setMinimumHeight(92)
        self.dataset_list.setMaximumHeight(150)
        self.dataset_list.currentItemChanged.connect(self._on_dataset_selection_changed)
        self.dataset_list.itemDoubleClicked.connect(
            lambda _item: self._load_selected_dataset())
        dataset_lay.addWidget(self.dataset_list)

        self.dataset_info = QtWidgets.QLabel(
            "通过右上角「导入」导入；导入后不会立即显示。",
            objectName="DatasetInfo")
        self.dataset_info.setWordWrap(True)
        self.dataset_info.setMaximumHeight(42)
        dataset_lay.addWidget(self.dataset_info)

        dataset_actions = QtWidgets.QVBoxLayout()
        dataset_actions.setSpacing(7)
        self.btn_dataset_load = QtWidgets.QPushButton(
            "加载显示", objectName="Primary")
        self.btn_dataset_remove = QtWidgets.QPushButton(
            "移除", objectName="DatasetRemove")
        self.btn_dataset_remove.setEnabled(False)
        self.btn_dataset_load.setEnabled(False)
        self.btn_dataset_remove.clicked.connect(self._remove_selected_dataset)
        self.btn_dataset_load.clicked.connect(self._load_selected_dataset)
        dataset_actions.addWidget(self.btn_dataset_load)
        dataset_actions.addWidget(self.btn_dataset_remove)
        dataset_lay.addLayout(dataset_actions)
        p1.addWidget(dataset_card)

        # 1 - Appearance
        p1.addWidget(self._section("显示效果"))
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
        p1.addLayout(seg)

        op_row = QtWidgets.QHBoxLayout()
        op_row.addWidget(QtWidgets.QLabel("不透明度"))
        self.opacity_slider = MarkedSlider(QtCore.Qt.Horizontal)
        self.opacity_slider.setRange(5, 100)
        self.opacity_slider.setValue(int(OPAQUE_SCALE * 100))
        self.opacity_slider.set_mark_value(OPACITY_MARK_VALUE)   # 15% 参考刻度
        self.opacity_slider.valueChanged.connect(self._on_slider)
        # 拖动时 valueChanged 每像素一报，而 set_opacity_scale 会重建传递函数
        # 并渲染整个 3D 视图——按 ~25fps 合批，释放滑块时立即落最终值
        # （与 viewer.py 图层菜单行 _LayerMenuRow 的做法一致）。
        self._pending_opacity_scale = None
        self._opacity_flush_timer = QtCore.QTimer(self)
        self._opacity_flush_timer.setSingleShot(True)
        self._opacity_flush_timer.setInterval(40)
        self._opacity_flush_timer.timeout.connect(self._flush_opacity_scale)
        self.opacity_slider.sliderReleased.connect(self._flush_opacity_scale)
        op_row.addWidget(self.opacity_slider, 1)
        p1.addLayout(op_row)

        z_row = QtWidgets.QHBoxLayout()
        z_lbl = QtWidgets.QLabel("Z 比例")
        z_lbl.setToolTip("校正层间距比例:模型显得太长就调小、太扁就调大。\nDICOM 通常保持 1.0(已含真实层厚)。")
        z_row.addWidget(z_lbl)
        self.z_spin = NoWheelDoubleSpinBox()
        self.z_spin.setRange(0.1, 10.0)
        self.z_spin.setSingleStep(0.1)
        self.z_spin.setValue(1.0)
        self.z_spin.setToolTip(z_lbl.toolTip())
        self.z_spin.valueChanged.connect(self._on_z_spacing_changed)
        z_row.addWidget(self.z_spin, 1)
        p1.addLayout(z_row)
        p1.addStretch(1)
        self._plan_pages.addWidget(page1)

        # ---- 第②页 自动分割 ----
        page2 = QtWidgets.QWidget()
        p2 = QtWidgets.QVBoxLayout(page2)
        p2.setContentsMargins(0, 2, 0, 2)
        p2.setSpacing(6)
        p2.addWidget(self._page_header(
            2, "自动分割", "AI 识别肺结节与器官；分割是推荐步骤，也可跳过。"))

        # AI - TotalSegmentator organs + MONAI lung-nodule segmentation
        p2.addWidget(self._section("AI · 自动医学分割"))
        self.seg_preset = NoWheelComboBox()
        self.seg_preset.addItems(segmentation.preset_names())
        p2.addWidget(self.seg_preset)

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
        p2.addLayout(seg_opts)

        seg_device_row = QtWidgets.QHBoxLayout()
        seg_device_row.setSpacing(8)
        seg_device_row.addWidget(QtWidgets.QLabel("设备"))
        self.seg_device = NoWheelComboBox()
        # 不在启动时 import torch / 探测 CUDA（可能卡数秒），首帧后再补全 GPU 名称
        self.seg_device.addItem("GPU（检测中…）", "gpu")
        self.seg_device.addItem("CPU", "cpu")
        self.seg_device.setToolTip(
            "GPU 会使用显卡推理,通常更快并减少 CPU 内存压力。"
            "如果运行时报 CUDA/显存错误,再切回 CPU。")
        seg_device_row.addWidget(self.seg_device, 1)
        p2.addLayout(seg_device_row)

        seg_actions = QtWidgets.QHBoxLayout()
        seg_actions.setSpacing(8)
        self.btn_seg_run = QtWidgets.QPushButton("自动分割", objectName="Primary")
        self.btn_seg_download = QtWidgets.QPushButton("下载模型", objectName="Segment")
        self.btn_seg_run.clicked.connect(self._run_totalsegmentator)
        self.btn_seg_download.clicked.connect(self._download_totalseg_weights)
        seg_actions.addWidget(self.btn_seg_run)
        seg_actions.addWidget(self.btn_seg_download)
        p2.addLayout(seg_actions)

        seg_view_actions = QtWidgets.QHBoxLayout()
        seg_view_actions.setSpacing(8)
        self.btn_seg_focus = QtWidgets.QPushButton("突出显示", objectName="Segment")
        self.btn_seg_clear = QtWidgets.QPushButton("清除分割", objectName="Segment")
        self.btn_seg_focus.clicked.connect(self._highlight_segmentations)
        self.btn_seg_clear.clicked.connect(self._clear_segmentations)
        seg_view_actions.addWidget(self.btn_seg_focus)
        seg_view_actions.addWidget(self.btn_seg_clear)
        p2.addLayout(seg_view_actions)

        self.seg_status = QtWidgets.QLabel(
            "调用 TotalSegmentator 生成器官 mask,并叠加到 3D 视图。", objectName="Status")
        self.seg_status.setWordWrap(True)
        p2.addWidget(self.seg_status)
        self.seg_result = QtWidgets.QLabel("", objectName="Status")
        self.seg_result.setTextFormat(QtCore.Qt.RichText)
        self.seg_result.setWordWrap(True)
        p2.addWidget(self.seg_result)
        self.seg_preset.currentTextChanged.connect(self._on_seg_preset_changed)
        self._on_seg_preset_changed()
        p2.addStretch(1)
        self._plan_pages.addWidget(page2)

        # ---- 第③页 定位结节 ----
        page3 = QtWidgets.QWidget()
        p3 = QtWidgets.QVBoxLayout(page3)
        p3.setContentsMargins(0, 2, 0, 2)
        p3.setSpacing(6)
        p3.addWidget(self._page_header(
            3, "定位结节", "点击结节卡片即定位取景；双击卡片可放大经过结节的切片。"))
        self.nodule_section = self._section("结节定位 · 点击列表定位取景")
        self.nodule_section.setVisible(False)
        p3.addWidget(self.nodule_section)
        self.nodule_list = QtWidgets.QListWidget()
        self.nodule_list.setObjectName("NoduleList")
        self.nodule_list.setMaximumHeight(132)
        self.nodule_list.setVisible(False)
        self.nodule_list.itemClicked.connect(self._on_nodule_card_clicked)
        self.nodule_list.itemActivated.connect(self._on_nodule_card_activated)
        p3.addWidget(self.nodule_list)
        self.nodule_empty_hint = QtWidgets.QLabel(
            "先在第②页「自动分割」选择肺结节预设并运行，完成后这里会列出"
            "每个结节（体积/深度），点击即可定位取景。", objectName="Status")
        self.nodule_empty_hint.setWordWrap(True)
        p3.addWidget(self.nodule_empty_hint)
        p3.addStretch(1)
        self._plan_pages.addWidget(page3)
        self._nodule_cards = []
        self._nodule_selected = None

        # ---- 第④页 规划针道 ----
        page4 = QtWidgets.QWidget()
        p4 = QtWidgets.QVBoxLayout(page4)
        p4.setContentsMargins(0, 2, 0, 2)
        p4.setSpacing(6)
        p4.addWidget(self._page_header(
            4, "规划针道", "放置消融点后自动推荐避骨入针点，两点齐备连成针道。"))
        p4.addWidget(self._section("针道规划 · 入针点/消融点"))
        plan_hint = QtWidgets.QLabel(
            "双击/回车结节卡片定位并放大切片，或拖动切片十字到目标；"
            "放好消融点后可自动推荐避骨入针点，两点齐备自动连成针道。",
            objectName="Status")
        plan_hint.setWordWrap(True)
        p4.addWidget(plan_hint)

        self.btn_plan_auto = QtWidgets.QPushButton(
            "自动推荐避骨入针点", objectName="Segment")
        self.btn_plan_auto.setToolTip(
            "根据当前消融点、针杆长度和 CT 骨密度，搜索可达且避开骨骼的体表入针点。")
        self.btn_plan_auto.clicked.connect(self._recommend_entry_point)
        p4.addWidget(self.btn_plan_auto)

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
        p4.addLayout(plan_row)

        self.plan_status = QtWidgets.QLabel(
            "入针点:未放置   消融点:未放置", objectName="Status")
        self.plan_status.setWordWrap(True)
        p4.addWidget(self.plan_status)
        p4.addStretch(1)
        self._plan_pages.addWidget(page4)

        # ---- 第⑤页 仿真并导出 ----
        page5 = QtWidgets.QWidget()
        p5 = QtWidgets.QVBoxLayout(page5)
        p5.setContentsMargins(0, 2, 0, 2)
        p5.setSpacing(6)
        p5.addWidget(self._page_header(
            5, "仿真并导出", "确认针型与消融参数，运行仿真后导出规划核对单。"))
        # 消融针仿真 + 消融仿真两区并入本页（原先堆在向导卡片下方）
        self._fill_simulation_page(p5)
        p5.addWidget(self._section("导出核对单"))
        self.btn_plan_report = QtWidgets.QPushButton(
            "导出规划核对单", objectName="Segment")
        self.btn_plan_report.setToolTip(
            "汇总当前结节定位、针道规划与消融参数，生成可打印的核对单（HTML）。")
        self.btn_plan_report.clicked.connect(self._export_planning_report)
        p5.addWidget(self.btn_plan_report)
        p5.addStretch(1)
        self._plan_pages.addWidget(page5)

        # ---- 底部导航：分隔线 + 等宽对称大按钮 + 步数指示 ----
        nav_divider = QtWidgets.QFrame(objectName="StepDivider")
        nav_divider.setFixedHeight(1)
        core_lay.addWidget(nav_divider)
        nav_row = QtWidgets.QHBoxLayout()
        nav_row.setSpacing(10)
        self.btn_plan_prev = QtWidgets.QPushButton("◀  上一步", objectName="Segment")
        self.btn_plan_next = QtWidgets.QPushButton("下一步  ▶", objectName="Primary")
        for nav_button in (self.btn_plan_prev, self.btn_plan_next):
            nav_button.setMinimumHeight(36)
            nav_button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.plan_page_label = QtWidgets.QLabel("", objectName="Status")
        self.plan_page_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.btn_plan_prev.clicked.connect(
            lambda: self._show_plan_page(self._plan_pages.currentIndex() - 1))
        self.btn_plan_next.clicked.connect(
            lambda: self._show_plan_page(self._plan_pages.currentIndex() + 1))
        nav_row.addWidget(self.btn_plan_prev, 1)
        nav_row.addWidget(self.plan_page_label, 0)
        nav_row.addWidget(self.btn_plan_next, 1)
        core_lay.addLayout(nav_row)
        self._update_plan_nav()

        lay.addWidget(core, 1)

        return scroll

    def _fill_simulation_page(self, lay):
        """向导第⑤页的消融仿真内容：针型参数 + 功率/时长/启停控制。

        原先是堆在向导卡片下方的独立面板；并入第⑤页后，"仿真并导出"
        一页内即可完成确认针型 → 运行仿真 → 导出核对单的完整动作。
        """
        # 2 - Ablation needle planning
        lay.addWidget(self._section("消融针仿真"))
        self.needle_preset = NoWheelComboBox()
        self.needle_preset.addItems(ablation.preset_names())
        self.needle_preset.currentTextChanged.connect(self._on_needle_preset_changed)
        lay.addWidget(self.needle_preset)

        needle_grid = QtWidgets.QGridLayout()
        needle_grid.setHorizontalSpacing(8)
        needle_grid.setVerticalSpacing(6)
        self.needle_diameter = NoWheelDoubleSpinBox()
        self.needle_diameter.setRange(0.2, 5.0)
        self.needle_diameter.setSingleStep(0.1)
        self.needle_diameter.setSuffix(" mm")
        self.needle_active = NoWheelDoubleSpinBox()
        self.needle_active.setRange(1.0, 80.0)
        self.needle_active.setSingleStep(1.0)
        self.needle_active.setSuffix(" mm")
        self.needle_shaft = NoWheelDoubleSpinBox()
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
        lay.addSpacing(8)

        lay.addWidget(self._section("消融仿真"))
        sim_hint = QtWidgets.QLabel(
            "基于当前针道生成随时间增长的消融范围。", objectName="Status")
        sim_hint.setWordWrap(True)
        lay.addWidget(sim_hint)

        sim_grid = QtWidgets.QGridLayout()
        sim_grid.setHorizontalSpacing(8)
        sim_grid.setVerticalSpacing(6)
        self.sim_power = NoWheelDoubleSpinBox()
        self.sim_power.setRange(5.0, 200.0)
        self.sim_power.setSingleStep(5.0)
        self.sim_power.setSuffix(" W")
        preset = ablation.preset_by_name(self.needle_preset.currentText())
        self.sim_power.setValue(preset.get("power_w", 30.0))
        self.sim_time = NoWheelDoubleSpinBox()
        self.sim_time.setRange(30.0, 1800.0)
        self.sim_time.setSingleStep(30.0)
        self.sim_time.setSuffix(" s")
        self.sim_time.setValue(300.0)
        self.sim_speed = NoWheelComboBox()
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

    def _build_mwa_panel(self):
        """按实体设备面板构建四通道微波消融仪显示与调节区。"""
        panel = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # 通讯详情仍供状态栏与日志使用，不在设备显示屏中重复占位。
        self.mwa_status = QtWidgets.QLabel("未连接", panel)
        self.mwa_status.hide()

        status_chip = QtWidgets.QFrame(panel, objectName="MwaStatusChip")
        status_chip_lay = QtWidgets.QHBoxLayout(status_chip)
        status_chip_lay.setContentsMargins(10, 5, 10, 5)
        status_chip_lay.setSpacing(6)
        self.mwa_online_dot = QtWidgets.QLabel(objectName="SerialStatusDot")
        self.mwa_online_dot.setFixedSize(8, 8)
        self.mwa_online_dot.setProperty("connected", False)
        self.mwa_link_label = QtWidgets.QLabel("未连接", objectName="MwaLink")
        status_chip_lay.addWidget(
            self.mwa_online_dot, 0, QtCore.Qt.AlignVCenter)
        status_chip_lay.addWidget(
            self.mwa_link_label, 0, QtCore.Qt.AlignVCenter)
        status_chip.setProperty("online", False)
        self.mwa_status_chip = status_chip
        panel.mwa_status_chip = status_chip

        screen = QtWidgets.QFrame(objectName="MwaDeviceScreen")
        screen_lay = QtWidgets.QHBoxLayout(screen)
        screen_lay.setContentsMargins(0, 0, 0, 0)
        screen_lay.setSpacing(12)

        # 读数辉光颜色（与 QSS 中的通道色一致）
        glow_colors = {
            "temp": QtGui.QColor(45, 212, 191, 150),
            "rod": QtGui.QColor(251, 191, 36, 140),
            "time": QtGui.QColor(56, 189, 248, 150),
            "power": QtGui.QColor(250, 204, 21, 130),
        }

        def make_value(kind, initial, compact=False):
            """监护仪风格的大号读数标签：通道色由 QSS 按 kind 匹配，
            并叠加同色霓虹辉光（报警时由 _set_mwa_alarm_state 切红）。"""
            label = QtWidgets.QLabel(initial, objectName="MwaValue")
            label.setProperty("kind", kind)
            label.setProperty("alarm", False)
            label.setProperty("compact", bool(compact))
            label.setAlignment(QtCore.Qt.AlignCenter)
            label.setMinimumHeight(40 if compact else 84)
            label.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Ignored,
                QtWidgets.QSizePolicy.Policy.Expanding,
            )
            glow = QtWidgets.QGraphicsDropShadowEffect(label)
            glow.setBlurRadius(22 if compact else 30)
            glow.setOffset(0, 0)
            glow.setColor(glow_colors[kind])
            label.setGraphicsEffect(glow)
            label.setProperty("glow_rgba", glow_colors[kind])
            return label

        def icon_chip(kind, size=34, icon_px=22):
            chip = QtWidgets.QFrame(objectName="MwaIconChip")
            chip.setProperty("kind", kind)
            chip.setFixedSize(size, size)
            chip_lay = QtWidgets.QVBoxLayout(chip)
            chip_lay.setContentsMargins(0, 0, 0, 0)
            icon = QtWidgets.QLabel()
            icon.setPixmap(self._mwa_icon(kind).pixmap(icon_px, icon_px))
            icon.setAlignment(QtCore.Qt.AlignCenter)
            chip_lay.addWidget(icon)
            return chip

        def step_group(on_add, on_sub):
            group = QtWidgets.QFrame(objectName="MwaStepGroup")
            row = QtWidgets.QHBoxLayout(group)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            btn_sub = QtWidgets.QPushButton("−", objectName="MwaStep")
            btn_add = QtWidgets.QPushButton("+", objectName="MwaStep")
            for btn in (btn_sub, btn_add):
                btn.setMinimumHeight(40)
                btn.setSizePolicy(
                    QtWidgets.QSizePolicy.Policy.Expanding,
                    QtWidgets.QSizePolicy.Policy.Fixed,
                )
                btn.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            btn_sub.setAccessibleName("减少")
            btn_add.setAccessibleName("增加")
            btn_sub.setToolTip("减少")
            btn_add.setToolTip("增加")
            btn_add.clicked.connect(on_add)
            btn_sub.clicked.connect(on_sub)
            # 左加右减
            row.addWidget(btn_add)
            row.addWidget(btn_sub)
            return group, btn_add, btn_sub

        def text_label(text, object_name):
            return QtWidgets.QLabel(text, objectName=object_name)

        def footer_line(*widgets):
            line = QtWidgets.QFrame(objectName="MwaFooterLine")
            row = QtWidgets.QHBoxLayout(line)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            for widget in widgets:
                if widget is None:
                    row.addStretch(1)
                else:
                    row.addWidget(widget, 0, QtCore.Qt.AlignVCenter)
            return line

        def footer_box(lines, height=56):
            footer = QtWidgets.QFrame(objectName="MwaChannelFooter")
            footer.setFixedHeight(height)
            footer_lay = QtWidgets.QVBoxLayout(footer)
            footer_lay.setContentsMargins(2, 4, 2, 0)
            footer_lay.setSpacing(3)
            if len(lines) == 1:
                footer_lay.addStretch(1)
            for line in lines:
                footer_lay.addWidget(line)
            if len(lines) == 1:
                footer_lay.addStretch(1)
            return footer

        def state_label():
            label = text_label("●  正常", "MwaState")
            label.setProperty("alarm", False)
            return label

        def channel(kind, caption, unit, value, footer, controls=None,
                    compact=False):
            frame = QtWidgets.QFrame(objectName="MwaChannel")
            frame.setProperty("kind", kind)
            frame.setSizePolicy(
                QtWidgets.QSizePolicy.Policy.Ignored,
                QtWidgets.QSizePolicy.Policy.Expanding,
            )
            channel_lay = QtWidgets.QVBoxLayout(frame)
            if compact:
                channel_lay.setContentsMargins(12, 9, 12, 8)
                channel_lay.setSpacing(7)
            else:
                channel_lay.setContentsMargins(14, 12, 14, 10)
                channel_lay.setSpacing(9)

            header = QtWidgets.QFrame(objectName="MwaChannelHeader")
            header.setFixedHeight(30 if compact else 36)
            header_lay = QtWidgets.QHBoxLayout(header)
            header_lay.setContentsMargins(0, 0, 0, 0)
            header_lay.setSpacing(8 if compact else 9)
            chip = icon_chip(kind, 28, 18) if compact else icon_chip(kind)
            header_lay.addWidget(chip, 0, QtCore.Qt.AlignVCenter)
            header_lay.addWidget(
                text_label(caption, "MwaChannelTitle"),
                0, QtCore.Qt.AlignVCenter)
            header_lay.addStretch(1)
            unit_badge = text_label(unit, "MwaUnitBadge")
            unit_badge.setProperty("kind", kind)
            header_lay.addWidget(unit_badge, 0, QtCore.Qt.AlignVCenter)
            channel_lay.addWidget(header)

            value_holder = QtWidgets.QFrame(objectName="MwaValueHolder")
            value_holder.setProperty("kind", kind)
            value_holder.setProperty("alarm", False)
            value_lay = QtWidgets.QVBoxLayout(value_holder)
            if compact:
                value_lay.setContentsMargins(10, 4, 10, 4)
            else:
                value_lay.setContentsMargins(12, 8, 12, 8)
            value_lay.addWidget(value)
            channel_lay.addWidget(value_holder, 1)

            if controls is not None:
                channel_lay.addWidget(controls)

            bottom_line = QtWidgets.QFrame(objectName="MwaSectionDivider")
            bottom_line.setFixedHeight(1)
            channel_lay.addWidget(bottom_line)
            channel_lay.addWidget(footer)
            return frame

        self.mwa_bypass_temp = make_value("temp", "15.0", compact=True)
        self.mwa_rod_temp = make_value("rod", "15.0", compact=True)
        self.mwa_time_value = make_value("time", "00:00")
        self.mwa_power_value = make_value("power", "0")

        self.mwa_bypass_setpoint = text_label("15 ℃", "MwaFooterValue")
        self.mwa_side_alarm = self.mwa_bypass_setpoint
        self.mwa_bypass_state = state_label()
        self.mwa_rod_limit = text_label("— ℃", "MwaFooterValue")
        self.mwa_rod_state = state_label()
        self.mwa_set_time = text_label("00:00", "MwaFooterValue")
        self.mwa_elapsed_time = text_label("00:00:00", "MwaFooterValue")
        self.mwa_set_power = text_label("0 W", "MwaFooterValue")
        self.mwa_swr = text_label("--", "MwaFooterValue")

        time_steps, self.btn_mwa_time_add, self.btn_mwa_time_sub = step_group(
            lambda: self._mwa.adjust_work_time(10),
            lambda: self._mwa.adjust_work_time(-10))
        power_steps, self.btn_mwa_power_add, self.btn_mwa_power_sub = step_group(
            lambda: self._mwa.adjust_power(1),
            lambda: self._mwa.adjust_power(-1))

        bypass_footer = footer_box([footer_line(
            text_label("设定温度", "MwaFooterLabel"),
            self.mwa_bypass_setpoint,
            None,
            self.mwa_bypass_state,
        )], height=32)
        rod_footer = footer_box([footer_line(
            text_label("上限温度", "MwaFooterLabel"),
            self.mwa_rod_limit,
            None,
            self.mwa_rod_state,
        )], height=32)
        time_footer = footer_box([
            footer_line(
                text_label("设置时间", "MwaFooterLabel"),
                self.mwa_set_time,
                None,
            ),
            footer_line(
                text_label("累计时间", "MwaFooterLabel"),
                self.mwa_elapsed_time,
                None,
            ),
        ])
        power_footer = footer_box([footer_line(
            text_label("设定功率", "MwaFooterLabel"),
            self.mwa_set_power,
            None,
            text_label("SWR", "MwaFooterLabel"),
            self.mwa_swr,
        )])

        # 左列：两个温度通道纵向堆叠（紧凑卡片）；右侧：时间/功率放大
        temps_col = QtWidgets.QVBoxLayout()
        temps_col.setSpacing(12)
        temps_col.addWidget(
            channel("temp", "旁路温度", "℃", self.mwa_bypass_temp,
                    bypass_footer, compact=True), 1)
        temps_col.addWidget(
            channel("rod", "针杆温度", "℃", self.mwa_rod_temp,
                    rod_footer, compact=True), 1)
        screen_lay.addLayout(temps_col, 5)
        screen_lay.addWidget(
            channel("time", "工作时间", "min:s", self.mwa_time_value,
                    time_footer, time_steps), 8)
        screen_lay.addWidget(
            channel("power", "微波功率", "W", self.mwa_power_value,
                    power_footer, power_steps), 8)

        lay.addWidget(screen, 1)

        action_row = QtWidgets.QHBoxLayout()
        action_row.setSpacing(12)
        self.btn_mwa_cooling = QtWidgets.QPushButton(
            "蠕动泵启动", objectName="MwaAction")
        self.btn_mwa_cooling.setIcon(self._mwa_icon("cool"))
        self.btn_mwa_cooling.setProperty("kind", "cool")
        self.btn_mwa_microwave = QtWidgets.QPushButton(
            "微波启动", objectName="MwaAction")
        self.btn_mwa_microwave.setIcon(self._mwa_icon("wave"))
        self.btn_mwa_microwave.setProperty("kind", "mw")
        for button in (self.btn_mwa_cooling, self.btn_mwa_microwave):
            button.setIconSize(QtCore.QSize(22, 22))
            button.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.btn_mwa_cooling.clicked.connect(self._on_mwa_cooling)
        self.btn_mwa_microwave.clicked.connect(self._on_mwa_microwave)
        action_row.addWidget(self.btn_mwa_cooling)
        action_row.addWidget(self.btn_mwa_microwave)
        lay.addLayout(action_row)
        return panel
    @staticmethod
    def _mwa_icon(kind, tint=None):
        """微波消融面板的小图标（QPainter 绘制，不依赖 emoji 字体）。

        tint 可覆盖默认颜色（如激活态按钮上改用白色图标）。
        """
        size = 32
        pixmap = QtGui.QPixmap(size, size)
        pixmap.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pixmap)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)

        p.scale(size / 20.0, size / 20.0)
        if kind in ("temp", "rod"):
            # 温度计：玻璃管 + 汞柱 + 底部球（旁温青绿=水冷 / 杆温琥珀=发热）
            color = QtGui.QColor("#2DD4BF" if kind == "temp" else "#FBBF24")
            p.setPen(QtGui.QPen(QtGui.QColor("#CBD5E1"), 1.4))
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawRoundedRect(QtCore.QRectF(8.1, 2.2, 3.8, 10.6), 1.9, 1.9)
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(color)
            p.drawRect(QtCore.QRectF(9.2, 6.6, 1.6, 6.8))
            p.drawEllipse(QtCore.QPointF(10.0, 15.2), 3.3, 3.3)
        elif kind == "time":
            # 时钟：表盘 + 指针
            color = QtGui.QColor("#38BDF8")
            p.setPen(QtGui.QPen(color, 1.8))
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawEllipse(QtCore.QRectF(3.0, 3.0, 14.0, 14.0))
            p.drawLine(QtCore.QPointF(10.0, 10.2), QtCore.QPointF(10.0, 5.6))
            p.drawLine(QtCore.QPointF(10.0, 10.2), QtCore.QPointF(13.4, 11.8))
        elif kind == "power":
            # 闪电：输出功率
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(QtGui.QColor("#FACC15"))
            bolt = QtGui.QPolygonF([
                QtCore.QPointF(11.6, 1.8), QtCore.QPointF(5.2, 11.0),
                QtCore.QPointF(9.2, 11.0), QtCore.QPointF(7.9, 18.2),
                QtCore.QPointF(14.8, 8.6), QtCore.QPointF(10.6, 8.6),
            ])
            p.drawPolygon(bolt)
        elif kind == "alarm":
            # 铃铛：报警阈值
            color = QtGui.QColor("#FB923C")
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(color)
            dome = QtGui.QPainterPath()
            dome.moveTo(4.6, 13.0)
            dome.cubicTo(4.6, 5.4, 15.4, 5.4, 15.4, 13.0)
            dome.closeSubpath()
            p.drawPath(dome)
            p.drawRoundedRect(QtCore.QRectF(3.4, 12.6, 13.2, 1.9), 0.9, 0.9)
            p.drawEllipse(QtCore.QPointF(10.0, 16.4), 1.5, 1.5)
        elif kind == "mode":
            # 正弦波：输出模式
            color = QtGui.QColor("#4ADE80")
            p.setPen(QtGui.QPen(color, 1.8, QtCore.Qt.SolidLine,
                                QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
            p.setBrush(QtCore.Qt.NoBrush)
            wave = QtGui.QPainterPath(QtCore.QPointF(2.4, 10.0))
            wave.cubicTo(4.6, 3.6, 7.6, 3.6, 10.0, 10.0)
            wave.cubicTo(12.4, 16.4, 15.4, 16.4, 17.6, 10.0)
            p.drawPath(wave)
        elif kind == "cool":
            # 雪花：冷却循环
            color = QtGui.QColor(tint or "#7DD3FC")
            pen = QtGui.QPen(color, 1.6, QtCore.Qt.SolidLine,
                             QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
            p.setPen(pen)
            center = QtCore.QPointF(10.0, 10.0)
            for i in range(6):
                angle = math.radians(i * 60.0 + 90.0)
                dx, dy = math.cos(angle), math.sin(angle)
                tip = QtCore.QPointF(center.x() + dx * 7.6,
                                     center.y() - dy * 7.6)
                p.drawLine(center, tip)
                # 每根主枝上加一对短侧枝，更像雪花
                base = QtCore.QPointF(center.x() + dx * 4.6,
                                      center.y() - dy * 4.6)
                for branch in (angle + math.radians(50),
                               angle - math.radians(50)):
                    bx, by = math.cos(branch), math.sin(branch)
                    p.drawLine(base, QtCore.QPointF(base.x() + bx * 2.4,
                                                    base.y() - by * 2.4))
        elif kind == "wave":
            # 同心弧线：微波辐射
            color = QtGui.QColor(tint or "#FB923C")
            p.setPen(QtGui.QPen(color, 1.8, QtCore.Qt.SolidLine,
                                QtCore.Qt.RoundCap))
            p.setBrush(QtCore.Qt.NoBrush)
            for radius in (4.2, 7.4, 10.6):
                p.drawArc(QtCore.QRectF(4.6 - radius, 15.4 - radius,
                                        radius * 2, radius * 2),
                          8 * 16, 74 * 16)
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(color)
            p.drawEllipse(QtCore.QPointF(4.6, 15.4), 1.7, 1.7)

        p.end()
        return QtGui.QIcon(pixmap)

    @staticmethod
    def _serial_icon(kind, tint=None):
        """设备连接面板的小图标（QPainter 绘制，不依赖 emoji 字体）。

        tint 可覆盖默认颜色（如按钮激活态改用白色图标）。
        """
        size = 32
        pixmap = QtGui.QPixmap(size, size)
        pixmap.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(pixmap)
        p.setRenderHint(QtGui.QPainter.Antialiasing, True)
        p.scale(size / 20.0, size / 20.0)

        if kind == "com":
            # 串口插头：接口主体 + 引脚点 + 出线（主机串口）
            color = QtGui.QColor(tint or "#38BDF8")
            p.setPen(QtGui.QPen(color, 1.5, QtCore.Qt.SolidLine,
                                QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawRoundedRect(QtCore.QRectF(4.2, 4.4, 8.6, 8.4), 1.8, 1.8)
            p.drawLine(QtCore.QPointF(12.8, 8.6), QtCore.QPointF(16.6, 8.6))
            p.drawLine(QtCore.QPointF(16.6, 8.6), QtCore.QPointF(15.8, 6.6))
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(color)
            for cx in (6.2, 8.0, 9.8, 11.6):
                p.drawEllipse(QtCore.QPointF(cx, 9.6), 0.9, 0.9)
        elif kind == "vna":
            # 网络分析仪：频谱曲线（基线 + 峰包 + 刻度）
            color = QtGui.QColor(tint or "#A78BFA")
            p.setPen(QtGui.QPen(color, 1.7, QtCore.Qt.SolidLine,
                                QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawLine(QtCore.QPointF(2.6, 16.6), QtCore.QPointF(17.4, 16.6))
            curve = QtGui.QPainterPath(QtCore.QPointF(2.6, 15.8))
            curve.cubicTo(6.0, 15.8, 6.6, 5.2, 10.0, 5.2)
            curve.cubicTo(13.4, 5.2, 14.0, 15.8, 17.4, 15.8)
            p.drawPath(curve)
            p.setPen(QtGui.QPen(QtGui.QColor(color.red(), color.green(),
                                             color.blue(), 150), 1.2))
            for x in (5.2, 10.0, 14.8):
                p.drawLine(QtCore.QPointF(x, 17.6), QtCore.QPointF(x, 18.8))
        elif kind == "mon":
            # 终端监视：窗口 + 提示符（串口收发日志）
            color = QtGui.QColor(tint or "#2DD4BF")
            p.setPen(QtGui.QPen(color, 1.6, QtCore.Qt.SolidLine,
                                QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawRoundedRect(QtCore.QRectF(2.8, 4.6, 14.4, 10.8), 2.2, 2.2)
            p.drawLine(QtCore.QPointF(2.8, 7.4), QtCore.QPointF(17.2, 7.4))
            p.drawLine(QtCore.QPointF(4.6, 5.8), QtCore.QPointF(6.6, 5.8))
            p.drawLine(QtCore.QPointF(8.2, 5.8), QtCore.QPointF(10.2, 5.8))
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(color)
            p.drawRect(QtCore.QRectF(4.6, 9.6, 6.6, 1.6))
            p.drawRect(QtCore.QRectF(4.6, 12.2, 4.2, 1.6))
        elif kind == "refresh":
            # 刷新：循环箭头
            color = QtGui.QColor(tint or "#94A3B8")
            pen = QtGui.QPen(color, 1.8, QtCore.Qt.SolidLine,
                             QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawArc(QtCore.QRectF(4.0, 4.0, 12.0, 12.0), 55 * 16, 250 * 16)
            p.setBrush(color)
            p.setPen(QtCore.Qt.NoPen)
            head = QtGui.QPolygonF([
                QtCore.QPointF(15.8, 2.6), QtCore.QPointF(16.8, 6.6),
                QtCore.QPointF(12.8, 6.0),
            ])
            p.drawPolygon(head)
        elif kind == "clear":
            # 清空：圆叉
            color = QtGui.QColor(tint or "#F87171")
            pen = QtGui.QPen(color, 1.8, QtCore.Qt.SolidLine,
                             QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawEllipse(QtCore.QRectF(3.4, 3.4, 13.2, 13.2))
            p.drawLine(QtCore.QPointF(7.4, 7.4), QtCore.QPointF(12.6, 12.6))
            p.drawLine(QtCore.QPointF(12.6, 7.4), QtCore.QPointF(7.4, 12.6))
        elif kind == "unplug":
            # 断开：插头 + 中间断口（断开按钮）
            color = QtGui.QColor(tint or "#F87171")
            p.setPen(QtGui.QPen(color, 1.5, QtCore.Qt.SolidLine,
                                QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
            p.setBrush(QtCore.Qt.NoBrush)
            p.drawRoundedRect(QtCore.QRectF(4.2, 4.4, 8.6, 8.4), 1.8, 1.8)
            p.drawLine(QtCore.QPointF(12.8, 8.6), QtCore.QPointF(16.6, 8.6))
            p.drawLine(QtCore.QPointF(16.6, 8.6), QtCore.QPointF(15.8, 6.6))
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(color)
            for cx in (6.2, 8.0, 9.8, 11.6):
                p.drawEllipse(QtCore.QPointF(cx, 9.6), 0.9, 0.9)
            p.setPen(QtGui.QPen(QtGui.QColor("#0F172A"), 2.6,
                                QtCore.Qt.SolidLine,
                                QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin))
            p.drawLine(QtCore.QPointF(13.4, 5.2), QtCore.QPointF(16.8, 11.4))
        elif kind == "send":
            # 发送：纸飞机
            color = QtGui.QColor(tint or "#FFFFFF")
            p.setPen(QtCore.Qt.NoPen)
            p.setBrush(color)
            plane = QtGui.QPolygonF([
                QtCore.QPointF(2.6, 10.4), QtCore.QPointF(17.4, 3.4),
                QtCore.QPointF(13.8, 16.6), QtCore.QPointF(11.0, 11.8),
            ])
            p.drawPolygon(plane)
            p.setBrush(QtGui.QColor(color.red(), color.green(),
                                    color.blue(), 170))
            p.drawPolygon(QtGui.QPolygonF([
                QtCore.QPointF(11.0, 11.8), QtCore.QPointF(13.8, 16.6),
                QtCore.QPointF(9.8, 15.6),
            ]))

        p.end()
        return QtGui.QIcon(pixmap)

    def _ensure_mwa_overlay(self):
        if self._mwa_overlay is not None:
            return self._mwa_overlay
        if self._mwa_panel is None:
            self._mwa_panel = self._build_mwa_panel()
        self._mwa_overlay = MwaControlOverlay(self, self._mwa_panel)
        return self._mwa_overlay

    def _prewarm_overlays(self):
        """空闲时先把弹窗建好并完成一次样式/布局解算。

        首次点开时的卡顿主要来自这一次性开销：新建顶层窗口、对整棵子树套用
        QSS、首次布局与首帧绘制。提前在空闲时完成控件构建与布局解算；背景
        仍在每次打开时实时抓取，确保显示的是当前病例和当前视角。
        """
        for ensure in (self._ensure_mwa_overlay, self._ensure_serial_overlay,
                       self._ensure_planning_alert_overlay,
                       self._ensure_file_dialog_overlay):
            try:
                overlay = ensure()
            except Exception:
                log.exception("预热弹窗失败")
                continue
            # ensurePolished() 会触发 QSS 解析与字体度量；再算一次卡片尺寸，
            # 把首次布局的开销也提前付掉。窗口本身始终保持隐藏。
            overlay.ensurePolished()
            card = getattr(overlay, "_card", None)
            if card is not None:
                card.ensurePolished()
                card.adjustSize()

    def _prewarm_frosted_backgrounds(self):
        overlays = [self._mwa_overlay, self._serial_overlay,
                    self._planning_alert_overlay]
        try:
            overlays.append(self._ensure_file_dialog_overlay())
        except Exception:
            log.exception("预热文件对话框失败")
        for overlay in overlays:
            if overlay is None:
                continue
            try:
                overlay.prewarm_background()
            except Exception:
                log.exception("预热模糊背景失败")

    def _show_mwa_overlay(self):
        """右上角入口：弹窗从居中位置下方浮现。"""
        overlay = self._ensure_mwa_overlay()
        overlay.present()

    def _ensure_serial_overlay(self):
        if self._serial_overlay is not None:
            return self._serial_overlay
        if self._serial_panel is None:
            self._serial_panel = self._build_serial_panel()
        self._serial_overlay = SerialControlOverlay(self, self._serial_panel)
        return self._serial_overlay

    def _show_serial_overlay(self):
        """右上角入口：弹窗从居中位置下方浮现。

        端口枚举放到弹窗出现之后再做：QSerialPortInfo.availablePorts() 在 Windows
        上遇到蓝牙虚拟串口可能要几百毫秒甚至更久，同步做会让点击后迟迟不弹窗。
        """
        overlay = self._ensure_serial_overlay()
        self._refresh_device_summary()
        overlay.present()
        QtCore.QTimer.singleShot(0, self._refresh_device_ports)

    def _section(self, text):
        """创建一个分区标题标签，样式由 QSS #SectionTitle 控制。
        修改分区标题样式：编辑 style.py 中 QLabel#SectionTitle 的 QSS 规则。
        """
        return QtWidgets.QLabel(text, objectName="SectionTitle")

    # ============================================================
    # 串口连接
    # ============================================================

    def _refresh_serial_ports(self, ports=None):
        if ports is None or isinstance(ports, bool):
            ports = self._serial.available_ports()
        ports = list(ports)
        current = self.serial_port.currentData()
        self.serial_port.blockSignals(True)
        self.serial_port.clear()
        for port in ports:
            self.serial_port.addItem(port["label"], port["name"])
        if not ports:
            self.serial_port.addItem("未发现串口", "")
        if current:
            index = self.serial_port.findData(current)
            if index >= 0:
                self.serial_port.setCurrentIndex(index)
        self.serial_port.blockSignals(False)
        if not self._serial.is_connected():
            self.serial_status.setText("串口未连接。" if ports else "未发现可用串口。")
        self._update_serial_controls()

    def _update_serial_controls(self, *_):
        connected = self._serial.is_connected()
        has_port = bool(self.serial_port.currentData())
        self.serial_port.setEnabled(not connected)
        self.serial_baud.setEnabled(not connected)
        self.btn_serial_connect.setEnabled(connected or has_port)
        self._sync_device_connect_button(self.btn_serial_connect, connected)
        self.mon_serial.set_send_enabled(connected)
        self._sync_link_state(getattr(self, "serial_link_state", None), connected)
        self._repolish_prop(self.ser_card, "connected", connected)
        self._repolish_prop(getattr(self, "ser_page", None), "connected", connected)
        self._repolish_prop(self.serial_status, "connected", connected)
        self._update_serial_menu_indicator()
        self._refresh_device_summary()

    def _update_serial_menu_indicator(self):
        """通用串口、微波消融仪、网分任一链路连接即点亮入口。"""
        serial_open = self._serial.is_connected()
        mwa_open = self._mwa.is_port_open()
        vna = self._vna.is_connected()
        icon = self._header_icon(
            "serial", active=serial_open or mwa_open or vna, size=22)
        if hasattr(self, "btn_serial"):
            self.btn_serial.setIcon(icon)
        tip = "通用串口%s；微波消融仪%s；网络分析仪%s" % (
            "已连接" if serial_open else "未连接",
            "已连接" if mwa_open else "未连接",
            "已连接" if vna else "未连接")
        if hasattr(self, "btn_serial"):
            self.btn_serial.setToolTip(tip)

    def _toggle_serial_connection(self):
        if self._serial.is_connected():
            self._serial.disconnect_port()
            return
        port_name = self.serial_port.currentData()
        if not port_name:
            self.serial_status.setText("未选择可用串口。")
            self.statusBar().showMessage("未选择可用串口。")
            return
        baud_rate = int(self.serial_baud.currentText())
        self._serial.connect_port(port_name, baud_rate)

    def _send_serial_text(self, text):
        append_newline = self.mon_serial.newline_chk.isChecked()
        if self.mon_serial.tx_hex_chk.isChecked():
            try:
                payload = serial_connection.parse_hex_bytes(text)
            except ValueError as exc:
                message = "HEX 发送格式错误：%s" % exc
                self.serial_status.setText(message)
                self._repolish_prop(self.serial_status, "error", True)
                self.statusBar().showMessage(message)
                return
            if append_newline:
                payload += b"\r\n"
            if self._serial.send_bytes(payload):
                self._repolish_prop(self.serial_status, "error", False)
                self.statusBar().showMessage("通用串口 HEX 数据已发送。")
                hex_text = payload.hex(" ").upper()
                self.mon_serial.note_tx(hex_text)
                log.info("通用串口发送(HEX): %s", hex_text)
            return
        if self._serial.send_text(text, append_newline):
            self.statusBar().showMessage("通用串口数据已发送。")
            self.mon_serial.note_tx(text)
            log.info("通用串口发送(文本): %s", text)

    def _on_serial_rx_mode_changed(self, _enabled):
        # Do not let undecoded bytes collected in the previous display mode
        # appear later in the newly selected mode.
        self._serial.clear_text_receive_buffer()
        self._serial_rx_buffer = ""

    def _on_serial_status_changed(self, message, connected):
        self._repolish_prop(self.serial_status, "error", False)
        self.serial_status.setText(message)
        self.statusBar().showMessage(message)
        self._update_serial_controls()
        if not connected:
            # 断开后清空行缓冲；仅当读数来源是本通道时才撤下传感器读数，
            # 避免误清另一个串口仍在更新的实时数据。
            self._serial_rx_buffer = ""
            self._rtx_parser.reset()
            if self._imu_source in ("ser1", "rtx"):
                self._imu_source = None
                self.viewer.clear_imu_sensor_readout()

    # 下位机(RF)持续上报的 IMU 温度/角度遥测行：
    #   $IMU,<temp_x10>,<pitch_x10>,<roll_x10>,<yaw_x10>*<CK>
    # temp_x10 单位 0.1°C，角度单位 0.1°（有符号），CK 为 '$' 与 '*' 之间所有
    # 字符的 XOR 校验（2 位十六进制，NMEA 风格）。纯 ASCII，不受其它中文调试
    # 文本干扰，逐行匹配即可。
    _IMU_LINE_RE = re.compile(
        r"\$(IMU,(-?\d+),(-?\d+),(-?\d+),(-?\d+))\*([0-9A-Fa-f]{2})\s*$"
    )

    def _consume_rx_lines(self, buffer, text, source):
        """任意串口通用的逐行解析：按 \n 切分接收流，逐行识别 IMU 遥测。

        通用串口收到 $IMU 遥测时解析，其余内容原样透传显示。
        返回剩余未完结的行缓冲，由调用方保存。
        """
        buffer += text
        if len(buffer) > 8192:
            # 防御：异常情况下（长期无换行）避免缓冲无限增长。
            buffer = buffer[-8192:]
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            self._parse_imu_telemetry_line(line.strip(), source)
        return buffer

    def _on_serial_data_received(self, text):
        if not self.mon_serial.rx_hex_chk.isChecked():
            self.mon_serial.append_rx(text)
            log.info("通用串口接收(文本): %s", text.rstrip())
        self._serial_rx_buffer = self._consume_rx_lines(
            self._serial_rx_buffer, text, "ser1")

    def _on_serial_raw_data_received(self, raw):
        """Parse the RTX 0x13 binary uplink without converting it to text."""
        if self.mon_serial.rx_hex_chk.isChecked():
            hex_text = bytes(raw).hex(" ").upper()
            self.mon_serial.append_rx(hex_text + "\n")
            log.info("通用串口接收(HEX): %s", hex_text)
        frames, issues = self._rtx_parser.feed(bytes(raw))
        for issue in issues:
            log.warning("RTX 数据帧丢弃: %s", issue)
        for frame in frames:
            try:
                data = rtx_telemetry.decode_rtx_data(frame)
            except ValueError as exc:
                log.warning("RTX 数据帧解析失败: %s", exc)
                continue

            self._imu_source = "rtx"
            self.viewer.set_imu_sensor_readout(
                data["rod_temp_c"],
                data["pitch_deg"],
                data["roll_deg"],
                data["yaw_deg"],
                magnetic=data["magnetic_ut"],
            )
            heading = data["magnetic_heading_deg"]
            heading_text = "--" if heading is None else "%.1f°" % heading
            if not self.mon_serial.rx_hex_chk.isChecked():
                self.mon_serial.append_info(
                    "RTX 0x13 · 姿态(%+.1f, %+.1f, %+.1f)° · 磁方位 %s\n"
                    % (data["pitch_deg"], data["roll_deg"], data["yaw_deg"],
                       heading_text)
                )
            # The RTX session watchdog expects AA 93 93 BB after every valid
            # uplink sample.  Acknowledge only after checksum and field parsing
            # have both succeeded.
            ack = rtx_telemetry.build_data_ack()
            if self._serial.send_bytes(ack):
                log.info("通用串口发送(自动 ACK/HEX): %s",
                         ack.hex(" ").upper())

    def _parse_imu_telemetry_line(self, line, source):
        """解析一行 IMU 温度/角度遥测，校验通过后更新 3D 视图上的传感器读数。

        source 是来源通道标识：读数只受来源通道的
        断开事件清理，避免误清另一路仍在更新的数据。
        """
        if "$IMU," not in line:
            return
        # 容错：一行里可能夹杂前导调试字符，从 '$IMU' 起截取。
        match = self._IMU_LINE_RE.search(line)
        if match is None:
            return
        body = match.group(1)                 # "IMU,...."，参与校验的部分
        checksum = 0
        for ch in body:
            checksum ^= ord(ch)
        if checksum != int(match.group(6), 16):
            log.warning("IMU 遥测校验失败: %s", line)
            return
        temp_c = int(match.group(2)) / 10.0
        pitch = int(match.group(3)) / 10.0
        roll = int(match.group(4)) / 10.0
        yaw = int(match.group(5)) / 10.0
        self._imu_source = source
        self.viewer.set_imu_sensor_readout(temp_c, pitch, roll, yaw)

    def _on_serial_error(self, message):
        self.serial_status.setText(message)
        self._repolish_prop(self.serial_status, "error", True)
        self.statusBar().showMessage(message)
        log.warning("通用串口错误: %s", message)

    # ============================================================
    # 网络分析仪（TCP/SCPI）
    # ============================================================

    def _update_vna_controls(self, *_):
        connected = self._vna.is_connected()
        self.vna_ip.setEnabled(not connected)
        self.vna_port.setEnabled(not connected)
        self._sync_device_connect_button(self.btn_vna_connect, connected)
        self._sync_link_state(getattr(self, "vna_link_state", None), connected)
        self._repolish_prop(self.vna_card, "connected", connected)
        self._repolish_prop(getattr(self, "vna_page", None), "connected", connected)
        self._repolish_prop(self.vna_status, "connected", connected)
        self.mon_vna.set_send_enabled(connected)
        self._refresh_device_summary()

    def _toggle_vna_connection(self):
        if self._vna.is_connected():
            self._vna.disconnect_analyzer()
            return
        if self._vna_connecting:
            self.statusBar().showMessage("正在连接网络分析仪，请稍候…")
            return

        ip_address = self.vna_ip.text().strip()
        if not ip_address:
            message = "请输入网络分析仪 IP 地址。"
            self.vna_status.setText(message)
            self.statusBar().showMessage(message)
            return

        port_text = self.vna_port.text().strip() or str(network_analyzer.DEFAULT_PORT)
        try:
            port = int(port_text)
        except ValueError:
            message = "端口号无效。"
            self.vna_status.setText(message)
            self.statusBar().showMessage(message)
            return

        # connect() 内部是阻塞 socket.connect + *IDN? 探活（超时默认 5s），
        # 在主线程同步执行会在 IP 不可达时冻结整个界面——移到工作线程，
        # 结果经 NetworkAnalyzerManager 的 Qt 信号（跨线程自动排队）回主线程。
        self._vna_connecting = True
        self.vna_ip.setEnabled(False)
        self.vna_port.setEnabled(False)
        self.vna_status.setText("正在连接 %s:%s …" % (ip_address, port))
        self.statusBar().showMessage("正在连接网络分析仪…")

        def worker():
            try:
                self._vna.connect(ip_address, port)
            except Exception:
                log.exception("网分连接线程异常")
            finally:
                self._vna_connect_done.done.emit()

        threading.Thread(target=worker, daemon=True).start()

    def _on_vna_connect_finished(self):
        self._vna_connecting = False
        self._update_vna_controls()
        if not self._vna.is_connected():
            self.statusBar().showMessage("网络分析仪连接未成功。", 5000)

    def _send_vna_text(self, text):
        """监视窗发送 SCPI：socket 收发是阻塞的，必须丢到工作线程，
        响应经 _vna_reply 信号回主线程再落日志。"""
        command = text.strip()
        if not command or not self._vna.is_connected():
            return
        self.mon_vna.note_tx(command)
        # 响应回来之前锁住发送，避免并发 recv 互相抢包。
        self.mon_vna.set_send_enabled(False)

        def worker():
            response = self._vna.send_command(command)
            self._vna_reply.done.emit(command, response)

        threading.Thread(target=worker, daemon=True).start()

    def _on_vna_reply(self, command, response):
        self.mon_vna.set_send_enabled(self._vna.is_connected())
        if response:
            self.mon_vna.append_rx(response + "\n")

    def _on_vna_status_changed(self, connected):
        if connected:
            info = self._vna.get_connection_info() or {}
            idn = (info.get("idn") or self._vna.last_idn or "").strip()
            if idn:
                message = "已连接: %s" % idn
            else:
                message = "已连接 %s:%s" % (
                    info.get("ip_address", self.vna_ip.text().strip()),
                    info.get("port", self.vna_port.text().strip()),
                )
            self._repolish_prop(self.vna_status, "error", False)
            self.mon_vna.append_info("[网分] %s\n" % message)
        else:
            message = "网络分析仪已断开连接。"
            self._repolish_prop(self.vna_status, "error", False)
        self.vna_status.setText(message)
        self.statusBar().showMessage(message)
        self._update_vna_controls()

    def _on_vna_data_received(self, data):
        text = (data or "").strip()
        if not text:
            return
        self.vna_status.setText(text)
        self.mon_vna.append_info("[网分] %s\n" % text)
        log.info("网分数据: %s", text)

    def _on_vna_error(self, message):
        self.vna_status.setText(message)
        self._repolish_prop(self.vna_status, "error", True)
        self.statusBar().showMessage(message)
        self.mon_vna.append_info("[网分错误] %s\n" % message)
        self._update_vna_controls()
        log.warning("网分错误: %s", message)

    # ============================================================
    # 微波消融仪主机（PC↔HW100 V1：AA/BB、固定长度、XOR 校验）
    # ============================================================

    def _refresh_mwa_ports(self, ports=None):
        if not hasattr(self, "mwa_port"):
            return
        if ports is None or isinstance(ports, bool):
            ports = self._mwa.available_ports()
        ports = list(ports)
        current = self.mwa_port.currentData()
        self.mwa_port.blockSignals(True)
        self.mwa_port.clear()
        for port in ports:
            self.mwa_port.addItem(port["label"], port["name"])
        if not ports:
            self.mwa_port.addItem("未发现串口", "")
        if current:
            index = self.mwa_port.findData(current)
            if index >= 0:
                self.mwa_port.setCurrentIndex(index)
        self.mwa_port.blockSignals(False)
        if not self._mwa.is_port_open():
            self._set_mwa_status(
                "微波消融仪未连接。" if ports else "未发现可用串口。")
        self._update_mwa_controls()

    def _mwa_state_signature(self):
        """_update_mwa_controls 依赖的全部设备布尔状态。"""
        m = self._mwa
        return (m.is_port_open(), m.is_online(), m.is_cooling_on(),
                m.is_microwave_on(), m.is_start_pending(),
                m.is_settlement_pending(), m.is_countdown_active())

    def _schedule_mwa_controls_refresh(self):
        """按需刷新 MWA 控制区。

        控制区（按钮联锁/连接态/图标）只取决于设备布尔状态，与遥测数值
        无关；而 _update_mwa_controls 是重操作（repolish、重绘图标、刷新
        设备摘要）。状态签名变化时立即刷新，否则限频 ~8Hz 作兜底，
        遥测数值本身仍以原始帧率更新上方标签。
        """
        signature = self._mwa_state_signature()
        if signature != self._last_mwa_state_signature:
            self._last_mwa_state_signature = signature
            self._mwa_controls_clock.restart()
            self._update_mwa_controls()
            return
        if self._mwa_controls_clock.elapsed() >= 125:
            self._mwa_controls_clock.restart()
            self._update_mwa_controls()

    def _update_mwa_controls(self, *_):
        port_open = self._mwa.is_port_open()
        online = self._mwa.is_online()
        cooling = self._mwa.is_cooling_on()
        microwave = self._mwa.is_microwave_on()
        start_pending = self._mwa.is_start_pending()
        settlement_pending = self._mwa.is_settlement_pending()
        countdown = self._mwa.is_countdown_active()

        # 连接入口固定在“设备连接”的微波消融仪行。
        if hasattr(self, "mwa_port"):
            has_port = bool(self.mwa_port.currentData())
            self.mwa_port.setEnabled(not port_open)
            self.mwa_baud.setEnabled(not port_open)
            self.btn_mwa_connect.setEnabled(port_open or has_port)
            self._sync_device_connect_button(self.btn_mwa_connect, port_open)
        if hasattr(self, "mon_mwa"):
            self.mon_mwa.set_send_enabled(port_open)

        self.mwa_link_label.setText("已连接" if online else "未连接")
        self.mwa_link_label.setProperty("online", online)
        self.mwa_link_label.style().unpolish(self.mwa_link_label)
        self.mwa_link_label.style().polish(self.mwa_link_label)
        linked = port_open or online
        self._sync_link_state(
            getattr(self, "mwa_link_state", None),
            linked,
            on_text=("●  在线" if online else "●  已连接"),
            off_text="●  未连接")
        if hasattr(self, "mwa_online_dot") and self.mwa_online_dot is not None:
            self._repolish_prop(self.mwa_online_dot, "connected", online)
        self._repolish_prop(
            getattr(self, "mwa_card", None), "connected", linked)
        self._repolish_prop(
            getattr(self, "mwa_page", None), "connected", linked)
        self._repolish_prop(
            getattr(self, "mwa_conn_status", None), "connected", port_open)
        chip = getattr(self, "mwa_status_chip", None)
        if chip is not None and chip.property("online") != online:
            chip.setProperty("online", online)
            chip.style().unpolish(chip)
            chip.style().polish(chip)
        self._refresh_device_summary()

        # 启动处理中允许“取消启动”（走 0x22 抢占），但不允许停泵；
        # 结算处理中不得再启动。泵未开时的启泵始终只会改善联锁条件。
        can_toggle_pump = online and (
            (not cooling) or ((not microwave) and (not start_pending)))
        self.btn_mwa_cooling.setEnabled(can_toggle_pump)
        self.btn_mwa_microwave.setEnabled(
            online and (microwave or start_pending or (cooling and not settlement_pending)))
        can_adjust = online and (not microwave) and (not start_pending) and (not countdown)
        for button in (
                self.btn_mwa_time_sub, self.btn_mwa_time_add,
                self.btn_mwa_power_sub, self.btn_mwa_power_add):
            button.setEnabled(can_adjust)

        self.btn_mwa_cooling.setText(
            "蠕动泵停止" if cooling else "蠕动泵启动")
        self.btn_mwa_cooling.setProperty("active", cooling)
        self.btn_mwa_cooling.setIcon(
            self._mwa_icon("cool", "#FFFFFF" if cooling else None))
        self.btn_mwa_microwave.setText(
            "取消启动" if start_pending
            else ("微波停止" if microwave else "微波启动"))
        self.btn_mwa_microwave.setProperty("active", microwave)
        self.btn_mwa_microwave.setIcon(
            self._mwa_icon("wave", "#FFFFFF" if microwave else None))
        for button in (self.btn_mwa_cooling, self.btn_mwa_microwave):
            button.style().unpolish(button)
            button.style().polish(button)
        self._update_serial_menu_indicator()

    def _toggle_mwa_connection(self):
        if not hasattr(self, "mwa_port"):
            return
        if self._mwa.is_port_open():
            self._mwa.disconnect_port()
            return
        port = self.mwa_port.currentData()
        if not port:
            self.statusBar().showMessage("请先选择微波消融仪串口。")
            return
        baud = int(self.mwa_baud.currentText())
        self._mwa.connect_port(port, baud)
        self._update_mwa_controls()

    def _send_mwa_hex(self, text):
        """Parse and send one complete PC -> HW100 hexadecimal frame."""
        cleaned = text.strip()
        for separator in (",", "，", ";", "；", "-"):
            cleaned = cleaned.replace(separator, " ")
        tokens = cleaned.split()
        if len(tokens) == 1:
            compact = tokens[0]
            if compact.lower().startswith("0x"):
                compact = compact[2:]
            if len(compact) % 2 == 0:
                tokens = [compact[i:i + 2] for i in range(0, len(compact), 2)]
        try:
            normalized = []
            for token in tokens:
                token = token[2:] if token.lower().startswith("0x") else token
                if len(token) != 2:
                    raise ValueError
                normalized.append(int(token, 16))
            frame = bytes(normalized)
        except (TypeError, ValueError):
            message = "请输入按字节分隔的十六进制帧，例如 AA 24 24 BB。"
            self._set_mwa_status(message)
            self.statusBar().showMessage(message)
            self.mon_mwa.append_info("[输入错误] %s\n" % message)
            return
        if self._mwa.send_debug_frame(frame):
            self.mon_mwa.send_edit.clear()
            self.statusBar().showMessage("HW100 协议帧已发送。")

    def _on_mwa_bytes_sent(self, data):
        if hasattr(self, "mon_mwa") and data:
            self.mon_mwa.append_tx(bytes(data).hex(" ").upper())

    def _on_mwa_bytes_received(self, data):
        if hasattr(self, "mon_mwa") and data:
            self.mon_mwa.append_rx(bytes(data).hex(" ").upper() + "\n")

    def _on_mwa_cooling(self):
        self._mwa.toggle_cooling()
        self._update_mwa_controls()

    def _on_mwa_microwave(self):
        self._mwa.toggle_microwave()
        self._update_mwa_controls()

    def _set_mwa_status(self, message):
        """同步消融仪状态文字：控制面板里的隐藏标签 + 设备连接卡片上的状态行。"""
        self.mwa_status.setText(message)
        card_status = getattr(self, "mwa_conn_status", None)
        if card_status is not None:
            card_status.setText(message)

    def _on_mwa_port_status(self, message, connected):
        self._set_mwa_status(message)
        self.statusBar().showMessage(message)
        self._update_mwa_controls()

    def _on_mwa_connection(self, online, message):
        self.mwa_link_label.setToolTip(message or "")
        self._set_mwa_status(message)
        self.statusBar().showMessage(message)
        if not online and self._imu_source == "hw100":
            self._imu_source = None
            self.viewer.clear_imu_sensor_readout()
        self._update_mwa_controls()

    def _on_mwa_status(self, message):
        if message:
            self._set_mwa_status(message)

    def _on_mwa_log(self, message):
        if message:
            log.info("消融仪: %s", message)

    def _on_mwa_error(self, message):
        self._set_mwa_status(message)
        self.statusBar().showMessage(message)
        if hasattr(self, "mon_mwa"):
            self.mon_mwa.append_info("[协议错误] %s\n" % message)
        self._update_mwa_controls()
        log.warning("消融仪错误: %s", message)

    @staticmethod
    def _format_lcd_temp(value):
        """温度读数固定保留 1 位小数，与实体主机一致。"""
        v = max(-9.9, min(99.9, float(value)))
        return "%.1f" % v

    @staticmethod
    def _format_elapsed_hms(seconds):
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return "%02d:%02d:%02d" % (hours, minutes, secs)

    @staticmethod
    def _set_mwa_alarm_state(label, alarm, lcd=None):
        label.setText("●  报警" if alarm else "●  正常")
        label.setProperty("alarm", bool(alarm))
        label.style().unpolish(label)
        label.style().polish(label)
        # 报警时对应通道的读数、辉光和屏底 LED 灯带同步转红，一眼可辨
        if lcd is not None and lcd.property("alarm") != bool(alarm):
            lcd.setProperty("alarm", bool(alarm))
            lcd.style().unpolish(lcd)
            lcd.style().polish(lcd)
            effect = lcd.graphicsEffect()
            if effect is not None:
                if alarm:
                    effect.setColor(QtGui.QColor(248, 113, 113, 170))
                else:
                    normal = lcd.property("glow_rgba")
                    if normal is not None:
                        effect.setColor(normal)
            holder = lcd.parentWidget()
            if holder is not None and holder.objectName() == "MwaValueHolder":
                holder.setProperty("alarm", bool(alarm))
                holder.style().unpolish(holder)
                holder.style().polish(holder)

    def _on_mwa_telemetry(self, data):
        if not isinstance(data, dict):
            return

        display_time = data.get("display_time_s")
        time_set = data.get("time_s")
        if display_time is not None:
            self.mwa_time_value.setText(
                microwave_ablator.format_work_time(int(display_time)))
        if time_set is not None:
            self.mwa_set_time.setText(
                microwave_ablator.format_work_time(int(time_set)))

        elapsed = data.get("elapsed_time_s")
        if elapsed is None and time_set is not None and display_time is not None:
            elapsed = max(0, int(time_set) - int(display_time))
        if elapsed is not None:
            self.mwa_elapsed_time.setText(self._format_elapsed_hms(elapsed))

        power = data.get("power_w")
        if power is not None:
            power_value = max(0, int(power))
            self.mwa_power_value.setText(str(power_value))
            self.mwa_set_power.setText("%d W" % power_value)
            # 同步到消融仿真功率，便于联机时区生长一致
            if abs(float(self.sim_power.value()) - float(power)) >= 0.5:
                self.sim_power.blockSignals(True)
                self.sim_power.setValue(float(power))
                self.sim_power.blockSignals(False)

        bypass = data.get("bypass_temp_c")
        if bypass is not None:
            self.mwa_bypass_temp.setText(self._format_lcd_temp(bypass))
        rod = data.get("rod_temp_c")
        if rod is not None:
            self.mwa_rod_temp.setText(self._format_lcd_temp(rod))
        elif data.get("rod_temp_available") is False:
            # 协议规定空闲和结算处理中杆温填 0，该 0 不是实测 0℃。
            self.mwa_rod_temp.setText("--")

        side = data.get("side_alarm_c")
        if side is not None:
            self.mwa_bypass_setpoint.setText("%.1f ℃" % float(side))
        rod_limit = data.get("rod_alarm_c", data.get("rod_limit_c"))
        if rod_limit is not None:
            self.mwa_rod_limit.setText("%.1f ℃" % float(rod_limit))

        status_flag = data.get("status_flag")
        if status_flag is not None:
            status_flag = int(status_flag)
            self._set_mwa_alarm_state(
                self.mwa_bypass_state, bool(status_flag & 0x01),
                self.mwa_bypass_temp)
            self._set_mwa_alarm_state(
                self.mwa_rod_state, bool(status_flag & 0x02),
                self.mwa_rod_temp)

        swr = data.get("swr", data.get("swr_value"))
        if swr is not None:
            self.mwa_swr.setText("%.1f" % float(swr))

        if data.get("realtime"):
            pitch = data.get("pitch_deg")
            roll = data.get("roll_deg")
            yaw = data.get("yaw_deg")
            if rod is not None and pitch is not None and roll is not None and yaw is not None:
                self._imu_source = "hw100"
                magnetic = None
                if data.get("magnetic_available"):
                    # Firmware transmits the three signed fields in 0.1 µT.
                    magnetic = tuple(
                        float(data.get(key, 0)) / 10.0
                        for key in ("mag_x", "mag_y", "mag_z")
                    )
                self.viewer.set_imu_sensor_readout(
                    rod, pitch, roll, yaw, magnetic=magnetic)

        status_text = data.get("status_text")
        if status_text and not data.get("countdown_tick"):
            self._set_mwa_status(status_text)
        self._schedule_mwa_controls_refresh()
    # ============================================================
    # 数据加载
    # _load() 是核心通用加载方法，四个菜单项最终都调用它
    # ============================================================

    def _open_import(self):
        """统一导入入口：自动识别 DICOM 文件夹、图片序列或 ZIP。"""
        path = self._choose_import_path(
            "导入影像数据（DICOM / 图片序列 / ZIP）", mixed=True)
        if not path:
            return
        try:
            kind = loader.detect_source_kind(path)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "无法导入", str(exc))
            return
        self._stage_dataset(kind, path)

    def _open_dicom(self):
        """打开 DICOM 文件夹的菜单响应。"""
        d = self._choose_import_path("选择 DICOM 序列文件夹", directory=True)
        if d:
            self._stage_dataset("dicom", d)

    def _open_stack(self):
        """打开图片序列文件夹的菜单响应。"""
        d = self._choose_import_path("选择图片序列文件夹", directory=True)
        if d:
            self._stage_dataset("images", d)

    def _open_zip(self):
        """导入 ZIP 压缩包的菜单响应。"""
        path = self._choose_import_path(
            "选择 ZIP 压缩包", name_filter="ZIP 压缩包 (*.zip)")
        if path:
            self._stage_dataset("zip", path)

    def _ensure_file_dialog_overlay(self):
        if self._file_dialog_overlay is None:
            self._file_dialog_overlay = EmbeddedFileDialogOverlay(self)
        return self._file_dialog_overlay

    def _schedule_file_dialog_bg_refresh(self):
        """3D 视角交互结束后延迟刷新文件对话框背景缓存。

        打开对话框时背景直接复用缓存（零现场抓屏，不会卡）；在用户
        停止转动视角后的空闲时段重抓，让缓存紧跟当前画面——下次打开
        时背景即与消融仪/串口弹窗一样实时。
        """
        self._file_dialog_bg_timer.start()

    def _refresh_file_dialog_background(self):
        overlay = self._file_dialog_overlay
        if overlay is None or overlay.isVisible():
            return
        try:
            overlay._capture_background(force=True)
        except Exception:
            log.exception("刷新文件对话框背景缓存失败")

    def _choose_import_path(self, title, directory=False, name_filter="",
                            mixed=False):
        """通过固定在软件内容区内的主题化浏览器选择导入路径。"""
        return self._ensure_file_dialog_overlay().choose(
            title,
            root_directory=IMPORT_ROOT_DIRECTORY,
            directory=directory,
            name_filter=name_filter,
            mixed=mixed,
        )

    def _open_demo(self):
        """加载演示体模的菜单响应。"""
        self._active_dataset_key = None
        self._refresh_dataset_items()
        self._load(loader.make_demo_phantom, "演示体模")

    def _dataset_key(self, kind, path):
        return "%s|%s" % (kind, os.path.normcase(os.path.realpath(path)))

    def _stage_dataset(self, kind, path):
        """把数据源加入左侧暂存列表，不触发体数据加载或视图更新。"""
        path = os.path.realpath(os.path.abspath(path))
        key = self._dataset_key(kind, path)
        if key in self._dataset_entries:
            item = self._dataset_entries[key]["item"]
            self.dataset_list.setCurrentItem(item)
            self.statusBar().showMessage("该影像数据已在左侧列表中。")
            return

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        self.statusBar().showMessage("正在读取影像基本信息…")
        QtWidgets.QApplication.processEvents()
        try:
            metadata = loader.inspect_source(kind, path)
        except Exception as exc:
            log.warning("读取影像基本信息失败:%s", exc)
            metadata = {}
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

        label = os.path.basename(path.rstrip(os.sep)) or path
        entry = {
            "key": key,
            "kind": kind,
            "path": path,
            "label": label,
            "metadata": metadata,
        }
        item = QtWidgets.QListWidgetItem()
        item.setData(QtCore.Qt.ItemDataRole.UserRole, key)
        item.setToolTip(path)
        item.setSizeHint(QtCore.QSize(0, 48))
        icon_type = (QtWidgets.QStyle.StandardPixmap.SP_FileIcon
                     if kind == "zip"
                     else QtWidgets.QStyle.StandardPixmap.SP_DirIcon)
        item.setIcon(self.style().standardIcon(icon_type))
        entry["item"] = item
        self._dataset_entries[key] = entry
        self.dataset_list.addItem(item)
        self._refresh_dataset_item(entry)
        self.dataset_list.setCurrentItem(item)
        self._update_dataset_count()
        self.statusBar().showMessage(
            "已导入 %s 到影像列表；点击“加载显示”后才会显示。" % label)

    def _dataset_kind_label(self, kind, metadata):
        if kind == "zip":
            return metadata.get("modality") or "ZIP"
        if kind == "images":
            return "图片序列"
        return metadata.get("modality") or "DICOM"

    def _refresh_dataset_item(self, entry):
        metadata = entry.get("metadata") or {}
        title = metadata.get("patient_name") or entry["label"]
        details = [self._dataset_kind_label(entry["kind"], metadata)]
        patient_id = metadata.get("patient_id")
        if patient_id:
            details.append("ID %s" % patient_id)
        dimensions = metadata.get("dimensions_text")
        if dimensions:
            details.append(dimensions)
        if (self._dataset_load_entry is not None
                and entry["key"] == self._dataset_load_entry["key"]):
            details.append("加载中")
        else:
            details.append(
                "显示中" if entry["key"] == self._active_dataset_key else "待加载")
        entry["item"].setText("%s\n%s" % (title, " · ".join(details)))

    def _refresh_dataset_items(self):
        for entry in self._dataset_entries.values():
            self._refresh_dataset_item(entry)
        self._on_dataset_selection_changed(self.dataset_list.currentItem(), None)

    def _update_dataset_count(self):
        self.dataset_count.setText("%d 项" % len(self._dataset_entries))

    def _selected_dataset_entry(self):
        item = self.dataset_list.currentItem()
        if item is None:
            return None
        return self._dataset_entries.get(
            item.data(QtCore.Qt.ItemDataRole.UserRole))

    def _on_dataset_selection_changed(self, current, _previous):
        entry = self._selected_dataset_entry() if current is not None else None
        enabled = entry is not None and self._dataset_load_thread is None
        self.btn_dataset_load.setEnabled(enabled)
        self.btn_dataset_remove.setEnabled(enabled)
        if entry is None:
            self.dataset_info.setText(
                "通过右上角「导入」导入；导入后不会立即显示。")
            self.dataset_info.setToolTip("")
            return
        metadata = entry.get("metadata") or {}
        parts = []
        if metadata.get("study_date"):
            parts.append("日期 %s" % metadata["study_date"])
        if metadata.get("series_description"):
            parts.append(metadata["series_description"])
        self.dataset_info.setText(
            " · ".join(parts) if parts else entry["label"])
        self.dataset_info.setToolTip(entry["path"])

    def _remove_selected_dataset(self):
        if self._dataset_load_thread is not None:
            return
        entry = self._selected_dataset_entry()
        if entry is None:
            return
        row = self.dataset_list.row(entry["item"])
        self.dataset_list.takeItem(row)
        self._dataset_entries.pop(entry["key"], None)
        if self._active_dataset_key == entry["key"]:
            self._active_dataset_key = None
        self._update_dataset_count()
        self._refresh_dataset_items()
        self.statusBar().showMessage(
            "已从影像列表移除 %s；磁盘文件未删除。" % entry["label"])

    def _load_selected_dataset(self):
        if self._dataset_load_thread is not None:
            return
        entry = self._selected_dataset_entry()
        if entry is None:
            return
        self._dataset_load_entry = entry
        self._dataset_load_thread = QtCore.QThread(self)
        self._dataset_load_worker = DatasetLoadWorker(
            entry["kind"], entry["path"])
        self._dataset_load_worker.moveToThread(self._dataset_load_thread)
        self._dataset_load_thread.started.connect(
            self._dataset_load_worker.run)
        self._dataset_load_worker.progressChanged.connect(
            self._on_dataset_load_progress)
        self._dataset_load_worker.finished.connect(
            self._on_dataset_load_finished)
        self._dataset_load_worker.failed.connect(
            self._on_dataset_load_failed)
        self._dataset_load_worker.finished.connect(
            self._dataset_load_thread.quit)
        self._dataset_load_worker.failed.connect(
            self._dataset_load_thread.quit)
        self._dataset_load_worker.finished.connect(
            self._dataset_load_worker.deleteLater)
        self._dataset_load_worker.failed.connect(
            self._dataset_load_worker.deleteLater)
        self._dataset_load_thread.finished.connect(
            self._on_dataset_load_thread_finished)
        self._dataset_load_thread.finished.connect(
            self._dataset_load_thread.deleteLater)
        self.btn_dataset_load.setText("加载中…")
        self.btn_dataset_load.setEnabled(False)
        self.btn_dataset_remove.setEnabled(False)
        self._refresh_dataset_items()
        self.statusBar().showMessage("正在加载 %s… 0%%" % entry["label"])
        log.info("开始后台加载:%s", entry["label"])
        self._dataset_load_thread.start()

    @QtCore.Slot(int, str)
    def _on_dataset_load_progress(self, percent, message):
        entry = self._dataset_load_entry
        if entry is not None:
            self.statusBar().showMessage(
                "%s %s… %d%%" % (message.rstrip("…"), entry["label"], percent))

    @QtCore.Slot(object, object)
    def _on_dataset_load_finished(self, image, info):
        entry = self._dataset_load_entry
        if entry is None or self._close_after_load:
            return
        try:
            self._apply_loaded_volume(image, info, entry["label"])
        except Exception as exc:
            log.exception("建立渲染管线失败:%s", entry["label"])
            QtWidgets.QMessageBox.critical(self, "无法显示数据", str(exc))
            self.statusBar().showMessage("显示失败。")
            return
        self._active_dataset_key = entry["key"]
        self._refresh_dataset_items()

    @QtCore.Slot(str)
    def _on_dataset_load_failed(self, message):
        entry = self._dataset_load_entry
        label = entry["label"] if entry is not None else "影像"
        log.error("加载失败:%s: %s", label, message)
        if not self._close_after_load:
            QtWidgets.QMessageBox.critical(self, "无法加载数据", message)
            self.statusBar().showMessage("加载失败。")

    @QtCore.Slot()
    def _on_dataset_load_thread_finished(self):
        self._dataset_load_thread = None
        self._dataset_load_worker = None
        self._dataset_load_entry = None
        self.btn_dataset_load.setText("加载显示")
        self._refresh_dataset_items()
        if self._close_after_load:
            self.close()

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
            return False

        try:
            self._apply_loaded_volume(image, info, label)
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            log.exception("建立渲染管线失败:%s", label)
            QtWidgets.QMessageBox.critical(self, "无法显示数据", str(exc))
            self.statusBar().showMessage("显示失败。")
            return False
        QtWidgets.QApplication.restoreOverrideCursor()
        return True

    def _apply_loaded_volume(self, image, info, label):
        """在 GUI 线程一次性接管解码结果，并更新界面状态。"""
        self._cancel_segmentation()
        self._populate_nodule_cards(None)   # 旧病例的结节卡片不再有效
        self._plan_report_exported = False
        self._plan_completed = 0            # 完成步数重置;随后的状态刷新会把
        # 向导自动翻到第②页(分割)——加载本身就是第①步的完成动作。
        self.viewer.set_volume(image, info)  # clears old data and schedules staged renders
        self._update_plan_status()  # set_volume cleared 入针点/消融点
        self._stop_simulation()  # reset sim UI to idle for the new volume
        if self.btn_needle_show.isChecked():
            self._on_needle_params_changed()
            self.viewer.reset_ablation_needle()
        self.z_spin.blockSignals(True)
        self.z_spin.setValue(1.0)
        self.z_spin.blockSignals(False)
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
        # 弹层背景预抓改由 viewer.initialRendersFinished 驱动
        # （首帧渲染完成、画面稳定后再抓，避免抓到未完成的黑帧）。
        QtCore.QTimer.singleShot(
            5000, self._prewarm_frosted_backgrounds)  # 兜底：信号未触发时补抓

    def _on_initial_renders_finished(self):
        """体渲染首帧全部完成后，等画面呈现稳定再预抓弹层毛玻璃背景。"""
        QtCore.QTimer.singleShot(250, self._prewarm_frosted_backgrounds)

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
        # 拖动合批中可能还挂着一个旧值，丢弃并以本次按钮选择为准，
        # 否则 40ms 后定时器会把过期值再覆盖回去。
        self._pending_opacity_scale = None
        self._opacity_flush_timer.stop()
        self.viewer.set_opacity_scale(scale)

    def _on_slider(self, value):
        """不透明度滑块拖动回调：将滑块值转为 0~1 系数，
        并自动更新不透明/透明按钮的选中状态（≥75% 为不透明）。

        重渲染按 40ms 合批（见滑块创建处说明），这里只记录待落值。
        """
        scale = value / 100.0
        self.btn_opaque.setChecked(scale >= 0.75)
        self.btn_transparent.setChecked(scale < 0.75)
        self._pending_opacity_scale = scale
        self._opacity_flush_timer.start()

    def _flush_opacity_scale(self):
        """把合批中的最新不透明度真正应用到 3D 视图（定时器到期/释放滑块）。"""
        if self._pending_opacity_scale is None:
            return
        scale, self._pending_opacity_scale = self._pending_opacity_scale, None
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
        self._update_plan_status()

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
        point_analysis = self.viewer.evaluate_planning_point(ijk)
        if point_analysis.get("available") and point_analysis.get("collision"):
            self._show_planning_blocked(
                "当前位置与骨骼重叠或安全余量不足（骨面距离约 %.1f mm），不可选择。"
                % point_analysis["bone_clearance_mm"])
            return

        candidate = self.viewer.planning_points()
        candidate[kind] = tuple(ijk)
        entry, tip = candidate["entry"], candidate["tip"]
        path_analysis = None
        if entry is not None and tip is not None:
            if self._same_voxel(entry, tip):
                self._show_planning_blocked(
                    "入针点与消融点重合，不可形成有效针道。请移动坐标轴后重新选择。")
                return
            path_analysis = self.viewer.evaluate_needle_path(entry, tip)
            if path_analysis.get("available"):
                if path_analysis.get("too_long"):
                    self._show_planning_blocked(
                        "当前路径长 %.1f mm，超过所选消融针 %.1f mm 的针杆长度，不可选择。"
                        % (path_analysis["path_length_mm"], self.needle_shaft.value()))
                    return
                if path_analysis.get("collision"):
                    self._show_planning_blocked(
                        "当前路径经过骨骼，不可选择。最近碰撞位置距入针点约 %.1f mm，"
                        "请调整入针点或消融点。"
                        % path_analysis["collision_distance_from_entry_mm"])
                    return
        connected = self.viewer.set_planning_point(kind, ijk)
        self._update_plan_status()
        near_analysis = path_analysis if path_analysis is not None else point_analysis
        if (near_analysis.get("available")
                and near_analysis.get("near_bone")):
            self.statusBar().showMessage(
                "%s已放置，但当前位置或路径距离骨骼较近（最小约 %.1f mm），请谨慎复核。"
                % (label, near_analysis["bone_clearance_mm"]), 8000)
            return
        if not point_analysis.get("available"):
            self.statusBar().showMessage(
                "%s已放置；当前影像无法自动进行 HU 骨骼判定，请人工复核针道。" % label,
                8000)
            return
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

    def _show_planning_blocked(self, message):
        """在状态栏和统一毛玻璃弹层中提示不可用针道。"""
        self.statusBar().showMessage(message, 10000)
        self._show_planning_alert("针道不可选", message)

    def _ensure_planning_alert_overlay(self):
        if self._planning_alert_overlay is None:
            overlay = PlanningAlertOverlay(self)
            overlay.accepted.connect(self._accept_planning_alert)
            overlay.cancelled.connect(self._cancel_planning_alert)
            overlay.dismissed.connect(self._on_planning_alert_dismissed)
            self._planning_alert_overlay = overlay
        return self._planning_alert_overlay

    def _show_planning_alert(self, title, message, *, confirm_text="知道了",
                             cancel_text=None, on_confirm=None):
        """显示与退出框一致的针道提醒，可选确认后的继续动作。"""
        overlay = self._ensure_planning_alert_overlay()
        self._planning_alert_on_confirm = on_confirm
        self._planning_alert_pending_confirm = None
        overlay.configure(title, message, confirm_text, cancel_text)
        overlay.present()

    def _accept_planning_alert(self):
        self._planning_alert_pending_confirm = self._planning_alert_on_confirm
        self._planning_alert_on_confirm = None
        if self._planning_alert_overlay is not None:
            self._planning_alert_overlay.dismiss()

    def _cancel_planning_alert(self):
        self._planning_alert_on_confirm = None
        self._planning_alert_pending_confirm = None
        if self._planning_alert_overlay is not None:
            self._planning_alert_overlay.dismiss()

    def _on_planning_alert_dismissed(self):
        callback = self._planning_alert_pending_confirm
        self._planning_alert_pending_confirm = None
        if callback is not None:
            QtCore.QTimer.singleShot(0, callback)

    def _recommend_entry_point(self):
        """Recommend a reachable skin entry for the manually placed tip.

        搜索在后台线程执行（扇形采样 + 骨距分析随 CT 规模线性增长，
        主线程只做输入校验与结果落点），期间按钮置灰防重复触发。
        """
        if not hasattr(self, "viewer") or self.viewer.image is None:
            self.statusBar().showMessage("请先加载 CT，再计算推荐入针点。")
            return
        tip = self.viewer.planning_points().get("tip")
        if tip is None:
            self.statusBar().showMessage(
                "请先把坐标轴移到病灶并放置消融点，再计算推荐入针点。", 8000)
            return
        availability = self.viewer.evaluate_planning_point(tip)
        if not availability.get("available"):
            self._show_planning_blocked(
                "当前影像无法使用 HU 进行骨骼检测，不能自动推荐避骨入针点。")
            return
        args = self.viewer.planning_entry_search_args(tip)
        if args is None:
            self._show_planning_blocked(
                "当前影像无法使用 HU 进行骨骼检测，不能自动推荐避骨入针点。")
            return
        if self._entry_search_running:
            self.statusBar().showMessage("正在搜索避骨入针路径，请稍候…")
            return

        # 数据集标记用 vtkImageData 对象本身：numpy 视图每次调用都会
        # 新建对象，id() 不稳定，不能用来判断"搜索期间是否换了病例"。
        image_token = self.viewer.image
        self._entry_search_running = True
        self.btn_plan_auto.setEnabled(False)
        self.statusBar().showMessage("正在搜索避骨入针路径……")

        def worker():
            try:
                result = needle_planning.recommend_entry_point(
                    args["volume"], args["spacing"], args["target_ijk"],
                    max_length_mm=args["max_length_mm"],
                    needle_radius_mm=args["needle_radius_mm"])
            except Exception:
                log.exception("推荐入针点搜索失败")
                result = None
            finally:
                self._entry_search_done.done.emit(
                    None if result is None else (result, image_token))

        threading.Thread(target=worker, daemon=True).start()

    def _on_entry_search_finished(self, payload):
        self._entry_search_running = False
        self.btn_plan_auto.setEnabled(True)
        if not hasattr(self, "viewer") or self.viewer.image is None:
            return                      # 搜索期间数据已被清空/关闭
        if payload is None:
            self._show_planning_blocked(
                "在当前针杆长度内未找到可达且避开骨骼的入针路径。"
                "请调整消融点、针型或手动规划后复核。")
            return
        result, image_token = payload
        if self.viewer.image is not image_token:
            # 搜索期间切换了病例：结果的体素坐标属于旧体数据，直接丢弃。
            self.statusBar().showMessage(
                "搜索期间影像已切换，推荐结果已丢弃；请重新计算。", 8000)
            return

        tip = self.viewer.planning_points().get("tip")
        entry = result["entry_ijk"]
        connected = self.viewer.set_planning_point("entry", entry)
        self._update_plan_status()
        analysis = result["analysis"]
        message = (
            "已推荐避骨入针点：路径 %.1f mm，最小骨骼间距%s；"
            "共比较 %d 条可行路径。请结合三向切片人工复核。"
            % (analysis["path_length_mm"],
               self._bone_clearance_text(analysis),
               result["valid_candidate_count"]))
        self.statusBar().showMessage(message, 12000)
        if not connected:
            log.warning("推荐入针点未与现有消融点连接：entry=%s tip=%s", entry, tip)

    @staticmethod
    def _bone_clearance_text(analysis):
        distance = float(analysis.get("bone_clearance_mm", 0.0))
        if distance > needle_planning.BONE_WARNING_DISTANCE_MM:
            return "≥%.1f mm" % needle_planning.BONE_WARNING_DISTANCE_MM
        return "约 %.1f mm" % distance

    @staticmethod
    def _same_voxel(a, b):
        """两个 ijk 是否落在同一体素（用于提醒入针点/消融点重合）。"""
        return all(round(a[i]) == round(b[i]) for i in range(3))

    # ---- 结节定位卡片与规划核对单 ---------------------------------------

    def _populate_nodule_cards(self, cards):
        """填充/清空结节定位卡片列表（肺结节分割完成后调用）。

        每个连通域一张卡：体积 + 沿腹侧到体表的深度（与推荐入针点同一套
        HU 体壁检测，单条射线，毫秒级）。cards 为 None/空 时隐藏整个区块。
        """
        self._nodule_cards = list(cards or [])
        self._nodule_selected = None
        self.nodule_list.clear()
        has_cards = bool(self._nodule_cards)
        self.nodule_section.setVisible(has_cards)
        self.nodule_list.setVisible(has_cards)
        empty_hint = getattr(self, "nodule_empty_hint", None)
        if empty_hint is not None:
            empty_hint.setVisible(not has_cards)
        for index, card in enumerate(self._nodule_cards, start=1):
            depth = self.viewer.nodule_depth_mm(card["ijk"])
            card["depth_mm"] = depth
            depth_text = "深度待复核" if depth is None else "距体表 %.0f mm" % depth
            card["label"] = "结节 %d · %.2f ml · %s" % (
                index, card["volume_ml"], depth_text)
            QtWidgets.QListWidgetItem(card["label"], self.nodule_list)
        self._refresh_nodule_card_labels()
        self._update_plan_steps()

    def _refresh_nodule_card_labels(self):
        """按当前已定位行刷新卡片文案（定位过的卡片追加 ✓ 标记）。"""
        located = getattr(self, "_nodule_selected", None)
        for row in range(self.nodule_list.count()):
            card = self._nodule_cards[row] if row < len(self._nodule_cards) else None
            if card is None:
                continue
            text = card.get("label", "")
            if row == located:
                text += "  ✓"
            self.nodule_list.item(row).setText(text)

    def _on_nodule_card_clicked(self, item):
        self._locate_nodule(self.nodule_list.row(item))

    def _on_nodule_card_activated(self, item):
        """双击/回车结节卡片：定位后直接放大经过该结节的轴状位切片。"""
        row = self.nodule_list.row(item)
        # 随后要展开切片浮层，跳过 3D 全屏取景，避免两次全屏过渡叠加。
        self._locate_nodule(row, frame=False)
        viewer = getattr(self, "viewer", None)
        if viewer is None or viewer.image is None:
            return
        axial = next((v for v in viewer.slice_panel.views
                      if getattr(v, "orientation", None) == "axial"), None)
        if axial is not None:
            viewer._show_expanded_slice(axial)

    def _locate_nodule(self, row, frame=True):
        """定位取景：十字移到该结节、骨骼调淡、相机转到腹侧、进入全屏。"""
        if not hasattr(self, "viewer") or self.viewer.image is None:
            return
        cards = self._nodule_cards
        if not cards or not 0 <= int(row) < len(cards):
            return
        row = int(row)
        card = cards[row]
        self._nodule_selected = row
        self.nodule_list.setCurrentRow(row)
        self._refresh_nodule_card_labels()
        viewer = self.viewer
        viewer.set_crosshair_ijk(card["ijk"])
        # 结节观察取景：两层骨骼调淡让深部结节透出来，肺实质保持解剖衬托；
        # 组织层的显示/透明度随时可在 3D 右键菜单手动恢复。
        for bone_name in ("皮质骨", "松质骨 / 钙化"):
            viewer.set_tissue_opacity(bone_name, 0.35)
        viewer.focus_camera_on_ijk(card["ijk"])
        viewer._flush_crosshair_update()
        if frame:
            viewer.enter_view3d_fullscreen()
        self._update_plan_steps()
        depth = card.get("depth_mm")
        self.statusBar().showMessage(
            "已定位到结节 %d/%d（约 %.2f ml%s）。双击 3D 视图可退出全屏，"
            "请人工复核。" % (
                row + 1, len(cards), card["volume_ml"],
                "" if depth is None else "，距体表约 %.0f mm" % depth), 10000)

    def _update_plan_steps(self):
        """按 加载/分割/定位/针道/导出 五步进度刷新指示条与向导页。

        分割是可选步骤：用户直接手动放置针道而跳过分割时，该步按
        "已被跨过"处理（后面走到了，前面就算过），不应永远卡在分割步。
        """
        if not hasattr(self, "plan_steps"):
            return
        viewer = getattr(self, "viewer", None)
        pts = viewer.planning_points() if viewer is not None else {}
        conditions = (
            viewer is not None and getattr(viewer, "image", None) is not None,
            viewer is not None
            and callable(getattr(viewer, "segmentation_names", None))
            and bool(viewer.segmentation_names()),
            getattr(self, "_nodule_selected", None) is not None
            or pts.get("tip") is not None,
            pts.get("entry") is not None and pts.get("tip") is not None,
            getattr(self, "_plan_report_exported", False),
        )
        done = [False] * len(conditions)
        for index in range(len(conditions) - 1, -1, -1):
            done[index] = conditions[index] or any(done[index + 1:])
        states = []
        for index, is_done in enumerate(done):
            if is_done:
                states.append("done")
            elif "active" not in states:
                states.append("active")
            else:
                states.append("pending")
        states = tuple(states)
        self.plan_steps.set_states(states)
        # 向导自动翻页：完成的步骤数前进时，翻到第一个未完成的步骤。
        # 手动翻页不受影响——徽章和前后按钮随时可回看任意页。
        completed = 0
        for state in states:
            if state == "done":
                completed += 1
            else:
                break
        if hasattr(self, "_plan_pages") \
                and completed > getattr(self, "_plan_completed", 0):
            self._show_plan_page(min(completed, self._plan_pages.count() - 1))
        self._plan_completed = completed
        # 当前步骤对应的行动控件金色描边——打开界面即可看出"下一步点哪"。
        has_cards = bool(getattr(self, "_nodule_cards", None))
        self._set_step_emphasis(
            getattr(self, "btn_dataset_load", None), states[0] == "active")
        self._set_step_emphasis(
            getattr(self, "btn_seg_run", None),
            states[1] == "active" or (states[2] == "active" and not has_cards))
        self._set_step_emphasis(
            getattr(self, "nodule_list", None),
            states[2] == "active" and has_cards)
        self._set_step_emphasis(
            getattr(self, "btn_plan_auto", None), states[3] == "active")
        self._set_step_emphasis(
            getattr(self, "btn_plan_report", None), states[4] == "active")

    def _show_plan_page(self, index):
        """切换主流程向导到指定页（0/1/2…），并刷新翻页按钮可用性。"""
        if not hasattr(self, "_plan_pages"):
            return
        index = max(0, min(int(index), self._plan_pages.count() - 1))
        self._plan_pages.setCurrentIndex(index)
        self._update_plan_nav()

    def _update_plan_nav(self):
        index = self._plan_pages.currentIndex()
        count = self._plan_pages.count()
        self.btn_plan_prev.setEnabled(index > 0)
        self.btn_plan_next.setEnabled(index < count - 1)
        self.plan_page_label.setText("第 %d / %d 步" % (index + 1, count))

    def _page_header(self, number, title, subtitle):
        """向导页的大号页头：步骤序号 + 标题 + 一句副标题。"""
        head = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(head)
        row.setContentsMargins(0, 2, 0, 4)
        row.setSpacing(10)
        num = QtWidgets.QLabel(str(number), objectName="PageNumber")
        num.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        num.setFixedSize(30, 30)
        column = QtWidgets.QVBoxLayout()
        column.setSpacing(0)
        title_label = QtWidgets.QLabel(title, objectName="PageTitle")
        subtitle_label = QtWidgets.QLabel(subtitle, objectName="PageSubtitle")
        subtitle_label.setWordWrap(True)
        column.addWidget(title_label)
        column.addWidget(subtitle_label)
        row.addWidget(num, 0, QtCore.Qt.AlignmentFlag.AlignTop)
        row.addLayout(column, 1)
        return head

    @staticmethod
    def _set_step_emphasis(widget, on):
        """当前步骤行动控件的强调描边（QSS 由 current_step 属性驱动）。"""
        if widget is None:
            return
        widget.setProperty("current_step", bool(on))
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _collect_planning_report_data(self):
        """汇总核对单所需的全部数据（患者/定位/针道/针型/截图）。"""
        viewer = self.viewer
        info = viewer.info or {}
        try:
            images = viewer.capture_report_pngs()
        except Exception:
            log.exception("抓取核对单截图失败")
            images = {}
        world = viewer.planning_world_points()
        needle = viewer.ablation_params()
        needle["preset"] = self.needle_preset.currentText()
        cards = self._nodule_cards
        nodule = None
        if cards:
            index = 0 if self._nodule_selected is None else self._nodule_selected
            card = cards[min(index, len(cards) - 1)]
            nodule = {
                "index": index + 1,
                "total": len(cards),
                "volume_ml": card.get("volume_ml"),
                "depth_mm": card.get("depth_mm"),
            }
        planning = None
        if world.get("entry") is not None or world.get("tip") is not None:
            planning = {
                "entry_world": world.get("entry"),
                "tip_world": world.get("tip"),
                "length_mm": viewer.needle_path_length_mm(),
                "angles": viewer.needle_axis_angles(),
            }
        return {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "patient": {
                "name": info.get("patient_name", ""),
                "id": info.get("patient_id", ""),
                "study_date": info.get("study_date", ""),
                "series": info.get("series_description", ""),
            },
            "nodule": nodule,
            "planning": planning,
            "needle": needle,
            "zone": viewer.ablation_zone_info(),
            "images": images or {},
        }

    def _export_planning_report(self):
        """导出当前定位/规划状态的 HTML 核对单（浏览器打开即可打印）。"""
        if not hasattr(self, "viewer") or self.viewer.image is None:
            self.statusBar().showMessage("请先加载 CT，再导出规划核对单。")
            return
        try:
            html_text = planning_report.build_html(
                self._collect_planning_report_data())
        except Exception:
            log.exception("生成规划核对单失败")
            QtWidgets.QMessageBox.critical(
                self, "导出失败", "生成核对单内容时出错，详见日志。")
            return
        default_name = "CTto3D_规划核对单_%s.html" % (
            datetime.now().strftime("%Y%m%d_%H%M%S"),)
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出规划核对单", default_name, "HTML 核对单 (*.html)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(html_text)
        except OSError as exc:
            QtWidgets.QMessageBox.critical(self, "导出失败", str(exc))
            return
        self.statusBar().showMessage("规划核对单已保存:%s" % path, 10000)
        log.info("规划核对单已导出:%s", path)
        self._plan_report_exported = True
        self._update_plan_steps()
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def _clear_planning_points(self):
        """清除入针点/消融点及其连成的针道。"""
        if not hasattr(self, "viewer"):
            return
        self.viewer.clear_planning_points()
        self.viewer.clear_ablation_needle()
        self._plan_report_exported = False   # 旧核对单对应的针道已不存在
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

        text = "入针点:%s   消融点:%s" % (fmt(pts["entry"]), fmt(pts["tip"]))
        entry, tip = pts["entry"], pts["tip"]
        if entry is not None and tip is not None:
            analysis = self.viewer.evaluate_needle_path(entry, tip)
            if analysis.get("available"):
                if analysis.get("too_long"):
                    text += " · ⚠ 路径超过针长"
                elif analysis.get("collision"):
                    text += " · ⚠ 路径穿过骨骼，不可用"
                elif analysis.get("near_bone"):
                    text += " · ⚠ 近骨 %s" % self._bone_clearance_text(analysis)
                else:
                    text += " · 路径 %.1f mm · 骨距%s" % (
                        analysis["path_length_mm"],
                        self._bone_clearance_text(analysis))
            # 进针方向角与 3D 视图左上角叠加文字同源（与世界 X/Y/Z 轴夹角），
            # 就近显示避免用户为读角度转动视角。
            angles = self.viewer.needle_axis_angles()
            if angles is not None:
                text += " · 夹角 %.0f°/%.0f°/%.0f°" % angles
        elif entry is not None or tip is not None:
            analysis = self.viewer.evaluate_planning_point(
                entry if entry is not None else tip)
            if analysis.get("available") and analysis.get("near_bone"):
                text += " · ⚠ 当前位置近骨 %s" % self._bone_clearance_text(analysis)
        self.plan_status.setText(text)
        self._update_plan_steps()

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

    def _start_simulation(self, _checked=False, *, skip_near_bone_warning=False):
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

        path_analysis = self.viewer.current_needle_bone_analysis()
        if path_analysis and path_analysis.get("available"):
            if path_analysis.get("too_long"):
                self._show_planning_blocked(
                    "当前针道长 %.1f mm，超过 %.1f mm 的针杆长度，不能开始仿真。"
                    % (path_analysis["path_length_mm"], self.needle_shaft.value()))
                return
            if path_analysis.get("collision"):
                self._show_planning_blocked(
                    "当前路径经过骨骼，不可用，不能开始仿真。请重新规划入针点。")
                return
            if path_analysis.get("near_bone") and not skip_near_bone_warning:
                self._show_planning_alert(
                    "针道近骨提醒",
                    "当前路径距离骨骼较近（最小%s）。请确认已在三向切片中复核。"
                    % self._bone_clearance_text(path_analysis),
                    confirm_text="继续仿真",
                    cancel_text="返回检查",
                    on_confirm=lambda: self._start_simulation(
                        skip_near_bone_warning=True))
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

    def _on_seg_preset_changed(self, *_):
        preset = segmentation.preset_by_name(self.seg_preset.currentText())
        if segmentation.is_monai_nodule_preset(preset):
            ready = segmentation.monai_nodule_models_ready()
            self.seg_status.setText(
                "MONAI 两阶段肺结节分割：先定位肺区，再生成体素级结节 mask。"
                + ("模型已就绪。" if ready else "需要先下载模型。"))
            self.seg_result.setText(
                "研究用途模型，不作为临床诊断结论。<br>模型目录：<br>%s"
                % segmentation.weights_cache_dir_hint())
        else:
            self.seg_status.setText(
                "调用 TotalSegmentator 生成器官 mask，并叠加到 3D 视图。")
            self.seg_result.setText("")
        self._update_segmentation_controls()

    def _current_segmentation_preset(self):
        preset = segmentation.preset_by_name(self.seg_preset.currentText())
        if not segmentation.is_monai_nodule_preset(preset) \
                and not self.seg_fast.isChecked():
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
        preset = self._current_segmentation_preset()
        is_monai = segmentation.is_monai_nodule_preset(preset)
        if not is_monai and segmentation.find_totalsegmentator() is None \
                and segmentation.find_totalseg_download_weights() is None:
            QtWidgets.QMessageBox.information(
                self, "需要安装 TotalSegmentator",
                segmentation.TOTAL_SEGMENTATOR_INSTALL_HINT)
            return

        tasks = (["monai_nodule"] if is_monai
                 else segmentation.download_tasks_for_preset(preset))
        label = segmentation.download_tasks_display_name(tasks)
        self.seg_status.setText("准备下载/检查 %s..." % label)
        self.seg_result.setText("模型缓存目录:<br>%s" % segmentation.weights_cache_dir_hint())
        self.statusBar().showMessage("正在准备模型权重……")

        self._set_seg_download_running(True)
        self._seg_download_thread = QtCore.QThread(self)
        self._seg_download_worker = (
            segmentation.MonaiNoduleDownloadWorker()
            if is_monai
            else segmentation.TotalSegmentatorDownloadWorker(preset))
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
        log.info("分割模型权重下载: %s", text)

    def _on_seg_download_finished(self, task):
        label = segmentation.download_task_display_name(task)
        self.seg_status.setText("%s 已下载/已确认,可以直接运行自动分割。" % label)
        self.seg_result.setText("模型缓存目录:<br>%s" % segmentation.weights_cache_dir_hint())
        self.statusBar().showMessage("分割模型权重已准备好。")

    def _on_seg_download_failed(self, message):
        self.seg_status.setText("模型权重下载失败。")
        QtWidgets.QMessageBox.warning(self, "分割模型下载失败", message)
        self.statusBar().showMessage("分割模型权重下载失败。")

    def _on_seg_download_thread_finished(self):
        self._set_seg_download_running(False)
        self._seg_download_thread = None
        self._seg_download_worker = None

    def _run_totalsegmentator(self):
        if not hasattr(self, "viewer") or self.viewer.image is None:
            self.statusBar().showMessage("请先加载 CT 数据，再运行自动分割。")
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
            self.statusBar().showMessage("自动分割正在运行，请等待完成。")
            return
        preset = self._current_segmentation_preset()
        is_monai = segmentation.is_monai_nodule_preset(preset)
        if is_monai and not segmentation.monai_nodule_models_ready():
            QtWidgets.QMessageBox.information(
                self, "需要下载 MONAI 肺结节模型",
                "请先点击“下载模型”，准备 3D 肺结节模型和配套肺区 ROI 模型。")
            return
        if not is_monai and segmentation.find_totalsegmentator() is None:
            QtWidgets.QMessageBox.information(
                self, "需要安装 TotalSegmentator",
                segmentation.TOTAL_SEGMENTATOR_INSTALL_HINT)
            return

        self._seg_cancelled = False
        self._cleanup_seg_temp_dir()
        if is_monai:
            # 肺结节观察模式：开始分割前就把组织层切到"只显肺+骨"，
            # 让用户立刻看到变化；分割完成后会再确保一次。
            self.viewer.show_only_tissues(presets.LUNG_BONE_TISSUES)
        self._seg_temp_dir = tempfile.mkdtemp(
            prefix="ctto3d_monai_nodule_" if is_monai else "ctto3d_totalseg_")
        input_path = os.path.join(self._seg_temp_dir, "ct_input.nii")
        output_dir = os.path.join(self._seg_temp_dir, "segmentations")
        os.makedirs(output_dir, exist_ok=True)

        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        self.seg_status.setText("正在导出临时 NIfTI...")
        self.statusBar().showMessage("正在准备分割模型输入数据……")
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
        self._seg_engine = "monai_nodule" if is_monai else "totalsegmentator"
        self._seg_thread = QtCore.QThread(self)
        worker_type = (segmentation.MonaiNoduleWorker
                       if is_monai else segmentation.TotalSegmentatorWorker)
        self._seg_worker = worker_type(input_path, output_dir, preset)
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
        self.statusBar().showMessage(
            "MONAI 肺结节分割已启动。" if is_monai
            else "TotalSegmentator 已启动,首次运行可能需要下载模型。")

    def _on_segmentation_progress(self, text):
        text = segmentation.clean_progress_text(text)
        if not text:
            return
        short = text if len(text) <= 120 else text[:117] + "..."
        self.seg_status.setText(short)
        self.statusBar().showMessage(short)
        log.info("自动分割: %s", text)

    def _on_segmentation_finished(self, output_dir, expected_names):
        if self._seg_cancelled:
            self._cleanup_seg_temp_dir()
            return
        self.seg_status.setText("分割完成,正在载入 mask...")
        self.statusBar().showMessage("正在载入自动分割输出结果……")
        QtWidgets.QApplication.processEvents()
        try:
            loaded, skipped, loaded_items = self._load_segmentation_masks(output_dir, expected_names)
        except Exception as exc:
            log.exception("载入自动分割输出失败")
            QtWidgets.QMessageBox.critical(self, "载入分割失败", str(exc))
            self.seg_status.setText("分割完成,但结果载入失败。")
            return
        finally:
            self._cleanup_seg_temp_dir()

        loaded_names = [item["name"] for item in loaded_items]
        nodule_view = (self._seg_engine == "monai_nodule"
                       and "lung_nodule" in loaded_names)
        if not nodule_view:
            # 换成其他分割任务后，上一轮的结节定位卡片不再有效。
            self._populate_nodule_cards(None)
        focus = None
        if loaded_names:
            self._highlight_segmentations()
            if nodule_view:
                # 肺结节观察模式：除肺和骨外的组织层自动隐藏，
                # 避免脂肪/肌肉/血管等体渲染遮挡结节。
                # 3D 视图右键「组织」菜单可随时手动恢复任意层。
                changed = self.viewer.show_only_tissues(presets.LUNG_BONE_TISSUES)
                hidden = [n for n in self.viewer._tissue_order
                          if not self.viewer._tissue_visible.get(n, True)]
                log.info("结节观察模式：自动隐藏组织层 changed=%s hidden=%s",
                         changed, hidden)
            if "lung_nodule" in loaded_names:
                # 分割完成后把参考坐标轴定位到最大结节的质心，
                # 三向切片随之滚到该层面——用户无需自己找结节。
                focus = next(
                    (item.get("focus") for item in loaded_items
                     if item.get("name") == "lung_nodule"
                     and item.get("focus") is not None), None)
                if focus is not None:
                    self.viewer.set_crosshair_ijk(focus["ijk"])
                    log.info(
                        "已定位到最大结节: 质心=%s %.2fml, 连通域 %d 个",
                        tuple(round(v) for v in focus["ijk"]),
                        focus["volume_ml"], focus["components"])
                # 结节定位卡片：列出全部结节区域供逐个点击定位（含最大者）。
                self._populate_nodule_cards(next(
                    (item.get("components") for item in loaded_items
                     if item.get("name") == "lung_nodule"), None))
                focus_text = (
                    ("<br>已自动定位到最大结节（约 %.2f ml，共 %d 个结节区域），"
                     "左侧「结节定位」列表可逐个查看。"
                     % (focus["volume_ml"], focus["components"]))
                    if focus is not None else "")
                # 正文只留一行结论；完整清单挪到悬停提示，避免把左栏撑长。
                self.seg_result.setText(
                    "肺结节分割已叠加（研究用途），已隐藏肺/骨以外组织层。"
                    + focus_text)
                self.seg_result.setToolTip(
                    self._format_segmentation_result(loaded_items))
            else:
                self.seg_result.setText(
                    "已加载 %d 个分割部位，悬停可查看清单；"
                    "3D 视图右键可控制显示与透明度。" % loaded)
                self.seg_result.setToolTip(
                    self._format_segmentation_result(loaded_items))
            log.info("已载入自动分割: %s", ", ".join(
                "%s(%.1fml/%dvox)" % (
                    item["name"], item["volume_ml"], item["voxels"])
                for item in loaded_items))
        else:
            if self._seg_engine == "monai_nodule":
                self.seg_result.setText(
                    "本次模型未生成可显示的肺结节区域。<br>"
                    "这不等同于临床排除结节，仍需由专业人员复核原始 CT。")
            else:
                self.seg_result.setText("")
        self.seg_status.setText("已载入 %d 个分割结果%s。" % (
            loaded, "" if skipped == 0 else "，跳过 %d 个空/不匹配 mask" % skipped))
        if nodule_view:
            if focus is not None:
                self.statusBar().showMessage(
                    "结节分割已叠加；参考坐标轴已定位到最大结节（约 %.2f ml，"
                    "共 %d 个区域），请人工复核。" % (
                        focus["volume_ml"], focus["components"]))
            else:
                self.statusBar().showMessage(
                    "结节分割已叠加；已自动隐藏除肺/骨以外的组织层，"
                    "右键「组织」可恢复。")
        else:
            self.statusBar().showMessage("自动分割结果已叠加到 3D 视图。")

    def _on_segmentation_failed(self, message):
        self._cleanup_seg_temp_dir()
        if self._seg_cancelled:
            self.seg_status.setText("自动分割已取消。")
            self.statusBar().showMessage("自动分割已取消。")
            return
        self.seg_status.setText("自动分割失败。")
        QtWidgets.QMessageBox.warning(self, "自动分割失败", message)
        self.statusBar().showMessage("自动分割失败。")

    def _on_segmentation_thread_finished(self):
        self._set_segmentation_running(False)
        self._seg_thread = None
        self._seg_worker = None
        self._seg_engine = None

    def _load_segmentation_masks(self, output_dir, expected_names):
        files = segmentation.mask_files(output_dir, expected_names)
        if not files:
            raise RuntimeError("没有找到自动分割输出的 .nii/.nii.gz mask。")

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
                    item = {
                        "name": name,
                        "voxels": stats["voxels"],
                        "volume_ml": stats["volume_ml"],
                    }
                    if name == "lung_nodule":
                        # 分割完成后要把参考坐标轴定位到最大结节，
                        # 载入时顺手算好质心，避免回头再碰 mask。
                        # 同时枚举全部连通域：结节定位卡片逐个点击用，
                        # focus 直接取最大者，不再单独跑一遍洪泛。
                        components = segmentation.mask_components(mask)
                        item["components"] = components
                        item["focus"] = (
                            dict(components[0], components=len(components))
                            if components else None)
                    loaded_items.append(item)
                else:
                    skipped += 1
        finally:
            self.viewer.end_segmentation_update()
        return loaded, skipped, loaded_items

    def _segmentation_opacity(self, name):
        if name == "lung_nodule":
            return 1.0
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
            # 退出分割观察模式：恢复所有组织层显示（含肺结节模式自动隐藏的层）
            self.viewer.set_all_tissues_visible(True)
        self.seg_status.setText("分割叠加已清除，组织层已恢复全部显示。")
        self.seg_result.setText("")
        self._populate_nodule_cards(None)
        self.statusBar().showMessage("分割叠加已清除，组织层已恢复全部显示。")

    def _highlight_segmentations(self):
        if not hasattr(self, "viewer") or not self.viewer.segmentation_names():
            self.statusBar().showMessage("当前没有可突出的分割叠加。")
            return
        self.viewer.set_volume_visible(True)
        # 分割完成后把整体不透明度拉到最低，最大限度透出彩色分割
        self._set_mode(self._min_opacity_scale())
        self.viewer.set_volume_visible(True)
        self.statusBar().showMessage("已保留原始 CT 并调淡，突出彩色分割结果。")

    def _format_segmentation_result(self, loaded_items):
        """分割清单纯文本（seg_result 的悬停提示用；正文只保留一行结论）。"""
        rows = []
        max_rows = 36
        for item in loaded_items[:max_rows]:
            rows.append("%s %.1f ml / %d 体素" % (
                segmentation.segment_display_name(item["name"]),
                item["volume_ml"], item["voxels"]))
        if len(loaded_items) > max_rows:
            rows.append("... 还有 %d 个部位已加载到 3D/三视图" % (
                len(loaded_items) - max_rows))
        return "本次分割:\n" + "\n".join(rows)

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
        preset = segmentation.preset_by_name(self.seg_preset.currentText())
        is_monai = segmentation.is_monai_nodule_preset(preset)
        self.seg_preset.setEnabled(idle)
        self.seg_fast.setEnabled(idle and not is_monai)
        self.seg_lowmem.setEnabled(idle and not is_monai)
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
                  self.btn_plan_auto, self.btn_plan_entry, self.btn_plan_tip,
                  self.btn_plan_clear,
                  self.sim_power, self.sim_time, self.sim_speed, self.btn_sim_start,
                  self.act_reset, self.act_shot, self.act_mesh):
            w.setEnabled(on)
        self._update_segmentation_controls()
        if not on and getattr(self, "_sim_running", False):
            self._stop_simulation()

    def _refresh_device_ports(self):
        """一次枚举，同时刷新通用串口与 HW100 协议串口列表。"""
        ports = self._serial.available_ports()
        self._refresh_serial_ports(ports)
        self._refresh_mwa_ports(ports)

    def _probe_seg_cuda(self):
        """首帧后再探测 CUDA，避免启动时 import torch 卡住。"""
        if not hasattr(self, "seg_device"):
            return
        current = self.seg_device.currentData()
        cuda_name = segmentation.cuda_device_name()
        self.seg_device.blockSignals(True)
        self.seg_device.clear()
        if cuda_name:
            self.seg_device.addItem("GPU · %s" % cuda_name, "gpu")
        else:
            self.seg_device.addItem("GPU（尝试使用 CUDA）", "gpu")
        self.seg_device.addItem("CPU", "cpu")
        index = self.seg_device.findData(current)
        if index >= 0:
            self.seg_device.setCurrentIndex(index)
        self.seg_device.blockSignals(False)

    def _ensure_exit_overlay(self):
        if self._exit_overlay is not None:
            return
        self._exit_overlay = ExitConfirmOverlay(self)
        self._exit_overlay.confirmed.connect(self._confirm_exit)
        self._exit_overlay.cancelled.connect(self._cancel_exit_confirmation)

    def _show_exit_confirmation(self):
        """显示退出确认层：从居中位置下方浮现。"""
        self._ensure_exit_overlay()
        self._exit_overlay.present()

    def _cancel_exit_confirmation(self):
        if self._exit_overlay is not None:
            self._exit_overlay.dismiss()

    def _confirm_exit(self):
        self._allow_close = True
        if self._exit_overlay is not None:
            self._exit_overlay.dismiss()
        if (self._dataset_load_thread is not None
                and self._dataset_load_thread.isRunning()):
            # 不在线程仍执行 pydicom/VTK 数据构建时销毁 QThread；完成后直接
            # 退出，不再把结果挂入已关闭的渲染窗口。
            self._close_after_load = True
            self.statusBar().showMessage("正在结束影像加载，完成后自动退出…")
            return
        self.close()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._exit_overlay is not None and self._exit_overlay.isVisible():
            self._exit_overlay._sync_geometry()
        if (self._planning_alert_overlay is not None
                and self._planning_alert_overlay.isVisible()):
            self._planning_alert_overlay._sync_geometry()
        if self._mwa_overlay is not None and self._mwa_overlay.isVisible():
            self._mwa_overlay._sync_geometry()
        if self._serial_overlay is not None and self._serial_overlay.isVisible():
            self._serial_overlay._sync_geometry()
        if (self._file_dialog_overlay is not None
                and self._file_dialog_overlay.isVisible()):
            self._file_dialog_overlay._sync_geometry()

    def moveEvent(self, event):
        super().moveEvent(event)
        if self._exit_overlay is not None and self._exit_overlay.isVisible():
            self._exit_overlay._sync_geometry()
        if (self._planning_alert_overlay is not None
                and self._planning_alert_overlay.isVisible()):
            self._planning_alert_overlay._sync_geometry()
        if self._mwa_overlay is not None and self._mwa_overlay.isVisible():
            self._mwa_overlay._sync_geometry()
        if self._serial_overlay is not None and self._serial_overlay.isVisible():
            self._serial_overlay._sync_geometry()
        if (self._file_dialog_overlay is not None
                and self._file_dialog_overlay.isVisible()):
            self._file_dialog_overlay._sync_geometry()

    def showEvent(self, event):
        """窗口首次显示时一次性初始化全部 VTK，避免先露出黑框再补画。"""
        super().showEvent(event)
        if self._viewer_init_started:
            return
        self._viewer_init_started = True
        self.viewer.initialize()
        # CUDA 探测仍可延后：不影响首屏画面完整性
        QtCore.QTimer.singleShot(0, self._probe_seg_cuda)
        # 首屏之后再预热弹窗，避免首次点开时才付构建/套样式/首帧绘制的开销
        QtCore.QTimer.singleShot(400, self._prewarm_overlays)

    def prepare_first_frame(self):
        """在启动页后方完成布局、Qt 控件与全部 VTK 画布的首帧合成。"""
        app = QtWidgets.QApplication.instance()
        if app is None:
            return

        self.ensurePolished()
        central = self.centralWidget()
        if central is not None:
            central.ensurePolished()
            layout = central.layout()
            if layout is not None:
                layout.activate()

        flags = QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
        # 第一轮先落实窗口尺寸、布局和所有 show/resize 事件。
        app.sendPostedEvents(None, QtCore.QEvent.Type.UpdateRequest)
        app.processEvents(flags)

        # VTK/OpenGL 使用独立原生画布，需要显式提交各自的完整首帧。
        self.viewer.render_all_views()
        self.repaint()

        # 给窗口系统约两帧时间在启动页后方完成原生子画布合成。
        settle_loop = QtCore.QEventLoop(self)
        QtCore.QTimer.singleShot(34, settle_loop.quit)
        settle_loop.exec(flags)

        # 第二轮提交最终画面，完成后主窗口即可作为一个整体揭开。
        app.sendPostedEvents(None, QtCore.QEvent.Type.UpdateRequest)
        app.processEvents(flags)
        self.viewer.render_all_views()
        self.repaint()
        app.processEvents(flags)

    def closeEvent(self, event):
        if not self._allow_close:
            event.ignore()
            self._show_exit_confirmation()
            return
        if hasattr(self, "_serial"):
            self._serial.close()
        if hasattr(self, "_vna"):
            self._vna.disconnect_analyzer()
        if hasattr(self, "_mwa"):
            self._mwa.close()
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
