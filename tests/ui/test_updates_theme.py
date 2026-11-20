"""Update banner and download, winget banner, theme switching with persisted setting."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QPushButton

from winstaller.core.updater import (
    ReleaseAsset,
    ReleaseInfo,
    UpdateCancelled,
    UpdateCheck,
    UpdateError,
)
from winstaller.core.winget import WingetResult
from winstaller.ui.app import load_theme_mode
from winstaller.ui.main_window import SETTINGS_THEME_KEY
from winstaller.ui.theme import ThemeMode
from winstaller.ui.theme.manager import system_prefers_dark
from winstaller.ui.theme.tokens import palette_for

ASSET = ReleaseAsset("winstaller-x64.exe", "https://example.invalid/w.exe", 1000)


def update_check(latest: str = "2.0.1", available: bool = True) -> UpdateCheck:
    release = ReleaseInfo(f"v{latest}", latest, f"Winstaller {latest}", "", "", (ASSET,))
    return UpdateCheck("2.0.0", latest, available, ASSET, release)


def banner_button(qtbot, banner, name: str) -> QPushButton:
    """The banner's action button (layouts show new children on the next event loop pass)."""
    qtbot.waitUntil(
        lambda: (b := banner.findChild(QPushButton, name)) is not None and b.isVisible()
    )
    return banner.findChild(QPushButton, name)


