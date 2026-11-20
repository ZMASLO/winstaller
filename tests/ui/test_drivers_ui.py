"""The "Sterowniki GPU" section: tiles, confirmation, dry run, ARM64, profile, select all."""

from __future__ import annotations

from PySide6.QtCore import Qt

from tests.ui.conftest import wait_log, wait_run_finished
from winstaller.core.runner import TaskStatus
from winstaller.ui.widgets.item_tile import kind_tag

DRIVER_IDS = ["driver-nvidia", "driver-amd", "driver-intel"]


def test_section_with_three_driver_tiles(make_window) -> None:
    w = make_window().window
    section = w.apps_page.sections["gpu-drivers"]
    assert section.isVisible()
    assert [t.item.id for t in section.tiles] == DRIVER_IDS
    for tile in section.tiles:
        assert kind_tag(tile.item) == "[STEROWNIK]"
        assert tile.available and tile.interactive
    order = list(w.apps_page.sections)
    assert order.index("gpu-drivers") == order.index("testing") + 1


def test_drivers_need_one_combined_confirmation(make_window, qtbot) -> None:
    h = make_window()
    w = h.window
    w.selection.select_many([*DRIVER_IDS, "7zip"])
    h.recorder.answers = [False]
    assert not w.start_install()
    assert h.executor.calls == []
    assert len(h.recorder.confirms) == 1
    names = [name for name, _ in h.recorder.confirms[0].details]
    assert names == ["NVIDIA GeForce Game Ready", "AMD Radeon Adrenalin", "Intel Arc"]
    assert all("wymaga ponownego uruchomienia" in d for _, d in h.recorder.confirms[0].details)


def test_dry_run_of_a_driver(make_window, qtbot) -> None:
    h = make_window()
    w = h.window
    w.selection.select("driver-amd")
    assert w.start_install()
    wait_run_finished(qtbot, w)
    assert h.executor.calls == ["driver-amd"]
    rows = {row.item.id: row.status for row in w.run_page.model.rows()}
    assert rows == {"driver-amd": TaskStatus.OK}
    wait_log(qtbot, w, "Uruchom ponownie komputer")
    wait_log(qtbot, w, "Najnowsza wersja: 26.11.2")
    assert len(h.recorder.confirms) == 1


def test_dry_run_driver_failure_is_retryable(make_window, qtbot) -> None:
    h = make_window(fail_ids={"driver-intel"})
    w = h.window
    w.selection.select_many(["driver-nvidia", "driver-intel"])
    assert w.start_install()
    wait_run_finished(qtbot, w)
    assert w.run.last_summary.failed_ids == ["driver-intel"]
    assert w.selection.ids() == ["driver-intel"]


def test_drivers_unavailable_on_arm64(make_window) -> None:
    w = make_window(arch="arm64").window
    section = w.apps_page.sections["gpu-drivers"]
    assert all(not t.available for t in section.tiles)
    for item_id in DRIVER_IDS:
        assert not w.selection.is_selectable(item_id)
        w.selection.select(item_id)
    assert w.selection.ids() == []


def test_profile_does_not_select_drivers(make_window, qtbot) -> None:
    w = make_window().window
    qtbot.mouseClick(w.apps_page.profile_buttons["benchmark-starter"], Qt.MouseButton.LeftButton)
    assert w.selection.ids()
    assert not set(w.selection.ids()) & set(DRIVER_IDS)


def test_select_all_in_driver_section(make_window, qtbot) -> None:
    w = make_window().window
    section = w.apps_page.sections["gpu-drivers"]
    qtbot.mouseClick(section.toggle_button, Qt.MouseButton.LeftButton)
    assert set(w.selection.ids()) == set(DRIVER_IDS)
    assert section.counter.text() == "3 / 3"
    qtbot.mouseClick(section.toggle_button, Qt.MouseButton.LeftButton)
    assert w.selection.ids() == []


def test_select_all_on_arm64_selects_nothing(make_window, qtbot) -> None:
    w = make_window(arch="arm64").window
    section = w.apps_page.sections["gpu-drivers"]
    if section.toggle_button.isEnabled():
        qtbot.mouseClick(section.toggle_button, Qt.MouseButton.LeftButton)
    assert w.selection.ids() == []
