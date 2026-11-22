"""The background Windows Update installation in the UI: ``BackgroundUpdateMonitor``,
the "W tle" block (``BackgroundUpdatePanel``), the nav badge, the ``wuBanner``, the
close question and the dry-run simulation. Every read goes through a fake
``Services.read_install_progress`` / ``find_background_install``; no files, no
processes."""

from __future__ import annotations

import dataclasses
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtWidgets import QPushButton, QWidget

from tests.ui.conftest import wait_log, wait_run_finished
from winstaller.core import updates
from winstaller.core.runner import TaskResult, TaskStatus
from winstaller.core.updates import InstallItem, InstallProgress, UpdatesError
from winstaller.ui.bridge import BackgroundUpdateMonitor
from winstaller.ui.main_window import SETTINGS_HIDDEN_WU_KEY
from winstaller.ui.services import Services, SimulatedBackgroundInstall, simulated_progress
from winstaller.ui.widgets.background_panel import BackgroundItemModel, item_tag

START = datetime(2026, 11, 22, 16, 0, 0).astimezone()
BOOT = START - timedelta(hours=2)
STATUS = Path(r"C:\ProgramData\Winstaller\logs\windows-update-2026.11.22-160000.json")
DRY_STATUS = updates.DRY_RUN_LOG_PATH.with_suffix(".json")


class Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeInstall:
    """``read_install_progress`` / ``find_background_install`` double. ``value`` is
    returned (status/log path filled in) or raised; ``gate`` parks a read."""

    def __init__(self, value: InstallProgress | BaseException | None = None) -> None:
        self.value = value
        self.found: InstallProgress | BaseException | None = None
        self.reads: list[Path] = []
        self.gate: threading.Event | None = None
        self.in_read = threading.Event()
        self._lock = threading.Lock()

    def read(self, path: Path) -> InstallProgress:
        with self._lock:
            self.reads.append(path)
        self.in_read.set()
        if self.gate is not None:
            self.gate.wait(5)
        value = self.value
        if isinstance(value, BaseException):
            raise value
        assert value is not None
        return dataclasses.replace(value, status_path=path, log_path=path.with_suffix(".log"))

    def find(self) -> InstallProgress | None:
        if isinstance(self.found, BaseException):
            raise self.found
        return self.found


def sim(steps: float, **changes: Any) -> InstallProgress:
    p = simulated_progress(steps, start=START, path=STATUS, step_seconds=60)
    return dataclasses.replace(p, **changes) if changes else p


SEARCHING, DOWNLOADING, INSTALLING, FINISHED = sim(0), sim(1.5), sim(4.5), sim(99)


def final(statuses: tuple[str, ...], **fields: Any) -> InstallProgress:
    items = tuple(InstallItem(f"KB{i} aktualizacja", s) for i, s in enumerate(statuses))  # type: ignore[arg-type]
    base: dict[str, Any] = {
        "state": "finished",
        "phase": "finished",
        "items": items,
        "total": len(items),
        "exit_code": 0,
        "started": START,
        "finished": START + timedelta(minutes=9),
        "status_path": STATUS,
        "log_path": STATUS.with_suffix(".log"),
        "pid": 4242,
    }
    base.update(fields)
    return InstallProgress(**base)


@pytest.fixture
def clock() -> Clock:
    return Clock(START + timedelta(minutes=5))


@pytest.fixture
def fake() -> FakeInstall:
    return FakeInstall(INSTALLING)


@pytest.fixture
def wu_window(make_window, fake: FakeInstall, clock: Clock) -> Callable[..., Any]:
    def factory(**kw: Any) -> Any:
        options: dict[str, Any] = {
            "read_install_progress": fake.read,
            "find_background_install": fake.find,
            "boot_time": lambda: BOOT,
            "clock": clock,
            "background_poll_ms": 20,
            # Decision 17: "Otwórz log" needs an existing file; these logs are fictional.
            "path_exists": lambda _p: True,
        }
        options.update(kw)
        return make_window(**options)

    yield factory
    if fake.gate is not None:
        fake.gate.set()


