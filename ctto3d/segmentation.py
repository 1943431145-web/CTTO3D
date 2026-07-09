"""TotalSegmentator integration helpers.

This module keeps the optional AI dependency outside the normal app startup:
the main application can run without TotalSegmentator installed, and only
checks for the CLI when the user starts automatic organ segmentation.
"""

import os
import re
import shutil
import subprocess
import sys
import colorsys
import hashlib
from pathlib import Path

import vtk
from PySide6 import QtCore
from vtk.util import numpy_support


TOTAL_SEGMENTATOR_INSTALL_HINT = (
    "未找到 TotalSegmentator 命令。\n\n"
    "请先在当前 Python 环境安装:\n"
    "pip install TotalSegmentator\n\n"
    "如果使用 GPU,还需要按显卡环境安装 PyTorch。"
)


CHEST_MAIN_OUTPUTS = [
    "lung_upper_lobe_left",
    "lung_lower_lobe_left",
    "lung_upper_lobe_right",
    "lung_middle_lobe_right",
    "lung_lower_lobe_right",
    "trachea",
    "esophagus",
    "heart",
    "aorta",
]

LUNG_LOBE_OUTPUTS = [
    "lung_upper_lobe_left",
    "lung_lower_lobe_left",
    "lung_upper_lobe_right",
    "lung_middle_lobe_right",
    "lung_lower_lobe_right",
    "trachea",
]

LUNG_VESSEL_AIRWAY_OUTPUTS = [
    "lung_airways",
    "lung_airways_wall",
    "lung_arteries",
    "lung_veins",
]

LUNG_DETAILED_OUTPUTS = LUNG_LOBE_OUTPUTS + LUNG_VESSEL_AIRWAY_OUTPUTS

TOTAL_ORGAN_OUTPUTS = [
    "spleen", "kidney_right", "kidney_left", "gallbladder", "liver",
    "stomach", "pancreas", "adrenal_gland_right", "adrenal_gland_left",
    "lung_upper_lobe_left", "lung_lower_lobe_left", "lung_upper_lobe_right",
    "lung_middle_lobe_right", "lung_lower_lobe_right", "esophagus",
    "trachea", "thyroid_gland", "small_bowel", "duodenum", "colon",
    "urinary_bladder", "prostate", "kidney_cyst_left", "kidney_cyst_right",
    "sacrum", "vertebrae_S1", "vertebrae_L5", "vertebrae_L4",
    "vertebrae_L3", "vertebrae_L2", "vertebrae_L1", "vertebrae_T12",
    "vertebrae_T11", "vertebrae_T10", "vertebrae_T9", "vertebrae_T8",
    "vertebrae_T7", "vertebrae_T6", "vertebrae_T5", "vertebrae_T4",
    "vertebrae_T3", "vertebrae_T2", "vertebrae_T1", "vertebrae_C7",
    "vertebrae_C6", "vertebrae_C5", "vertebrae_C4", "vertebrae_C3",
    "vertebrae_C2", "vertebrae_C1", "heart", "aorta", "pulmonary_vein",
    "brachiocephalic_trunk", "subclavian_artery_right",
    "subclavian_artery_left", "common_carotid_artery_right",
    "common_carotid_artery_left", "brachiocephalic_vein_left",
    "brachiocephalic_vein_right", "atrial_appendage_left",
    "superior_vena_cava", "inferior_vena_cava",
    "portal_vein_and_splenic_vein", "iliac_artery_left",
    "iliac_artery_right", "iliac_vena_left", "iliac_vena_right",
    "humerus_left", "humerus_right", "scapula_left", "scapula_right",
    "clavicula_left", "clavicula_right", "femur_left", "femur_right",
    "hip_left", "hip_right", "spinal_cord", "gluteus_maximus_left",
    "gluteus_maximus_right", "gluteus_medius_left", "gluteus_medius_right",
    "gluteus_minimus_left", "gluteus_minimus_right", "autochthon_left",
    "autochthon_right", "iliopsoas_left", "iliopsoas_right", "brain",
    "skull", "rib_left_1", "rib_left_2", "rib_left_3", "rib_left_4",
    "rib_left_5", "rib_left_6", "rib_left_7", "rib_left_8", "rib_left_9",
    "rib_left_10", "rib_left_11", "rib_left_12", "rib_right_1",
    "rib_right_2", "rib_right_3", "rib_right_4", "rib_right_5",
    "rib_right_6", "rib_right_7", "rib_right_8", "rib_right_9",
    "rib_right_10", "rib_right_11", "rib_right_12", "sternum",
    "costal_cartilages",
]

