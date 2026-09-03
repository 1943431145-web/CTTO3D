"""滑块合批刷新与遥测刷新死区行为测试。

图层透明度/窗宽窗位滑块拖动时 valueChanged 每像素一报，而回调会重建
传递函数并触发整视图渲染；这两行控件必须合批（见 viewer.py 的
_LayerMenuRow/_WindowLevelRow），释放滑块时立即落最终值。主面板的
不透明度滑块（mainwindow.py MainWindow）采用同一契约；IMU 遥测约
20 帧/秒，数值无实质变化时不得触发全量体渲染（viewer.py 死区）。
"""
import os
import types
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from ctto3d.mainwindow import MainWindow
from ctto3d.viewer import VolumeViewer, _LayerMenuRow, _WindowLevelRow


class SliderCoalescingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def _drain(self, row, timer_attr):
        timer = getattr(row, timer_attr)
        timer.timeout.emit()          # 手动触发到期，不依赖真实事件循环

    def test_layer_row_coalesces_rapid_slides_into_one_callback(self):
        calls = []
        row = _LayerMenuRow(
            "bone", "骨骼", (1.0, 1.0, 1.0), True, 1.0,
            on_visible=lambda *_: None,
            on_opacity=lambda name, value: calls.append((name, value)))
        for value in range(0, 101, 5):
            row._on_slide(value)
        self.assertEqual(calls, [])          # 拖动中不落回调
        self.assertEqual(row.pct.text(), "100%")
        self._drain(row, "_opacity_flush")
        self.assertEqual(calls, [("bone", 1.0)])   # 只落最终值

        row.deleteLater()

    def test_layer_row_flushes_on_slider_release(self):
        calls = []
        row = _LayerMenuRow(
            "lung", "肺部", (0.9, 0.9, 0.9), True, 1.0,
            on_visible=lambda *_: None,
            on_opacity=lambda name, value: calls.append((name, value)))
        row._on_slide(37)
        row.slider.sliderReleased.emit()      # 释放立即生效，不等定时器
        self.assertEqual(calls, [("lung", 0.37)])
        row.deleteLater()

    def test_window_level_row_coalesces_and_flushes_final_value(self):
        calls = []
        row = _WindowLevelRow("窗宽", 0, 4000, 1500,
                              on_change=lambda value: calls.append(value))
        for value in (1600, 1700, 1800, 1900):
            row._on_slide(value)
        self.assertEqual(calls, [])
        self._drain(row, "_wl_flush")
        self.assertEqual(calls, [1900.0])

        row._on_slide(2200)
        row.slider.sliderReleased.emit()
        self.assertEqual(calls, [1900.0, 2200.0])
        row.deleteLater()


class _StubCheckable:
    def __init__(self):
        self.checked = None

    def setChecked(self, on):
        self.checked = on


class _StubTimer:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1


class _StubSlider:
    def __init__(self):
        self.values = []

    def blockSignals(self, block):
        pass

    def setValue(self, value):
        self.values.append(value)


class _StubViewer:
    def __init__(self):
        self.scales = []
        self.visible_calls = []

    def set_volume_visible(self, visible):
        self.visible_calls.append(visible)

    def set_opacity_scale(self, scale):
        self.scales.append(scale)


class MainOpacitySliderCoalescingTests(unittest.TestCase):
    """主面板不透明度滑块与图层行采用同一套 40ms 合批契约。

    用桩对象 + 未绑定方法调用，避免为一条滑块逻辑构造整个主窗口。
    """

    def _window(self):
        return types.SimpleNamespace(
            btn_opaque=_StubCheckable(),
            btn_transparent=_StubCheckable(),
            opacity_slider=_StubSlider(),
            _pending_opacity_scale=None,
            _opacity_flush_timer=_StubTimer(),
            viewer=_StubViewer(),
        )

    def test_rapid_slides_apply_once_on_flush(self):
        window = self._window()
        for value in (20, 30, 40, 50):
            MainWindow._on_slider(window, value)
        self.assertEqual(window.viewer.scales, [])       # 拖动中不落回调
        self.assertEqual(window._pending_opacity_scale, 0.5)
        MainWindow._flush_opacity_scale(window)
        self.assertEqual(window.viewer.scales, [0.5])    # 只落最终值
        self.assertIsNone(window._pending_opacity_scale)
        MainWindow._flush_opacity_scale(window)          # 无待落值时是空操作
        self.assertEqual(window.viewer.scales, [0.5])

    def test_set_mode_discards_stale_pending_value(self):
        window = self._window()
        MainWindow._on_slider(window, 50)               # 拖动合批中
        MainWindow._set_mode(window, 0.2)               # 随即点了"透明"
        self.assertEqual(window.viewer.scales, [0.2])   # 按钮选择立即生效
        MainWindow._flush_opacity_scale(window)          # 过期值不得再覆盖
        self.assertEqual(window.viewer.scales, [0.2])


