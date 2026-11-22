"""Reading the background installation: ``read_shared``, ``read_install_progress``
(liveness, ``stale`` + result from the log), ``find_background_install``,
``system_boot_time``. Files only in ``tmp_path``; the only child process is this
Python interpreter (``sys.executable -c``) replacing a file in a loop."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.updates_support import DATA
from winstaller.core import updates
from winstaller.core.updates import (
    InstallProgress,
    UpdatesError,
    describe_progress,
    find_background_install,
    read_install_progress,
    read_shared,
)

REAL = "windows-update-2026.11.22-155534"
PROGRESS = DATA / "progress"
NOW = datetime.now().astimezone()


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates, "READ_RETRY_DELAY", 0.0)


@pytest.fixture
def procs(monkeypatch: pytest.MonkeyPatch) -> dict[int, str]:
    """PID -> image name of "running" processes; boot two days ago."""
    table: dict[int, str] = {}
    monkeypatch.setattr(updates, "process_image", lambda pid: table.get(pid))
    monkeypatch.setattr(updates, "system_boot_time", lambda: NOW - timedelta(days=2))
    return table


def copy_real(folder: Path, *, with_log: bool = True, stem: str = REAL) -> Path:
    status = folder / f"{stem}.json"
    status.write_bytes((DATA / f"{REAL}.json").read_bytes())
    if with_log:
        (folder / f"{stem}.log").write_bytes((DATA / f"{REAL}.log").read_bytes())
    return status


def write_status(path: Path, **fields: Any) -> Path:
    data = json.loads((PROGRESS / "harness-06-installing.json").read_text(encoding="utf-8"))
    data.update(started=(NOW - timedelta(hours=1)).isoformat(), pid=4321)
    data.update(fields)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def write_log(path: Path, *bodies: str) -> None:
    lines = [f"2026-11-22 16:00:{i:02d} {b}" for i, b in enumerate(bodies)]
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8-sig"))


def entry(title: str, status: str = "pending", **fields: Any) -> dict[str, Any]:
    return {
        "id": "",
        "revision": 1,
        "title": title,
        "status": status,
        "hresult": None,
        "rebootRequired": False,
        **fields,
    }


# ------------------------------------------------------------------ read_shared


def test_read_shared_reads_and_limits(tmp_path: Path) -> None:
    f = tmp_path / "a.json"
    f.write_bytes(b"0123456789")
    assert read_shared(f, 100) == b"0123456789"
    assert read_shared(f, 4) == b"01234"  # limit + 1: the caller detects "too big"
    assert read_shared(f, 0) == b"0"


def test_read_shared_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        read_shared(tmp_path / "missing.json", 10)
    with pytest.raises(FileNotFoundError):
        read_shared(tmp_path / "no" / "dir" / "x.json", 10)
    with pytest.raises(OSError):
        read_shared(tmp_path, 10)  # a directory


def replace_file(target: Path, source: Path) -> int:
    """``ReplaceFileW`` (what .NET ``File.Replace`` calls in the script); 0 or an error."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = kernel32.ReplaceFileW
    fn.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    fn.restype = ctypes.c_int
    return 0 if fn(str(target), str(source), None, 0, None, None) else ctypes.get_last_error()


