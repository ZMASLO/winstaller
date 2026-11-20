"""UI start-up and responsiveness, compared with 1.1.8 where both sides measure the same.

* time to the first window: a fresh ``python -m winstaller --dry-run --exit-after-show``
  process (offscreen) vs ``window.time_to_window_s`` of 1.1.8 (fresh process building
  ``ModernApp`` + 36 checkboxes + ``update_idletasks``); wall time from process start
  until the child reports its first frame, median of several runs;
* building ``MainWindow`` in-process plus its first paint (1.1.8: construct + checkboxes
  + idletasks from the same baseline breakdown);
* the search filter over the whole catalog (both selection pages) within 1-2 frames.

All budgets are multiplied by ``WINSTALLER_PERF_SLACK``; see tests/perf/README.md.
"""

from __future__ import annotations

import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.perf.conftest import report, slack

pytestmark = pytest.mark.perf

CREATE_NO_WINDOW = 0x08000000
RUNS = 5
MAX_TIME_TO_WINDOW_S = 1.5  # absolute budget for the fresh process
MAX_BUILD_WINDOW_S = 0.3  # MainWindow() + first paint, in-process
FRAME_S = 1 / 60
MAX_FILTER_S = 2 * FRAME_S  # one keystroke re-filters both pages
NEEDLES = ("c", "cpu", "steam", "zzzz-brak", "win", "")


def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("WINSTALLER_")}
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.setdefault(
        "QT_QPA_FONTDIR", str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "Fonts")
    )
    return env


def time_to_window() -> tuple[float, float]:
    """(wall seconds from Popen to the child's report, child's own uptime at that point)."""
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, "-m", "winstaller", "--dry-run", "--exit-after-show"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=_child_env(),
        creationflags=CREATE_NO_WINDOW,
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().decode(errors="replace").strip()
        wall = time.perf_counter() - t0
        _, err = proc.communicate(timeout=30)
    except BaseException:
        proc.kill()
        proc.communicate()
        raise
    assert proc.returncode == 0, err.decode(errors="replace")
    assert line.startswith("window-shown "), (line, err.decode(errors="replace"))
    return wall, float(line.split()[1])


def test_time_to_window_vs_118(baseline: dict[str, Any], machine_factor: float) -> None:
    time_to_window()  # warm-up: file cache, byte-compiled modules
    samples = [time_to_window() for _ in range(RUNS)]
    wall = statistics.median(s[0] for s in samples)
    uptime = statistics.median(s[1] for s in samples)
    old = baseline["window"]["time_to_window_s"]["median"]
    report(
        "time to window (fresh process)",
        f"{wall * 1e3:.0f} ms",
        f"{old * 1e3:.0f} ms",
        f"x{wall / (old * machine_factor):.2f} of 1.1.8 scaled; in-process {uptime * 1e3:.0f} ms",
    )
    assert wall <= old * machine_factor * slack(), f"{wall:.3f} s vs 1.1.8 {old:.3f} s"
    assert wall <= MAX_TIME_TO_WINDOW_S * slack()


@pytest.fixture
def services(tmp_path: Path):
    from winstaller.core.catalog import load_catalog
    from winstaller.core.runner import TaskRunner
    from winstaller.ui.services import Services

    catalog = load_catalog()

    def factory(listener):
        return TaskRunner(catalog, dry_run=True, listener=listener, arch="x64", app_dir=tmp_path)

    return Services(
        catalog=catalog,
        runner_factory=factory,
        tool_runner_factory=factory,
        dry_run=True,
        arch="x64",
        app_dir=tmp_path,
        version="2.0.0",
        auto_check_updates=False,
        check_winget_on_start=False,
    )


def test_build_main_window_and_first_paint(
    qtbot, services, baseline: dict[str, Any], machine_factor: float
) -> None:
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    from winstaller.ui.main_window import MainWindow
    from winstaller.ui.theme import ThemeManager, ThemeMode

    def build() -> float:
        t0 = time.perf_counter()
        theme = ThemeManager(ThemeMode.DARK)
        theme.apply()
        window = MainWindow(services, theme=theme)
        window.resize(1100, 720)
        window.show()
        QApplication.processEvents()
        window.grab()  # forces a full paint of the window
        seconds = time.perf_counter() - t0
        window._force_close = True
        window.close()
        window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        return seconds

    build()  # warm-up (first QSS parse, font cache)
    runs = [build() for _ in range(RUNS)]
    seconds = statistics.median(runs)
    parts = baseline["window"]["breakdown"]
    old = sum(parts[k]["median"] for k in ("construct_s", "checkboxes_s", "idletasks_s"))
    report(
        "MainWindow() + first paint",
        f"{seconds * 1e3:.0f} ms",
        f"{old * 1e3:.0f} ms",
        "1.1.8: ModernApp + 36 checkboxes + idletasks",
    )
    assert seconds <= old * machine_factor * slack()
    assert seconds <= MAX_BUILD_WINDOW_S * slack()


def test_search_filter_whole_catalog_within_two_frames(qtbot, services) -> None:
    from PySide6.QtWidgets import QApplication

    from winstaller.ui.main_window import MainWindow
    from winstaller.ui.theme import ThemeManager, ThemeMode

    window = MainWindow(services, theme=ThemeManager(ThemeMode.DARK))
    window.resize(1100, 720)
    window.show()
    qtbot.waitExposed(window)
    pages = (window.apps_page, window.system_page)
    tiles = sum(len(p.tiles) for p in pages)
    assert tiles == len(services.catalog.items()) - len(services.catalog.tools())

    def filter_all(needle: str) -> float:
        t0 = time.perf_counter()
        for page in pages:
            page.set_filter(needle)
        QApplication.processEvents()  # layout of the re-flowed grid
        return time.perf_counter() - t0

    for needle in NEEDLES:
        filter_all(needle)  # warm-up
    worst = 0.0
    medians = []
    for needle in NEEDLES:
        med = statistics.median(filter_all(needle) for _ in range(7))
        medians.append(med)
        worst = max(worst, med)
    report(
        "search filter, whole catalog",
        f"{worst * 1e3:.1f} ms",
        "",
        f"worst median of {len(NEEDLES)} needles, {tiles} tiles; budget 2 frames",
    )
    window._force_close = True
    window.close()
    window.deleteLater()
    assert worst <= MAX_FILTER_S * slack(), [f"{m * 1e3:.1f}" for m in medians]
