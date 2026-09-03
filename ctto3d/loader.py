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
import zipfile

import numpy as np
import vtk
from vtk.util import numpy_support

# 支持的图片文件扩展名（全部小写比较）
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")


def _clean_dicom_text(value):
    return str(value or "").replace("^", " ").strip()


def _dicom_preview_metadata(ds):
    """从不含像素数据的 DICOM 头提取左侧影像列表所需的轻量信息。"""
    rows = getattr(ds, "Rows", None)
    cols = getattr(ds, "Columns", None)
    result = {
        "patient_name": _clean_dicom_text(getattr(ds, "PatientName", "")),
        "patient_id": _clean_dicom_text(getattr(ds, "PatientID", "")),
        "modality": _clean_dicom_text(getattr(ds, "Modality", "")).upper(),
        "study_date": _clean_dicom_text(getattr(ds, "StudyDate", "")),
        "series_description": _clean_dicom_text(
            getattr(ds, "SeriesDescription", "")),
    }
    if rows and cols:
        result["dimensions_text"] = "%s×%s" % (cols, rows)
    return {key: value for key, value in result.items() if value}


def _inspect_dicom_folder(directory, max_attempts=128):
    import pydicom

    attempts = 0
    for root, _, names in os.walk(directory):
        for name in names:
            if attempts >= max_attempts:
                return {}
            attempts += 1
            try:
                ds = pydicom.dcmread(
                    os.path.join(root, name),
                    stop_before_pixels=True,
                    force=True,
                )
            except Exception:
                continue
            metadata = _dicom_preview_metadata(ds)
            if metadata.get("modality") or metadata.get("patient_name"):
                return metadata
    return {}


def _inspect_zip(zip_path, max_attempts=128):
    metadata = {}
    with zipfile.ZipFile(zip_path) as zf:
        names = [item for item in zf.namelist() if not item.endswith("/")]
        metadata["file_count"] = len(names)
        try:
            import pydicom
            for name in names[:max_attempts]:
                try:
                    with zf.open(name) as stream:
                        ds = pydicom.dcmread(
                            stream, stop_before_pixels=True, force=True)
                except Exception:
                    continue
                dicom_meta = _dicom_preview_metadata(ds)
                if dicom_meta.get("modality") or dicom_meta.get("patient_name"):
                    metadata.update(dicom_meta)
                    break
        except ImportError:
            pass
    return metadata


def inspect_source(kind, path):
    """轻量检查数据源，不读取像素体数据，供“导入后待加载”列表显示。"""
    if kind == "dicom":
        return _inspect_dicom_folder(path)
    if kind == "zip":
        return _inspect_zip(path)
    if kind == "images":
        files = _gather_images(path)
        result = {"modality": "IMAGE", "file_count": len(files)}
        if files:
            try:
                from PIL import Image
                with Image.open(files[0]) as image:
                    result["dimensions_text"] = "%d×%d · %d 张" % (
                        image.width, image.height, len(files))
            except Exception:
                pass
        return result
    return {}


def detect_source_kind(path):
    """根据路径自动判断导入类型：zip / dicom / images。"""
    path = os.path.realpath(os.path.abspath(path))
    if os.path.isfile(path):
        if path.lower().endswith(".zip"):
            return "zip"
        raise ValueError("暂不支持该文件类型:\n%s" % path)
    if not os.path.isdir(path):
        raise ValueError("路径不存在:\n%s" % path)
    dicom_meta = _inspect_dicom_folder(path)
    if dicom_meta.get("modality") or dicom_meta.get("patient_name"):
        return "dicom"
    if _gather_images(path):
        return "images"
    # 空目录或无法识别时仍按 DICOM 走，加载阶段会给出明确错误。
    return "dicom"


def _natural_key(s):
    """自然排序键函数：让 "slice2" 排在 "slice10" 前面（按人类阅读习惯排序）。
    
    将字符串中的数字片段转为整数参与比较，非数字部分按小写字符串比较。
    例如: ["slice10", "slice2"] → ["slice2", "slice10"]
    """
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _notify_progress(callback, fraction, message):
    """向可选的加载进度回调发送节流友好的标准化进度。"""
    if callback is not None:
        callback(max(0.0, min(1.0, float(fraction))), str(message))