def test_read_shared_does_not_block_a_replace(tmp_path: Path, monkeypatch) -> None:
    """While ``read_shared`` holds the status file, the script must still be able to
    ``File.Replace`` it (the second bug of 2026-11-22). A plain ``open()`` reader blocks
    the same call (control: the check is sensitive)."""
    target = tmp_path / "s.json"
    target.write_bytes(b"old")
    new = tmp_path / "s.json.tmp"
    real_fdopen = os.fdopen
    results: list[int] = []

    def fdopen_then_replace(fd: int, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        new.write_bytes(b"new")
        results.append(replace_file(target, new))  # our handle is open right now
        return real_fdopen(fd, *args, **kwargs)

    monkeypatch.setattr(updates.os, "fdopen", fdopen_then_replace)
    data = read_shared(target, 10)
    monkeypatch.undo()
    assert results == [0], f"ReplaceFileW failed with error {results}"
    assert data == b"old" and target.read_bytes() == b"new"

    new.write_bytes(b"newer")
    with open(target, "rb"):
        assert replace_file(target, new) != 0
    assert target.read_bytes() == b"new"


_WRITER = textwrap.dedent(
    """
    import ctypes, json, os, sys
    target, count = sys.argv[1], int(sys.argv[2])
    replace = ctypes.WinDLL("kernel32").ReplaceFileW
    replace.argtypes = [ctypes.c_wchar_p] * 3 + [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
    failures = 0
    big = "x" * 20000
    for n in range(count):
        data = {"winstaller": "wu-install", "state": "running", "phase": "installing",
                "pid": 4321, "total": 6, "current": n % 6 + 1, "pad": big,
                "updates": [{"title": f"U{i}", "status": "pending"} for i in range(6)]}
        tmp = target + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data))
        # Exactly like Save-Status: File.Replace (ReplaceFileW), up to 20 attempts.
        for attempt in range(20):
            if replace(target, tmp, None, 0, None, None):
                break
            if attempt == 19:
                failures += 1
                os.remove(tmp)
    print(failures)
    """
)


def test_concurrent_writer_is_never_blocked_and_reader_never_breaks(tmp_path: Path, procs) -> None:
    status = tmp_path / "windows-update-2026.11.22-160000.json"
    write_status(status)
    procs[4321] = "powershell.exe"
    child = subprocess.Popen(
        [sys.executable, "-c", _WRITER, str(status), "80"],
        stdout=subprocess.PIPE,
        text=True,
    )
    reads = errors = 0
    try:
        while child.poll() is None:
            try:
                p = read_install_progress(status)
            except UpdatesError:
                errors += 1
                continue
            reads += 1
            assert p.state == "running" and p.alive and p.phase == "installing"
            assert p.total in (3, 6) and 1 <= p.current <= p.total
    finally:
        out, _ = child.communicate(timeout=30)
    assert child.returncode == 0
    assert int(out.strip()) == 0, "the reader blocked the writer's rename"
    assert reads > 0
    assert errors <= reads  # only after 5 failed attempts in a row; never anything else


def test_read_install_progress_never_raises_other_errors(tmp_path: Path, procs) -> None:
    status = tmp_path / "s.json"
    for content in (
        b"",
        b"\xff\xfe{\x00",
        "{}".encode("utf-16"),
        b"{" * 10,
        b"[]",
        b'\xef\xbb\xbf{"winstaller": "wu-install", "state": "x"}',
    ):
        status.write_bytes(content)
        with pytest.raises(UpdatesError):
            read_install_progress(status)
    status.write_bytes(b" " * (updates.MAX_STATUS_BYTES + 1))
    with pytest.raises(UpdatesError, match="zbyt duży"):
        read_install_progress(status)
    with pytest.raises(UpdatesError, match="Nie znaleziono"):
        read_install_progress(tmp_path / "gone.json")
    with pytest.raises(UpdatesError):
        read_install_progress(tmp_path)


def test_utf8_bom_status_is_read(tmp_path: Path, procs) -> None:
    status = tmp_path / "s.json"
    status.write_bytes(b"\xef\xbb\xbf" + (PROGRESS / "harness-08-finished.json").read_bytes())
    assert read_install_progress(status).state == "finished"


# ------------------------------------------------------------ liveness / stale


def test_finished_status_needs_no_process(tmp_path: Path, monkeypatch) -> None:
    def boom(_pid: int) -> str:
        raise AssertionError("process checked for a finished status")

    monkeypatch.setattr(updates, "process_image", boom)
    status = tmp_path / "s.json"
    status.write_bytes((PROGRESS / "harness-08-finished.json").read_bytes())
    p = read_install_progress(status)
    assert p.state == "finished" and p.exit_code == 2 and not p.alive
    assert p.log_path == tmp_path / "s.log" and p.status_path == status


def test_running_and_alive(tmp_path: Path, procs) -> None:
    procs[4321] = "powershell.exe"
    p = read_install_progress(write_status(tmp_path / "s.json"))
    assert (p.state, p.alive, p.done) == ("running", True, False)


@pytest.mark.parametrize("image", [None, "", "python.exe", "pwsh.exe", "notepad.exe"])
def test_running_but_process_is_not_ours(tmp_path: Path, procs, image: str | None) -> None:
    if image is not None:
        procs[4321] = image
    p = read_install_progress(write_status(tmp_path / "s.json"))
    assert p.state == "stale" and not p.alive and p.done and not p.succeeded
    assert describe_progress(p)[0] == "Instalacja przerwana"


def test_running_process_from_before_the_last_boot(tmp_path: Path, procs, monkeypatch) -> None:
    procs[4321] = "powershell.exe"
    monkeypatch.setattr(updates, "system_boot_time", lambda: NOW - timedelta(minutes=10))
    p = read_install_progress(write_status(tmp_path / "s.json"))
    assert p.state == "stale"


def test_process_check_os_error_is_stale(tmp_path: Path, monkeypatch, procs) -> None:
    def fail(_pid: int) -> str:
        raise OSError("denied")

    monkeypatch.setattr(updates, "process_image", fail)
    assert read_install_progress(write_status(tmp_path / "s.json")).state == "stale"


def test_running_without_pid_is_unknown(tmp_path: Path, procs) -> None:
    p = read_install_progress(write_status(tmp_path / "s.json", pid=None))
    # Decision 3: no PID and no result in the log -> "unknown" as a FINAL state.
    assert p.state == "unknown" and p.done and not p.succeeded


def test_reused_pid_of_a_newer_powershell_is_not_alive(tmp_path: Path, procs) -> None:
    me = os.getpid()  # this process was created minutes ago, long after 'started'
    procs[me] = "powershell.exe"
    status = write_status(
        tmp_path / "s.json", pid=me, started=(NOW - timedelta(days=1)).isoformat()
    )
    assert read_install_progress(status).state != "running"


def test_real_run_stale_status_recovered_from_log(tmp_path: Path, procs) -> None:
    """The hardware run of 2026-11-22: status stuck on 'running' with an empty list,
    process gone, full log -> 6 OK, restart, code 0."""
    p = read_install_progress(copy_real(tmp_path))
    assert (p.state, p.phase, p.exit_code, p.from_log) == ("stale", "finished", 0, True)
    assert (p.ok_count, p.failed_count, p.count) == (6, 0, 6)
    assert p.reboot_required and p.succeeded
    assert p.finished is not None and p.finished.strftime("%H:%M") == "16:04"
    assert describe_progress(p) == (
        "Zakończono — wymagany restart",
        "Wynik odczytany z logu (proces nie zapisał statusu). Zainstalowano 6 z 6. Uruchom "
        "ponownie komputer, aby dokończyć instalację.",
    )


def test_real_run_without_log_is_interrupted(tmp_path: Path, procs) -> None:
    p = read_install_progress(copy_real(tmp_path, with_log=False))
    assert (p.state, p.exit_code, p.from_log) == ("stale", None, False)
    assert describe_progress(p)[0] == "Instalacja przerwana" and not p.succeeded


def test_log_of_another_pid_is_ignored(tmp_path: Path, procs) -> None:
    status = copy_real(tmp_path)
    log = status.with_suffix(".log")
    log.write_bytes(log.read_bytes().replace(b"(PID 9968)", b"(PID 1111)"))
    p = read_install_progress(status)
    assert p.state == "stale" and p.exit_code is None and not p.from_log


def test_log_without_end_is_interrupted_with_the_item_in_progress(tmp_path: Path, procs) -> None:
    status = write_status(
        tmp_path / "s.json",
        updates=[entry("A", "succeeded"), entry("B", "installing"), entry("C")],
    )
    write_log(
        status.with_suffix(".log"),
        "Start instalacji aktualizacji w tle (PID 4321).",
        "Instalacja: A",
        "-> OK (0x00000000)",
        "Instalacja: B",
    )
    p = read_install_progress(status)
    assert p.state == "stale" and p.exit_code is None and p.from_log
    assert [i.status for i in p.items] == ["succeeded", "installing", "pending"]
    assert (p.ok_count, p.failed_count, p.skipped_count) == (1, 1, 1)
    assert describe_progress(p)[0] == "Instalacja przerwana"


def test_log_with_errors(tmp_path: Path, procs) -> None:
    status = write_status(tmp_path / "s.json", total=2, updates=[entry("A"), entry("B")])
    write_log(
        status.with_suffix(".log"),
        "Start instalacji aktualizacji w tle (PID 4321).",
        "Instalacja: A",
        "-> błąd (0x80240022)",
        "Instalacja: B",
        "-> OK (0x00000000)",
        "Koniec (kod 2).",
    )
    p = read_install_progress(status)
    assert (p.exit_code, p.ok_count, p.failed_count) == (2, 1, 1) and not p.succeeded
    assert describe_progress(p)[0] == "Zakończono z błędami (1 z 2 nieudane)"


def test_status_says_two_of_six_log_says_done(tmp_path: Path, procs) -> None:
    titles = [f"KB{i}" for i in range(6)]
    status = write_status(
        tmp_path / "s.json",
        total=6,
        current=3,
        updates=[
            entry(t, s)
            for t, s in zip(
                titles, ["succeeded"] * 2 + ["installing"] + ["downloaded"] * 3, strict=True
            )
        ],
    )
    bodies = ["Start instalacji aktualizacji w tle (PID 4321)."]
    for t in titles:
        bodies += [f"Instalacja: {t}", "-> OK (0x00000000)"]
    write_log(status.with_suffix(".log"), *bodies, "Koniec (kod 0).")
    p = read_install_progress(status)
    assert (p.ok_count, p.failed_count, p.skipped_count) == (6, 0, 0)
    assert p.succeeded and p.phase == "finished"


def test_duplicate_titles_keep_their_own_results(tmp_path: Path, procs) -> None:
    status = write_status(
        tmp_path / "s.json",
        updates=[entry("Intel - System - 1.2.3"), entry("Intel - System - 1.2.3")],
    )
    write_log(
        status.with_suffix(".log"),
        "Start instalacji aktualizacji w tle (PID 4321).",
        "Instalacja: Intel - System - 1.2.3",
        "-> błąd (0x80240022)",
        "Instalacja: Intel - System - 1.2.3",
        "-> OK (0x00000000)",
        "Koniec (kod 2).",
    )
    p = read_install_progress(status)
    assert p.failed_count == 1 and p.ok_count == 1


def test_reboot_from_log_or_status(tmp_path: Path, procs) -> None:
    status = write_status(tmp_path / "s.json", updates=[entry("A")], rebootRequired=True)
    write_log(status.with_suffix(".log"), "Instalacja: A", "-> OK (0x00000000)", "Koniec (kod 0).")
    assert read_install_progress(status).reboot_required
    status = write_status(tmp_path / "t.json", updates=[entry("A")], rebootRequired=False)
    write_log(
        status.with_suffix(".log"),
        "Instalacja: A",
        "-> OK (0x00000000), wymaga restartu",
        "Koniec (kod 0).",
    )
    p = read_install_progress(status)
    assert p.reboot_required and "Uruchom ponownie" in describe_progress(p)[1]


def test_garbage_log_falls_back_to_interrupted(tmp_path: Path, procs) -> None:
    status = write_status(tmp_path / "s.json")
    status.with_suffix(".log").write_bytes("zażółć".encode("utf-16"))
    p = read_install_progress(status)
    assert p.state == "stale" and not p.from_log
    status.with_suffix(".log").write_bytes(b"x" * (updates.MAX_LOG_BYTES + 1))
    assert read_install_progress(status).state == "stale"


def test_status_disappears_while_followed(tmp_path: Path, procs) -> None:
    procs[4321] = "powershell.exe"
    status = write_status(tmp_path / "s.json")
    assert read_install_progress(status).state == "running"
    status.unlink()
    with pytest.raises(UpdatesError):
        read_install_progress(status)


def test_alive_but_stuck_for_hours_is_still_running(tmp_path: Path, procs) -> None:
    """Documents the current behaviour (design question in the report): a live process
    whose last status write is 3 h old is plain 'running', nothing flags it."""
    procs[4321] = "powershell.exe"
    old = (NOW - timedelta(hours=3)).isoformat()
    p = read_install_progress(write_status(tmp_path / "s.json", updated=old, stepStarted=old))
    assert p.state == "running" and p.alive
    assert describe_progress(p)[0] == "Instalacja 1 z 3"


def test_system_boot_time_is_in_the_past() -> None:
    boot = updates.system_boot_time()
    now = datetime.now().astimezone()
    assert boot.tzinfo is not None
    assert datetime(2000, 1, 1).astimezone() < boot <= now


# ------------------------------------------------------- find_background_install


@pytest.fixture
def log_dirs(tmp_path: Path, monkeypatch, procs) -> list[Path]:
    dirs = [tmp_path / "pd" / "Winstaller" / "logs", tmp_path / "pd" / "Winstaller-ab" / "logs"]
    for d in dirs:
        d.mkdir(parents=True)
    monkeypatch.setattr(updates, "existing_log_dirs", lambda: list(dirs))
    return dirs


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")
    }


