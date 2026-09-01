"""Standalone MONAI lung-nodule segmentation runner.

The GUI launches this module with the Python installation used by
TotalSegmentator.  Keeping torch/MONAI inference in a child process prevents
GPU memory and a failed CUDA context from destabilising the VTK/Qt process.
"""

from __future__ import annotations

import argparse
import gc
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOCAL_LIBS = PROJECT_ROOT / ".applibs"
if LOCAL_LIBS.is_dir() and str(LOCAL_LIBS) not in sys.path:
    sys.path.insert(0, str(LOCAL_LIBS))

NODULE_REPO = "Kakimaki00/nodule-segresnet-3d-small"
ROI_REPO = "Kakimaki00/roi-segresnet-2d"
NODULE_DIR = PROJECT_ROOT / "model" / "MONAI_nodule_segresnet_3d_small"
ROI_DIR = PROJECT_ROOT / "model" / "MONAI_lung_roi_segresnet_2d"
REQUIRED_FILES = ("model.pth", "config.yaml", "README.md")

# -----------------------------------------------------------
# 结节推理调参旋钮（默认值为精度优先档）
#   threshold         — 结节 softmax 概率阈值：调高→误报(散点)减少、召回降低
#   smooth_sigma      — 概率图高斯平滑 σ（256³ 体素单位），压散点噪声。
#                       注意 σ=0.8 会把概率峰值 1.0 的单体素压到 ~0.12，
#                       小结节会被抹掉；σ=0.5 只衰减到 ~0.56，安全。
#   opening_radius    — 二值开运算半径（256³ 体素单位），去毛刺
#   min_voxels_256    — 256³ 尺度连通域最小体素数（去孤立噪点）
#   min_voxels_native — 原始分辨率连通域最小体素数（最终过滤）
#   tta               — 8 向翻转测试时增强。
#                       2026-08 在 RTX 3060 Ti + torch 2.12 上实测：该模型
#                       对翻转输入的 softmax 概率图彼此不一致（翻转平均后
#                       峰值从 1.0 掉到 0.63，再经平滑全部 <0.5，真结节被
#                       全部冲销）。模型官方验证指标也是单次 argmax 评测，
#                       因此默认关闭 TTA，与官方评测协议一致。
# -----------------------------------------------------------
DEFAULT_THRESHOLD = 0.60
DEFAULT_SMOOTH_SIGMA = 0.5
DEFAULT_OPENING_RADIUS = 1
DEFAULT_MIN_VOXELS_256 = 2
DEFAULT_MIN_VOXELS_NATIVE = 5
# 8 组翻转 TTA 的翻转组合（第 2/3/4 维 = D/H/W）
_FLIP_COMBOS = (
    (0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1),
    (1, 1, 0), (1, 0, 1), (0, 1, 1), (1, 1, 1),
)


def _progress(message):
    print(message, flush=True)


def _download_repo(repo_id, local_dir):
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "缺少 huggingface_hub，无法自动下载模型。请先安装 huggingface-hub。"
        ) from exc
    local_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id,
        local_dir=str(local_dir),
        allow_patterns=list(REQUIRED_FILES) + [".gitattributes"],
    )


def download_models():
    for label, repo_id, directory in (
        ("3D 肺结节 SegResNet", NODULE_REPO, NODULE_DIR),
        ("2D 肺区 ROI SegResNet", ROI_REPO, ROI_DIR),
    ):
        if all((directory / name).is_file() for name in REQUIRED_FILES):
            _progress("%s 已存在，跳过下载。" % label)
            continue
        _progress("正在从 Hugging Face 下载 %s……" % label)
        _download_repo(repo_id, directory)
        _progress("%s 下载完成。" % label)