CLINICAL_ORGAN_OUTPUTS = [
    name for name in TOTAL_ORGAN_OUTPUTS
    if not (
        name == "sacrum"
        or name.startswith("vertebrae_")
        or name.startswith("rib_")
        or name in {
            "humerus_left", "humerus_right", "scapula_left", "scapula_right",
            "clavicula_left", "clavicula_right", "femur_left", "femur_right",
            "hip_left", "hip_right", "spinal_cord", "gluteus_maximus_left",
            "gluteus_maximus_right", "gluteus_medius_left",
            "gluteus_medius_right", "gluteus_minimus_left",
            "gluteus_minimus_right", "autochthon_left", "autochthon_right",
            "iliopsoas_left", "iliopsoas_right", "brain", "skull", "sternum",
            "costal_cartilages",
        }
    )
]


SEGMENTATION_PRESETS = {
    "肺部精细分割（推荐）": {
        "task": "total",
        "tasks": [
            {
                "task": "total",
                "expected_outputs": LUNG_LOBE_OUTPUTS,
                # 只重采样/保存我们真正会用到的肺叶+气管。否则 total 任务会把
                # ~117 个全分辨率 mask 全部写盘(单次约 200s),而我们只留这几个。
                # roi_subset 会触发一个很小的 3mm 粗裁剪模型,顺带缩小推理 FOV、
                # 降低峰值内存。
                "roi_subset": LUNG_LOBE_OUTPUTS,
            },
            {
                "task": "lung_vessels",
                "expected_outputs": LUNG_VESSEL_AIRWAY_OUTPUTS,
                "fast": False,
                "fastest": False,
            },
        ],
        "fast": True,
        "fastest": False,
        "sequential": True,
        "roi_subset": [],
        "expected_outputs": LUNG_DETAILED_OUTPUTS,
    },
    "肺叶/气管（轻量）": {
        "task": "total",
        "fast": True,
        "fastest": False,
        "sequential": True,
        # 用 roi_subset 把 total 任务限制到这几个 ROI,TotalSegmentator 就只会
        # 重采样/保存我们要的(~7 个而不是 ~117 个)。这是单项最大的提速点;它额外
        # 触发的 3mm 粗裁剪模型很轻量,而且会裁剪推理 FOV,反而降低峰值内存。
        "roi_subset": LUNG_LOBE_OUTPUTS,
        "expected_outputs": LUNG_LOBE_OUTPUTS,
    },
    "肺气道/血管（仅内部）": {
        "task": "lung_vessels",
        "fast": True,
        "fastest": False,
        "sequential": True,
        "roi_subset": [],
        "expected_outputs": [
            "lung_arteries",
            "lung_veins",
            "lung_airways",
            "lung_airways_wall",
        ],
    },
}


