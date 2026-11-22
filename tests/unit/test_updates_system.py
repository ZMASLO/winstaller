"""Windows Update / Store: the system layer and the functions built on it.

Real processes are only harmless ``python -c`` children (``powershell_args`` or
``store_cli_path`` pointed at this interpreter); everything else (PowerShell, icacls,
store.exe, the WUA mutex) is faked. Junctions are created with ``_winapi`` inside
``tmp_path``.
"""

from __future__ import annotations

import _winapi
import contextlib
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import types
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from tests.updates_support import (
    GUID_A,
    GUID_B,
    decode_script,
    scan_line,
    store_line,
)
from winstaller.core import platform as plat
from winstaller.core import update_scripts, updates, winget
from winstaller.core.updates import UpdatesCancelled, UpdatesError

NOW = datetime(2026, 11, 22, 12, 0, 0)
STAMP = "2026.11.22-120000"
#: The real interpreter (a venv python.exe is a launcher that runs it as a child).
PYTHON = getattr(sys, "_base_executable", None) or sys.executable

# ------------------------------------------------------------- helpers


def wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


_SYNCHRONIZE = 0x00100000
_QUERY = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION


def process_alive(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(_SYNCHRONIZE | _QUERY, 0, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


@pytest.fixture
def python_powershell(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_powershell`` runs ``sys.executable -c <script>`` instead of PowerShell."""
    monkeypatch.setattr(
        updates, "powershell_args", lambda script, hidden=False: [PYTHON, "-c", script]
    )


@pytest.fixture
def background(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[subprocess.Popen[bytes]]]:
    procs: list[subprocess.Popen[bytes]] = []
    monkeypatch.setattr(updates, "_BACKGROUND", procs)
    yield procs
    for proc in procs:
        with contextlib.suppress(OSError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(10)


class FakeProc:
    """Popen stand-in: ``communicate`` returns ``out`` or blocks until killed."""

    def __init__(self, out: bytes = b"", err: bytes = b"", rc: int = 0, block: bool = False):
        self.out, self.err, self.rc = out, err, rc
        self.killed = threading.Event()
        self.block = block
        self.returncode: int | None = None
        self.pid = 4242

    def communicate(self) -> tuple[bytes, bytes]:
        if self.block:
            self.killed.wait(10)
            self.returncode = 1
            return b"", b""
        self.returncode = self.rc
        return self.out, self.err

    def kill(self) -> None:
        self.killed.set()

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0


class PopenRecorder:
    def __init__(self, *results: object) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.results = list(results)

    def __call__(self, args: Any, **kwargs: Any) -> Any:
        self.calls.append((list(args), kwargs))
        result = self.results.pop(0) if self.results else FakeProc()
        if isinstance(result, BaseException):
            raise result
        return result


# ------------------------------------------------------------- validate_update_ids


def test_validate_update_ids_normalises_and_dedupes() -> None:
    ids = [GUID_A.upper(), f"  {GUID_B}\t", GUID_A, GUID_B.upper()]
    assert updates.validate_update_ids(ids) == [GUID_A, GUID_B]
    assert updates.validate_update_ids([]) == []
    assert updates.validate_update_ids(iter([GUID_A])) == [GUID_A]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        " ",
        None,
        5,
        "{" + GUID_A + "}",
        GUID_A + "'; calc; '",
        f"{GUID_A},{GUID_B}",
        GUID_A + "\n" + GUID_B,
        GUID_A[:-1],
        GUID_A + "0",
        GUID_A.replace("-", ""),
        "$(Remove-Item C:\\x)",
        "71ab9cfc-4bdf-48b7-b7ad-7f4ce4605cdg",
        "71ab9cfc-4bdf-48b7-b7ad-7f4ce4605cd٧",  # Arabic-Indic digit
    ],
)
def test_validate_update_ids_rejects(bad: object) -> None:
    with pytest.raises(UpdatesError) as exc:
        updates.validate_update_ids([GUID_A, bad])  # type: ignore[list-item]
    assert exc.value.kind == "format"
    assert exc.value.message.startswith("Nieprawidłowy identyfikator aktualizacji")


def test_validate_rejects_a_bare_string() -> None:
    with pytest.raises(UpdatesError):
        updates.validate_update_ids(GUID_A)  # iterates characters


def test_many_ids_fit_in_one_environment_variable() -> None:
    ids = [f"{n:08x}-0000-4000-8000-000000000000" for n in range(800)]
    value = ",".join(updates.validate_update_ids(ids))
    assert len(value) < 32_767  # per-variable limit of CreateProcess


# ------------------------------------------------------------- child environment


def test_child_env_merges_and_overrides(monkeypatch) -> None:
    monkeypatch.setenv("WINSTALLER_TEST_PARENT", "rodzic")
    env = updates._child_env({"WINSTALLER_TEST_PARENT": "dziecko", "NO_COLOR": "1"})
    assert env["WINSTALLER_TEST_PARENT"] == "dziecko" and env["NO_COLOR"] == "1"
    assert "SYSTEMROOT" in {k.upper() for k in env}
    assert updates._child_env(None)["WINSTALLER_TEST_PARENT"] == "rodzic"


def test_child_env_drops_psmodulepath(monkeypatch) -> None:
    monkeypatch.setenv("PSModulePath", r"C:\Program Files\PowerShell\7\Modules")
    env = updates._child_env({"X": "1"})
    # The user's value never passes; the system module path is set explicitly instead.
    values = [v for k, v in env.items() if k.lower() == "psmodulepath"]
    assert values == [str(plat.windows_system_dir() / "WindowsPowerShell" / "v1.0" / "Modules")]


def test_child_env_drops_dotnet_injection_variables(monkeypatch) -> None:
    for name in ("COR_ENABLE_PROFILING", "COR_PROFILER", "COR_PROFILER_PATH", "COMPlus_X"):
        monkeypatch.setenv(name, "1")
    keys = {k.upper() for k in updates._child_env(None)}
    assert not {"COR_ENABLE_PROFILING", "COR_PROFILER", "COR_PROFILER_PATH", "COMPLUS_X"} & keys


# ------------------------------------------------------------- run_powershell (python child)


def test_run_powershell_output_exit_code_and_stdin(python_powershell) -> None:
    script = (
        "import sys\n"
        "data = sys.stdin.read()\n"
        "sys.stdout.buffer.write(('zażółć ' + repr(data) + '\\n').encode('utf-8'))\n"
        "sys.stderr.buffer.write('błąd'.encode('utf-8'))\n"
        "sys.exit(3)\n"
    )
    rc, out, err = updates.run_powershell(script, timeout=30)
    assert (rc, out, err) == (3, "zażółć ''\n", "błąd")


def test_run_powershell_passes_environment(python_powershell, monkeypatch) -> None:
    monkeypatch.setenv("WINSTALLER_TEST_PARENT", "z rodzica")
    script = (
        "import os, json\n"
        "print(json.dumps({k: os.environ.get(k) for k in "
        "('WINSTALLER_TEST_PARENT', 'WINSTALLER_X', 'SYSTEMROOT')}))\n"
    )
    _rc, out, _err = updates.run_powershell(script, env={"WINSTALLER_X": "ąę ' $x"}, timeout=30)
    data = json.loads(out)
    assert data["WINSTALLER_TEST_PARENT"] == "z rodzica"
    assert data["WINSTALLER_X"] == "ąę ' $x"
    assert data["SYSTEMROOT"]


def test_run_powershell_timeout_kills(python_powershell, tmp_path) -> None:
    pid_file = tmp_path / "pid"
    script = (
        f"import os, time, pathlib\npathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    with pytest.raises(UpdatesError) as exc:
        updates.run_powershell(script, timeout=1.5)
    assert exc.value.kind == "timeout" and not isinstance(exc.value, UpdatesCancelled)
    assert "Przekroczono limit czasu (1 s)" in exc.value.message
    assert time.monotonic() - started < 10
    if pid_file.exists():
        wait_for(lambda: not process_alive(int(pid_file.read_text())))


def test_run_powershell_cancel_kills(python_powershell, tmp_path) -> None:
    pid_file = tmp_path / "pid"
    script = (
        f"import os, time, pathlib\npathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    cancel = threading.Event()
    timer = threading.Thread(target=lambda: (wait_for(pid_file.exists), cancel.set()))
    timer.start()
    with pytest.raises(UpdatesCancelled):
        updates.run_powershell(script, timeout=60, cancel_event=cancel)
    timer.join()
    wait_for(lambda: not process_alive(int(pid_file.read_text())))


def test_run_powershell_does_not_start_when_already_cancelled(monkeypatch) -> None:
    recorder = PopenRecorder(FakeProc(block=True))
    monkeypatch.setattr(subprocess, "Popen", recorder)
    monkeypatch.setattr(updates, "powershell_args", lambda s, hidden=False: ["x"])
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(UpdatesCancelled):
        updates.run_powershell("x", timeout=30, cancel_event=cancel)
    assert recorder.calls == []


def test_run_powershell_popen_flags(monkeypatch) -> None:
    recorder = PopenRecorder(FakeProc(out=b"ok", rc=0))
    monkeypatch.setattr(subprocess, "Popen", recorder)
    rc, out, err = updates.run_powershell(update_scripts.WU_SCAN_SCRIPT, timeout=5)
    assert (rc, out, err) == (0, "ok", "")
    args, kwargs = recorder.calls[0]
    assert args == updates.powershell_args(update_scripts.WU_SCAN_SCRIPT)
    assert "-WindowStyle" not in args
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE and kwargs["stderr"] is subprocess.PIPE
    assert kwargs["creationflags"] == winget.CREATE_NO_WINDOW
    assert not kwargs.get("shell")


def test_run_powershell_cannot_start(monkeypatch) -> None:
    monkeypatch.setattr(
        updates, "powershell_args", lambda s, hidden=False: [r"C:\nie-ma\powershell.exe"]
    )
    with pytest.raises(UpdatesError) as exc:
        updates.run_powershell("x", timeout=5)
    assert exc.value.kind == "start"
    assert exc.value.message.startswith("Nie udało się uruchomić PowerShella")


def test_run_powershell_missing_powershell(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(plat, "windows_system_dir", lambda: tmp_path)
    with pytest.raises(UpdatesError) as exc:
        updates.run_powershell(update_scripts.WU_SCAN_SCRIPT, timeout=5)
    assert exc.value.kind == "start"


def test_run_powershell_returncode_none_becomes_minus_one(monkeypatch) -> None:
    proc = FakeProc(out=b"x")
    proc.communicate = lambda: (b"x", b"")  # type: ignore[method-assign]
    monkeypatch.setattr(subprocess, "Popen", PopenRecorder(proc))
    assert updates.run_powershell("x", timeout=5)[0] == -1


def test_run_powershell_communicate_error_gives_empty_output(monkeypatch) -> None:
    proc = FakeProc()

    def broken() -> tuple[bytes, bytes]:
        proc.returncode = 5
        raise OSError("pipe")

    proc.communicate = broken  # type: ignore[method-assign]
    monkeypatch.setattr(subprocess, "Popen", PopenRecorder(proc))
    assert updates.run_powershell("x", timeout=5) == (5, "", "")


# ------------------------------------------------------------- start_background


def test_start_background_flags(monkeypatch) -> None:
    recorder = PopenRecorder(FakeProc())
    monkeypatch.setattr(subprocess, "Popen", recorder)
    monkeypatch.setattr(updates, "_BACKGROUND", [])
    assert updates.start_background(["a", "b"], {"WINSTALLER_WU_LOG": "x"}) == 4242
    args, kwargs = recorder.calls[0]
    assert args == ["a", "b"]
    assert kwargs["creationflags"] == (
        winget.CREATE_NO_WINDOW
        | updates._CREATE_NEW_PROCESS_GROUP
        | updates._CREATE_BREAKAWAY_FROM_JOB
    )
    assert kwargs["creationflags"] == 0x08000000 | 0x00000200 | 0x01000000
    for stream in ("stdin", "stdout", "stderr"):
        assert kwargs[stream] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    assert kwargs["cwd"] == str(plat.windows_system_dir())
    assert kwargs["env"]["WINSTALLER_WU_LOG"] == "x"
    assert len(updates._BACKGROUND) == 1


def test_start_background_retries_without_breakaway_only_on_access_denied(monkeypatch) -> None:
    denied = OSError(None, "Odmowa dostępu", None, 5)
    recorder = PopenRecorder(denied, FakeProc())
    monkeypatch.setattr(subprocess, "Popen", recorder)
    monkeypatch.setattr(updates, "_BACKGROUND", [])
    assert updates.start_background(["a"], {}) == 4242
    first, second = (kw["creationflags"] for _a, kw in recorder.calls)
    assert first & updates._CREATE_BREAKAWAY_FROM_JOB
    assert second == winget.CREATE_NO_WINDOW | updates._CREATE_NEW_PROCESS_GROUP


@pytest.mark.parametrize("error", [OSError(None, "x", None, 2), FileNotFoundError(2, "x")])
def test_start_background_other_errors_raise(monkeypatch, error: OSError) -> None:
    recorder = PopenRecorder(error, FakeProc())
    monkeypatch.setattr(subprocess, "Popen", recorder)
    with pytest.raises(OSError):
        updates.start_background(["a"], {})
    assert len(recorder.calls) == 1


def test_start_background_real_child_survives_detached(background, tmp_path) -> None:
    out = tmp_path / "child.json"
    held = tmp_path / "held.txt"
    fh = open(held, "w", encoding="utf-8")  # noqa: SIM115 - closed below
    os.set_inheritable(fh.fileno(), True)
    code = (
        "import json, os, sys, time\n"
        "info = {'pid': os.getpid(), 'cwd': os.getcwd(),"
        " 'log': os.environ.get('WINSTALLER_WU_LOG'),"
        " 'stdin': sys.stdin is None or sys.stdin.read()}\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write(json.dumps(info))\n"
        "time.sleep(60)\n"
    )
    try:
        pid = updates.start_background(
            [PYTHON, "-c", code, str(out)], {"WINSTALLER_WU_LOG": r"C:\Ścieżka 'x'\a.log"}
        )
    finally:
        fh.close()
    wait_for(out.exists)
    wait_for(lambda: out.stat().st_size > 0)
    info = json.loads(out.read_text(encoding="utf-8"))
    assert info["pid"] == pid
    assert Path(info["cwd"]) == plat.windows_system_dir()
    assert info["log"] == r"C:\Ścieżka 'x'\a.log"
    assert info["stdin"] in ("", True)
    assert process_alive(pid)
    # The inheritable handle of the parent was not passed on: the file can be deleted.
    held.unlink()
    assert background and background[0].pid == pid


# ------------------------------------------------------------- mutex


def test_mutex_exists_real_named_mutex() -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    name = f"Local\\Winstaller.Test.{os.getpid()}.{time.monotonic_ns()}"
    assert not updates.mutex_exists(name)
    handle = kernel32.CreateMutexW(None, 0, name)
    assert handle
    try:
        assert updates.mutex_exists(name)
    finally:
        kernel32.CloseHandle(handle)
    assert not updates.mutex_exists(name)


def test_mutex_access_denied_counts_as_existing(monkeypatch) -> None:
    fake = types.SimpleNamespace(
        OpenMutexW=types.SimpleNamespace(),
        CloseHandle=types.SimpleNamespace(),
    )
    calls: list[str] = []

    def open_mutex(access: int, inherit: int, name: str) -> int:
        calls.append(name)
        return 0

    fake.OpenMutexW = open_mutex  # type: ignore[assignment]
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: fake)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)
    assert updates.mutex_exists("Global\\X")
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 2)
    assert not updates.mutex_exists("Global\\X")
    assert calls == ["Global\\X", "Global\\X"]


def test_mutex_without_kernel32(monkeypatch) -> None:
    def broken(*args: object, **kwargs: object) -> object:
        raise OSError("no dll")

    monkeypatch.setattr(ctypes, "WinDLL", broken)
    assert not updates.mutex_exists("Global\\X")


def test_background_install_running_uses_the_script_mutex(monkeypatch) -> None:
    names: list[str] = []
    monkeypatch.setattr(updates, "mutex_exists", lambda name: names.append(name) or True)
    assert updates.background_install_running()
    assert names == [updates.MUTEX_NAME] == ["Global\\Winstaller.WindowsUpdate"]


# ------------------------------------------------------------- log folder


@pytest.fixture
def dirs(monkeypatch, tmp_path) -> types.SimpleNamespace:
    """``create_secure_dir`` is replaced by a plain mkdir (the real one, with its
    protected DACL, is covered in test_updates_hardening.py); ``dir_is_trusted`` says
    yes only for paths in ``trusted``."""
    ns = types.SimpleNamespace(
        program_data=tmp_path / "ProgramData",
        local=tmp_path / "Local",
        admin=True,
        trusted=set(),
        created=[],
        create_error=None,
    )
    ns.program_data.mkdir()
    ns.local.mkdir()
    monkeypatch.setattr(plat, "program_data_dir", lambda: ns.program_data)
    monkeypatch.setattr(plat, "local_app_data_dir", lambda: ns.local)
    monkeypatch.setattr(plat, "is_admin", lambda: ns.admin)

    def create(path: Path, sddl: str = updates.LOG_DIR_SDDL) -> bool:
        if ns.create_error is not None:
            raise ns.create_error
        try:
            os.mkdir(path)
        except FileExistsError:
            return False
        ns.created.append(path)
        return True

    monkeypatch.setattr(updates, "create_secure_dir", create)
    monkeypatch.setattr(
        updates,
        "dir_is_trusted",
        lambda path: path in ns.trusted and path.is_dir() and not updates._is_reparse_point(path),
    )
    return ns


def _is_fresh(folder: Path, program_data: Path) -> bool:
    return (
        folder.name == "logs"
        and folder.parent.parent == program_data
        and folder.parent.name.startswith("Winstaller-")
        and len(folder.parent.name) == len("Winstaller-") + 8
    )


def test_log_dir_admin_creates_secure_tree(dirs) -> None:
    folder = updates.log_dir()
    root = dirs.program_data / "Winstaller"
    assert folder == root / "logs" and folder.is_dir()
    assert dirs.created == [root, root / "logs"]
    assert list(folder.iterdir()) == []  # the write probe is removed


def test_log_dir_not_admin_uses_local_app_data_without_acl(dirs) -> None:
    dirs.admin = False
    assert updates.log_dir() == dirs.local / "Winstaller" / "logs"
    assert dirs.created == []
    assert not (dirs.program_data / "Winstaller").exists()


def test_log_dir_existing_trusted_tree_is_used(dirs) -> None:
    root = dirs.program_data / "Winstaller"
    (root / "logs").mkdir(parents=True)
    dirs.trusted = {root, root / "logs"}
    assert updates.log_dir() == root / "logs"
    assert dirs.created == []


def test_log_dir_untrusted_tree_is_not_repaired(dirs) -> None:
    root = dirs.program_data / "Winstaller"
    (root / "logs").mkdir(parents=True)
    dirs.trusted = {root}  # 'logs' fails the owner/DACL check
    folder = updates.log_dir()
    assert _is_fresh(folder, dirs.program_data)
    assert dirs.created == [folder.parent, folder]


def test_log_dir_file_in_place_gives_fresh_folder(dirs) -> None:
    (dirs.program_data / "Winstaller").write_text("plik, nie katalog")
    assert _is_fresh(updates.log_dir(), dirs.program_data)


def test_log_dir_refuses_junction_root(dirs, tmp_path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    _winapi.CreateJunction(str(target), str(dirs.program_data / "Winstaller"))
    dirs.trusted = {dirs.program_data / "Winstaller"}
    assert _is_fresh(updates.log_dir(), dirs.program_data)


def test_junction_root_target_is_not_touched(dirs, tmp_path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    _winapi.CreateJunction(str(target), str(dirs.program_data / "Winstaller"))
    updates.log_dir()
    assert list(target.iterdir()) == []


def test_log_dir_refuses_junction_logs(dirs, tmp_path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    root = dirs.program_data / "Winstaller"
    root.mkdir()
    _winapi.CreateJunction(str(target), str(root / "logs"))
    dirs.trusted = {root, root / "logs"}
    assert _is_fresh(updates.log_dir(), dirs.program_data)
    assert list(target.iterdir()) == []


def test_acl_hardening_refused_when_tree_contains_links(dirs, tmp_path) -> None:
    victim = tmp_path / "victim"
    victim.mkdir()
    root = dirs.program_data / "Winstaller"
    (root / "logs").mkdir(parents=True)
    _winapi.CreateJunction(str(victim), str(root / "planted"))
    folder = updates.log_dir()
    # Nothing is created inside (or repaired in) the pre-existing tree.
    assert not any(p == root or root in p.parents for p in dirs.created)
    assert root not in folder.parents and list(victim.iterdir()) == []


def test_log_dir_neither_works(dirs) -> None:
    (dirs.program_data / "Winstaller").write_text("x")
    dirs.create_error = OSError(5, "Odmowa dostępu")
    with pytest.raises(UpdatesError) as exc:
        updates.log_dir()
    assert exc.value.kind == "start"
    assert str(dirs.program_data) in exc.value.message


def test_log_dir_not_admin_refuses_links(dirs, tmp_path) -> None:
    dirs.admin = False
    target = tmp_path / "elsewhere"
    target.mkdir()
    _winapi.CreateJunction(str(target), str(dirs.local / "Winstaller"))
    with pytest.raises(UpdatesError) as exc:
        updates.log_dir()
    assert exc.value.kind == "start" and list(target.iterdir()) == []


def test_latest_log(tmp_path) -> None:
    assert updates.latest_log(tmp_path) is None
    assert updates.latest_log(tmp_path / "missing") is None
    for name in (
        "windows-update-2026.11.21-235959.log",
        "windows-update-2026.11.22-120000.log",
        "windows-update-2026.11.22-120000.json",
        "inny-2027.01.01-000000.log",
    ):
        (tmp_path / name).write_text("x")
    assert updates.latest_log(tmp_path) == tmp_path / "windows-update-2026.11.22-120000.log"


def test_latest_log_prefers_collision_suffix(tmp_path) -> None:
    for name in ("windows-update-2026.11.22-120000.log", "windows-update-2026.11.22-120000-2.log"):
        (tmp_path / name).write_text("x")
    assert updates.latest_log(tmp_path) == tmp_path / "windows-update-2026.11.22-120000-2.log"


# ------------------------------------------------------------- start_windows_update_install


@pytest.fixture
def start_env(monkeypatch, tmp_path) -> types.SimpleNamespace:
    folder = tmp_path / "logs"
    folder.mkdir()
    ns = types.SimpleNamespace(folder=folder, mutex=False, starts=[], start_error=None, live=set())
    monkeypatch.setattr(updates, "log_dir", lambda: ns.folder)
    monkeypatch.setattr(updates, "existing_log_dirs", lambda: [ns.folder])
    monkeypatch.setattr(updates, "is_live_powershell", lambda pid: pid in ns.live)
    monkeypatch.setattr(updates, "mutex_exists", lambda name: ns.mutex)

    def start(args: list[str], env: dict[str, str]) -> int:
        if ns.start_error is not None:
            raise ns.start_error
        ns.starts.append((list(args), dict(env)))
        return 4242

    monkeypatch.setattr(updates, "start_background", start)
    return ns


def test_start_install_happy_path(start_env) -> None:
    result = updates.start_windows_update_install(update_ids=[GUID_A.upper(), GUID_B], now=NOW)
    log = start_env.folder / f"windows-update-{STAMP}.log"
    status = start_env.folder / f"windows-update-{STAMP}.json"
    assert result == updates.BackgroundInstall(log_path=log, status_path=status, pid=4242)
    assert not result.already_running
    args, env = start_env.starts[0]
    assert env == {
        "WINSTALLER_WU_LOG": str(log),
        "WINSTALLER_WU_STATUS": str(status),
        "WINSTALLER_WU_IDS": f"{GUID_A},{GUID_B}",
        "WINSTALLER_WU_ALL": "0",
        "WINSTALLER_WU_SKIP_DRIVERS": "0",
        "WINSTALLER_WU_SKIP_DISPLAY_DRIVERS": "0",
        "WINSTALLER_WU_SKIP_INTERACTIVE": "0",
    }
    assert Path(env["WINSTALLER_WU_LOG"]).is_absolute()
    assert args == updates.powershell_args(update_scripts.WU_INSTALL_SCRIPT, hidden=True)
    script = decode_script(args)
    assert script == updates.compact_script(update_scripts.WU_INSTALL_SCRIPT)
    assert GUID_A not in script and str(start_env.folder) not in script
    raw = log.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf2026-11-22 12:00:00 ")
    assert raw.endswith(b"(2 pozycji).\r\n")
    assert "Winstaller uruchamia instalację" in raw.decode("utf-8-sig")
    assert not status.exists()  # created by the script


def test_start_install_skip_flags(start_env) -> None:
    updates.start_windows_update_install(
        update_ids=[GUID_A], skip_drivers=True, skip_display_drivers=True, skip_interactive=True
    )
    env = start_env.starts[0][1]
    assert env["WINSTALLER_WU_SKIP_DRIVERS"] == "1"
    assert env["WINSTALLER_WU_SKIP_DISPLAY_DRIVERS"] == "1"
    assert env["WINSTALLER_WU_SKIP_INTERACTIVE"] == "1"


def test_start_install_empty_list_is_refused(start_env) -> None:
    with pytest.raises(UpdatesError) as exc:
        updates.start_windows_update_install(now=NOW)
    assert exc.value.kind == "format"
    assert start_env.starts == [] and list(start_env.folder.iterdir()) == []


def test_start_install_all_needs_explicit_flag(start_env) -> None:
    result = updates.start_windows_update_install(install_all=True, now=NOW)
    env = start_env.starts[0][1]
    assert env["WINSTALLER_WU_IDS"] == "" and env["WINSTALLER_WU_ALL"] == "1"
    assert result.install_all
    log = start_env.folder / f"windows-update-{STAMP}.log"
    assert "(wszystkie pozycji)" in log.read_text(encoding="utf-8-sig")


def _running_status(folder: Path, stem: str, pid: int, state: str = "running") -> Path:
    status = folder / f"{stem}.json"
    status.write_text(
        json.dumps({"winstaller": "wu-install", "state": state, "pid": pid}), encoding="utf-8"
    )
    (folder / f"{stem}.log").write_text("log", encoding="utf-8")
    return status


def test_start_install_does_nothing_while_mutex_held(start_env) -> None:
    status = _running_status(start_env.folder, "windows-update-2026.11.20-080000", 777)
    start_env.live = {777}
    start_env.mutex = True
    result = updates.start_windows_update_install(update_ids=[GUID_A], now=NOW)
    assert result.already_running and result.pid == 777 and result.status_path == status
    assert result.log_path == start_env.folder / "windows-update-2026.11.20-080000.log"
    assert start_env.starts == []
    assert sorted(p.name for p in start_env.folder.iterdir()) == [
        "windows-update-2026.11.20-080000.json",
        "windows-update-2026.11.20-080000.log",
    ]


def test_unconfirmed_mutex_is_blocked_not_running(start_env) -> None:
    _running_status(start_env.folder, "windows-update-2026.11.20-080000", 777)  # pid dead
    _running_status(start_env.folder, "windows-update-2026.11.20-090000", 778, "finished")
    start_env.live = {778}
    start_env.mutex = True
    with pytest.raises(UpdatesError) as exc:
        updates.start_windows_update_install(update_ids=[GUID_A], now=NOW)
    assert exc.value.kind == "blocked" and "Inny proces blokuje" in exc.value.message
    assert start_env.starts == []


def test_running_install_without_logs(start_env) -> None:
    with pytest.raises(UpdatesError) as exc:
        updates.running_install()
    assert exc.value.kind == "blocked"


def test_running_install_without_log_folder(monkeypatch) -> None:
    monkeypatch.setattr(updates, "existing_log_dirs", lambda: [])

    def forbidden() -> Path:
        raise AssertionError("running_install must not create folders")

    monkeypatch.setattr(updates, "log_dir", forbidden)
    with pytest.raises(UpdatesError) as exc:
        updates.running_install()
    assert exc.value.kind == "blocked"


def test_reparse_check_of_missing_path(tmp_path) -> None:
    assert not updates._is_reparse_point(tmp_path / "missing")


def test_plain_tree_refuses_unwritable_folder(monkeypatch, tmp_path) -> None:
    def no_write(*args: object, **kwargs: object) -> object:
        raise PermissionError(13, "Odmowa dostępu")

    monkeypatch.setattr(updates, "open", no_write, raising=False)
    assert updates._plain_tree(tmp_path / "Winstaller") is None


def test_invalid_ids_fail_before_anything(start_env) -> None:
    with pytest.raises(UpdatesError):
        updates.start_windows_update_install(update_ids=[GUID_A, "'; calc; '"], now=NOW)
    assert start_env.starts == [] and list(start_env.folder.iterdir()) == []


@pytest.mark.parametrize(
    "existing,expected",
    [
        ([f"windows-update-{STAMP}.log"], f"windows-update-{STAMP}-2"),
        ([f"windows-update-{STAMP}.json"], f"windows-update-{STAMP}-2"),
        (
            [f"windows-update-{STAMP}.log", f"windows-update-{STAMP}-2.json"],
            f"windows-update-{STAMP}-3",
        ),
    ],
)
def test_name_collisions_in_the_same_second(start_env, existing, expected) -> None:
    for name in existing:
        (start_env.folder / name).write_text("x")
    result = updates.start_windows_update_install(update_ids=[GUID_A], now=NOW)
    assert result.log_path == start_env.folder / f"{expected}.log"
    assert result.status_path == start_env.folder / f"{expected}.json"
    assert (start_env.folder / f"windows-update-{STAMP}.log").exists() == (
        f"windows-update-{STAMP}.log" in existing
    )


def test_all_collision_names_taken(start_env) -> None:
    (start_env.folder / f"windows-update-{STAMP}.log").write_text("x")
    for n in range(2, 100):
        (start_env.folder / f"windows-update-{STAMP}-{n}.log").write_text("x")
    with pytest.raises(UpdatesError) as exc:
        updates.start_windows_update_install(update_ids=[GUID_A], now=NOW)
    assert exc.value.kind == "start" and start_env.starts == []


def test_existing_link_at_log_path_is_never_written_through(start_env, tmp_path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("nietknięty")
    target_dir = tmp_path / "victim-dir"
    target_dir.mkdir()
    _winapi.CreateJunction(str(target_dir), str(start_env.folder / f"windows-update-{STAMP}.log"))
    result = updates.start_windows_update_install(update_ids=[GUID_A], now=NOW)
    assert result.log_path.name == f"windows-update-{STAMP}-2.log"
    assert list(target_dir.iterdir()) == [] and victim.read_text() == "nietknięty"


@pytest.mark.parametrize(
    "error,fragment",
    [
        (OSError(None, "x", None, 87), "Nie udało się uruchomić instalacji aktualizacji w tle"),
        (FileNotFoundError(2, "brak"), "Nie znaleziono PowerShella"),
    ],
)
def test_start_failure_is_updates_error(start_env, error: OSError, fragment: str) -> None:
    start_env.start_error = error
    with pytest.raises(UpdatesError) as exc:
        updates.start_windows_update_install(update_ids=[GUID_A], now=NOW)
    assert exc.value.kind == "start" and fragment in exc.value.message


def test_powershell_missing_at_start(start_env, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(plat, "windows_system_dir", lambda: tmp_path / "none")
    with pytest.raises(UpdatesError) as exc:
        updates.start_windows_update_install(update_ids=[GUID_A], now=NOW)
    assert "Nie znaleziono PowerShella" in exc.value.message
    assert start_env.starts == []


def test_log_folder_failure_propagates(start_env, monkeypatch) -> None:
    def broken() -> Path:
        raise UpdatesError("brak katalogu", kind="start")

    monkeypatch.setattr(updates, "log_dir", broken)
    with pytest.raises(UpdatesError, match="brak katalogu"):
        updates.start_windows_update_install(update_ids=[GUID_A])
    assert start_env.starts == []


def test_log_creation_failure(start_env, tmp_path) -> None:
    start_env.folder = tmp_path / "missing"  # never created
    with pytest.raises(UpdatesError) as exc:
        updates.start_windows_update_install(update_ids=[GUID_A], now=NOW)
    assert exc.value.message.startswith("Nie udało się utworzyć logu")
    assert start_env.starts == []


def test_relative_log_folder_is_refused(start_env, monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    Path("rel").mkdir()
    start_env.folder = Path("rel")
    with pytest.raises(UpdatesError):
        updates.start_windows_update_install(update_ids=[GUID_A], now=NOW)


def test_paths_with_quotes_and_polish_characters_go_only_through_env(start_env, tmp_path) -> None:
    folder = tmp_path / "Jan Kowalski's $(calc) %TEMP% ąę;&"
    folder.mkdir()
    start_env.folder = folder
    result = updates.start_windows_update_install(update_ids=[GUID_A], now=NOW)
    args, env = start_env.starts[0]
    assert env["WINSTALLER_WU_LOG"] == str(result.log_path)
    assert result.log_path.exists()
    assert all(str(folder) not in a for a in args)


def test_concurrent_starts_start_one_process(start_env, monkeypatch) -> None:
    barrier = threading.Barrier(2)

    def mutex(name: str) -> bool:
        barrier.wait(5)
        return False

    monkeypatch.setattr(updates, "mutex_exists", mutex)
    results: list[object] = []

    def call(second: int) -> None:
        try:
            results.append(
                updates.start_windows_update_install(
                    update_ids=[GUID_A], now=NOW.replace(second=second)
                )
            )
        except UpdatesError as exc:
            results.append(exc)

    threads = [threading.Thread(target=call, args=(s,)) for s in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(results) == 2
    assert len(start_env.starts) == 1


# ------------------------------------------------------------- scan / store orchestration


@pytest.fixture
def ps_calls(monkeypatch) -> types.SimpleNamespace:
    ns = types.SimpleNamespace(calls=[], results=[], cli_calls=[], cli_results=[])

    def run(script: str, *, env=None, timeout: float, cancel_event=None):
        ns.calls.append({"script": script, "env": env, "timeout": timeout, "cancel": cancel_event})
        result = ns.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def cli(args, *, timeout=updates.STORE_CLI_TIMEOUT, cancel_event=None):
        ns.cli_calls.append({"args": list(args), "timeout": timeout, "cancel": cancel_event})
        result = ns.cli_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(updates, "run_powershell", run)
    monkeypatch.setattr(updates, "run_store_cli", cli)
    return ns


def test_scan_runs_the_scan_script(ps_calls) -> None:
    cancel = threading.Event()
    ps_calls.results = [(0, scan_line(), "")]
    scan = updates.scan_windows_updates(cancel_event=cancel)
    assert len(scan.updates) == 5
    call = ps_calls.calls[0]
    assert call["script"] is update_scripts.WU_SCAN_SCRIPT
    assert call["env"] is None and call["timeout"] == updates.SCAN_TIMEOUT == 180.0
    assert call["cancel"] is cancel


def test_scan_zero_updates(ps_calls) -> None:
    ps_calls.results = [(0, scan_line(updates=[]), "")]
    assert updates.scan_windows_updates().updates == ()


@pytest.mark.parametrize(
    "error",
    [UpdatesCancelled(), UpdatesError("limit", kind="timeout"), UpdatesError("x", kind="start")],
)
def test_scan_propagates_run_errors(ps_calls, error: UpdatesError) -> None:
    ps_calls.results = [error]
    with pytest.raises(type(error)) as exc:
        updates.scan_windows_updates(timeout=5)
    assert exc.value is error


def test_scan_failure_json(ps_calls) -> None:
    ps_calls.results = [(1, scan_line(ok=False, error={"hresult": "0x8024402C"}), "")]
    with pytest.raises(UpdatesError) as exc:
        updates.scan_windows_updates()
    assert exc.value.kind == "network"


def test_list_store_queue_is_read_only_mode(ps_calls) -> None:
    ps_calls.results = [(0, store_line(mode="list"), "")]
    updates.list_store_queue(timeout=10)
    call = ps_calls.calls[0]
    assert call["script"] is update_scripts.STORE_SCRIPT
    assert call["env"] == {"WINSTALLER_STORE_MODE": "list", "WINSTALLER_STORE_TIMEOUT": "10"}
    assert call["timeout"] == 40
    assert ps_calls.cli_calls == []


def test_store_primary_ok(ps_calls) -> None:
    found = [{"productId": "9WZDNCRFJBMP", "packageFamilyName": "Microsoft.WindowsStore_8w"}]
    ps_calls.results = [(0, store_line(found=found), "")]
    notes: list[str] = []
    result = updates.update_store_apps(log=notes.append)
    assert result.method == "AppInstallManager" and result.count == 1
    assert ps_calls.calls[0]["env"][updates.ENV_STORE_MODE] == "update"
    assert ps_calls.calls[0]["timeout"] == updates.STORE_TIMEOUT + 30
    assert ps_calls.cli_calls == [] and notes == []


def test_store_policy_block_has_no_fallback(ps_calls) -> None:
    ps_calls.results = [(1, store_line(ok=False, blockedByPolicy=True), "")]
    with pytest.raises(UpdatesError) as exc:
        updates.update_store_apps()
    assert exc.value.kind == "policy"
    assert updates.STORE_MANUAL_HINT in exc.value.message
    assert ps_calls.cli_calls == []


@pytest.mark.parametrize(
    "primary",
    [
        (1, store_line(ok=False, error={"hresult": "0x80070005", "message": "x"}), ""),
        (0, "", ""),
        UpdatesError("Nie udało się uruchomić PowerShella", kind="start"),
    ],
)
def test_store_fallback_to_cli(ps_calls, primary: object) -> None:
    cancel = threading.Event()
    ps_calls.results = [primary]
    ps_calls.cli_results = [(0, "Checking for updates…\r\nNo updates available.\r\n")]
    notes: list[str] = []
    result = updates.update_store_apps(log=notes.append, cancel_event=cancel)
    assert result.method == "store-cli" and result.count == 0
    assert ps_calls.cli_calls == [
        {"args": ["updates", "--apply"], "timeout": updates.STORE_CLI_TIMEOUT, "cancel": cancel}
    ]
    assert len(notes) == 1
    assert notes[0].startswith("Usługa Microsoft Store nie przyjęła zlecenia (")
    assert notes[0].endswith("Próbuję store.exe…")


@pytest.mark.parametrize(
    "primary",
    [
        UpdatesError("Przekroczono limit czasu", kind="timeout"),
        (1, store_line(ok=False, error={"message": "x", "timeout": True}), ""),
    ],
)
def test_store_timeout_never_falls_back(ps_calls, primary: object) -> None:
    ps_calls.results = [primary]
    with pytest.raises(UpdatesError) as exc:
        updates.update_store_apps()
    assert exc.value.kind == "timeout" and "nie udało się tego potwierdzić" in exc.value.message
    assert ps_calls.cli_calls == []


def test_store_both_fail(ps_calls) -> None:
    ps_calls.results = [(1, store_line(ok=False, error={"message": "Błąd A"}), "")]
    ps_calls.cli_results = [UpdatesError("Nie znaleziono polecenia store.exe.", kind="start")]
    with pytest.raises(UpdatesError) as exc:
        updates.update_store_apps()
    err = exc.value
    assert err.kind == "store"
    assert "Błąd A" in err.message and "Nie znaleziono polecenia store.exe" in err.message
    assert err.message.endswith(updates.STORE_MANUAL_HINT)


def test_store_cli_output_failure_also_gives_manual_hint(ps_calls) -> None:
    ps_calls.results = [(0, "", "")]
    ps_calls.cli_results = [
        (
            0,
            (Path(__file__).parents[1] / "data/updates/store-updates.txt")
            .read_bytes()
            .decode("utf-8"),
        )
    ]
    with pytest.raises(UpdatesError) as exc:
        updates.update_store_apps()
    assert updates.STORE_MANUAL_HINT in exc.value.message


def test_store_cancel_in_primary_skips_fallback(ps_calls) -> None:
    ps_calls.results = [UpdatesCancelled()]
    with pytest.raises(UpdatesCancelled):
        updates.update_store_apps()
    assert ps_calls.cli_calls == []


def test_store_cancel_in_fallback(ps_calls) -> None:
    ps_calls.results = [(0, "", "")]
    ps_calls.cli_results = [UpdatesCancelled()]
    with pytest.raises(UpdatesCancelled):
        updates.update_store_apps()


# ------------------------------------------------------------- run_store_cli


@pytest.fixture
def store_alias(monkeypatch, tmp_path) -> Callable[[Path], None]:
    """Make ``path`` look like an App Execution Alias to ``run_store_cli``."""

    def install(path: Path) -> None:
        monkeypatch.setattr(updates, "store_cli_path", lambda: path)
        real_lstat = os.lstat

        def lstat(target: Any, *args: Any, **kwargs: Any) -> Any:
            result = real_lstat(target, *args, **kwargs)
            if os.path.normcase(os.fspath(target)) == os.path.normcase(str(path)):
                return types.SimpleNamespace(
                    st_reparse_tag=updates._IO_REPARSE_TAG_APPEXECLINK,
                    st_file_attributes=result.st_file_attributes,
                )
            return result

        monkeypatch.setattr(os, "lstat", lstat)

    return install


def test_store_cli_path_is_the_user_alias(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(plat, "local_app_data_dir", lambda: tmp_path)
    assert updates.store_cli_path() == tmp_path / "Microsoft" / "WindowsApps" / "store.exe"


def test_store_cli_missing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(updates, "store_cli_path", lambda: tmp_path / "store.exe")
    with pytest.raises(UpdatesError) as exc:
        updates.run_store_cli(["updates", "--apply"])
    assert exc.value.kind == "start"


def test_store_cli_regular_file_is_not_run(monkeypatch, tmp_path) -> None:
    fake = tmp_path / "store.exe"
    fake.write_bytes(b"MZ")
    recorder = PopenRecorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)
    monkeypatch.setattr(updates, "store_cli_path", lambda: fake)
    with pytest.raises(UpdatesError) as exc:
        updates.run_store_cli(["updates", "--apply"])
    assert "nie jest aliasem" in exc.value.message and recorder.calls == []


def test_store_cli_symlink_is_not_run(monkeypatch, tmp_path) -> None:
    target = tmp_path / "dir"
    target.mkdir()
    link = tmp_path / "store.exe"
    _winapi.CreateJunction(str(target), str(link))
    recorder = PopenRecorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)
    monkeypatch.setattr(updates, "store_cli_path", lambda: link)
    with pytest.raises(UpdatesError):
        updates.run_store_cli(["updates", "--apply"])
    assert recorder.calls == []


def test_store_cli_popen_arguments(monkeypatch, tmp_path, store_alias) -> None:
    exe = tmp_path / "store.exe"
    exe.write_bytes(b"MZ")
    store_alias(exe)
    recorder = PopenRecorder(FakeProc(out=b"Brak aktualizacji\r\n"))
    monkeypatch.setattr(subprocess, "Popen", recorder)
    assert updates.run_store_cli(["updates", "--apply"]) == (0, "Brak aktualizacji\r\n")
    args, kwargs = recorder.calls[0]
    assert args == [str(exe), "updates", "--apply"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE and kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["creationflags"] == winget.CREATE_NO_WINDOW
    assert kwargs["env"]["NO_COLOR"] == "1"


def test_store_cli_start_error(monkeypatch, tmp_path, store_alias) -> None:
    exe = tmp_path / "store.exe"
    exe.write_bytes(b"MZ")
    store_alias(exe)
    monkeypatch.setattr(subprocess, "Popen", PopenRecorder(OSError("Brak dostępu")))
    with pytest.raises(UpdatesError) as exc:
        updates.run_store_cli(["updates", "--apply"])
    assert exc.value.kind == "start"


def test_store_cli_real_child_output(store_alias) -> None:
    store_alias(Path(PYTHON))
    code = (
        "import os, sys; "
        "sys.stdout.write('NO_COLOR=' + os.environ['NO_COLOR'] + repr(sys.stdin.read()))"
    )
    rc, out = updates.run_store_cli(["-c", code], timeout=30)
    assert (rc, out) == (0, "NO_COLOR=1''")


def test_store_cli_real_child_timeout_and_cancel(store_alias) -> None:
    store_alias(Path(PYTHON))
    with pytest.raises(UpdatesError) as exc:
        updates.run_store_cli(["-c", "import time; time.sleep(60)"], timeout=1)
    assert exc.value.kind == "timeout" and "nie zakończył się w 1 s" in exc.value.message
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    with pytest.raises(UpdatesCancelled):
        updates.run_store_cli(
            ["-c", "import time; time.sleep(60)"], timeout=60, cancel_event=cancel
        )


def test_store_cli_communicate_error(monkeypatch, tmp_path, store_alias) -> None:
    exe = tmp_path / "store.exe"
    exe.write_bytes(b"MZ")
    store_alias(exe)
    proc = FakeProc()

    def broken() -> tuple[bytes, bytes]:
        raise ValueError("closed")

    proc.communicate = broken  # type: ignore[method-assign]
    monkeypatch.setattr(subprocess, "Popen", PopenRecorder(proc))
    assert updates.run_store_cli(["updates"]) == (-1, "")