def _load_config(path):
    import yaml

    with path.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def _build_segresnet(model_dir):
    import contextlib
    import io
    import torch

    # Some optional packages imported by MONAI print their search path on this
    # workstation.  Keep that unrelated diagnostic out of the GUI progress log.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        from monai.networks.nets import SegResNet

    cfg = _load_config(model_dir / "config.yaml")["model"]
    model = SegResNet(
        spatial_dims=int(cfg["spatial_dims"]),
        in_channels=int(cfg["in_channels"]),
        out_channels=int(cfg["out_channels"]),
        init_filters=int(cfg["init_filters"]),
        blocks_down=tuple(cfg["blocks_down"]),
        blocks_up=tuple(cfg["blocks_up"]),
        dropout_prob=float(cfg.get("dropout_prob", 0.0)),
    )
    state = torch.load(
        str(model_dir / "model.pth"), map_location="cpu", weights_only=True)
    if isinstance(state, dict) and "state_dict" in state:
        state = state["state_dict"]
    if state and all(str(key).startswith("module.") for key in state):
        state = {str(key)[7:]: value for key, value in state.items()}
    model.load_state_dict(state, strict=True)
    model.eval()
    return model


def _normalise_ct(volume):
    """Map the standard lung CT window [-1000, 400] HU to [0, 1]."""
    import numpy as np

    volume = np.nan_to_num(volume, nan=-1000.0, posinf=400.0, neginf=-1000.0)
    return np.clip((volume.astype(np.float32) + 1000.0) / 1400.0, 0.0, 1.0)


def _largest_lung_components(mask):
    """Retain at most the two dominant connected lung components."""
    import numpy as np
    from scipy import ndimage

    labelled, count = ndimage.label(mask, structure=np.ones((3, 3, 3), dtype=np.uint8))
    if count == 0:
        return np.zeros_like(mask, dtype=bool)
    sizes = np.bincount(labelled.ravel())
    sizes[0] = 0
    keep = [int(index) for index in np.argsort(sizes)[-2:] if sizes[index] >= 256]
    if not keep:
        return np.zeros_like(mask, dtype=bool)
    return np.isin(labelled, keep)


def _lung_bbox(volume, roi_model, device):
    """Infer an axial lung mask and return a padded bbox in D/H/W voxels."""
    import numpy as np
    import torch
    import torch.nn.functional as functional

    depth, height, width = volume.shape
    masks = []
    batch_size = 16 if device.type == "cuda" else 4
    _progress("正在定位肺区……")
    with torch.inference_mode():
        for start in range(0, depth, batch_size):
            stop = min(depth, start + batch_size)
            batch = torch.from_numpy(volume[start:stop, None]).to(device)
            batch = functional.interpolate(
                batch, size=(256, 256), mode="bilinear", align_corners=False)
            with torch.autocast(
                device_type=device.type,
                dtype=torch.float16,
                enabled=device.type == "cuda",
            ):
                probability = torch.sigmoid(roi_model(batch))
            masks.append((probability[:, 0] > 0.5).to("cpu", torch.uint8).numpy())
            _progress("肺区定位 %d/%d 层" % (stop, depth))

    roi_mask = _largest_lung_components(np.concatenate(masks, axis=0).astype(bool))
    points = np.argwhere(roi_mask)
    if not points.size:
        raise RuntimeError(
            "肺区模型没有找到有效肺组织。请确认输入是包含完整双肺的胸部 CT，"
            "并且像素值仍是 HU。"
        )

    low = points.min(axis=0)
    high = points.max(axis=0) + 1
    # ROI mask is D x 256 x 256. Map its in-plane bounds back to the CT grid.
    z0, z1 = int(low[0]), int(high[0])
    y0 = int(np.floor(low[1] * height / 256.0))
    y1 = int(np.ceil(high[1] * height / 256.0))
    x0 = int(np.floor(low[2] * width / 256.0))
    x1 = int(np.ceil(high[2] * width / 256.0))
    padding = 20
    return (
        max(0, z0 - padding), min(depth, z1 + padding),
        max(0, y0 - padding), min(height, y1 + padding),
        max(0, x0 - padding), min(width, x1 + padding),
    )


