# CTto3D

Convert CT (DICOM) slices — or plain stacks of image slices — into an
interactive **3D volume rendering** for clinical review. Built on
[VTK](https://vtk.org) (the same open-source rendering engine behind 3D Slicer)
with a clean [PySide6/Qt](https://doc.qt.io/qtforpython/) interface.

![workflow](docs/screenshot.png)

> 界面为简体中文 (Chinese UI), suitable for use in a hospital setting.

## Features

- **Load DICOM series** — recursively reads a folder of `.dcm` files, sorts the
  slices, and rescales pixel values to true Hounsfield Units.
- **Load image stacks** — ordered PNG / JPG / TIFF / BMP slices (sub-folders too).
- **Import ZIP archives** (导入 ZIP 压缩包) — load a `.zip` straight from a
  patient CD; the contents are extracted to a temp folder and **auto-detected**
  as a DICOM series or an image stack. Extraction is guarded against unsafe paths.
- **Per-tissue layers** (组织·勾选显示·单独调透明度) — everything shows by
  default; each tissue has its own **show/hide checkbox _and_ opacity slider**,
  so you can e.g. fade the skin to 20% while keeping bone solid. 全选 / 全不选
  (all / none) buttons included. CT tissues: 软组织 / 脂肪 / 肺部 / 血管·造影 /
  骨骼; image stacks: 低 / 中 / 高密度. HU windows and colours follow public
  references (Hounsfield scale + **3D Slicer** CT presets) for clean separation.
- **Transparent ↔ Opaque** (透明 / 不透明) — one-click toggle plus a fine opacity
  slider, so you can see through to inner structures or render a solid surface.
- **Z proportion control** (Z 比例) — corrects slice-spacing so the model isn't
  stretched or flattened (DICOM is auto-calibrated; handy for image stacks).
- **Interactive 3D view** — rotate, pan, zoom with the mouse; orientation marker.
- **Export** — save a high-resolution screenshot, or extract a printable
  **3D mesh** (STL / OBJ / PLY) via marching-cubes iso-surfacing.
- **Demo phantom** (演示体模) — a synthetic torso (body + lungs + spine) to try
  the app without any patient data.

## Install

```powershell
cd C:\Users\Administrator\Desktop\CTTO3D
python -m pip install -r requirements.txt
```

## Run

```powershell
python main.py
```

1. Click **加载演示体模** (demo phantom), or **打开 DICOM 文件夹** /
   **打开图片序列** / **导入 ZIP 压缩包**.
2. In **组织** , untick a tissue to hide it, or drag its slider to set that
   tissue's opacity (全选 / 全不选 = all / none).
3. Toggle **不透明 / 透明** (Opaque / Transparent master) or drag the 不透明度 slider.
4. Drag in the 3D view to rotate. Export a screenshot (保存截图) or STL model
   (导出三维模型).

## Notes

- Compressed (JPEG-LOSSLESS) DICOM may require an extra codec:
  `python -m pip install pylibjpeg pylibjpeg-libjpeg gdcm`.
- Image stacks have no real-world scale; set slice thickness in
  `loader.load_image_stack(..., slice_spacing=...)` if needed.
- GPU volume rendering is used when available, falling back to CPU automatically.

## References

Transfer functions and Hounsfield-Unit thresholds are based on public sources:

- 3D Slicer CT volume-rendering presets — `presets.xml`
  (https://github.com/Slicer/Slicer/blob/main/Modules/Loadable/VolumeRendering/Resources/presets.xml)
- Hounsfield scale reference values
  (https://en.wikipedia.org/wiki/Hounsfield_scale)

## Project layout

```
main.py                 entry point
ctto3d/loader.py        DICOM / image-stack / phantom loading -> vtkImageData
ctto3d/presets.py       organ transfer-function presets (color + opacity)
ctto3d/viewer.py        VTK GPU volume-rendering viewport (Qt widget)
ctto3d/mainwindow.py    UI layout, controls, export
ctto3d/style.py         Qt stylesheet (clinical theme)
```
