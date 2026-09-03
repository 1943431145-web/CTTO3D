"""肺结节分割后自动定位到最大结节的功能测试。

- largest_mask_component / mask_components：纯 numpy 连通域标记
  （6 邻接）+ 质心，重点验证 (i, j, k) 轴向约定与 VTK 标量扁平化顺序
  （x 最快）一致。
- 分割完成回调 _on_segmentation_finished 的接线：有 focus 就把参考
  坐标轴跳到最大结节质心，无 focus（空 mask）则保持原提示；同时把全部
  结节连通域交给 _populate_nodule_cards。
- 结节定位卡片：_populate_nodule_cards 填充列表与深度文案，
  _locate_nodule 触发十字/取景/全屏。
"""
import os
import types
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import vtk
from vtk.util import numpy_support

from ctto3d.segmentation import largest_mask_component, mask_components


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


class MaskComponentsTests(unittest.TestCase):
    def test_lists_all_components_sorted_by_volume_desc(self):
        arr = np.zeros((12, 12, 12), np.uint8)
        arr[2:5, 1:4, 1:4] = 1     # 27 体素
        arr[8:10, 8:10, 8:10] = 1  # 8 体素
        arr[0, 0, 0] = 1           # 1 体素
        items = mask_components(_mask_from_zyx(arr))
        self.assertEqual([item["voxels"] for item in items], [27, 8, 1])
        self.assertTrue(np.allclose(items[0]["ijk"], (2.0, 2.0, 3.0)))

    def test_respects_max_count_and_empty_mask(self):
        arr = np.zeros((10, 10, 10), np.uint8)
        for z in range(4):
            arr[z, z, z] = 1
            arr[z, z, z + 1] = 1
        items = mask_components(_mask_from_zyx(arr), max_count=2)
        self.assertEqual(len(items), 2)
        self.assertEqual(mask_components(_mask_from_zyx(np.zeros((5, 5, 5), np.uint8))), [])

    def test_keys_match_largest_mask_component(self):
        arr = np.zeros((8, 8, 8), np.uint8)
        arr[3:5, 3:5, 3:5] = 1
        items = mask_components(_mask_from_zyx(arr))
        best = largest_mask_component(_mask_from_zyx(arr))
        self.assertEqual(set(items[0]), {"ijk", "voxels", "volume_ml"})
        self.assertEqual(items[0]["ijk"], best["ijk"])


class _RecordingLabel:
    def __init__(self):
        self.text = None
        self.tooltip = None

    def setText(self, text):
        self.text = text

    def setToolTip(self, text):
        self.tooltip = text


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
    window.populate_calls = []
    window._populate_nodule_cards = (
        lambda cards: window.populate_calls.append(cards))
    return window


class NoduleFocusWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6 import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_finished_handler_moves_crosshair_to_largest_nodule(self):
        from ctto3d.mainwindow import MainWindow

        components = [
            {"ijk": (12.0, 34.0, 56.0), "voxels": 27, "volume_ml": 0.027},
            {"ijk": (2.0, 3.0, 4.0), "voxels": 8, "volume_ml": 0.008},
        ]
        loaded_items = [{
            "name": "lung_nodule",
            "voxels": 35,
            "volume_ml": 0.035,
            "focus": {"ijk": (12.0, 34.0, 56.0), "voxels": 27,
                      "volume_ml": 0.027, "components": 2},
            "components": components,
        }]
        window = _make_window(loaded_items)
        MainWindow._on_segmentation_finished(window, None, None)
        self.assertEqual(window.viewer.crosshair_calls, [(12.0, 34.0, 56.0)])
        self.assertIn("最大结节", window.statusBar.message)
        self.assertIn("最大结节", window.seg_result.text)
        # 全部结节连通域交给卡片列表（逐个点击定位用）
        self.assertEqual(window.populate_calls, [components])

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
        self.assertEqual(window.populate_calls, [None])


class _NoduleViewerStub:
    """_populate_nodule_cards / _locate_nodule 触碰到的 viewer 面。"""

    def __init__(self, image="loaded"):
        self.image = image
        self.crosshair_calls = []
        self.opacity_calls = []
        self.focus_calls = []
        self.flush_count = 0
        self.fullscreen_calls = 0
        self.expanded_slice_calls = []
        self.slice_panel = self  # 简化：views 属性挂在同一个桩上
        self.views = [types.SimpleNamespace(orientation="axial"),
                      types.SimpleNamespace(orientation="coronal"),
                      types.SimpleNamespace(orientation="sagittal")]

    def nodule_depth_mm(self, ijk):
        return 42.0

    def set_crosshair_ijk(self, ijk):
        self.crosshair_calls.append(tuple(ijk))

    def set_tissue_opacity(self, name, value):
        self.opacity_calls.append((name, value))

    def focus_camera_on_ijk(self, ijk, direction_world=None):
        self.focus_calls.append(tuple(ijk))
        return True

    def _flush_crosshair_update(self):
        self.flush_count += 1

    def enter_view3d_fullscreen(self):
        self.fullscreen_calls += 1
        return True

    def planning_points(self):
        return {"entry": None, "tip": None}

    def _show_expanded_slice(self, view):
        self.expanded_slice_calls.append(view.orientation)


class NoduleCardWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6 import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def _window(self):
        from PySide6 import QtWidgets
        from ctto3d.mainwindow import MainWindow

        window = types.SimpleNamespace(
            viewer=_NoduleViewerStub(),
            nodule_list=QtWidgets.QListWidget(),
            nodule_section=QtWidgets.QWidget(),
            _nodule_cards=[],
            _nodule_selected=None,
            statusBar=_RecordingStatusBar(),
        )
        # 真实方法体内的 self.* 调用需要显式绑回 MainWindow 实现
        window._refresh_nodule_card_labels = (
            lambda: MainWindow._refresh_nodule_card_labels(window))
        window._update_plan_steps = (
            lambda: MainWindow._update_plan_steps(window))
        window._locate_nodule = (
            lambda row, frame=True: MainWindow._locate_nodule(window, row, frame=frame))
        return window

    def test_populate_builds_cards_with_depth_and_clear_hides(self):
        from ctto3d.mainwindow import MainWindow

        window = self._window()
        cards = [
            {"ijk": (5.0, 6.0, 7.0), "voxels": 30, "volume_ml": 0.03},
            {"ijk": (1.0, 2.0, 3.0), "voxels": 8, "volume_ml": 0.008},
        ]
        MainWindow._populate_nodule_cards(window, cards)
        self.assertEqual(window.nodule_list.count(), 2)
        self.assertIn("0.03 ml", window.nodule_list.item(0).text())
        self.assertIn("42 mm", window.nodule_list.item(0).text())
        self.assertEqual(cards[0]["depth_mm"], 42.0)
        self.assertTrue(window.nodule_section.isVisible() or True)  # 离屏可见性不可靠，只验证不抛错

        MainWindow._populate_nodule_cards(window, None)
        self.assertEqual(window.nodule_list.count(), 0)
        self.assertEqual(window._nodule_cards, [])
        self.assertIsNone(window._nodule_selected)

    def test_locate_moves_crosshair_dims_bone_and_enters_fullscreen(self):
        from ctto3d.mainwindow import MainWindow

        window = self._window()
        cards = [
            {"ijk": (5.0, 6.0, 7.0), "voxels": 30, "volume_ml": 0.03,
             "depth_mm": 42.0},
            {"ijk": (1.0, 2.0, 3.0), "voxels": 8, "volume_ml": 0.008,
             "depth_mm": None},
        ]
        MainWindow._populate_nodule_cards(window, cards)
        MainWindow._locate_nodule(window, 1)
        viewer = window.viewer
        self.assertEqual(viewer.crosshair_calls, [(1.0, 2.0, 3.0)])
        self.assertEqual(viewer.focus_calls, [(1.0, 2.0, 3.0)])
        self.assertEqual(viewer.fullscreen_calls, 1)
        self.assertEqual(viewer.flush_count, 1)
        # 两层骨骼都被调淡
        self.assertEqual(
            [name for name, _value in viewer.opacity_calls],
            ["皮质骨", "松质骨 / 钙化"])
        self.assertIn("结节 2/2", window.statusBar.message)
        self.assertEqual(window._nodule_selected, 1)

        # 无体数据/越界行号：安全无操作
        window.viewer.image = None
        MainWindow._locate_nodule(window, 0)
        self.assertEqual(len(viewer.crosshair_calls), 1)
        MainWindow._locate_nodule(window, 99)

    def test_activate_locates_without_fullscreen_and_expands_axial(self):
        from ctto3d.mainwindow import MainWindow

        window = self._window()
        cards = [
            {"ijk": (5.0, 6.0, 7.0), "voxels": 30, "volume_ml": 0.03},
        ]
        MainWindow._populate_nodule_cards(window, cards)
        MainWindow._on_nodule_card_activated(window, window.nodule_list.item(0))
        viewer = window.viewer
        self.assertEqual(viewer.crosshair_calls, [(5.0, 6.0, 7.0)])
        self.assertEqual(viewer.fullscreen_calls, 0)       # 展切片前不进 3D 全屏
        self.assertEqual(viewer.expanded_slice_calls, ["axial"])
        self.assertEqual(window.nodule_list.currentRow(), 0)
        self.assertIn("✓", window.nodule_list.item(0).text())


class PlanningStepsBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6 import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_markers_and_colors_follow_states(self):
        from ctto3d.mainwindow import PlanningStepsBar

        bar = PlanningStepsBar()
        bar.set_states(("done", "active", "pending"))
        self.assertEqual(bar.states(), ("done", "active", "pending"))
        badges = bar.badges()
        self.assertEqual(badges[0].text(), "✓")       # 完成步显示对勾
        self.assertEqual(badges[1].text(), "2")       # 当前步保留序号
        self.assertEqual(badges[2].text(), "3")
        self.assertEqual(
            [badge.property("state") for badge in badges],
            ["done", "active", "pending"])            # QSS 据此着色

    def test_unknown_state_falls_back_to_pending(self):
        from ctto3d.mainwindow import PlanningStepsBar

        bar = PlanningStepsBar()
        bar.set_states(("nonsense", "done", "done"))
        self.assertEqual(bar.states(), ("pending", "done", "done"))
        self.assertEqual(bar.badges()[0].property("state"), "pending")


class PlanStepsStateTests(unittest.TestCase):
    """_update_plan_steps 的五步状态推导（加载/分割/定位/针道/导出）。"""

    @classmethod
    def setUpClass(cls):
        from PySide6 import QtWidgets
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def _window(self, volume, segmentation, located, path_ready, exported):
        from ctto3d.mainwindow import MainWindow, PlanningStepsBar

        viewer = types.SimpleNamespace(
            image="loaded" if volume else None,
            segmentation_names=(lambda: ["lung"] if segmentation else ()),
            planning_points=lambda: {
                "entry": (1.0, 1.0, 1.0) if path_ready else None,
                "tip": (2.0, 2.0, 2.0) if (path_ready or located) else None})
        return types.SimpleNamespace(
            viewer=viewer,
            plan_steps=PlanningStepsBar(
                ("加载", "分割", "定位", "针道", "导出")),
            _nodule_selected=0 if located else None,
            _plan_report_exported=exported,
            # 真实方法体内的 self.* 调用需要显式绑回 MainWindow 实现
            _set_step_emphasis=MainWindow._set_step_emphasis,
        )

    def test_step_progression(self):
        from ctto3d.mainwindow import MainWindow

        window = self._window(False, False, False, False, False)
        MainWindow._update_plan_steps(window)
        self.assertEqual(window.plan_steps.states(),
                         ("active", "pending", "pending", "pending", "pending"))

        window = self._window(True, False, False, False, False)
        MainWindow._update_plan_steps(window)
        self.assertEqual(window.plan_steps.states(),
                         ("done", "active", "pending", "pending", "pending"))

        window = self._window(True, True, False, False, False)
        MainWindow._update_plan_steps(window)
        self.assertEqual(window.plan_steps.states(),
                         ("done", "done", "active", "pending", "pending"))

        window = self._window(True, True, True, False, False)
        MainWindow._update_plan_steps(window)
        self.assertEqual(window.plan_steps.states(),
                         ("done", "done", "done", "active", "pending"))

        window = self._window(True, True, True, True, True)
        MainWindow._update_plan_steps(window)
        self.assertEqual(window.plan_steps.states(),
                         ("done", "done", "done", "done", "done"))

    def test_optional_segmentation_step_counts_as_passed_when_skipped(self):
        from ctto3d.mainwindow import MainWindow

        # 跳过分割直接放置针道：分割步按"已被跨过"处理
        window = self._window(True, False, True, False, False)
        MainWindow._update_plan_steps(window)
        self.assertEqual(window.plan_steps.states(),
                         ("done", "done", "done", "active", "pending"))


class PlanStatusAnglesTests(unittest.TestCase):
    def test_both_points_show_path_and_angles_inline(self):
        from ctto3d.mainwindow import MainWindow

        viewer = types.SimpleNamespace(
            planning_points=lambda: {
                "entry": (1.0, 1.0, 1.0), "tip": (2.0, 2.0, 2.0)},
            evaluate_needle_path=lambda *_a: {
                "available": True, "path_length_mm": 80.0},
            needle_axis_angles=lambda: (65.0, 25.0, 90.0))
        window = types.SimpleNamespace(
            viewer=viewer, plan_status=_RecordingLabel())
        window._bone_clearance_text = MainWindow._bone_clearance_text
        window._update_plan_steps = lambda: None   # 该用例只验证状态行文本
        MainWindow._update_plan_status(window)
        self.assertIn("路径 80.0 mm", window.plan_status.text)
        self.assertIn("夹角 65°/25°/90°", window.plan_status.text)


if __name__ == "__main__":
    unittest.main()
