"""Application entry point: ``run(*, dry_run) -> int``."""

from __future__ import annotations

import contextlib
import ctypes
import sys

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from winstaller.core.catalog import CatalogError
from winstaller.core.version import APP_NAME
from winstaller.ui.icons import app_icon
from winstaller.ui.main_window import SETTINGS_THEME_KEY, MainWindow
from winstaller.ui.services import Services
from winstaller.ui.theme import ThemeManager, ThemeMode, fonts
from winstaller.ui.theme.fonts import Role

APP_USER_MODEL_ID = "ZMASLO.Winstaller"


def _set_app_user_model_id() -> None:
    """Own taskbar identity, so Windows shows our icon instead of python.exe's."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(OSError, AttributeError):
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)


def create_application(argv: list[str] | None = None) -> QApplication:
    existing = QApplication.instance()
    if isinstance(existing, QApplication):
        return existing
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(argv if argv is not None else sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("ZMASLO")
    app.setStyle("Fusion")
    app.setFont(fonts.font(Role.BODY))
    app.setWindowIcon(app_icon())
    return app


def load_theme_mode(settings: QSettings) -> ThemeMode:
    raw = settings.value(SETTINGS_THEME_KEY, ThemeMode.SYSTEM.value)
    try:
        return ThemeMode(str(raw))
    except ValueError:
        return ThemeMode.SYSTEM


def run(*, dry_run: bool) -> int:
    _set_app_user_model_id()
    app = create_application()
    try:
        services = Services.create(dry_run=dry_run)
    except CatalogError as exc:
        QMessageBox.critical(None, APP_NAME, f"Nie udało się wczytać katalogu programów:\n{exc}")
        return 1
    settings = QSettings()
    theme = ThemeManager(load_theme_mode(settings))
    theme.apply()
    window = MainWindow(services, theme=theme, settings=settings)
    window.show()
    QTimer.singleShot(0, window.start_background_tasks)
    return int(app.exec())