def finished(path: Path, code: int = 0) -> Path:
    data = json.loads((PROGRESS / "harness-08-finished.json").read_text(encoding="utf-8"))
    data["exitCode"] = code
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_find_none(log_dirs: list[Path], monkeypatch) -> None:
    assert find_background_install() is None
    monkeypatch.setattr(updates, "existing_log_dirs", lambda: [])
    assert find_background_install() is None


def test_find_newest_by_name_not_mtime(log_dirs: list[Path], tmp_path: Path) -> None:
    a, b = log_dirs
    newest = finished(a / "windows-update-2026.11.22-160000.json", 2)
    older = finished(b / "windows-update-2026.11.22-150000.json", 0)
    os.utime(newest, (time.time() - 3600, time.time() - 3600))
    os.utime(older, None)
    (a / "windows-update-2026.11.22-170000.json.tmp").write_text("{}", encoding="utf-8")
    (a / "windows-update-2026.11.22-180000.log").write_text("", encoding="utf-8")
    before = snapshot(tmp_path)
    p = find_background_install()
    assert isinstance(p, InstallProgress) and p.status_path == newest and p.exit_code == 2
    assert snapshot(tmp_path) == before  # creates and changes nothing


def test_find_collision_suffix_is_newer(log_dirs: list[Path]) -> None:
    a, _ = log_dirs
    finished(a / "windows-update-2026.11.22-160000.json")
    second = finished(a / "windows-update-2026.11.22-160000-2.json", 2)
    assert find_background_install().status_path == second


