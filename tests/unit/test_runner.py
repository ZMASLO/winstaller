"""TaskRunner: ordering, events, statuses, cancellation, threading, dry run."""

from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path

import pytest

from tests.support import RecordingListener, make_catalog, make_item
from winstaller.core import actions, winget
from winstaller.core.actions import ActionContext, ActionError, ActionOutcome
from winstaller.core.catalog import CatalogError, Item, ItemKind, load_catalog
from winstaller.core.runner import (
    DryRunConfig,
    NullListener,
    RunSummary,
    Task,
    TaskResult,
    TaskRunner,
    TaskStatus,
    make_dry_run_executor,
)

OK = ActionOutcome(True, "Gotowe.")


def ok_executor(item: Item, ctx: ActionContext) -> ActionOutcome:
    return OK


@pytest.fixture
def catalog():
    return make_catalog(
        [
            make_item("a"),
            make_item("b"),
            make_item("c"),
            make_item("arm", arch=("arm64",)),
            make_item("gone", unavailable="Wycofany."),
            make_item("files", requires_files=("Missing",)),
        ]
    )


def make_runner(
    catalog, tmp_path: Path, executor=ok_executor, **kwargs
) -> tuple[TaskRunner, RecordingListener]:
    listener = RecordingListener()
    runner = TaskRunner(
        catalog, listener=listener, executor=executor, arch="x64", app_dir=tmp_path, **kwargs
    )
    return runner, listener


# ------------------------------------------------------------------ order


def test_catalog_order_and_dedup(catalog, tmp_path: Path) -> None:
    seen: list[str] = []
    runner, _ = make_runner(catalog, tmp_path, lambda i, c: seen.append(i.id) or OK)
    summary = runner.run(["c", "a", "c", "b", "a"])
    assert seen == ["a", "b", "c"]
    assert [r.item_id for r in summary.results] == ["a", "b", "c"]


def test_preserve_order_keeps_given_order_and_dedups(catalog, tmp_path: Path) -> None:
    seen: list[str] = []
    runner, _ = make_runner(
        catalog, tmp_path, lambda i, c: seen.append(i.id) or OK, preserve_order=True
    )
    runner.run(["c", "a", "c", "b"])
    assert seen == ["c", "a", "b"]


def test_accepts_any_iterable(catalog, tmp_path: Path) -> None:
    runner, _ = make_runner(catalog, tmp_path)
    assert runner.run(iter(["b", "a"])).ok_count == 2
    assert runner.run(x for x in ["c"]).ok_count == 1


@pytest.mark.parametrize("preserve_order", [False, True])
def test_unknown_id_fails_before_anything_starts(catalog, tmp_path: Path, preserve_order) -> None:
    calls: list[str] = []
    runner, listener = make_runner(
        catalog, tmp_path, lambda i, c: calls.append(i.id) or OK, preserve_order=preserve_order
    )
    with pytest.raises(CatalogError):
        runner.run(["a", "ghost"])
    assert calls == [] and listener.events == []
    assert not runner.is_running


def test_empty_run(catalog, tmp_path: Path) -> None:
    runner, listener = make_runner(catalog, tmp_path)
    summary = runner.run([])
    assert summary.results == () and not summary.cancelled
    assert [n for n, _ in listener.events] == ["run_started", "run_finished"]


# ----------------------------------------------------------------- events


