"""Fixtures for the Qt UI tests (pytest-qt, offscreen, marker ``ui``).

Everything the window needs from outside comes from a :class:`Services` built here:
dry-run runners with a recording executor (``delay=0``), recorded confirmations,
``open_path``/``open_url`` recorders, winget "available", no automatic update check.
On top of the root system guard (no processes, registry, network, ...) the UI guard
blocks the Qt ways out of the sandbox: ``QDesktopServices.openUrl`` (shell
execute), modal dialogs (``QDialog.exec``, file dialogs, message boxes - a test
would hang), and redirects default ``QSettings`` into ``tmp_path``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QFileDialog, QMessageBox

from winstaller.core.actions import ActionContext, ActionOutcome
from winstaller.core.catalog import Catalog, Item, load_catalog
from winstaller.core.runner import (
    DryRunConfig,
    Executor,
    RunnerListener,
    TaskRunner,
    make_dry_run_executor,
)
from winstaller.ui.main_window import MainWindow
from winstaller.ui.services import ConfirmRequest, Services
from winstaller.ui.theme import ThemeManager, ThemeMode

WINDOW_SIZE = (1100, 720)
#: Local files that make every local item of the catalog available.
LOCAL_FILES = (
    "procyon/procyon-setup.exe",
    "DaVinci_Resolve_Studio_19.1_Windows.exe",
)
LOCAL_DIRS = ("BenchmarkTools", "BenchmarkToolsARM")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    for item in items:
        if here in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.ui)


# ----------------------------------------------------------------- UI guard


@pytest.fixture(autouse=True)
def ui_guard(system_guard, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def blocked(what: str) -> Callable[..., Any]:
        def _raise(*args: object, **kwargs: object) -> Any:
            raise system_guard.block(f"{what}{args!r}")

        return _raise

    monkeypatch.setattr(QDesktopServices, "openUrl", blocked("QDesktopServices.openUrl"))
    monkeypatch.setattr(QDialog, "exec", blocked("QDialog.exec (modal dialog in a test)"))
    for name in ("getSaveFileName", "getOpenFileName", "getExistingDirectory"):
        monkeypatch.setattr(QFileDialog, name, blocked(f"QFileDialog.{name}"))
    for name in ("critical", "warning", "information", "question"):
        monkeypatch.setattr(QMessageBox, name, blocked(f"QMessageBox.{name}"))
    # Default-constructed QSettings must never reach the user's registry.
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
        QSettings.setPath(QSettings.Format.IniFormat, scope, str(tmp_path / "qsettings"))


# ------------------------------------------------------------ test doubles


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    return load_catalog()


@pytest.fixture
def app_dir(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    for rel in LOCAL_FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(b"MZ")
    for rel in LOCAL_DIRS:
        (root / rel).mkdir(parents=True)
    return root


class RecordingExecutor:
    """Dry-run simulation (``delay=0``) that records which items ran.

    ``gate_ids``: these items park mid-task until :meth:`release` (or cancellation);
    ``entered`` is set when one of them is reached.
    """

    def __init__(self, fail_ids: frozenset[str] = frozenset(), gate_ids: tuple[str, ...] = ()):
        self._inner = make_dry_run_executor(DryRunConfig(delay=0.0, fail_ids=fail_ids))
        self.calls: list[str] = []
        self.gate_ids = gate_ids
        self.entered = threading.Event()
        self._gate = threading.Event()
        self._lock = threading.Lock()

    def release(self) -> None:
        self._gate.set()

    def __call__(self, item: Item, ctx: ActionContext) -> ActionOutcome:
        with self._lock:
            self.calls.append(item.id)
        if item.id in self.gate_ids:
            ctx.log(f"[dry-run] {item.name} (czeka)")
            self.entered.set()
            while not self._gate.wait(0.01):
                if ctx.cancel_event.is_set():
                    return ActionOutcome(False, "Przerwano przez użytkownika.", cancelled=True)
        return self._inner(item, ctx)


def runner_factory(
    catalog: Catalog, executor: Executor, *, arch: str, app_dir: Path, preserve_order: bool
) -> Callable[[RunnerListener], TaskRunner]:
    def factory(listener: RunnerListener) -> TaskRunner:
        return TaskRunner(
            catalog,
            dry_run=True,
            listener=listener,
            executor=executor,
            arch=arch,  # type: ignore[arg-type]
            app_dir=app_dir,
            preserve_order=preserve_order,
        )

    return factory


@dataclass
class Recorder:
    """What the window asked the outside world for."""

    confirms: list[ConfirmRequest] = field(default_factory=list)
    answers: list[bool] = field(default_factory=list)  # consumed first; then ``default``
    default_answer: bool = True
    opened_paths: list[Path] = field(default_factory=list)
    opened_urls: list[str] = field(default_factory=list)

    def confirm(self, _parent: object, request: ConfirmRequest) -> bool:
        self.confirms.append(request)
        return self.answers.pop(0) if self.answers else self.default_answer


@dataclass
class Harness:
    window: MainWindow
    services: Services
    executor: RecordingExecutor
    tool_executor: Executor
    recorder: Recorder
    settings: QSettings


@pytest.fixture
def settings(tmp_path: Path) -> QSettings:
    return QSettings(str(tmp_path / "winstaller.ini"), QSettings.Format.IniFormat)


@pytest.fixture
def make_window(
    qtbot, catalog: Catalog, app_dir: Path, settings: QSettings
) -> Iterator[Callable[..., Harness]]:
    created: list[Harness] = []

    def factory(
        *,
        arch: str = "x64",
        fail_ids: frozenset[str] | set[str] = frozenset(),
        gate_ids: tuple[str, ...] = (),
        tool_executor: Executor | None = None,
        mode: ThemeMode = ThemeMode.DARK,
        show: bool = True,
        **overrides: Any,
    ) -> Harness:
        executor = RecordingExecutor(frozenset(fail_ids), gate_ids)
        tools = tool_executor or RecordingExecutor(frozenset(fail_ids))
        recorder = Recorder()
        services = Services(
            catalog=catalog,
            runner_factory=runner_factory(
                catalog, executor, arch=arch, app_dir=app_dir, preserve_order=False
            ),
            tool_runner_factory=runner_factory(
                catalog, tools, arch=arch, app_dir=app_dir, preserve_order=True
            ),
            dry_run=True,
            arch=arch,  # type: ignore[arg-type]
            app_dir=app_dir,
            version="2.0.0",
            winget_available=lambda: True,
            cleanup_old=lambda: True,
            confirm=recorder.confirm,
            open_path=recorder.opened_paths.append,
            open_url=recorder.opened_urls.append,
            auto_check_updates=False,
            check_winget_on_start=False,
        )
        for key, value in overrides.items():
            setattr(services, key, value)
        window = MainWindow(services, theme=ThemeManager(mode), settings=settings)
        window.resize(*WINDOW_SIZE)
        if show:
            window.show()
            qtbot.waitExposed(window)
        harness = Harness(window, services, executor, tools, recorder, settings)
        created.append(harness)
        return harness

    yield factory

    for h in created:
        h.executor.release()
        h.window.run.cancel()
        h.window.updates.cancel_download()
        assert h.window.run.wait(5000), "run thread did not finish"
        qtbot.waitUntil(lambda h=h: not h.window.run.busy and not h.window.tools.is_running())
        h.window._force_close = True
        h.window.close()
        h.window.deleteLater()
    qtbot.wait(1)


def wait_run_finished(qtbot, window: MainWindow, timeout: int = 5000) -> None:
    qtbot.waitUntil(
        lambda: window.run.last_summary is not None and not window.run.busy, timeout=timeout
    )


def wait_log(qtbot, window: MainWindow, text: str, timeout: int = 3000) -> None:
    qtbot.waitUntil(lambda: any(text in line for line in window.log.lines()), timeout=timeout)
