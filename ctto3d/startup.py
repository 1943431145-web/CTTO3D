"""HealthLink 品牌启动界面。"""

import os

from PySide6 import QtCore, QtGui, QtWidgets


_ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
LOGO_PATH = os.path.join(_ASSET_DIR, "app_logo_hq.png")
ICON_PATH = os.path.join(_ASSET_DIR, "app.ico")


def _rounded_app_icon(radius_ratio=0.20):
    """从 app.ico 生成圆角版多尺寸图标（标题栏/任务栏用）。

    原图标是四角不透明的方形实底，显示出来就是一个方块；这里按现代
    App 图标惯例裁出圆角（半径约为边长的 20%），并预渲染 16~256 常用
    尺寸，Windows 在任意缩放下都能拿到清晰位图。加载失败返回空图标。
    """
    icon = QtGui.QIcon()
    source = QtGui.QImage(ICON_PATH)
    if source.isNull():
        return icon
    source = source.convertToFormat(
        QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    for size in (16, 24, 32, 48, 64, 128, 256):
        img = source.scaled(
            size, size,
            QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation)
        radius = max(1, round(size * radius_ratio))
        out = QtGui.QImage(
            size, size, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        out.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(out)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        path = QtGui.QPainterPath()
        path.addRoundedRect(0, 0, size, size, radius, radius)
        painter.setClipPath(path)
        painter.drawImage(0, 0, img)
        painter.end()
        icon.addPixmap(QtGui.QPixmap.fromImage(out))
    return icon


def _load_hidpi_logo(path, max_logical_size=176):
    """按屏幕物理像素生成一次 Logo，避免 Qt 二次放大低 DPI 位图。"""
    source = QtGui.QPixmap(path) if os.path.isfile(path) else QtGui.QPixmap()
    if source.isNull():
        return source
    screen = QtGui.QGuiApplication.primaryScreen()
    dpr = max(1.0, float(screen.devicePixelRatio()) if screen else 1.0)
    # 高分辨率源图按屏幕物理像素一次缩小到位，避免 QLabel 再次缩放
    # 而破坏透明边缘的抗锯齿效果。
    logical_size = min(
        float(max_logical_size),
        max(128.0, source.width() * 1.35 / dpr),
    )
    physical_size = max(1, int(round(logical_size * dpr)))
    if source.width() != physical_size or source.height() != physical_size:
        source = source.scaled(
            physical_size,
            physical_size,
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
    source.setDevicePixelRatio(dpr)
    return source


THEMES = {
    "dark": {
        "bg_top": "#050909",
        "bg_bottom": "#071615",
        "text": "#F5FAFA",
        "muted": "#8FA6A4",
        "soft": "#BED0CE",
        "accent": "#4EC43B",
        "accent_2": "#23A9D6",
        "track": "rgba(255, 255, 255, 24)",
    },
    "light": {
        "bg_top": "#FCFEFE",
        "bg_bottom": "#EAF4F2",
        "text": "#112927",
        "muted": "#647976",
        "soft": "#405C58",
        "accent": "#43B52F",
        "accent_2": "#168EB8",
        "track": "rgba(18, 69, 64, 22)",
    },
}


class StartupCover(QtWidgets.QWidget):
    """在主窗口准备完成前显示的居中品牌启动窗口。"""

    WINDOW_SIZE = QtCore.QSize(896, 504)
    CORNER_RADIUS = 24.0

    def __init__(self, theme="dark", parent=None):
        super().__init__(parent)
        self._theme = theme if theme in THEMES else "dark"
        self._colors = THEMES[self._theme]
        self._transition = None
        self.setObjectName("StartupCover")
        self.setWindowFlags(
            QtCore.Qt.WindowType.FramelessWindowHint
            | QtCore.Qt.WindowType.WindowStaysOnTopHint
            | QtCore.Qt.WindowType.Tool
        )
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowTitle("HealthLink")
        if os.path.isfile(ICON_PATH):
            self.setWindowIcon(_rounded_app_icon())

        self.setFixedSize(self.WINDOW_SIZE)
        self._center_on_screen()
        self._build_ui()

    def _center_on_screen(self):
        """把启动窗口放在主屏幕可用区域中央。"""
        screen = QtWidgets.QApplication.primaryScreen()
        area = (
            screen.availableGeometry()
            if screen
            else QtCore.QRect(0, 0, 1280, 800)
        )
        geometry = QtCore.QRect(QtCore.QPoint(0, 0), self.WINDOW_SIZE)
        geometry.moveCenter(area.center())
        self.move(geometry.topLeft())

    def _build_ui(self):
        c = self._colors
        self.setStyleSheet(
            """
            QWidget#StartupCover { background: transparent; }
            QLabel { background: transparent; border: none; }
            QLabel#BrandName {
                color: %s;
                font-family: "Segoe UI", "Microsoft YaHei UI";
                font-size: 40pt;
                font-weight: 700;
            }
            QLabel#CompanyName {
                color: %s;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei";
                font-size: 13pt;
                font-weight: 500;
            }
            QLabel#ProductName {
                color: %s;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei";
                font-size: 19pt;
                font-weight: 650;
            }
            QLabel#ProductEnglish {
                color: %s;
                font-family: "Segoe UI", "Microsoft YaHei UI";
                font-size: 8.5pt;
                font-weight: 600;
            }
            QLabel#LoadingText {
                color: %s;
                font-family: "Microsoft YaHei UI", "Microsoft YaHei";
                font-size: 9.5pt;
                font-weight: 500;
            }
            QLabel#Footer {
                color: %s;
                font-family: "Segoe UI", "Microsoft YaHei UI";
                font-size: 8.5pt;
            }
            QProgressBar#StartupProgress {
                min-height: 4px;
                max-height: 4px;
                background: %s;
                border: none;
                border-radius: 2px;
            }
            QProgressBar#StartupProgress::chunk {
                background: %s;
                border-radius: 2px;
            }
            """
            % (
                c["text"], c["soft"], c["text"], c["muted"],
                c["muted"], c["muted"], c["track"], c["accent"],
            )
        )

        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(54, 34, 48, 25)
        root.setSpacing(0)
        root.addStretch(1)

        content = QtWidgets.QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(0)

        logo = QtWidgets.QLabel()
        logo.setFixedSize(230, 230)
        logo.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        pixmap = _load_hidpi_logo(LOGO_PATH)
        if not pixmap.isNull():
            logo.setPixmap(pixmap)
        content.addWidget(logo, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
        content.addSpacing(36)

        divider = QtWidgets.QFrame()
        divider.setFixedSize(2, 226)
        divider.setStyleSheet(
            "background: %s; border: none; border-radius: 1px;" % c["accent"]
        )
        content.addWidget(divider, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
        content.addSpacing(42)

        info = QtWidgets.QWidget()
        info_lay = QtWidgets.QVBoxLayout(info)
        info_lay.setContentsMargins(0, 0, 0, 0)
        info_lay.setSpacing(0)

        brand = QtWidgets.QLabel("HealthLink", objectName="BrandName")
        brand.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        info_lay.addWidget(brand)
        info_lay.addSpacing(1)

        company = QtWidgets.QLabel(
            "苏州海思临科医学科技有限公司", objectName="CompanyName"
        )
        company.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        info_lay.addWidget(company)
        info_lay.addSpacing(26)

        product = QtWidgets.QLabel("消融手术规划系统", objectName="ProductName")
        product.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        info_lay.addWidget(product)
        info_lay.addSpacing(5)

        product_en = QtWidgets.QLabel(
            "CT RECONSTRUCTION & ABLATION PLANNING", objectName="ProductEnglish"
        )
        product_en.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        info_lay.addWidget(product_en)
        info_lay.addSpacing(36)

        self._status = QtWidgets.QLabel(
            "正在初始化三维医学影像工作区…", objectName="LoadingText"
        )
        self._status.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        info_lay.addWidget(self._status)
        info_lay.addSpacing(12)

        self._progress = QtWidgets.QProgressBar(objectName="StartupProgress")
        self._progress.setRange(0, 100)
        self._progress.setValue(8)
        self._progress.setTextVisible(False)
        self._progress.setFixedWidth(385)
        info_lay.addWidget(self._progress)
        content.addWidget(info, 1, QtCore.Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(content)
        root.addStretch(1)

        footer = QtWidgets.QLabel(
            "HealthLink  ·  MEDICAL TECHNOLOGY", objectName="Footer"
        )
        footer.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        root.addWidget(footer)

    def set_status(self, text, progress=None):
        """更新启动阶段说明和确定进度，并立即刷新界面。"""
        self._status.setText(text)
        if progress is not None:
            self._progress.setValue(max(0, min(100, int(progress))))
        QtWidgets.QApplication.processEvents(
            QtCore.QEventLoop.ProcessEventsFlag.AllEvents
        )

    def show_cover(self):
        self._center_on_screen()
        self.show()
        self.raise_()
        self.activateWindow()
        QtWidgets.QApplication.processEvents(
            QtCore.QEventLoop.ProcessEventsFlag.AllEvents
        )

    def finish(self, target_window=None):
        """在主窗口首帧就绪后执行协调淡入、轻微上移与淡出过渡。"""
        if not self.isVisible():
            return

        if target_window is not None:
            target_window.setWindowOpacity(0.0)
            target_window.raise_()
            self.raise_()

            main_fade = QtCore.QPropertyAnimation(
                target_window, b"windowOpacity"
            )
            main_fade.setDuration(320)
            main_fade.setStartValue(0.0)
            main_fade.setEndValue(1.0)
            main_fade.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)

            cover_fade = QtCore.QPropertyAnimation(self, b"windowOpacity")
            cover_fade.setDuration(235)
            cover_fade.setStartValue(1.0)
            cover_fade.setEndValue(0.0)
            cover_fade.setEasingCurve(QtCore.QEasingCurve.Type.InOutCubic)

            delayed_cover_fade = QtCore.QSequentialAnimationGroup()
            delayed_cover_fade.addPause(55)
            delayed_cover_fade.addAnimation(cover_fade)

            cover_lift = QtCore.QPropertyAnimation(self, b"pos")
            cover_lift.setDuration(290)
            cover_lift.setStartValue(self.pos())
            cover_lift.setEndValue(self.pos() + QtCore.QPoint(0, -9))
            cover_lift.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)

            transition = QtCore.QParallelAnimationGroup(self)
            transition.addAnimation(main_fade)
            transition.addAnimation(delayed_cover_fade)
            transition.addAnimation(cover_lift)
            self._transition = transition

            loop = QtCore.QEventLoop(self)
            transition.finished.connect(loop.quit)
            transition.start()
            loop.exec(QtCore.QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents)
            target_window.setWindowOpacity(1.0)
            target_window.activateWindow()

        self.hide()
        self.close()
        self.deleteLater()

    def paintEvent(self, event):
        """绘制两套主题共用的低干扰医疗科技背景。"""
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect = QtCore.QRectF(self.rect()).adjusted(1.0, 1.0, -1.0, -1.0)
        card = QtGui.QPainterPath()
        card.addRoundedRect(rect, self.CORNER_RADIUS, self.CORNER_RADIUS)
        painter.setClipPath(card)
        gradient = QtGui.QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, QtGui.QColor(self._colors["bg_top"]))
        gradient.setColorAt(1.0, QtGui.QColor(self._colors["bg_bottom"]))
        painter.fillPath(card, gradient)

        accent = QtGui.QColor(self._colors["accent_2"])
        accent.setAlpha(14 if self._theme == "dark" else 10)
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawEllipse(
            QtCore.QRectF(-self.width() * 0.28, -self.height() * 0.22,
                          self.width() * 0.70, self.width() * 0.70)
        )

        green = QtGui.QColor(self._colors["accent"])
        green.setAlpha(12 if self._theme == "dark" else 9)
        painter.setBrush(green)
        diameter = self.width() * 0.48
        painter.drawEllipse(
            QtCore.QRectF(self.width() - diameter * 0.54,
                          self.height() - diameter * 0.50, diameter, diameter)
        )

        painter.setClipping(False)
        border = (
            QtGui.QColor(126, 173, 168, 44)
            if self._theme == "dark"
            else QtGui.QColor(28, 105, 96, 34)
        )
        painter.setPen(QtGui.QPen(border, 1.0))
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(
            rect, self.CORNER_RADIUS, self.CORNER_RADIUS
        )
        painter.end()


def show_startup_cover(app, theme="dark"):
    """创建并展示启动界面。"""
    cover = StartupCover(theme)
    if os.path.isfile(ICON_PATH):
        app.setWindowIcon(_rounded_app_icon())
    cover.show_cover()
    return cover