def _infer_nodule_probability(model, tensor, device, tta_enabled, use_amp):
    """Forward the nodule model and return softmax nodule probability.

    `tensor` is (1,1,D,H,W) already at model scale. `tta_enabled` switches
    on 8-flip averaging; note this model's flip probabilities disagree
    strongly with each other (see the notes at the top of this file), so
    TTA is OFF by default and single-pass softmax is the accurate path.
    """
    import torch

    prob_sum = None
    combos = _FLIP_COMBOS if tta_enabled else ((0, 0, 0),)
    with torch.inference_mode():
        for flips in combos:
            x = tensor
            flip_dims = [axis + 2 for axis, on in enumerate(flips) if on]
            if flip_dims:
                x = torch.flip(x, dims=flip_dims)
            with torch.autocast(
                device_type=device.type,
                dtype=torch.float16,
                enabled=use_amp and device.type == "cuda",
            ):
                logits = model(x)
            # fp32 softmax：fp16 的 exp 在阈值附近精度不足
            prob = torch.softmax(logits.float(), dim=1)[:, 1:2]
            if flip_dims:
                prob = torch.flip(prob, dims=flip_dims)
            prob_sum = prob if prob_sum is None else prob_sum + prob
            del x, logits
    return prob_sum / len(combos)


def _infer_nodule_probability_highres(model, crop, device, tta_enabled, use_amp):
    """Sliding-window inference at native crop resolution.

    Windows are 256³ (the training input size) with 0.5 overlap, so small
    nodules keep their native voxel detail instead of being squeezed by the
    global 256³ resample. The same flip-TTA averaging as the resample mode.
    """
    import torch
    from monai.inferers import sliding_window_inference
    from monai.utils import BlendMode

    def predictor(window):
        with torch.autocast(
            device_type=device.type,
            dtype=torch.float16,
            enabled=use_amp and device.type == "cuda",
        ):
            return model(window)

    tensor = torch.from_numpy(crop[None, None]).to(device)
    prob_sum = None
    combos = _FLIP_COMBOS if tta_enabled else ((0, 0, 0),)
    with torch.inference_mode():
        for flips in combos:
            x = tensor
            flip_dims = [axis + 2 for axis, on in enumerate(flips) if on]
            if flip_dims:
                x = torch.flip(x, dims=flip_dims)
            logits = sliding_window_inference(
                x, roi_size=(256, 256, 256), sw_batch_size=1,
                predictor=predictor, overlap=0.5,
                mode=BlendMode.CONSTANT, progress=False, device=device)
            prob = torch.softmax(logits.float(), dim=1)[:, 1:2]
            if flip_dims:
                prob = torch.flip(prob, dims=flip_dims)
            prob_sum = prob if prob_sum is None else prob_sum + prob
            del x, logits
    del tensor
    return prob_sum / len(combos)


def _postprocess_probability(prob, threshold, smooth_sigma, opening_radius):
    """Probability map -> clean binary mask.

    1. Gaussian-smooth the probability field (kills salt-and-pepper specks)
    2. Threshold at `threshold` (argmax is the special case 0.5)
    3. Morphological opening (removes single-voxel spikes and thin bridges)
    """
    import numpy as np
    from scipy import ndimage

    prob = np.asarray(prob, dtype=np.float32)
    if smooth_sigma:
        sigma = tuple(float(smooth_sigma) for _ in range(prob.ndim)) \
            if not isinstance(smooth_sigma, (tuple, list)) else tuple(smooth_sigma)
        prob = ndimage.gaussian_filter(prob, sigma=sigma)
    mask = (prob >= float(threshold)).astype(np.uint8)
    if opening_radius and int(opening_radius) > 0:
        mask = ndimage.binary_opening(
            mask,
            structure=ndimage.generate_binary_structure(3, 1),
            iterations=int(opening_radius),
        ).astype(np.uint8)
    return mask, prob


def _remove_tiny_components(mask, minimum_voxels=2):
    import numpy as np
    from scipy import ndimage

    labelled, count = ndimage.label(mask, structure=np.ones((3, 3, 3), dtype=np.uint8))
    if count == 0:
        return np.zeros_like(mask, dtype=np.uint8)
    sizes = np.bincount(labelled.ravel())
    keep = np.flatnonzero(sizes >= int(minimum_voxels))
    keep = keep[keep != 0]
    return np.isin(labelled, keep).astype(np.uint8)