def test_event_sequence_for_ok_failed_skipped(catalog, tmp_path: Path) -> None:
    def executor(item: Item, ctx: ActionContext) -> ActionOutcome:
        ctx.log(f"working on {item.id}")
        ctx.replace_last("50%")
        if item.id == "b":
            return ActionOutcome(False, "Nie wyszło.")
        return ActionOutcome(True, "Zrobione.")

    runner, listener = make_runner(catalog, tmp_path, executor)
    summary = runner.run(["a", "b", "arm", "gone", "files"])
    assert listener.events[0] == ("run_started", ["a", "b", "arm", "gone", "files"])
    assert listener.events[1:9] == [
        ("task_started", "a"),
        ("output_line", "--- A ---"),
        ("output_line", "working on a"),
        ("output_replace_last", "50%"),
        ("output_line", "Zrobione."),
        ("task_finished", ("a", "ok")),
        ("task_started", "b"),
        ("output_line", "--- B ---"),
    ]
    assert listener.names() == [
        ("run_started", ["a", "b", "arm", "gone", "files"]),
        ("task_started", "a"),
        ("task_finished", ("a", "ok")),
        ("task_started", "b"),
        ("task_finished", ("b", "failed")),
        # Unavailable items finish as SKIPPED without task_started.
        ("task_finished", ("arm", "skipped")),
        ("task_finished", ("gone", "skipped")),
        ("task_finished", ("files", "skipped")),
        ("run_finished", summary),
    ]
    reasons = {r.item_id: r.message for r in summary.results}
    assert reasons["gone"] == "Wycofany."
    assert "ARM64" in reasons["arm"]
    assert "Missing" in reasons["files"]


def test_summary_counters(catalog, tmp_path: Path) -> None:
    outcomes = {
        "a": ActionOutcome(True, "ok"),
        "b": ActionOutcome(False, "fail"),
        "c": ActionOutcome(False, "cancel", cancelled=True),
    }
    runner, _ = make_runner(catalog, tmp_path, lambda i, c: outcomes[i.id])
    summary = runner.run(["a", "b", "c", "gone"])
    assert (summary.ok_count, summary.failed_count, summary.cancelled_count) == (1, 1, 1)
    assert summary.skipped_count == 1
    assert summary.count(TaskStatus.PENDING) == 0
    assert summary.failed_ids == ["b", "c"]
    assert summary.duration >= 0
    assert all(isinstance(r, TaskResult) for r in summary.results)


def test_tasks_are_immutable_snapshots_and_artifact_propagates(catalog, tmp_path: Path) -> None:
    # Contract (stage 3): listeners get frozen Task snapshots; the runner never
    # mutates them later (the UI hands them to the GUI thread). Final statuses
    # live in TaskResult.
    tasks: list[Task] = []
    started: list[Task] = []
    artifact = tmp_path / "report.html"

    class Listener(NullListener):
        def run_started(self, t) -> None:
            tasks.extend(t)

        def task_started(self, task: Task) -> None:
            started.append(task)

    runner = TaskRunner(
        catalog,
        listener=Listener(),
        executor=lambda i, c: ActionOutcome(True, "r", artifact=artifact),
        arch="x64",
        app_dir=tmp_path,
    )
    summary = runner.run(["a", "gone"])
    assert [t.status for t in tasks] == [TaskStatus.PENDING, TaskStatus.PENDING]
    assert [t.index for t in tasks] == [0, 1]
    assert tasks[0].item_id == "a"
    assert [(t.item_id, t.index, t.status) for t in started] == [("a", 0, TaskStatus.RUNNING)]
    with pytest.raises(dataclasses.FrozenInstanceError):
        tasks[0].status = TaskStatus.OK  # type: ignore[misc]
    assert [r.status for r in summary.results] == [TaskStatus.OK, TaskStatus.SKIPPED]
    assert summary.results[0].artifact == artifact
    assert summary.results[0].duration >= 0


@pytest.mark.parametrize(
    ("value", "status", "message"),
    [
        ("Zrobione.", TaskStatus.OK, "Zrobione."),
        (None, TaskStatus.OK, "Gotowe."),
        ("", TaskStatus.OK, "Gotowe."),
        (42, TaskStatus.FAILED, "Nieoczekiwany błąd: nieprawidłowy wynik zadania (int)."),
    ],
)
def test_executor_result_is_normalized(catalog, tmp_path: Path, value, status, message) -> None:
    # Executors may return what an ActionFunc returns (str / None), not only ActionOutcome.
    runner, _ = make_runner(catalog, tmp_path, lambda i, c: value)
    summary = runner.run(["a", "b"])
    assert [r.status for r in summary.results] == [status, status]
    assert summary.results[0].message == message
    assert not runner.is_running


