"""Thread boundary between the synchronous core and the Qt GUI thread.

Rules enforced here:

* blocking core calls run on worker threads (a ``QThread`` for the install queue,
  daemon threads for short background jobs);
* workers never touch widgets: they emit signals of QObjects that live in the GUI
  thread, connected with ``Qt.QueuedConnection``;
* process output is not signalled per line: it goes into :class:`LogStore`, a
  lock-protected buffer drained on the GUI thread at most every ``FLUSH_MS``.
"""

from __future__ import annotations

import enum
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from winstaller.core.catalog import Item
from winstaller.core.runner import (
    NullListener,
    RunSummary,
    Task,
    TaskResult,
    TaskRunner,
    TaskStatus,
)
from winstaller.core.updater import UpdateCancelled, UpdateCheck, UpdateError
from winstaller.ui.services import Services

FLUSH_MS = 40
MAX_LOG_LINES = 5000
QUEUED = Qt.ConnectionType.QueuedConnection


# ===================================================================== log


class LogStore(QObject):
    """Session log shared by the run view and the Dziennik page.

    ``push_line`` / ``push_replace`` are thread-safe and cheap; the GUI thread
    applies them in batches and emits ``batch(replace_last, lines)``: when
    ``replace_last`` is True the current last line must be removed before
    appending ``lines`` (``output_replace_last`` from winget progress bars).
    """

    batch = Signal(bool, list)
    cleared = Signal()
    _wake = Signal()

    def __init__(self, max_lines: int = MAX_LOG_LINES, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.max_lines = max_lines
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._ops: list[tuple[bool, str]] = []  # (is_replace, text)
        self._lock = threading.Lock()
        self._scheduled = False
        self._wake.connect(self._schedule, QUEUED)

    # -- any thread
    def push_line(self, text: str) -> None:
        self._push(False, text)

    def push_replace(self, text: str) -> None:
        self._push(True, text)

    def info(self, text: str) -> None:
        """Application message (not process output)."""
        self._push(False, f"[winstaller] {text}")

    def _push(self, replace: bool, text: str) -> None:
        with self._lock:
            self._ops.append((replace, text))
            if self._scheduled:
                return
            self._scheduled = True
        self._wake.emit()

    # -- GUI thread
    @Slot()
    def _schedule(self) -> None:
        QTimer.singleShot(FLUSH_MS, self.flush)

    @Slot()
    def flush(self) -> None:
        with self._lock:
            ops, self._ops = self._ops, []
            self._scheduled = False
        if not ops:
            return
        replace_existing = False
        pending: list[str] = []
        for is_replace, text in ops:
            if not is_replace:
                pending.append(text)
            elif pending:
                pending[-1] = text
            elif self._lines:
                replace_existing = True
                pending.append(text)
            else:
                pending.append(text)
        if replace_existing:
            self._lines.pop()
        self._lines.extend(pending)
        if len(pending) > self.max_lines:
            pending = pending[-self.max_lines :]
        self.batch.emit(replace_existing, pending)

    def lines(self) -> list[str]:
        return list(self._lines)

    def text(self) -> str:
        return "\n".join(self._lines)

    def __len__(self) -> int:
        return len(self._lines)

    def clear(self) -> None:
        self.flush()
        self._lines.clear()
        self.cleared.emit()


# ============================================================== run queue


class RunState(enum.StrEnum):
    IDLE = "idle"
    STARTING = "starting"  # thread started, waiting for run_started
    RUNNING = "running"
    STOPPING = "stopping"  # cancel requested
    FINISHED = "finished"


class _ListenerSignals(QObject):
    """Lives in the GUI thread; its signals are emitted from the runner thread."""

    runStarted = Signal(object)
    taskStarted = Signal(object)
    taskFinished = Signal(object)
    runFinished = Signal(object)
    runFailed = Signal(str)


class _QtListener:
    """``RunnerListener`` called on the runner thread; forwards to signals / LogStore."""

    def __init__(self, signals: _ListenerSignals, log: LogStore) -> None:
        self._signals = signals
        self._log = log

    def run_started(self, tasks: Sequence[Task]) -> None:
        # Task objects are mutated by the runner thread: hand over immutable snapshots.
        self._signals.runStarted.emit(tuple((t.item, t.index) for t in tasks))

    def task_started(self, task: Task) -> None:
        self._signals.taskStarted.emit(task.item_id)

    def output_line(self, text: str) -> None:
        self._log.push_line(text)

    def output_replace_last(self, text: str) -> None:
        self._log.push_replace(text)

    def task_finished(self, result: TaskResult) -> None:
        self._signals.taskFinished.emit(result)

    def run_finished(self, summary: RunSummary) -> None:
        self._signals.runFinished.emit(summary)


class _RunThread(QThread):
    def __init__(self, runner: TaskRunner, ids: list[str], signals: _ListenerSignals) -> None:
        super().__init__()
        self._runner = runner
        self._ids = ids
        self._signals = signals

    def run(self) -> None:
        try:
            self._runner.run(self._ids)
        except Exception as exc:  # e.g. CatalogError before run_started
            self._signals.runFailed.emit(f"{type(exc).__name__}: {exc}")


class RunController(QObject):
    """Owns the install-queue worker thread and exposes its lifecycle as signals.

    ``runStarted(tuple[(Item, index)])``, ``taskStarted(item_id)``,
    ``taskFinished(TaskResult)``, ``runFinished(RunSummary)``, ``runFailed(str)``,
    ``stateChanged(RunState)``.
    """

    runStarted = Signal(object)
    taskStarted = Signal(object)
    taskFinished = Signal(object)
    runFinished = Signal(object)
    runFailed = Signal(str)
    stateChanged = Signal(object)

    def __init__(self, services: Services, log: LogStore, parent: QObject | None = None):
        super().__init__(parent)
        self._services = services
        self._log = log
        self._state = RunState.IDLE
        self._runner: TaskRunner | None = None
        self._thread: _RunThread | None = None
        self._signals = _ListenerSignals(self)
        self._signals.runStarted.connect(self._on_run_started, QUEUED)
        self._signals.taskStarted.connect(self._on_task_started, QUEUED)
        self._signals.taskFinished.connect(self._on_task_finished, QUEUED)
        self._signals.runFinished.connect(self._on_run_finished, QUEUED)
        self._signals.runFailed.connect(self._on_run_failed, QUEUED)
        self.last_summary: RunSummary | None = None

    @property
    def state(self) -> RunState:
        return self._state

    @property
    def busy(self) -> bool:
        return self._state in (RunState.STARTING, RunState.RUNNING, RunState.STOPPING)

    @property
    def can_cancel(self) -> bool:
        return self._state == RunState.RUNNING

    def _set_state(self, state: RunState) -> None:
        if state != self._state:
            self._state = state
            self.stateChanged.emit(state)

    def start(self, item_ids: Sequence[str]) -> bool:
        if self.busy or not item_ids:
            return False
        if self._thread is not None:
            self._thread.wait()  # previous run already returned; just reap the thread
            self._thread = None
        self._runner = self._services.runner_factory(_QtListener(self._signals, self._log))
        self._thread = _RunThread(self._runner, list(item_ids), self._signals)
        self._thread.finished.connect(self._on_thread_finished, QUEUED)
        self._set_state(RunState.STARTING)
        self._thread.start()
        return True

    def cancel(self) -> bool:
        """Request cancellation; only effective after ``run_started`` (core contract)."""
        if self._state != RunState.RUNNING or self._runner is None:
            return False
        self._runner.cancel()
        self._set_state(RunState.STOPPING)
        self._log.info("Zatrzymywanie kolejki…")
        return True

    def wait(self, msecs: int = 5000) -> bool:
        return self._thread.wait(msecs) if self._thread is not None else True

    @Slot(object)
    def _on_run_started(self, tasks: tuple[tuple[Item, int], ...]) -> None:
        self._set_state(RunState.RUNNING)
        self.runStarted.emit(tasks)

    @Slot(object)
    def _on_task_started(self, item_id: str) -> None:
        self.taskStarted.emit(item_id)

    @Slot(object)
    def _on_task_finished(self, result: TaskResult) -> None:
        self.taskFinished.emit(result)

    @Slot(object)
    def _on_run_finished(self, summary: RunSummary) -> None:
        self.last_summary = summary
        self._set_state(RunState.FINISHED)
        self.runFinished.emit(summary)

    @Slot(str)
    def _on_run_failed(self, message: str) -> None:
        self._log.info(f"Nie udało się uruchomić kolejki: {message}")
        self._set_state(RunState.IDLE)
        self.runFailed.emit(message)

    @Slot()
    def _on_thread_finished(self) -> None:
        if self._thread is not None and not self._thread.isRunning():
            self._thread.deleteLater()
            self._thread = None


# ========================================================= background jobs


class _JobSignals(QObject):
    done = Signal(object)
    failed = Signal(object)
    progress = Signal(object)


class Job(QObject):
    """Runs ``fn(report)`` on a daemon thread; callbacks run on the GUI thread.

    Daemon threads (not QThreadPool) so that a hanging network call or a 10 s
    ``cleanup_old`` wait never blocks closing the application.
    """

    finished = Signal()

    _active: set[Job] = set()

    def __init__(
        self,
        fn: Callable[[Callable[[Any], None]], Any],
        *,
        on_done: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        on_progress: Callable[[Any], None] | None = None,
        name: str = "winstaller-job",
    ) -> None:
        super().__init__()
        self._fn = fn
        self._on_done = on_done
        self._on_error = on_error
        self._on_progress = on_progress
        self._name = name
        self._signals = _JobSignals(self)
        self._signals.done.connect(self._handle_done, QUEUED)
        self._signals.failed.connect(self._handle_failed, QUEUED)
        self._signals.progress.connect(self._handle_progress, QUEUED)
        self.running = False

    def start(self) -> Job:
        Job._active.add(self)
        self.running = True
        threading.Thread(target=self._work, name=self._name, daemon=True).start()
        return self

    def _work(self) -> None:
        try:
            result = self._fn(self._signals.progress.emit)
        except BaseException as exc:
            self._signals.failed.emit(exc)
        else:
            self._signals.done.emit(result)

    @Slot(object)
    def _handle_progress(self, value: Any) -> None:
        if self._on_progress is not None:
            self._on_progress(value)

    @Slot(object)
    def _handle_done(self, result: Any) -> None:
        self._finish()
        if self._on_done is not None:
            self._on_done(result)

    @Slot(object)
    def _handle_failed(self, exc: BaseException) -> None:
        self._finish()
        if self._on_error is not None:
            self._on_error(exc)

    def _finish(self) -> None:
        self.running = False
        Job._active.discard(self)
        self.finished.emit()


def run_job(
    fn: Callable[[Callable[[Any], None]], Any],
    *,
    on_done: Callable[[Any], None] | None = None,
    on_error: Callable[[BaseException], None] | None = None,
    on_progress: Callable[[Any], None] | None = None,
    name: str = "winstaller-job",
) -> Job:
    return Job(fn, on_done=on_done, on_error=on_error, on_progress=on_progress, name=name).start()


# ================================================================ updates


class UpdateController(QObject):
    """Background update check and download. Never touches widgets."""

    checking = Signal(bool)  # manual
    checkFinished = Signal(object, bool)  # UpdateCheck, manual
    checkFailed = Signal(str, bool)  # message, manual
    downloadStarted = Signal()
    downloadProgress = Signal(int, int)  # received, total (-1 = unknown)
    downloadFinished = Signal(object)  # Path
    downloadFailed = Signal(str)
    downloadCancelled = Signal()
    applied = Signal()  # the application must quit now

    _PROGRESS_INTERVAL = 0.05

    def __init__(self, services: Services, log: LogStore, parent: QObject | None = None):
        super().__init__(parent)
        self._services = services
        self._log = log
        self._check_job: Job | None = None
        self._download_job: Job | None = None
        self._cancel = threading.Event()
        self.last_check: UpdateCheck | None = None

    @property
    def enabled(self) -> bool:
        return self._services.check_update is not None

    @property
    def simulated(self) -> bool:
        return self._services.apply_update is None

    @property
    def downloading(self) -> bool:
        return self._download_job is not None and self._download_job.running

    def check(self, *, manual: bool = False) -> bool:
        fn = self._services.check_update
        if fn is None or (self._check_job is not None and self._check_job.running):
            return False
        self.checking.emit(manual)
        self._check_job = run_job(
            lambda _report: fn(),
            on_done=lambda result: self._check_done(result, manual),
            on_error=lambda exc: self._check_error(exc, manual),
            name="winstaller-update-check",
        )
        return True

    def _check_done(self, result: UpdateCheck, manual: bool) -> None:
        self.last_check = result
        if result.update_available:
            self._log.info(f"Dostępna nowa wersja {result.latest} (obecna {result.current}).")
        elif manual:
            self._log.info(f"Brak aktualizacji (najnowsza wersja: {result.latest}).")
        self.checkFinished.emit(result, manual)

    def _check_error(self, exc: BaseException, manual: bool) -> None:
        message = str(exc) if isinstance(exc, UpdateError) else f"{type(exc).__name__}: {exc}"
        self._log.info(f"Sprawdzanie aktualizacji nie powiodło się: {message}")
        self.checkFailed.emit(message, manual)

    def download(self) -> bool:
        check = self.last_check
        fn = self._services.download_update
        dest_fn = self._services.update_dest
        if check is None or fn is None or dest_fn is None or self.downloading:
            return False
        asset = check.asset
        if asset is None:
            self.downloadFailed.emit("Wydanie nie zawiera pliku dla tej architektury.")
            return False
        self._cancel = threading.Event()
        cancel = self._cancel
        last = [0.0]

        def work(report: Callable[[Any], None]) -> Path:
            def progress(received: int, total: int | None) -> None:
                now = time.monotonic()
                if now - last[0] >= self._PROGRESS_INTERVAL or (total and received >= total):
                    last[0] = now
                    report((received, -1 if total is None else total))

            dest = dest_fn(asset)
            return fn(asset, dest, progress, cancel)

        self.downloadStarted.emit()
        self._log.info(f"Pobieranie aktualizacji {asset.name}…")
        self._download_job = run_job(
            work,
            on_done=self._download_done,
            on_error=self._download_error,
            on_progress=lambda v: self.downloadProgress.emit(int(v[0]), int(v[1])),
            name="winstaller-update-download",
        )
        return True

    def cancel_download(self) -> None:
        self._cancel.set()

    def _download_done(self, path: Path) -> None:
        apply = self._services.apply_update
        if apply is None:
            self._log.info("Tryb dry-run: pobieranie zasymulowane, podmiana pliku pominięta.")
            self.downloadFinished.emit(path)
            return
        try:
            quit_now = apply(path)
        except UpdateError as exc:
            self._log.info(f"Nie udało się zainstalować aktualizacji: {exc}")
            self.downloadFailed.emit(str(exc))
            return
        self.downloadFinished.emit(path)
        if quit_now:
            self._log.info("Uruchamianie nowej wersji…")
            self.applied.emit()

    def _download_error(self, exc: BaseException) -> None:
        if isinstance(exc, UpdateCancelled):
            self._log.info("Pobieranie aktualizacji przerwane.")
            self.downloadCancelled.emit()
            return
        message = str(exc) if isinstance(exc, UpdateError) else f"{type(exc).__name__}: {exc}"
        self._log.info(f"Pobieranie aktualizacji nie powiodło się: {message}")
        self.downloadFailed.emit(message)

    def cleanup_old(self) -> None:
        fn = self._services.cleanup_old
        if fn is None:
            return
        run_job(
            lambda _report: fn(),
            on_done=lambda ok: (
                None
                if ok in (True, None)
                else self._log.info("Nie udało się usunąć poprzedniej wersji programu (.old).")
            ),
            on_error=lambda exc: self._log.info(f"Sprzątanie po aktualizacji: {exc}"),
            name="winstaller-cleanup-old",
        )


# ================================================================= winget


class WingetController(QObject):
    availabilityChecked = Signal(bool)
    installStarted = Signal()
    installFinished = Signal(bool, str)  # ok, message

    def __init__(self, services: Services, log: LogStore, parent: QObject | None = None):
        super().__init__(parent)
        self._services = services
        self._log = log
        self._job: Job | None = None
        self._cancel = threading.Event()
        self.available: bool | None = None

    @property
    def installing(self) -> bool:
        return self._job is not None and self._job.running

    def check(self) -> None:
        fn = self._services.winget_available
        if fn is None:
            return
        run_job(
            lambda _report: bool(fn()),
            on_done=self._checked,
            on_error=lambda exc: self._checked(False),
            name="winstaller-winget-check",
        )

    def _checked(self, available: bool) -> None:
        self.available = available
        if not available:
            self._log.info("Nie znaleziono winget — instalacja programów nie zadziała.")
        self.availabilityChecked.emit(available)

    def install(self) -> bool:
        fn = self._services.ensure_winget
        if fn is None or self.installing:
            return False
        self._cancel = threading.Event()
        cancel = self._cancel
        log = self._log
        self.installStarted.emit()
        log.info("Instalowanie winget…")
        self._job = run_job(
            lambda _report: fn(log.push_line, log.push_replace, cancel),
            on_done=self._installed,
            on_error=lambda exc: self._installed(None, f"{type(exc).__name__}: {exc}"),
            name="winstaller-winget-install",
        )
        return True

    def cancel(self) -> None:
        self._cancel.set()

    def _installed(self, result: Any, error: str | None = None) -> None:
        ok = bool(result is not None and result.ok)
        message = error or (result.message if result is not None else "Nieznany błąd.")
        self._log.info(message)
        if ok:
            self.available = True
        self.installFinished.emit(ok, message)


# ================================================================== tools


class _ToolListener(NullListener):
    def __init__(self, log: LogStore) -> None:
        self._log = log

    def output_line(self, text: str) -> None:
        self._log.push_line(text)

    def output_replace_last(self, text: str) -> None:
        self._log.push_replace(text)


class ToolController(QObject):
    """Runs a single ``tools`` item through its own TaskRunner on a worker thread."""

    toolStarted = Signal(str)  # item id
    toolFinished = Signal(str, object)  # item id, TaskResult

    def __init__(self, services: Services, log: LogStore, parent: QObject | None = None):
        super().__init__(parent)
        self._services = services
        self._log = log
        self._running: set[str] = set()

    def is_running(self, item_id: str | None = None) -> bool:
        return bool(self._running) if item_id is None else item_id in self._running

    def run(self, item_id: str) -> bool:
        if item_id in self._running:
            return False
        runner = self._services.tool_runner_factory(_ToolListener(self._log))
        self._running.add(item_id)
        self.toolStarted.emit(item_id)

        def work(_report: Callable[[Any], None]) -> TaskResult:
            return runner.run([item_id]).results[0]

        run_job(
            work,
            on_done=lambda result: self._done(item_id, result),
            on_error=lambda exc: self._failed(item_id, exc),
            name=f"winstaller-tool-{item_id}",
        )
        return True

    def _done(self, item_id: str, result: TaskResult) -> None:
        self._running.discard(item_id)
        self.toolFinished.emit(item_id, result)

    def _failed(self, item_id: str, exc: BaseException) -> None:
        self._running.discard(item_id)
        message = f"{type(exc).__name__}: {exc}"
        self._log.info(f"Narzędzie {item_id}: {message}")
        self.toolFinished.emit(item_id, TaskResult(item_id, TaskStatus.FAILED, message, 0.0))
