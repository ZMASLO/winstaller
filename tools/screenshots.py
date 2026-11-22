"""Render UI screenshots offscreen (dry-run, nothing touches the system).

Usage::

    .venv\\Scripts\\python tools\\screenshots.py <output_dir> [--only NAME ...] [--scale 1.5]

Writes 1100x720 PNGs for the main UI states, a light-theme set and one 1.5x variant
(rendered in a subprocess, because the scale factor must be set before Qt starts).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

# Must happen before any Qt import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# The offscreen platform has no font database of its own on Windows: point it at the
# system fonts so Polish glyphs and the real families are used.
os.environ.setdefault(
    "QT_QPA_FONTDIR", str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "Fonts")
)
os.environ.pop("WINSTALLER_FAKE_UPDATE", None)

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import QEventLoop, QPoint, QRect  # noqa: E402
from PySide6.QtGui import QColor, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from winstaller.core.actions import ActionContext, ActionOutcome  # noqa: E402
from winstaller.core.catalog import Item  # noqa: E402
from winstaller.core.runner import (  # noqa: E402
    DryRunConfig,
    RunnerListener,
    TaskRunner,
    make_dry_run_executor,
)
from winstaller.ui.app import create_application  # noqa: E402
from winstaller.ui.main_window import MainWindow  # noqa: E402
from winstaller.ui.services import ConfirmRequest, Services, _fake_check  # noqa: E402
from winstaller.ui.theme import ThemeManager, ThemeMode  # noqa: E402
from winstaller.ui.widgets.confirm_dialog import ConfirmDialog  # noqa: E402

SIZE = (1100, 720)
FAIL_IDS = frozenset({"steam", "hwinfo"})
FAKE_VERSION = "2.0.1"


# ---------------------------------------------------------------- helpers


def pump(ms: int = 30) -> None:
    app = QApplication.instance()
    assert app is not None
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
        time.sleep(0.002)


def wait_until(predicate: Callable[[], bool], timeout: float = 15.0) -> None:
    app = QApplication.instance()
    assert app is not None
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("UI state not reached in time")
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
        time.sleep(0.002)


def base_services(**overrides: object) -> Services:
    services = Services.create(
        dry_run=True, dry_run_config=DryRunConfig(delay=0.0, fail_ids=FAIL_IDS), environ={}
    )
    services.auto_check_updates = False
    services.check_winget_on_start = False
    services.confirm = lambda _parent, _request: True
    services.open_path = lambda _p: None
    services.open_url = lambda _u: None
    return replace(services, **overrides) if overrides else services


def make_window(services: Services, mode: ThemeMode) -> MainWindow:
    theme = ThemeManager(mode)
    window = MainWindow(services, theme=theme)
    window.resize(*SIZE)
    window.show()
    pump(60)
    return window


def save(window: MainWindow | QPixmap, out: Path, name: str) -> Path:
    pump(40)
    pixmap = window if isinstance(window, QPixmap) else window.grab()
    path = out / f"{name}.png"
    pixmap.save(str(path))
    print(f"  {path.name}  {pixmap.width()}x{pixmap.height()}")
    return path


def close(window: MainWindow) -> None:
    window._force_close = True
    window.close()
    window.deleteLater()
    pump(20)


def select_profile(window: MainWindow) -> None:
    chip = next(iter(window.apps_page.profile_buttons.values()))
    chip.click()
    pump()


def gated_runner(services: Services, block_id: str, gate: threading.Event):
    """Runner whose executor parks on ``block_id`` mid-progress until ``gate`` is set."""
    base = make_dry_run_executor(DryRunConfig(delay=0.0, fail_ids=FAIL_IDS))

    def executor(item: Item, ctx: ActionContext) -> ActionOutcome:
        if item.id == block_id:
            ctx.log(f"[dry-run] {item.name}")
            ctx.log(f"Znaleziono {item.name} [{item.winget_id}] Wersja 1.0.0")
            ctx.log("Pobieranie https://example.invalid/installer.exe")
            ctx.log("██████████▒▒▒▒▒▒▒▒▒▒▒▒▒▒  4.5 MB / 12.0 MB")
            ctx.replace_last("██████████████▒▒▒▒▒▒▒▒▒▒  7.0 MB / 12.0 MB")
            while not gate.wait(0.02):
                if ctx.cancel_event.is_set():
                    return ActionOutcome(False, "Przerwano przez użytkownika.", cancelled=True)
        return base(item, ctx)

    def factory(listener: RunnerListener) -> TaskRunner:
        return TaskRunner(
            services.catalog,
            dry_run=True,
            listener=listener,
            executor=executor,
            arch=services.arch,
            app_dir=services.app_dir,
        )

    return factory


# ---------------------------------------------------------------- scenes


def scene_apps(out: Path, mode: ThemeMode, prefix: str = "") -> None:
    w = make_window(base_services(), mode)
    save(w, out, f"{prefix}01_aplikacje_puste")
    select_profile(w)
    save(w, out, f"{prefix}02_aplikacje_profil")
    w.apps_page.set_filter("cpu")
    save(w, out, f"{prefix}03_wyszukiwanie_wyniki")
    w.apps_page.set_filter("onedrive")
    save(w, out, f"{prefix}04_wyszukiwanie_brak")
    w.apps_page.set_filter("")
    w.navigate("system")
    save(w, out, f"{prefix}05_system")
    close(w)


def scene_run(out: Path, mode: ThemeMode, prefix: str = "") -> None:
    services = base_services()
    gate = threading.Event()
    services.runner_factory = gated_runner(services, "epic-games", gate)
    w = make_window(services, mode)
    select_profile(w)
    w.start_install()
    wait_until(lambda: w.run_page.model.row_for("epic-games") is not None)
    wait_until(lambda: str(w.run_page.model.row_for("epic-games").status) == "running")
    pump(120)
    save(w, out, f"{prefix}06_przebieg_w_toku")
    gate.set()
    wait_until(lambda: w.run.last_summary is not None)
    pump(80)
    save(w, out, f"{prefix}07_podsumowanie_bledy")
    if not prefix:
        w.navigate("log")
        save(w, out, "09_dziennik")
    close(w)


def scene_tools(out: Path, mode: ThemeMode, prefix: str = "") -> None:
    services = base_services()
    report = Path(os.environ.get("USERPROFILE", r"C:\Users\user")) / "Desktop/battery-report.html"

    def tool_executor(item: Item, ctx: ActionContext) -> ActionOutcome:
        ctx.log(f"[dry-run] {item.name}")
        return ActionOutcome(True, "Raport baterii zapisano (dry-run).", artifact=report)

    services.tool_runner_factory = lambda listener: TaskRunner(
        services.catalog,
        dry_run=True,
        listener=listener,
        executor=tool_executor,
        preserve_order=True,
    )
    w = make_window(services, mode)
    w.navigate("tools")
    w.run_tool("battery-report")
    wait_until(lambda: not w.tools.is_running())
    save(w, out, f"{prefix}08_narzedzia")
    close(w)


def scene_about(out: Path, mode: ThemeMode, prefix: str = "") -> None:
    w = make_window(base_services(), mode)
    w.navigate("about")
    save(w, out, f"{prefix}10_o_programie")
    close(w)


def scene_update(out: Path, mode: ThemeMode, prefix: str = "") -> None:
    services = Services.create(
        dry_run=True,
        dry_run_config=DryRunConfig(delay=0.05),
        environ={"WINSTALLER_FAKE_UPDATE": FAKE_VERSION},
    )
    services = replace(
        services,
        auto_check_updates=True,
        check_winget_on_start=False,
        confirm=lambda _p, _r: True,
        check_update=_fake_check(FAKE_VERSION, services.arch),
    )
    w = make_window(services, mode)
    w.start_background_tasks()
    wait_until(lambda: w.update_banner.isVisible())
    save(w, out, f"{prefix}11_aktualizacja_baner")
    w.start_update()
    wait_until(lambda: w.update_banner.progress.value() > 300)
    save(w, out, f"{prefix}12_aktualizacja_pobieranie")
    w.updates.cancel_download()
    wait_until(lambda: not w.updates.downloading)
    close(w)


def scene_winget(out: Path, mode: ThemeMode, prefix: str = "") -> None:
    services = base_services(winget_available=lambda: False, check_winget_on_start=True)
    w = make_window(services, mode)
    w.start_background_tasks()
    wait_until(lambda: w.winget_banner.isVisible())
    save(w, out, f"{prefix}14_winget_baner")
    close(w)


def scene_confirm(out: Path, mode: ThemeMode, prefix: str = "") -> None:
    w = make_window(base_services(), mode)
    select_profile(w)
    items = [w.services.catalog.item(i) for i in w.selection.ids()]
    risky = [i for i in items if i.confirm]
    request = ConfirmRequest(
        title="Potwierdź operację systemową",
        message=(
            f"Kolejka zawiera {len(risky)} "
            + ("pozycję, która zmienia" if len(risky) == 1 else "pozycje, które zmieniają")
            + " system. Pozostałe pozycje nie wymagają potwierdzenia."
        ),
        details=tuple((i.name, i.confirm or "") for i in risky),
    )
    dialog = ConfirmDialog(request, w)
    dialog.adjustSize()
    dialog.resize(560, dialog.sizeHint().height())
    pump()
    base = w.grab()
    dlg = dialog.grab()
    composed = QPixmap(base)
    p = QPainter(composed)
    p.fillRect(QRect(QPoint(0, 0), w.size()), QColor(0, 0, 0, 150))
    pos = QPoint((w.width() - dialog.width()) // 2, (w.height() - dialog.height()) // 2)
    p.drawPixmap(pos, dlg)
    p.end()
    save(composed, out, f"{prefix}13_potwierdzenie")
    dialog.deleteLater()
    close(w)


def scene_drivers(out: Path, mode: ThemeMode, prefix: str = "") -> None:
    w = make_window(base_services(), mode)
    w.navigate("system")
    section = w.system_page.sections["gpu-drivers"]
    w.selection.select("driver-nvidia")
    pump()
    w.system_page.scroll.verticalScrollBar().setValue(max(0, section.y() - 8))
    save(w, out, f"{prefix}15_sterowniki_gpu")
    w.start_install()
    wait_until(lambda: w.run.last_summary is not None)
    pump(80)
    save(w, out, f"{prefix}16_przebieg_sterownik")
    close(w)


def scene_updates(out: Path, mode: ThemeMode, prefix: str = "") -> None:
    w = make_window(base_services(), mode)
    w.navigate("system")
    w.selection.select_many(["windows-update", "store-update"])
    pump()
    w.system_page.scroll.verticalScrollBar().setValue(0)
    save(w, out, f"{prefix}17_aktualizacje_system")
    w.start_install()
    wait_until(lambda: w.run.last_summary is not None)
    pump(80)
    save(w, out, f"{prefix}18_przebieg_aktualizacje")
    if not prefix:
        w.navigate("log")
        save(w, out, "19_dziennik_aktualizacje")
    close(w)


SCENES: dict[str, Callable[[Path, ThemeMode, str], None]] = {
    "apps": scene_apps,
    "run": scene_run,
    "tools": scene_tools,
    "about": scene_about,
    "update": scene_update,
    "winget": scene_winget,
    "confirm": scene_confirm,
    "drivers": scene_drivers,
    "updates": scene_updates,
}
LIGHT_SCENES = ("apps", "run", "about", "tools", "updates")


def scale_variant(out: Path, scale: str) -> None:
    w = make_window(base_services(), ThemeMode.DARK)
    select_profile(w)
    save(w, out, f"31_aplikacje_profil_x{scale}")
    w.navigate("run")
    close(w)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--only", nargs="*", choices=sorted(SCENES), default=None)
    parser.add_argument("--scale", default=None, help="internal: render the scaled variant")
    parser.add_argument("--no-light", action="store_true")
    args = parser.parse_args(argv)
    out: Path = args.output
    out.mkdir(parents=True, exist_ok=True)

    if args.scale:
        create_application()
        scale_variant(out, args.scale)
        return 0

    app = create_application()
    app.setQuitOnLastWindowClosed(False)
    names = args.only or list(SCENES)
    print(f"Dark theme -> {out}")
    for name in names:
        SCENES[name](out, ThemeMode.DARK, "")
    if not args.no_light:
        print("Light theme")
        for name in names:
            if name in LIGHT_SCENES:
                SCENES[name](out, ThemeMode.LIGHT, "light_")
    if args.only is None:
        env = dict(os.environ, QT_SCALE_FACTOR="1.5")
        print("Scale 1.5")
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), str(out), "--scale", "1.5"],
            env=env,
            check=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
