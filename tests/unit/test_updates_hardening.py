"""Windows Update / Store hardening: log folder DACL, child environment, start
verification, mutex confirmation, Store result rules and other fixes after review.

Real Win32 calls only on folders inside ``tmp_path`` (CreateDirectoryW with a security
descriptor, GetNamedSecurityInfoW, junctions); processes and PowerShell are faked.
"""

from __future__ import annotations

import _winapi
import ast
import json
import re
import threading
import types
from pathlib import Path

import pytest

from tests.support import make_ctx
from tests.updates_support import GUID_A, GUID_B, info, scan_with, store_line, update_entry
from winstaller.core import actions, drivers, update_scripts, updates, winget
from winstaller.core import platform as plat
from winstaller.core.actions import ActionError
from winstaller.core.catalog import load_catalog
from winstaller.core.updates import (
    BackgroundInstall,
    StoreAppInfo,
    StoreUpdateResult,
    UpdatesCancelled,
    UpdatesError,
    WindowsUpdateScan,
)

CATALOG = load_catalog()
SID_OWNER_RIGHTS = "S-1-3-4"
SID_USERS = "S-1-5-32-545"
#: LOG_DIR_SDDL plus OWNER RIGHTS, so the (non-elevated) test can still delete the folder.
TEST_SDDL = updates.LOG_DIR_SDDL + "(A;OICI;FA;;;OW)"

# ------------------------------------------------------------- secure folder (real API)


def test_create_secure_dir_applies_protected_dacl(tmp_path) -> None:
    folder = tmp_path / "Winstaller"
    assert updates.create_secure_dir(folder, TEST_SDDL) is True
    assert folder.is_dir()
    security = updates.read_dir_security(folder)
    assert security is not None
    _owner, aces = security
    sids = {sid for _type, _mask, sid in aces}
    # Exactly what the SDDL says: nothing inherited from tmp_path (protected DACL).
    assert sids == {
        updates.SID_ADMINISTRATORS,
        updates.SID_SYSTEM,
        SID_USERS,
        SID_OWNER_RIGHTS,
    }
    users = [mask for _t, mask, sid in aces if sid == SID_USERS]
    assert users and not any(mask & updates._WRITE_RIGHTS for mask in users)
    # Second call: something exists, nothing is modified.
    assert updates.create_secure_dir(folder, TEST_SDDL) is False


