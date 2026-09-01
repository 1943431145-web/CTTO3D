"""
程序入口模块 - CTto3D 消融手术规划系统

============================================================
功能概述
============================================================
本程序将 CT 扫描图像（DICOM 格式）或普通图片序列转换为交互式
三维体渲染，支持多组织分层显示、消融针规划与消融范围仿真。

主要功能：
  1. 加载 DICOM 文件夹 / 图片序列 / ZIP 压缩包 / 演示体模
  2. 三维体渲染 + 三向正交切片（轴状/冠状/矢状位）
  3. 按组织类型分层显示（骨骼/血管/肌肉/脂肪/肺气腔等）
  4. 消融针规划（入针点/针尖位置、多款针型预设）
  5. 消融范围仿真（随时间生长的椭球体凝固模型）
  6. 深色/浅色主题切换
  7. 截图导出（PNG）和三维模型导出（STL/OBJ/PLY）

运行方式：
  python main.py
  或双击 main.py（需关联 Python 解释器）
============================================================
"""

import logging
import os
import sys

# -----------------------------------------------------------
# 本地库路径支持：当系统 pip 无法写入 site-packages 时，
# 允许 PySide6 从项目目录下的 .applibs 文件夹加载
# -----------------------------------------------------------
_ROOT = os.path.dirname(os.path.abspath(__file__))
_LOCAL_LIBS = os.path.join(_ROOT, ".applibs")
if os.path.isdir(_LOCAL_LIBS) and _LOCAL_LIBS not in sys.path:
    sys.path.insert(0, _LOCAL_LIBS)

from PySide6 import QtWidgets


def main():
    """
    应用程序主入口函数。

    执行流程：
      1. 初始化日志
      2. 展示跟随当前主题的 HealthLink 品牌启动界面
      3. 加载主题与主窗口，一次性初始化 VTK 后再揭开
      4. 进入事件循环

    若需修改启动行为：
      - 启动界面：修改 ctto3d/startup.py
      - 默认主题：参见 style.py 中的 DEFAULT_THEME 变量
      - 窗口标题：修改 app.setApplicationName() 和 MainWindow 的 setWindowTitle()
      - 启动显示模式：修改下方 window.showFullScreen()
    """
    from ctto3d import logsetup
    logsetup.setup_logging()

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("HealthLink 消融手术规划系统")
    app.setApplicationDisplayName("HealthLink 消融手术规划系统")
    app.setOrganizationName("苏州海思临科医学科技有限公司")

    from ctto3d import style
    from ctto3d.startup import show_startup_cover

    theme = style.load_theme()
    style.apply_theme(app, theme)
    cover = show_startup_cover(app, theme)
    cover.set_status("正在加载三维医学影像引擎…", 22)

    import vtk
    vtk.vtkObject.GlobalWarningDisplayOff()
    from ctto3d.mainwindow import MainWindow

    cover.set_status("正在创建临床规划工作区…", 52)
    window = MainWindow()
    # VTK 必须在窗口映射后才能初始化；先让整个主窗口完全透明，
    # 避免居中启动页四周提前露出正在分块绘制的界面。
    window.setWindowOpacity(0.0)
    window.showFullScreen()
    cover.set_status("正在渲染三维视图与切片…", 76)
    window.prepare_first_frame()
    cover.set_status("准备完成", 100)

    # 主窗口首帧已经完整合成，可以安全地协调淡入主界面并淡出启动卡片。
    cover.finish(window)
    window.raise_()
    window.activateWindow()

    logging.getLogger("ctto3d").info("应用已启动(主题=%s)", theme)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
