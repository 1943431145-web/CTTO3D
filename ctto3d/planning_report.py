"""规划核对单（HTML）生成。

纯函数模块，不依赖 Qt/VTK，便于离线测试：mainwindow 收集数据、抓取
截图后调 build_html 得到单文件 HTML（截图内嵌 base64），可直接用浏览器
打开或打印为 PDF。

build_html(data) 的 data 结构（缺的键/None 一律显示为"—"）：

    generated_at  str  生成时间
    patient       dict name / id / study_date / series
    nodule        None | dict index / total / volume_ml / depth_mm
    planning      None | dict entry_world / tip_world（3 元组 mm）、
                        length_mm、angles（3 元组度）
    needle        dict preset / diameter_mm / shaft_mm / active_mm
    zone          None | dict half_long_mm / half_short_mm / volume_ml
    images        dict 名称 → PNG 字节（view3d / axial / coronal / sagittal）
"""
import base64
import html as html_module


def _escape(value):
    return html_module.escape("" if value is None else str(value), quote=True)


def _fmt(value, pattern="%s"):
    return "—" if value is None else pattern % value


def _fmt_tuple(value, unit=""):
    if value is None:
        return "—"
    return "(" + ", ".join("%.1f" % float(v) for v in value) + ")" + unit


def _image_tag(png_bytes, title):
    if not png_bytes:
        return ""
    encoded = base64.b64encode(bytes(png_bytes)).decode("ascii")
    return (
        '<figure><img src="data:image/png;base64,%s" alt="%s"/>'
        "<figcaption>%s</figcaption></figure>" % (
            encoded, _escape(title), _escape(title))
    )


def _kv_table(rows):
    cells = "".join(
        "<tr><th>%s</th><td>%s</td></tr>" % (_escape(key), value)
        for key, value in rows)
    return "<table>%s</table>" % cells


# 注意：CSS 里含大量百分号，只能作为 %s 参数传入，不能放进 %-格式化模板。
_STYLE = (
    "<style>"
    "body{font-family:'Microsoft YaHei',SimSun,sans-serif;margin:24px;"
    "color:#1f2937;}"
    "h1{font-size:20px;border-bottom:2px solid #0e7490;padding-bottom:6px;}"
    "h2{font-size:15px;color:#0e7490;margin:18px 0 6px;}"
    "table{border-collapse:collapse;width:100%;margin-bottom:8px;}"
    "th,td{border:1px solid #d1d5db;padding:5px 10px;text-align:left;"
    "font-size:13px;}"
    "th{width:200px;background:#f3f4f6;font-weight:600;}"
    ".figures{display:grid;grid-template-columns:1fr 1fr;gap:10px;}"
    "figure{margin:0;} img{width:100%;border:1px solid #d1d5db;}"
    "figcaption{font-size:12px;color:#6b7280;padding:2px 0 6px;}"
    ".foot{margin-top:18px;font-size:12px;color:#6b7280;"
    "border-top:1px solid #d1d5db;padding-top:8px;}"
    "@media print{body{margin:8mm;}}"
    "</style>")


def build_html(data):
    data = data or {}
    patient = data.get("patient") or {}
    nodule = data.get("nodule")
    planning = data.get("planning")
    needle = data.get("needle") or {}
    zone = data.get("zone")
    images = data.get("images") or {}

    patient_rows = [
        ("姓名 / 编号", "%s / %s" % (
            _escape(patient.get("name")) or "—",
            _escape(patient.get("id")) or "—")),
        ("检查日期", _escape(patient.get("study_date")) or "—"),
        ("序列", _escape(patient.get("series")) or "—"),
    ]

    nodule_rows = [
        ("定位结节", _fmt(
            None if nodule is None else "第 %d / %d 个" % (
                nodule.get("index"), nodule.get("total")))),
        ("结节体积", _fmt(
            None if nodule is None or nodule.get("volume_ml") is None
            else "%.2f ml" % nodule["volume_ml"])),
        ("距体表深度", _fmt(
            None if nodule is None or nodule.get("depth_mm") is None
            else "%.1f mm" % nodule["depth_mm"])),
    ]

    planning_rows = [
        ("入针点（患者坐标 mm）", _fmt_tuple(
            None if planning is None else planning.get("entry_world"))),
        ("消融点（患者坐标 mm）", _fmt_tuple(
            None if planning is None else planning.get("tip_world"))),
        ("针道长度", _fmt(
            None if planning is None or planning.get("length_mm") is None
            else "%.1f mm" % planning["length_mm"])),
        ("进针方向角 X / Y / Z", _fmt(
            None if planning is None or planning.get("angles") is None
            else " / ".join("%.0f°" % a for a in planning["angles"]))),
    ]

    needle_rows = [
        ("针型", _escape(needle.get("preset")) or "—"),
        ("针径 / 针杆 / 活性端", _fmt(
            None if not needle.get("diameter_mm") else
            "%.1f / %.0f / %.1f mm" % (
                needle.get("diameter_mm", 0),
                needle.get("shaft_mm", 0),
                needle.get("active_mm", 0)))),
        ("消融区半轴（沿针 / 垂直）", _fmt(
            None if zone is None else "%.1f / %.1f mm" % (
                zone.get("half_long_mm", 0), zone.get("half_short_mm", 0)))),
        ("消融区体积", _fmt(
            None if zone is None or zone.get("volume_ml") is None
            else "%.1f ml" % zone["volume_ml"])),
    ]

    image_html = "".join(_image_tag(
        images.get(name), title)
        for name, title in (
            ("view3d", "三维视图（参考十字 = 定位/规划位置）"),
            ("axial", "轴状位"), ("coronal", "冠状位"), ("sagittal", "矢状位")))

    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<title>CTto3D 规划核对单</title>%s"
        "</head><body>"
        "<h1>CTto3D 消融规划核对单</h1>"
        "<p style=\"font-size:12px;color:#6b7280;\">生成时间：%s</p>"
        "<h2>患者信息</h2>%s"
        "<h2>结节定位</h2>%s"
        "<h2>针道规划</h2>%s"
        "<h2>消融针与消融区</h2>%s"
        "<h2>影像截图</h2><div class=\"figures\">%s</div>"
        "<p class=\"foot\">本核对单由 CTto3D 规划软件自动生成，仅供研究/"
        "术前参考；图像定位与针道须经执业医师复核后方可用于临床操作。</p>"
        "</body></html>" % (
            _STYLE,
            _escape(data.get("generated_at")) or "—",
            _kv_table(patient_rows), _kv_table(nodule_rows),
            _kv_table(planning_rows), _kv_table(needle_rows),
            image_html)
    )