def shown(widget: QWidget) -> bool:
    """Visible within the Przebieg page (the page itself may not be the current one)."""
    page = widget
    while page is not None and page.objectName() != "page_run":
        page = page.parentWidget()
    assert page is not None
    return widget is page or widget.isVisibleTo(page)


def emissions(monitor: BackgroundUpdateMonitor) -> list[InstallProgress]:
    seen: list[InstallProgress] = []
    monitor.changed.connect(seen.append)
    return seen


def badge(w: Any) -> tuple[str, str]:
    btn = w.nav.buttons["run"]
    return btn.extra_badge, btn.extra_badge_tone


def banner_button(w: Any, name: str) -> QPushButton:
    found = [b for b in w.wu_banner.buttons() if b.objectName() == name]
    assert len(found) == 1, [b.objectName() for b in w.wu_banner.buttons()]
    return found[0]


def tags(panel: Any) -> list[str]:
    model = panel.model
    return [
        model.data(model.index(r), BackgroundItemModel.TagRole) for r in range(model.rowCount())
    ]


def ok_result(**fields: Any) -> TaskResult:
    base: dict[str, Any] = {
        "item_id": "windows-update",
        "status": TaskStatus.OK,
        "message": "ok",
        "duration": 1.0,
        "artifact": STATUS.with_suffix(".log"),
    }
    base.update(fields)
    return TaskResult(**base)


# ------------------------------------------------------------------- monitor


def test_disabled_without_services(make_window, qtbot) -> None:
    h = make_window()
    w = h.window
    assert not w.bg_updates.enabled
    w.selection.select("windows-update")
    assert w.start_install()
    wait_run_finished(qtbot, w)
    assert not shown(w.run_page.bg_panel) and badge(w)[0] == ""
    assert not w.bg_updates.polling


def test_dry_run_queue_item_starts_following(wu_window, qtbot) -> None:
    simulation = SimulatedBackgroundInstall(0)
    h = wu_window(read_install_progress=simulation.read)
    w = h.window
    w.selection.select("windows-update")
    assert w.start_install()
    wait_run_finished(qtbot, w)
    panel = w.run_page.bg_panel
    qtbot.waitUntil(lambda: shown(panel) and w.bg_updates.progress is not None)
    assert w.bg_updates.status_path == DRY_STATUS
    qtbot.waitUntil(lambda: not w.bg_updates.polling)
    assert panel.title_label.text() == "Zakończono — wymagany restart"
    assert badge(w) == ("WU OK", "text")
    wait_log(qtbot, w, "Śledzenie instalacji aktualizacji Windows")
    message = w.run.last_summary.results[0].message
    assert "zakładka Przebieg" in message and "Ustawienia" not in message


@pytest.mark.parametrize(
    "result",
    [
        ok_result(status=TaskStatus.FAILED),
        # Decision 15: CANCELLED *with* a log is followed (the install may have started).
        ok_result(status=TaskStatus.CANCELLED, artifact=None),
        ok_result(status=TaskStatus.SKIPPED),
        ok_result(item_id="store-update"),
        ok_result(artifact=None),
        ok_result(artifact=STATUS.with_suffix(".txt")),
        ok_result(artifact=STATUS),
    ],
    ids=["failed", "cancelled-no-log", "skipped", "other-item", "no-artifact", "txt", "json"],
)
def test_track_result_only_after_a_successful_windows_update(
    wu_window, fake: FakeInstall, result: TaskResult
) -> None:
    w = wu_window().window
    assert not w.bg_updates.track_result(result)
    assert fake.reads == [] and not w.bg_updates.polling


def test_track_result_follows_the_status_next_to_the_log(wu_window, fake, qtbot) -> None:
    w = wu_window().window
    assert w.bg_updates.track_result(ok_result(artifact=STATUS.with_suffix(".LOG")))
    qtbot.waitUntil(lambda: bool(fake.reads))
    assert fake.reads[0] == STATUS


