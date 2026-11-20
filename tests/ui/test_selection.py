"""Main window start-up, navigation, search, tiles, profiles and the action bar."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton

from tests.ui.conftest import LOCAL_DIRS
from winstaller.ui.main_window import PAGES, SELECTION_PAGES
from winstaller.ui.widgets.item_tile import ItemTile
from winstaller.ui.widgets.nav import NavButton


def activate(qtbot, window) -> None:
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)


def nav_button(window, page_id: str) -> NavButton:
    button = window.findChild(NavButton, f"nav_{page_id}")
    assert button is not None
    return button


# ------------------------------------------------------------- start / nav


def test_window_starts_on_apps_with_search_focused(make_window, qtbot) -> None:
    h = make_window()
    w = h.window
    assert w.windowTitle() == "Winstaller 2.0.0  [DRY-RUN]"
    assert w.current_page_id() == "apps"
    assert w.action_bar.isVisible()
    assert not w.update_banner.isVisible() and not w.winget_banner.isVisible()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is w.apps_page.search)


def test_navigation_through_every_page(make_window, qtbot) -> None:
    w = make_window().window
    for page_id, _title in PAGES:
        with qtbot.waitSignal(w.pageChanged) as blocker:
            qtbot.mouseClick(nav_button(w, page_id), Qt.MouseButton.LeftButton)
        assert blocker.args == [page_id]
        assert w.current_page_id() == page_id
        assert w.stack.currentWidget() is w.pages[page_id]
        assert nav_button(w, page_id).isChecked()
        assert w.action_bar.isVisible() == (page_id in SELECTION_PAGES)


def test_keyboard_shortcuts_switch_pages(make_window, qtbot) -> None:
    w = make_window().window
    activate(qtbot, w)
    for n, (page_id, _title) in enumerate(PAGES, start=1):
        qtbot.keyClick(w, Qt.Key(Qt.Key.Key_0 + n), Qt.KeyboardModifier.ControlModifier)
        assert w.current_page_id() == page_id
    qtbot.keyClick(w, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert w.current_page_id() == "apps"


# ----------------------------------------------------------------- search


def visible(page) -> set[str]:
    return set(page.visible_ids())


def test_search_filters_tiles(make_window, qtbot) -> None:
    w = make_window().window
    page = w.apps_page
    all_ids = visible(page)
    expected = {i for i, t in page.tiles.items() if t.matches("cpu")}
    assert "cpu-z" in expected and len(expected) < len(all_ids)
    with qtbot.waitSignal(page.filterChanged) as blocker:  # debounced (60 ms)
        qtbot.keyClicks(page.search, "cpu")
    assert blocker.args == ["cpu", len(expected)]
    assert visible(page) == expected
    assert not page.empty_state.isVisible()
    # Sections without hits are hidden entirely.
    assert page.sections["diagnostics"].isVisible()
    assert not page.sections["launchers"].isVisible()
    qtbot.keyClick(page.search, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: visible(page) == all_ids)


def test_search_matches_description_and_winget_id(make_window) -> None:
    w = make_window().window
    assert w.apps_page.set_filter("Valve.Steam") == 1
    assert visible(w.apps_page) == {"steam"}


def test_search_without_hits_shows_empty_state(make_window) -> None:
    w = make_window().window
    page = w.apps_page
    assert page.set_filter("zzzz-nic") == 0
    assert visible(page) == set()
    assert page.empty_state.isVisible()
    assert page.empty_title.text() == "Nic nie pasuje do „zzzz-nic”."
    assert not page.cross_button.isVisible()


def test_search_hint_jumps_to_the_other_page(make_window, qtbot) -> None:
    w = make_window().window
    page = w.apps_page
    assert page.set_filter("onedrive") == 0
    assert page.cross_button.isVisible()
    assert page.cross_button.text() == "2 trafienia na stronie System →"
    qtbot.mouseClick(page.cross_button, Qt.MouseButton.LeftButton)
    assert w.current_page_id() == "system"
    assert w.system_page.search.text() == "onedrive"
    assert visible(w.system_page) == {"onedrive-install", "onedrive-uninstall"}
    # and back: a system search hinting at the apps page
    assert w.system_page.set_filter("steam") == 0
    assert w.system_page.cross_button.text() == "1 trafienie na stronie Aplikacje →"


# ------------------------------------------------------------------ tiles


def tile(window, item_id: str) -> ItemTile:
    page = window.apps_page if item_id in window.apps_page.tiles else window.system_page
    return page.tiles[item_id]


def test_tile_toggles_with_mouse(make_window, qtbot) -> None:
    w = make_window().window
    t = tile(w, "steam")
    qtbot.mouseClick(t, Qt.MouseButton.LeftButton)
    assert w.selection.is_selected("steam") and t.checked
    qtbot.mouseClick(t, Qt.MouseButton.LeftButton)
    assert not w.selection.is_selected("steam") and not t.checked


def test_tile_toggles_with_keyboard_and_arrows_move_focus(make_window, qtbot) -> None:
    w = make_window().window
    activate(qtbot, w)
    first = tile(w, "google-chrome")
    first.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(first.hasFocus)
    qtbot.keyClick(first, Qt.Key.Key_Space)
    assert w.selection.is_selected("google-chrome")
    qtbot.keyClick(first, Qt.Key.Key_Return)
    assert not w.selection.is_selected("google-chrome")
    qtbot.keyClick(first, Qt.Key.Key_Right)
    focused = QApplication.focusWidget()
    assert isinstance(focused, ItemTile) and focused is not first
    assert focused.item.id == "signal"
    qtbot.keyClick(focused, Qt.Key.Key_Left)
    assert QApplication.focusWidget() is first


def test_unavailable_items_are_not_clickable(make_window, qtbot) -> None:
    w = make_window().window
    # The arm64-only copy is unavailable on x64 even though its folder exists.
    t = tile(w, "copy-benchmark-tools-arm")
    assert not t.available and not t.interactive
    assert t.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert "Niedostępne:" in t.toolTip()
    qtbot.mouseClick(t, Qt.MouseButton.LeftButton)
    qtbot.keyClick(t, Qt.Key.Key_Space)
    assert not w.selection.is_selected("copy-benchmark-tools-arm")


def test_local_items_without_files_are_unavailable(make_window, qtbot, app_dir) -> None:
    (app_dir / "procyon" / "procyon-setup.exe").unlink()
    w = make_window().window
    t = tile(w, "ul-procyon")
    assert not t.available and "procyon-setup.exe" in t.toolTip()
    qtbot.mouseClick(t, Qt.MouseButton.LeftButton)
    assert not w.selection.is_selected("ul-procyon")


# --------------------------------------------------------------- profiles


def profile_chip(window) -> QPushButton:
    return window.apps_page.profile_buttons["benchmark-starter"]


def expected_profile(window, arch: str) -> set[str]:
    ids = window.services.catalog.profile("benchmark-starter").items_for(arch)
    return {i for i in ids if window.selection.is_selectable(i)}


def test_profile_selects_and_deselects_its_set_x64(make_window, qtbot) -> None:
    w = make_window(arch="x64").window
    chip = profile_chip(w)
    expected = expected_profile(w, "x64")
    assert "copy-benchmark-tools-arm" not in expected
    assert "copy-benchmark-tools" in expected and "ul-procyon" in expected
    assert chip.text() == f"Benchmark starter  {len(expected)}"
    qtbot.mouseClick(chip, Qt.MouseButton.LeftButton)
    assert set(w.selection.ids()) == expected
    assert chip.isChecked()
    # Items of both pages are part of the profile.
    assert w.apps_page.selected_count() and w.system_page.selected_count()
    qtbot.mouseClick(chip, Qt.MouseButton.LeftButton)
    assert w.selection.ids() == [] and not chip.isChecked()


def test_profile_on_arm64_adds_the_arm_extra(make_window, qtbot) -> None:
    w = make_window(arch="arm64").window
    expected = expected_profile(w, "arm64")
    assert "copy-benchmark-tools-arm" in expected
    qtbot.mouseClick(profile_chip(w), Qt.MouseButton.LeftButton)
    assert set(w.selection.ids()) == expected
    assert w.selection.is_selected("copy-benchmark-tools-arm")


def test_profile_skips_unavailable_items(make_window, qtbot, app_dir) -> None:
    (app_dir / "procyon" / "procyon-setup.exe").unlink()
    w = make_window().window
    qtbot.mouseClick(profile_chip(w), Qt.MouseButton.LeftButton)
    assert "ul-procyon" not in w.selection.ids()
    assert profile_chip(w).isChecked()  # everything selectable is selected


def test_local_dirs_fixture_matches_catalog() -> None:
    assert set(LOCAL_DIRS) == {"BenchmarkTools", "BenchmarkToolsARM"}


# ---------------------------------------------------- category / action bar


def test_select_all_in_category(make_window, qtbot) -> None:
    w = make_window().window
    section = w.apps_page.sections["launchers"]
    launchers = [t.item.id for t in section.tiles]
    qtbot.mouseClick(section.toggle_button, Qt.MouseButton.LeftButton)
    assert set(w.selection.ids()) == set(launchers)
    assert section.toggle_button.text() == "Odznacz wszystkie"
    assert section.counter.text() == f"{len(launchers)} / {len(launchers)}"
    qtbot.mouseClick(section.toggle_button, Qt.MouseButton.LeftButton)
    assert w.selection.ids() == []
    assert section.toggle_button.text() == "Zaznacz wszystkie"


def test_select_all_skips_unavailable(make_window, qtbot) -> None:
    w = make_window().window
    section = w.system_page.sections["extras"]
    qtbot.mouseClick(section.toggle_button, Qt.MouseButton.LeftButton)
    assert "copy-benchmark-tools-arm" not in w.selection.ids()
    usable = [t.item.id for t in section.tiles if t.available]
    assert section.counter.text() == f"{len(usable)} / {len(usable)}"


def test_counter_and_install_button_state(make_window, qtbot) -> None:
    w = make_window().window
    bar = w.action_bar
    assert not bar.install_button.isEnabled() and bar.install_button.text() == "Zainstaluj"
    assert bar.count_label.text() == "Zaznaczono 0"
    assert not bar.clear_button.isEnabled()
    qtbot.mouseClick(tile(w, "steam"), Qt.MouseButton.LeftButton)
    qtbot.mouseClick(tile(w, "7zip"), Qt.MouseButton.LeftButton)
    assert bar.install_button.isEnabled()
    assert bar.install_button.text() == "Zainstaluj (2)  →"
    assert bar.count_label.text() == "Zaznaczono 2"
    assert nav_button(w, "apps").badge == "2"
    w.system_page.tiles["dark-theme"].toggleRequested.emit("dark-theme")
    assert bar.count_label.text() == "Zaznaczono 3"
    assert bar.hint_label.text() == "w tym 1 na innej stronie"
    assert nav_button(w, "system").badge == "1"
    qtbot.mouseClick(bar.clear_button, Qt.MouseButton.LeftButton)
    assert w.selection.ids() == [] and not bar.install_button.isEnabled()
    assert nav_button(w, "apps").badge == ""
