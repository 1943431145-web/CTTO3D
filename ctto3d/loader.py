"""
数据加载模块 — DICOM序列、图片序列、ZIP压缩包和内置演示体模

============================================================
模块功能
============================================================
负责从各种数据源读取医学影像或图片序列，转换为 VTK 格式的
三维体数据（vtkImageData），供 VolumeViewer 渲染使用。

支持的数据源：
  1. DICOM 文件夹 — load_dicom_series(directory)
     自动识别 SeriesInstanceUID 并选取主序列，排除定位像/剂量报告
     输出 HU 校准后的 CT/MR 体数据

  2. 图片序列文件夹 — load_image_stack(directory)
     按自然排序读取 PNG/JPG/TIFF/BMP 图片，堆叠为体数据

  3. ZIP 压缩包 — load_zip(zip_path)
     解压到临时目录后自动检测内容类型（DICOM 优先，回退到图片）

  4. 自动检测 — load_folder_auto(directory)
     先尝试 DICOM，失败则作为图片序列加载

  5. 演示体模 — make_demo_phantom(n=128)
     生成一个含骨骼/肺部/血管/肌肉/脂肪的合成胸部 CT

每个加载器返回 (vtkImageData, info_dict)，其中 info 字典包含：
  modality — "CT"、"MR" 或 "IMAGE"
  scalar_range — (min, max) 数值范围
  dimensions — (x, y, z) 体素尺寸

修改方法：
  - 添加新数据格式支持：新增加载函数，返回 (image, info) 元组
  - 调整体模形状：修改 make_demo_phantom() 中的各 ellipsoid 参数
  - 修改默认层间距：调整 load_image_stack() 的 slice_spacing 默认值
============================================================
"""

import os
import re
import shutil
import tempfile
import zipfile

import numpy as np
import vtk
from vtk.util import numpy_support

# 支持的图片文件扩展名（全部小写比较）
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")


def _natural_key(s):
    """自然排序键函数：让 "slice2" 排在 "slice10" 前面（按人类阅读习惯排序）。
    
    将字符串中的数字片段转为整数参与比较，非数字部分按小写字符串比较。
    例如: ["slice10", "slice2"] → ["slice2", "slice10"]
    """
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _numpy_to_vtk(arr, spacing, modality):
    """将 (z, y, x) float32 的 numpy 数组转换为 vtkImageData。
    
    注意：numpy 的 C-order 存储中，x 维度在内存中连续变化，
    这与 vtkImageData 期望的点顺序一致，无需转置。
    
    参数：
      arr     — numpy 数组，形状 (z, y, x)，dtype=float32
      spacing — (sx, sy, sz) 体素间距，单位 mm
      modality — 模态标签 ("CT" / "MR" / "IMAGE")
    
    返回：
      (vtkImageData, info_dict) 元组
    """
    arr = np.ascontiguousarray(arr, dtype=np.float32)
    z, y, x = arr.shape
    # 将 numpy 数组展平为 VTK 可识别的线性存储
    flat = numpy_support.numpy_to_vtk(arr.ravel(order="C"), deep=True,
                                      array_type=vtk.VTK_FLOAT)
    img = vtk.vtkImageData()
    img.SetDimensions(x, y, z)
    img.SetSpacing(float(spacing[0]), float(spacing[1]), float(spacing[2]))
    img.GetPointData().SetScalars(flat)
    info = {"modality": modality,
            "scalar_range": (float(arr.min()), float(arr.max())),
            "dimensions": (x, y, z)}
    return img, info


def _round_tuple(values, ndigits=4):
    """将可迭代对象各元素舍入到指定位数，返回元组。"""
    return tuple(round(float(v), ndigits) for v in values)


# ============================================================
# DICOM 辅助函数
# ============================================================

def _dicom_orientation_normal(ds):
    """从 DICOM 数据集中提取切片法向量（ImageOrientationPatient 叉积）。
    若无法提取返回 None。
    """
    iop = getattr(ds, "ImageOrientationPatient", None)
    if iop is None or len(iop) != 6:
        return None
    row = np.asarray([float(v) for v in iop[:3]], dtype=np.float64)
    col = np.asarray([float(v) for v in iop[3:]], dtype=np.float64)
    normal = np.cross(row, col)
    norm = np.linalg.norm(normal)
    if norm <= 0:
        return None
    return normal / norm


def _dicom_slice_position(ds):
    """获取 DICOM 切片沿法向方向的排序位置。
    优先使用 ImagePositionPatient，回退到 InstanceNumber。
    """
    ipp = getattr(ds, "ImagePositionPatient", None)
    if ipp is not None and len(ipp) == 3:
        normal = _dicom_orientation_normal(ds)
        pos = np.asarray([float(v) for v in ipp], dtype=np.float64)
        if normal is not None:
            return float(np.dot(pos, normal))
        return float(pos[2])
    return float(getattr(ds, "InstanceNumber", 0) or 0)