def test_task_status_finished_property() -> None:
    assert not TaskStatus.PENDING.finished and not TaskStatus.RUNNING.finished
    assert all(
        s.finished
        for s in (TaskStatus.OK, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SKIPPED)
    )


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (ActionError("Brak folderu."), "Brak folderu."),
        (ValueError("zła wartość"), "Nieoczekiwany błąd: ValueError: zła wartość"),
        (OSError(5, "Odmowa dostępu"), "Nieoczekiwany błąd: OSError: [Errno 5] Odmowa dostępu"),
    ],
)
def test_exception_in_executor_fails_task_and_run_continues(
    catalog, tmp_path: Path, exc, message
) -> None:
    def executor(item: Item, ctx: ActionContext) -> ActionOutcome:
        if item.id == "a":
            raise exc
        return OK

    runner, listener = make_runner(catalog, tmp_path, executor)
    summary = runner.run(["a", "b"])
    assert [(r.item_id, r.status) for r in summary.results] == [
        ("a", TaskStatus.FAILED),
        ("b", TaskStatus.OK),
    ]
    assert summary.results[0].message == message
    assert ("output_line", message) in listener.events
    assert not runner.is_running


def test_listener_methods_called_on_run_thread(catalog, tmp_path: Path) -> None:
    runner, listener = make_runner(catalog, tmp_path, lambda i, c: (c.log("x"), OK)[1])
    worker = threading.Thread(target=runner.run, args=(["a", "b", "gone"],))
    worker.start()
    worker.join(5)
    assert listener.threads == {worker.ident}


# ----------------------------------------------------------- cancellation


def test_cancel_during_task_cancels_current_and_rest(catalog, tmp_path: Path) -> None:
    runner: TaskRunner

    def executor(item: Item, ctx: ActionContext) -> ActionOutcome:
        if item.id == "b":
            runner.cancel()
            assert ctx.cancel_event.is_set()
            return ActionOutcome(False, "Przerwano przez użytkownika.", cancelled=True)
        return OK

    runner, listener = make_runner(catalog, tmp_path, executor)
    summary = runner.run(["a", "b", "c", "gone"])
    assert [(r.item_id, r.status) for r in summary.results] == [
        ("a", TaskStatus.OK),
        ("b", TaskStatus.CANCELLED),
        ("c", TaskStatus.CANCELLED),
        ("gone", TaskStatus.CANCELLED),
    ]
    assert summary.cancelled
    assert summary.failed_ids == ["b", "c", "gone"]
    # Cancelled-before-start tasks get no task_started.
    assert ("task_started", "c") not in listener.events
    assert summary.results[2].message == "Anulowano."


def test_failure_after_cancel_counts_as_cancelled(catalog, tmp_path: Path) -> None:
    runner: TaskRunner

    def executor(item: Item, ctx: ActionContext) -> ActionOutcome:
        runner.cancel()
        return ActionOutcome(False, "proces zabity")  # not flagged cancelled

    runner, _ = make_runner(catalog, tmp_path, executor)
    assert runner.run(["a"]).results[0].status is TaskStatus.CANCELLED


def test_success_despite_cancel_stays_ok(catalog, tmp_path: Path) -> None:
    runner: TaskRunner

    def executor(item: Item, ctx: ActionContext) -> ActionOutcome:
        runner.cancel()
        return OK

    runner, _ = make_runner(catalog, tmp_path, executor)
    summary = runner.run(["a", "b"])
    assert [r.status for r in summary.results] == [TaskStatus.OK, TaskStatus.CANCELLED]