def test_failed_windows_update_item_is_not_followed(wu_window, fake, qtbot) -> None:
    h = wu_window(fail_ids={"windows-update"})
    h.window.selection.select("windows-update")
    assert h.window.start_install()
    wait_run_finished(qtbot, h.window)
    assert fake.reads == [] and not shown(h.window.run_page.bg_panel)


def test_changed_only_on_change_and_polling_stops_at_the_end(wu_window, fake, qtbot) -> None:
    w = wu_window().window
    seen = emissions(w.bg_updates)
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: len(fake.reads) >= 5)
    assert len(seen) == 1 and w.bg_updates.polling and w.bg_updates.running
    fake.value = dataclasses.replace(INSTALLING, current=2)
    qtbot.waitUntil(lambda: len(seen) == 2)
    fake.value = FINISHED
    qtbot.waitUntil(lambda: not w.bg_updates.polling)
    assert len(seen) == 3 and seen[-1].done and not w.bg_updates.running
    reads = len(fake.reads)
    qtbot.wait(120)
    assert len(fake.reads) == reads


def test_read_errors_stop_polling_after_a_series(wu_window, fake, qtbot, monkeypatch) -> None:
    monkeypatch.setattr(BackgroundUpdateMonitor, "MAX_FAILURES", 3)
    fake.value = UpdatesError("Nie znaleziono pliku.", kind="format")
    w = wu_window().window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: not w.bg_updates.polling)
    assert len(fake.reads) == 3
    lines = [line for line in w.log.lines() if "Nie udało się odczytać stanu" in line]
    assert len(lines) == 1
    assert not shown(w.run_page.bg_panel) and badge(w)[0] == ""


def test_lost_status_file_does_not_stay_in_progress(wu_window, fake, qtbot, monkeypatch) -> None:
    monkeypatch.setattr(BackgroundUpdateMonitor, "MAX_FAILURES", 3)
    w = wu_window().window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: w.bg_updates.progress is not None)
    fake.value = UpdatesError("Nie znaleziono pliku.", kind="format")
    qtbot.waitUntil(lambda: not w.bg_updates.polling)
    assert not w.bg_updates.running
    assert not badge(w)[0].startswith("WU 1/")


def test_slow_read_does_not_block_the_gui(wu_window, fake, qtbot) -> None:
    fake.gate = threading.Event()
    w = wu_window().window
    t0 = time.monotonic()
    assert w.bg_updates.track(STATUS)
    assert time.monotonic() - t0 < 0.2
    assert fake.in_read.wait(2)
    fired: list[bool] = []
    QTimer.singleShot(10, lambda: fired.append(True))
    qtbot.waitUntil(lambda: bool(fired), timeout=400)  # while the read is parked
    assert len(fake.reads) == 1  # no second read started while one runs
    fake.gate.set()
    qtbot.waitUntil(lambda: shown(w.run_page.bg_panel))


def test_result_after_close_is_ignored(wu_window, fake, qtbot) -> None:
    fake.gate = threading.Event()
    h = wu_window()
    w = h.window
    seen = emissions(w.bg_updates)
    w.bg_updates.track(STATUS)
    assert fake.in_read.wait(2)
    assert w.close()  # nothing known yet: no question
    assert h.recorder.confirms == [] and not w.bg_updates.polling
    fake.gate.set()
    qtbot.wait(60)
    assert seen == [] and w.bg_updates.progress is None
    assert not w.bg_updates.track(STATUS)


def test_two_windows_update_runs_in_one_session(wu_window, fake, qtbot) -> None:
    h = wu_window()
    w = h.window
    seen = emissions(w.bg_updates)
    for _ in range(2):
        w.selection.select("windows-update")
        assert w.start_install()
        wait_run_finished(qtbot, w)
        w.back_to_selection()
    qtbot.waitUntil(lambda: len(seen) >= 1)
    assert w.bg_updates.status_path == DRY_STATUS and w.bg_updates.polling
    assert len(seen) == 1  # same snapshot: no second "changed"
    assert set(fake.reads) == {DRY_STATUS}


