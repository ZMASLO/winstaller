"""Application entry point: ``run(*, dry_run, exit_after_show=False) -> int``."""

from __future__ import annotations

import contextlib
import ctypes
import sys

from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

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


def process_uptime() -> float:
    """Seconds since this process was created (Windows ``GetProcessTimes``)."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
    kernel32.GetSystemTimePreciseAsFileTime.argtypes = [ctypes.POINTER(wintypes.FILETIME)]
    creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    kernel32.GetProcessTimes(
        kernel32.GetCurrentProcess(),
        ctypes.byref(creation),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    )
    now = wintypes.FILETIME()
    kernel32.GetSystemTimePreciseAsFileTime(ctypes.byref(now))

    def ticks(ft: wintypes.FILETIME) -> int:
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

    return (ticks(now) - ticks(creation)) / 1e7


class _ExitAfterShow(QObject):
    """Diagnostics (``--exit-after-show``): on the first show of the main window let the
    event loop lay it out and paint once, print the time since process start, quit.
    Used by tests/perf to measure the time to the first window."""

    def __init__(self, app: QApplication, window: QWidget) -> None:
        super().__init__(window)
        self._app = app
        self._done = False
        window.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and not self._done:
            self._done = True
            QTimer.singleShot(0, self._finish)
        return False

    def _finish(self) -> None:
        try:
            self._app.processEvents()  # pending layout and paint of the first frame
            sys.stdout.write(f"window-shown {process_uptime():.4f}\n")
            sys.stdout.flush()
        finally:
            self._app.exit(0)  # never leave a diagnostic process running


def run(*, dry_run: bool, exit_after_show: bool = False) -> int:
    """Start the UI and return the exit code.

    ``exit_after_show`` (hidden ``--exit-after-show`` flag) is a diagnostic mode: the
    window is shown and the process exits right after its first frame, printing
    ``window-shown <seconds since process start>``. Deferred start-up work (update
    and winget checks) is not started in that mode.
    """
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
    if exit_after_show:
        _ExitAfterShow(app, window)
    else:
        QTimer.singleShot(0, window.start_background_tasks)
    window.show()
    return int(app.exec())
