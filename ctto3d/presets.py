"""
组织分层预设与体渲染传递函数合成模块

============================================================
模块功能
============================================================
本模块定义了 CT 和通用图片的组织分层"预设（preset）"，
负责将用户选中的组织层合成为 VTK 体渲染的传递函数
（color transfer function + opacity transfer function）。

每个组织层是一个 HU 窗口（CT）或强度范围窗口（IMAGE），包含：
  - name    — 组织名称（UI 中显示）
  - opacity — 透明度分段函数 [(值, 透明度), ...]
  - color   — 颜色分段函数 [(值, R, G, B), ...]
  - iso     — 等值面阈值（用于 STL 模型导出）

工作原理：
  build_composite() 遍历所有组织层，将用户勾选（可见）的层的
  颜色和透明度曲线逐点合并到同一个传递函数中。
  未勾选的层对传递函数贡献为 0（完全透明）。

CT 组织层的 HU 值参考：
  https://en.wikipedia.org/wiki/Hounsfield_scale
  空气 −1000 · 肺 −700~−600 · 脂肪 −120~−90 · 水 0 ·
  血液 +13~+50 · 软组织 +35~+80 · 皮质骨 +700~+3000

修改方法：
  - 添加/修改 CT 组织层：编辑 CT_TISSUES 列表
  - 添加/修改通用图片组织层：编辑 IMAGE_TISSUES 列表
  - 修改组织颜色：调整对应层 color 列表中的 RGB 值（0~1）
  - 修改组织透明度：调整对应层 opacity 列表中的透明度值（0~1）
  - 修改全局光照：编辑 _LIGHT 字典
  - 修改等值面阈值（影响 STL 导出）：调整各层的 iso 值
============================================================
"""

import vtk

# ============================================================
# 全局光照参数
# 所有可见组织层共用的环境光/漫反射/镜面反射设置
# ============================================================
_LIGHT = {"ambient": 0.15, "diffuse": 0.9, "specular": 0.2, "power": 10}


# ============================================================
# CT 组织层预设（基于绝对 Hounsfield Units）
# ============================================================
# 注意事项：
#   1. 相邻组织的透明度曲线在边界处收回到 0，确保多选时合并自然
#   2. 修改颜色时，RGB 值范围为 0.0~1.0（对应 0~255），会影响体渲染显示效果
#   3. opacity 列表中的值也是 0.0~1.0，1.0 = 完全不透明
#   4. iso（等值面阈值）用于 STL 网格导出，数值越大越偏硬组织
#   5. 组织列表的顺序决定了渲染时的叠加顺序（后覆盖先）
#   6. 每个组织的颜色曲线需要至少 2 个点，透明度曲线同理
#   7. 重要：相邻层的不透明度窗口不可重叠。build_composite 把所有可见层
#      的控制点合并进同一条分段函数，重叠区会互相覆盖控制点（不是叠加），
#      所以每层都必须在进入下一层之前回到 0。颜色窗口允许跨层平滑过渡。
CT_TISSUES = [
    {
        "name": "肺实质",            # 充气肺实质（约 −900 ~ −600 HU）
        # 旧版从 −720 起，把最"空气感"的肺（−750 以下）整段排除了，
        # 导致肺看起来缺了大半。这里把下沿拉到 −950 补回充气肺。
        "opacity": [(-950, 0.0), (-900, 0.12), (-620, 0.12), (-600, 0.0)],
        "color": [(-900, 0.55, 0.68, 0.90), (-620, 0.70, 0.82, 0.96)],  # 浅蓝
        "iso": -750,
    },
    {
        "name": "肺纹理 / 支气管",    # 肺内血管纹理、支气管壁（约 −600 ~ −300 HU）
        "opacity": [(-600, 0.0), (-560, 0.16), (-330, 0.16), (-300, 0.0)],
        "color": [(-560, 0.46, 0.70, 0.86), (-330, 0.62, 0.80, 0.92)],  # 青蓝
        "iso": -450,
    },
    {
        "name": "脂肪",              # 脂肪组织（约 −120 ~ −60 HU）
        "opacity": [(-135, 0.0), (-115, 0.22), (-65, 0.22), (-55, 0.0)],
        "color": [(-115, 0.95, 0.86, 0.45), (-65, 0.97, 0.91, 0.60)],  # 暖黄
        "iso": -90,
    },
    {
        "name": "水 / 积液",          # 水样密度（脑脊液/囊肿/积液/水肿，约 0 HU）
        "opacity": [(-25, 0.0), (-8, 0.28), (14, 0.28), (24, 0.0)],
        "color": [(-8, 0.25, 0.72, 0.80), (14, 0.40, 0.84, 0.90)],    # 蓝青
        "iso": 5,
    },
    {
        "name": "肌肉",              # 肌肉（约 25 ~ 55 HU）
        "opacity": [(26, 0.0), (34, 0.18), (52, 0.18), (56, 0.0)],
        "color": [(34, 0.76, 0.38, 0.34), (52, 0.88, 0.52, 0.46)],    # 红棕
        "iso": 42,
    },
    {
        "name": "软组织 / 腺体",      # 较致密软组织、腺体、凝血（约 60 ~ 95 HU）
        "opacity": [(58, 0.0), (68, 0.16), (92, 0.16), (98, 0.0)],
        "color": [(68, 0.86, 0.56, 0.54), (92, 0.93, 0.68, 0.64)],    # 粉红
        "iso": 80,
    },
    {
        "name": "器官实质",           # 增强实质脏器（肝/脾/肾等，约 100 ~ 155 HU）
        "opacity": [(100, 0.0), (110, 0.26), (150, 0.26), (160, 0.0)],
        "color": [(110, 0.85, 0.52, 0.28), (150, 0.93, 0.68, 0.40)],  # 橙色
        "iso": 125,
    },
    {
        "name": "血管 / 造影",        # 造影增强血管（约 160 ~ 345 HU，亮红）
        "opacity": [(162, 0.0), (182, 0.55), (318, 0.55), (344, 0.0)],
        "color": [(182, 0.92, 0.14, 0.11), (318, 0.99, 0.42, 0.30)],  # 红色
        "iso": 240,
    },
    {
        "name": "松质骨 / 钙化",      # 松质骨、钙化灶（约 345 ~ 600 HU，淡棕）
        "opacity": [(346, 0.0), (390, 0.58), (560, 0.74), (600, 0.0)],
        "color": [(390, 0.82, 0.74, 0.58), (560, 0.92, 0.86, 0.72)],  # 淡棕
        "iso": 430,
    },
    {
        "name": "皮质骨",            # 致密皮质骨（600+ HU，象牙白）
        "opacity": [(600, 0.0), (660, 0.70), (900, 0.92), (3071, 0.92)],
        "color": [(660, 0.90, 0.84, 0.72), (900, 0.96, 0.92, 0.84),
                  (1600, 1, 1, 1), (3071, 1, 1, 1)],                  # 象牙白
        "iso": 700,
    },
]


