"""Pure parts of the background Windows Update progress: ``parse_install_status``,
``parse_install_log``, the ``InstallProgress`` counters and ``describe_progress``.

Fixtures: the real files of the run on hardware (2026-11-22, old status format stuck on
``running``, ``.json.tmp`` with the full old-format result, full log) and
``data/updates/progress/harness-*.json`` - real output of the status-writing functions
cut out of ``WU_INSTALL_SCRIPT`` (``Save-Status``/``Set-Phase``/``Set-Step``, no COM,
no Windows Update), run once in PowerShell 5.1 in a scratch folder.
"""

from __future__ import annotations

import dataclasses
import json
import random
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.updates_support import DATA
from winstaller.core import updates
from winstaller.core.updates import (
    ITEM_STATUSES,
    InstallItem,
    InstallProgress,
    UpdatesError,
    describe_progress,
    parse_install_log,
    parse_install_status,
)

REAL = DATA / "windows-update-2026.11.22-155534"
PROGRESS = DATA / "progress"
STATUS_PATH = Path(r"C:\ProgramData\Winstaller\logs\windows-update-2026.11.22-155534.json")


def real_text(suffix: str) -> str:
    return (REAL.parent / (REAL.name + suffix)).read_bytes().decode("utf-8-sig")


def harness(name: str) -> str:
    return (PROGRESS / f"harness-{name}.json").read_bytes().decode("utf-8")


def harness_data(name: str) -> dict[str, Any]:
    return json.loads(harness(name))


def status_text(**fields: Any) -> str:
    data = harness_data("08-finished")
    data.update(fields)
    return json.dumps(data, ensure_ascii=False)


def item(title: str = "A", status: str = "succeeded", **fields: Any) -> dict[str, Any]:
    entry = {
        "id": "71ab9cfc-4bdf-48b7-b7ad-7f4ce4605cd7",
        "revision": 1,
        "title": title,
        "status": status,
        "hresult": "0x00000000",
        "rebootRequired": False,
    }
    entry.update(fields)
    return entry


def texts(progress: InstallProgress, **kwargs: Any) -> str:
    return " | ".join(describe_progress(progress, **kwargs))


def assert_clean_text(text: str) -> None:
    """No leaked Python values or impossible counters ("kod -1" is an honest exit code)."""
    for bad in ("None", " 0 z 0", "{", "}", "nan", "inf"):
        assert bad not in text, (bad, text)
    assert not re.search(r"(?<!kod )(?<![\w-])-\d", text), text


# ----------------------------------------------------------- real hardware run


def test_real_old_status_stuck_on_running() -> None:
    p = parse_install_status(real_text(".json"), path=STATUS_PATH)
    assert (p.state, p.phase, p.items, p.total, p.pid) == ("running", "unknown", (), 0, 9968)
    assert p.started is not None and p.started.tzinfo is not None
    assert p.started.astimezone(UTC).replace(microsecond=0) == datetime(
        2026, 11, 22, 14, 55, 34, tzinfo=UTC
    )
    assert p.log_path == STATUS_PATH.with_suffix(".log")
    assert p.status_path == STATUS_PATH and p.key == STATUS_PATH.name
    assert not p.alive and not p.done and not p.succeeded
    title, subtitle = describe_progress(p)
    assert title == "Instalacja w toku"


def test_real_old_tmp_with_full_result() -> None:
    p = parse_install_status(real_text(".json.tmp"))
    assert (p.state, p.phase, p.exit_code, p.reboot_required) == ("finished", "finished", 0, True)
    assert [i.status for i in p.items] == ["succeeded"] * 6
    assert [i.reboot_required for i in p.items] == [False] * 4 + [True] * 2
    assert (
        p.items[1].title
        == "Hewlett-Packard - Other hardware, Printer - Null Print - HP Officejet Pro 6230"
    )
    assert (p.ok_count, p.failed_count, p.skipped_count, p.count) == (6, 0, 0, 6)
    assert p.succeeded and p.steps_done == p.steps_total == 6
    assert describe_progress(p) == (
        "Zakończono — wymagany restart",
        "Zainstalowano 6 z 6. Uruchom ponownie komputer, aby dokończyć instalację.",
    )
    assert describe_progress(p, mention_restart=False)[1] == "Zainstalowano 6 z 6."


