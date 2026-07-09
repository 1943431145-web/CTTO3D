"""
ctto3d 包 - 消融手术规划系统核心模块

============================================================
模块清单
============================================================
  ablation.py    — 消融针型号预设 + 消融范围生长模型（椭球体）
  loader.py      — 数据加载（DICOM/图片序列/ZIP/演示体模）
  logsetup.py    — 日志系统配置（滚动日志文件 + 控制台 + 崩溃捕获）
  mainwindow.py  — 主窗口 UI 和控制面板（组织选择/显示效果/消融针/仿真）
  presets.py     — 组织分层预设 + 体渲染传递函数合成
  style.py       — 深色/浅色主题样式表（QSS）和调色板
  viewer.py      — VTK 三维体渲染视图 + 三向正交切片视图
  assets/        — 存放主题所需的箭头图标等静态资源（PNG）

修改主题样式：
  - 颜色变量在 style.py 的 DARK 和 LIGHT 字典中定义
  - QSS 样式模板在 style.py 的 _base_stylesheet() 和 _input_stylesheet() 中
  - 如需更换品牌色，修改 styly.py 开头的 ACCENT 变量（#0E9F9B）

修改组织分层/颜色：
  - CT 组织在 presets.py 的 CT_TISSUES 列表中定义
  - 通用图片序列组织在 presets.py 的 IMAGE_TISSUES 中定义
  - 每个组织包含 name（名称）、opacity（透明度曲线）、color（颜色曲线）、iso（等值面阈值）

修改消融针预设：
  - 在 ablation.py 的 NEEDLE_PRESETS 列表中添加/修改型号
  - 每款针包含：名称、直径(mm)、活性端长度(mm)、针杆长度(mm)、建议功率(W)

修改消融仿真模型参数：
  - 修改 ablation.py 中的 ZONE_TAU_S（时间常数）、ZONE_R_BASE_MM（基础半径）、
    ZONE_R_PER_SQRT_W（功率增益系数）、ZONE_ELONGATION（长短轴比）

修改日志行为：
  - logsetup.py 开头可修改 LOG_FILENAME（日志文件名）、_MAX_BYTES（单文件大小上限）、
    _BACKUP_COUNT（保留份数）
============================================================
"""

__version__ = "1.0.0"
