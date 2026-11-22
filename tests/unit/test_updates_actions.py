"""Windows Update / Store as queue items: actions, catalog entries, runner order, dry run.

``winstaller.core.updates`` is replaced at its public functions (scan, start, store),
so nothing here can reach PowerShell, store.exe or the log folder.
"""

from __future__ import annotations

import dataclasses
import json
import threading
import types
from pathlib import Path
from typing import Any

import pytest

from tests.support import RecordingListener, make_ctx
from tests.updates_support import GUID_A, GUID_B, info, real_scan
from winstaller.core import actions, updates
from winstaller.core.actions import ActionError
from winstaller.core.catalog import ItemKind, load_catalog
from winstaller.core.runner import DryRunConfig, TaskRunner, TaskStatus
from winstaller.core.updates import (
    BackgroundInstall,
    StoreAppInfo,
    StoreUpdateResult,
    UpdatesCancelled,
    UpdatesError,
    WindowsUpdateScan,
)

CATALOG = load_catalog()
WU = CATALOG.item("windows-update")
STORE = CATALOG.item("store-update")
LOG = Path(r"C:\ProgramData\Winstaller\logs\windows-update-2026.11.22-120000.log")
STATUS = LOG.with_suffix(".json")


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    ns = types.SimpleNamespace(
        running=False,
        # The sample was captured without elevation; the action refuses to start then.
        scan=dataclasses.replace(real_scan(), is_admin=True),
        scan_calls=[],
        starts=[],
        start_result=BackgroundInstall(LOG, STATUS, 4321),
        store=StoreUpdateResult(method="AppInstallManager"),
        store_calls=[],
        events=[],
    )

    def running() -> bool:
        ns.events.append("mutex")
        return ns.running

    def scan(*, cancel_event: threading.Event | None = None, timeout: float = 180.0):
        ns.events.append("scan")
        ns.scan_calls.append(cancel_event)
        if isinstance(ns.scan, BaseException):
            raise ns.scan
        return ns.scan

    def start(*, update_ids=(), **kwargs: Any) -> BackgroundInstall:
        ns.events.append("start")
        ns.starts.append((list(update_ids), kwargs))
        if isinstance(ns.start_result, BaseException):
            raise ns.start_result
        return ns.start_result

    def store(*, cancel_event=None, timeout: float = 180.0, log=None) -> StoreUpdateResult:
        ns.store_calls.append((cancel_event, log))
        if isinstance(ns.store, BaseException):
            raise ns.store
        return ns.store

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("system layer reached")

    monkeypatch.setattr(updates, "background_install_running", running)
    monkeypatch.setattr(
        updates, "running_install", lambda: BackgroundInstall(LOG, None, None, True)
    )
    monkeypatch.setattr(updates, "scan_windows_updates", scan)
    monkeypatch.setattr(updates, "start_windows_update_install", start)
    monkeypatch.setattr(updates, "update_store_apps", store)
    monkeypatch.setattr(
        updates, "verify_background_start", lambda started, cancel_event=None: started
    )
    for name in ("run_powershell", "start_background", "run_store_cli", "log_dir", "mutex_exists"):
        monkeypatch.setattr(updates, name, forbidden)
    return ns


def run_wu(tmp_path: Path, cancel: threading.Event | None = None):
    ctx, rec = make_ctx(tmp_path, WU, cancel_event=cancel)
    return actions.execute(WU, ctx), rec


def run_store(tmp_path: Path, cancel: threading.Event | None = None):
    ctx, rec = make_ctx(tmp_path, STORE, cancel_event=cancel)
    return actions.execute(STORE, ctx), rec


# ------------------------------------------------------------- windows_update


