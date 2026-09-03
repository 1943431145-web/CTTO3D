# CTto3D / HealthLink 消融手术规划系统

Convert CT (DICOM) slices — or plain stacks of image slices — into an
interactive **3D volume rendering** for clinical review. Built on
[VTK](https://vtk.org) (the same open-source rendering engine behind 3D Slicer)
with a [PySide6/Qt](https://doc.qt.io/qtforpython/) interface. The application
also integrates organ and lung-nodule segmentation, needle-path planning,
ablation-zone visualization, planning reports, and device communication.

> 界面为简体中文。当前规划流程：加载影像 → 自动分割 → 定位结节 →
> 规划针道 → 仿真并导出。分割可跳过，步骤徽章和前后按钮可随时切换页面。

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
- **Orthogonal slices** — axial, coronal, and sagittal views with linked
  crosshairs, window/level controls, and expanded slice views. Double-click the
  3D view to expand or restore it.
- **Optional AI segmentation** — TotalSegmentator organ masks and MONAI lung
  nodule segmentation run in child processes. Nodule cards show component
  volume and estimated depth; select a card to locate it in the views.
- **Needle-path planning** — place entry and target points, inspect path length
  and direction angles, and request a reachable skin entry with CT-based bone
  collision and clearance checks.
- **Ablation visualization** — needle presets, configurable geometry, power,
  duration, and a growing ellipsoidal ablation zone. The simplified growth model
  is for planning visualization and is not a validated thermal-dose model.
- **Planning checklist** — export one HTML file containing patient metadata,
  nodule measurements, planning parameters, and embedded 3D/slice screenshots.
  Open it in a browser to print or save as PDF.
- **Device integration** — Qt serial-port communication, RTX sensor telemetry,
  HW100 microwave ablator control, and TCP/SCPI network-analyzer communication.
- **UI and performance** — light/dark themes, a five-step planning wizard,
  coalesced opacity updates, telemetry render thresholds, and reduced memory
  copies during CT normalization and mask loading.

## Install

Run these commands from the project root. Create the virtual environment only
on first setup; an existing checkout may already contain `.venv`.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The base requirements are VTK, PySide6, pydicom, Pillow, and NumPy. The desktop
application starts without the optional AI packages.

For organ segmentation, install TotalSegmentator and make its command available
on `PATH`. Lung-nodule inference additionally uses PyTorch, MONAI, SciPy,
NiBabel, Hugging Face Hub, and PyYAML. The GUI uses the Python environment of
the TotalSegmentator command when available, otherwise its own Python; install
the AI dependencies in that inference environment. GPU inference also requires
a compatible PyTorch/CUDA installation.

Use **下载模型** in the segmentation page to prepare the selected models.
Weights are cached under `model/` and are not included in Git.

## Run

```powershell
.\.venv\Scripts\python.exe main.py
```

1. Import a DICOM folder, image stack, ZIP, or demo phantom, then load the
   selected item from the dataset list.
2. Optionally choose an AI preset and run segmentation. Use the 3D context menu
   **组织** to control individual tissue visibility and opacity.
3. Select a nodule card to locate it, or position the crosshair manually.
   Double-click a card or press Enter to open the corresponding expanded slice.
4. Place the ablation target and an entry point, or request an automatic
   bone-avoiding entry. Review the displayed path length and direction angles.
5. Set needle and simulation parameters, run the visualization, and export the
   planning checklist. Screenshots and STL/OBJ/PLY exports are also available.

## Tests

Run the regression suite without showing Qt windows:

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests cover planning geometry, nodule localization, report generation, NIfTI
mask lifetime/geometry, UI update coalescing, DICOM orientation, and device
protocols. Two nodule post-processing tests are skipped when SciPy is absent.
These tests do not exercise model inference, connected hardware, or interactive
GPU rendering.

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
ctto3d/mainwindow.py     planning wizard, dataset workflow, device UI, export
ctto3d/viewer.py         3D rendering, orthogonal slices, needle/zone overlays
ctto3d/loader.py         DICOM / image-stack / ZIP / phantom loading
ctto3d/presets.py        tissue color and opacity transfer functions
ctto3d/segmentation.py   AI process orchestration, NIfTI masks, component stats
ctto3d/totalseg_runner.py       sequential TotalSegmentator inference runner
ctto3d/monai_nodule_runner.py   MONAI lung ROI and nodule inference runner
ctto3d/needle_planning.py       bone-path checks and entry recommendation
ctto3d/ablation.py             needle presets and ablation-zone growth model
ctto3d/planning_report.py      standalone HTML planning checklist generation
ctto3d/serial_connection.py    generic Qt serial-port connection
ctto3d/rtx_telemetry.py        RTX telemetry parser and acknowledgement
ctto3d/hw100_protocol.py       HW100 binary protocol
ctto3d/microwave_ablator.py    HW100 controller and connection state
ctto3d/network_analyzer.py    TCP/SCPI network-analyzer connection
ctto3d/startup.py        startup cover and first-frame transition
ctto3d/style.py          light/dark Qt stylesheets
ctto3d/logsetup.py       application logging
ctto3d/assets/          icons and needle mesh assets
tests/                  regression tests
开发实施计划.md          development roadmap (planned scope, not completion status)
```

Local CT data (`CT_DATA/`), model weights (`model/`), environments (`.venv/`,
`.applibs/`), logs, tuning outputs, and temporary files are excluded by
`.gitignore`.
