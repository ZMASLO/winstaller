"""Fixes after the independent review of the background-install progress (points 1, 4,
11, 13, 14, 18 of the review): PID reuse, the ``[i]`` log marker, statuses closed by
the script's ``finally``, a status lagging behind the log, private paths in fixtures.
Files only in ``tmp_path``; nothing runs PowerShell."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.unit.test_install_script_progress import block_after, loop_bodies
from tests.updates_support import DATA
from winstaller.core import updates
from winstaller.core.update_scripts import WU_INSTALL_SCRIPT
from winstaller.core.updates import (
    InstallItem,
    InstallProgress,
    describe_progress,
    parse_install_log,
    parse_install_status,
    read_install_progress,
)

NOW = datetime.now().astimezone()
PROGRESS = DATA / "progress"


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates, "READ_RETRY_DELAY", 0.0)


@pytest.fixture
def procs(monkeypatch: pytest.MonkeyPatch) -> dict[int, str]:
    table: dict[int, str] = {}
    monkeypatch.setattr(updates, "process_image", lambda pid: table.get(pid))
    monkeypatch.setattr(updates, "system_boot_time", lambda: NOW - timedelta(days=2))
    return table


def log_text(*bodies: str, start: datetime) -> str:
    lines = [f"{start + timedelta(seconds=n):%Y-%m-%d %H:%M:%S} {b}" for n, b in enumerate(bodies)]
    return "\r\n".join(lines) + "\r\n"


def entry(title: str, status: str = "pending") -> dict[str, Any]:
    return {"id": "", "revision": 1, "title": title, "status": status, "hresult": None}


def write_status(path: Path, **fields: Any) -> Path:
    data = json.loads((PROGRESS / "harness-06-installing.json").read_text(encoding="utf-8"))
    data.update(started=(NOW - timedelta(hours=1)).isoformat(), pid=4321)
    data.update(fields)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# ------------------------------------------------------------------ 1. PID reuse


def test_installer_process_window(monkeypatch: pytest.MonkeyPatch) -> None:
    started = NOW - timedelta(hours=1)
    monkeypatch.setattr(updates, "is_live_powershell", lambda pid: True)
    monkeypatch.setattr(updates, "system_boot_time", lambda: NOW - timedelta(days=1))
    for created, ok in (
        (started - timedelta(seconds=3), True),
        (started + timedelta(seconds=30), True),
        (started - timedelta(minutes=30), False),  # older than the run: another process
        (started + timedelta(minutes=5), False),  # newer: the PID was reused
        (None, True),  # cannot be queried: the image check decides
    ):
        monkeypatch.setattr(updates, "process_create_time", lambda pid, c=created: c)
        assert updates.is_installer_process(4321, started) is ok, created


def test_process_create_time_of_this_process() -> None:
    created = updates.process_create_time(os.getpid())
    assert created is not None and created.tzinfo is not None
    assert NOW - timedelta(days=30) < created <= datetime.now().astimezone()
    assert updates.process_create_time(4321) is None  # PIDs are multiples of 4


def test_running_install_ignores_a_reused_pid(tmp_path: Path, monkeypatch, procs) -> None:
    procs[os.getpid()] = "powershell.exe"
    monkeypatch.setattr(updates, "existing_log_dirs", lambda: [tmp_path])
    write_status(
        tmp_path / "windows-update-2026.11.22-160000.json",
        state="running",
        pid=os.getpid(),
        started=(NOW - timedelta(days=1)).isoformat(),
    )
    with pytest.raises(updates.UpdatesError) as exc:
        updates.running_install()
    assert exc.value.kind == "blocked"


# ------------------------------------------------------------- 4. the [i] marker


def test_marker_in_the_script_lines() -> None:
    download, install = loop_bodies()
    assert "'Pobieranie {0} z {1} [{3}]: {2}'" in download
    assert '"Instalacja [$i]: $($entry.title)"' in install
    assert "'  -> [{3}] {0} ({1}){2}'" in install
    assert "'  -> [{2}] błąd ({0}): {1}'" in install
    assert '"Nie pobrano, pomijam instalację [$i]: $($entry.title)"' in install


def test_log_with_markers_keeps_positions_and_duplicates() -> None:
    s = parse_install_log(
        log_text(
            "Start instalacji aktualizacji w tle (PID 4321).",
            "Pobieranie 1 z 1 [2]: Intel - System - 1.2.3",
            "-> pobrano",
            "Instalacja [1]: Intel - System - 1.2.3",
            "-> [1] błąd (0x80240022)",
            "Instalacja [2]: Intel - System - 1.2.3",
            "-> [2] OK (0x00000000), wymaga restartu",
            "Nie pobrano, pomijam instalację [3]: KB3",
            "Koniec (kod 2).",
            start=NOW,
        )
    )
    assert s.positions == (1, 2, 3)
    assert [(i.status, i.reboot_required) for i in s.items] == [
        ("failed", False),
        ("succeeded", True),
        ("not_downloaded", False),
    ]
    assert s.phase is None and s.current_index is None


def test_markers_win_over_order(tmp_path: Path, procs) -> None:
    """Results logged out of order still land on their own update."""
    status = write_status(tmp_path / "s.json", updates=[entry("T"), entry("T"), entry("T")])
    status.with_suffix(".log").write_text(
        log_text(
            "Start instalacji aktualizacji w tle (PID 4321).",
            "Instalacja [3]: T",
            "-> [3] błąd (0x80240022)",
            "Instalacja [1]: T",
            "-> [1] OK (0x00000000)",
            "Koniec (kod 2).",
            start=NOW,
        ),
        encoding="utf-8",
    )
    p = read_install_progress(status)
    assert [i.status for i in p.items] == ["succeeded", "pending", "failed"]
    assert (p.ok_count, p.failed_count, p.skipped_count) == (1, 1, 1)


def test_old_log_by_order_of_appearance(tmp_path: Path, procs) -> None:
    status = write_status(tmp_path / "s.json", updates=[entry("D"), entry("X"), entry("D")])
    status.with_suffix(".log").write_text(
        log_text(
            "Start instalacji aktualizacji w tle (PID 4321).",
            "Instalacja: D",
            "-> OK (0x00000000)",
            "Instalacja: X",
            "-> OK (0x00000000)",
            "Instalacja: D",
            "-> błąd (0x80240022)",
            "Koniec (kod 2).",
            start=NOW,
        ),
        encoding="utf-8",
    )
    p = read_install_progress(status)
    assert [i.status for i in p.items] == ["succeeded", "succeeded", "failed"]


def test_real_run_still_six_of_six(tmp_path: Path, procs) -> None:
    stem = "windows-update-2026.11.22-155534"
    for suffix in (".json", ".log"):
        (tmp_path / f"{stem}{suffix}").write_bytes((DATA / f"{stem}{suffix}").read_bytes())
    p = read_install_progress(tmp_path / f"{stem}.json")
    assert (p.state, p.ok_count, p.count, p.exit_code) == ("stale", 6, 6, 0)
    assert p.succeeded and describe_progress(p)[0] == "Zakończono — wymagany restart"


# --------------------------------------------------- 11. statuses closed at the end


def test_finally_closes_unfinished_items() -> None:
    final = block_after(
        WU_INSTALL_SCRIPT.index("} finally {", WU_INSTALL_SCRIPT.index("$exitCode = 1")) + 2
    )
    close = final.index("foreach ($e in $status.updates)")
    assert close < final.index("Save-Status")
    assert "$e.status -in 'pending', 'downloaded') { $e.status = 'skipped' }" in final
    assert "$e.status -in 'downloading', 'installing') { $e.status = 'aborted' }" in final


def test_download_result_read_inside_the_try() -> None:
    download, install = loop_bodies()
    body = download[download.index("try {") : download.index("} catch {")]
    assert "$u.IsDownloaded" in body and "-in 2, 3" in body
    assert "if ($got)" in download[download.index("} catch {") :]
    skip = install[install.index("if (-not $u.IsDownloaded)") :]
    assert skip.index("Set-Step $entry 'not_downloaded'") < skip.index("} else {")


def test_skipped_items_are_not_a_success() -> None:
    p = parse_install_status(
        json.dumps(
            {
                "winstaller": "wu-install",
                "state": "finished",
                "exitCode": 5,
                "updates": [entry("A", "skipped"), entry("B", "aborted")],
            }
        )
    )
    assert [i.status for i in p.items] == ["skipped", "aborted"]
    assert (p.ok_count, p.failed_count, p.skipped_count) == (0, 1, 1) and not p.succeeded
    assert describe_progress(p)[0] == "Nie zainstalowano aktualizacji"


# ------------------------------------------------- 13. status lagging behind the log


def test_live_installer_with_a_stale_status_is_completed_from_the_log(
    tmp_path: Path, procs
) -> None:
    procs[4321] = "powershell.exe"
    updated = NOW - timedelta(minutes=20)
    status = write_status(
        tmp_path / "s.json",
        updated=updated.isoformat(),
        phase="installing",
        current=1,
        total=3,
        updates=[entry("A", "installing"), entry("B", "downloaded"), entry("C", "downloaded")],
    )
    status.with_suffix(".log").write_text(
        log_text(
            "Start instalacji aktualizacji w tle (PID 4321).",
            "Instalacja [1]: A",
            "-> [1] OK (0x00000000)",
            "Instalacja [2]: B",
            start=NOW - timedelta(minutes=5),
        ),
        encoding="utf-8",
    )
    p = read_install_progress(status)
    assert p.state == "running" and p.alive and p.from_log
    assert [i.status for i in p.items] == ["succeeded", "installing", "downloaded"]
    assert (p.current, p.current_title) == (2, "B")
    title, subtitle = describe_progress(p)
    assert title == "Instalacja 2 z 3" and subtitle.startswith("Stan wg logu")


def test_fresh_status_is_not_replaced_by_the_log(tmp_path: Path, procs) -> None:
    procs[4321] = "powershell.exe"
    status = write_status(tmp_path / "s.json", updated=NOW.isoformat())
    status.with_suffix(".log").write_text(
        log_text("Instalacja [3]: X", start=NOW + timedelta(minutes=10)), encoding="utf-8"
    )
    assert not read_install_progress(status).from_log


# ------------------------------------------------------------------- 3. no PID


def test_no_pid_with_a_finished_log_is_the_log_result(tmp_path: Path, procs) -> None:
    status = write_status(tmp_path / "s.json", pid=None, updates=[entry("A")])
    status.with_suffix(".log").write_text(
        log_text("Instalacja: A", "-> OK (0x00000000)", "Koniec (kod 0).", start=NOW),
        encoding="utf-8",
    )
    p = read_install_progress(status)
    assert p.state == "stale" and p.from_log and p.succeeded


# -------------------------------------------------------------- 7. reboot per item


def test_needs_restart_from_an_item() -> None:
    p = InstallProgress(
        state="finished",
        exit_code=0,
        items=(InstallItem("A", "succeeded", reboot_required=True),),
    )
    assert p.needs_restart and not p.reboot_required
    assert describe_progress(p)[0] == "Zakończono — wymagany restart"
    empty = InstallProgress(state="finished", exit_code=0, reboot_required=True)
    assert "Uruchom ponownie" in describe_progress(empty)[1]


# ------------------------------------------------------------- 18. private paths


def test_no_profile_paths_in_test_data() -> None:
    pattern = re.compile(rb"[A-Za-z]:\\{1,2}Users\\{1,2}", re.IGNORECASE)
    for path in DATA.rglob("*"):
        if path.is_file():
            assert not pattern.search(path.read_bytes()), path
