"""肺结节分割后自动定位到最大结节的功能测试。

- largest_mask_component：纯 numpy 连通域标记（6 邻接）+ 最大连通域质心，
  重点验证 (i, j, k) 轴向约定与 VTK 标量扁平化顺序（x 最快）一致。
- 分割完成回调 _on_segmentation_finished 的接线：有 focus 就把参考
  坐标轴跳到最大结节质心，无 focus（空 mask）则保持原提示。
"""
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import vtk
from vtk.util import numpy_support

from ctto3d.segmentation import largest_mask_component


def _mask_from_zyx(arr_zyx, spacing=(1.0, 1.0, 1.0)):
    nz, ny, nx = arr_zyx.shape
    image = vtk.vtkImageData()
    image.SetDimensions(nx, ny, nz)
    image.SetSpacing(*spacing)
    image.SetOrigin(0.0, 0.0, 0.0)
    flat = np.ascontiguousarray(arr_zyx.astype(np.uint8).reshape(-1))
    image.GetPointData().SetScalars(numpy_support.numpy_to_vtk(flat))
    return image


class LargestMaskComponentTests(unittest.TestCase):
    def test_picks_largest_of_two_blobs_with_correct_centroid(self):
        arr = np.zeros((12, 12, 12), np.uint8)
        arr[2:5, 1:4, 1:4] = 1     # 3x3x3=27 体素，质心 (i,j,k)=(2,2,3)
        arr[8:10, 8:10, 8:10] = 1  # 2x2x2=8 体素
        result = largest_mask_component(_mask_from_zyx(arr))
        self.assertEqual(result["components"], 2)
        self.assertEqual(result["voxels"], 27)
        self.assertTrue(np.allclose(result["ijk"], (2.0, 2.0, 3.0)))
        self.assertAlmostEqual(result["volume_ml"], 0.027)

    def test_centroid_reflects_xyz_axis_convention(self):
        # 不对称放置的单个 2 体素 blob：i=5..6, j=3, k=7
        arr = np.zeros((10, 10, 10), np.uint8)
        arr[7, 3, 5] = 1
        arr[7, 3, 6] = 1
        result = largest_mask_component(_mask_from_zyx(arr))
        self.assertTrue(np.allclose(result["ijk"], (5.5, 3.0, 7.0)))

    def test_corner_touching_voxels_are_separate_components(self):
        # 仅对角相邻：6 邻接下应算两个连通域
        arr = np.zeros((4, 4, 4), np.uint8)
        arr[1, 1, 1] = 1
        arr[2, 2, 2] = 1
        result = largest_mask_component(_mask_from_zyx(arr))
        self.assertEqual(result["components"], 2)
        self.assertEqual(result["voxels"], 1)

    def test_empty_mask_returns_none(self):
        arr = np.zeros((5, 5, 5), np.uint8)
        self.assertIsNone(largest_mask_component(_mask_from_zyx(arr)))

    def test_volume_uses_voxel_spacing(self):
        arr = np.zeros((6, 6, 6), np.uint8)
        arr[1:3, 1:3, 1:3] = 1          # 8 体素
        result = largest_mask_component(
            _mask_from_zyx(arr, spacing=(2.0, 2.0, 5.0)))
        self.assertAlmostEqual(result["volume_ml"], 8 * 20.0 / 1000.0)


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


class _ViewerStub:
    def __init__(self):
        self.crosshair_calls = []
        self._tissue_order = ["lung", "bone", "fat"]
        self._tissue_visible = {"lung": True, "bone": True, "fat": False}

    def show_only_tissues(self, *_a):
        return True

    def set_crosshair_ijk(self, ijk):
        self.crosshair_calls.append(tuple(ijk))


def _make_window(loaded_items):
    """构造一个只实现 _on_segmentation_finished 所需面的窗口桩。"""
    viewer = _ViewerStub()

    class WindowStub:
        _seg_cancelled = False
        _seg_engine = "monai_nodule"
        seg_status = _RecordingLabel()
        seg_result = _RecordingLabel()
        statusBar = _RecordingStatusBar()

        def _cleanup_seg_temp_dir(self):
            pass

        def _load_segmentation_masks(self, *_a):
            return len(loaded_items), 0, loaded_items

        def _highlight_segmentations(self):
            pass

        @staticmethod
        def _format_segmentation_result(_items):
            return ""

    window = WindowStub()
    window.viewer = viewer          # 类体不走闭包查找，实例化后挂上
    return window


class NoduleFocusWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6 import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_finished_handler_moves_crosshair_to_largest_nodule(self):
        from ctto3d.mainwindow import MainWindow

        loaded_items = [{
            "name": "lung_nodule",
            "voxels": 27,
            "volume_ml": 0.027,
            "focus": {"ijk": (12.0, 34.0, 56.0), "voxels": 27,
                      "volume_ml": 0.027, "components": 3},
        }]
        window = _make_window(loaded_items)
        MainWindow._on_segmentation_finished(window, None, None)
        self.assertEqual(window.viewer.crosshair_calls, [(12.0, 34.0, 56.0)])
        self.assertIn("最大结节", window.statusBar.message)
        self.assertIn("最大结节", window.seg_result.text)

    def test_finished_handler_without_focus_keeps_old_message(self):
        from ctto3d.mainwindow import MainWindow

        loaded_items = [{
            "name": "lung_nodule", "voxels": 0, "volume_ml": 0.0,
            "focus": None,
        }]
        window = _make_window(loaded_items)
        MainWindow._on_segmentation_finished(window, None, None)
        self.assertEqual(window.viewer.crosshair_calls, [])
        self.assertNotIn("定位", window.statusBar.message)


if __name__ == "__main__":
    unittest.main()
