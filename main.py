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
_LOCAL_LIBS = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".applibs")
if os.path.isdir(_LOCAL_LIBS) and _LOCAL_LIBS not in sys.path:
    sys.path.insert(0, _LOCAL_LIBS)

from PySide6 import QtWidgets
import vtk

from ctto3d.mainwindow import MainWindow
from ctto3d import logsetup, style


def main():
    """
    应用程序主入口函数。

    执行流程：
      1. 初始化日志系统（在创建 QApplication 之前，确保启动报错也能记录）
      2. 关闭 VTK 的全局警告（避免控制台刷屏）
      3. 创建 QApplication 实例
      4. 加载用户上次选择的主题（默认深色），应用主题样式
      5. 创建主窗口并最大化显示
      6. 进入 Qt 事件循环（阻塞等待用户操作）

    若需修改启动行为：
      - 默认主题：参见 style.py 中的 DEFAULT_THEME 变量
      - 窗口标题：修改 app.setApplicationName() 和 MainWindow 的 setWindowTitle()
      - 窗口初始大小：修改 MainWindow.__init__ 中的 self.resize(w, h)
    """
    # 日志最先初始化，确保 Qt 消息和启动期间的崩溃都能被捕获
    logsetup.setup_logging()
    # 屏蔽 VTK 全局警告（避免控制台输出大量底层库警告）
    vtk.vtkObject.GlobalWarningDisplayOff()

    # 创建 Qt 应用程序对象
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("消融手术规划系统")

    # 加载用户记忆的主题（默认深色"dark"），可在运行时通过「视图→主题」菜单切换
    # 如需修改默认主题为浅色，将 style.py 中的 DEFAULT_THEME 改为 "light"
    theme = style.load_theme()
    style.apply_theme(app, theme)

    # 创建主窗口，最大化显示（保留标题栏和系统控件）
    window = MainWindow()
    window.showMaximized()
    logging.getLogger("ctto3d").info("应用已启动(主题=%s)", theme)

    # 进入 Qt 事件主循环，程序在此阻塞直到用户关闭窗口
    # sys.exit 确保返回码正确传递给操作系统
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