def _dicom_series_key(ds):
    """生成 DICOM 序列分组的键：基于 UID + 图像尺寸 + 像素间距 + 方向。
    确保不同方位/尺寸的序列不会被错误合并到同一体数据中。
    """
    uid = str(getattr(ds, "SeriesInstanceUID", "") or "")
    if not uid:
        # 如果没有 SeriesInstanceUID，用 StudyUID + SeriesNumber 作为备用键
        uid = "%s:%s" % (
            getattr(ds, "StudyInstanceUID", "NO_STUDY"),
            getattr(ds, "SeriesNumber", "NO_SERIES"),
        )
    rows = int(getattr(ds, "Rows", 0) or 0)
    cols = int(getattr(ds, "Columns", 0) or 0)
    spacing = getattr(ds, "PixelSpacing", None) or [1.0, 1.0]
    orientation = getattr(ds, "ImageOrientationPatient", None) or []
    return (
        uid,
        rows,
        cols,
        _round_tuple(spacing),
        _round_tuple(orientation) if len(orientation) == 6 else (),
    )


def _dicom_group_score(items):
    """对 DICOM 序列组打分，分数越高越可能是有用的诊断序列。
    
    评分规则（从高到低优先级）：
      1. CT/MR 模态优先于其他（如 DR/SC 等）
      2. 排除定位像（LOCALIZER/SCOUT）
      3. 唯一切层位置数越多越好
      4. 总切层数越多越好
    """
    ds0 = items[0]
    modality = str(getattr(ds0, "Modality", "") or "").upper()
    image_type = "\\".join(str(v).upper() for v in getattr(ds0, "ImageType", []) or [])
    positions = [_dicom_slice_position(ds) for ds in items]
    unique_positions = len({round(p, 3) for p in positions})
    is_primary_ct_mr = 1 if modality in ("CT", "MR") else 0
    is_not_localizer = 0 if any(token in image_type for token in ("LOCALIZER", "SCOUT")) else 1
    return (is_primary_ct_mr, is_not_localizer, unique_positions, len(items))


def _select_dicom_series(slices):
    """从多个 DICOM 序列中自动选择最佳的诊断序列。
    
    病人 CD/ZIP 中常包含多个序列（定位像、剂量报告等），
    直接混合所有像素文件会导致三维重建严重畸变。
    本函数按 SeriesInstanceUID + 几何参数分组，选出分数最高的一组。
    """
    groups = {}
    for ds in slices:
        groups.setdefault(_dicom_series_key(ds), []).append(ds)
    return max(groups.values(), key=_dicom_group_score)


def load_dicom_series(directory):
    """从指定目录及其子目录加载一个完整的 DICOM 序列。
    
    自动完成：
      - 递归搜索所有含 PixelData 的 DICOM 文件
      - 分组并选择主诊断序列（排除定位像等）
      - 按切片位置排序
      - 应用 RescaleSlope/RescaleIntercept 转换为 HU 值
      - 从 DICOM 元数据提取体素间距
    
    参数：
      directory: DICOM 文件夹路径
    
    返回：
      (vtkImageData, info_dict) — 三维 CT/MR 体数据
    
    异常：
      ValueError: 目录中未找到有效的 DICOM 图像
    """
    import pydicom

    slices = []
    for root, _, names in os.walk(directory):
        for n in names:
            path = os.path.join(root, n)
            try:
                ds = pydicom.dcmread(path, force=True)
            except Exception:
                continue
            # 过滤：必须有像素数据和尺寸信息
            if "PixelData" in ds and getattr(ds, "Rows", None) and getattr(ds, "Columns", None):
                slices.append(ds)

    if not slices:
        raise ValueError("No DICOM images with pixel data were found in:\n%s" % directory)

    # 自动选择最佳序列
    slices = _select_dicom_series(slices)
    # 按切片位置（Z方向）排序
    slices.sort(key=_dicom_slice_position)
    ref = slices[0]
    rows, cols = int(ref.Rows), int(ref.Columns)

    # 排除尺寸不一致的切片
    slices = [s for s in slices if int(s.Rows) == rows and int(s.Columns) == cols]

    # 构建三维体数据数组
    vol = np.zeros((len(slices), rows, cols), dtype=np.float32)
    for i, ds in enumerate(slices):
        px = ds.pixel_array.astype(np.float32)
        # 应用 DICOM 的线性转换：HU = pixel × slope + intercept
        slope = float(getattr(ds, "RescaleSlope", 1.0) or 1.0)
        intercept = float(getattr(ds, "RescaleIntercept", 0.0) or 0.0)
        vol[i] = px * slope + intercept

    # 提取像素间距
    ps = getattr(ref, "PixelSpacing", None) or [1.0, 1.0]
    sx, sy = float(ps[1]), float(ps[0])  # 注意：PixelSpacing = [行距, 列距]
    sz = float(getattr(ref, "SpacingBetweenSlices", 0) or 0)
    if sz <= 0:
        sz = float(getattr(ref, "SliceThickness", 0) or 0)

    # 从实际切片位置估算 Z 间距（比元数据更可靠）
    positions = [_dicom_slice_position(s) for s in slices]
    diffs = [abs(b - a) for a, b in zip(positions, positions[1:]) if abs(b - a) > 1e-4]
    if diffs:
        sz = float(np.median(diffs))  # 用中位数抗异常
    if sz <= 0:
        sz = 1.0

    # 规范化模态标签
    modality = str(getattr(ref, "Modality", "CT") or "CT").upper()
    if modality not in ("CT", "MR"):
        modality = "CT"
    return _numpy_to_vtk(vol, (sx, sy, sz), modality)


