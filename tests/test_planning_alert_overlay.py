import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtGui, QtWidgets

from ctto3d.mainwindow import (
    FrostedCenterOverlay,
    MainWindow,
    PlanningAlertOverlay,
)


class PlanningAlertOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_alert_reuses_exit_dialog_widgets_for_single_and_dual_actions(self):
        host = QtWidgets.QWidget()
        overlay = PlanningAlertOverlay(host)

        overlay.configure("针道不可选", "当前路径经过骨骼，不可选择。")
        self.assertEqual(overlay._card.objectName(), "ExitDialogCard")
        self.assertEqual(overlay.title.objectName(), "ExitDialogTitle")
        self.assertEqual(overlay.message.objectName(), "ExitDialogQuestion")
        self.assertEqual(overlay.btn_confirm.objectName(), "ExitConfirm")
        self.assertEqual(overlay.btn_confirm.text(), "知道了")
        self.assertTrue(overlay.btn_cancel.isHidden())

        overlay.configure(
            "针道近骨提醒", "请确认已复核。",
            confirm_text="继续仿真", cancel_text="返回检查")
        self.assertEqual(overlay.btn_confirm.text(), "继续仿真")
        self.assertEqual(overlay.btn_cancel.text(), "返回检查")
        self.assertFalse(overlay.btn_cancel.isHidden())

        overlay.deleteLater()
        host.deleteLater()

    def test_near_bone_path_waits_for_explicit_confirmation(self):
        class ViewerStub:
            @staticmethod
            def has_ablation_needle():
                return True

            @staticmethod
            def current_needle_bone_analysis():
                return {
                    "available": True,
                    "too_long": False,
                    "collision": False,
                    "near_bone": True,
                    "bone_clearance_mm": 2.0,
                }

        class WindowStub:
            viewer = ViewerStub()

            @staticmethod
            def _bone_clearance_text(_analysis):
                return "2.0 mm"

            def _show_planning_alert(self, title, message, **options):
                self.alert = (title, message, options)

            def _start_simulation(self, *args, **kwargs):
                self.resume_call = (args, kwargs)

        window = WindowStub()
        MainWindow._start_simulation(window)

        title, _message, options = window.alert
        self.assertEqual(title, "针道近骨提醒")
        self.assertEqual(options["confirm_text"], "继续仿真")
        self.assertEqual(options["cancel_text"], "返回检查")
        self.assertFalse(hasattr(window, "resume_call"))

        options["on_confirm"]()
        self.assertEqual(
            window.resume_call,
            ((), {"skip_near_bone_warning": True}))

    def test_each_presentation_refreshes_the_background_snapshot(self):
        class CountingOverlay(FrostedCenterOverlay):
            def __init__(self, parent):
                self.capture_count = 0
                super().__init__(parent, card_width=300)

            def _populate_card(self, card_lay):
                card_lay.addWidget(QtWidgets.QLabel("测试"))

            def _capture_frosted(self, host):
                self.capture_count += 1
                pixmap = QtGui.QPixmap(max(1, host.width()), max(1, host.height()))
                pixmap.fill(QtGui.QColor("#123456"))
                return pixmap

        host = QtWidgets.QWidget()
        host.resize(640, 480)
        overlay = CountingOverlay(host)
        overlay._sync_scrim()
        overlay._sync_geometry()
        overlay._get_frosted_background(host)
        self.assertEqual(overlay.capture_count, 1)

        overlay.present()
        self.assertEqual(overlay.capture_count, 2)

        overlay._stop_progress_anim()
        overlay.hide()
        overlay.deleteLater()
        host.deleteLater()


if __name__ == "__main__":
    unittest.main()