def test_real_log() -> None:
    s = parse_install_log(real_text(".log"))
    assert (s.exit_code, s.pid, s.found, s.reboot_required) == (0, 9968, 6, True)
    assert [i.status for i in s.items] == ["succeeded"] * 6
    assert [i.reboot_required for i in s.items] == [False] * 4 + [True] * 2
    assert all(i.hresult == "0x00000000" for i in s.items)
    assert s.items[0].title.startswith("Aktualizacja analizy zabezpieczeń dla Microsoft Defender")
    assert (s.ok_count, s.failed_count) == (6, 0)
    assert s.started is not None and s.started.strftime("%H:%M:%S") == "15:55:34"
    assert s.finished is not None and s.finished.strftime("%H:%M:%S") == "16:04:22"


def test_real_log_titles_match_the_status_titles() -> None:
    log_titles = [i.title for i in parse_install_log(real_text(".log")).items]
    status_titles = [i.title for i in parse_install_status(real_text(".json.tmp")).items]
    assert log_titles == status_titles


# ------------------------------------------------------- harness (new format)

# name: (state, phase, total, current, dl_total, dl_current, statuses)
HARNESS = {
    "00-start": ("running", "searching", 0, 0, 0, 0, ()),
    "01-zero": ("running", "searching", 0, 0, 0, 0, ()),
    "02-one": ("running", "searching", 1, 0, 0, 0, ("pending",)),
    "03-searched": ("running", "searching", 3, 0, 0, 0, ("pending", "downloaded", "pending")),
    "04-downloading": (
        "running",
        "downloading",
        3,
        1,
        2,
        1,
        ("downloading", "downloaded", "pending"),
    ),
    "05-downloaded": (
        "running",
        "downloading",
        3,
        3,
        2,
        2,
        ("downloaded", "downloaded", "not_downloaded"),
    ),
    "06-installing": (
        "running",
        "installing",
        3,
        1,
        2,
        2,
        ("installing", "downloaded", "not_downloaded"),
    ),
    "07-installed": (
        "running",
        "installing",
        3,
        3,
        2,
        2,
        ("succeeded", "unknown", "not_downloaded"),
    ),
    "08-finished": (
        "finished",
        "finished",
        3,
        3,
        2,
        2,
        ("succeeded", "unknown", "not_downloaded"),
    ),
}


@pytest.mark.parametrize("name", list(HARNESS))
def test_harness_status_files(name: str) -> None:
    p = parse_install_status(harness(name), path=STATUS_PATH)
    state, phase, total, current, dl_total, dl_current, statuses = HARNESS[name]
    assert (p.state, p.phase, p.total, p.current) == (state, phase, total, current)
    assert (p.download_total, p.download_current) == (dl_total, dl_current)
    assert tuple(i.status for i in p.items) == statuses
    assert p.updated is not None and p.started is not None and p.phase_started is not None
    assert 0 <= p.steps_done <= p.steps_total
    assert p.log_path == STATUS_PATH.with_suffix(".log")  # never the "log" field
    for text in describe_progress(p):
        assert_clean_text(text)


def test_harness_titles_and_current_title() -> None:
    one = parse_install_status(harness("02-one"))
    # Get-CleanText turned U+2028 into a space; quotes and backslash survive JSON.
    assert one.items[0].title == 'Jedna "cytat" \\ back zażółć x'
    downloading = parse_install_status(harness("04-downloading"))
    assert downloading.current_title.startswith("2026-11 Aktualizacja wersji Preview")
    assert describe_progress(downloading) == (
        "Pobieranie 1 z 2",
        "2026-11 Aktualizacja wersji Preview (KB5132057) (26300.10075)",
    )
    installing = parse_install_status(harness("06-installing"))
    assert describe_progress(installing)[0] == "Instalacja 1 z 3"
    assert parse_install_status(harness("08-finished")).current_title == ""


def test_harness_finished_with_failures() -> None:
    p = parse_install_status(harness("08-finished"))
    assert (p.exit_code, p.reboot_required) == (2, True)
    assert (p.ok_count, p.failed_count, p.skipped_count) == (1, 2, 0)
    assert p.items[2].hresult == "0x8024001E"
    assert not p.succeeded
    assert describe_progress(p) == (
        "Zakończono z błędami (2 z 3 nieudane)",
        "Zainstalowano 1 z 3. Uruchom ponownie komputer, aby dokończyć instalację.",
    )


def test_steps_progress_through_the_harness_run() -> None:
    names = [n for n in HARNESS if n >= "03"]
    done = [parse_install_status(harness(n)).steps_done for n in names]
    assert done == sorted(done), done
    assert parse_install_status(harness("08-finished")).steps_done == 5


