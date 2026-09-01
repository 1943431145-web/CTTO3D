"""入针点后台搜索结果回传的回归测试。

历史 bug：数据集标记曾用 numpy 视图的 id()——每次 _planning_ct_array()
都新建视图对象，id 永不相等，导致搜索结果永远被当成"影像已切换"丢弃，
自动推荐入针点功能失效。标记必须用 vtkImageData 对象身份。
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class _RecordingLabel:
    def __init__(self):
        self.text = None

    def setText(self, text):
        self.text = text


class _RecordingStatusBar:
    def __init__(self):
        self.message = None

    def __call__(self):
        return self

    def showMessage(self, text, *_a):
        self.message = text


class _RecordingButton:
    def __init__(self):
        self.enabled = True

    def setEnabled(self, on):
        self.enabled = on


class _ViewerStub:
    def __init__(self):
        self.image = object()                  # 模拟 vtkImageData 对象身份
        self.placed = []
        self._tip = (30.0, 40.0, 50.0)

    def planning_points(self):
        return {"entry": None, "tip": self._tip}

    def set_planning_point(self, kind, ijk):
        self.placed.append((kind, tuple(ijk)))
        return True


def _make_window():
    class WindowStub:
        def __init__(self):
            self.viewer = _ViewerStub()
            self.btn_plan_auto = _RecordingButton()
            self.statusBar = _RecordingStatusBar()
            self._entry_search_running = True

        def _update_plan_status(self):
            pass

        @staticmethod
        def _bone_clearance_text(_analysis):
            return "9.5 mm"

    return WindowStub()


_RESULT = {
    "entry_ijk": (10, 20, 30),
    "analysis": {"path_length_mm": 88.0, "bone_clearance_mm": 9.5,
                 "near_bone": False, "collision": False, "too_long": False},
    "valid_candidate_count": 5,
}


class EntrySearchResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6 import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_result_from_same_dataset_is_applied(self):
        from ctto3d.mainwindow import MainWindow

        window = _make_window()
        token = window.viewer.image        # 搜索发起时的数据集对象
        MainWindow._on_entry_search_finished(window, (_RESULT, token))
        self.assertEqual(window.viewer.placed, [("entry", (10, 20, 30))])
        self.assertIn("已推荐避骨入针点", window.statusBar.message)
        self.assertTrue(window.btn_plan_auto.enabled)
        self.assertFalse(window._entry_search_running)

    def test_result_from_stale_dataset_is_discarded(self):
        from ctto3d.mainwindow import MainWindow

        window = _make_window()
        window.viewer.image = object()     # 搜索期间切换了病例
        MainWindow._on_entry_search_finished(window, (_RESULT, object()))
        self.assertEqual(window.viewer.placed, [])
        self.assertIn("影像已切换", window.statusBar.message)
        self.assertTrue(window.btn_plan_auto.enabled)

    def test_none_result_shows_blocked_message(self):
        from ctto3d.mainwindow import MainWindow

        window = _make_window()
        shown = []
        window._show_planning_blocked = shown.append
        MainWindow._on_entry_search_finished(window, None)
        self.assertEqual(len(shown), 1)
        self.assertIn("未找到可达", shown[0])
        self.assertEqual(window.viewer.placed, [])
        self.assertTrue(window.btn_plan_auto.enabled)


if __name__ == "__main__":
    unittest.main()
