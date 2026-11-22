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

import dataclasses
import enum
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from winstaller.core.runner import (
    NullListener,
    RunSummary,
    Task,
    TaskResult,
    TaskRunner,
    TaskStatus,
)
from winstaller.core.updater import UpdateCancelled, UpdateCheck, UpdateError
from winstaller.core.updates import LOST_MESSAGE, InstallProgress
from winstaller.ui.services import Services

FLUSH_MS = 40
#: Queue item that starts the background Windows Update installation.
WINDOWS_UPDATE_ID = "windows-update"
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
        # Task objects are frozen snapshots (core contract): safe to hand over as is.
        self._signals.runStarted.emit(tuple(tasks))

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
    def __init__(
        self,
        runner: TaskRunner,
        ids: list[str],
        signals: _ListenerSignals,
        cancel: threading.Event,
    ) -> None:
        super().__init__()
        self._runner = runner
        self._ids = ids
        self._signals = signals
        self._cancel = cancel

    def run(self) -> None:
        try:
            self._runner.run(self._ids, cancel_event=self._cancel)
        except Exception as exc:  # e.g. CatalogError before run_started
            self._signals.runFailed.emit(f"{type(exc).__name__}: {exc}")


class RunController(QObject):
    """Owns the install-queue worker thread and exposes its lifecycle as signals.

    ``runStarted(tuple[Task, ...])``, ``taskStarted(item_id)``,
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
        self._cancel = threading.Event()
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
        return self._state in (RunState.STARTING, RunState.RUNNING)

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
        self._cancel = threading.Event()
        self._thread = _RunThread(self._runner, list(item_ids), self._signals, self._cancel)
        self._thread.finished.connect(self._on_thread_finished, QUEUED)
        self._set_state(RunState.STARTING)
        self._thread.start()
        return True

    def cancel(self) -> bool:
        """Request cancellation. Works from the moment ``start`` returned: the run's
        cancel event is created here and handed to ``TaskRunner.run`` (never lost)."""
        if not self.can_cancel:
            return False
        self._cancel.set()
        self._set_state(RunState.STOPPING)
        self._log.info("Zatrzymywanie kolejki…")
        return True

    def wait(self, msecs: int = 5000) -> bool:
        return self._thread.wait(msecs) if self._thread is not None else True

    @Slot(object)
    def _on_run_started(self, tasks: tuple[Task, ...]) -> None:
        if self._state == RunState.STARTING:  # STOPPING (early Stop) stays
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


# ===================================================== background Windows Update


class BackgroundUpdateMonitor(QObject):
    """Follows the detached Windows Update installation through its status file.

    Polls (``Services.background_poll_ms``) only while there is something to follow:
    after a ``windows-update`` queue item finished OK with a log (:meth:`track_result`)
    or when :meth:`detect` found a running installation at start-up; stops at a final
    state. Each read runs on a worker thread (``Services.read_install_progress`` may
    retry for a fraction of a second while the script renames over the file).
    ``changed(InstallProgress)`` is emitted only when the snapshot differs.
    """

    changed = Signal(object)
    #: Consecutive failed reads after which polling stops (the file is gone for good).
    MAX_FAILURES = 60

    def __init__(self, services: Services, log: LogStore, parent: QObject | None = None):
        super().__init__(parent)
        self._services = services
        self._log = log
        self._timer = QTimer(self)
        self._timer.setInterval(max(50, services.background_poll_ms))
        self._timer.timeout.connect(self._poll)
        self._path: Path | None = None
        self._job: Job | None = None
        self._failures = 0
        # Plain Python object captured by worker callbacks: a result arriving after the
        # window closed must not touch this QObject.
        self._closed = threading.Event()
        self.progress: InstallProgress | None = None

    @property
    def enabled(self) -> bool:
        return self._services.read_install_progress is not None

    @property
    def polling(self) -> bool:
        return self._timer.isActive()

    @property
    def status_path(self) -> Path | None:
        return self._path

    @property
    def running(self) -> bool:
        """The installation is (as far as we know) still working."""
        return self.progress is not None and not self.progress.done

    def track(self, status_path: Path) -> bool:
        """Follow ``status_path`` from now on (replaces whatever was followed)."""
        if not self.enabled or self._closed.is_set():
            return False
        self._path = status_path
        self._failures = 0
        self._timer.start()
        self._poll()
        return True

    def track_result(self, result: TaskResult) -> bool:
        """After a queue item: a ``windows-update`` that ended OK with a log artifact
        started (or found) a background installation; its status file is next to it.
        CANCELLED with a log too: the wait for the start confirmation was interrupted,
        but the installation may have started - the status file tells."""
        artifact = result.artifact
        if (
            result.item_id != WINDOWS_UPDATE_ID
            or result.status not in (TaskStatus.OK, TaskStatus.CANCELLED)
            or artifact is None
            or artifact.suffix.lower() != ".log"
        ):
            return False
        return self.track(artifact.with_suffix(".json"))

    def detect(self, is_hidden: Callable[[InstallProgress], bool] = lambda _p: False) -> bool:
        """Start-up check (worker thread): a running installation is followed; one that
        finished after the last system start and is not ``is_hidden`` is shown."""
        fn = self._services.find_background_install
        if fn is None or self._closed.is_set():
            return False
        closed = self._closed
        run_job(
            lambda _report: fn(),
            on_done=lambda p: None if closed.is_set() else self._detected(p, is_hidden),
            on_error=lambda exc: None if closed.is_set() else self._detect_failed(exc),
            name="winstaller-wu-detect",
        )
        return True

    def stop(self) -> None:
        self._closed.set()
        self._timer.stop()

    # -- internals (GUI thread)
    def _detected(self, progress: InstallProgress | None, is_hidden) -> None:  # type: ignore[no-untyped-def]
        if progress is None or self._path is not None:
            return  # nothing found, or this session already follows an installation
        if progress.done:
            if not self._recent(progress) or is_hidden(progress):
                return
            self._set(progress)
            return
        self._set(progress)
        if progress.status_path is not None:
            self.track(progress.status_path)

    def _detect_failed(self, exc: BaseException) -> None:
        self._log.info(f"Nie udało się sprawdzić instalacji aktualizacji w tle: {exc}")

    def _recent(self, progress: InstallProgress) -> bool:
        boot = self._services.boot_time
        when = progress.reference_time
        if boot is None or when is None:
            return True
        try:
            return when >= boot()
        except (OSError, ValueError, TypeError, OverflowError):
            return True

    def _poll(self) -> None:
        fn = self._services.read_install_progress
        path = self._path
        if fn is None or path is None or (self._job is not None and self._job.running):
            return
        closed = self._closed
        self._job = run_job(
            lambda _report: fn(path),
            on_done=lambda p: None if closed.is_set() else self._read_done(path, p),
            on_error=lambda exc: None if closed.is_set() else self._read_failed(path, exc),
            name="winstaller-wu-poll",
        )

    def _read_done(self, path: Path, progress: InstallProgress) -> None:
        if path != self._path:
            return  # a result for a file we no longer follow
        self._failures = 0
        self._set(progress)
        if progress.done:
            self._timer.stop()

    def _read_failed(self, path: Path, exc: BaseException) -> None:
        if path != self._path:
            return
        self._failures += 1
        if self._failures == 1:
            self._log.info(f"Nie udało się odczytać stanu instalacji aktualizacji w tle: {exc}")
        if self._failures >= self.MAX_FAILURES:
            self._timer.stop()
            last = self.progress
            if last is not None and not last.done:
                # The file is gone for good: a final "unknown", never a frozen "in progress".
                self._set(
                    dataclasses.replace(
                        last, state="unknown", error=LOST_MESSAGE, alive=False, current_title=""
                    )
                )

    def _set(self, progress: InstallProgress) -> None:
        if progress != self.progress:
            self.progress = progress
            self.changed.emit(progress)


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