class _ImuViewerShell:
    """把 VolumeViewer 的真实方法挂到轻量壳上（PySide6 包装类无法用
    object.__new__ 跳过控件构造），只提供这些方法触碰到的属性。"""

    _IMU_READOUT_EPS = VolumeViewer._IMU_READOUT_EPS
    _IMU_MAGNETIC_EPS = VolumeViewer._IMU_MAGNETIC_EPS
    _imu_readout_changed = VolumeViewer._imu_readout_changed
    set_imu_sensor_readout = VolumeViewer.set_imu_sensor_readout
    clear_imu_sensor_readout = VolumeViewer.clear_imu_sensor_readout

    def __init__(self, readout=None, image=None):
        self._imu_readout = readout
        self.image = image
        self.renders = 0
        self.refreshes = 0

    def render(self):
        self.renders += 1

    def _refresh_imu_readout_overlay(self):
        self.refreshes += 1


class ImuReadoutDeadzoneTests(unittest.TestCase):
    """IMU 遥测死区：数值无实质变化 / 无体数据时不触发重渲染。"""

    def _viewer(self, readout=None, image=None):
        return _ImuViewerShell(readout=readout, image=image)

    def test_readout_always_cached_but_render_gated(self):
        viewer = self._viewer()
        viewer.set_imu_sensor_readout(36.6, 10.0, -5.0, 90.0)
        self.assertEqual(viewer.refreshes, 1)       # 读数照常缓存/同步
        self.assertEqual(viewer.renders, 0)         # 无体数据 -> HUD 不可见
        viewer.image = "loaded"
        viewer.set_imu_sensor_readout(36.7, 10.0, -5.0, 90.0)
        self.assertEqual(viewer.renders, 1)         # 有数据且越过死区 -> 渲染
        viewer.set_imu_sensor_readout(36.71, 10.0, -5.0, 90.0)
        self.assertEqual(viewer.renders, 1)         # 死区内 -> 不渲染
        self.assertEqual(viewer.refreshes, 3)

    def test_threshold_crossing_counts_as_change(self):
        viewer = self._viewer(readout=(36.6, 10.0, -5.0, 90.0, None))
        self.assertFalse(viewer._imu_readout_changed(
            (36.61, 10.02, -5.03, 90.04, None)))     # 全部分量在死区内
        self.assertTrue(viewer._imu_readout_changed(
            (36.8, 10.0, -5.0, 90.0, None)))         # 温度越过死区

    def test_magnetic_appearance_and_axis_change(self):
        viewer = self._viewer(readout=(36.6, 10.0, -5.0, 90.0, None))
        self.assertTrue(viewer._imu_readout_changed(
            (36.6, 10.0, -5.0, 90.0, (1.0, 2.0, 3.0))))   # 磁场出现
        viewer._imu_readout = (36.6, 10.0, -5.0, 90.0, (1.0, 2.0, 3.0))
        self.assertFalse(viewer._imu_readout_changed(
            (36.6, 10.0, -5.0, 90.0, (1.1, 2.1, 3.1))))   # 各轴在死区内
        self.assertTrue(viewer._imu_readout_changed(
            (36.6, 10.0, -5.0, 90.0, (1.0, 2.0, 3.9))))   # 单轴越过死区
        self.assertTrue(viewer._imu_readout_changed(
            (36.6, 10.0, -5.0, 90.0, None)))              # 磁场消失

    def test_clear_only_renders_when_hud_was_shown(self):
        viewer = self._viewer(readout=(36.6, 10.0, -5.0, 90.0, None))
        viewer.clear_imu_sensor_readout()
        self.assertEqual(viewer.renders, 0)         # 本来就不可见
        viewer._imu_readout = (36.6, 10.0, -5.0, 90.0, None)
        viewer.image = "loaded"
        viewer.clear_imu_sensor_readout()
        self.assertEqual(viewer.renders, 1)         # 可见 -> 渲染一帧隐藏


if __name__ == "__main__":
    unittest.main()