SEGMENT_COLORS = {
    "spleen": (0.72, 0.20, 0.76),
    "kidney_right": (0.70, 0.28, 0.18),
    "kidney_left": (0.82, 0.36, 0.22),
    "gallbladder": (0.18, 0.72, 0.28),
    "liver": (0.72, 0.34, 0.18),
    "stomach": (0.95, 0.58, 0.30),
    "pancreas": (0.95, 0.76, 0.34),
    "adrenal_gland_right": (0.92, 0.66, 0.18),
    "adrenal_gland_left": (0.86, 0.58, 0.12),
    "lung_upper_lobe_left": (0.35, 0.72, 1.0),
    "lung_lower_lobe_left": (0.25, 0.62, 0.95),
    "lung_upper_lobe_right": (0.42, 0.82, 1.0),
    "lung_middle_lobe_right": (0.30, 0.70, 0.95),
    "lung_lower_lobe_right": (0.18, 0.56, 0.88),
    "trachea": (0.95, 0.83, 0.28),
    "esophagus": (0.72, 0.45, 0.88),
    "thyroid_gland": (0.95, 0.44, 0.72),
    "small_bowel": (0.92, 0.70, 0.48),
    "duodenum": (0.90, 0.62, 0.36),
    "colon": (0.78, 0.50, 0.26),
    "urinary_bladder": (0.28, 0.62, 0.95),
    "prostate": (0.52, 0.42, 0.90),
    "kidney_cyst_left": (0.20, 0.86, 0.92),
    "kidney_cyst_right": (0.16, 0.78, 0.86),
    "sacrum": (0.88, 0.82, 0.66),
    "heart": (1.0, 0.30, 0.38),
    "aorta": (0.92, 0.08, 0.12),
    "pulmonary_vein": (0.42, 0.56, 1.0),
    "brachiocephalic_trunk": (0.96, 0.18, 0.18),
    "subclavian_artery_right": (0.98, 0.24, 0.18),
    "subclavian_artery_left": (0.92, 0.16, 0.14),
    "common_carotid_artery_right": (0.90, 0.12, 0.18),
    "common_carotid_artery_left": (0.82, 0.08, 0.16),
    "brachiocephalic_vein_left": (0.15, 0.34, 0.96),
    "brachiocephalic_vein_right": (0.10, 0.42, 0.94),
    "atrial_appendage_left": (1.0, 0.42, 0.48),
    "superior_vena_cava": (0.12, 0.52, 0.96),
    "inferior_vena_cava": (0.10, 0.44, 0.84),
    "portal_vein_and_splenic_vein": (0.16, 0.48, 0.88),
    "iliac_artery_left": (0.88, 0.08, 0.12),
    "iliac_artery_right": (0.96, 0.14, 0.12),
    "iliac_vena_left": (0.10, 0.28, 0.78),
    "iliac_vena_right": (0.14, 0.36, 0.86),
    "spinal_cord": (0.96, 0.90, 0.38),
    "brain": (0.86, 0.58, 0.92),
    "skull": (0.86, 0.84, 0.76),
    "sternum": (0.90, 0.82, 0.62),
    "costal_cartilages": (0.65, 0.86, 0.72),
    "lung_arteries": (0.95, 0.12, 0.15),
    "lung_veins": (0.08, 0.35, 0.95),
    "lung_airways": (0.96, 0.78, 0.18),
    "lung_airways_wall": (0.98, 0.92, 0.45),
}