def test_full_log_and_message(fake, tmp_path) -> None:
    outcome, rec = run_wu(tmp_path)
    labels = [f"  {u.label()}" for u in fake.scan.updates]
    assert rec.lines == [
        "Wyszukiwanie aktualizacji Windows (także opcjonalnych, Preview i sterowników) — "
        "może to potrwać kilka minut...",
        "Wyszukiwanie zakończone (7 s). Znalezione aktualizacje:",
        *labels,
        "Uruchomiono pobieranie i instalację w tle (PID 4321).",
        f"Log: {LOG}",
        f"Plik statusu: {STATUS}",
    ]
    assert outcome.ok and not outcome.cancelled and outcome.artifact == LOG
    assert outcome.message == (
        "Znaleziono 5 aktualizacji (w tym 5 opcjonalnych); instalacja uruchomiona w tle. "
        f"Postęp: Ustawienia → Windows Update lub log {LOG}. Po zakończeniu może być "
        "wymagany restart."
    )
    assert fake.starts == [([u.id for u in fake.scan.updates], {})]
    assert fake.events == ["mutex", "scan", "start"]


def test_counts_in_message(fake, tmp_path) -> None:
    fake.scan = WindowsUpdateScan(
        updates=(info(GUID_A, optional=True), info(GUID_B), info(GUID_A, revision=2))
    )
    outcome, _rec = run_wu(tmp_path)
    assert outcome.message.startswith("Znaleziono 3 aktualizacji (w tym 1 opcjonalnych)")
    assert fake.starts[0][0] == [GUID_A, GUID_B, GUID_A]  # start_* deduplicates


def test_up_to_date(fake, tmp_path) -> None:
    fake.scan = WindowsUpdateScan(updates=())
    outcome, rec = run_wu(tmp_path)
    assert outcome.ok and outcome.message == (
        "System jest aktualny — brak aktualizacji do zainstalowania."
    )
    assert fake.starts == [] and len(rec.lines) == 1


def test_already_running_skips_the_scan(fake, tmp_path) -> None:
    fake.running = True
    outcome, rec = run_wu(tmp_path)
    assert outcome.ok and outcome.artifact == LOG
    assert outcome.message == f"Instalacja aktualizacji już trwa w tle. Log: {LOG}"
    assert rec.lines == ["Instalacja aktualizacji Windows już trwa w tle — nie uruchamiam drugiej."]
    assert fake.scan_calls == [] and fake.starts == []


def test_already_running_detected_at_start(fake, tmp_path) -> None:
    fake.start_result = BackgroundInstall(LOG, None, None, already_running=True)
    outcome, rec = run_wu(tmp_path)
    assert outcome.ok and "już trwa w tle" in outcome.message
    assert not any(line.startswith("Uruchomiono") for line in rec.lines)


def test_reboot_pending_warning_before_start(fake, tmp_path) -> None:
    fake.scan = WindowsUpdateScan(updates=(info(),), reboot_pending=True)
    outcome, rec = run_wu(tmp_path)
    assert outcome.ok
    warning = next(i for i, line in enumerate(rec.lines) if line.startswith("Ostrzeżenie:"))
    started = next(i for i, line in enumerate(rec.lines) if line.startswith("Uruchomiono"))
    assert warning < started
    assert "ponowne uruchomienie" in rec.lines[warning]


def test_status_line_omitted_without_status_path(fake, tmp_path) -> None:
    fake.start_result = BackgroundInstall(LOG, None, 1)
    _outcome, rec = run_wu(tmp_path)
    assert not any(line.startswith("Plik statusu") for line in rec.lines)


@pytest.mark.parametrize(
    "error",
    [
        UpdatesError(
            "Brak połączenia z serwerami Windows Update (kod 0x8024402C).", kind="network"
        ),
        UpdatesError("Przekroczono limit czasu (180 s) — operacja przerwana.", kind="timeout"),
    ],
)
def test_scan_error_is_action_error(fake, tmp_path, error: UpdatesError) -> None:
    fake.scan = error
    with pytest.raises(ActionError) as exc:
        run_wu(tmp_path)
    assert str(exc.value) == error.message
    assert fake.starts == []


def test_start_error_is_action_error(fake, tmp_path) -> None:
    fake.start_result = UpdatesError("Nie udało się utworzyć logu X", kind="start")
    with pytest.raises(ActionError, match="Nie udało się utworzyć logu X"):
        run_wu(tmp_path)