def test_cancel_from_another_thread_interrupts_waiting_task(catalog, tmp_path: Path) -> None:
    started = threading.Event()

    def executor(item: Item, ctx: ActionContext) -> ActionOutcome:
        started.set()
        if ctx.cancel_event.wait(10):
            return ActionOutcome(False, "Przerwano.", cancelled=True)
        return OK

    runner, _ = make_runner(catalog, tmp_path, executor)
    result: list[RunSummary] = []
    worker = threading.Thread(target=lambda: result.append(runner.run(["a", "b"])))
    worker.start()
    assert started.wait(5)
    assert runner.is_running
    t0 = time.monotonic()
    runner.cancel()
    worker.join(5)
    assert not worker.is_alive()
    assert time.monotonic() - t0 < 2
    assert [r.status for r in result[0].results] == [TaskStatus.CANCELLED, TaskStatus.CANCELLED]
    assert not runner.is_running


def test_cancel_before_run_is_ignored(catalog, tmp_path: Path) -> None:
    runner, _ = make_runner(catalog, tmp_path)
    runner.cancel()
    summary = runner.run(["a"])
    assert summary.ok_count == 1 and not summary.cancelled


def test_preset_cancel_event_cancels_everything(catalog, tmp_path: Path) -> None:
    # The UI creates the event before starting the worker thread, so a Stop clicked
    # before run() was even entered is never lost.
    calls: list[str] = []
    runner, listener = make_runner(catalog, tmp_path, lambda i, c: calls.append(i.id) or OK)
    event = threading.Event()
    event.set()
    summary = runner.run(["a", "b"], cancel_event=event)
    assert calls == []
    assert [r.status for r in summary.results] == [TaskStatus.CANCELLED, TaskStatus.CANCELLED]
    assert summary.cancelled
    assert [n for n, _ in listener.names()] == [
        "run_started",
        "task_finished",
        "task_finished",
        "run_finished",
    ]


def test_cancel_event_set_from_other_thread_before_run_enters(catalog, tmp_path: Path) -> None:
    # Race of stage 2: cancel() between thread start and run() replacing the event.
    event = threading.Event()
    gate = threading.Event()
    runner, _ = make_runner(catalog, tmp_path)
    result: list[RunSummary] = []

    def worker() -> None:
        gate.wait(5)  # cancel happens before run() is entered
        result.append(runner.run(["a"], cancel_event=event))

    thread = threading.Thread(target=worker)
    thread.start()
    event.set()
    gate.set()
    thread.join(5)
    assert result[0].results[0].status is TaskStatus.CANCELLED


def test_cancel_method_uses_the_given_event(catalog, tmp_path: Path) -> None:
    runner: TaskRunner
    event = threading.Event()

    def executor(item: Item, ctx: ActionContext) -> ActionOutcome:
        assert ctx.cancel_event is event
        runner.cancel()
        return ActionOutcome(False, "x", cancelled=True)

    runner, _ = make_runner(catalog, tmp_path, executor)
    assert runner.run(["a"], cancel_event=event).cancelled
    assert event.is_set()


def test_cancel_after_last_task_is_not_a_cancelled_run(catalog, tmp_path: Path) -> None:
    runner: TaskRunner

    class LateCancel(RecordingListener):
        def task_finished(self, result: TaskResult) -> None:
            super().task_finished(result)
            if result.item_id == "b":
                runner.cancel()  # arrives after the last task already finished

    listener = LateCancel()
    runner = TaskRunner(
        catalog, listener=listener, executor=ok_executor, arch="x64", app_dir=tmp_path
    )
    summary = runner.run(["a", "b"])
    assert summary.ok_count == 2
    assert not summary.cancelled


def test_cancel_after_run_does_not_leak_into_next_run(catalog, tmp_path: Path) -> None:
    runner, _ = make_runner(catalog, tmp_path)
    runner.run(["a"])
    runner.cancel()
    assert not runner.run(["a"]).cancelled


def test_runner_reusable_after_cancelled_run(catalog, tmp_path: Path) -> None:
    runner: TaskRunner

    def executor(item: Item, ctx: ActionContext) -> ActionOutcome:
        if item.id == "a":
            runner.cancel()
        return OK

    runner, listener = make_runner(catalog, tmp_path, executor)
    assert runner.run(["a", "b"]).cancelled
    second = runner.run(["b", "c"])
    assert not second.cancelled and second.ok_count == 2
    assert [n for n, _ in listener.events].count("run_finished") == 2


