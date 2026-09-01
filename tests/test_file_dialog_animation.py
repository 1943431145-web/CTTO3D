import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets

from ctto3d.mainwindow import EmbeddedFileDialogOverlay


class FileDialogAnimationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    @staticmethod
    def _wait(milliseconds):
        loop = QtCore.QEventLoop()
        QtCore.QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    def setUp(self):
        self.host = QtWidgets.QMainWindow()
        self.host.setCentralWidget(QtWidgets.QWidget())
        self.host.resize(1000, 720)
        self.host.show()
        self.overlay = EmbeddedFileDialogOverlay(self.host)
        self.overlay._sync_geometry()

    def tearDown(self):
        self.overlay._stop_card_animation()
        self.overlay.deleteLater()
        self.host.deleteLater()

    def test_card_floats_in_and_retracts_before_hiding(self):
        self.assertFalse(self.overlay.isWindow())
        self.assertFalse(bool(
            self.overlay.windowFlags() & QtCore.Qt.WindowType.Tool))
        self.overlay.show()
        self.app.processEvents()
        self.overlay._play_present_animation()

        target = QtCore.QPoint(self.overlay._card_target_pos)
        self.assertEqual(self.overlay._card.pos(), target)
        self.assertTrue(self.overlay._card.isHidden())
        self.assertIsNotNone(self.overlay._card_snapshot)
        self.assertEqual(self.overlay._card_progress, 0.0)

        self._wait(420)
        self.assertEqual(self.overlay._card.pos(), target)
        self.assertTrue(self.overlay._card.isVisible())
        self.assertIsNone(self.overlay._card_snapshot)
        self.assertEqual(self.overlay._card_progress, 1.0)

        self.overlay._finish("")
        self.assertTrue(self.overlay.isVisible())
        self.assertTrue(self.overlay._closing)

        self._wait(340)
        self.assertFalse(self.overlay.isVisible())
        self.assertFalse(self.overlay._closing)

    def test_choose_can_cancel_during_present_animation_without_hanging(self):
        with tempfile.TemporaryDirectory() as root:
            QtCore.QTimer.singleShot(40, self.overlay._reject)
            selected = self.overlay.choose(
                "选择 DICOM 序列文件夹",
                root_directory=root,
                directory=True,
            )

        self.assertEqual(selected, "")
        self.assertIsNone(self.overlay._event_loop)
        self.assertFalse(self.overlay._background.isNull())
        self.assertEqual(self.overlay._background.size(), self.overlay.size())
        self.assertFalse(self.overlay.isVisible())
        self.assertFalse(self.overlay._closing)


if __name__ == "__main__":
    unittest.main()