SEGMENT_LABELS = {
    "lung_upper_lobe_left": "左肺上叶",
    "lung_lower_lobe_left": "左肺下叶",
    "lung_upper_lobe_right": "右肺上叶",
    "lung_middle_lobe_right": "右肺中叶",
    "lung_lower_lobe_right": "右肺下叶",
    "trachea": "气管",
    "esophagus": "食管",
    "thyroid_gland": "甲状腺",
    "spleen": "脾脏",
    "kidney_right": "右肾",
    "kidney_left": "左肾",
    "gallbladder": "胆囊",
    "liver": "肝脏",
    "stomach": "胃",
    "pancreas": "胰腺",
    "adrenal_gland_right": "右肾上腺",
    "adrenal_gland_left": "左肾上腺",
    "small_bowel": "小肠",
    "duodenum": "十二指肠",
    "colon": "结肠",
    "urinary_bladder": "膀胱",
    "prostate": "前列腺",
    "kidney_cyst_left": "左肾囊肿",
    "kidney_cyst_right": "右肾囊肿",
    "sacrum": "骶骨",
    "heart": "心脏",
    "aorta": "主动脉",
    "pulmonary_vein": "肺静脉",
    "brachiocephalic_trunk": "头臂干",
    "subclavian_artery_right": "右锁骨下动脉",
    "subclavian_artery_left": "左锁骨下动脉",
    "common_carotid_artery_right": "右颈总动脉",
    "common_carotid_artery_left": "左颈总动脉",
    "brachiocephalic_vein_left": "左头臂静脉",
    "brachiocephalic_vein_right": "右头臂静脉",
    "atrial_appendage_left": "左心耳",
    "superior_vena_cava": "上腔静脉",
    "inferior_vena_cava": "下腔静脉",
    "portal_vein_and_splenic_vein": "门静脉/脾静脉",
    "iliac_artery_left": "左髂动脉",
    "iliac_artery_right": "右髂动脉",
    "iliac_vena_left": "左髂静脉",
    "iliac_vena_right": "右髂静脉",
    "spinal_cord": "脊髓",
    "brain": "脑",
    "skull": "颅骨",
    "sternum": "胸骨",
    "costal_cartilages": "肋软骨",
    "lung_arteries": "肺动脉",
    "lung_veins": "肺静脉",
    "lung_airways": "肺气道",
    "lung_airways_wall": "肺气道壁",
}

DEFAULT_SEGMENT_COLOR = (0.15, 0.85, 0.72)

# 推理后处理(重采样回原分辨率 + gzip 写盘)的线程数。必须保持为 1。
# TotalSegmentator 保存时用 multiprocessing.Pool 并行,每个 worker 调
# nibabel 的 img.get_fdata(),它默认把整卷 mask 读成 float64:
# 512×512×670 × 8字节 ≈ 1.4GB 一个。全分辨率任务(如 lung_vessels,
# 不走 fast/roi_subset)并行多开会同时吃数个 1.4GB,叠加 torch 直接 MemoryError。
# 提速主要靠 roi_subset(只存需要的 mask),不要靠加这个线程数。
SEG_POSTPROC_THREADS = 1

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WEIGHTS_DIR = PROJECT_ROOT / "model"

DOWNLOAD_TASK_LABELS = {
    "total_fast": "快速 total 模型（3mm/6mm）",
    "total": "标准 total 模型（1.5mm）",
    "lung_vessels": "肺气道/肺血管模型",
}


def preset_names():
    return list(SEGMENTATION_PRESETS.keys())


def preset_by_name(name):
    return dict(SEGMENTATION_PRESETS.get(name, SEGMENTATION_PRESETS[preset_names()[0]]))


def _side_text(side):
    return {"left": "左", "right": "右"}.get(side, "")


def segment_display_name(name):
    if name in SEGMENT_LABELS:
        return SEGMENT_LABELS[name]
    if name.startswith("vertebrae_"):
        return "%s椎体" % name.replace("vertebrae_", "")
    match = re.match(r"rib_(left|right)_(\d+)$", name)
    if match:
        return "%s第%s肋骨" % (_side_text(match.group(1)), match.group(2))
    match = re.match(
        r"(humerus|scapula|clavicula|femur|hip|gluteus_maximus|"
        r"gluteus_medius|gluteus_minimus|autochthon|iliopsoas)_(left|right)$",
        name)
    if match:
        base_labels = {
            "humerus": "肱骨",
            "scapula": "肩胛骨",
            "clavicula": "锁骨",
            "femur": "股骨",
            "hip": "髋骨",
            "gluteus_maximus": "臀大肌",
            "gluteus_medius": "臀中肌",
            "gluteus_minimus": "臀小肌",
            "autochthon": "竖脊肌",
            "iliopsoas": "髂腰肌",
        }
        return "%s%s" % (_side_text(match.group(2)), base_labels[match.group(1)])
    return name


