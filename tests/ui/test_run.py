"""The install queue: confirmation, a full dry run, retry, Stop, locking, closing."""

from __future__ import annotations

from PySide6.QtCore import Qt

from winstaller.core.runner import TaskStatus
from winstaller.ui.bridge import RunState
from winstaller.ui.widgets.nav import NavButton

S = TaskStatus


def statuses(window) -> dict[str, TaskStatus]:
    return {row.item.id: row.status for row in window.run_page.model.rows()}


def start(qtbot, window, ids: list[str] | None = None, timeout: int = 5000):
    """Start the queue and wait for ``runFinished``; returns the summary."""
    if ids is not None:
        window.selection.select_many(ids)
    with qtbot.waitSignal(window.run.runFinished, timeout=timeout) as blocker:
        assert window.start_install()
    return blocker.args[0]


# ----------------------------------------------------------- confirmation


def test_destructive_items_need_one_combined_confirmation(make_window, qtbot) -> None:
    h = make_window()
    w = h.window
    w.selection.select_many(["steam", "remove-bloat", "dark-theme"])
    h.recorder.answers = [False]
    assert not w.start_install()
    assert len(h.recorder.confirms) == 1
    request = h.recorder.confirms[0]
    assert request.title == "Potwierdź operację systemową"
    assert [name for name, _ in request.details] == ["Usuń bloatware z Windows"]
    assert "1 pozycję" in request.message
    assert not w.run.busy and w.current_page_id() == "apps"
    assert h.executor.calls == []
    qtbot.waitUntil(lambda: any("anulowany przez użytkownika" in x for x in w.log.lines()))
    # accept: the queue starts once, with everything
    summary = start(qtbot, w)
    assert len(h.recorder.confirms) == 2
    assert h.executor.calls == ["steam", "remove-bloat", "dark-theme"]
    assert summary.ok_count == 3


def test_no_confirmation_without_destructive_items(make_window, qtbot) -> None:
    h = make_window()
    start(qtbot, h.window, ["steam", "7zip"])
    assert h.recorder.confirms == []


# -------------------------------------------------------------- full run


def test_full_dry_run(make_window, qtbot) -> None:
    h = make_window(fail_ids={"hwinfo"})
    w = h.window
    ids = ["steam", "hwinfo", "7zip", "dark-theme"]
    w.selection.select_many([*ids, "google-chrome"])
    w.selection.deselect("google-chrome")
    with (
        qtbot.waitSignal(w.run.runStarted, timeout=3000),
        qtbot.waitSignal(w.run.runFinished, timeout=5000) as finished,
    ):
        assert w.start_install()
        assert w.current_page_id() == "run"
        assert w.run.busy
    summary = finished.args[0]
    assert h.executor.calls == ["7zip", "steam", "hwinfo", "dark-theme"]  # catalog order
    assert statuses(w) == {"7zip": S.OK, "steam": S.OK, "hwinfo": S.FAILED, "dark-theme": S.OK}
    assert (summary.ok_count, summary.failed_count) == (3, 1)
    page = w.run_page
    assert page.current_label.text() == "Zakończono z błędami"
    assert page.summary_label.isVisible()
    assert "[OK]</span>&nbsp;3" in page.summary_label.text()
    assert "[BŁĄD]</span>&nbsp;1" in page.summary_label.text()
    assert page.counter_label.text() == "// PODSUMOWANIE — 4 / 4"
    assert not page.stop_button.isVisible()
    assert page.retry_button.isVisible() and page.retry_button.text() == "Ponów nieudane (1)"
    assert page.back_button.isVisible()
    # successful items leave the selection, failed ones stay for another try
    assert w.selection.ids() == ["hwinfo"]
    assert w.findChild(NavButton, "nav_run").badge == "1 BŁ."
    qtbot.waitUntil(
        lambda: any(
            x.startswith("[winstaller] Koniec kolejki: OK 3, błędy 1") for x in w.log.lines()
        )
    )
    qtbot.waitUntil(lambda: "--- Steam ---" in w.run_page.log_view.toPlainText())
    qtbot.mouseClick(page.back_button, Qt.MouseButton.LeftButton)
    assert w.current_page_id() == "apps"


def test_retry_runs_only_failed(make_window, qtbot) -> None:
    h = make_window(fail_ids={"steam"})
    w = h.window
    first = start(qtbot, w, ["steam", "7zip", "cpu-z"])
    assert first.failed_ids == ["steam"]
    h.executor.calls.clear()
    with qtbot.waitSignal(w.run.runFinished, timeout=5000) as blocker:
        qtbot.mouseClick(w.run_page.retry_button, Qt.MouseButton.LeftButton)
    assert h.executor.calls == ["steam"]
    assert [r.item_id for r in blocker.args[0].results] == ["steam"]
    assert list(statuses(w)) == ["steam"]


def test_unavailable_item_in_queue_is_skipped(make_window, qtbot, app_dir) -> None:
    h = make_window()
    (app_dir / "RTSSSetup734.exe").unlink()  # disappears after the window was built
    summary = start(qtbot, h.window, ["local-rtss", "7zip"])
    assert statuses(h.window)["local-rtss"] is S.SKIPPED
    assert summary.skipped_count == 1 and "local-rtss" not in h.executor.calls