def segment(input_path, output_dir, device_name,
            threshold=DEFAULT_THRESHOLD,
            tta="off",
            smooth_sigma=DEFAULT_SMOOTH_SIGMA,
            opening_radius=DEFAULT_OPENING_RADIUS,
            min_voxels_256=DEFAULT_MIN_VOXELS_256,
            min_voxels_native=DEFAULT_MIN_VOXELS_NATIVE,
            mode="resample"):
    import nibabel as nib
    import numpy as np
    import torch
    import torch.nn.functional as functional
    from scipy import ndimage

    if not 0.0 <= float(threshold) <= 1.0:
        raise ValueError("--threshold 必须在 0 到 1 之间。")
    if float(smooth_sigma) < 0.0:
        raise ValueError("--smooth-sigma 不能小于 0。")
    if int(opening_radius) < 0:
        raise ValueError("--opening 不能小于 0。")
    if int(min_voxels_256) < 1 or int(min_voxels_native) < 1:
        raise ValueError("最小连通域体素数必须大于等于 1。")
    if tta not in ("auto", "on", "off"):
        raise ValueError("--tta 只支持 auto、on 或 off。")
    if mode not in ("resample", "highres"):
        raise ValueError("--mode 只支持 resample（256³ 重采样，与训练一致）或 highres（原始分辨率滑窗）。")
    download_models()
    if device_name == "gpu":
        if not torch.cuda.is_available():
            raise RuntimeError("选择了 GPU，但当前 PyTorch 未检测到 CUDA 显卡。")
        device = torch.device("cuda:0")
    else:
        device = torch.device("cpu")
    tta_enabled = (tta == "on") or (tta == "auto" and device.type == "cuda")
    _progress("推理设备：%s" % (torch.cuda.get_device_name(0) if device.type == "cuda" else "CPU"))
    _progress("结节参数：threshold=%.2f tta=%s mode=%s smooth=%.2f opening=%d 最小体素=%d/%d" % (
        float(threshold), "on" if tta_enabled else "off", mode,
        float(smooth_sigma), int(opening_radius),
        int(min_voxels_256), int(min_voxels_native)))

    image = nib.load(str(input_path))
    source_xyz = np.asarray(image.dataobj, dtype=np.float32)
    if source_xyz.ndim != 3:
        raise RuntimeError("肺结节模型仅支持单通道三维 CT。")
    # NIfTI/VTK uses X/Y/Z while the networks use D/H/W = Z/Y/X.
    source = source_xyz.transpose(2, 1, 0)
    normalised = _normalise_ct(source)

    roi_model = _build_segresnet(ROI_DIR).to(device)
    bbox = _lung_bbox(normalised, roi_model, device)
    del roi_model
    if device.type == "cuda":
        torch.cuda.empty_cache()
    z0, z1, y0, y1, x0, x1 = bbox
    _progress("肺区范围：Z %d:%d，Y %d:%d，X %d:%d" % bbox)

    crop = normalised[z0:z1, y0:y1, x0:x1]
    nodule_model = _build_segresnet(NODULE_DIR).to(device)
    _progress("正在执行 3D 肺结节分割……")
    if mode == "highres":
        # 滑窗在原始分辨率上推理：小结节不被全局 256³ 降采样抹糊。
        prob = _infer_nodule_probability_highres(
            nodule_model, crop, device, tta_enabled, use_amp=True)
        prob = prob[0, 0].to("cpu", torch.float32).numpy()
        # 平滑 σ 按 crop/256 的几何比例放大到原生分辨率
        sigma = tuple(max(0.5, float(smooth_sigma) * side / 256.0) for side in crop.shape)
        restored, _ = _postprocess_probability(
            prob, threshold, sigma, opening_radius)
    else:
        tensor = torch.from_numpy(crop[None, None]).to(device)
        tensor = functional.interpolate(
            tensor, size=(256, 256, 256), mode="trilinear", align_corners=False)
        prob = _infer_nodule_probability(
            nodule_model, tensor, device, tta_enabled, use_amp=True)
        prob = prob[0, 0].to("cpu", torch.float32).numpy()
        del tensor
        prediction, _ = _postprocess_probability(
            prob, threshold, smooth_sigma, opening_radius)
        prediction = _remove_tiny_components(
            prediction, minimum_voxels=int(min_voxels_256))
        zoom = tuple(float(target) / 256.0 for target in crop.shape)
        restored = ndimage.zoom(prediction, zoom=zoom, order=0, prefilter=False)
        restored = restored[: crop.shape[0], : crop.shape[1], : crop.shape[2]]
        if restored.shape != crop.shape:
            padded = np.zeros(crop.shape, dtype=np.uint8)
            common = tuple(slice(0, min(a, b)) for a, b in zip(restored.shape, crop.shape))
            padded[common] = restored[common]
            restored = padded
    del nodule_model, prob
    if device.type == "cuda":
        torch.cuda.empty_cache()

    result = np.zeros(source.shape, dtype=np.uint8)
    result[z0:z1, y0:y1, x0:x1] = restored
    result = _remove_tiny_components(result, minimum_voxels=int(min_voxels_native))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "lung_nodule.nii.gz"
    output_xyz = result.transpose(2, 1, 0)
    output_header = image.header.copy()
    output_header.set_data_dtype(np.uint8)
    output_header.set_slope_inter(1.0, 0.0)
    nib.save(nib.Nifti1Image(output_xyz, image.affine, output_header), str(output_path))
    count = int(result.sum())
    labelled, components = ndimage.label(
        result, structure=ndimage.generate_binary_structure(3, 1))
    _progress("肺结节分割完成：%d 个阳性体素，%d 个独立区域。" % (count, components))
    gc.collect()