# ============================================================
# 图片序列加载
# ============================================================

def _gather_images(directory):
    """递归搜索目录及其子目录中的图片文件。
    返回文件数量最多的那个子文件夹中的图片列表（自然排序）。
    这样即使序列放在子文件夹中也能正确识别。
    """
    groups = {}
    for root, _, names in os.walk(directory):
        imgs = [os.path.join(root, n) for n in names
                if n.lower().endswith(IMAGE_EXTS)]
        if imgs:
            groups[root] = imgs
    if not groups:
        return []
    # 选择文件最多的文件夹
    best = max(groups.values(), key=len)
    # 自然排序：slice2.png 在 slice10.png 之前
    best.sort(key=lambda p: _natural_key(os.path.basename(p)))
    return best


def load_image_stack(directory, slice_spacing=1.0, pixel_spacing=1.0):
    """从文件夹加载一组有序的 PNG/JPG/TIFF/BMP 切片作为三维体数据。
    
    参数：
      directory      — 包含图片序列的文件夹路径
      slice_spacing  — Z 方向的层间距（mm），默认 1.0
      pixel_spacing  — X/Y 方向的像素间距（mm），默认 1.0
    
    返回：
      (vtkImageData, info_dict) — 三维体数据（modality="IMAGE"）
    
    异常：
      ValueError: 目录中未找到支持的图片文件
    """
    from PIL import Image

    files = _gather_images(directory)
    if not files:
        raise ValueError("No image files (%s) found in:\n%s"
                         % (", ".join(IMAGE_EXTS), directory))

    # 读取第一张图片确定尺寸
    first = np.asarray(Image.open(files[0]).convert("L"), dtype=np.float32)
    h, w = first.shape
    planes = [first]
    # 逐一读取剩余图片，只接受尺寸匹配的
    for f in files[1:]:
        a = np.asarray(Image.open(f).convert("L"), dtype=np.float32)
        if a.shape == (h, w):
            planes.append(a)

    # 沿 Z 轴堆叠
    vol = np.stack(planes, axis=0)
    return _numpy_to_vtk(vol, (pixel_spacing, pixel_spacing, slice_spacing), "IMAGE")


def load_folder_auto(directory):
    """自动检测文件夹内容类型：先尝试 DICOM，失败则作为图片序列加载。
    
    这是一个便捷函数，用于 ZIP 解压后不确定内容类型的情况。
    """
    try:
        return load_dicom_series(directory)
    except ValueError:
        return load_image_stack(directory)


def load_zip(zip_path):
    """解压 ZIP 压缩包并加载其中的 DICOM 或图片序列数据。
    
    工作流程：
      1. 验证 ZIP 格式有效性
      2. 路径遍历安全检查（防止 ZIP 炸弹攻击）
      3. 解压到临时目录
      4. 自动检测内容类型并加载
      5. 清理临时文件
    
    参数：
      zip_path: ZIP 文件路径
    
    返回：
      (vtkImageData, info_dict) — 三维体数据
    
    异常：
      ValueError: 非有效 ZIP 文件 或 ZIP 内包含不安全的路径
    """
    if not zipfile.is_zipfile(zip_path):
        raise ValueError("Not a valid ZIP archive:\n%s" % zip_path)

    tmp = tempfile.mkdtemp(prefix="ctto3d_zip_")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            # 路径遍历漏洞防护：检查所有成员路径在解压目标目录内
            for member in zf.namelist():
                dest = os.path.realpath(os.path.join(tmp, member))
                if not dest.startswith(os.path.realpath(tmp) + os.sep) \
                        and dest != os.path.realpath(tmp):
                    raise ValueError("Unsafe path in ZIP archive: %s" % member)
            zf.extractall(tmp)
        return load_folder_auto(tmp)
    finally:
        # 确保临时目录被清理
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 内置演示体模 — 合成胸部 CT
# ============================================================