def test_find_skips_foreign_and_unreadable(log_dirs: list[Path]) -> None:
    a, b = log_dirs
    good = finished(b / "windows-update-2026.11.22-120000.json")
    (a / "windows-update-2026.11.22-160000.json").write_text(
        '{"winstaller": "other", "state": "finished"}', encoding="utf-8"
    )
    (a / "windows-update-zzz.json").write_text("{}", encoding="utf-8")
    assert find_background_install().status_path == good
    # Decision 14: an unreadable newest file of ours is "unknown", never the older result.
    broken = a / "windows-update-2026.11.22-150000.json"
    broken.write_text("{trunc", encoding="utf-8")
    p = find_background_install()
    assert p.status_path == broken and p.state == "unknown" and p.done


def test_find_only_the_newest_five(log_dirs: list[Path]) -> None:
    a, _ = log_dirs
    finished(a / "windows-update-2026.11.22-100000.json")
    for n in range(5):
        (a / f"windows-update-2026.11.22-16000{n}.json").write_text("x", encoding="utf-8")
    # Decision 14: the unreadable newest one is reported as "unknown" (not the old result).
    p = find_background_install()
    assert p is not None and p.state == "unknown"
    assert p.status_path == a / "windows-update-2026.11.22-160004.json"


def test_find_ignores_untrusted_folders(tmp_path: Path, monkeypatch, procs) -> None:
    from winstaller.core import platform as plat

    pd = tmp_path / "ProgramData"
    trusted = pd / "Winstaller" / "logs"
    untrusted = pd / "Winstaller-evil" / "logs"
    trusted.mkdir(parents=True)
    untrusted.mkdir(parents=True)
    monkeypatch.setattr(plat, "program_data_dir", lambda: pd)
    monkeypatch.setattr(plat, "is_admin", lambda: True)
    monkeypatch.setattr(updates, "dir_is_trusted", lambda p: p in (pd / "Winstaller", trusted))
    finished(trusted / "windows-update-2026.11.22-100000.json", 0)
    finished(untrusted / "windows-update-2026.11.22-200000.json", 2)
    p = find_background_install()
    assert p is not None and p.status_path is not None and p.status_path.parent == trusted


def test_find_running_and_dead_in_order(log_dirs: list[Path], procs) -> None:
    a, _ = log_dirs
    write_status(a / "windows-update-2026.11.22-160000.json", pid=777)
    p = find_background_install()
    assert p is not None and p.state == "stale"  # 777 is not running
    procs[777] = "powershell.exe"
    assert find_background_install().state == "running"
