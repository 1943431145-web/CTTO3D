"""
消融针预设与消融范围生长模型

============================================================
模块功能
============================================================
1. 消融针型号预设 — 定义多款临床常用射频消融针的参数
2. 消融范围生长模型 — 基于简化物理模型计算消融椭球体半径随时间增长

消融针预设参数说明：
  name         — 型号名称（显示在下拉菜单中）
  diameter_mm  — 针直径（mm）
  active_mm    — 活性端（裸露金属端）长度（mm），射频能量从此段发出
  shaft_mm     — 针杆总长度（mm）
  power_w      — 建议射频功率（W），用于仿真默认值

消融范围生长模型说明：
  采用简化指数饱和模型，仅用于术前规划可视化，非临床剂量验证：
    r(t) = R_inf(P) × (1 − e^(−t/τ))
  其中：
    R_inf(P) = R_base + k × √P   （稳态短轴半径，与功率平方根成正比）
    τ = ZONE_TAU_S                （生长时间常数，默认 150 秒）
    长轴 = R_long = r_short × ELONGATION + 0.5 × active_mm

修改方法：
  - 添加新针型：在 NEEDLE_PRESETS 列表中添加新字典
  - 调整仿真速度：修改 ZONE_TAU_S（越大越慢）
  - 调整消融范围大小：修改 ZONE_R_BASE_MM 和 ZONE_R_PER_SQRT_W
  - 调整椭球形状：修改 ZONE_ELONGATION（长短轴比）
============================================================
"""

import math


# ============================================================
# 消融针型号预设表
# 添加新针型只需在此列表中追加一个字典即可
# ============================================================
NEEDLE_PRESETS = [
    {
        "name": "自定义",
        "diameter_mm": 1.6,
        "active_mm": 5.0,
        "shaft_mm": 150.0,
        "power_w": 30.0,
    },
    {
        "name": "DW-XR-I3 甲状腺细针",
        "diameter_mm": 1.4,
        "active_mm": 3.0,
        "shaft_mm": 100.0,
        "power_w": 50.0,
    },
    {
        "name": "DW-XR-I6 小病灶针",
        "diameter_mm": 1.6,
        "active_mm": 5.0,
        "shaft_mm": 150.0,
        "power_w": 80.0,
    },
    {
        "name": "DW-XR-I10 水冷针",
        "diameter_mm": 2.0,
        "active_mm": 11.0,
        "shaft_mm": 180.0,
        "power_w": 80.0,
    },
    {
        "name": "DW-XR-II 大针",
        "diameter_mm": 2.2,
        "active_mm": 5.0,
        "shaft_mm": 300.0,
        "power_w": 80.0,
    },
]


def preset_names():
    """获取所有消融针预设的名称列表，供 UI 下拉菜单使用。"""
    return [item["name"] for item in NEEDLE_PRESETS]


def preset_by_name(name):
    """根据名称查找消融针预设，返回参数字典副本。
    若未找到匹配名称，返回第一个预设（"自定义"）作为默认值。"""
    for item in NEEDLE_PRESETS:
        if item["name"] == name:
            return dict(item)
    return dict(NEEDLE_PRESETS[0])


# ============================================================
# 消融范围生长模型 — 简化指数饱和模型
# 仅用于术前规划可视化，非临床验证模型
# ============================================================
#
# 模型公式：
#   短轴半径 r_short(t) = R_inf(P) × (1 − e^(−t / τ))
#   其中 R_inf(P) = R_base + k × √P  （稳态平台半径）
#   长轴半径 r_long = r_short × ELONGATION + 0.5 × active_mm
#
# 若要修改仿真行为，调整以下常量即可：

ZONE_TAU_S = 150.0            # 生长时间常数（秒），数值越大生长越慢
ZONE_R_BASE_MM = 3.5         # 基础稳态短轴半径（mm），功率=0时的最小半径
ZONE_R_PER_SQRT_W = 1.7      # 功率增益系数，短轴半径增量 ∝ √功率
ZONE_R_MAX_MM = 28.0         # 稳态短轴半径的硬上限（mm）
ZONE_ELONGATION = 1.25       # 生长区长短轴比（>1 表示沿针方向略长）
ZONE_MIN_HALF_MM = 0.5       # 最小半轴长（mm），防止 t=0 时椭球退化为0


def zone_plateau_short_radius_mm(power_w):
    """计算给定功率下的稳态（t→∞）消融短轴半径。
    
    参数：
      power_w: 射频功率（瓦特）
    
    返回：
      稳态短轴半径（mm），受 ZONE_R_MAX_MM 上限约束
    """
    r = ZONE_R_BASE_MM + ZONE_R_PER_SQRT_W * math.sqrt(max(0.0, float(power_w)))
    return max(0.0, min(ZONE_R_MAX_MM, r))


def ablation_zone_half_axes_mm(power_w, active_mm, elapsed_s):
    """计算消融椭球体的半轴长度（半长轴, 半短轴）。
    
    半长轴沿消融针方向延伸，半短轴垂直于针方向。
    
    参数：
      power_w   — 射频功率（瓦特）
      active_mm — 活性端长度（mm），长轴在此基础上延伸
      elapsed_s — 已消融时间（秒），从 0 开始增长
    
    返回：
      (half_long, half_short) 元组，单位为 mm
      - half_long:  沿针方向的半轴长（含活性端贡献）
      - half_short: 垂直于针方向的半轴长
    """
    elapsed_s = max(0.0, float(elapsed_s))
    r_inf = zone_plateau_short_radius_mm(power_w)
    # 短轴按指数饱和曲线增长
    short = r_inf * (1.0 - math.exp(-elapsed_s / ZONE_TAU_S))
    # 长轴 = 生长出的部分 × 伸长比 + 活性端长度的一半
    half_long = short * ZONE_ELONGATION + 0.5 * max(0.0, float(active_mm))
    half_short = short
    # 确保半轴不小于最小值，防止椭球退化为点
    return max(ZONE_MIN_HALF_MM, half_long), max(ZONE_MIN_HALF_MM, half_short)
