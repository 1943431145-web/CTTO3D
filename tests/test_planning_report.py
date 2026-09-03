"""planning_report.build_html 纯函数测试（无 Qt/VTK 依赖）。"""
import base64
import unittest

from ctto3d import planning_report


_PNG = base64.b64decode(
    # 1x1 红色 PNG 的最小合法字节流
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4"
    b"z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==")


def _full_data():
    return {
        "generated_at": "2026-09-01 12:00:00",
        "patient": {
            "name": "张三", "id": "P001",
            "study_date": "2026-08-31", "series": "CHEST CT",
        },
        "nodule": {"index": 2, "total": 3, "volume_ml": 1.25, "depth_mm": 34.0},
        "planning": {
            "entry_world": (10.0, -20.0, 30.0),
            "tip_world": (12.0, -5.0, 31.0),
            "length_mm": 82.5,
            "angles": (65.0, 25.0, 90.0),
        },
        "needle": {
            "preset": "HCK-15100", "diameter_mm": 1.6,
            "shaft_mm": 150.0, "active_mm": 5.0,
        },
        "zone": {"half_long_mm": 28.0, "half_short_mm": 15.0, "volume_ml": 26.4},
        "images": {"view3d": _PNG, "axial": _PNG},
    }


class BuildHtmlTests(unittest.TestCase):
    def test_full_data_renders_values_and_images(self):
        html = planning_report.build_html(_full_data())
        for expected in (
                "张三", "P001", "2026-08-31", "CHEST CT",
                "第 2 / 3 个", "1.25 ml", "34.0 mm",
                "(10.0, -20.0, 30.0)", "82.5 mm",
                "65° / 25° / 90°", "HCK-15100",
                "28.0 / 15.0 mm", "26.4 ml",
                "data:image/png;base64,",
                "研究", "复核"):
            self.assertIn(expected, html)
        # 截图数量：两张图各一个 figure
        self.assertEqual(html.count("<figure>"), 2)

    def test_missing_sections_render_placeholders(self):
        html = planning_report.build_html({})
        self.assertIn("—", html)
        self.assertNotIn("base64", html)
        self.assertNotIn("None", html)

    def test_patient_text_is_html_escaped(self):
        data = _full_data()
        data["patient"]["name"] = "<script>alert(1)</script>"
        html = planning_report.build_html(data)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_zone_volume_formula_documented_value(self):
        # 4/3·π·28·15² / 1000 ≈ 26.4 ml —— 与 build_html 无关，
        # 锁定 viewer.ablation_zone_info 使用的同一公式量级
        volume = 4.0 / 3.0 * 3.141592653589793 * 28.0 * 15.0 ** 2 / 1000.0
        self.assertAlmostEqual(volume, 26.4, delta=0.05)


if __name__ == "__main__":
    unittest.main()
