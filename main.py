"""qPCR 分析工具入口。

直接运行：``.\\.venv\\Scripts\\python.exe main.py``；
用 PyInstaller 打包后同样从这里启动。
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from qpcr_tool import APP_NAME, __version__
from qpcr_tool.gui import MainWindow, apply_theme, install_exception_hook


def resource_path(relative: str) -> str:
    """把相对路径解析成真实路径，兼容 PyInstaller 单文件模式的临时解包目录。"""
    base = getattr(sys, "_MEIPASS", None)
    if base is None:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative)


def main() -> int:
    # Qt6 里 AA_EnableHighDpiScaling / AA_UseHighDpiPixmaps 已废弃，
    # 只需在创建 QApplication 之前设置缩放取整策略。
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("qPCR Analysis Tool")
    icon_file = resource_path(os.path.join("assets", "app.ico"))
    if os.path.isfile(icon_file):
        app.setWindowIcon(QIcon(icon_file))

    apply_theme(app)
    install_exception_hook()

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