def self_test(device_name):
    import torch

    download_models()
    device = torch.device(
        "cuda:0" if device_name == "gpu" and torch.cuda.is_available() else "cpu")
    roi = _build_segresnet(ROI_DIR)
    nodule = _build_segresnet(NODULE_DIR)
    roi_params = sum(parameter.numel() for parameter in roi.parameters())
    nodule_params = sum(parameter.numel() for parameter in nodule.parameters())
    _progress("ROI 模型参数：%d" % roi_params)
    _progress("结节模型参数：%d" % nodule_params)
    _progress("MONAI 肺结节模型自检通过（%s）。" % device)


def main():
    # Windows 控制台默认 GBK，遇到"³"等字符会直接崩溃，中文也会乱码；
    # GUI 侧按 UTF-8 解码，这里统一按 UTF-8 输出。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--device", choices=("gpu", "cpu"), default="gpu")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help="结节 softmax 概率阈值（默认 0.60，精度优先）")
    parser.add_argument("--tta", choices=("auto", "on", "off"), default="off",
                        help="8 向翻转测试时增强（默认关闭；本模型翻转平均会冲销概率，见文件头注释）")
    parser.add_argument("--mode", choices=("resample", "highres"), default="resample",
                        help="resample=肺区重采样 256³（与训练一致）；highres=原始分辨率滑窗")
    parser.add_argument("--smooth-sigma", type=float, default=DEFAULT_SMOOTH_SIGMA,
                        help="概率图高斯平滑 σ（0 关闭）")
    parser.add_argument("--opening", type=int, default=DEFAULT_OPENING_RADIUS,
                        help="二值开运算半径（0 关闭）")
    parser.add_argument("--min-voxels-256", type=int, default=DEFAULT_MIN_VOXELS_256)
    parser.add_argument("--min-voxels-native", type=int, default=DEFAULT_MIN_VOXELS_NATIVE)
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        if args.download_only:
            download_models()
            return 0
        if args.self_test:
            self_test(args.device)
            return 0
        if not args.input or not args.output:
            parser.error("--input and --output are required for inference")
        segment(
            args.input, args.output, args.device,
            threshold=args.threshold, tta=args.tta,
            smooth_sigma=args.smooth_sigma, opening_radius=args.opening,
            min_voxels_256=args.min_voxels_256,
            min_voxels_native=args.min_voxels_native,
            mode=args.mode,
        )
        return 0
    except Exception as exc:
        _progress("ERROR: %s" % exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
