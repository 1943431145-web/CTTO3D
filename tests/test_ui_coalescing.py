"""滑块合批刷新的行为测试。

图层透明度/窗宽窗位滑块拖动时 valueChanged 每像素一报，而回调会重建
传递函数并触发整视图渲染；这两行控件必须合批（见 viewer.py 的
_LayerMenuRow/_WindowLevelRow），释放滑块时立即落最终值。
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets

from ctto3d.viewer import _LayerMenuRow, _WindowLevelRow


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


if __name__ == "__main__":
    unittest.main()