# ---------------------------------------------------------------- reentry


def test_second_concurrent_run_raises(catalog, tmp_path: Path) -> None:
    release = threading.Event()
    entered = threading.Event()

    def executor(item: Item, ctx: ActionContext) -> ActionOutcome:
        entered.set()
        release.wait(10)
        return OK

    runner, _ = make_runner(catalog, tmp_path, executor)
    worker = threading.Thread(target=runner.run, args=(["a"],))
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(RuntimeError, match="już w toku"):
            runner.run(["b"])
        assert runner.is_running
    finally:
        release.set()
        worker.join(5)
    assert not runner.is_running


def test_reentrant_run_from_executor_raises(catalog, tmp_path: Path) -> None:
    runner: TaskRunner
    errors: list[BaseException] = []

    def executor(item: Item, ctx: ActionContext) -> ActionOutcome:
        try:
            runner.run(["b"])
        except RuntimeError as exc:
            errors.append(exc)
        return OK

    runner, _ = make_runner(catalog, tmp_path, executor)
    assert runner.run(["a"]).ok_count == 1
    assert len(errors) == 1


@pytest.mark.parametrize(
    "method",
    ["run_started", "task_started", "output_line", "task_finished", "run_finished"],
)
def test_listener_exception_does_not_abort_the_run(catalog, tmp_path: Path, method: str) -> None:
    # Contract (stage 3, previously "propagates and aborts the run"): a broken
    # listener never aborts the run nor changes task statuses; every other event is
    # still delivered (run_finished included) and the error is recorded.
    class Broken(RecordingListener):
        pass

    def boom(*args, **kwargs):
        raise LookupError("listener bug")

    listener = Broken()
    setattr(listener, method, boom)
    runner = TaskRunner(
        catalog, listener=listener, executor=ok_executor, arch="x64", app_dir=tmp_path
    )
    summary = runner.run(["a", "b"])
    assert summary.ok_count == 2
    assert not runner.is_running
    assert runner.listener_errors and all(
        e.startswith(f"{method}: LookupError: listener bug") for e in runner.listener_errors
    )
    names = [n for n, _ in listener.events]
    if method != "run_finished":
        assert names[-1] == "run_finished"
    runner.listener = NullListener()
    assert runner.run(["a"]).ok_count == 1
    assert runner.listener_errors == []  # reset per run


def test_listener_exception_inside_task_output_keeps_task_ok(catalog, tmp_path: Path) -> None:
    # Contract (stage 3, previously FAILED): a listener bug is not the task's failure.
    class Picky(RecordingListener):
        def output_line(self, text: str) -> None:
            if text == "explode":
                raise LookupError("listener bug")
            super().output_line(text)

    listener = Picky()

    def executor(item: Item, ctx: ActionContext) -> ActionOutcome:
        if item.id == "a":
            ctx.log("explode")
            ctx.log("after")
        return OK

    runner = TaskRunner(catalog, listener=listener, executor=executor, arch="x64", app_dir=tmp_path)
    summary = runner.run(["a", "b"])
    assert [r.status for r in summary.results] == [TaskStatus.OK, TaskStatus.OK]
    assert ("output_line", "after") in listener.events
    assert runner.listener_errors == ["output_line: LookupError: listener bug"]


def test_system_access_blocked_from_listener_is_not_swallowed(catalog, tmp_path: Path) -> None:
    # Only Exception is caught: BaseException (KeyboardInterrupt, the test guard) escapes.
    class Interrupting(NullListener):
        def task_started(self, task: Task) -> None:
            raise KeyboardInterrupt

    runner = TaskRunner(
        catalog, listener=Interrupting(), executor=ok_executor, arch="x64", app_dir=tmp_path
    )
    with pytest.raises(KeyboardInterrupt):
        runner.run(["a"])
    assert not runner.is_running