# ============================================================
# 通用图片强度分层（用于非 CT 模态，如普通图片/MR 序列）
# ============================================================
# 这里使用的是归一化强度值 0.0~1.0（相对于数据范围），
# 实际渲染时通过 _resolve() 映射到真实数据范围。
# 修改方法同 CT_TISSUES，但 value 范围为 0.0~1.0。
IMAGE_TISSUES = [
    {
        "name": "低密度",
        "opacity": [(0.0, 0.0), (0.05, 0.10), (0.38, 0.10), (0.40, 0.0)],
        "color": [(0.05, 0.40, 0.55, 0.85), (0.38, 0.45, 0.62, 0.90)],
        "iso": 0.20,
    },
    {
        "name": "中密度",
        "opacity": [(0.40, 0.0), (0.45, 0.30), (0.70, 0.30), (0.72, 0.0)],
        "color": [(0.45, 0.85, 0.60, 0.30), (0.70, 0.90, 0.70, 0.40)],
        "iso": 0.55,
    },
    {
        "name": "高密度",
        "opacity": [(0.72, 0.0), (0.78, 0.75), (1.0, 0.75)],
        "color": [(0.78, 0.95, 0.95, 0.95), (1.0, 1, 1, 1)],
        "iso": 0.82,
    },
]


def _tissues(modality):
    """根据模态返回对应的组织预设列表（CT 或 IMAGE）。"""
    return IMAGE_TISSUES if modality == "IMAGE" else CT_TISSUES


def tissue_names(modality):
    """获取指定模态下所有组织层的名称列表，供 UI 显示。
    
    参数：
      modality — "CT"、"MR" 或 "IMAGE"
    
    返回：
      组织名称字符串列表
    """
    return [t["name"] for t in _tissues(modality)]


def _resolve(points, modality, scalar_range):
    """将 IMAGE 模式的归一化值（0~1）映射到真实数据范围。
    CT 模式不需要映射，直接返回原值。
    
    参数：
      points       — 传递函数控制点列表 [(值, ...), ...]
      modality     — "CT"/"MR"（不处理）或 "IMAGE"（需要映射）
      scalar_range — (min, max) 数据真实范围
    
    返回：
      映射后的控制点列表（CT 模式不变）
    """
    if modality != "IMAGE":
        return points
    lo, hi = scalar_range
    span = (hi - lo) or 1.0
    return [(lo + p[0] * span,) + tuple(p[1:]) for p in points]