def test_create_secure_dir_never_goes_through_a_junction(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    _winapi.CreateJunction(str(target), str(link))
    assert updates.create_secure_dir(link, TEST_SDDL) is False
    assert list(target.iterdir()) == []
    assert not updates.dir_is_trusted(link)


def test_create_secure_dir_missing_parent_raises(tmp_path) -> None:
    with pytest.raises(OSError):
        updates.create_secure_dir(tmp_path / "nie" / "ma", TEST_SDDL)


def test_dir_created_by_a_user_is_not_trusted(tmp_path) -> None:
    folder = tmp_path / "plain"
    folder.mkdir()  # owner: the current (non-elevated) user, inherited DACL
    assert not updates.dir_is_trusted(folder)
    assert not updates.dir_is_trusted(tmp_path / "missing")


ADMINS = updates.SID_ADMINISTRATORS
SYSTEM = updates.SID_SYSTEM
FULL = 0x001F01FF
READ = 0x001200A9


@pytest.mark.parametrize(
    "owner,aces,trusted",
    [
        (ADMINS, [(0, FULL, ADMINS), (0, FULL, SYSTEM), (0, READ, SID_USERS)], True),
        (SYSTEM, [(0, FULL, SYSTEM)], True),
        (updates.SID_TRUSTED_INSTALLER, [(0, READ, SID_USERS)], True),
        ("S-1-5-21-1-2-3-1001", [(0, FULL, ADMINS)], False),  # a user owns it
        (ADMINS, [(0, 0x0002, SID_USERS)], False),  # Users may add files
        (ADMINS, [(0, 0x0004, "S-1-1-0")], False),  # Everyone may add folders
        (ADMINS, [(0, 0x10000000, "S-1-3-0")], False),  # CREATOR OWNER: GENERIC_ALL
        (ADMINS, [(0, 0x00040000, SID_USERS)], False),  # WRITE_DAC
        (ADMINS, [(1, FULL, SID_USERS), (0, READ, SID_USERS)], True),  # deny is fine
        (ADMINS, [(9, READ, "")], False),  # callback ACE: not understood -> untrusted
        ("", [], False),
    ],
)
def test_security_rule(owner: str, aces: list[tuple[int, int, str]], trusted: bool) -> None:
    assert updates.security_is_trusted(owner, aces) is trusted


def test_existing_log_dirs_is_read_only(monkeypatch, tmp_path) -> None:
    program_data = tmp_path / "ProgramData"
    local = tmp_path / "Local"
    program_data.mkdir()
    local.mkdir()
    (program_data / "Winstaller" / "logs").mkdir(parents=True)
    (program_data / "Winstaller-ab12cd34" / "logs").mkdir(parents=True)
    monkeypatch.setattr(plat, "program_data_dir", lambda: program_data)
    monkeypatch.setattr(plat, "local_app_data_dir", lambda: local)
    monkeypatch.setattr(plat, "is_admin", lambda: True)
    trusted = {program_data / "Winstaller-ab12cd34", program_data / "Winstaller-ab12cd34" / "logs"}
    monkeypatch.setattr(updates, "dir_is_trusted", lambda p: p in trusted)
    before = sorted(p for p in tmp_path.rglob("*"))
    assert updates.existing_log_dirs() == [program_data / "Winstaller-ab12cd34" / "logs"]
    assert sorted(p for p in tmp_path.rglob("*")) == before
    monkeypatch.setattr(plat, "is_admin", lambda: False)
    assert updates.existing_log_dirs() == [program_data / "Winstaller-ab12cd34" / "logs"]
    (local / "Winstaller" / "logs").mkdir(parents=True)
    assert updates.existing_log_dirs()[-1] == local / "Winstaller" / "logs"


# ------------------------------------------------------------- child environment


def test_child_env_is_an_allow_list(monkeypatch) -> None:
    for name, value in {
        "FOO_BAR": "x",
        "DOTNET_STARTUP_HOOKS": r"C:\x.dll",
        "COMPLUS_ETWENABLED": "1",
        "PYTHONPATH": r"C:\evil",
        "Path": r"C:\Users\x\bin;C:\evil",
        "WINSTALLER_ARCH": "arm64",
        "systemroot": r"C:\WINDOWS",
    }.items():
        monkeypatch.setenv(name, value)
    env = updates._child_env({"systemroot": r"D:\override", "WINSTALLER_X": "1"})
    keys = {k.upper() for k in env}
    assert not {"FOO_BAR", "DOTNET_STARTUP_HOOKS", "COMPLUS_ETWENABLED", "PYTHONPATH"} & keys
    assert env["WINSTALLER_ARCH"] == "arm64" and env["WINSTALLER_X"] == "1"
    assert [v for k, v in env.items() if k.upper() == "SYSTEMROOT"] == [r"D:\override"]
    system = plat.windows_system_dir()
    paths = [v for k, v in env.items() if k.upper() == "PATH"]
    assert paths == [f"{system};{system / 'Wbem'};{system / 'WindowsPowerShell' / 'v1.0'}"]
    assert set(keys) <= {n.upper() for n in updates.CHILD_ENV_ALLOWED} | {
        "PATH",
        "PSMODULEPATH",
        "WINSTALLER_ARCH",
        "WINSTALLER_X",
    }


def test_store_cli_and_background_use_the_allow_list(monkeypatch) -> None:
    monkeypatch.setenv("COR_PROFILER", "{x}")
    calls: list[dict[str, str]] = []

    class Proc:
        pid = 1

    def popen(args, **kwargs):
        calls.append(kwargs["env"])
        return Proc()

    monkeypatch.setattr(updates.subprocess, "Popen", popen)
    monkeypatch.setattr(updates, "_BACKGROUND", [])
    updates.start_background(["x"], {"WINSTALLER_WU_LOG": "y"})
    assert "COR_PROFILER" not in {k.upper() for k in calls[0]}


# ------------------------------------------------------------- start verification


@pytest.fixture
def started(tmp_path, monkeypatch) -> types.SimpleNamespace:
    ns = types.SimpleNamespace(alive=True, exit_code=None)
    log = tmp_path / "windows-update-2026.11.22-120000.log"
    log.write_text("x", encoding="utf-8")
    ns.install = BackgroundInstall(log, log.with_suffix(".json"), 4242)
    monkeypatch.setattr(updates, "process_running", lambda pid: ns.alive)
    monkeypatch.setattr(updates, "background_exit_code", lambda pid: ns.exit_code)

    def status(**data: object) -> None:
        payload = {"winstaller": "wu-install", "pid": 4242, **data}
        ns.install.status_path.write_text(json.dumps(payload), encoding="utf-8")

    ns.status = status
    return ns


def test_verify_running_status_confirms(started) -> None:
    started.status(state="running")
    assert updates.verify_background_start(started.install, timeout=2) == started.install


def test_verify_finished_zero_is_ok(started) -> None:
    started.status(state="finished", exitCode=0)
    result = updates.verify_background_start(started.install, timeout=2)
    assert result.finished and result.pid == 4242


@pytest.mark.parametrize(
    "code,fragment",
    [
        (3, "już trwa w innym procesie"),
        (4, "uprawnień administratora"),
        (5, "ponownego uruchomienia"),
        (6, "zajęty"),
        (7, "Nieprawidłowe parametry"),
        (1, "nieoczekiwanym błędem"),
        (42, "kod 42"),
    ],
)
def test_verify_early_exit_with_status(started, code: int, fragment: str) -> None:
    started.status(state="finished", exitCode=code)
    started.alive = False
    with pytest.raises(UpdatesError) as exc:
        updates.verify_background_start(started.install, timeout=2)
    assert fragment in exc.value.message
    assert str(started.install.log_path) in exc.value.message


def test_verify_exit_without_status_uses_the_exit_code(started) -> None:
    started.alive = False
    started.exit_code = 7
    with pytest.raises(UpdatesError, match="Nieprawidłowe parametry"):
        updates.verify_background_start(started.install, timeout=2)
    started.exit_code = None
    with pytest.raises(UpdatesError, match="bez zapisania statusu"):
        updates.verify_background_start(started.install, timeout=2)


def test_verify_status_of_another_pid_is_ignored(started) -> None:
    started.status(state="running", pid=1)
    with pytest.raises(UpdatesError, match="nie potwierdził startu"):
        updates.verify_background_start(started.install, timeout=0.3, poll=0.05)


def test_verify_running_status_of_a_dead_process_is_not_success(started) -> None:
    started.status(state="running")
    started.alive = False
    started.exit_code = 1
    with pytest.raises(UpdatesError):
        updates.verify_background_start(started.install, timeout=2)


def test_verify_cancel(started) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(UpdatesCancelled, match="mogła już ruszyć"):
        updates.verify_background_start(started.install, cancel_event=cancel, timeout=5)


def test_verify_garbage_status_is_not_confirmation(started) -> None:
    started.install.status_path.write_bytes(b"\xff{nie json")
    with pytest.raises(UpdatesError):
        updates.verify_background_start(started.install, timeout=0.3, poll=0.05)


def test_verify_skips_already_running() -> None:
    running = BackgroundInstall(Path("C:/x.log"), None, None, already_running=True)
    assert updates.verify_background_start(running) is running


def test_read_status_rejects_foreign_json(tmp_path) -> None:
    path = tmp_path / "s.json"
    path.write_text('{"state": "running", "pid": 1}', encoding="utf-8")
    assert updates.read_status(path) is None
    path.write_text('\ufeff{"winstaller": "wu-install", "state": "running"}', encoding="utf-8")
    assert updates.read_status(path) == {"winstaller": "wu-install", "state": "running"}
    assert updates.read_status(tmp_path / "missing.json") is None


# ------------------------------------------------------------- start: lists, orphans, paths


@pytest.fixture
def start_env(monkeypatch, tmp_path) -> types.SimpleNamespace:
    folder = tmp_path / "logs"
    folder.mkdir()
    ns = types.SimpleNamespace(folder=folder, starts=[], error=None)
    monkeypatch.setattr(updates, "log_dir", lambda: ns.folder)
    monkeypatch.setattr(updates, "mutex_exists", lambda name: False)

    def start(args, env):
        if ns.error is not None:
            raise ns.error
        ns.starts.append(env)
        return 99

    monkeypatch.setattr(updates, "start_background", start)
    return ns


def test_too_long_allow_list_switches_to_install_all(start_env) -> None:
    ids = [f"{n:08x}-0000-4000-8000-000000000000" for n in range(900)]
    result = updates.start_windows_update_install(update_ids=ids)
    assert result.install_all
    env = start_env.starts[0]
    assert env[updates.ENV_WU_IDS] == "" and env[updates.ENV_WU_ALL] == "1"


def test_failed_start_removes_the_orphaned_log(start_env) -> None:
    start_env.error = OSError(None, "x", None, 87)
    with pytest.raises(UpdatesError):
        updates.start_windows_update_install(update_ids=[GUID_A])
    assert list(start_env.folder.iterdir()) == []


def test_untrusted_fallback_folder_is_noted_in_the_log(start_env, tmp_path) -> None:
    folder = tmp_path / "Winstaller-0011aabb" / "logs"
    folder.mkdir(parents=True)
    start_env.folder = folder
    result = updates.start_windows_update_install(update_ids=[GUID_A])
    assert "był niezaufany" in result.log_path.read_text(encoding="utf-8-sig")


# ------------------------------------------------------------- parsers and helpers


def test_seconds_not_finite_are_dropped() -> None:
    for value in ("NaN", "Infinity", "-Infinity"):
        text = scan_with(update_entry()).replace('"ok": true', f'"ok": true, "seconds": {value}')
        assert updates.parse_wu_scan(text).seconds == 0.0


def test_result_code_3_is_a_warning() -> None:
    scan = updates.parse_wu_scan(scan_with(update_entry(), resultCode=3))
    assert len(scan.updates) == 1 and "ResultCode 3" in scan.warnings[0]


def test_is_admin_unknown_when_missing() -> None:
    assert updates.parse_wu_scan(scan_with(update_entry())).is_admin is None
    assert updates.parse_wu_scan(scan_with(update_entry(), isAdmin=False)).is_admin is False


def test_invisible_characters_removed_from_titles() -> None:
    title = "Sterownik\u202eexe.txt\u200b X\u2066"
    scan = updates.parse_wu_scan(scan_with(update_entry(title=title, kb=[])))
    assert scan.updates[0].title == "Sterownikexe.txt X"


def test_plain_stderr_with_objs_is_kept() -> None:
    assert updates.clean_powershell_stderr("Błąd: <Objs> nie działa") == "Błąd: <Objs> nie działa"


def test_display_driver_skip_is_case_insensitive() -> None:
    items = [info(GUID_A, type="driver", driver_class="DISPLAY"), info(GUID_B)]
    assert updates.select_updates(items, skip_display_drivers=True) == [items[1]]


def test_store_error_items_are_not_counted() -> None:
    found = [
        {"productId": "A", "packageFamilyName": "A_x", "state": "Pending"},
        {"productId": "B", "packageFamilyName": "B_x", "state": "Error"},
        {"productId": "C", "packageFamilyName": "C_x", "errorCode": "0x80070005"},
    ]
    result = updates.parse_store_update(store_line(found=found))
    assert [i.product_id for i in result.items] == ["A"]
    assert [i.product_id for i in result.failed] == ["B", "C"]
    assert result.failed[1].error_code == "0x80070005" and result.count == 1
    assert result.confirmed


def test_store_cli_result_is_never_confirmed() -> None:
    result = updates.parse_store_cli_output("Updates available (2 found)\r\nSomething\r\n")
    assert not result.confirmed and result.found_count == 2
    unknown = updates.parse_store_cli_output("Something happened")
    assert unknown.found_count is None and not unknown.confirmed


#: Calls whose first argument is a regular expression.
_REGEX_CALLS = {"compile", "search", "match", "fullmatch", "sub", "subn", "split", "findall"}


def _string_values(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [v for part in node.values for v in _string_values(part)]
    if isinstance(node, ast.FormattedValue):
        return []
    if isinstance(node, ast.BinOp):
        return _string_values(node.left) + _string_values(node.right)
    return []


@pytest.mark.parametrize("module", [updates, drivers, winget])
def test_no_control_characters_in_regex_sources(module) -> None:
    """A "\\b" in a non-raw string is a backspace (0x08), not a word boundary."""
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    control = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
    checked = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in _REGEX_CALLS:
            continue
        if isinstance(func, ast.Attribute) and getattr(func.value, "id", "re") != "re":
            continue
        for value in _string_values(node.args[0]):
            checked += 1
            assert not control.search(value), (module.__name__, node.lineno, value)
    assert checked > 0


# ------------------------------------------------------------- scripts


def test_install_script_restricts_its_mutex() -> None:
    script = update_scripts.WU_INSTALL_SCRIPT
    owned = script.index("} else {", script.index("if (-not $owned)"))
    acl = script.index("$mutex.SetAccessControl($sec)")
    assert owned < acl < script.index("Save-Status", owned)
    assert "SetAccessRuleProtection($true, $false)" in script
    assert "@('S-1-5-32-544', 'S-1-5-18')" in script


def test_install_script_checks_elevation_before_the_first_status() -> None:
    script = update_scripts.WU_INSTALL_SCRIPT
    body = script[script.index("$exitCode = 1") :]
    assert body.index("$exitCode = 4") < body.index("Save-Status")


def test_install_script_requires_list_or_all_flag() -> None:
    script = update_scripts.WU_INSTALL_SCRIPT
    assert "$InstallAll = ([string]$env:WINSTALLER_WU_ALL -eq '1')" in script
    assert "(-not $InstallAll -and $IdList.Count -eq 0)) { exit 7 }" in script


def test_scripts_clean_text_before_json_and_log() -> None:
    assert "title = (Get-CleanText $u.Title)" in update_scripts.WU_SCAN_SCRIPT
    assert "(Get-CleanText $Message)" in update_scripts.WU_INSTALL_SCRIPT
    assert "timeout = $isTimeout" in update_scripts.STORE_SCRIPT
    assert "if ($out.resultCode -ge 4)" in update_scripts.WU_SCAN_SCRIPT


# ------------------------------------------------------------- actions

WU = CATALOG.item("windows-update")
STORE = CATALOG.item("store-update")


@pytest.fixture
def wu(monkeypatch) -> types.SimpleNamespace:
    log = Path(r"C:\ProgramData\Winstaller\logs\windows-update-2026.11.22-120000.log")
    ns = types.SimpleNamespace(
        mutex=False,
        running=None,
        scan=WindowsUpdateScan(updates=(info(GUID_A),), is_admin=True),
        started=BackgroundInstall(log, log.with_suffix(".json"), 1),
        verify=None,
        starts=0,
    )

    def running_install() -> BackgroundInstall:
        if isinstance(ns.running, BaseException):
            raise ns.running
        return ns.running

    def verify(started, cancel_event=None):
        if isinstance(ns.verify, BaseException):
            raise ns.verify
        return ns.verify or started

    def start(**kwargs):
        ns.starts += 1
        return ns.started

    monkeypatch.setattr(updates, "background_install_running", lambda: ns.mutex)
    monkeypatch.setattr(updates, "running_install", running_install)
    monkeypatch.setattr(updates, "scan_windows_updates", lambda **kw: ns.scan)
    monkeypatch.setattr(updates, "start_windows_update_install", start)
    monkeypatch.setattr(updates, "verify_background_start", verify)
    return ns


def run(tmp_path: Path, item=WU):
    ctx, rec = make_ctx(tmp_path, item)
    return actions.execute(item, ctx), rec


def test_blocked_mutex_fails_the_task(wu, tmp_path) -> None:
    wu.mutex = True
    wu.running = UpdatesError(updates.BLOCKED_MESSAGE.format(name="x"), kind="blocked")
    with pytest.raises(ActionError, match="Inny proces blokuje"):
        run(tmp_path)


def test_unconfirmed_start_fails_the_task(wu, tmp_path) -> None:
    wu.verify = UpdatesError("Instalacja aktualizacji Windows wymaga uprawnień (kod 4). Log: x")
    with pytest.raises(ActionError, match="kod 4"):
        run(tmp_path)


def test_cancel_while_confirming(wu, tmp_path) -> None:
    wu.verify = UpdatesCancelled("Przerwano oczekiwanie; instalacja mogła już ruszyć w tle.")
    outcome, _rec = run(tmp_path)
    assert outcome.cancelled and "mogła już ruszyć" in outcome.message


def test_finished_at_once_is_reported(wu, tmp_path) -> None:
    wu.verify = BackgroundInstall(wu.started.log_path, None, 1, finished=True)
    outcome, _rec = run(tmp_path)
    assert outcome.ok and "zakończył się od razu bez błędu" in outcome.message


def test_scan_warnings_and_skipped_entries_are_logged(wu, tmp_path) -> None:
    wu.scan = WindowsUpdateScan(
        updates=(info(GUID_A),), is_admin=True, warnings=("Ostrzeżenie: pominięto X",), skipped=1
    )
    _outcome, rec = run(tmp_path)
    assert "Ostrzeżenie: pominięto X" in rec.lines
    assert "Wyszukiwanie zakończone. Znalezione aktualizacje:" in rec.lines  # no "(0 s)"


def test_only_skipped_entries_is_not_up_to_date(wu, tmp_path) -> None:
    wu.scan = WindowsUpdateScan(updates=(), is_admin=True, skipped=2, warnings=("a", "b"))
    with pytest.raises(ActionError, match="2 pominiętych"):
        run(tmp_path)
    assert wu.starts == 0


def test_install_all_fallback_is_logged(wu, tmp_path) -> None:
    wu.started = BackgroundInstall(wu.started.log_path, None, 1, install_all=True)
    _outcome, rec = run(tmp_path)
    assert any("zbyt długa" in line for line in rec.lines)


@pytest.fixture
def store(monkeypatch) -> types.SimpleNamespace:
    ns = types.SimpleNamespace(result=StoreUpdateResult(method="AppInstallManager"))
    monkeypatch.setattr(updates, "update_store_apps", lambda **kw: ns.result)
    return ns


def test_store_all_items_failed_is_an_error(store, tmp_path) -> None:
    store.result = StoreUpdateResult(
        method="AppInstallManager", failed=(StoreAppInfo("A", "A_x", "", "Error", "0x1"),)
    )
    with pytest.raises(ActionError, match="wszystkie 1 pozycji"):
        run(tmp_path, STORE)


def test_store_partial_failure_warns(store, tmp_path) -> None:
    store.result = StoreUpdateResult(
        method="AppInstallManager",
        items=(StoreAppInfo("A", "A_x"),),
        failed=(StoreAppInfo("B", "B_x", "", "Error"),),
    )
    outcome, rec = run(tmp_path, STORE)
    assert outcome.ok and "Uwaga: 1 pozycji z błędem" in outcome.message
    assert any(line.startswith("  Błąd: B (B)") for line in rec.lines)


def test_store_cli_message_says_unconfirmed(store, tmp_path) -> None:
    store.result = StoreUpdateResult(method="store-cli", lines=("x",), found_count=None)
    outcome, _rec = run(tmp_path, STORE)
    assert outcome.ok and "wiersz poleceń Sklepu" in outcome.message
    assert "nie da się potwierdzić" in outcome.message
