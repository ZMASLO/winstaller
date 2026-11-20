"""GPU drivers: the hardening added after the security review (points 1-13 of the
review). Nothing touches the network; the only real processes are harmless
``python -c`` children, the only locked files are temporary copies or python.exe."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import threading
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import urllib3

from tests.drivers_support import (
    AMD_B_URL,
    AMD_NOTES,
    AMD_URL,
    NV_URL,
    SMALL_MIN,
    SUBJECTS,
    FakeResponse,
    FakeSession,
    data,
    exe_response,
    installer_bytes,
    release,
    valid_signature_lines,
)
from tests.unit.test_drivers_install import (
    BASE_PYTHON,
    DOWNLOAD_URL,
    FakeRun,
    TreeRun,
    kill_pid,
    vendor_routes,
    wait_dead,
)
from tests.unit.test_drivers_parsers import dsa_arc, dsa_bytes, dsa_zip
from winstaller.core import actions, drivers, winget
from winstaller.core import platform as plat
from winstaller.core.drivers import DriverError, ProcessResult

NV = drivers.NVIDIA_DOWNLOAD_HOSTS


@pytest.fixture
def small(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drivers, "MIN_INSTALLER_BYTES", SMALL_MIN)
    monkeypatch.setattr(drivers, "free_space", lambda _p: 10**15)


@pytest.fixture
def run_installer(monkeypatch: pytest.MonkeyPatch, small: None, tmp_path: Path):
    """install_latest('nvidia') with faked network and processes; returns a runner."""

    def go(fake: FakeRun, vendor: str = "nvidia", **kw: Any) -> drivers.DriverInstallResult:
        payload = kw.pop("payload", installer_bytes(seed=3))
        session = FakeSession(vendor_routes(vendor, payload))
        for url, route in kw.pop("routes", {}).items():
            session.routes[url] = route
        monkeypatch.setattr(drivers, "run_process", fake)
        work = tmp_path / "work"
        work.mkdir(exist_ok=True)
        return drivers.install_latest(
            vendor,
            log=kw.pop("log", lambda _t: None),
            replace_last=lambda _t: None,
            session=session,
            work_dir=work,
        )

    return go


def leftovers(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in (tmp_path / "work").iterdir())


# ----------------------------------------------------- 1. host allow-list


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.com\\.us.download.nvidia.com/x.exe",
        "https://us.download.nvidia.com\\@evil.com/x.exe",
        "https://us.download.nvidia.com/x\\y.exe",  # backslash anywhere
        "https://us.download.nvidia.com/x .exe",
        "https://us.download.nvidia.com/x .exe",
        "https://us.download.nvidia.com/ż.exe",  # non-ASCII anywhere
        "https://us%2edownload.nvidia.com/x.exe",
        "https://-us.download.nvidia.com/x.exe",
        "https://us-.download.nvidia.com/x.exe",
        "https://us..download.nvidia.com/x.exe",
        "https://us_x.download.nvidia.com/x.exe",
        "https://1.2.3.4/x.exe",
    ],
)
def test_host_allowed_rejects_parser_tricks(url: str) -> None:
    assert not drivers.host_allowed(url, (*NV, "1.2.3.4"))


def test_host_allowed_requires_both_parsers_to_agree(monkeypatch: pytest.MonkeyPatch) -> None:
    # If urllib3 ever saw another host than urlsplit, the URL must be refused.
    monkeypatch.setattr(drivers, "_client_host", lambda _u: ("https", "evil.com", None, False))
    assert not drivers.host_allowed(NV_URL, NV)
    monkeypatch.setattr(drivers, "_client_host", lambda _u: None)
    assert not drivers.host_allowed(NV_URL, NV)


@pytest.mark.parametrize("seed", range(5))
def test_every_accepted_url_has_an_allowed_host_for_urllib3(seed: int) -> None:
    import random

    rng = random.Random(seed)
    pieces = [
        "evil.com",
        "\\",
        "@",
        "%2f",
        "#",
        "?",
        "/",
        ".",
        ":443",
        " ",
        "us.download.nvidia.com",
    ]
    for _ in range(400):
        url = "https://" + "".join(rng.choice(pieces) for _ in range(rng.randint(1, 6))) + "/x.exe"
        if drivers.host_allowed(url, NV):
            host = urllib3.util.parse_url(url).host or ""
            assert drivers.host_allowed(f"https://{host}/x.exe", NV), url
            assert host.rstrip(".").endswith(".download.nvidia.com"), url


def test_backslash_url_in_nvidia_answer_is_ignored() -> None:
    from tests.unit.test_drivers_parsers import nv_answer, nv_entry, nv_url

    evil = "https://evil.com\\.us.download.nvidia.com/700.01.exe"
    answer = nv_answer(nv_entry(Version="700.01", DownloadURL=evil), nv_entry())
    assert drivers.parse_nvidia_lookup(answer).url == NV_URL
    assert nv_url("621.24") == NV_URL


# ----------------------------------------------------- 2. TOCTOU


def test_download_dir_is_new_and_random_every_time(tmp_path: Path, monkeypatch) -> None:
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / drivers.DOWNLOAD_SUBDIR).mkdir()  # pre-created by someone else: never used
    (temp / drivers.DOWNLOAD_SUBDIR / "planted.exe").write_bytes(b"MZ")
    monkeypatch.setattr(drivers.tempfile, "gettempdir", lambda: str(temp))
    first, second = drivers.download_dir(), drivers.download_dir()
    assert first != second and first.parent == second.parent == temp
    assert first.name != drivers.DOWNLOAD_SUBDIR and not list(first.iterdir())


def test_download_dir_refuses_reparse_points(tmp_path: Path, monkeypatch) -> None:
    temp, app = tmp_path / "temp", tmp_path / "app"
    temp.mkdir()
    monkeypatch.setattr(drivers.tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(drivers, "_is_reparse_point", lambda p: Path(p) == temp)
    assert drivers.download_dir(app).parent == app
    assert list(temp.iterdir()) == []
    monkeypatch.setattr(drivers, "_is_reparse_point", lambda _p: True)
    with pytest.raises(DriverError, match="prawem zapisu"):
        drivers.download_dir(app)


def test_locked_file_cannot_be_changed_but_can_be_read(tmp_path: Path) -> None:
    exe = tmp_path / "dir" / "a.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ original")
    with drivers.lock_file(exe):
        assert exe.read_bytes() == b"MZ original"
        for attempt in (
            lambda: exe.write_bytes(b"MZ evil"),
            lambda: exe.open("ab").close(),
            exe.unlink,
            lambda: exe.rename(tmp_path / "dir" / "b.exe"),
            lambda: exe.parent.rename(tmp_path / "moved"),
        ):
            with pytest.raises(OSError):
                attempt()
    exe.write_bytes(b"MZ changed")  # released
    assert exe.read_bytes() == b"MZ changed"


def test_lock_file_refuses_missing_file(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        drivers.lock_file(tmp_path / "missing.exe")


def test_locked_exe_still_starts_and_reads_itself() -> None:
    # The interpreter is the only program the test guard lets run; locking its exe is
    # harmless (read sharing stays open for everyone).
    exe = Path(BASE_PYTHON)
    lines: list[str] = []
    with drivers.lock_file(exe):
        result = drivers.run_process(
            [str(exe), "-c", "import sys; print(open(sys.executable, 'rb').read(2))"],
            on_line=lines.append,
            on_replace_last=lines.append,
            cancel_event=None,
            timeout=60,
        )
    assert result == ProcessResult(0) and lines == ["b'MZ'"]


def test_file_is_locked_during_signature_check_and_installation(run_installer, tmp_path) -> None:
    attempts: list[str] = []

    def try_to_swap() -> None:
        exe = next((tmp_path / "work").glob("*.exe"))
        for name, action in (
            ("write", lambda: exe.write_bytes(b"MZ evil")),
            ("rename", lambda: exe.rename(exe.with_name("x.exe"))),
            ("delete", exe.unlink),
        ):
            try:
                action()
                attempts.append(f"{name} ALLOWED")
            except OSError:
                attempts.append(f"{name} blocked")

    fake = FakeRun(ps_lines=valid_signature_lines(SUBJECTS["nvidia"]), on_install=try_to_swap)
    result = run_installer(fake)
    assert result.ok
    assert attempts == ["write blocked", "rename blocked", "delete blocked"]
    assert leftovers(tmp_path) == []  # removed after the lock was released


def test_file_changed_between_download_and_lock_is_never_run(
    run_installer, tmp_path, monkeypatch
) -> None:
    real_lock = drivers.lock_file

    def tamper_then_lock(path: Path) -> drivers.FileLock:
        path.write_bytes(b"MZ" + b"\xcc" * (path.stat().st_size - 2))  # same size
        return real_lock(path)

    monkeypatch.setattr(drivers, "lock_file", tamper_then_lock)
    fake = FakeRun(ps_lines=valid_signature_lines(SUBJECTS["nvidia"]))
    with pytest.raises(DriverError, match="zmienił się po pobraniu"):
        run_installer(fake)
    assert fake.verified == [] and fake.executed == [] and leftovers(tmp_path) == []


def test_lock_failure_means_no_run(run_installer, tmp_path, monkeypatch) -> None:
    def cannot_lock(_path: Path) -> drivers.FileLock:
        raise PermissionError(32, "in use")

    monkeypatch.setattr(drivers, "lock_file", cannot_lock)
    fake = FakeRun(ps_lines=valid_signature_lines(SUBJECTS["nvidia"]))
    with pytest.raises(DriverError, match="zablokować"):
        run_installer(fake)
    assert fake.executed == [] and leftovers(tmp_path) == []


def test_download_returns_sha256_of_received_bytes(tmp_path: Path, small: None) -> None:
    payload = installer_bytes()
    got = drivers.download_verified(
        release(), tmp_path, session=FakeSession({NV_URL: exe_response(payload)})
    )
    assert got.sha256 == hashlib.sha256(payload).hexdigest() and got.size == len(payload)
    assert drivers.sha256_file(got.path) == got.sha256


# ----------------------------------------------------- 3. system programs by full path


def test_system32_path(tmp_path: Path) -> None:
    for name in ("shutdown.exe", "powercfg.exe", plat.POWERSHELL_EXE):
        path = Path(plat.system32_path(name))
        assert path.is_absolute() and path.is_file()
        assert path.parent.parts[: len(plat.windows_system_dir().parts)] == (
            plat.windows_system_dir().parts
        )
    assert plat.windows_system_dir().name.lower() == "system32"
    with pytest.raises(FileNotFoundError):
        plat.system32_path("no-such-program-winstaller.exe")


def test_no_system_program_is_started_by_bare_name(monkeypatch, tmp_path) -> None:
    started: list[str] = []

    def record(args: Any, **_kw: Any) -> Any:
        started.append(list(args)[0])
        raise FileNotFoundError(2, "stop here")

    monkeypatch.setattr(actions, "run_command", record)
    monkeypatch.setattr(actions, "stream_command", lambda _ctx, args, **_k: record(args))
    monkeypatch.setattr(actions, "desktop_dir", lambda: tmp_path)
    monkeypatch.setattr(winget, "stream_process", record)
    monkeypatch.setattr(winget, "is_available", lambda: False)
    from tests.support import make_ctx

    ctx = make_ctx(tmp_path)[0]
    for func in (actions.reboot_to_bios, actions.battery_report, actions.remove_bloat):
        with pytest.raises(actions.ActionError):
            func(ctx)
    winget.ensure_installed()
    started.append(drivers.powershell_args("x")[0])
    assert started and all(Path(s).is_absolute() for s in started), started


def test_missing_powershell_is_a_driver_error(monkeypatch) -> None:
    def missing(_name: str) -> str:
        raise FileNotFoundError(2, "missing")

    monkeypatch.setattr(plat, "system32_path", missing)
    with pytest.raises(DriverError, match="weryfikacji podpisu"):
        drivers.verify_signature("a.exe", "O=NVIDIA Corporation")


# ----------------------------------------------------- 4. signer subject


@pytest.mark.parametrize(
    ("subject", "signer", "ok"),
    [
        (SUBJECTS["nvidia"], "O=NVIDIA Corporation", True),
        (SUBJECTS["intel"], "O=Intel Corporation", True),
        (SUBJECTS["amd"], "O=Advanced Micro Devices", True),
        ("CN=a, O=Advanced Micro Devices, C=US", "O=Advanced Micro Devices", True),
        ('O="Advanced Micro Devices, Inc", C=US', "O=Advanced Micro Devices", False),
        ('O="Advanced Micro Devices, Inc.", C=US', "O=Advanced Micro Devices", True),
        ("O=NVIDIA Corporation, C=XX", "O=NVIDIA Corporation", False),
        ("O=NVIDIA Corporation", "O=NVIDIA Corporation", False),  # C=US required
        ("O=NVIDIA Corporation, C=US, C=US", "O=NVIDIA Corporation", False),
        ("O=NVIDIA Corporation, C=us", "O=NVIDIA Corporation", False),
        ("O=NVIDIA CORPORATION, C=US", "O=NVIDIA Corporation", False),
        ('O="NVIDIA Corporation, Evil Ltd", C=US', "O=NVIDIA Corporation", False),
        ("O=NVIDIA Corporation, O=NVIDIA Corporation, C=US", "O=NVIDIA Corporation", False),
        ("O=NVIDIA Corporation, C=US", "CN=NVIDIA Corporation", False),  # only O= checks
    ],
)
def test_signer_is_an_exact_legal_name(subject: str, signer: str, ok: bool) -> None:
    assert drivers.subject_matches(subject, signer) is ok


def test_signer_names_cover_every_vendor() -> None:
    for spec in drivers.VENDORS.values():
        assert spec.signer in drivers.SIGNER_NAMES


# ----------------------------------------------------- 5. PowerShell literal


@pytest.mark.parametrize("quote", ["'", "‘", "’", "‚", "‛"])
def test_every_powershell_quote_is_doubled(quote: str) -> None:
    assert drivers.ps_single_quoted(f"a{quote}b") == f"'a{quote}{quote}b'"


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\x00b", "a\x1bb"])
def test_control_characters_are_refused(bad: str) -> None:
    with pytest.raises(DriverError):
        drivers.build_signature_script(bad)


# ----------------------------------------------------- 6. parsers never leak other errors


@pytest.mark.parametrize(
    "exc",
    [
        MemoryError(),
        RecursionError(),
        UnicodeDecodeError("utf-8", b"", 0, 1, "x"),
        KeyError("x"),
        IndexError(),
    ],
)
@pytest.mark.parametrize(
    "parse",
    [
        drivers.parse_nvidia_lookup,
        drivers.parse_intel_dsa,
        lambda raw: drivers.parse_signature_output([raw]),
    ],
)
def test_parser_contract(monkeypatch, exc: BaseException, parse: Callable[[str], Any]) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise exc

    monkeypatch.setattr(drivers.json, "loads", boom)
    with pytest.raises(DriverError):
        parse('{"Status": "Valid"}')


def test_unsupported_zip_compression_is_a_driver_error() -> None:
    raw = bytearray(dsa_zip(b"[]", compression=zipfile.ZIP_STORED))
    name = b"data/software-configurations.json"
    for header in (b"PK\x03\x04", b"PK\x01\x02"):
        pos = raw.find(header)
        while pos >= 0:
            offset = 8 if header == b"PK\x03\x04" else 10  # compression method field
            if name in raw[pos : pos + 100]:
                raw[pos + offset : pos + offset + 2] = (99).to_bytes(2, "little")
            pos = raw.find(header, pos + 4)
    with pytest.raises(DriverError):
        drivers.read_intel_dsa_zip(bytes(raw))


def test_intel_plan_b_after_any_dsa_failure(monkeypatch) -> None:
    def broken(*_a: Any, **_k: Any) -> Any:
        raise MemoryError

    archive = dsa_zip(b"[]")
    monkeypatch.setattr(drivers.zipfile, "ZipFile", broken)
    session = FakeSession(
        {
            drivers.INTEL_DSA_URL: FakeResponse(200, archive),
            drivers.INTEL_PAGE_URL: FakeResponse(200, data("intel_page.html")),
        }
    )
    assert drivers.resolve("intel", session).sha512


# ----------------------------------------------------- 7. file names, stale files


@pytest.mark.parametrize("stem", ["CON", "con", "Nul", "COM0", "COM9", "LPT0", "lpt5", "AUX"])
def test_reserved_device_names(stem: str) -> None:
    name = drivers.sanitize_filename(f"https://x/{stem}.exe")
    assert name.lower().endswith(".exe") and name.split(".")[0].upper() not in {stem.upper()}
    assert drivers.sanitize_filename("https://x/CONSOLE.exe") == "CONSOLE.exe"


def test_stale_part_and_target_are_removed_before_download(tmp_path: Path, small: None) -> None:
    name = NV_URL.rsplit("/", 1)[1]
    (tmp_path / name).write_bytes(b"MZ stale")
    (tmp_path / f"{name}.part").write_bytes(b"MZ stale part")
    with pytest.raises(DriverError):
        drivers.download(release(), tmp_path, session=FakeSession({NV_URL: FakeResponse(503)}))
    assert list(tmp_path.iterdir()) == []


# ----------------------------------------------------- 8. Job Object


def grandchild(seconds: float, code: int = 0) -> str:
    """Parent that starts a child sleeping ``seconds`` and exits at once with ``code``."""
    return (
        "import subprocess, sys; "
        f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({seconds})']); "
        f"print('GRANDCHILD', c.pid, flush=True); sys.exit({code})"
    )


def run_tree(tree: TreeRun, script: str, **kw: Any) -> tuple[ProcessResult, float]:
    t0 = time.monotonic()
    result = drivers.run_process(
        [BASE_PYTHON, "-c", script],
        on_line=kw.pop("on_line", tree.on_line),
        on_replace_last=tree.on_line,
        cancel_event=kw.pop("cancel_event", None),
        timeout=kw.pop("timeout", 60),
    )
    return result, time.monotonic() - t0


def test_short_lived_child_is_waited_for(tree: TreeRun) -> None:
    # 6 s: longer than stream_process's own 3 s wait for the pipe after the main exit
    result, elapsed = run_tree(tree, grandchild(6.0, code=14))
    assert result == ProcessResult(14)  # exit code of the main process
    assert tree.pids and wait_dead(tree.pids[0], 0.1), "returned before the child ended"
    assert elapsed >= 5.0
    waiting = [x for x in tree.lines if "czekam na zakończenie" in x]
    assert len(waiting) == 1 and "python" in waiting[0].lower()


def test_child_outliving_the_grace_is_left_running(tree: TreeRun, monkeypatch) -> None:
    monkeypatch.setattr(drivers, "POST_EXIT_GRACE_SECONDS", 1.0)
    result, elapsed = run_tree(tree, grandchild(60))
    assert result == ProcessResult(0, left_running=True)
    assert elapsed < 15
    assert not wait_dead(tree.pids[0], 0.5), "the child must NOT be killed (job closed)"
    assert any(x.startswith("Ostrzeżenie: procesy instalatora nadal działają") for x in tree.lines)


def test_cancel_while_waiting_kills_the_children(tree: TreeRun, monkeypatch) -> None:
    monkeypatch.setattr(drivers, "POST_EXIT_GRACE_SECONDS", 60.0)
    cancel = threading.Event()

    def on_line(text: str) -> None:
        tree.on_line(text)
        if "czekam na zakończenie" in text:
            cancel.set()

    result, elapsed = run_tree(tree, grandchild(60), on_line=on_line, cancel_event=cancel)
    assert result.cancelled and not result.timed_out and result.returncode == -1
    assert wait_dead(tree.pids[0], 3.0) and elapsed < 15


def test_overall_timeout_while_waiting_kills_the_children(tree: TreeRun, monkeypatch) -> None:
    monkeypatch.setattr(drivers, "POST_EXIT_GRACE_SECONDS", 60.0)
    result, elapsed = run_tree(tree, grandchild(60), timeout=3.0)
    assert result.timed_out and not result.cancelled
    assert wait_dead(tree.pids[0], 3.0) and elapsed < 15


def test_job_is_never_kill_on_close() -> None:
    assert "KILL_ON_JOB_CLOSE =" not in inspect.getsource(drivers)
    assert "SetInformationJobObject" not in inspect.getsource(drivers)


def test_children_left_running_is_still_a_success(run_installer, tmp_path) -> None:
    lines: list[str] = []
    fake = FakeRun(
        ps_lines=valid_signature_lines(SUBJECTS["nvidia"]),
        installer_result=ProcessResult(0, left_running=True),
    )
    result = run_installer(fake, log=lines.append)
    assert result.ok and leftovers(tmp_path) == []


def test_process_is_suspended_until_it_is_in_the_job(monkeypatch, tmp_path: Path) -> None:
    marker = tmp_path / "started.txt"
    seen: list[bool] = []
    real_job_for = drivers._job_for

    def slow_job_for(proc: Any) -> Any:
        time.sleep(0.5)  # a running child would have written the marker by now
        seen.append(marker.exists())
        return real_job_for(proc)

    monkeypatch.setattr(drivers, "_job_for", slow_job_for)
    result = drivers.run_process(
        [BASE_PYTHON, "-c", f"open(r'{marker}', 'w').close()"],
        on_line=lambda _t: None,
        on_replace_last=lambda _t: None,
        cancel_event=None,
        timeout=60,
    )
    assert result.returncode == 0 and seen == [False] and marker.exists()


def test_resume_failure_stops_the_process(monkeypatch) -> None:
    def no_resume(_proc: Any) -> None:
        raise OSError("cannot resume")

    monkeypatch.setattr(drivers, "_resume", no_resume)
    t0 = time.monotonic()
    with pytest.raises(OSError, match="cannot resume"):
        drivers.run_process(
            [BASE_PYTHON, "-c", "print('never')"],
            on_line=lambda _t: None,
            on_replace_last=lambda _t: None,
            cancel_event=None,
            timeout=60,
        )
    assert time.monotonic() - t0 < 10


def test_private_popen_handle_is_not_used() -> None:
    assert not re.search(r"\._handle\b", inspect.getsource(drivers))


def test_without_kill_tree_nothing_is_suspended(monkeypatch) -> None:
    flags: list[int] = []
    real = winget.stream_process

    def spy(args: Any, **kw: Any) -> Any:
        flags.append(kw.get("creationflags", 0))
        return real(args, **kw)

    monkeypatch.setattr(winget, "stream_process", spy)
    for kill_tree in (False, True):
        drivers.run_process(
            [BASE_PYTHON, "-c", "pass"],
            on_line=lambda _t: None,
            on_replace_last=lambda _t: None,
            cancel_event=None,
            timeout=30,
            kill_tree=kill_tree,
        )
    assert flags == [0, drivers._CREATE_SUSPENDED]


@pytest.fixture
def tree() -> Any:
    run = TreeRun()
    yield run
    for pid in run.pids:
        kill_pid(pid)


# ----------------------------------------------------- 9. hard size limit


@pytest.mark.parametrize("length", [None, "auto"])
def test_hard_download_limit(tmp_path: Path, small: None, monkeypatch, length: Any) -> None:
    monkeypatch.setattr(drivers, "MAX_DOWNLOAD_BYTES", 5 * SMALL_MIN)
    payload = installer_bytes(8 * SMALL_MIN)
    response = exe_response(payload, length=length)
    with pytest.raises(DriverError, match="podejrzanie duży"):
        drivers.download(release(), tmp_path, session=FakeSession({NV_URL: response}))
    assert list(tmp_path.iterdir()) == []
    assert response.closed


def test_real_limit_is_4_gib() -> None:
    assert drivers.MAX_DOWNLOAD_BYTES == 4 * 1024**3


# ----------------------------------------------------- 10. AMD version in the URL


def amd_session(notes: bytes, page: bytes | None = None) -> FakeSession:
    return FakeSession(
        {
            drivers.AMD_PRODUCT_URL: FakeResponse(200, page or data("amd_product_page.html")),
            AMD_NOTES: FakeResponse(200, notes),
        }
    )


def test_amd_notes_link_for_another_version_falls_back() -> None:
    other = AMD_URL.replace("26.11.2", "26.11.1")
    notes = data("amd_relnotes.html").replace(AMD_URL.encode(), other.encode())
    rel = drivers.resolve("amd", amd_session(notes))
    assert rel.url == AMD_B_URL and rel.notes


def test_amd_notes_without_title_version_are_still_tied_to_the_version() -> None:
    other = AMD_URL.replace("26.11.2", "26.10.1")
    notes = (
        data("amd_relnotes.html")
        .replace(b"Adrenalin Edition 26.11.2 Driver Release", b"Adrenalin Edition Driver Release")
        .replace(AMD_URL.encode(), other.encode())
    )
    rel = drivers.resolve("amd", amd_session(notes))
    assert rel.url == AMD_B_URL  # the stale universal link was not taken


def test_amd_fallback_package_must_also_match() -> None:
    page = data("amd_product_page.html").replace(
        b"edition-26.11.2-win11-b.exe", b"edition-26.10.1-win11-b.exe"
    )
    with pytest.raises(DriverError, match="AMD"):
        drivers.resolve("amd", amd_session(b"<html></html>", page))


@pytest.mark.parametrize(
    ("url", "version", "ok"),
    [
        (AMD_URL, "26.11.2", True),
        (AMD_URL, "26.11.20", False),
        (AMD_URL.replace("26.11.2", "26.11.20"), "26.11.2", False),
        (AMD_URL.replace("26.11.2", "126.11.2"), "26.11.2", False),
        (AMD_URL.replace("26.11.2", "26.11.2.1"), "26.11.2", False),
        ("https://drivers.amd.com/26.11.2/x.exe", "26.11.2", False),  # only the file name
        (AMD_URL, "x", False),
    ],
)
def test_url_has_version(url: str, version: str, ok: bool) -> None:
    assert drivers.url_has_version(url, version) is ok


# ----------------------------------------------------- 11. PowerShell exit code


def test_nonzero_powershell_exit_blocks_the_installer(run_installer, tmp_path) -> None:
    fake = FakeRun(ps_lines=valid_signature_lines(SUBJECTS["nvidia"]), ps_result=ProcessResult(1))
    with pytest.raises(DriverError, match="PowerShell"):
        run_installer(fake)
    assert fake.executed == [] and leftovers(tmp_path) == []


# ----------------------------------------------------- 12. progress lines


def test_first_progress_line_has_no_speed_and_finish_shows_the_end() -> None:
    now = [0.0]
    log: list[str] = []
    replaced: list[str] = []
    rep = drivers.ProgressReporter(log.append, replaced.append, clock=lambda: now[0])
    rep(1024**2, None)
    now[0] += 0.05
    rep(5 * 1024**2, None)  # throttled
    assert log == ["Pobieranie: 1.0 MB"] and replaced == []
    rep.finish()
    assert replaced == ["Pobieranie: 5.0 MB · 100.0 MB/s"]
    rep.finish()  # nothing new: no duplicate line
    assert len(replaced) == 1
    assert "MB/s" not in drivers.format_progress(10, 100, None)


def test_install_latest_shows_the_final_progress_without_total(run_installer, tmp_path) -> None:
    payload = installer_bytes(seed=3)
    lines: list[str] = []
    fake = FakeRun(ps_lines=valid_signature_lines(SUBJECTS["nvidia"]))
    run_installer(
        fake,
        payload=payload,
        routes={DOWNLOAD_URL["nvidia"]: exe_response(payload, length=None)},
        log=lines.append,
    )
    progress = [x for x in lines if x.startswith("Pobieranie:")]
    assert progress and "MB/s" not in progress[0]


def test_reporter_without_any_update_finishes_quietly() -> None:
    log: list[str] = []
    drivers.ProgressReporter(log.append, log.append).finish()
    assert log == []


# ----------------------------------------------------- 13. Intel install flags


def test_intel_flags_do_not_follow_dsa_install_options() -> None:
    arc = dsa_arc()
    assert arc["AllowSilentInstallation"] is False and arc["InstallOptions"] == "--nouninstall"
    assert drivers.parse_intel_dsa(dsa_bytes([arc])).url.endswith(".exe")
    assert drivers.VENDORS["intel"].install_args == ("-s", "--terminateProcesses")


def test_sample_json_is_valid() -> None:
    assert json.loads(dsa_bytes([dsa_arc()]).decode("utf-8-sig"))
