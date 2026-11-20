"""Session log (LogStore / LogView), the Dziennik page and the Narzędzia page."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, Qt, QTimer
from PySide6.QtGui import QGuiApplication

from winstaller.core.actions import ActionContext, ActionOutcome
from winstaller.core.catalog import Item
from winstaller.core.runner import TaskStatus
from winstaller.ui.bridge import MAX_LOG_LINES, LogStore
from winstaller.ui.widgets.log_view import LogView

# ------------------------------------------------------------------ store


def test_replace_last_overwrites_the_last_line(qtbot) -> None:
    store = LogStore()
    view = LogView(store)
    qtbot.addWidget(view)
    store.push_replace("first")  # nothing to replace yet: appended
    store.push_line("Pobieranie")
    store.push_line("10%")
    with qtbot.waitSignal(store.batch):
        pass
    assert store.lines() == ["first", "Pobieranie", "10%"]
    store.push_replace("50%")
    store.push_replace("100%")
    with qtbot.waitSignal(store.batch) as blocker:
        pass
    assert blocker.args == [True, ["100%"]]
    assert store.lines() == ["first", "Pobieranie", "100%"]
    assert view.toPlainText() == "first\nPobieranie\n100%"
    assert view.last_line() == "100%" and view.line_count() == 3
    # a replace following new lines in the same batch replaces the newest of them
    store.push_line("a")
    store.push_replace("b")
    store.flush()
    assert store.lines()[-2:] == ["100%", "b"]
    assert view.last_line() == "b"


def test_line_limit(qtbot) -> None:
    store = LogStore(max_lines=10)
    view = LogView(store)
    qtbot.addWidget(view)
    for n in range(25):
        store.push_line(f"line {n}")
    store.flush()
    assert len(store) == 10 and store.lines()[0] == "line 15"
    assert view.line_count() == 10
    assert view.toPlainText().splitlines() == [f"line {n}" for n in range(15, 25)]
    store.clear()
    assert len(store) == 0 and view.line_count() == 0 and view.toPlainText() == ""


def test_window_log_limit(make_window) -> None:
    w = make_window().window
    for n in range(MAX_LOG_LINES + 500):
        w.log.push_line(f"x{n}")
    w.log.flush()
    assert len(w.log) == MAX_LOG_LINES
    assert w.log_page.view.line_count() == MAX_LOG_LINES
    assert w.run_page.log_view.line_count() == MAX_LOG_LINES
    assert w.log_page.count_label.text() == f"{MAX_LOG_LINES} linii · limit {MAX_LOG_LINES}"


def test_flood_of_lines_does_not_block_the_gui(make_window, qtbot) -> None:
    """30 000 lines (with progress-bar replaces) pushed from a worker thread, as fast
    as possible: the GUI thread keeps ticking; the longest stall stays in budget."""
    w = make_window().window
    w.navigate("run")  # both log views exist; the run view is the visible one
    slack = max(1.0, float(os.environ.get("WINSTALLER_PERF_SLACK", "1") or 1))
    budget_ms = 250 * slack
    gaps: list[float] = []
    clock = QElapsedTimer()
    clock.start()
    last = [clock.elapsed()]

    def tick() -> None:
        now = clock.elapsed()
        gaps.append(now - last[0])
        last[0] = now

    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(tick)
    timer.start()
    total = 30_000

    def flood() -> None:
        for n in range(total):
            if n % 10 == 9:
                w.log.push_replace(f"postęp {n}")
            else:
                w.log.push_line(f"linia {n} " + "x" * 60)

    started = time.perf_counter()
    worker = threading.Thread(target=flood)
    worker.start()
    qtbot.waitUntil(lambda: not worker.is_alive(), timeout=10_000)
    qtbot.waitUntil(lambda: w.log.lines()[-1:] == [f"postęp {total - 1}"], timeout=10_000)
    elapsed = time.perf_counter() - started
    qtbot.wait(60)
    timer.stop()
    assert len(w.log) == MAX_LOG_LINES
    assert w.run_page.log_view.last_line() == f"postęp {total - 1}"
    assert max(gaps) < budget_ms, f"GUI stalled for {max(gaps):.0f} ms (budget {budget_ms:.0f})"
    assert elapsed < 5 * slack, f"flood took {elapsed:.2f} s"


# ---------------------------------------------------------------- Dziennik


def fill(window, *lines: str) -> None:
    for line in lines:
        window.log.push_line(line)
    window.log.flush()


def test_log_page_copy(make_window, qtbot) -> None:
    w = make_window().window
    page = w.log_page
    assert not page.copy_button.isEnabled()  # empty log
    fill(w, "zażółć", "gęślą jaźń")
    assert page.copy_button.isEnabled()
    qtbot.mouseClick(page.copy_button, Qt.MouseButton.LeftButton)
    assert QGuiApplication.clipboard().text() == "zażółć\ngęślą jaźń"
    assert page.status.text() == "Skopiowano do schowka"


def test_log_page_save_via_button(make_window, qtbot, tmp_path: Path) -> None:
    w = make_window().window
    page = w.log_page
    target = tmp_path / "out" / "dziennik.txt"
    target.parent.mkdir()
    page._save_target = lambda _parent: target
    fill(w, "linia 1", "Świnoujście")
    with qtbot.waitSignal(page.saved) as blocker:
        qtbot.mouseClick(page.save_button, Qt.MouseButton.LeftButton)
    assert blocker.args == [target]
    assert target.read_text(encoding="utf-8") == "linia 1\nŚwinoujście\n"
    assert page.status.text() == f"Zapisano → {target}"


def test_log_page_save_cancelled_and_failing(make_window, qtbot, tmp_path: Path) -> None:
    w = make_window().window
    page = w.log_page
    fill(w, "x")
    page._save_target = lambda _parent: None  # dialog cancelled
    qtbot.mouseClick(page.save_button, Qt.MouseButton.LeftButton)
    assert page.status.text() == ""
    assert not page.save_to(tmp_path)  # a directory: cannot be written
    assert page.status.text().startswith("Nie udało się zapisać:")


def test_log_page_clear(make_window, qtbot) -> None:
    w = make_window().window
    page = w.log_page
    fill(w, "a", "b")
    assert page.count_label.text() == "2 linie · limit 5000"
    qtbot.mouseClick(page.clear_button, Qt.MouseButton.LeftButton)
    assert len(w.log) == 0 and page.view.toPlainText() == ""
    assert w.run_page.log_view.toPlainText() == ""
    assert page.count_label.text() == "0 linii · limit 5000"
    assert not page.clear_button.isEnabled() and not page.copy_button.isEnabled()


# ------------------------------------------------------------------- tools


class ToolExecutor:
    def __init__(self, artifact: Path | None = None, ok: bool = True) -> None:
        self.artifact = artifact
        self.ok = ok
        self.calls: list[str] = []

    def __call__(self, item: Item, ctx: ActionContext) -> ActionOutcome:
        self.calls.append(item.id)
        ctx.log(f"[test] {item.name}")
        if not self.ok:
            return ActionOutcome(False, "Symulowany błąd narzędzia.")
        return ActionOutcome(True, "Zapisano raport baterii.", artifact=self.artifact)


def run_tool(qtbot, window, item_id: str) -> None:
    with qtbot.waitSignal(window.tools.toolFinished, timeout=5000):
        qtbot.mouseClick(window.tools_page.cards[item_id].run_button, Qt.MouseButton.LeftButton)


def test_tool_with_artifact_and_open(make_window, qtbot, tmp_path: Path) -> None:
    report = tmp_path / "battery-report.html"
    tools = ToolExecutor(artifact=report)
    h = make_window(tool_executor=tools)
    w = h.window
    w.navigate("tools")
    card = w.tools_page.cards["battery-report"]
    assert not card.result_frame.isVisible()
    run_tool(qtbot, w, "battery-report")
    assert tools.calls == ["battery-report"]
    assert h.recorder.confirms == []  # no confirm text for this tool
    assert card.result_tag.text() == "[OK]"
    assert card.result_text.text() == f"Zapisano raport baterii.\n{report}"
    assert card.open_button.isVisible()
    assert card.run_button.isEnabled() and card.run_button.text() == "Generuj raport  →"
    qtbot.mouseClick(card.open_button, Qt.MouseButton.LeftButton)
    assert h.recorder.opened_paths == [report]
    qtbot.waitUntil(lambda: any("[test] Raport baterii" in x for x in w.log.lines()))


def test_tool_needs_confirmation(make_window, qtbot) -> None:
    tools = ToolExecutor()
    h = make_window(tool_executor=tools)
    w = h.window
    card = w.tools_page.cards["reboot-bios"]
    h.recorder.answers = [False]
    qtbot.mouseClick(card.run_button, Qt.MouseButton.LeftButton)
    assert len(h.recorder.confirms) == 1
    request = h.recorder.confirms[0]
    assert request.title == w.services.catalog.item("reboot-bios").name
    assert "BIOS" in request.message and request.accept_text == card.action_text
    assert tools.calls == [] and not w.tools.is_running()
    h.recorder.answers = [True]
    run_tool(qtbot, w, "reboot-bios")
    assert tools.calls == ["reboot-bios"]
    assert card.result_tag.text() == "[OK]" and not card.open_button.isVisible()


def test_tool_failure_is_shown(make_window, qtbot) -> None:
    h = make_window(tool_executor=ToolExecutor(ok=False))
    w = h.window
    run_tool(qtbot, w, "battery-report")
    card = w.tools_page.cards["battery-report"]
    assert card.result_tag.text() == "[BŁĄD]"
    assert card.result_text.text() == "Symulowany błąd narzędzia."


def test_tool_button_busy_while_running(make_window, qtbot) -> None:
    gate = threading.Event()

    def slow(item: Item, ctx: ActionContext) -> ActionOutcome:
        gate.wait(5)
        return ActionOutcome(True, "ok")

    w = make_window(tool_executor=slow).window
    card = w.tools_page.cards["battery-report"]
    with qtbot.waitSignal(w.tools.toolFinished, timeout=5000) as blocker:
        assert w.run_tool("battery-report")
        assert not card.run_button.isEnabled() and card.run_button.text() == "Wykonywanie…"
        assert not w.run_tool("battery-report")  # already running
        gate.set()
    assert blocker.args[1].status is TaskStatus.OK
    assert card.run_button.isEnabled()