# -------------------------------------------------------------------- detect


def test_detect_running_installation_is_followed(wu_window, fake, qtbot) -> None:
    fake.found = dataclasses.replace(INSTALLING, status_path=STATUS)
    w = wu_window().window
    w.start_background_tasks()
    qtbot.waitUntil(lambda: w.bg_updates.polling and shown(w.run_page.bg_panel))
    qtbot.waitUntil(lambda: bool(fake.reads))
    assert fake.reads[0] == STATUS
    assert badge(w) == ("WU 1/4", "accent")
    assert not w.wu_banner.isVisible()


def test_detect_recent_result_is_shown(wu_window, fake, qtbot) -> None:
    fake.found = FINISHED
    w = wu_window().window
    w.start_background_tasks()
    qtbot.waitUntil(lambda: w.wu_banner.isVisible())
    assert shown(w.run_page.bg_panel) and not w.bg_updates.polling
    assert fake.reads == []


def test_detect_result_from_before_the_last_boot_is_not_shown(wu_window, fake, qtbot) -> None:
    fake.found = dataclasses.replace(
        FINISHED, finished=BOOT - timedelta(minutes=1), updated=BOOT - timedelta(minutes=1)
    )
    w = wu_window().window
    assert w.bg_updates.detect(w._background_hidden)
    qtbot.wait(80)
    assert w.bg_updates.progress is None and not w.wu_banner.isVisible()
    assert not shown(w.run_page.bg_panel) and badge(w)[0] == ""


def test_hidden_result_stays_hidden_but_a_new_one_shows(wu_window, fake, qtbot, settings) -> None:
    settings.setValue(SETTINGS_HIDDEN_WU_KEY, STATUS.name)
    fake.found = FINISHED
    w = wu_window().window
    w.start_background_tasks()
    qtbot.wait(80)
    assert w.bg_updates.progress is None and not w.wu_banner.isVisible()
    newer = STATUS.with_name("windows-update-2026.11.22-170000.json")
    fake.found = dataclasses.replace(FINISHED, status_path=newer)
    w2 = wu_window().window
    w2.start_background_tasks()
    qtbot.waitUntil(lambda: w2.wu_banner.isVisible())


def test_detect_failure_is_logged_and_shows_nothing(wu_window, fake, qtbot) -> None:
    fake.found = UpdatesError("boom", kind="format")
    w = wu_window().window
    w.start_background_tasks()
    qtbot.waitUntil(
        lambda: any("Nie udało się sprawdzić instalacji" in line for line in w.log.lines())
    )
    assert w.bg_updates.progress is None


def test_detect_does_not_replace_what_the_session_follows(wu_window, fake, qtbot) -> None:
    fake.found = dataclasses.replace(FINISHED, status_path=STATUS.with_name("old.json"))
    w = wu_window().window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: w.bg_updates.progress is not None)
    w.bg_updates.detect(w._background_hidden)
    qtbot.wait(60)
    assert w.bg_updates.status_path == STATUS and not w.bg_updates.progress.done


def test_unknown_installation_from_before_boot_is_not_followed(wu_window, fake, qtbot) -> None:
    old = InstallProgress(
        state="unknown",
        phase="installing",
        started=BOOT - timedelta(days=3),
        updated=BOOT - timedelta(days=3),
        status_path=STATUS,
    )
    fake.found = old
    fake.value = old
    w = wu_window().window
    w.start_background_tasks()
    qtbot.wait(80)
    assert not w.bg_updates.running and not w.bg_updates.polling


# --------------------------------------------------------------------- panel


