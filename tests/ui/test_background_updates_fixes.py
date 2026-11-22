"""UI fixes after the independent review (points 2, 8, 12, 15, 16, 17): lost status,
nothing to install, stall warning, a cancelled start confirmation, "Ukryj" in the
session, dates and the log button."""

from __future__ import annotations

import dataclasses
from datetime import timedelta

from PySide6.QtCore import Qt

from tests.ui import test_background_updates as _base
from tests.ui.test_background_updates import (
    BOOT,
    INSTALLING,
    START,
    STATUS,
    badge,
    banner_button,
    final,
    ok_result,
    shown,
)
from winstaller.core.runner import TaskStatus
from winstaller.core.updates import UpdatesError
from winstaller.ui.bridge import BackgroundUpdateMonitor
from winstaller.ui.widgets.background_panel import STALL_SECONDS

# The fixtures of the base module (registered here by name).
clock, fake, wu_window = _base.clock, _base.fake, _base.wu_window


def test_cancelled_start_confirmation_with_a_log_is_followed(wu_window, fake, qtbot) -> None:
    w = wu_window().window
    assert w.bg_updates.track_result(ok_result(status=TaskStatus.CANCELLED))
    qtbot.waitUntil(lambda: bool(fake.reads))
    assert fake.reads[0] == STATUS


def test_lost_status_is_final_with_a_message(wu_window, fake, qtbot, monkeypatch) -> None:
    monkeypatch.setattr(BackgroundUpdateMonitor, "MAX_FAILURES", 2)
    h = wu_window()
    w = h.window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: w.bg_updates.progress is not None)
    fake.value = UpdatesError("Nie znaleziono pliku.", kind="format")
    qtbot.waitUntil(lambda: w.bg_updates.progress.state == "unknown")
    panel = w.run_page.bg_panel
    assert panel.title_label.text() == "Stan instalacji nieznany"
    assert "Nie można odczytać stanu instalacji — sprawdź log" in panel.subtitle_label.text()
    assert badge(w) == ("WU ?", "error") and not shown(panel.cursor)
    assert w.close() and h.recorder.confirms == []  # nothing runs any more


def test_nothing_to_install_banner_and_panel(wu_window, fake, qtbot) -> None:
    fake.value = final(())
    w = wu_window().window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: w.wu_banner.isVisible())
    assert w.wu_banner.message.text() == "Brak aktualizacji Windows do zainstalowania"
    assert w.wu_banner.property("tone") == "neutral"
    assert w.run_page.bg_panel.title_label.text() == (
        "Zakończono — brak aktualizacji do zainstalowania"
    )


def test_incomplete_result_is_not_ok(wu_window, fake, qtbot) -> None:
    fake.value = final(("succeeded", "skipped"))
    w = wu_window().window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: w.wu_banner.isVisible())
    assert w.wu_banner.message.text() == "Instalacja aktualizacji Windows niepełna"
    assert badge(w) == ("WU BŁ.", "error")
    assert "pominięte: 1" in w.wu_banner.detail.text()


def test_stall_warning_only_after_the_threshold(wu_window, clock) -> None:
    w = wu_window().window
    panel = w.run_page.bg_panel
    step = INSTALLING.step_started
    assert step is not None
    clock.now = step + timedelta(seconds=STALL_SECONDS - 60)
    panel.set_progress(INSTALLING)
    assert "Brak postępu" not in panel.subtitle_label.text() and not panel.stalled()
    clock.now = step + timedelta(seconds=STALL_SECONDS + 60)
    panel.set_progress(INSTALLING)
    text = panel.subtitle_label.text()
    assert panel.stalled() and text.startswith(INSTALLING.current_title)
    assert "Brak postępu od 1:31:00 — sprawdź log." in text
    assert panel.title_label.text() == "Instalacja 1 z 4"  # the state is not changed


def test_hide_hides_banner_badge_and_finished_block(wu_window, fake, qtbot) -> None:
    fake.value = final(("succeeded",))
    w = wu_window().window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: w.wu_banner.isVisible())
    qtbot.mouseClick(banner_button(w, "wuBannerHide"), Qt.MouseButton.LeftButton)
    assert not w.wu_banner.isVisible() and badge(w)[0] == ""
    assert not shown(w.run_page.bg_panel)


def test_running_installation_cannot_be_hidden(wu_window, fake, qtbot) -> None:
    w = wu_window().window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: shown(w.run_page.bg_panel))
    w.hide_background_result()
    assert shown(w.run_page.bg_panel) and badge(w)[0].startswith("WU ")


def test_date_for_another_day(wu_window, clock) -> None:
    w = wu_window().window
    panel = w.run_page.bg_panel
    clock.now = START + timedelta(days=1)
    panel.set_progress(
        final(("succeeded",), started=START - timedelta(days=1), finished=START + timedelta(days=1))
    )
    text = panel.clock_label.text()
    assert f"START {START - timedelta(days=1):%Y.%m.%d %H:%M}" in text
    assert f"KONIEC {START + timedelta(days=1):%H:%M}" in text and "KONIEC 20" not in text


def test_log_button_follows_the_file(wu_window, fake, qtbot) -> None:
    fake.value = final(("succeeded",))
    w = wu_window(path_exists=lambda _p: False).window
    w.bg_updates.track(STATUS)
    qtbot.waitUntil(lambda: w.wu_banner.isVisible())
    assert not w.run_page.bg_panel.log_button.isEnabled()
    assert "wuBannerLog" not in [b.objectName() for b in w.wu_banner.buttons()]


def test_unknown_from_before_boot_is_not_shown(wu_window, fake, qtbot) -> None:
    fake.found = dataclasses.replace(
        final(()), state="unknown", finished=None, updated=BOOT - timedelta(hours=1)
    )
    w = wu_window().window
    w.start_background_tasks()
    qtbot.wait(80)
    assert w.bg_updates.progress is None and badge(w)[0] == ""