class FakeUpdater:
    """check / download / apply doubles; ``download`` can stop half way on a gate."""

    def __init__(self, tmp_path: Path, check: UpdateCheck | Exception) -> None:
        self.result = check
        self.checks = 0
        self.dest = tmp_path / "winstaller.update.exe"
        self.applied: list[Path] = []
        self.half = threading.Event()
        self.gate = threading.Event()
        self.gate.set()
        self.fail_download: Exception | None = None

    def check(self) -> UpdateCheck:
        self.checks += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def download(self, asset, dest, progress, cancel) -> Path:
        progress(asset.size // 2, asset.size)
        self.half.set()
        while not self.gate.wait(0.01):
            if cancel.is_set():
                raise UpdateCancelled("Pobieranie aktualizacji przerwane.")
        if self.fail_download is not None:
            raise self.fail_download
        progress(asset.size, asset.size)
        Path(dest).write_bytes(b"NEW")
        return Path(dest)

    def apply(self, path: Path) -> bool:
        self.applied.append(path)
        return True


@pytest.fixture
def no_quit(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(QApplication, "quit", lambda: calls.append(1))
    return calls


def window_with_updates(make_window, tmp_path: Path, check, **kw):
    fake = FakeUpdater(tmp_path, check)
    h = make_window(
        check_update=fake.check,
        download_update=fake.download,
        apply_update=fake.apply,
        update_dest=lambda asset: fake.dest,
        auto_check_updates=True,
        **kw,
    )
    return h, fake


# ----------------------------------------------------------------- updates


def test_no_update_is_silent(make_window, qtbot, tmp_path) -> None:
    h, fake = window_with_updates(make_window, tmp_path, update_check(available=False))
    w = h.window
    with qtbot.waitSignal(w.updates.checkFinished):
        w.start_background_tasks()
    assert fake.checks == 1
    assert not w.update_banner.isVisible()
    assert w.about_page.update_status.text() == ""
    assert not any("aktualiz" in line.lower() for line in w.log.lines())


def test_manual_check_without_update_reports_it(make_window, qtbot, tmp_path) -> None:
    h, _ = window_with_updates(make_window, tmp_path, update_check("2.0.0", available=False))
    w = h.window
    w.navigate("about")
    with qtbot.waitSignal(w.updates.checkFinished):
        qtbot.mouseClick(w.about_page.check_button, Qt.MouseButton.LeftButton)
    assert w.about_page.update_status.text() == "Masz najnowszą wersję (2.0.0)."
    assert w.about_page.check_button.isEnabled()


def test_update_available_download_apply_and_quit(make_window, qtbot, tmp_path, no_quit) -> None:
    h, fake = window_with_updates(make_window, tmp_path, update_check())
    w = h.window
    fake.gate.clear()
    with qtbot.waitSignal(w.updates.checkFinished):
        w.start_background_tasks()
    banner = w.update_banner
    assert banner.isVisible()
    assert banner.message.text() == "Dostępna wersja 2.0.1"
    assert banner.detail.text() == "Masz 2.0.0"
    assert w.about_page.update_status.text() == "Dostępna wersja 2.0.1 (masz 2.0.0)."
    qtbot.mouseClick(banner_button(qtbot, banner, "updateNow"), Qt.MouseButton.LeftButton)
    assert fake.half.wait(5)
    qtbot.waitUntil(lambda: banner.progress.value() == 500)
    assert banner.progress.isVisible()
    assert banner.message.text() == "Pobieranie wersji 2.0.1…"
    banner_button(qtbot, banner, "updateCancel")
    with qtbot.waitSignal(w.updates.applied, timeout=5000):
        fake.gate.set()
    assert fake.applied == [fake.dest]
    assert fake.dest.read_bytes() == b"NEW"
    qtbot.waitUntil(lambda: not w.isVisible())
    assert no_quit == [1]


def test_update_download_can_be_cancelled(make_window, qtbot, tmp_path) -> None:
    h, fake = window_with_updates(make_window, tmp_path, update_check())
    w = h.window
    fake.gate.clear()
    with qtbot.waitSignal(w.updates.checkFinished):
        w.start_background_tasks()
    assert w.start_update()
    assert fake.half.wait(5)
    with qtbot.waitSignal(w.updates.downloadCancelled, timeout=5000):
        qtbot.mouseClick(
            banner_button(qtbot, w.update_banner, "updateCancel"), Qt.MouseButton.LeftButton
        )
    assert fake.applied == []
    assert w.update_banner.message.text() == "Dostępna wersja 2.0.1"
    banner_button(qtbot, w.update_banner, "updateNow")
    assert w.isVisible()


def test_update_download_failure_offers_retry(make_window, qtbot, tmp_path) -> None:
    h, fake = window_with_updates(make_window, tmp_path, update_check())
    w = h.window
    fake.fail_download = UpdateError("Suma kontrolna SHA256 pobranego pliku się nie zgadza.")
    with qtbot.waitSignal(w.updates.checkFinished):
        w.start_background_tasks()
    with qtbot.waitSignal(w.updates.downloadFailed, timeout=5000):
        w.start_update()
    banner = w.update_banner
    assert banner.tag.text() == "[BŁĄD]"
    assert banner.detail.text() == "Suma kontrolna SHA256 pobranego pliku się nie zgadza."
    banner_button(qtbot, banner, "updateRetry")
    qtbot.mouseClick(banner_button(qtbot, banner, "updateDismiss"), Qt.MouseButton.LeftButton)
    assert not banner.isVisible()


def test_network_error_on_auto_check_goes_only_to_the_log(make_window, qtbot, tmp_path) -> None:
    error = UpdateError("Nie udało się połączyć z GitHub: brak sieci")
    h, _ = window_with_updates(make_window, tmp_path, error)
    w = h.window
    with qtbot.waitSignal(w.updates.checkFailed) as blocker:
        w.start_background_tasks()
    assert blocker.args == [str(error), False]
    assert not w.update_banner.isVisible()
    assert w.about_page.update_status.text() == ""
    qtbot.waitUntil(lambda: any("brak sieci" in line for line in w.log.lines()))


def test_network_error_on_manual_check_is_shown(make_window, qtbot, tmp_path) -> None:
    error = UpdateError("Nie udało się połączyć z GitHub: brak sieci")
    h, _ = window_with_updates(make_window, tmp_path, error)
    w = h.window
    w.navigate("about")
    with qtbot.waitSignal(w.updates.checkFailed):
        qtbot.mouseClick(w.about_page.check_button, Qt.MouseButton.LeftButton)
    assert w.about_page.update_status.text() == str(error)
    assert w.about_page.update_status.property("role") == "error"
    assert not w.update_banner.isVisible()


def test_updates_disabled_without_checker(make_window) -> None:
    w = make_window(check_update=None).window
    assert not w.updates.enabled
    assert not w.about_page.check_button.isEnabled()
    assert "dry-run" in w.about_page.update_status.text()


# ------------------------------------------------------------------ winget


def test_winget_banner_and_install(make_window, qtbot) -> None:
    calls: list[str] = []

    def ensure(on_line, on_replace, cancel) -> WingetResult:
        calls.append("ensure")
        on_line("Rejestrowanie App Installer (winget)...")
        return WingetResult(0, True, False, "Zainstalowano winget.")

    h = make_window(
        winget_available=lambda: False, ensure_winget=ensure, check_winget_on_start=True
    )
    w = h.window
    with qtbot.waitSignal(w.winget.availabilityChecked) as blocker:
        w.start_background_tasks()
    assert blocker.args == [False]
    banner = w.winget_banner
    assert banner.isVisible() and banner.tag.text() == "[WINGET]"
    assert banner.message.text().startswith("Nie znaleziono winget")
    with qtbot.waitSignal(w.winget.installFinished, timeout=5000) as done:
        qtbot.mouseClick(banner_button(qtbot, banner, "wingetInstall"), Qt.MouseButton.LeftButton)
    assert done.args == [True, "Zainstalowano winget."]
    assert calls == ["ensure"]
    assert banner.tag.text() == "[OK]" and w.winget.available
    qtbot.waitUntil(lambda: any("Rejestrowanie App Installer" in x for x in w.log.lines()))


def test_no_winget_banner_when_available(make_window, qtbot) -> None:
    w = make_window(check_winget_on_start=True).window
    with qtbot.waitSignal(w.winget.availabilityChecked) as blocker:
        w.start_background_tasks()
    assert blocker.args == [True]
    assert not w.winget_banner.isVisible()


def test_winget_check_error_counts_as_missing(make_window, qtbot) -> None:
    def broken() -> bool:
        raise OSError("nie da się")

    w = make_window(winget_available=broken, check_winget_on_start=True).window
    with qtbot.waitSignal(w.winget.availabilityChecked) as blocker:
        w.start_background_tasks()
    assert blocker.args == [False]
    assert w.winget_banner.isVisible()


# ------------------------------------------------------------------- theme


def window_color() -> str:
    return QApplication.palette().color(QPalette.ColorRole.Window).name().lower()


@pytest.mark.parametrize("mode", [ThemeMode.LIGHT, ThemeMode.DARK, ThemeMode.SYSTEM])
def test_theme_switch_changes_palette_and_persists(
    make_window, qtbot, settings: QSettings, tmp_path, mode: ThemeMode
) -> None:
    start = ThemeMode.DARK if mode != ThemeMode.DARK else ThemeMode.LIGHT
    w = make_window(mode=start).window
    w.navigate("about")
    qtbot.mouseClick(w.about_page.theme_buttons[mode], Qt.MouseButton.LeftButton)
    dark = system_prefers_dark() if mode == ThemeMode.SYSTEM else mode == ThemeMode.DARK
    assert w.theme.mode == mode
    assert w.theme.palette.is_dark == dark
    assert w.theme.palette == palette_for(dark)
    assert window_color() == palette_for(dark).bg.lower()
    assert w.about_page.theme_buttons[mode].isChecked()
    assert sum(b.isChecked() for b in w.about_page.theme_buttons.values()) == 1
    settings.sync()
    assert settings.value(SETTINGS_THEME_KEY) == mode.value
    ini = Path(settings.fileName())
    assert ini.parent == tmp_path and f"theme={mode.value}" in ini.read_text(encoding="utf-8")
    reread = QSettings(str(ini), QSettings.Format.IniFormat)
    assert load_theme_mode(reread) == mode


def test_theme_change_emits_changed_once(make_window, qtbot) -> None:
    w = make_window(mode=ThemeMode.DARK).window
    with qtbot.waitSignal(w.theme.changed) as blocker:
        w.set_theme_mode(ThemeMode.LIGHT)
    assert blocker.args[0].is_dark is False
    with qtbot.assertNotEmitted(w.theme.changed):
        w.set_theme_mode(ThemeMode.LIGHT)


def test_load_theme_mode_falls_back_to_system(settings: QSettings) -> None:
    settings.setValue(SETTINGS_THEME_KEY, "purple")
    assert load_theme_mode(settings) == ThemeMode.SYSTEM