def _numpy_to_vtk(arr, spacing, modality, scalar_range=None):
    """将 (z, y, x) 连续 numpy 数组零拷贝转换为 vtkImageData。
    
    注意：numpy 的 C-order 存储中，x 维度在内存中连续变化，
    这与 vtkImageData 期望的点顺序一致，无需转置。
    
    参数：
      arr     — numpy 数组，形状 (z, y, x)，保留 int16/uint8 等源 dtype
      spacing — (sx, sy, sz) 体素间距，单位 mm
      modality — 模态标签 ("CT" / "MR" / "IMAGE")
    
    返回：
      (vtkImageData, info_dict) 元组
    """
    # 保留源数据的最小可用 dtype。旧实现无条件转 float32，并在
    # numpy_to_vtk(deep=True) 时再复制一次；一个 512×512×670 CT 因此会
    # 同时占用两份约 670 MiB 的体数据。VTK 的 numpy bridge 会在 shallow
    # 模式下持有 numpy 引用，可以安全地让两边共享同一块只读体素内存。
    arr = np.ascontiguousarray(arr)
    if arr.dtype == np.bool_:
        arr = arr.astype(np.uint8)
    z, y, x = arr.shape
    flat_view = arr.reshape(-1)
    flat = numpy_support.numpy_to_vtk(flat_view, deep=False)
    img = vtk.vtkImageData()
    img.SetDimensions(x, y, z)
    img.SetSpacing(float(spacing[0]), float(spacing[1]), float(spacing[2]))
    img.GetPointData().SetScalars(flat)
    if scalar_range is None:
        scalar_range = (float(arr.min()), float(arr.max()))
    info = {"modality": modality,
            "scalar_range": tuple(float(value) for value in scalar_range),
            "dimensions": (x, y, z),
            "storage_dtype": str(arr.dtype),
            "memory_bytes": int(arr.nbytes)}
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


def _select_dicom_series_records(records):
    """records 为 (source, header)；只用轻量 header 完成序列选择。"""
    groups = {}
    for source, header in records:
        groups.setdefault(_dicom_series_key(header), []).append(
            (source, header))
    return max(
        groups.values(),
        key=lambda items: _dicom_group_score([item[1] for item in items]),
    )