def tissue_color(name, modality):
    """获取指定组织的代表颜色 (R, G, B)，用于 UI 中的颜色色块显示。
    
    颜色取自该组织的最后一个颜色控制点。
    
    参数：
      name     — 组织名称
      modality — 数据模态
    
    返回：
      (r, g, b) 元组，各分量 0~1
    """
    for t in _tissues(modality):
        if t["name"] == name:
            last = t["color"][-1]
            return (last[1], last[2], last[3])
    return (0.8, 0.8, 0.8)  # 默认灰色（找不到匹配时）


def build_composite(tissue_states, modality, scalar_range, global_scale=1.0):
    """将可见的组织层合并为 VTK 颜色和透明度传递函数。
    
    这是整个渲染管线的核心函数。它遍历所有组织层，将用户勾选的
    （可见的）层的颜色和透明度曲线逐点合并到同一个传递函数中。
    每个层的透明度 = 基础透明度 × 用户滑动条系数 × 总量系数。
    
    参数：
      tissue_states — {组织名: 透明度系数} 字典，或包含名称的可迭代对象
                     系数范围 0.0~1.0（1.0 = 完全不透明）
      modality      — 数据模态字符串 "CT"/"MR"/"IMAGE"
      scalar_range  — 数据数值范围 (min, max)
      global_scale  — 总量的透明度缩放（主面板的不透明/透明控制），0~1
    
    返回：
      (color_tf, opacity_tf, light_dict, shade_bool) 四元组
      - color_tf:    vtkColorTransferFunction 颜色传递函数
      - opacity_tf:  vtkPiecewiseFunction 透明度传递函数
      - light_dict:  光照参数字典（ambient/diffuse/specular/power）
      - shade_bool:  是否启用着色（始终为 True）
    """
    if not isinstance(tissue_states, dict):
        tissue_states = {n: 1.0 for n in tissue_states}
    g = max(0.0, min(1.0, global_scale))

    color_tf = vtk.vtkColorTransferFunction()
    opacity_tf = vtk.vtkPiecewiseFunction()

    added = False
    for t in _tissues(modality):
        factor = tissue_states.get(t["name"])
        if factor is None:  # 未勾选的组织层跳过
            continue
        added = True
        # 添加颜色控制点
        for v, r, g_, b in _resolve(t["color"], modality, scalar_range):
            color_tf.AddRGBPoint(v, r, g_, b)
        # 添加透明度控制点（= 基础透明度 × 用户系数 × 总量系数）
        for v, a in _resolve(t["opacity"], modality, scalar_range):
            opacity_tf.AddPoint(v, a * max(0.0, min(1.0, factor)) * g)

    # 如果没有任何组织被勾选，返回空传递函数（全透明）
    if not added:
        lo, hi = scalar_range
        opacity_tf.AddPoint(lo, 0.0)
        opacity_tf.AddPoint(hi, 0.0)
        color_tf.AddRGBPoint(lo, 0, 0, 0)
        color_tf.AddRGBPoint(hi, 0, 0, 0)

    return color_tf, opacity_tf, dict(_LIGHT), True


def composite_threshold(checked_names, modality, scalar_range):
    """计算等值面阈值（用于 STL/OBJ/PLY 网格导出）。
    
    取用户当前勾选的组织中密度最高的那个，使用其 iso 值作为
    等值面提取阈值。这样导出的模型能反映用户当前关注的解剖结构。
    
    参数：
      checked_names — 用户已勾选的组织名称集合/列表
      modality      — 数据模态
      scalar_range  — 数据数值范围
    
    返回：
      等值面阈值（浮点数）
    """
    checked = set(checked_names)
    chosen = None
    # 遍历组织列表（后面的通常是密度更高的），取最后一个勾选的高密度组织
    for t in _tissues(modality):
        if t["name"] in checked:
            chosen = t
    if chosen is None:
        # 没有任何组织被勾选 → 使用数据范围中值
        lo, hi = scalar_range
        return (lo + hi) / 2.0
    iso = chosen["iso"]
    # IMAGE 模式需要将归一化阈值映射到真实数据范围
    if modality == "IMAGE":
        lo, hi = scalar_range
        iso = lo + iso * ((hi - lo) or 1.0)
    return iso