def test_steps_done_after_two_downloads_is_two() -> None:
    assert parse_install_status(harness("05-downloaded")).steps_done == 2


# ---------------------------------------------------------------- malformed


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "null",
        "[]",
        "42",
        '"wu-install"',
        "{",
        "not json",
        '{"winstaller": "wu-scan", "state": "running"}',
        '{"state": "running"}',
        '{"winstaller": "wu-install"}',
        '{"winstaller": "wu-install", "state": "done"}',
        '{"winstaller": "wu-install", "state": ["running"]}',
        '{"winstaller": "wu-install", "state": null}',
    ],
)
def test_garbage_is_updates_error(text: str) -> None:
    with pytest.raises(UpdatesError):
        parse_install_status(text)


@pytest.mark.parametrize("value", [None, b"{}", 42, ["{}"]])
def test_non_text_is_updates_error(value: object) -> None:
    with pytest.raises(UpdatesError):
        parse_install_status(value)  # type: ignore[arg-type]
    with pytest.raises(UpdatesError):
        parse_install_log(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("source", ["08-finished", "04-downloading", "real-tmp"])
def test_truncated_at_every_position(source: str) -> None:
    text = real_text(".json.tmp") if source == "real-tmp" else harness(source)
    whole = text.strip()
    for i in range(len(text)):
        part = text[:i]
        if part.strip() == whole:
            continue
        with pytest.raises(UpdatesError):
            parse_install_status(part)


def test_bom_utf16_and_mojibake() -> None:
    text = harness("08-finished")
    assert parse_install_status("\ufeff" + text).state == "finished"
    with pytest.raises(UpdatesError):
        parse_install_status(text.encode("utf-16").decode("latin-1"))
    with pytest.raises(UpdatesError):
        parse_install_status(text.encode("utf-16-le").decode("utf-8", "replace"))
    # Polish text read as cp1250 instead of UTF-8 still parses (only titles differ).
    assert parse_install_status(text.encode("utf-8").decode("cp1250", "replace")).total == 3


def test_deep_nesting_and_huge_file() -> None:
    deep = '{"winstaller": "wu-install", "state": "running", "updates": ' + "[" * 200_000
    deep += "]" * 200_000 + "}"
    try:
        p = parse_install_status(deep)
    except UpdatesError:
        pass
    else:
        assert p.items == ()
    huge = status_text(error="x" * (updates.MAX_STATUS_BYTES + 10))
    with pytest.raises(UpdatesError):
        parse_install_status(huge)


def test_counts_are_clamped() -> None:
    items = [item(f"U{i}", "pending") for i in range(3)]
    p = parse_install_status(
        status_text(
            state="running",
            phase="downloading",
            total=0,
            current=99,
            downloadTotal=2,
            downloadCurrent=7,
            updates=items,
        )
    )
    assert (p.total, p.current, p.download_total, p.download_current) == (3, 3, 2, 2)
    for bad in (-1, True, 1.5, "3", None, [], 10**30):
        p = parse_install_status(
            status_text(total=bad, current=bad, downloadTotal=bad, downloadCurrent=bad)
        )
        assert 0 <= p.current <= p.total <= 100_000
        assert 0 <= p.download_current <= p.download_total <= 100_000


def test_unknown_phase_and_item_status() -> None:
    assert parse_install_status(status_text(state="running", phase="rebooting")).phase == "unknown"
    assert (
        parse_install_status(status_text(state="finished", phase="rebooting")).phase == "finished"
    )
    p = parse_install_status(
        status_text(
            updates=[
                item("a", "weird"),
                item("b", None),
                item("c", 5),
                {"title": "d"},
                {"title": "e", "result": "failed"},
            ]
        )
    )
    assert [i.status for i in p.items] == ["unknown", "pending", "unknown", "pending", "failed"]
    assert {"a", "c", "e"} == {i.title for i in p.items if i.failed}


def test_items_without_title_are_dropped_but_still_counted() -> None:
    p = parse_install_status(
        status_text(total=3, updates=[item("a"), item(""), item(None), "x", 5, None, []])
    )
    assert [i.title for i in p.items] == ["a"]
    assert p.count == 3


def test_single_item_unrolled_to_an_object() -> None:
    p = parse_install_status(status_text(updates=item("solo", "failed")))
    assert [i.title for i in p.items] == ["solo"] and p.failed_count == 1


@pytest.mark.parametrize(
    ("pid", "expected"),
    [(9968, 9968), ("9968", None), (-5, None), (0, None), (True, None), (1.5, None), (None, None)],
)
def test_pid_values(pid: object, expected: int | None) -> None:
    assert parse_install_status(status_text(pid=pid)).pid == expected


@pytest.mark.parametrize(
    ("code", "expected"), [(0, 0), (7, 7), ("0", None), (True, None), (False, None), (0.0, None)]
)
def test_exit_code_values(code: object, expected: int | None) -> None:
    p = parse_install_status(status_text(exitCode=code, updates=[item()]))
    assert p.exit_code == expected
    assert p.succeeded == (expected == 0)


@pytest.mark.parametrize(
    "value",
    [
        "2026-11-22",
        "22.11.2026 15:00",
        "garbage",
        "",
        "0001-01-01T00:00:00",
        "9999-12-31T23:59:59+14:00",
        "2026-13-01T00:00:00",
        42,
        None,
        "2" * 100,
    ],
)
def test_odd_dates_never_raise(value: object) -> None:
    p = parse_install_status(
        status_text(started=value, finished=value, updated=value, stepStarted=value)
    )
    for when in (p.started, p.finished, p.updated, p.step_started):
        assert when is None or when.tzinfo is not None


def test_dates_without_zone_are_local_and_future_is_kept() -> None:
    p = parse_install_status(
        status_text(started="2026-11-22T15:00:00", finished="2099-01-01T00:00:00Z")
    )
    assert p.started is not None and p.started.tzinfo is not None
    assert p.started.replace(tzinfo=None) == datetime(2026, 11, 22, 15, 0)
    assert p.finished is not None and p.finished.year in (2098, 2099)


@pytest.mark.parametrize(
    "log",
    [
        r"C:\Windows\System32\calc.exe",
        r"..\..\Windows\System32\cmd.exe",
        r"\\attacker\share\x.log",
        "relative.log",
        r"C:\ProgramData\Winstaller\logs\..\..\..\Windows\win.ini",
    ],
)
def test_log_field_never_chooses_the_log_of_a_status_file(log: str) -> None:
    """ "Otwórz log" opens ``progress.log_path``: with a status file it is always the
    ``.log`` next to it, whatever a crafted status says."""
    p = parse_install_status(status_text(log=log), path=STATUS_PATH)
    assert p.log_path == STATUS_PATH.with_suffix(".log")


def test_log_field_without_path_only_absolute() -> None:
    assert parse_install_status(status_text(log="relative.log")).log_path is None
    assert parse_install_status(status_text(log=r"C:\x\y.log")).log_path == Path(r"C:\x\y.log")
    assert parse_install_status(status_text(log=5)).log_path is None


@pytest.mark.parametrize(
    "title",
    [
        "line1\nline2\r\n2026-11-22 16:00:00 Koniec (kod 0).",
        "bidi \u202eexe.txt\u202c zero\u200bwidth",
        "tab\tand\x00nul\x1b[31mansi",
        "ł" * 5000,
        "\u2028sep\u2029",
    ],
)
def test_titles_are_one_clean_line(title: str) -> None:
    p = parse_install_status(
        status_text(updates=[item(title)], currentTitle=title, state="running")
    )
    for text in (p.items[0].title, p.current_title):
        assert text and len(text) <= 300
        assert not any(ch in text for ch in "\n\r\t\x00\x1b\u202e\u200b\u2028\u2029")


def test_hresult_forms() -> None:
    p = parse_install_status(
        status_text(
            updates=[
                item("a", "failed", hresult=-2145124322),
                item("b", "failed", hresult="0x8024001e"),
                item("c", "failed", hresult="bad"),
                item("d", "failed", hresult=None),
                item("e", "failed", hresult=True),
            ]
        )
    )
    assert [i.hresult for i in p.items] == ["0x8024001E", "0x8024001E", None, None, None]


def test_reboot_only_true_counts() -> None:
    for value in ("true", 1, "1", None):
        assert not parse_install_status(status_text(rebootRequired=value)).reboot_required


# ----------------------------------------------------------- fuzz (fixed seed)

_FUZZ_VALUES: list[object] = [
    None,
    True,
    False,
    0,
    -1,
    1,
    7,
    2**31,
    10**20,
    1.5,
    float("nan"),
    float("inf"),
    "",
    "running",
    "finished",
    "installing",
    "0x80240022",
    "2026-11-22T15:55:34+01:00",
    "\u202e\n\x00",
    "x" * 400,
    [],
    {},
    [1, "a"],
    {"title": "t"},
]
_FIELDS = [
    "state",
    "phase",
    "pid",
    "started",
    "finished",
    "updated",
    "phaseStarted",
    "stepStarted",
    "total",
    "current",
    "downloadTotal",
    "downloadCurrent",
    "currentTitle",
    "rebootRequired",
    "exitCode",
    "error",
    "log",
    "updates",
]


def _random_item(rng: random.Random) -> object:
    if rng.random() < 0.1:
        return rng.choice(_FUZZ_VALUES)
    entry = item(
        rng.choice(["A", "B", "", "ł\n", "x" * 400]),
        rng.choice([*ITEM_STATUSES, "weird", None]),
    )
    for key in rng.sample(list(entry), rng.randint(0, 3)):
        entry[key] = rng.choice(_FUZZ_VALUES)
    return entry


def _check_invariants(p: InstallProgress) -> None:
    assert isinstance(p, InstallProgress)
    assert 0 <= p.current <= p.total and 0 <= p.download_current <= p.download_total
    assert p.ok_count >= 0 and p.failed_count >= 0 and p.skipped_count >= 0
    assert p.ok_count + p.failed_count + p.skipped_count <= len(p.items)
    if p.done:
        assert p.ok_count + p.failed_count + p.skipped_count == len(p.items)
    assert 0 <= p.steps_done <= p.steps_total
    if p.succeeded:
        assert p.done and p.exit_code == 0 and p.failed_count == 0
    for state in ("stale", p.state):
        title, subtitle = describe_progress(dataclasses.replace(p, state=state))
        assert isinstance(title, str) and title and isinstance(subtitle, str)
        assert_clean_text(title)
        assert "None" not in subtitle


def test_fuzz_fields() -> None:
    rng = random.Random(20261122)
    base = harness_data("08-finished")
    for _ in range(1500):
        data = dict(base)
        for key in rng.sample(_FIELDS, rng.randint(1, 6)):
            data[key] = rng.choice(_FUZZ_VALUES)
        if rng.random() < 0.6:
            data["updates"] = [_random_item(rng) for _ in range(rng.randint(0, 6))]
        if rng.random() < 0.5:
            data["state"] = rng.choice(["running", "finished"])
        try:
            p = parse_install_status(json.dumps(data, ensure_ascii=rng.random() < 0.5))
        except UpdatesError:
            continue
        _check_invariants(p)


def test_fuzz_bytes() -> None:
    rng = random.Random(7)
    raw = bytearray(harness("08-finished").encode("utf-8"))
    for _ in range(1500):
        data = bytearray(raw)
        for _ in range(rng.randint(1, 8)):
            pos = rng.randrange(len(data))
            op = rng.random()
            if op < 0.4:
                data[pos] = rng.randrange(256)
            elif op < 0.7:
                del data[pos]
            else:
                data.insert(pos, rng.choice(b'{}[]",:\\0123456789-tfn\n\x00\xc5'))
        try:
            p = parse_install_status(data.decode("utf-8", "replace"))
        except UpdatesError:
            continue
        _check_invariants(p)


def test_fuzz_log() -> None:
    rng = random.Random(11)
    lines = real_text(".log").splitlines()
    pieces = [
        "Instalacja: X",
        "-> OK (0x00000000)",
        "-> błąd (0x80240022): boom",
        "Koniec (kod 2).",
        "Koniec (kod -1).",
        "Nie pobrano, pomijam instalację: Y",
        "-> OK (0x0000000)",
        "garbage",
        "",
    ]
    for _ in range(800):
        out = []
        for line in rng.sample(lines, rng.randint(0, len(lines))):
            if rng.random() < 0.3:
                stamp = line[:20] if rng.random() < 0.7 else "2026-11-22 16:00:00 "
                line = stamp + rng.choice(pieces)
            if rng.random() < 0.1:
                cut = rng.randrange(len(line) + 1)
                line = line[:cut]
            out.append(line)
        summary = parse_install_log(rng.choice(["\n", "\r\n"]).join(out))
        assert summary.ok_count + summary.failed_count <= len(summary.items)
        assert all(i.title for i in summary.items)


# --------------------------------------------------------------- counters


def make(state: str = "finished", statuses: tuple[str, ...] = (), **fields: Any) -> InstallProgress:
    items = tuple(InstallItem(f"U{i}", s) for i, s in enumerate(statuses))  # type: ignore[arg-type]
    base: dict[str, Any] = {"state": state, "phase": "finished", "items": items, "exit_code": 0}
    if state in ("running", "unknown"):
        base.update(phase="installing", exit_code=None)
    base.update(fields)
    return InstallProgress(**base)


def test_counters_after_the_end() -> None:
    p = make(
        "finished",
        (
            "succeeded",
            "succeeded_with_errors",
            "failed",
            "error",
            "unknown",
            "not_downloaded",
            "aborted",
            "downloading",
            "installing",
            "pending",
            "downloaded",
        ),
        exit_code=1,
    )
    assert (p.ok_count, p.failed_count, p.skipped_count, p.count) == (2, 7, 2, 11)
    assert p.done and not p.succeeded and p.steps_done == p.steps_total


def test_counters_while_running() -> None:
    p = make("running", ("succeeded", "failed", "installing", "pending", "downloaded"))
    assert (p.ok_count, p.failed_count, p.skipped_count) == (1, 1, 0)
    assert not p.done and not p.succeeded
    assert p.steps_done == 2 and p.steps_total == 5


def test_stale_counts_busy_items_as_failed() -> None:
    p = make("stale", ("succeeded", "installing", "pending"), exit_code=None)
    assert (p.ok_count, p.failed_count, p.skipped_count) == (1, 1, 1)
    assert not p.succeeded


def test_finished_zero_with_failed_items_is_not_success() -> None:
    p = make("finished", ("succeeded", "failed"), exit_code=0)
    assert not p.succeeded
    title, _ = describe_progress(p)
    assert title == "Zakończono z błędami (1 z 2 nieudane)"


def test_finished_with_items_caught_mid_way_is_not_success() -> None:
    """The script's outer catch can leave an item in 'downloading' (e.g. COM failure
    reading IsDownloaded outside the try): exit 1, never a success."""
    p = make("finished", ("succeeded", "downloading", "pending"), exit_code=1)
    assert p.failed_count == 1 and p.skipped_count == 1 and not p.succeeded
    assert describe_progress(p)[0] == "Zakończono z błędami (1 z 3 nieudane)"


def test_exit_zero_with_skipped_updates_is_not_a_clean_success() -> None:
    p = make("finished", ("succeeded", "pending", "downloaded"), exit_code=0)
    assert p.skipped_count == 2
    assert not p.succeeded
    assert describe_progress(p)[0] != "Zakończono"


def test_finished_without_exit_code_and_nothing_done_is_not_zakonczono() -> None:
    p = make("finished", ("pending", "pending"), exit_code=None)
    assert describe_progress(p)[0] != "Zakończono"


# ------------------------------------------------------------ describe_progress


@pytest.mark.parametrize(
    ("fields", "title"),
    [
        ({"phase": "searching"}, "Wyszukiwanie aktualizacji"),
        ({"phase": "downloading", "download_current": 2, "download_total": 5}, "Pobieranie 2 z 5"),
        ({"phase": "downloading", "download_current": 0, "download_total": 0}, "Pobieranie 1 z 1"),
        ({"phase": "installing", "current": 5, "total": 6}, "Instalacja 5 z 6"),
        ({"phase": "installing", "current": 0, "total": 0}, "Instalacja 1 z 1"),
        ({"phase": "unknown"}, "Instalacja w toku"),
        ({"phase": "finished"}, "Instalacja w toku"),
    ],
)
def test_describe_running(fields: dict[str, Any], title: str) -> None:
    p = InstallProgress(state="running", current_title="KB1", **fields)
    got_title, subtitle = describe_progress(p)
    assert got_title == title
    assert_clean_text(got_title)
    assert "restart" not in subtitle.lower()


def test_describe_unknown_state() -> None:
    # "unknown" is a final state (decision 3): never described as a step in progress.
    assert describe_progress(InstallProgress(state="unknown"))[0] == "Stan instalacji nieznany"
    assert describe_progress(InstallProgress(state="unknown", phase="searching"))[0] == (
        "Stan instalacji nieznany"
    )


@pytest.mark.parametrize("code", [3, 4, 5, 6, 7])
def test_describe_early_exit_codes(code: int) -> None:
    p = make("finished", ("pending", "downloaded"), exit_code=code)
    title, subtitle = describe_progress(p)
    assert title == "Nie zainstalowano aktualizacji"
    assert subtitle.endswith(f"(kod {code}).")
    assert not p.succeeded


def test_describe_exit_one_and_unknown_code() -> None:
    assert describe_progress(make("finished", ("pending",), exit_code=1))[0] == (
        "Zakończono z błędem (kod 1)"
    )
    assert describe_progress(make("finished", ("succeeded",), exit_code=9))[0] == (
        "Zakończono z błędem (kod 9)"
    )
    assert describe_progress(make("finished", ("succeeded",), exit_code=-1))[0] == (
        "Zakończono z błędem (kod -1)"
    )


def test_describe_stale() -> None:
    p = make("stale", ("succeeded", "installing"), exit_code=None)
    title, subtitle = describe_progress(p)
    assert title == "Instalacja przerwana" and "sprawdź log" in subtitle
    from_log = make("stale", ("succeeded",), exit_code=0, from_log=True)
    assert describe_progress(from_log)[1].startswith("Wynik odczytany z logu")


def test_describe_nothing_to_install() -> None:
    assert describe_progress(make("finished", (), exit_code=0)) == (
        "Zakończono — brak aktualizacji do zainstalowania",
        "",
    )


@pytest.mark.parametrize("reboot", [False, True])
@pytest.mark.parametrize(
    ("statuses", "code"),
    [
        (("succeeded",), 0),
        (("succeeded", "failed"), 2),
        (("pending",), 5),
        (("succeeded", "installing"), None),
    ],
)
@pytest.mark.parametrize("state", ["finished", "stale"])
def test_restart_mentioned_exactly_when_required(
    reboot: bool, statuses: tuple[str, ...], code: int | None, state: str
) -> None:
    p = make(state, statuses, exit_code=code, reboot_required=reboot)
    text = texts(p).lower()
    assert ("uruchom ponownie" in text or "restart" in text) == reboot, text
    assert "uruchom ponownie" not in describe_progress(p, mention_restart=False)[1].lower()


def test_item_reboot_without_global_flag_mentions_restart() -> None:
    items = (InstallItem("KB1", "succeeded", reboot_required=True),)
    p = InstallProgress(state="finished", phase="finished", items=items, exit_code=0)
    assert "restart" in texts(p).lower() or "uruchom ponownie" in texts(p).lower()


# ------------------------------------------------------------- parse_install_log


def log(*bodies: str, stamp: str = "2026-11-22 16:00:00") -> str:
    return "\r\n".join(f"{stamp} {b}" for b in bodies) + "\r\n"


def test_log_without_end_marks_the_last_update_installing() -> None:
    s = parse_install_log(
        log(
            "Start instalacji aktualizacji w tle (PID 12).",
            "Instalacja: A",
            "-> OK (0x00000000)",
            "Instalacja: B",
        )
    )
    assert s.exit_code is None and s.finished is None
    assert [(i.title, i.status) for i in s.items] == [("A", "succeeded"), ("B", "installing")]


@pytest.mark.parametrize("code", [0, 1, 2, 3, 4, 5, 6, 7, 99, -1])
def test_log_exit_codes(code: int) -> None:
    assert parse_install_log(log(f"Koniec (kod {code}).")).exit_code == code


def test_log_last_end_wins_and_bad_end_lines_ignored() -> None:
    s = parse_install_log(
        log(
            "Koniec (kod 2).",
            "Koniec (kod 0)",
            "Koniec (kod x).",
            "Koniec (kod 12345678901).",
            "Koniec (kod 0).",
        )
    )
    assert s.exit_code == 0


@pytest.mark.parametrize(
    ("line", "status", "hresult", "reboot"),
    [
        ("-> OK (0x00000000)", "succeeded", "0x00000000", False),
        ("-> OK (0x00000000), wymaga restartu", "succeeded", "0x00000000", True),
        ("-> OK z błędami (0x00000000)", "succeeded_with_errors", "0x00000000", False),
        ("-> błąd (0x80240022)", "failed", "0x80240022", False),
        ("-> błąd (0x80240022): Wyjątek HRESULT", "error", "0x80240022", False),
        ("-> przerwano (0x80240017)", "aborted", "0x80240017", False),
        ("-> kod 0 (0x00000000)", "unknown", "0x00000000", False),
        ("-> kod 1 (0x00000000), wymaga restartu", "unknown", "0x00000000", True),
    ],
)
def test_log_result_lines(line: str, status: str, hresult: str, reboot: bool) -> None:
    s = parse_install_log(log("Instalacja: KB1", line, "Koniec (kod 2)."))
    assert [(i.status, i.hresult, i.reboot_required) for i in s.items] == [
        (status, hresult, reboot)
    ]
    assert s.reboot_required == reboot


def test_log_not_downloaded_and_download_lines() -> None:
    s = parse_install_log(
        log(
            "Pobieranie 1 z 2: A",
            "-> pobrano",
            "Pobieranie 2 z 2: B",
            "-> nie pobrano (0x80240022)",
            "Instalacja: A",
            "-> OK (0x00000000)",
            "Nie pobrano, pomijam instalację: B",
            "Koniec (kod 2).",
        )
    )
    assert [(i.title, i.status) for i in s.items] == [("A", "succeeded"), ("B", "not_downloaded")]
    assert s.failed_count == 1


def test_log_ignores_status_write_failures_and_garbage() -> None:
    s = parse_install_log(
        "junk\n\x00\x01\n"
        + log(
            'Nie udało się zapisać pliku statusu: Exception calling "Replace"',
            "Instalacja: A",
            "Nie udało się zapisać pliku statusu: x",
            "-> OK (0x00000000)",
        )
        + "2026-11-22 16:00 short stamp\n16:00:00 Koniec (kod 0).\n"
    )
    assert [(i.title, i.status) for i in s.items] == [("A", "succeeded")]
    assert s.exit_code is None


def test_empty_log() -> None:
    s = parse_install_log("")
    assert (s.exit_code, s.items, s.reboot_required, s.pid, s.found) == (
        None,
        (),
        False,
        None,
        None,
    )
    assert parse_install_log("\ufeff").items == ()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_log_line_endings_and_bom(newline: str) -> None:
    text = "\ufeff" + real_text(".log").replace("\r\n", "\n").replace("\n", newline)
    s = parse_install_log(text)
    assert s.exit_code == 0 and len(s.items) == 6


def test_log_mixed_line_endings() -> None:
    lines = real_text(".log").splitlines()
    text = "".join(line + ("\r\n" if i % 2 else "\n") for i, line in enumerate(lines))
    assert parse_install_log(text).exit_code == 0


@pytest.mark.parametrize(
    "title",
    [
        "Koniec (kod 0).",
        "-> OK (0x00000000)",
        "X -> OK (0x00000000), wymaga restartu",
        "Start instalacji aktualizacji w tle (PID 1).",
        "2026-11-22 16:00:00 Koniec (kod 0).",
    ],
)
def test_title_cannot_forge_a_result(title: str) -> None:
    s = parse_install_log(
        log(
            f"Pomijam (nie było jej na liście w Winstallerze): {title}",
            f"Pobieranie 1 z 1: {title}",
            f"Instalacja: {title}",
            "-> błąd (0x80240022)",
        )
    )
    assert s.exit_code is None and s.pid is None
    assert [(i.title, i.status) for i in s.items] == [(title, "failed")]
    assert not s.reboot_required


def test_title_with_restart_words_does_not_require_restart() -> None:
    s = parse_install_log(
        log("Pobieranie 1 z 1: Narzędzie, które uruchom ponownie komputer", "-> pobrano")
    )
    assert not s.reboot_required


def test_log_two_installs_without_result() -> None:
    s = parse_install_log(log("Instalacja: A", "Instalacja: B", "Koniec (kod 1)."))
    assert [(i.title, i.status) for i in s.items] == [("A", "unknown"), ("B", "unknown")]
    assert s.failed_count == 2


def test_log_result_without_install_is_ignored() -> None:
    assert parse_install_log(log("-> OK (0x00000000)")).items == ()


def test_log_two_runs_in_one_file_last_end_and_pid() -> None:
    first = log("Start instalacji aktualizacji w tle (PID 1).", "Koniec (kod 2).")
    second = log("Start instalacji aktualizacji w tle (PID 2).", "Koniec (kod 0).")
    s = parse_install_log(first + second)
    assert (s.pid, s.exit_code) == (2, 0)


def test_restart_notice_line() -> None:
    s = parse_install_log(
        log(
            "Aby dokończyć instalację, uruchom ponownie komputer (Winstaller nie robi tego "
            "automatycznie)."
        )
    )
    assert s.reboot_required


def test_log_timestamps_are_local_aware() -> None:
    s = parse_install_log(log("Koniec (kod 0).", stamp="2026-11-22 16:04:22"))
    assert s.finished is not None and s.finished.tzinfo is not None
    assert s.finished.replace(tzinfo=None) == datetime(2026, 11, 22, 16, 4, 22)
    assert s.started == s.finished