def make_demo_phantom(n=128):
    """生成一个合成的胸部 CT 体模，包含逼真的 HU 值。
    
    体模包含以下结构（内→外）：
      - 双肺（空气填充，−550 HU）
      - 纵隔/胸部器官（40 HU）
      - 脊柱（皮质骨，1000 HU）
      - 肋骨环（皮质骨，600 HU）
      - 主动脉（造影填充，280 HU）
      - 肝脏（实质密度，115 HU）
      - 积液腔（8 HU）
      - 肌肉/体壁（40 HU）
      - 皮下脂肪（−80 HU）
      - 体外衣物/空气（−870 HU / −1000 HU）
    
    用途：让用户无需真实数据即可体验所有组织分层功能。
    
    参数：
      n: 体素网格尺寸（n×n×n），默认 128
        增大 n 可获得更精细的体模，但也消耗更多内存
    
    返回：
      (vtkImageData, info_dict) — 合成 CT 体数据

    修改方法：
      - 调整各结构大小/位置：修改各 ellipsoid 的 rx/ry/rz 参数（相对于网格尺寸的比例）
      - 修改 HU 密度值：修改 arr[mask] = value 中的 value
      - 添加/删除结构：新增 ellipsoid + 赋值代码块
    """
    z, y, x = n, n, n
    arr = np.full((z, y, x), -1000.0, np.float32)          # 初始填充空气（−1000 HU）
    zz, yy, xx = np.mgrid[0:z, 0:y, 0:x].astype(np.float32)
    cx, cy, cz = x / 2.0, y / 2.0, z / 2.0               # 网格中心点

    def ellipsoid(rx, ry, rz, dx=0.0, dy=0.0, dz=0.0):
        """生成椭球体布尔掩膜。
        参数 rx/ry/rz 是相对于网格尺寸的比例（如 0.4 表示半径 = 0.4×n）
        参数 dx/dy/dz 是相对于网格尺寸的偏移量
        """
        return ((((xx - (cx + dx * x)) / (rx * x)) ** 2) +
                (((yy - (cy + dy * y)) / (ry * y)) ** 2) +
                (((zz - (cz + dz * z)) / (rz * z)) ** 2)) <= 1.0

    # 体外躯干轮廓
    body = ellipsoid(0.40, 0.30, 0.45)
    arr[body] = -80.0                                      # 皮下脂肪（−80 HU）
    arr[ellipsoid(0.385, 0.285, 0.44)] = 40.0              # 肌肉体壁（40 HU）
    cavity = ellipsoid(0.355, 0.255, 0.43)
    arr[cavity] = 40.0                                     # 胸腔器官/纵隔（40 HU）

    # 左右双肺（空气填充）
    for sgn in (-1.0, 1.0):
        lung = ellipsoid(0.135, 0.185, 0.30, dx=sgn * 0.15, dy=-0.02)
        arr[lung & cavity] = -550.0

    # 脊柱：后方高密度皮质骨柱
    spine = (((xx - cx) / (0.055 * x)) ** 2 +
             ((yy - (cy + 0.21 * y)) / (0.06 * y)) ** 2) <= 1.0
    arr[spine & body] = 1000.0

    # 肋骨环：在体壁中按等间距分布的皮质骨环
    wall = body & ~ellipsoid(0.35, 0.25, 0.43)
    for fz in np.linspace(0.30, 0.74, 7):
        band = np.abs(zz - fz * z) < (0.012 * z)
        arr[band & wall] = 600.0

    # 主动脉：造影增强血管（低于骨骼 HU，保持可分辨）
    aorta = (((xx - cx) / (0.03 * x)) ** 2 +
             ((yy - (cy - 0.02 * y)) / (0.03 * y)) ** 2) <= 1.0
    arr[aorta & cavity] = 280.0

    # 肝脏（实质密度）和积液腔（展示液体层次）
    liver = ellipsoid(0.14, 0.12, 0.13, dx=0.11, dy=0.06, dz=-0.22)
    arr[liver & cavity] = 115.0
    fluid = ellipsoid(0.06, 0.06, 0.07, dx=-0.11, dy=0.05, dz=-0.20)
    arr[fluid & cavity] = 8.0

    # 体外衣物/毯子（接近空气的密度），不影响肺部显示
    cloth = ellipsoid(0.435, 0.335, 0.455) & ~body
    arr[cloth] = -870.0

    return _numpy_to_vtk(arr, (2.0, 2.0, 2.0), "CT")