# ------------------------------------------------------------- construction


def test_default_executor_is_actions_execute(catalog) -> None:
    runner = TaskRunner(catalog, arch="x64")
    assert runner._executor is actions.execute
    assert isinstance(runner.listener, NullListener)


def test_arch_and_app_dir_defaults(catalog, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("WINSTALLER_ARCH", "arm64")
    monkeypatch.setenv("WINSTALLER_APP_DIR", str(tmp_path))
    runner = TaskRunner(catalog)
    assert runner.arch == "arm64"
    assert runner.app_dir == tmp_path


def test_context_passed_to_executor(catalog, tmp_path: Path) -> None:
    contexts: list[ActionContext] = []
    runner, _ = make_runner(catalog, tmp_path, lambda i, c: contexts.append(c) or OK)
    runner.run(["a"])
    ctx = contexts[0]
    assert ctx.item.id == "a" and ctx.arch == "x64" and ctx.app_dir == tmp_path
    assert ctx.args == ()


# ---------------------------------------------------------------- dry run


class TestDryRunConfig:
    def test_defaults(self) -> None:
        cfg = DryRunConfig.from_env({})
        assert cfg == DryRunConfig()
        assert cfg.delay == 0.15 and cfg.progress_steps == 8 and cfg.fail_ids == frozenset()

    @pytest.mark.parametrize(
        ("raw", "delay"), [("0", 0.0), ("0.5", 0.5), ("-3", 0.0), ("abc", 0.15), ("", 0.15)]
    )
    def test_delay_parsing(self, raw: str, delay: float) -> None:
        assert DryRunConfig.from_env({"WINSTALLER_DRY_RUN_DELAY": raw}).delay == delay

    def test_fail_ids_parsing(self) -> None:
        cfg = DryRunConfig.from_env({"WINSTALLER_DRY_RUN_FAIL": " a, b ,,c "})
        assert cfg.fail_ids == {"a", "b", "c"}
        assert cfg.should_fail("b") and not cfg.should_fail("d")

    def test_star_fails_everything(self) -> None:
        cfg = DryRunConfig.from_env({"WINSTALLER_DRY_RUN_FAIL": "*"})
        assert cfg.should_fail("anything")

    def test_reads_os_environ_by_default(self, monkeypatch) -> None:
        monkeypatch.setenv("WINSTALLER_DRY_RUN_DELAY", "0")
        monkeypatch.setenv("WINSTALLER_DRY_RUN_FAIL", "x")
        assert DryRunConfig.from_env() == DryRunConfig(delay=0.0, fail_ids=frozenset({"x"}))


@pytest.fixture
def no_system(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make every real execution path fail loudly; dry run must not reach them."""
    touched: list[str] = []

    def trap(name: str):
        def _trap(*args, **kwargs):
            touched.append(name)
            raise AssertionError(f"dry run called {name}")

        return _trap

    monkeypatch.setattr(actions, "execute", trap("actions.execute"))
    for fn in ("install", "uninstall", "stream_process"):
        monkeypatch.setattr(winget, fn, trap(f"winget.{fn}"))
    for fn in ("run_command", "stream_command", "start_detached", "set_user_dword"):
        monkeypatch.setattr(actions, fn, trap(f"actions.{fn}"))
    return touched


class TestDryRun:
    def test_real_catalog_dry_run_touches_nothing(self, no_system, tmp_path: Path) -> None:
        catalog = load_catalog()
        listener = RecordingListener()
        runner = TaskRunner(
            catalog,
            dry_run=True,
            dry_run_config=DryRunConfig(delay=0, fail_ids=frozenset({"7zip"})),
            listener=listener,
            arch="x64",
            app_dir=tmp_path,
        )
        summary = runner.run([i.id for i in catalog.items()])
        assert no_system == []
        statuses = {r.item_id: r.status for r in summary.results}
        assert statuses["7zip"] is TaskStatus.FAILED
        assert statuses["google-chrome"] is TaskStatus.OK
        # Availability still applies: no local files in tmp_path, arm64-only item on x64.
        for skipped in ("ul-procyon", "davinci-resolve-studio", "copy-benchmark-tools-arm"):
            assert statuses[skipped] is TaskStatus.SKIPPED
        assert statuses["remove-bloat"] is TaskStatus.OK
        assert summary.results[[r.item_id for r in summary.results].index("7zip")].message == (
            "Symulowany błąd (dry-run)."
        )

    def test_dry_run_uses_env_config_when_not_given(self, no_system, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("WINSTALLER_DRY_RUN_DELAY", "0")
        monkeypatch.setenv("WINSTALLER_DRY_RUN_FAIL", "*")
        catalog = make_catalog([make_item("a"), make_item("b", kind=ItemKind.ACTION)])
        summary = TaskRunner(catalog, dry_run=True, arch="x64", app_dir=tmp_path).run(["a", "b"])
        assert summary.failed_count == 2

    def test_explicit_executor_overrides_dry_run(self, catalog, tmp_path) -> None:
        runner = TaskRunner(
            catalog, dry_run=True, executor=ok_executor, arch="x64", app_dir=tmp_path
        )
        assert runner._executor is ok_executor

    def test_simulated_output_shape(self, tmp_path: Path) -> None:
        item = make_item("w", winget_id="V.W")
        events: list[tuple[str, str]] = []
        ctx = ActionContext(
            item=item,
            log=lambda t: events.append(("line", t)),
            replace_last=lambda t: events.append(("replace", t)),
            app_dir=tmp_path,
            arch="x64",
        )
        outcome = make_dry_run_executor(DryRunConfig(delay=0, progress_steps=4))(item, ctx)
        assert outcome == ActionOutcome(True, "Wykonano (dry-run).")
        kinds = [k for k, _ in events]
        assert kinds.count("replace") == 3
        progress = [t for k, t in events if k == "replace"]
        assert progress[-1].endswith("12.0 MB / 12.0 MB")
        assert events[-1] == ("line", "Pomyślnie zainstalowano")

    @pytest.mark.parametrize("kind", list(ItemKind))
    def test_every_kind_is_simulated(self, kind: ItemKind, tmp_path: Path) -> None:
        item = make_item("k", kind=kind)
        lines: list[str] = []
        ctx = ActionContext(
            item=item, log=lines.append, replace_last=lines.append, app_dir=tmp_path, arch="x64"
        )
        assert make_dry_run_executor(DryRunConfig(delay=0, progress_steps=0))(item, ctx).ok
        assert lines[0] == "[dry-run] K"

    def test_dry_run_cancel_interrupts_delay(self, no_system, tmp_path: Path) -> None:
        catalog = make_catalog([make_item("a"), make_item("b")])
        runner = TaskRunner(
            catalog,
            dry_run=True,
            dry_run_config=DryRunConfig(delay=5),
            arch="x64",
            app_dir=tmp_path,
        )
        threading.Timer(0.3, runner.cancel).start()
        t0 = time.monotonic()
        summary = runner.run(["a", "b"])
        assert time.monotonic() - t0 < 3
        assert [r.status for r in summary.results] == [TaskStatus.CANCELLED, TaskStatus.CANCELLED]
        assert summary.results[0].message == "Przerwano przez użytkownika."

    @pytest.mark.parametrize("cancel_after", [0.05, 0.15, 0.3])
    def test_dry_run_cancel_at_various_points(self, cancel_after, tmp_path: Path) -> None:
        catalog = make_catalog([make_item("a")])
        runner = TaskRunner(
            catalog,
            dry_run=True,
            dry_run_config=DryRunConfig(delay=0.1, progress_steps=3),
            arch="x64",
            app_dir=tmp_path,
        )
        threading.Timer(cancel_after, runner.cancel).start()
        summary = runner.run(["a"])
        assert summary.results[0].status is TaskStatus.CANCELLED