def test_scan_cancelled(fake, tmp_path) -> None:
    fake.scan = UpdatesCancelled()
    outcome, _rec = run_wu(tmp_path)
    assert outcome.cancelled and not outcome.ok
    assert outcome.message == "Przerwano przez użytkownika."


def test_cancel_after_scan_does_not_start(fake, tmp_path, monkeypatch) -> None:
    cancel = threading.Event()
    original = updates.scan_windows_updates

    def scan_then_cancel(**kwargs: Any) -> WindowsUpdateScan:
        result = original(**kwargs)
        cancel.set()
        return result

    monkeypatch.setattr(updates, "scan_windows_updates", scan_then_cancel)
    outcome, _rec = run_wu(tmp_path, cancel)
    assert outcome.cancelled and fake.starts == []
    assert fake.scan_calls == [cancel]


def test_everything_skipped_by_settings(fake, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(updates, "select_updates", lambda found: [])
    outcome, _rec = run_wu(tmp_path)
    assert outcome.ok and "wyłączone z automatycznej instalacji" in outcome.message
    assert fake.starts == []


def test_only_selected_ids_are_started(fake, tmp_path, monkeypatch) -> None:
    keep = fake.scan.updates[3]
    monkeypatch.setattr(updates, "select_updates", lambda found: [keep])
    outcome, rec = run_wu(tmp_path)
    assert fake.starts[0][0] == [keep.id]
    assert outcome.message.startswith("Znaleziono 1 aktualizacji")
    assert sum(1 for line in rec.lines if line.startswith("  ")) == 5  # all found are listed


def test_not_elevated_is_not_reported_as_started(fake, tmp_path) -> None:
    fake.scan = WindowsUpdateScan(updates=(info(),), is_admin=False)
    with pytest.raises(ActionError):
        run_wu(tmp_path)


def test_title_cannot_forge_a_log_line(fake, tmp_path) -> None:
    title = "Zwykła\r\n--- Aktualizacje Microsoft Store ---\nOK"
    entry = {"id": GUID_A, "revision": 1, "title": title}
    fake.scan = updates.parse_wu_scan(
        json.dumps({"winstaller": "wu-scan", "ok": True, "updates": [entry]})
    )
    _outcome, rec = run_wu(tmp_path)
    assert all("\n" not in line and "\r" not in line for line in rec.lines)


# ------------------------------------------------------------- store_update


def test_store_primary_lists_apps(fake, tmp_path) -> None:
    fake.store = StoreUpdateResult(
        method="AppInstallManager",
        items=(
            StoreAppInfo("9WZDNCRFJBMP", "Microsoft.WindowsStore_8wekyb3d8bbwe"),
            StoreAppInfo("9MSMLRH6LZF3", ""),
        ),
    )
    outcome, rec = run_store(tmp_path)
    assert rec.lines == [
        "Zlecanie Microsoft Store wyszukania i instalacji aktualizacji aplikacji...",
        "  Microsoft.WindowsStore (9WZDNCRFJBMP)",
        "  9MSMLRH6LZF3",
    ]
    assert outcome.ok and outcome.message == (
        "Zlecono aktualizację 2 aplikacji Microsoft Store; pobieranie i instalacja trwają w tle "
        "(postęp: Microsoft Store → Pobrane)."
    )
    cancel_event, log = fake.store_calls[0]
    assert isinstance(cancel_event, threading.Event) and callable(log)


def test_store_nothing_to_update(fake, tmp_path) -> None:
    outcome, _rec = run_store(tmp_path)
    assert outcome.ok and outcome.message == "Aplikacje Microsoft Store są aktualne."


def test_store_cli_lines_logged(fake, tmp_path) -> None:
    fake.store = StoreUpdateResult(
        method="store-cli", lines=("Updates available (2 found)", "Pobieranie"), found_count=2
    )
    outcome, rec = run_store(tmp_path)
    assert rec.lines[1:] == ["  Updates available (2 found)", "  Pobieranie"]
    assert "Zlecono aktualizację 2 aplikacji" in outcome.message


def test_store_fallback_note_reaches_the_task_log(fake, tmp_path, monkeypatch) -> None:
    def store(*, cancel_event=None, timeout=180.0, log=None) -> StoreUpdateResult:
        assert log is not None
        log("Usługa Microsoft Store nie przyjęła zlecenia (x). Próbuję store.exe…")
        return StoreUpdateResult(method="store-cli", found_count=0)

    monkeypatch.setattr(updates, "update_store_apps", store)
    _outcome, rec = run_store(tmp_path)
    assert rec.lines[1].endswith("Próbuję store.exe…")


def test_store_error_and_cancel(fake, tmp_path) -> None:
    fake.store = UpdatesError(f"Nie udało się... {updates.STORE_MANUAL_HINT}", kind="store")
    with pytest.raises(ActionError) as exc:
        run_store(tmp_path)
    assert updates.STORE_MANUAL_HINT in str(exc.value)
    fake.store = UpdatesCancelled()
    outcome, _rec = run_store(tmp_path)
    assert outcome.cancelled


# ------------------------------------------------------------- runner integration


def run_real(ids: list[str], tmp_path: Path, cancel: threading.Event | None = None):
    listener = RecordingListener()
    runner = TaskRunner(CATALOG, listener=listener, arch="x64", app_dir=tmp_path)
    summary = runner.run(ids, cancel_event=cancel)
    lines = [p for n, p in listener.events if n == "output_line"]
    return summary, lines


def test_runner_reports_updates_error_as_failed_polish(fake, tmp_path) -> None:
    fake.scan = UpdatesError("Usługa Windows Update jest wyłączona (kod 0x80070422).")
    summary, lines = run_real(["windows-update"], tmp_path)
    result = summary.results[0]
    assert result.status == TaskStatus.FAILED
    assert result.message == "Usługa Windows Update jest wyłączona (kod 0x80070422)."
    assert lines[-1] == result.message


def test_runner_unexpected_exception_has_no_traceback(fake, tmp_path) -> None:
    fake.scan = RuntimeError("boom")
    summary, lines = run_real(["windows-update", "store-update"], tmp_path)
    assert [r.status for r in summary.results] == [TaskStatus.FAILED, TaskStatus.OK]
    assert summary.results[0].message == "Nieoczekiwany błąd: RuntimeError: boom"
    assert not any("Traceback" in line or 'File "' in line for line in lines)


def test_runner_cancelled_scan(fake, tmp_path) -> None:
    fake.scan = UpdatesCancelled()
    summary, _lines = run_real(["windows-update", "store-update"], tmp_path)
    assert summary.results[0].status == TaskStatus.CANCELLED


def test_runner_artifact_is_the_log(fake, tmp_path) -> None:
    summary, _lines = run_real(["windows-update"], tmp_path)
    assert summary.results[0].artifact == LOG and summary.results[0].status == TaskStatus.OK


# ------------------------------------------------------------- catalog


def test_catalog_entries() -> None:
    assert CATALOG.categories()[0].id == "updates"
    assert CATALOG.categories()[0].name == "Aktualizacje"
    assert [i.id for i in CATALOG.items() if i.category == "updates"] == [
        "windows-update",
        "store-update",
    ]
    for item, action in ((WU, "windows_update"), (STORE, "store_update")):
        assert item.kind == ItemKind.ACTION and item.action == action
        assert item.action in actions.ACTIONS
        for arch in ("x64", "arm64"):
            assert CATALOG.availability(item, arch=arch, base_dir=Path("C:/nie-ma")).available
    assert WU.destructive and "restart" in (WU.confirm or "")
    assert not STORE.destructive


def test_benchmark_profile_starts_with_updates() -> None:
    profile = CATALOG.profile("benchmark-starter")
    for arch in ("x64", "arm64"):
        assert list(profile.items_for(arch)[:2]) == ["windows-update", "store-update"]


def test_updates_run_first_whatever_the_selection_order(tmp_path) -> None:
    profile = CATALOG.profile("benchmark-starter").items_for("x64")
    listener = RecordingListener()
    runner = TaskRunner(
        CATALOG,
        dry_run=True,
        dry_run_config=DryRunConfig(delay=0.0),
        listener=listener,
        arch="x64",
        app_dir=tmp_path,
    )
    runner.run(list(reversed(profile)))
    order = listener.events[0][1]
    assert order[:2] == ["windows-update", "store-update"]
    assert sorted(order) == sorted(profile)
    everything = [i.id for i in CATALOG.items() if i.category != "tools"]
    assert CATALOG.in_catalog_order(list(reversed(everything)))[:2] == [
        "windows-update",
        "store-update",
    ]


# ------------------------------------------------------------- dry run


@pytest.fixture
def no_system(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("dry run reached the system layer")

    for name in (
        "run_powershell",
        "start_background",
        "run_store_cli",
        "log_dir",
        "mutex_exists",
        "scan_windows_updates",
        "start_windows_update_install",
        "update_store_apps",
        "background_install_running",
    ):
        monkeypatch.setattr(updates, name, forbidden)


def dry_runner(tmp_path: Path, **config: Any) -> tuple[TaskRunner, RecordingListener]:
    listener = RecordingListener()
    runner = TaskRunner(
        CATALOG,
        dry_run=True,
        dry_run_config=DryRunConfig(delay=0.0, **config),
        listener=listener,
        arch="arm64",
        app_dir=tmp_path,
    )
    return runner, listener


def test_dry_run_both_items(no_system, tmp_path) -> None:
    runner, listener = dry_runner(tmp_path)
    summary = runner.run(["store-update", "windows-update"])
    assert [(r.item_id, r.status) for r in summary.results] == [
        ("windows-update", TaskStatus.OK),
        ("store-update", TaskStatus.OK),
    ]
    lines = [p for n, p in listener.events if n == "output_line"]
    for sample in updates.DRY_RUN_WINDOWS_UPDATES:
        assert f"  {sample.label()}" in lines
    for app in updates.DRY_RUN_STORE_APPS:
        assert f"  {app.label()}" in lines
    assert any("symulacja, nic nie uruchomiono" in line for line in lines)
    assert "(dry-run)" in summary.results[0].message
    assert summary.results[0].message.startswith("Znaleziono 4 aktualizacji (w tym 3 opcjonalnych)")
    assert "Zlecono aktualizację 3 aplikacji" in summary.results[1].message
    assert list(tmp_path.iterdir()) == []


def test_dry_run_fail_ids(no_system, tmp_path) -> None:
    runner, _listener = dry_runner(tmp_path, fail_ids=frozenset({"windows-update"}))
    summary = runner.run(["windows-update", "store-update"])
    assert [r.status for r in summary.results] == [TaskStatus.FAILED, TaskStatus.OK]
    runner, _listener = dry_runner(tmp_path, fail_ids=frozenset({"*"}))
    summary = runner.run(["windows-update", "store-update"])
    assert [r.status for r in summary.results] == [TaskStatus.FAILED, TaskStatus.FAILED]


def test_dry_run_cancel_during_search(no_system, tmp_path) -> None:
    cancel = threading.Event()
    listener = RecordingListener()

    class Canceller(RecordingListener):
        def output_line(self, text: str) -> None:
            super().output_line(text)
            if "Wyszukiwanie aktualizacji Windows" in text:
                cancel.set()

    listener = Canceller()
    runner = TaskRunner(
        CATALOG,
        dry_run=True,
        dry_run_config=DryRunConfig(delay=0.01),
        listener=listener,
        arch="x64",
        app_dir=tmp_path,
    )
    summary = runner.run(["windows-update", "store-update"], cancel_event=cancel)
    assert [r.status for r in summary.results] == [TaskStatus.CANCELLED, TaskStatus.CANCELLED]
    lines = [p for n, p in listener.events if n == "output_line"]
    assert not any("Znalezione aktualizacje" in line for line in lines)


def test_dry_run_samples_are_valid() -> None:
    ids = [u.id for u in updates.DRY_RUN_WINDOWS_UPDATES]
    assert updates.validate_update_ids(ids) == ids
    assert updates.DRY_RUN_LOG_PATH.name.startswith(updates.LOG_PREFIX)
    assert updates.DRY_RUN_LOG_PATH.is_absolute()
