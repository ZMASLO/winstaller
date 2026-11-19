"""Theme mode (system / dark / light), live tracking of the OS colour scheme, title bar."""

from __future__ import annotations

import ctypes
import sys
from enum import StrEnum

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from winstaller.ui.theme.qss import build_qss
from winstaller.ui.theme.tokens import Palette, palette_for


class ThemeMode(StrEnum):
    SYSTEM = "system"
    DARK = "dark"
    LIGHT = "light"


def system_prefers_dark() -> bool:
    """OS colour scheme; unknown counts as dark (the primary theme)."""
    app = QGuiApplication.instance()
    if app is None:
        return True
    scheme = QGuiApplication.styleHints().colorScheme()
    return scheme != Qt.ColorScheme.Light


class ThemeManager(QObject):
    """Owns the current palette and re-styles the application when it changes."""

    changed = Signal(object)  # Palette

    def __init__(self, mode: ThemeMode = ThemeMode.SYSTEM, parent: QObject | None = None):
        super().__init__(parent)
        self._mode = mode
        self._palette = palette_for(self._resolve_dark())
        self._windows: list[QWidget] = []
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_system_scheme)

    # -- state
    @property
    def mode(self) -> ThemeMode:
        return self._mode

    @property
    def palette(self) -> Palette:
        return self._palette

    def set_mode(self, mode: ThemeMode) -> None:
        self._mode = ThemeMode(mode)
        self._refresh()

    def _resolve_dark(self) -> bool:
        if self._mode == ThemeMode.SYSTEM:
            return system_prefers_dark()
        return self._mode == ThemeMode.DARK

    def _on_system_scheme(self, _scheme: Qt.ColorScheme) -> None:
        if self._mode == ThemeMode.SYSTEM:
            self._refresh()

    def _refresh(self) -> None:
        new = palette_for(self._resolve_dark())
        if new is self._palette:
            return
        self._palette = new
        self.apply()
        self.changed.emit(new)

    # -- application
    def apply(self) -> None:
        app = QApplication.instance()
        if not isinstance(app, QApplication):
            return
        app.setPalette(qpalette(self._palette))
        app.setStyleSheet(build_qss(self._palette))
        for window in self._windows:
            set_title_bar_dark(window, self._palette.is_dark)

    def track_window(self, window: QWidget) -> None:
        """Keep the native title bar of ``window`` in sync with the theme."""
        if window not in self._windows:
            self._windows.append(window)
        set_title_bar_dark(window, self._palette.is_dark)


def qpalette(p: Palette) -> QPalette:
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: p.bg,
        QPalette.ColorRole.WindowText: p.text,
        QPalette.ColorRole.Base: p.surface,
        QPalette.ColorRole.AlternateBase: p.surface2,
        QPalette.ColorRole.Text: p.text,
        QPalette.ColorRole.Button: p.surface,
        QPalette.ColorRole.ButtonText: p.text,
        QPalette.ColorRole.Highlight: p.selection,
        QPalette.ColorRole.HighlightedText: p.text,
        QPalette.ColorRole.ToolTipBase: p.surface2,
        QPalette.ColorRole.ToolTipText: p.text,
        QPalette.ColorRole.PlaceholderText: p.muted,
        QPalette.ColorRole.Link: p.accent_fg,
        QPalette.ColorRole.Mid: p.border,
        QPalette.ColorRole.Dark: p.border_strong,
    }
    for role, value in roles.items():
        pal.setColor(role, QColor(value))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(p.faint))
    return pal


_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19  # Windows 10 builds before 20H1


def set_title_bar_dark(window: QWidget, dark: bool) -> bool:
    """Ask DWM for a dark/light native title bar. Silently does nothing elsewhere."""
    if sys.platform != "win32" or QGuiApplication.platformName() != "windows":
        return False
    try:
        hwnd = int(window.winId())
        value = ctypes.c_int(1 if dark else 0)
        dwm = ctypes.windll.dwmapi
        for attr in (_DWMWA_USE_IMMERSIVE_DARK_MODE, _DWMWA_USE_IMMERSIVE_DARK_MODE_OLD):
            hr = dwm.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), attr, ctypes.byref(value), ctypes.sizeof(value)
            )
            if hr == 0:
                return True
    except (OSError, AttributeError, ValueError):
        pass
    return False