@pytest.mark.parametrize(
    ("progress", "title", "tag_list"),
    [
        (SEARCHING, "Wyszukiwanie aktualizacji", ["[OCZEKUJE]"] * 2 + ["[POBRANO]", "[OCZEKUJE]"]),
        (
            DOWNLOADING,
            "Pobieranie 1 z 3",
            ["[POBIERANIE]", "[OCZEKUJE]", "[POBRANO]"] + ["[OCZEKUJE]"],
        ),
        (INSTALLING, "Instalacja 1 z 4", ["[INSTALACJA]"] + ["[POBRANO]"] * 3),
        (FINISHED, "Zakończono — wymagany restart", ["[OK]"] * 4),
    ],
    ids=["searching", "downloading", "installing", "finished"],
)
def test_panel_for_each_phase(wu_window, progress, title, tag_list) -> None:
    w = wu_window().window
    panel = w.run_page.bg_panel
    assert not shown(panel)
    panel.set_progress(progress)
    assert shown(panel) and panel.title_label.text() == title
    done = progress.done
    assert shown(panel.progress_bar) == (not done)
    assert panel.progress_bar.maximum() == max(1, progress.steps_total)
    assert panel.progress_bar.value() == progress.steps_done
    assert tags(panel) == tag_list and shown(panel.item_list)
    assert shown(panel.restart_label) == done
    assert shown(panel.stats_label) == done
    assert shown(panel.cursor) == (not done)
    assert panel.log_button.isEnabled()
    if progress.phase in ("downloading", "installing"):
        assert panel.subtitle_label.text() == progress.current_title
    for text in (panel.title_label.text(), panel.subtitle_label.text(), panel.clock_label.text()):
        assert "None" not in text and not re.search(r"(?<![\w-])-\d", text)
    panel.set_progress(None)
    assert not shown(panel)


def test_simulated_steps_never_go_back() -> None:
    values = [sim(s / 4) for s in range(0, 48)]
    done = [p.steps_done for p in values]
    assert done == sorted(done)
    phases = [p.phase for p in values]
    order = ["searching", "downloading", "installing", "finished"]
    assert [ph for i, ph in enumerate(phases) if i == 0 or ph != phases[i - 1]] == order
    assert values[-1].succeeded and values[-1].reboot_required


@pytest.mark.parametrize(
    ("progress", "title", "tag_list", "restart"),
    [
        (
            final(("succeeded", "succeeded"), reboot_required=True),
            "Zakończono — wymagany restart",
            ["[OK]", "[OK]"],
            True,
        ),
        (final(("succeeded", "succeeded")), "Zakończono", ["[OK]", "[OK]"], False),
        (
            final(("succeeded", "failed", "not_downloaded"), exit_code=2),
            "Zakończono z błędami (2 z 3 nieudane)",
            ["[OK]", "[BŁĄD]", "[BŁĄD]"],
            False,
        ),
        (
            final(("failed", "error"), exit_code=2),
            "Zakończono z błędami (2 z 2 nieudane)",
            ["[BŁĄD]", "[BŁĄD]"],
            False,
        ),
        (
            final(("succeeded", "installing", "pending"), state="stale", exit_code=None),
            "Instalacja przerwana",
            ["[OK]", "[PRZERWANO]", "[POMINIĘTO]"],
            False,
        ),
        (
            final(("pending", "downloaded"), exit_code=5, reboot_required=True),
            "Nie zainstalowano aktualizacji",
            ["[POMINIĘTO]", "[POMINIĘTO]"],
            True,
        ),
    ],
    ids=["ok-restart", "ok", "partial", "all-failed", "interrupted", "exit5"],
)
def test_panel_final_states(wu_window, progress, title, tag_list, restart) -> None:
    w = wu_window().window
    panel = w.run_page.bg_panel
    panel.set_progress(progress)
    assert panel.title_label.text() == title
    assert tags(panel) == tag_list
    assert shown(panel.restart_label) == restart
    assert not shown(panel.progress_bar) and not shown(panel.cursor)
    stats = panel.stats_label.text()
    assert f"[OK]</span>&nbsp;{progress.ok_count}" in stats
    assert f"[BŁĄD]</span>&nbsp;{progress.failed_count}" in stats
    for it in progress.items:
        tone = item_tag(it, True)[1]
        assert (tone == "error") == (it.failed or it.status in ("downloading", "installing"))
        if it.ok:
            assert tone == "text"