# ------------------------------------------------------------------- stop


def test_stop_during_run_cancels_the_rest(make_window, qtbot) -> None:
    h = make_window(gate_ids=("steam",))
    w = h.window
    w.selection.select_many(["7zip", "steam", "epic-games", "cpu-z"])
    assert w.start_install()
    assert h.executor.entered.wait(5)
    qtbot.waitUntil(lambda: statuses(w).get("steam") is S.RUNNING)
    page = w.run_page
    assert page.stop_button.isEnabled()
    with qtbot.waitSignal(w.run.runFinished, timeout=5000) as blocker:
        qtbot.mouseClick(page.stop_button, Qt.MouseButton.LeftButton)
        assert page.stop_button.text() == "Zatrzymywanie…"
        assert not page.stop_button.isEnabled()
        assert w.run.state == RunState.STOPPING
        assert "Zatrzymywanie" in page.status_label.text()
    summary = blocker.args[0]
    assert statuses(w) == {
        "7zip": S.OK,
        "steam": S.CANCELLED,
        "epic-games": S.CANCELLED,
        "cpu-z": S.CANCELLED,
    }
    assert summary.cancelled and page.current_label.text() == "Zatrzymano"
    assert h.executor.calls == ["7zip", "steam"]
    assert page.retry_button.text() == "Ponów nieudane (3)"
    assert set(w.selection.ids()) == {"steam", "epic-games", "cpu-z"}


def test_stop_right_after_start_is_never_lost(make_window, qtbot) -> None:
    # Stage-2 workaround (retry every 50 ms) is gone: the cancel event exists before
    # the worker thread even enters TaskRunner.run().
    h = make_window()
    w = h.window
    w.selection.select_many(["7zip", "steam"])
    with qtbot.waitSignal(w.run.runFinished, timeout=5000) as blocker:
        assert w.start_install()
        assert w.run.state == RunState.STARTING
        w.stop_run()
        assert w.run.state == RunState.STOPPING
        assert w.run_page.stop_button.text() == "Zatrzymywanie…"
    assert h.executor.calls == []
    assert all(r.status is S.CANCELLED for r in blocker.args[0].results)
    assert w.run_page.stop_button.text() == "Zatrzymywanie…"  # run_started did not reset it


def test_selection_and_tools_are_locked_while_running(make_window, qtbot) -> None:
    h = make_window(gate_ids=("steam",))
    w = h.window
    w.selection.select_many(["steam"])
    assert w.start_install()
    assert h.executor.entered.wait(5)
    assert w.selection.locked
    tile = w.apps_page.tiles["7zip"]
    assert not tile.interactive
    qtbot.mouseClick(tile, Qt.MouseButton.LeftButton)
    assert not w.selection.is_selected("7zip")
    assert not w.apps_page.profile_buttons["benchmark-starter"].isEnabled()
    assert not w.apps_page.sections["basic"].toggle_button.isEnabled()
    assert not w.action_bar.install_button.isEnabled()
    assert w.action_bar.hint_label.text() == "Wybór zablokowany na czas instalacji"
    assert all(not b.isEnabled() for b in w.tools_page.buttons())
    assert not w.tools_page.lock_note.isHidden()
    assert not w.start_install(["7zip"])
    assert not w.run_tool("battery-report")
    assert not w.start_update()
    h.executor.release()
    qtbot.waitUntil(lambda: not w.run.busy, timeout=5000)
    assert not w.selection.locked and tile.interactive
    assert all(b.isEnabled() for b in w.tools_page.buttons())
    assert w.tools_page.lock_note.isHidden()


# ------------------------------------------------------------------ close


def test_close_during_run_confirmed_cancels_then_closes(make_window, qtbot) -> None:
    h = make_window(gate_ids=("steam",))
    w = h.window
    w.selection.select_many(["steam", "7zip", "cpu-z"])
    assert w.start_install()
    assert h.executor.entered.wait(5)
    h.recorder.answers = [True]
    assert not w.close()  # ignored for now: waits for the queue to stop
    assert h.recorder.confirms[-1].title == "Przerwać instalację i zamknąć?"
    assert w.isVisible() and w.run.state == RunState.STOPPING
    qtbot.waitUntil(lambda: not w.isVisible(), timeout=5000)
    summary = w.run.last_summary
    assert summary is not None and summary.cancelled
    assert [r.status for r in summary.results] == [S.OK, S.CANCELLED, S.CANCELLED]


def test_close_during_run_rejected_keeps_running(make_window, qtbot) -> None:
    h = make_window(gate_ids=("steam",))
    w = h.window
    w.selection.select_many(["steam", "cpu-z"])
    assert w.start_install()
    assert h.executor.entered.wait(5)
    qtbot.waitUntil(lambda: w.run.state == RunState.RUNNING)
    h.recorder.answers = [False]
    assert not w.close()
    assert w.isVisible() and w.run.state == RunState.RUNNING
    h.executor.release()
    qtbot.waitUntil(lambda: not w.run.busy, timeout=5000)
    assert w.isVisible()
    assert w.run.last_summary is not None and w.run.last_summary.ok_count == 2


def test_close_when_idle_needs_no_confirmation(make_window) -> None:
    h = make_window()
    assert h.window.close()
    assert h.recorder.confirms == []