def segment_color(name):
    if name in SEGMENT_COLORS:
        return SEGMENT_COLORS[name]
    digest = hashlib.sha1(name.encode("utf-8")).digest()
    hue = int.from_bytes(digest[:2], "big") / 65535.0
    saturation = 0.56 + (digest[2] / 255.0) * 0.24
    value = 0.74 + (digest[3] / 255.0) * 0.22
    return colorsys.hsv_to_rgb(hue, saturation, value)


def expected_outputs_for_preset(preset):
    expected = (preset or {}).get("expected_outputs")
    if expected:
        return list(expected)
    if (preset or {}).get("task") == "total":
        return list(TOTAL_ORGAN_OUTPUTS)
    return []


def task_presets_for_run(preset):
    task_defs = (preset or {}).get("tasks")
    if not task_defs:
        return [dict(preset or {})]
    runs = []
    for item in task_defs:
        merged = dict(preset or {})
        merged.pop("tasks", None)
        merged.pop("expected_outputs", None)
        merged.update(item or {})
        runs.append(merged)
    return runs


def cuda_device_name():
    try:
        import torch
        if torch.cuda.is_available() and torch.cuda.device_count() > 0:
            return torch.cuda.get_device_name(0)
    except Exception:
        pass
    return ""


def cuda_available():
    return bool(cuda_device_name())


def find_totalsegmentator():
    for name in ("TotalSegmentator", "TotalSegmentator.exe", "totalsegmentator"):
        path = shutil.which(name)
        if path:
            return [path]
    return None


def find_totalseg_download_weights():
    for name in ("totalseg_download_weights", "totalseg_download_weights.exe"):
        path = shutil.which(name)
        if path:
            return [path]
    return None


def python_for_totalsegmentator(command):
    if command:
        exe = Path(command[0])
        if exe.name.lower() in ("totalsegmentator.exe", "totalsegmentator"):
            candidate = exe.parent.parent / "python.exe"
            if candidate.exists():
                return str(candidate)
    return sys.executable


def download_task_for_preset(preset):
    tasks = download_tasks_for_preset(preset)
    return tasks[0] if tasks else "total_fast"


def download_tasks_for_preset(preset):
    task_defs = (preset or {}).get("tasks") or [preset or {}]
    tasks = []
    for item in task_defs:
        merged = dict(preset or {})
        merged.pop("tasks", None)
        merged.update(item or {})
        task = _download_task_for_single_preset(merged)
        if task not in tasks:
            tasks.append(task)
    return tasks


def _download_task_for_single_preset(preset):
    task = (preset or {}).get("task", "total")
    if task == "total":
        if (preset or {}).get("fastest", False) or (preset or {}).get("fast", True):
            return "total_fast"
        return "total"
    if task == "lung_vessels":
        return "lung_vessels"
    return task


def download_task_display_name(task):
    return DOWNLOAD_TASK_LABELS.get(task, task)


def download_tasks_display_name(tasks):
    return " + ".join(download_task_display_name(task) for task in tasks)


def weights_cache_dir():
    return DEFAULT_WEIGHTS_DIR


def weights_cache_dir_hint():
    return str(weights_cache_dir())


def totalseg_process_env():
    env = os.environ.copy()
    env["TOTALSEG_WEIGHTS_PATH"] = str(weights_cache_dir())
    # 让子进程用 UTF-8 输出,这样我们按 UTF-8 读管道时中文/tqdm 进度条不会变成乱码
    # (否则默认是中文 Windows 的 GBK 控制台代码页,会和子进程输出对不上)。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def build_weight_download_commands(preset):
    tasks = download_tasks_for_preset(preset)
    command = find_totalsegmentator()
    commands = []
    for task in tasks:
        if command is not None:
            args = [
            python_for_totalsegmentator(command),
            "-u",
            "-m",
            "totalsegmentator.bin.totalseg_download_weights",
            "-t",
            task,
            ]
        else:
            downloader = find_totalseg_download_weights()
            if downloader is None:
                raise RuntimeError(TOTAL_SEGMENTATOR_INSTALL_HINT)
            args = downloader + ["-t", task]
        commands.append((args, task))
    return commands