def test_failed_updates_stay_listed_in_compact_mode(wu_window, qtbot) -> None:
    w = wu_window().window
    panel = w.run_page.bg_panel
    panel.set_compact(True)
    panel.set_progress(final(("succeeded", "failed", "succeeded"), exit_code=2))
    assert tags(panel) == ["[BŁĄD]"] and panel.list_button.text() == "Lista (3)"
    panel.toggle_list()
    assert tags(panel) == ["[OK]", "[BŁĄD]", "[OK]"]


def test_step_clock(wu_window, clock: Clock) -> None:
    w = wu_window().window
    panel = w.run_page.bg_panel
    step = INSTALLING.step_started
    assert step is not None
    clock.now = step + timedelta(seconds=30)
    panel.set_progress(INSTALLING)
    assert "KROK 00:30" in panel.clock_label.text()
    assert panel.clock_label.text().startswith("START 16:00")
    clock.now = step - timedelta(hours=1)  # clock behind / stepStarted in the future
    panel.set_progress(INSTALLING)
    assert "KROK 00:00" in panel.clock_label.text() and "-" not in panel.clock_label.text()
    clock.now = step + timedelta(hours=3, seconds=5)  # stuck installer: only the clock grows
    panel.set_progress(INSTALLING)
    assert "KROK 3:00:05" in panel.clock_label.text()
    assert panel.elapsed_step() == pytest.approx(3 * 3600 + 5)
    panel.set_progress(FINISHED)
    assert panel.elapsed_step() is None and "KONIEC" in panel.clock_label.text()


def test_open_log_from_panel_and_banner(wu_window, fake, qtbot) -> None:
    fake.value = FINISHED
    h = wu_window()
    w = h.window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: w.wu_banner.isVisible())
    qtbot.mouseClick(w.run_page.bg_panel.log_button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(banner_button(w, "wuBannerLog"), Qt.MouseButton.LeftButton)
    assert h.recorder.opened_paths == [STATUS.with_suffix(".log")] * 2


def test_log_button_disabled_without_a_log(wu_window) -> None:
    w = wu_window().window
    panel = w.run_page.bg_panel
    panel.set_progress(dataclasses.replace(INSTALLING, log_path=None))
    assert not panel.log_button.isEnabled()


# ---------------------------------------------------------- badge and banner


def test_badge_follows_the_installation(wu_window, fake, qtbot) -> None:
    fake.value = DOWNLOADING
    w = wu_window().window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: badge(w)[0] == "WU 1/3")
    assert badge(w)[1] == "accent"
    fake.value = SEARCHING
    qtbot.waitUntil(lambda: badge(w)[0] == "WU …")
    fake.value = final(("succeeded", "failed"), exit_code=2)
    qtbot.waitUntil(lambda: badge(w) == ("WU BŁ.", "error"))


def test_badge_is_grey_while_the_queue_runs(wu_window, fake, qtbot) -> None:
    h = wu_window(gate_ids=("7zip",))
    w = h.window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: badge(w) == ("WU 1/4", "accent"))
    w.selection.select("7zip")
    assert w.start_install()
    assert h.executor.entered.wait(5)
    qtbot.waitUntil(lambda: badge(w) == ("WU 1/4", "muted"))
    assert w.run_page.bg_panel.secondary and not shown(w.run_page.bg_panel.cursor)
    h.executor.release()
    wait_run_finished(qtbot, w)
    qtbot.waitUntil(lambda: badge(w)[1] == "accent")


