"""The "Aktualizacje" section: first on the System page, profile, confirmation, dry run."""

from __future__ import annotations

from PySide6.QtCore import Qt

from tests.ui.conftest import wait_log, wait_run_finished
from winstaller.core.runner import TaskStatus

UPDATE_IDS = ["windows-update", "store-update"]


def test_updates_section_first_on_system_page(make_window) -> None:
    w = make_window().window
    w.navigate("system")
    assert list(w.system_page.sections)[0] == "updates"
    section = w.system_page.sections["updates"]
    assert section.isVisible()
    assert [t.item.id for t in section.tiles] == UPDATE_IDS
    assert [t.item.name for t in section.tiles] == [
        "Windows Update",
        "Aktualizacje Microsoft Store",
    ]
    for tile in section.tiles:
        assert tile.available and tile.interactive
    assert "updates" not in w.apps_page.sections


def test_profile_selects_both_updates(make_window, qtbot) -> None:
    w = make_window().window
    qtbot.mouseClick(w.apps_page.profile_buttons["benchmark-starter"], Qt.MouseButton.LeftButton)
    assert set(UPDATE_IDS) <= set(w.selection.ids())


def test_combined_confirmation_lists_windows_update(make_window) -> None:
    h = make_window()
    w = h.window
    w.selection.select_many([*UPDATE_IDS, "driver-nvidia", "7zip"])
    h.recorder.answers = [False]
    assert not w.start_install()
    assert h.executor.calls == []
    assert len(h.recorder.confirms) == 1
    details = dict(h.recorder.confirms[0].details)
    assert list(details)[0] == "Windows Update"
    assert "restart" in details["Windows Update"]
    assert "Aktualizacje Microsoft Store" not in details


def test_store_alone_needs_no_confirmation(make_window, qtbot) -> None:
    h = make_window()
    h.window.selection.select("store-update")
    assert h.window.start_install()
    wait_run_finished(qtbot, h.window)
    assert h.recorder.confirms == []


def test_dry_run_of_both_updates(make_window, qtbot) -> None:
    h = make_window()
    w = h.window
    w.selection.select_many(["7zip", *reversed(UPDATE_IDS)])
    assert w.start_install()
    wait_run_finished(qtbot, w)
    assert h.executor.calls[:2] == UPDATE_IDS
    rows = {row.item.id: row.status for row in w.run_page.model.rows()}
    assert rows["windows-update"] == TaskStatus.OK and rows["store-update"] == TaskStatus.OK
    wait_log(qtbot, w, "instalacja uruchomiona w tle (dry-run)")
    wait_log(qtbot, w, "Zlecono aktualizację 3 aplikacji Microsoft Store (dry-run)")


def test_dry_run_failure_is_retryable(make_window, qtbot) -> None:
    h = make_window(fail_ids={"store-update"})
    w = h.window
    w.selection.select_many(UPDATE_IDS)
    assert w.start_install()
    wait_run_finished(qtbot, w)
    assert w.run.last_summary.failed_ids == ["store-update"]


def test_updates_available_on_arm64(make_window, qtbot) -> None:
    h = make_window(arch="arm64")
    w = h.window
    section = w.system_page.sections["updates"]
    assert all(t.available for t in section.tiles)
    for item_id in UPDATE_IDS:
        assert w.selection.is_selectable(item_id)
    w.selection.select_many(UPDATE_IDS)
    assert w.start_install()
    wait_run_finished(qtbot, w)
    assert [r.status for r in w.run.last_summary.results] == [TaskStatus.OK, TaskStatus.OK]


def test_select_all_in_updates_section(make_window, qtbot) -> None:
    w = make_window().window
    section = w.system_page.sections["updates"]
    qtbot.mouseClick(section.toggle_button, Qt.MouseButton.LeftButton)
    assert set(w.selection.ids()) == set(UPDATE_IDS)
    assert section.counter.text() == "2 / 2"
    qtbot.mouseClick(section.toggle_button, Qt.MouseButton.LeftButton)
    assert w.selection.ids() == []