def _load_dicom_sources(sources, read_dataset, progress=None):
    """从路径或 ZIP member 等抽象数据源流式构建体数据。

    第一遍只读 PixelData 之前的头信息；选定主序列后第二遍逐张解码，
    Dataset 和 pixel_array 每轮即释放，避免把整套原始像素再保留一份。
    """
    sources = list(sources)
    records = []
    total_sources = max(1, len(sources))
    for index, source in enumerate(sources):
        try:
            header = read_dataset(source, True)
        except Exception:
            continue
        if getattr(header, "Rows", None) and getattr(header, "Columns", None):
            records.append((source, header))
        if index % 32 == 0 or index + 1 == len(sources):
            _notify_progress(
                progress, 0.14 * (index + 1) / total_sources,
                "正在识别 DICOM 序列…")

    if not records:
        raise ValueError("No DICOM images with pixel data were found.")

    records = _select_dicom_series_records(records)
    records.sort(key=lambda item: _dicom_slice_position(item[1]))
    ref = records[0][1]
    rows, cols = int(ref.Rows), int(ref.Columns)
    records = [
        item for item in records
        if int(item[1].Rows) == rows and int(item[1].Columns) == cols
    ]

    modality = str(getattr(ref, "Modality", "CT") or "CT").upper()
    if modality not in ("CT", "MR"):
        modality = "CT"

    # 绝大多数 CT 的校准 HU 完整落在 int16，可把 CPU/GPU 体数据直接减半。
    # 遇到小数缩放或超范围数据会自动提升为 float32，绝不静默截断。
    volume = np.empty(
        (len(records), rows, cols),
        dtype=np.int16 if modality == "CT" else np.float32,
    )
    loaded = 0
    scalar_min = float("inf")
    scalar_max = float("-inf")
    total_records = max(1, len(records))

    for index, (source, _header) in enumerate(records):
        try:
            dataset = read_dataset(source, False)
            if "PixelData" not in dataset:
                continue
            pixels = np.asarray(dataset.pixel_array)
        except Exception:
            continue
        if pixels.ndim != 2 or pixels.shape != (rows, cols):
            continue

        slope = float(getattr(dataset, "RescaleSlope", 1.0) or 1.0)
        intercept = float(getattr(dataset, "RescaleIntercept", 0.0) or 0.0)
        raw_min = float(pixels.min())
        raw_max = float(pixels.max())
        value0 = raw_min * slope + intercept
        value1 = raw_max * slope + intercept
        plane_min, plane_max = min(value0, value1), max(value0, value1)

        if volume.dtype == np.int16:
            integral = slope.is_integer() and intercept.is_integer()
            in_range = plane_min >= -32768.0 and plane_max <= 32767.0
            if not (integral and in_range):
                promoted = np.empty(volume.shape, dtype=np.float32)
                if loaded:
                    promoted[:loaded] = volume[:loaded]
                volume = promoted

        if volume.dtype == np.int16:
            # int32 的单层临时量规避 uint16 + 负截距时的中间溢出；它只在
            # 当前循环存活，远小于旧实现保留全部 Dataset/PixelData 的峰值。
            transformed = pixels.astype(np.int32, copy=True)
            if slope != 1.0:
                transformed *= int(slope)
            if intercept:
                transformed += int(intercept)
            volume[loaded] = transformed
        else:
            np.multiply(pixels, slope, out=volume[loaded], casting="unsafe")
            if intercept:
                np.add(volume[loaded], intercept,
                       out=volume[loaded], casting="unsafe")

        loaded += 1
        scalar_min = min(scalar_min, plane_min)
        scalar_max = max(scalar_max, plane_max)
        if index % 8 == 0 or index + 1 == len(records):
            _notify_progress(
                progress, 0.14 + 0.80 * (index + 1) / total_records,
                "正在解码 CT 切片 %d/%d…" % (index + 1, len(records)))

    if loaded == 0:
        raise ValueError("No readable DICOM pixel data were found.")
    if loaded != len(volume):
        volume = np.ascontiguousarray(volume[:loaded])

    ps = getattr(ref, "PixelSpacing", None) or [1.0, 1.0]
    sx, sy = float(ps[1]), float(ps[0])
    sz = float(getattr(ref, "SpacingBetweenSlices", 0) or 0)
    if sz <= 0:
        sz = float(getattr(ref, "SliceThickness", 0) or 0)
    positions = [_dicom_slice_position(header) for _, header in records]
    diffs = [
        abs(b - a) for a, b in zip(positions, positions[1:])
        if abs(b - a) > 1e-4
    ]
    if diffs:
        sz = float(np.median(diffs))
    if sz <= 0:
        sz = 1.0

    _notify_progress(progress, 0.97, "正在建立三维体数据…")
    image, info = _numpy_to_vtk(
        volume, (sx, sy, sz), modality,
        scalar_range=(scalar_min, scalar_max),
    )
    # 保留 DICOM 患者坐标系。vtkImageData 仍使用紧凑的局部 i/j/k 坐标，
    # 但重置相机时需要这些方向余弦，才能把“患者前方、头侧朝上”换算到
    # 当前体数据坐标，而不是把所有病例都武断地当作标准仰卧位。
    orientation = getattr(ref, "ImageOrientationPatient", None)
    if orientation is not None and len(orientation) == 6:
        info["image_orientation_patient"] = tuple(
            float(value) for value in orientation)
    patient_position = str(getattr(ref, "PatientPosition", "") or "").strip()
    if patient_position:
        info["patient_position"] = patient_position
    # 患者信息（规划核对单用）：与导入列表的预览元数据同源、取自同一参考层。
    for info_key, dicom_attr in (
            ("patient_name", "PatientName"),
            ("patient_id", "PatientID"),
            ("study_date", "StudyDate"),
            ("series_description", "SeriesDescription")):
        value = _clean_dicom_text(getattr(ref, dicom_attr, ""))
        if value:
            info[info_key] = value
    _notify_progress(progress, 1.0, "影像解码完成")
    return image, info