@pytest.mark.parametrize(
    ("progress", "message", "tone", "detail"),
    [
        (
            FINISHED,
            "Aktualizacje Windows zainstalowane — uruchom ponownie komputer",
            "neutral",
            "Zainstalowano 4 z 4",
        ),
        (
            final(("succeeded",)),
            "Aktualizacje Windows zainstalowane",
            "neutral",
            "Zainstalowano 1 z 1",
        ),
        (
            final(("succeeded", "failed"), exit_code=2),
            "Aktualizacje Windows zakończone z błędami",
            "error",
            "Zainstalowano 1 z 2 · nieudane: 1",
        ),
        (
            final(("succeeded", "installing"), state="stale", exit_code=None),
            "Instalacja aktualizacji Windows przerwana",
            "error",
            "Zainstalowano 1 z 2 · nieudane: 1",
        ),
        (
            final(("pending",), exit_code=6),
            "Nie zainstalowano aktualizacji Windows",
            "error",
            "Windows Update jest zajęty inną instalacją — spróbuj ponownie za kilka minut (kod 6)",
        ),
        (
            final(("succeeded",), state="stale", from_log=True),
            "Aktualizacje Windows zainstalowane",
            "neutral",
            "Zainstalowano 1 z 1 · wynik odczytany z logu",
        ),
    ],
    ids=["ok-restart", "ok", "partial", "interrupted", "busy", "from-log"],
)
def test_banner_texts(wu_window, fake, qtbot, progress, message, tone, detail) -> None:
    fake.value = progress
    w = wu_window().window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: w.wu_banner.isVisible())
    assert w.wu_banner.message.text() == message
    assert w.wu_banner.property("tone") == tone
    assert w.wu_banner.detail.text() == detail
    assert (w.wu_banner.tag.text() == "[BŁĄD]") == (tone == "error")
    assert badge(w)[0] == ("WU OK" if tone == "neutral" else "WU BŁ.")


def test_banner_when_nothing_was_installed(wu_window, fake, qtbot) -> None:
    fake.value = final(())
    w = wu_window().window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: w.wu_banner.isVisible())
    assert "zainstalowane" not in w.wu_banner.message.text()


def test_banner_show_and_hide(wu_window, fake, qtbot, settings) -> None:
    fake.value = FINISHED
    w = wu_window().window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: w.wu_banner.isVisible())
    assert w.current_page_id() != "run"
    qtbot.mouseClick(banner_button(w, "wuBannerShow"), Qt.MouseButton.LeftButton)
    assert w.current_page_id() == "run"
    qtbot.mouseClick(banner_button(w, "wuBannerHide"), Qt.MouseButton.LeftButton)
    assert not w.wu_banner.isVisible()
    settings.sync()
    assert settings.value(SETTINGS_HIDDEN_WU_KEY) == STATUS.name
    # Next start: the same installation stays hidden (banner, block and badge).
    fake.found = FINISHED
    w2 = wu_window().window
    w2.start_background_tasks()
    qtbot.wait(80)
    assert not w2.wu_banner.isVisible() and not shown(w2.run_page.bg_panel)
    assert badge(w2)[0] == ""


def test_hidden_result_banner_does_not_return_in_the_session(wu_window, fake, qtbot) -> None:
    fake.value = FINISHED
    w = wu_window().window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: w.wu_banner.isVisible())
    w.hide_background_result()
    w.bg_updates.track(STATUS)  # e.g. the same installation again ("już trwa")
    qtbot.waitUntil(lambda: not w.bg_updates.polling)
    assert not w.wu_banner.isVisible()


# ------------------------------------------------------------------- geometry


def visible_rect(w: QWidget, widget: QWidget) -> QRect:
    return QRect(widget.mapTo(w, QPoint(0, 0)), widget.size())


