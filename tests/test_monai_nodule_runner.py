import importlib.util
import unittest

import numpy as np

from ctto3d import monai_nodule_runner as runner


SCIPY_AVAILABLE = importlib.util.find_spec("scipy") is not None


class MonaiNoduleRunnerTests(unittest.TestCase):
    def test_precision_tuned_defaults(self):
        self.assertEqual(runner.DEFAULT_THRESHOLD, 0.60)
        self.assertEqual(runner.DEFAULT_SMOOTH_SIGMA, 0.5)
        self.assertEqual(runner.DEFAULT_OPENING_RADIUS, 1)
        self.assertEqual(runner.DEFAULT_MIN_VOXELS_256, 2)
        self.assertEqual(runner.DEFAULT_MIN_VOXELS_NATIVE, 5)

    def test_gui_preset_matches_runner_defaults(self):
        from ctto3d import segmentation

        preset = next(
            value for value in segmentation.SEGMENTATION_PRESETS.values()
            if value.get("engine") == "monai_nodule")
        self.assertEqual(preset["monai_threshold"], runner.DEFAULT_THRESHOLD)
        self.assertEqual(
            preset["monai_smooth_sigma"], runner.DEFAULT_SMOOTH_SIGMA)
        self.assertEqual(
            preset["monai_opening"], runner.DEFAULT_OPENING_RADIUS)
        self.assertEqual(
            preset["monai_min_voxels_256"], runner.DEFAULT_MIN_VOXELS_256)
        self.assertEqual(
            preset["monai_min_voxels_native"],
            runner.DEFAULT_MIN_VOXELS_NATIVE)

    @unittest.skipUnless(SCIPY_AVAILABLE, "MONAI runtime dependency is unavailable")
    def test_opening_removes_isolated_probability_speck(self):
        probability = np.zeros((9, 9, 9), dtype=np.float32)
        probability[3:6, 3:6, 3:6] = 1.0
        probability[1, 1, 1] = 1.0

        mask, _ = runner._postprocess_probability(
            probability, threshold=0.60, smooth_sigma=0.0,
            opening_radius=1)

        self.assertEqual(mask[4, 4, 4], 1)
        self.assertEqual(mask[1, 1, 1], 0)

    @unittest.skipUnless(SCIPY_AVAILABLE, "MONAI runtime dependency is unavailable")
    def test_component_filter_keeps_only_configured_minimum(self):
        mask = np.zeros((6, 6, 6), dtype=np.uint8)
        mask[0, 0, 0] = 1
        mask[3:5, 3:5, 3:5] = 1

        filtered = runner._remove_tiny_components(mask, minimum_voxels=5)

        self.assertEqual(int(filtered.sum()), 8)
        self.assertEqual(filtered[0, 0, 0], 0)

    def test_normalise_ct_maps_window_and_replaces_non_finite(self):
        raw = np.array(
            [-1200.0, -1000.0, -300.0, 400.0, 900.0, np.nan, np.inf, -np.inf],
            dtype=np.float32)

        out = runner._normalise_ct(raw)

        self.assertEqual(out.dtype, np.float32)
        self.assertTrue((out >= 0.0).all() and (out <= 1.0).all())
        # nan/±inf 分别按 -1000/+400/-1000 替换后再窗口化
        expected = [0.0, 0.0, 700.0 / 1400.0, 1.0, 1.0, 0.0, 1.0, 0.0]
        for got, want in zip(out.tolist(), expected):
            self.assertAlmostEqual(got, want, places=6)

    def test_normalise_ct_reuses_float32_input_without_copy(self):
        raw = np.zeros((2, 2, 2), dtype=np.float32)
        raw[0, 0, 0] = 400.0

        out = runner._normalise_ct(raw)

        self.assertIs(out, raw)              # float32 输入零拷贝、原地修改
        self.assertEqual(out[0, 0, 0], 1.0)
        self.assertEqual(out[1, 1, 1], 1000.0 / 1400.0)


if __name__ == "__main__":
    unittest.main()
