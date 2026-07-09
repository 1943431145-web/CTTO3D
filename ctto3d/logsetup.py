"""
应用日志系统：滚动日志文件 + 控制台输出 + Qt消息路由 + 崩溃捕获

============================================================
模块功能
============================================================
在程序启动时（创建 QApplication 之前）调用 setup_logging() 即可配置
完整的日志记录系统，包括：

1. 滚动日志文件 — 项目根目录 logs/ctto3d.log，约 1MB × 3 份
2. 控制台输出 — 仅在有控制台时才启用（pythonw 模式下自动跳过）
3. Qt 消息路由 — 将 Qt 的 qDebug/qWarning/qCritical 等消息转发到 Python 日志
4. 崩溃捕获 — 未捕获异常自动记录到日志文件

日志存储位置：
  默认在项目根目录下的 logs/ 文件夹
  若该目录不可写，自动回退到系统临时目录（%TEMP%/ctto3d_logs/）

修改方法：
  - 日志文件名：修改 LOG_FILENAME
  - 单文件最大大小：修改 _MAX_BYTES
  - 保留旧日志份数：修改 _BACKUP_COUNT
  - 日志格式：修改 _FORMAT 和 _DATEFMT
  - 屏蔽特定 Qt 警告：在 _MUTED_QT 元组中添加关键字片段
============================================================
"""

import logging
import logging.handlers
import os
import sys
import tempfile

# 日志文件名
LOG_FILENAME = "ctto3d.log"
# 单个日志文件最大字节数 ≈1MB（超过后自动轮转）
_MAX_BYTES = 1_000_000
# 保留的旧日志文件份数（ctto3d.log + .1 .2 .3）
_BACKUP_COUNT = 3
# 日志格式：时间 级别 [模块名] 消息内容
_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

# 需要静默的 Qt 消息关键字（这些在本应用中是无害的常规提示）
_MUTED_QT = (
    "must be a top level window",
    "setDarkBorderToWindow",
)

_configured = False  # 防止重复初始化


def _log_dir():
    """获取日志文件存放目录。
    
    优先使用项目根目录下的 logs/ 文件夹，
    若不可写则回退到系统临时目录。
    
    这种设计确保了：
      - 开发阶段日志在项目文件夹中易于查找
      - 打包发布后即使安装目录只读也能正常记录
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    primary = os.path.join(root, "logs")
    try:
        os.makedirs(primary, exist_ok=True)
        if os.access(primary, os.W_OK):
            return primary
    except OSError:
        pass
    # 回退：系统临时目录
    fallback = os.path.join(tempfile.gettempdir(), "ctto3d_logs")
    os.makedirs(fallback, exist_ok=True)
    return fallback


def log_path():
    """获取当前活动日志文件的绝对路径（只读查询，不会创建新文件）。"""
    return os.path.join(_log_dir(), LOG_FILENAME)


def setup_logging(level=logging.INFO):
    """初始化日志系统（仅需调用一次）。
    
    配置内容：
      1. 设置根日志级别
      2. 添加滚动文件处理器（含大小限制和备份轮转）
      3. 添加控制台处理器（如可用）
      4. 注册 sys.excepthook 崩溃捕获
      5. 注册 Qt 消息处理器
    
    参数：
      level: 日志级别，默认 logging.INFO
    
    返回：
      日志文件路径（str），或 None（无法创建文件时）
    """
    global _configured
    path = log_path()
    if _configured:
        return path

    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    # 创建滚动文件处理器
    try:
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        path = None  # 无法创建文件 → 仅使用控制台输出

    # 控制台处理器（pythonw/双击启动时 sys.stderr 为 None，自动跳过）
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        root.addHandler(console)

    # 注册崩溃捕获和 Qt 消息路由
    _install_excepthook()
    install_qt_message_handler()

    _configured = True
    if path:
        logging.getLogger(__name__).info(
            "日志已启动 -> %s (滚动 %d 份 × %d 字节)", path, _BACKUP_COUNT, _MAX_BYTES)
    else:
        logging.getLogger(__name__).warning("无法写入日志文件,仅输出到控制台。")
    return path


def _install_excepthook():
    """安装全局异常捕获钩子。
    
    当程序触发未捕获的异常时（未 try-except 的致命错误），
    自动将异常信息记录到 crash 日志（crash logger），
    然后执行默认的异常处理（打印 traceback）。
    
    KeyboardInterrupt（Ctrl+C）不会被捕获，直接走默认处理。
    """
    crash_log = logging.getLogger("crash")
    previous = sys.excepthook

    def hook(exc_type, exc_value, exc_tb):
        # Ctrl+C 不记录为崩溃
        if issubclass(exc_type, KeyboardInterrupt):
            previous(exc_type, exc_value, exc_tb)
            return
        # 记录完整的异常堆栈到日志
        crash_log.critical("未捕获异常", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = hook


def install_qt_message_handler():
    """安装 Qt 消息处理器，将 Qt 的内部消息路由到 Python 日志系统。
    
    功能：
      - QtDebugMsg    → logging.DEBUG
      - QtInfoMsg     → logging.INFO
      - QtWarningMsg  → logging.WARNING
      - QtCriticalMsg → logging.ERROR
      - QtFatalMsg    → logging.CRITICAL + os.abort()（记录后终止进程）
    
    一些无害的 Qt 警告（如窗口层级提示）会被静默处理，
    避免日志中产生大量无意义输出。
    静默规则见模块顶部的 _MUTED_QT 元组。
    """
    from PySide6 import QtCore

    qt_log = logging.getLogger("qt")
    # Qt 消息级别 → Python 日志级别映射
    level_of = {
        QtCore.QtMsgType.QtDebugMsg: logging.DEBUG,
        QtCore.QtMsgType.QtInfoMsg: logging.INFO,
        QtCore.QtMsgType.QtWarningMsg: logging.WARNING,
        QtCore.QtMsgType.QtCriticalMsg: logging.ERROR,
        QtCore.QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(mode, context, message):
        # 静默处理某些无害的 Qt 消息
        if any(snippet in message for snippet in _MUTED_QT):
            return
        qt_log.log(level_of.get(mode, logging.INFO), "%s", message)
        # Fatal 消息必须在记录后中止进程（Qt 的默认行为）
        if mode == QtCore.QtMsgType.QtFatalMsg:
            os.abort()

    QtCore.qInstallMessageHandler(handler)