@pytest.mark.parametrize("size", [(1100, 720), (900, 600)])
@pytest.mark.parametrize("queue", ["idle", "running", "finished"])
def test_layout_has_room_for_everything(wu_window, fake, qtbot, size, queue) -> None:
    fake.value = FINISHED if queue == "finished" else INSTALLING
    h = wu_window(gate_ids=("7zip",))
    w = h.window
    w.resize(*size)
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: shown(w.run_page.bg_panel))
    if queue != "idle":
        w.selection.select_many(["7zip", "google-chrome"])
        assert w.start_install()
        if queue == "running":
            assert h.executor.entered.wait(5)
        else:
            h.executor.release()
            wait_run_finished(qtbot, w)
    w.navigate("run")
    qtbot.waitUntil(lambda: w.run_page.isVisible())
    qtbot.wait(20)
    panel = w.run_page.bg_panel
    window_rect = QRect(QPoint(0, 0), w.size())
    widgets = [panel, panel.title_label, panel.header, w.run_page.stack]
    widgets += [
        x
        for x in (
            panel.progress_bar,
            panel.item_list,
            panel.restart_label,
            panel.stats_label,
            panel.subtitle_label,
        )
        if x.isVisible()
    ]
    if queue != "idle":
        widgets += [w.run_page.progress, w.run_page.counter_label]
    for widget in widgets:
        if not widget.isVisible():
            continue
        rect = visible_rect(w, widget)
        assert rect.height() > 0 and rect.width() > 0, widget.objectName()
        assert window_rect.contains(rect), (widget.objectName(), rect, window_rect)
    assert visible_rect(w, panel).bottom() < visible_rect(w, w.run_page.stack).top()
    if queue != "idle":
        assert w.run_page.stack.height() >= 120, w.run_page.stack.height()
    h.executor.release()


# -------------------------------------------------------------------- closing


def test_close_with_only_the_background_install(wu_window, fake, qtbot) -> None:
    h = wu_window()
    w = h.window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: w.bg_updates.running)
    h.recorder.answers = [False]
    assert not w.close()
    assert w.isVisible() and len(h.recorder.confirms) == 1
    request = h.recorder.confirms[0]
    assert request.title == "Zamknąć Winstaller?" and request.accept_text == "Zamknij"
    assert "nie przerwie" in request.message and str(STATUS.with_suffix(".log")) in request.message
    h.recorder.answers = [True]
    assert w.close()
    assert not w.isVisible() and not w.bg_updates.polling and len(h.recorder.confirms) == 2


def test_close_with_queue_and_background_asks_once(wu_window, fake, qtbot) -> None:
    h = wu_window(gate_ids=("7zip",))
    w = h.window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: w.bg_updates.running)
    w.selection.select("7zip")
    assert w.start_install()
    assert h.executor.entered.wait(5)
    h.recorder.answers = [False]
    assert not w.close()
    assert len(h.recorder.confirms) == 1
    message = h.recorder.confirms[0].message
    assert "Kolejka jest w toku" in message and "instalują się dalej w tle" in message
    h.executor.release()


@pytest.mark.parametrize("value", [None, FINISHED])
def test_close_without_anything_running_asks_nothing(wu_window, fake, qtbot, value) -> None:
    h = wu_window()
    w = h.window
    if value is not None:
        fake.value = value
        w.bg_updates.track(STATUS)
        qtbot.waitUntil(lambda: w.bg_updates.progress is not None)
    assert w.close()
    assert h.recorder.confirms == []


# -------------------------------------------------------------------- dry run


def test_simulation_with_delay_zero_is_finished_at_once(tmp_path: Path) -> None:
    before = set(tmp_path.rglob("*"))
    p = SimulatedBackgroundInstall(0).read(tmp_path / "x.json")
    assert p.state == "finished" and p.succeeded and p.reboot_required and p.alive is False
    assert set(tmp_path.rglob("*")) == before


def test_simulation_timeline_with_a_clock() -> None:
    clock = Clock(START)
    simulation = SimulatedBackgroundInstall(0.5, clock=clock)  # one step = 4 s
    seen = []
    for second in range(0, 60, 2):
        clock.now = START + timedelta(seconds=second)
        p = simulation.read(STATUS)
        seen.append(p.phase)
        assert p.started == START
    assert seen[0] == "searching" and seen[-1] == "finished"
    assert "downloading" in seen and "installing" in seen
    other = simulation.read(STATUS.with_name("other.json"))
    assert other.phase == "searching"  # every path has its own timeline


def test_services_create_dry_run_wires_the_simulation() -> None:
    services = Services.create(dry_run=True, environ={})
    assert services.find_background_install is not None
    assert services.find_background_install() is None
    assert services.read_install_progress is not None and services.boot_time is not None
    assert services.boot_time() < datetime.now().astimezone()