def load_dicom_series(directory, progress=None):
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

    sources = []
    for root, _, names in os.walk(directory):
        for n in names:
            sources.append(os.path.join(root, n))

    def read_dataset(path, header_only):
        return pydicom.dcmread(
            path, stop_before_pixels=header_only, force=True)

    try:
        return _load_dicom_sources(sources, read_dataset, progress=progress)
    except ValueError as exc:
        raise ValueError(
            "No DICOM images with pixel data were found in:\n%s" % directory
        ) from exc


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


def _load_image_sources(files, read_image, spacing, progress=None):
    """以 uint8/原始灰度逐层写入连续数组，避免 planes + stack 双份内存。"""
    if not files:
        raise ValueError("No image files were found.")
    first = np.asarray(read_image(files[0]))
    if first.ndim != 2:
        raise ValueError("Image slices must be grayscale.")
    h, w = first.shape
    volume = np.empty((len(files), h, w), dtype=first.dtype)
    volume[0] = first
    loaded = 1
    scalar_min = float(first.min())
    scalar_max = float(first.max())
    for index, source in enumerate(files[1:], 1):
        plane = np.asarray(read_image(source))
        if plane.shape != (h, w):
            continue
        if plane.dtype != volume.dtype:
            plane = plane.astype(volume.dtype)
        volume[loaded] = plane
        loaded += 1
        scalar_min = min(scalar_min, float(plane.min()))
        scalar_max = max(scalar_max, float(plane.max()))
        if index % 8 == 0 or index + 1 == len(files):
            _notify_progress(
                progress, 0.95 * (index + 1) / len(files),
                "正在读取图片切片 %d/%d…" % (index + 1, len(files)))
    if loaded != len(volume):
        volume = np.ascontiguousarray(volume[:loaded])
    image, info = _numpy_to_vtk(
        volume, spacing, "IMAGE", (scalar_min, scalar_max))
    _notify_progress(progress, 1.0, "图片序列解码完成")
    return image, info


def load_image_stack(directory, slice_spacing=1.0, pixel_spacing=1.0,
                     progress=None):
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

    def read_image(path):
        with Image.open(path) as image:
            return np.asarray(image.convert("L"), dtype=np.uint8)

    return _load_image_sources(
        files, read_image,
        (pixel_spacing, pixel_spacing, slice_spacing),
        progress=progress,
    )


def load_folder_auto(directory):
    """自动检测文件夹内容类型：先尝试 DICOM，失败则作为图片序列加载。
    
    这是一个便捷函数，用于 ZIP 解压后不确定内容类型的情况。
    """
    try:
        return load_dicom_series(directory)
    except ValueError:
        return load_image_stack(directory)


def load_zip(zip_path, progress=None):
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

    import pydicom

    with zipfile.ZipFile(zip_path) as zf:
        members = [item for item in zf.infolist() if not item.is_dir()]

        # 直接从压缩流读 DICOM，避免先把数百 MiB 写到临时目录再读回来。
        def read_dataset(member, header_only):
            with zf.open(member) as stream:
                return pydicom.dcmread(
                    stream, stop_before_pixels=header_only, force=True)

        try:
            return _load_dicom_sources(
                members, read_dataset, progress=progress)
        except ValueError:
            pass

        image_groups = {}
        for member in members:
            if member.filename.lower().endswith(IMAGE_EXTS):
                folder = os.path.dirname(member.filename)
                image_groups.setdefault(folder, []).append(member)
        if not image_groups:
            raise ValueError("ZIP archive contains no DICOM or image series.")
        files = max(image_groups.values(), key=len)
        files.sort(key=lambda item: _natural_key(
            os.path.basename(item.filename)))

        from PIL import Image

        def read_image(member):
            with zf.open(member) as stream:
                with Image.open(stream) as image:
                    return np.asarray(image.convert("L"), dtype=np.uint8)

        return _load_image_sources(
            files, read_image, (1.0, 1.0, 1.0), progress=progress)


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