def build_weight_download_command(preset):
    commands = build_weight_download_commands(preset)
    if not commands:
        raise RuntimeError(TOTAL_SEGMENTATOR_INSTALL_HINT)
    return commands[0]


def write_nifti(image, path):
    writer = vtk.vtkNIFTIImageWriter()
    writer.SetInputData(image)
    writer.SetFileName(str(path))
    writer.Write()


def read_nifti(path, reference_image=None):
    reader = vtk.vtkNIFTIImageReader()
    reader.SetFileName(str(path))
    reader.Update()
    image = vtk.vtkImageData()
    image.DeepCopy(reader.GetOutput())
    if reference_image is not None and image.GetDimensions() == reference_image.GetDimensions():
        image.SetOrigin(reference_image.GetOrigin())
        image.SetSpacing(reference_image.GetSpacing())
    return image


def mask_statistics(mask_image):
    if mask_image is None:
        return {"voxels": 0, "volume_ml": 0.0}
    scalars = mask_image.GetPointData().GetScalars()
    if scalars is None:
        return {"voxels": 0, "volume_ml": 0.0}
    arr = numpy_support.vtk_to_numpy(scalars)
    voxels = int((arr > 0).sum())
    sx, sy, sz = mask_image.GetSpacing()
    volume_ml = voxels * abs(float(sx) * float(sy) * float(sz)) / 1000.0
    return {"voxels": voxels, "volume_ml": volume_ml}


def mask_files(output_dir, expected_names=None):
    output = Path(output_dir)
    if expected_names:
        files = []
        for name in expected_names:
            for suffix in (".nii.gz", ".nii"):
                path = output / (name + suffix)
                if path.exists():
                    files.append(path)
                    break
        return files
    return sorted(list(output.glob("*.nii.gz")) + list(output.glob("*.nii")))


