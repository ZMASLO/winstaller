"""Sequential task runner with cancellation and listener events.

``TaskRunner.run`` is synchronous and executes on the caller's thread; all
listener methods are called on that same thread. The UI runs it on a worker
thread and marshals events to the GUI thread itself. ``cancel`` is thread-safe;
``run(..., cancel_event=...)`` lets the caller cancel without a start-up race.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from winstaller.core import actions
from winstaller.core import platform as plat
from winstaller.core.actions import ActionContext, ActionError, ActionOutcome
from winstaller.core.catalog import Catalog, Item, ItemKind
from winstaller.core.platform import Arch

_log = logging.getLogger(__name__)


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"

    @property
    def finished(self) -> bool:
        return self not in (TaskStatus.PENDING, TaskStatus.RUNNING)


@dataclass(frozen=True, slots=True)
class Task:
    """Immutable snapshot of one queued item, as passed to the listener.

    ``run_started`` receives snapshots with status PENDING and ``task_started`` a
    RUNNING one; the final status is in :class:`TaskResult`. Listeners may keep
    the objects (e.g. hand them to another thread): the runner never mutates them.
    """

    item: Item
    index: int
    status: TaskStatus = TaskStatus.PENDING

    @property
    def item_id(self) -> str:
        return self.item.id


@dataclass(frozen=True, slots=True)
class TaskResult:
    item_id: str
    status: TaskStatus
    message: str
    duration: float
    artifact: Path | None = None


@dataclass(frozen=True, slots=True)
class RunSummary:
    results: tuple[TaskResult, ...]
    duration: float
    cancelled: bool

    def count(self, status: TaskStatus) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def ok_count(self) -> int:
        return self.count(TaskStatus.OK)

    @property
    def failed_count(self) -> int:
        return self.count(TaskStatus.FAILED)

    @property
    def cancelled_count(self) -> int:
        return self.count(TaskStatus.CANCELLED)

    @property
    def skipped_count(self) -> int:
        return self.count(TaskStatus.SKIPPED)

    @property
    def failed_ids(self) -> list[str]:
        """Ids worth retrying ("Ponów nieudane"): failed and cancelled tasks."""
        retry = (TaskStatus.FAILED, TaskStatus.CANCELLED)
        return [r.item_id for r in self.results if r.status in retry]


class RunnerListener(Protocol):
    def run_started(self, tasks: Sequence[Task]) -> None: ...
    def task_started(self, task: Task) -> None: ...
    def output_line(self, text: str) -> None: ...
    def output_replace_last(self, text: str) -> None: ...
    def task_finished(self, result: TaskResult) -> None: ...
    def run_finished(self, summary: RunSummary) -> None: ...


class NullListener:
    """Listener that ignores everything; subclass and override what you need."""

    def run_started(self, tasks: Sequence[Task]) -> None:
        pass

    def task_started(self, task: Task) -> None:
        pass

    def output_line(self, text: str) -> None:
        pass

    def output_replace_last(self, text: str) -> None:
        pass

    def task_finished(self, result: TaskResult) -> None:
        pass

    def run_finished(self, summary: RunSummary) -> None:
        pass


#: Returns an :class:`ActionOutcome`; like an ``ActionFunc`` it may also return a
#: success message (``str``) or ``None``, which the runner normalizes.
Executor = Callable[[Item, ActionContext], "ActionOutcome | str | None"]


# ----------------------------------------------------------------- dry run

ENV_DRY_RUN_FAIL = "WINSTALLER_DRY_RUN_FAIL"  # comma separated item ids, or "*"
ENV_DRY_RUN_DELAY = "WINSTALLER_DRY_RUN_DELAY"  # seconds per simulated step
_DEFAULT_DRY_RUN_DELAY = 0.15


@dataclass(frozen=True, slots=True)
class DryRunConfig:
    """Simulation parameters. ``delay=0`` makes the simulation instant (tests)."""

    delay: float = _DEFAULT_DRY_RUN_DELAY
    progress_steps: int = 8
    fail_ids: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> DryRunConfig:
        """Read ``WINSTALLER_DRY_RUN_DELAY`` and ``WINSTALLER_DRY_RUN_FAIL``."""
        env = os.environ if environ is None else environ
        delay = _DEFAULT_DRY_RUN_DELAY
        if raw := env.get(ENV_DRY_RUN_DELAY):
            with contextlib.suppress(ValueError):
                delay = max(0.0, float(raw))
        fail = frozenset(x.strip() for x in env.get(ENV_DRY_RUN_FAIL, "").split(",") if x.strip())
        return cls(delay=delay, fail_ids=fail)

    def should_fail(self, item_id: str) -> bool:
        return "*" in self.fail_ids or item_id in self.fail_ids


def _bar(done: int, total: int, width: int = 24) -> str:
    filled = round(width * done / total) if total else width
    return "█" * filled + "▒" * (width - filled)


def make_dry_run_executor(config: DryRunConfig) -> Executor:
    """Executor that simulates realistic output without touching the system."""

    def simulate(item: Item, ctx: ActionContext) -> ActionOutcome:
        def pause() -> bool:
            return config.delay > 0 and ctx.cancel_event.wait(config.delay)

        cancelled = ActionOutcome(False, "Przerwano przez użytkownika.", cancelled=True)
        ctx.log(f"[dry-run] {item.name}")
        match item.kind:
            case ItemKind.WINGET | ItemKind.WINGET_UNINSTALL:
                ctx.log(f"Znaleziono {item.name} [{item.winget_id}] Wersja 1.0.0")
            case ItemKind.LOCAL_EXE:
                ctx.log(f"Uruchamianie instalatora {item.path}...")
            case ItemKind.ACTION:
                ctx.log(f"Wykonywanie akcji {item.action}...")
        if pause():
            return cancelled
        if item.kind == ItemKind.WINGET:
            ctx.log("Pobieranie https://example.invalid/installer.exe")
            total_mb = 12.0
            steps = max(1, config.progress_steps)
            for step in range(1, steps + 1):
                text = f"{_bar(step, steps)}  {total_mb * step / steps:.1f} MB / {total_mb:.1f} MB"
                (ctx.log if step == 1 else ctx.replace_last)(text)
                if pause():
                    return cancelled
            ctx.log("Pomyślnie zweryfikowano skrót instalatora")
            ctx.log("Rozpoczynanie instalacji pakietu...")
            if pause():
                return cancelled
        if config.should_fail(item.id):
            ctx.log("Instalator zakończył się błędem (symulacja).")
            return ActionOutcome(False, "Symulowany błąd (dry-run).")
        ctx.log("Pomyślnie zainstalowano" if item.kind == ItemKind.WINGET else "Gotowe")
        return ActionOutcome(True, "Wykonano (dry-run).")

    return simulate


# ------------------------------------------------------------------ runner


class TaskRunner:
    """Runs catalog items one after another.

    ``dry_run=True`` swaps the real executor for a simulation (nothing touches
    the system); its behaviour comes from ``dry_run_config`` or, if omitted,
    from :meth:`DryRunConfig.from_env`. ``executor`` overrides both (tests).
    """

    def __init__(
        self,
        catalog: Catalog,
        *,
        dry_run: bool = False,
        listener: RunnerListener | None = None,
        dry_run_config: DryRunConfig | None = None,
        executor: Executor | None = None,
        arch: Arch | None = None,
        app_dir: Path | None = None,
        preserve_order: bool = False,
    ) -> None:
        self.catalog = catalog
        self.dry_run = dry_run
        self.listener: RunnerListener = listener or NullListener()
        self.arch: Arch = arch or plat.get_arch()
        self.app_dir = app_dir or plat.app_dir()
        self.preserve_order = preserve_order
        if executor is not None:
            self._executor = executor
        elif dry_run:
            self._executor = make_dry_run_executor(dry_run_config or DryRunConfig.from_env())
        else:
            self._executor = actions.execute
        self._cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        #: Exceptions raised by listener methods during the last run ("method: Type: msg").
        self.listener_errors: list[str] = []

    @property
    def is_running(self) -> bool:
        return self._running

    def cancel(self) -> None:
        """Stop the current task (kills its process) and cancel the remaining ones.

        Thread-safe. Affects only a run in progress; calling it while no run is
        active does nothing. To cancel a run that may not have entered :meth:`run`
        yet (no start-up race), pass your own ``cancel_event`` to :meth:`run`.
        """
        with self._lock:
            self._cancel_event.set()

    def _ordered_items(self, item_ids: Iterable[str]) -> list[Item]:
        if self.preserve_order:
            ids = list(dict.fromkeys(item_ids))
            return [self.catalog.item(i) for i in ids]
        return [self.catalog.item(i) for i in self.catalog.in_catalog_order(item_ids)]

    def run(
        self, item_ids: Iterable[str], *, cancel_event: threading.Event | None = None
    ) -> RunSummary:
        """Run items (catalog order unless ``preserve_order``). Blocks until done.

        ``cancel_event``: set it (from any thread, at any time, even before this
        call) to cancel this run; an event that is already set cancels every task.
        Without it a fresh event is used and only :meth:`cancel` during the run
        cancels. Unknown ids raise :class:`CatalogError` before anything starts.
        Unavailable items become SKIPPED; an exception in a task makes it FAILED
        and the run continues. Exceptions raised by listener methods never abort
        the run or change a task's status: they are logged (``logging``) and
        collected in :attr:`listener_errors`.
        """
        items = self._ordered_items(item_ids)
        with self._lock:
            if self._running:
                raise RuntimeError("TaskRunner.run() jest już w toku.")
            self._running = True
            self._cancel_event = cancel_event if cancel_event is not None else threading.Event()
            self.listener_errors = []
            event = self._cancel_event
        try:
            return self._run(items, event)
        finally:
            with self._lock:
                self._running = False
                # A cancel() after the run must not leak into a later run.
                self._cancel_event = threading.Event()

    # -- listener calls: a broken listener must not break the run
    def _notify(self, method: str, *args: object) -> None:
        try:
            getattr(self.listener, method)(*args)
        except Exception as exc:
            self.listener_errors.append(f"{method}: {type(exc).__name__}: {exc}")
            _log.warning(
                "Listener %s.%s failed", type(self.listener).__name__, method, exc_info=True
            )

    def _log_line(self, text: str) -> None:
        self._notify("output_line", text)

    def _replace_last(self, text: str) -> None:
        self._notify("output_replace_last", text)

    def _run(self, items: list[Item], cancel_event: threading.Event) -> RunSummary:
        started = time.monotonic()
        self._notify("run_started", tuple(Task(item, n) for n, item in enumerate(items)))
        results: list[TaskResult] = []
        for n, item in enumerate(items):
            if cancel_event.is_set():
                result = self._finish(item, TaskStatus.CANCELLED, "Anulowano.", 0.0)
            else:
                result = self._run_task(Task(item, n), cancel_event)
            results.append(result)
        # "Cancelled" means a task was actually cancelled, not merely that cancel()
        # arrived after the last task had already finished.
        cancelled = any(r.status == TaskStatus.CANCELLED for r in results)
        summary = RunSummary(tuple(results), time.monotonic() - started, cancelled)
        self._notify("run_finished", summary)
        return summary

    def _finish(
        self,
        item: Item,
        status: TaskStatus,
        message: str,
        duration: float,
        artifact: Path | None = None,
    ) -> TaskResult:
        result = TaskResult(item.id, status, message, duration, artifact)
        self._notify("task_finished", result)
        return result

    def _run_task(self, task: Task, cancel_event: threading.Event) -> TaskResult:
        item = task.item
        availability = self.catalog.availability(item, arch=self.arch, base_dir=self.app_dir)
        if not availability.available:
            return self._finish(
                item, TaskStatus.SKIPPED, availability.reason or "Niedostępne.", 0.0
            )

        self._notify("task_started", Task(item, task.index, TaskStatus.RUNNING))
        self._log_line(f"--- {item.name} ---")
        ctx = ActionContext(
            item=item,
            log=self._log_line,
            replace_last=self._replace_last,
            cancel_event=cancel_event,
            app_dir=self.app_dir,
            arch=self.arch,
        )
        t0 = time.monotonic()
        try:
            outcome = _normalize(self._executor(item, ctx))
        except ActionError as exc:
            outcome = ActionOutcome(False, str(exc))
        except Exception as exc:  # one broken task must not stop the whole run
            outcome = ActionOutcome(False, f"Nieoczekiwany błąd: {type(exc).__name__}: {exc}")
        duration = time.monotonic() - t0

        if outcome.cancelled or (not outcome.ok and cancel_event.is_set()):
            status = TaskStatus.CANCELLED
        elif outcome.ok:
            status = TaskStatus.OK
        else:
            status = TaskStatus.FAILED
        self._log_line(outcome.message)
        return self._finish(item, status, outcome.message, duration, outcome.artifact)


def _normalize(value: object) -> ActionOutcome:
    """Executor result -> ActionOutcome (``str``/``None`` mean success, like ActionFunc)."""
    if value is None or isinstance(value, (ActionOutcome, str)):
        return actions.as_outcome(value)
    return ActionOutcome(
        False, f"Nieoczekiwany błąd: nieprawidłowy wynik zadania ({type(value).__name__})."
    )
