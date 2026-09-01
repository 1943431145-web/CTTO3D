import unittest

import numpy as np

from ctto3d import needle_planning


class NeedlePlanningTests(unittest.TestCase):
    def setUp(self):
        self.volume = np.full((64, 64, 64), -1000.0, dtype=np.float32)
        self.volume[8:56, 8:56, 8:56] = 40.0
        self.spacing = (1.0, 1.0, 1.0)

    def test_path_crossing_bone_is_rejected(self):
        self.volume[30:35, 30:35, 28:36] = 900.0
        result = needle_planning.analyze_bone_path(
            self.volume, self.spacing,
            entry_ijk=(10, 32, 32), tip_ijk=(52, 32, 32),
            needle_radius_mm=0.8, max_length_mm=100.0)
        self.assertTrue(result["collision"])
        self.assertFalse(result["too_long"])
        self.assertEqual(result["bone_clearance_mm"], 0.0)

    def test_near_bone_warns_without_rejecting(self):
        self.volume[30:35, 30:35, 28:36] = 900.0
        result = needle_planning.analyze_bone_path(
            self.volume, self.spacing,
            entry_ijk=(10, 39, 32), tip_ijk=(52, 39, 32),
            needle_radius_mm=0.8, max_length_mm=100.0)
        self.assertFalse(result["collision"])
        self.assertTrue(result["near_bone"])
        self.assertGreater(result["bone_clearance_mm"], 1.3)

    def test_path_longer_than_needle_is_reported(self):
        result = needle_planning.analyze_bone_path(
            self.volume, self.spacing,
            entry_ijk=(10, 10, 10), tip_ijk=(50, 50, 50),
            max_length_mm=50.0)
        self.assertTrue(result["too_long"])
        self.assertFalse(result["collision"])

    def test_recommendation_returns_reachable_bone_free_skin_entry(self):
        # Air-filled lung around the target and a bone plate blocking +X.
        zz, yy, xx = np.mgrid[:64, :64, :64]
        lung = ((xx - 25) ** 2 / 12 ** 2
                + (yy - 32) ** 2 / 15 ** 2
                + (zz - 32) ** 2 / 18 ** 2) <= 1.0
        self.volume[lung] = -600.0
        self.volume[28:37, 26:39, 50:55] = 900.0

        result = needle_planning.recommend_entry_point(
            self.volume, self.spacing, tip_ijk=(25, 32, 32),
            max_length_mm=80.0, needle_radius_mm=0.8,
            azimuth_step_degrees=30, elevations_degrees=(0,))

        self.assertIsNotNone(result)
        self.assertFalse(result["analysis"]["collision"])
        self.assertFalse(result["analysis"]["too_long"])
        self.assertGreaterEqual(result["valid_candidate_count"], 1)
        self.assertGreaterEqual(result["analysis"]["path_length_mm"], 15.0)

    def test_connecting_entry_returns_crosshair_to_tip(self):
        from ctto3d.viewer import VolumeViewer

        entry = (10.0, 20.0, 30.0)
        tip = (40.0, 50.0, 60.0)

        class ViewerStub:
            _planning_points = {"entry": entry, "tip": tip}

            def set_ablation_needle(self, selected_entry, selected_tip):
                self.connected = (selected_entry, selected_tip)

            def _remove_planning_markers(self):
                self.markers_removed = True

            def set_crosshair_ijk(self, ijk):
                self.crosshair = ijk

        viewer = ViewerStub()
        connected = VolumeViewer.connect_planning_points(viewer)

        self.assertTrue(connected)
        self.assertEqual(viewer.connected, (entry, tip))
        self.assertEqual(viewer.crosshair, tip)


if __name__ == "__main__":
    unittest.main()