def clean_progress_text(text):
    """Normalize tqdm/ANSI progress output before showing it in Qt labels."""
    if not text:
        return ""
    text = ANSI_RE.sub("", text)
    text = text.replace("\r", "\n")
    parts = [part.strip() for part in text.splitlines() if part.strip()]
    if not parts:
        return ""
    text = parts[-1]
    text = text.replace("\b", "")
    text = CONTROL_RE.sub("", text)
    text = text.replace("�", "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    lowered = text.lower()
    if "download finished" in lowered:
        return "模型权重下载完成,正在解压..."
    if "download" in lowered or "downloading" in lowered:
        match = re.search(r"(\d{1,3})\s*%", text)
        if match:
            return "正在下载 TotalSegmentator 模型权重: %s%%" % match.group(1)
        return "正在下载 TotalSegmentator 模型权重..."
    if lowered.startswith("processing "):
        return "正在检查/下载模型权重: %s" % text.replace("Processing", "Task")
    return text


def download_failure_message(code, recent_output, task):
    output = "\n".join(recent_output)
    lowered = output.lower()
    if any(token in lowered for token in (
        "incompleteread", "chunkedencodingerror", "connection broken",
        "read timed out", "connection aborted", "connection reset",
        "proxyerror", "unable to connect to proxy", "maxretryerror",
        "remote end closed connection", "release-assets.githubusercontent.com",
    )):
        return (
            "模型权重下载失败。\n\n"
            "当前要下载的是 %s,下载源是 TotalSegmentator 的 GitHub release。"
            "这个错误通常是网络、代理或 GitHub 连接中断导致的。\n\n"
            "可以先检查代理/网络后重新点击“下载模型”;如果之前中断过,也可以删除未完成临时文件后再试:\n%s\n\n"
            "最近输出:\n%s"
        ) % (
            download_task_display_name(task),
            str(Path(weights_cache_dir_hint()) / "tmp_download_file.zip"),
            output[-1500:],
        )
    return "模型权重下载失败,退出码 %s。\n\n最近输出:\n%s" % (
        code, output[-1500:])


class TotalSegmentatorDownloadWorker(QtCore.QObject):
    progress = QtCore.Signal(str)
    finished = QtCore.Signal(str)
    failed = QtCore.Signal(str)

    def __init__(self, preset, parent=None):
        super().__init__(parent)
        self.preset = dict(preset)
        self._process = None
        self._last_progress = ""
        self._recent_output = []
        self._task = ""

    @QtCore.Slot()
    def run(self):
        try:
            commands = build_weight_download_commands(self.preset)
        except Exception as exc:
            self.failed.emit(str(exc))
            return

        completed = []
        for index, (args, task) in enumerate(commands, start=1):
            self._task = task
            self._last_progress = ""
            label = download_task_display_name(task)
            self.progress.emit("开始下载/检查 TotalSegmentator %s (%d/%d): %s" % (
                label, index, len(commands), " ".join(args)))
            try:
                self._process = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=totalseg_process_env(),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                for line in self._process.stdout or []:
                    line = clean_progress_text(line)
                    if line and line != self._last_progress:
                        self._last_progress = line
                        self._remember_output(line)
                        self.progress.emit(line)
                code = self._process.wait()
            except Exception as exc:
                self.failed.emit(str(exc))
                return

            if code != 0:
                self.failed.emit(download_failure_message(
                    code, self._recent_output, task))
                return
            completed.append(task)
        self.finished.emit(download_tasks_display_name(completed))

    def cancel(self):
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()

    def _remember_output(self, text):
        self._recent_output.append(text)
        if len(self._recent_output) > 60:
            self._recent_output = self._recent_output[-60:]


class TotalSegmentatorWorker(QtCore.QObject):
    progress = QtCore.Signal(str)
    finished = QtCore.Signal(str, list)
    failed = QtCore.Signal(str)

    def __init__(self, input_path, output_dir, preset, parent=None):
        super().__init__(parent)
        self.input_path = str(input_path)
        self.output_dir = str(output_dir)
        self.preset = dict(preset)
        self._process = None
        self._last_progress = ""
        self._recent_output = []

    @QtCore.Slot()
    def run(self):
        command = find_totalsegmentator()
        if command is None:
            self.failed.emit(TOTAL_SEGMENTATOR_INSTALL_HINT)
            return

        device = self.preset.get("device") or "gpu"
        lowmem = "低内存推理开启" if self.preset.get("force_split", False) else "低内存推理关闭"
        runs = task_presets_for_run(self.preset)
        for index, run_preset in enumerate(runs, start=1):
            args = self._build_command(command, run_preset)
            task = run_preset.get("task", "total")
            self._last_progress = ""
            self.progress.emit("启动 TotalSegmentator %s (%d/%d, %s, %s): %s" % (
                task, index, len(runs), device, lowmem, " ".join(args)))
            try:
                self._process = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=totalseg_process_env(),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                for line in self._process.stdout or []:
                    line = clean_progress_text(line)
                    if line and line != self._last_progress:
                        self._last_progress = line
                        self._remember_output(line)
                        self.progress.emit(line)
                code = self._process.wait()
            except Exception as exc:
                self.failed.emit(str(exc))
                return

            if code != 0:
                self.failed.emit(self._failure_message(code))
                return

        roi_subset = self.preset.get("roi_subset") or []
        expected = expected_outputs_for_preset(self.preset) or roi_subset
        self.finished.emit(self.output_dir, list(expected))

    def _build_command(self, command, preset=None):
        preset = preset or self.preset
        task = preset.get("task", "total")
        roi_subset = preset.get("roi_subset") or []
        args = None
        if preset.get("sequential", True):
            runner = Path(__file__).with_name("totalseg_runner.py")
            if runner.exists():
                args = [
                    python_for_totalsegmentator(command),
                    "-u",
                    str(runner),
                    "-i", self.input_path,
                    "-o", self.output_dir,
                    "--task", task,
                    "-nr", str(SEG_POSTPROC_THREADS),
                    "-ns", str(SEG_POSTPROC_THREADS),
                ]
        if args is None:
            args = command + [
                "-i", self.input_path,
                "-o", self.output_dir,
                "--task", task,
                "-nr", str(SEG_POSTPROC_THREADS),
                "-ns", str(SEG_POSTPROC_THREADS),
            ]
        if preset.get("fastest", False):
            args.append("--fastest")
        elif preset.get("fast", True):
            args.append("--fast")
        if preset.get("force_split", False):
            args.append("--force_split")
        device = preset.get("device")
        if device:
            args.extend(["--device", str(device)])
        if roi_subset:
            args.append("--roi_subset")
            args.extend(roi_subset)
        return args

    def cancel(self):
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()

    def _remember_output(self, text):
        self._recent_output.append(text)
        if len(self._recent_output) > 40:
            self._recent_output = self._recent_output[-40:]

    def _failure_message(self, code):
        output = "\n".join(self._recent_output)
        lowered = output.lower()
        if any(token in lowered for token in (
            "incompleteread", "chunkedencodingerror", "connection broken",
            "read timed out", "connection aborted", "connection reset",
            "proxyerror", "unable to connect to proxy", "maxretryerror",
            "remote end closed connection", "release-assets.githubusercontent.com",
        )):
            return (
                "TotalSegmentator 模型权重下载失败,所以自动分割没有开始。\n\n"
                "这次失败发生在下载 GitHub release 权重文件时,通常是网络/代理连接被断开。\n"
                "取消“快速模式”会使用 1.5mm 标准模型,需要额外下载更大的 Dataset291 等权重;当前机器只缓存了 3mm/6mm 快速模型。\n\n"
                "处理办法:\n"
                "1. 如果只是想要更平滑的可视化,建议先保持“快速模式”,它使用已缓存的 3mm 模型。\n"
                "2. 如果一定要标准质量,请检查网络/代理后重新点击“自动分割”,通常会重新下载或继续下载。\n"
                "3. 如果反复失败,删除未完成缓存后重试:\n"
                + str(Path(weights_cache_dir_hint()) / "tmp_download_file.zip") +
                "\n4. 也可以在左侧面板点击“下载模型”,等模型完整缓存后再运行自动分割。"
            )
        if any(token in lowered for token in (
            "background workers died", "ram was full",
            "couldn't register wait on event", "_share_filename_cpu_",
            "error code: <1008>", "spawnprocess",
        )):
            return (
                "TotalSegmentator 的 nnUNet/PyTorch 后台进程在 Windows 上退出了。\n\n"
                "这通常不是 CT 文件损坏,而是内存/虚拟内存压力,或 Windows 多进程共享内存不稳定导致的。\n\n"
                "我已经把软件默认推荐模式改成更稳的低内存调用:\n"
                "- 重采样/保存线程: 1(并行保存会同时把整卷 mask 读成 float64,易内存溢出)\n"
                "- 默认使用 --fast 3mm 模型,不再用最粗的 --fastest\n"
                "- 通过 --roi_subset 只输出需要的器官,既省时间也减少要缓存的 mask 数\n"
                "- 使用 nnUNet 顺序单进程推理,绕开 Windows 后台 worker\n\n"
                "处理建议:\n"
                "1. 重新启动软件后,用“肺部精细分割（推荐）”+“快速模式”再试。\n"
                "2. 关闭浏览器、三维软件等占内存程序;必要时增大 Windows 虚拟内存。\n"
                "3. 如果仍失败,优先换 Python 3.10/3.11 的独立环境运行 TotalSegmentator;Python 3.12 下这类多进程错误更容易遇到。"
            )
        return "TotalSegmentator 运行失败,退出码 %s。\n\n最近输出:\n%s" % (
            code, output[-1500:])
