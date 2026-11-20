"""GPU drivers: signature check, installer run (Job Object, timeout, cancel), exit codes
and the whole ``install_latest`` flow with every external effect faked.

The only real processes are harmless ``python -c`` children (tree-kill tests).
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import hashlib
import json
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tests.drivers_support import (
    AMD_NOTES,
    AMD_URL,
    INTEL_URL,
    NV_URL,
    SMALL_MIN,
    SUBJECTS,
    FakeResponse,
    FakeSession,
    data,
    exe_response,
    installer_bytes,
    valid_signature_lines,
)
from winstaller.core import drivers, winget
from winstaller.core.drivers import DriverCancelled, DriverError, ProcessResult

# ------------------------------------------------- PowerShell script (static)

_PS_QUOTES = "'\u2018\u2019\u201a\u201b"  # PowerShell treats all of these as single quotes


def ps_literal_path(script: str) -> str:
    """Value of the single-quoted string after ``-LiteralPath`` by PowerShell's rules;
    asserts nothing else follows it on that line (no injected statement)."""
    start = script.index("-LiteralPath ") + len("-LiteralPath ")
    assert script[start] in _PS_QUOTES
    i, value = start + 1, []
    while True:
        ch = script[i]
        if ch in _PS_QUOTES:
            if i + 1 < len(script) and script[i + 1] in _PS_QUOTES:
                value.append(script[i + 1])
                i += 2
                continue
            break
        value.append(ch)
        i += 1
    rest = script[i + 1 : script.index("\n", i)]
    assert rest.strip() == "", f"text after the path literal: {rest!r}"
    return "".join(value)


def decode_script(args: Sequence[str]) -> str:
    assert args[-2] == "-EncodedCommand"
    return base64.b64decode(args[-1]).decode("utf-16-le")


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\Jan Kowalski\AppData\Local\Temp\winstaller-drivers\621.24.exe",
        r"C:\Temp\it's\a.exe",
        r"C:\Temp\''\a.exe",
        r"C:\Temp\$(Remove-Item C:\x)\a.exe",
        r"C:\Temp\`n`$env:x\a.exe",
        r"C:\Temp\a'; Stop-Computer; '\a.exe",
        r"C:\Użytkownicy\Zażółć gęślą jaźń\a.exe",
        r"C:\Temp\[abc]*?\a.exe",
        r'C:\Temp\"q"\a.exe',
    ],
)
def test_signature_script_quotes_the_path(path: str) -> None:
    script = drivers.build_signature_script(path)
    assert ps_literal_path(script) == path
    assert script.startswith(winget.POWERSHELL_UTF8_PREAMBLE)
    assert "Get-AuthenticodeSignature -LiteralPath" in script
    args = drivers.powershell_args(script)
    assert decode_script(args) == script
    assert "-NoProfile" in args and "-NonInteractive" in args


@pytest.mark.parametrize("quote", ["\u2018", "\u2019", "\u201a", "\u201b"])
def test_signature_script_quotes_typographic_apostrophes(quote: str) -> None:
    path = rf"C:\Users\O{quote}Brien{quote}; Write-Output INJECTED; {quote}\a.exe"
    assert ps_literal_path(drivers.build_signature_script(path)) == path


def test_powershell_is_started_by_absolute_system_path() -> None:
    exe = drivers.powershell_args("x")[0]
    assert Path(exe).is_absolute()
    assert "system32" in exe.lower()


# ------------------------------------------------------ signature parsing


def sig_json(**fields: Any) -> str:
    base = {"Status": "Valid", "StatusMessage": "", "Subject": SUBJECTS["nvidia"], "Thumbprint": ""}
    base.update(fields)
    return json.dumps(base)


class TestParseSignatureOutput:
    def test_valid(self) -> None:
        info = drivers.parse_signature_output(valid_signature_lines(SUBJECTS["amd"]))
        assert info.status == "Valid" and info.subject == SUBJECTS["amd"]
        assert info.thumbprint == "AB" * 20 and info.signature_type == "Authenticode"

    def test_last_json_line_wins_and_noise_is_skipped(self) -> None:
        lines = [
            "WARNING: something",
            sig_json(Status="NotSigned"),
            "{not json",
            "#< CLIXML",
            '<Objs Version="1.1.0.1"><S S="Error">x</S></Objs>',
            sig_json(Status="Valid"),
            "",
            "[1, 2]",
            '{"NoStatus": 1}',
            '{"Status": 5}',
        ]
        assert drivers.parse_signature_output(lines).status == "Valid"

    def test_null_fields(self) -> None:
        info = drivers.parse_signature_output(['{"Status": null, "Subject": null}'])
        assert info.status == "" and info.subject == ""

    @pytest.mark.parametrize(
        "lines",
        [[], [""], ["garbage"], ["{"], ["[]"], ['{"Status": 1}'], ["\x00\x01"], ["null"]],
    )
    def test_garbage(self, lines: list[str]) -> None:
        with pytest.raises(DriverError, match="odczytać"):
            drivers.parse_signature_output(lines)

    def test_bom_before_json_is_not_accepted(self) -> None:
        # Documented behaviour: a UTF-8 BOM in front of the line makes the result
        # unreadable (fail-safe). Real PowerShell 5.1 output checked 2026-11-20: no BOM.
        with pytest.raises(DriverError):
            drivers.parse_signature_output(["\ufeff" + sig_json()])


class TestDistinguishedName:
    @pytest.mark.parametrize(
        ("subject", "expected"),
        [
            ("CN=x, O=Y, C=US", [("CN", "x"), ("O", "Y"), ("C", "US")]),
            (
                'CN="Advanced Micro Devices, Inc.", O="Advanced Micro Devices, Inc."',
                [("CN", "Advanced Micro Devices, Inc."), ("O", "Advanced Micro Devices, Inc.")],
            ),
            ('O="say ""hi"", ok"', [("O", 'say "hi", ok')]),
            ("O=A\\, B, C=D", [("O", "A, B"), ("C", "D")]),
            ("CN=a + O=b; C=c", [("CN", "a"), ("O", "b"), ("C", "c")]),
            ("o = lower ", [("O", "lower")]),
            ("", []),
            ("no equals sign", []),
            ('O="unterminated', [("O", "unterminated")]),
        ],
    )
    def test_parse(self, subject: str, expected: list[tuple[str, str]]) -> None:
        assert drivers.parse_distinguished_name(subject) == expected


class TestSubjectMatches:
    @pytest.mark.parametrize("vendor", ["nvidia", "amd", "intel"])
    def test_real_vendor_subjects(self, vendor: str) -> None:
        assert drivers.subject_matches(SUBJECTS[vendor], drivers.VENDORS[vendor].signer)
        others = {v for v in SUBJECTS if v != vendor}
        for other in others:
            assert not drivers.subject_matches(SUBJECTS[other], drivers.VENDORS[vendor].signer)

    @pytest.mark.parametrize(
        "subject",
        [
            'CN="O=NVIDIA Corporation", O=Evil Ltd',
            "CN=O=NVIDIA Corporation, O=Evil",
            "O=NVIDIA Corporation Fake, C=US",
            "O=NVIDIA Corporations",
            "O=Not NVIDIA Corporation",
            "OU=NVIDIA Corporation, O=Evil",
            "CN=NVIDIA Corporation",
            'O="NVIDIA Corporation Inc", C=US',
            "O=NVIDIA\\, Corporation",
            "O=NVIDIA  Corporation",
            "",
            "garbage",
        ],
    )
    def test_spoofed_subjects_are_rejected(self, subject: str) -> None:
        assert not drivers.subject_matches(subject, "O=NVIDIA Corporation")

    def test_values_are_case_sensitive_attribute_names_are_not(self) -> None:
        # Values are compared exactly (case-sensitive); attribute names are not (DN rules).
        assert not drivers.subject_matches("O=nvidia corporation, C=US", "O=NVIDIA Corporation")
        assert drivers.subject_matches("o=NVIDIA Corporation, c=US", "O=NVIDIA Corporation")

    def test_two_o_attributes_are_rejected(self) -> None:
        # Exactly one O= attribute is required: a second one makes the subject ambiguous.
        assert not drivers.subject_matches(
            "O=Evil, O=NVIDIA Corporation, C=US", "O=NVIDIA Corporation"
        )

    def test_amd_suffix_rule(self) -> None:
        amd = drivers.VENDORS["amd"].signer
        assert drivers.subject_matches('O="Advanced Micro Devices, Inc.", C=US', amd)
        assert drivers.subject_matches("O=Advanced Micro Devices, C=US", amd)
        assert not drivers.subject_matches('O="Advanced Micro Devices Evil, Inc."', amd)

    @pytest.mark.parametrize(
        ("subject", "signer"),
        [
            ('CN=x, O="NVIDIA Corporation, Evil Ltd", C=XX', "O=NVIDIA Corporation"),
            ('CN=x, O="Intel Corporation, Totally Legit LLC"', "O=Intel Corporation"),
            ('CN=x, O="Advanced Micro Devices, Evil GmbH"', "O=Advanced Micro Devices"),
        ],
    )
    def test_prefix_with_comma_is_not_enough(self, subject: str, signer: str) -> None:
        assert not drivers.subject_matches(subject, signer)

    @pytest.mark.parametrize("expected", ["", "O=", "=x", "NVIDIA Corporation"])
    def test_bad_expectation_never_matches(self, expected: str) -> None:
        assert not drivers.subject_matches(SUBJECTS["nvidia"], expected)


# --------------------------------------------------- verify_signature (faked)


@dataclass
class FakeRun:
    """Replacement for ``drivers.run_process``: PowerShell gets ``ps_lines`` back,
    anything else is the installer and gets ``installer_result``."""

    ps_lines: list[str] = field(default_factory=list)
    ps_result: ProcessResult | BaseException = field(default_factory=lambda: ProcessResult(0))
    installer_result: ProcessResult | BaseException = field(
        default_factory=lambda: ProcessResult(0)
    )
    calls: list[dict[str, Any]] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    executed: list[tuple[str, str]] = field(default_factory=list)  # (path, sha256 at start)
    events: list[str] | None = None
    on_install: Callable[[], None] | None = None

    def __call__(
        self,
        args: Sequence[str],
        *,
        on_line: Callable[[str], None],
        on_replace_last: Callable[[str], None],
        cancel_event: threading.Event | None,
        timeout: float,
        cwd: Path | None = None,
        kill_tree: bool = True,
    ) -> ProcessResult:
        self.calls.append(
            {"args": list(args), "timeout": timeout, "cwd": cwd, "kill_tree": kill_tree}
        )
        if Path(args[0]).name.lower() == "powershell.exe":
            path = ps_literal_path(decode_script(args))
            self.verified.append(path)
            if self.events is not None:
                self.events.append("verify")
            if isinstance(self.ps_result, BaseException):
                raise self.ps_result
            for line in self.ps_lines:
                on_line(line)
            return self.ps_result
        exe = Path(args[0])
        digest = hashlib.sha256(exe.read_bytes()).hexdigest() if exe.exists() else "missing"
        self.executed.append((args[0], digest))
        if self.events is not None:
            self.events.append("install")
        if self.on_install is not None:
            self.on_install()
        if isinstance(self.installer_result, BaseException):
            raise self.installer_result
        on_line("installer output")
        return self.installer_result


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun(ps_lines=valid_signature_lines(SUBJECTS["nvidia"]))
    monkeypatch.setattr(drivers, "run_process", fake)
    return fake


class TestVerifySignature:
    def test_valid(self, fake_run: FakeRun, tmp_path: Path) -> None:
        exe = tmp_path / "a.exe"
        info = drivers.verify_signature(exe, "O=NVIDIA Corporation", timeout=7)
        assert info.subject == SUBJECTS["nvidia"]
        assert fake_run.verified == [str(exe)]
        assert fake_run.calls[0]["timeout"] == 7 and fake_run.calls[0]["kill_tree"] is False

    @pytest.mark.parametrize(
        ("lines", "match"),
        [
            ([sig_json(Subject=SUBJECTS["amd"])], "innego wydawcę"),
            ([sig_json(Subject="")], "innego wydawcę"),
            ([sig_json(Status="NotSigned", Subject="")], "NotSigned"),
            ([sig_json(Status="HashMismatch", StatusMessage="changed")], "HashMismatch \\(changed"),
            ([sig_json(Status="UnknownError")], "UnknownError"),
            ([sig_json(Status="NotTrusted")], "NotTrusted"),
            ([sig_json(Status="valid")], "valid"),  # exact, case-sensitive status
            ([sig_json(Status="")], "Nie udało się sprawdzić"),
            ([], "odczytać"),
            (["Get-AuthenticodeSignature : File not found"], "odczytać"),
        ],
    )
    def test_rejections(self, fake_run: FakeRun, lines: list[str], match: str) -> None:
        fake_run.ps_lines = lines
        with pytest.raises(DriverError, match=match) as info:
            drivers.verify_signature("C:/x/a.exe", "O=NVIDIA Corporation")
        assert not isinstance(info.value, DriverCancelled)

    def test_timeout(self, fake_run: FakeRun) -> None:
        fake_run.ps_result = ProcessResult(-1, timed_out=True)
        with pytest.raises(DriverError, match="zbyt długo"):
            drivers.verify_signature("a.exe", "O=NVIDIA Corporation")

    def test_cancel(self, fake_run: FakeRun) -> None:
        fake_run.ps_result = ProcessResult(-1, cancelled=True)
        with pytest.raises(DriverCancelled):
            drivers.verify_signature("a.exe", "O=NVIDIA Corporation")

    def test_powershell_missing(self, fake_run: FakeRun) -> None:
        fake_run.ps_result = FileNotFoundError(2, "powershell.exe")
        with pytest.raises(DriverError, match="weryfikacji podpisu"):
            drivers.verify_signature("a.exe", "O=NVIDIA Corporation")

    def test_nonzero_exit_with_valid_json_is_rejected(self, fake_run: FakeRun) -> None:
        # The PowerShell exit code is checked too: non-zero is a failed check, whatever
        # the JSON says.
        fake_run.ps_result = ProcessResult(1)
        with pytest.raises(DriverError, match="PowerShell"):
            drivers.verify_signature("a.exe", "O=NVIDIA Corporation")


# --------------------------------------------------------- install / exits


@pytest.mark.parametrize(
    ("vendor", "args", "timeout"),
    [
        ("nvidia", ["-s", "-noreboot", "-noeula"], 30 * 60),
        ("amd", ["-INSTALL"], 40 * 60),
        ("intel", ["-s", "--terminateProcesses"], 30 * 60),
    ],
)
def test_install_arguments(fake_run: FakeRun, tmp_path: Path, vendor, args, timeout) -> None:
    exe = tmp_path / "setup.exe"
    result = drivers.install(drivers.VENDORS[vendor], exe, log=print, replace_last=print)
    assert result == ProcessResult(0)
    call = fake_run.calls[0]
    assert call["args"] == [str(exe), *args]
    assert call["timeout"] == timeout and call["cwd"] == tmp_path and call["kill_tree"] is True
    forbidden = {"-clean", "-boot", "-factoryresetinstall", "-f", "--fresh", "-b", "/reboot"}
    assert not {a.lower() for a in call["args"][1:]} & forbidden


def test_install_start_failure(fake_run: FakeRun, tmp_path: Path) -> None:
    fake_run.installer_result = PermissionError(5, "Access is denied")
    with pytest.raises(DriverError, match="Nie udało się uruchomić instalatora") as info:
        drivers.install(drivers.VENDORS["amd"], tmp_path / "a.exe", log=print, replace_last=print)
    assert info.value.vendor_url == drivers.AMD_PAGE


INTEL_OK = {0: False, 2: True, 14: True, 17: True, 18: True}
INTEL_ERRORS = {
    1: "Ogólny",
    5: "platformy",
    6: "zamknięty",
    7: "parametrów",
    8: "nie znalazł sterownika",
    9: "podpisu",
    10: "Dodatki",
    11: "miejsca",
    13: "restart",
    15: "Zamknij programy",
    16: "inna instalacja",
}
UNKNOWN = [-1, -2147024891, 3, 4, 12, 19, 255, 0xFFFFFFFF, 3010, 1641, 2**40]


def test_exit_codes_intel() -> None:
    spec = drivers.VENDORS["intel"]
    for code, reboot in INTEL_OK.items():
        info = spec.describe_exit(code)
        assert info.ok and info.reboot_required is reboot, code
    for code, words in INTEL_ERRORS.items():
        info = spec.describe_exit(code)
        assert not info.ok and words in info.message, code
    assert set(spec.exit_codes) == set(INTEL_OK) | set(INTEL_ERRORS)
    for code in UNKNOWN:
        assert not spec.describe_exit(code).ok, code


def test_exit_codes_nvidia_and_amd() -> None:
    nv, amd = drivers.VENDORS["nvidia"], drivers.VENDORS["amd"]
    assert nv.describe_exit(0).ok and not nv.describe_exit(0).reboot_required
    assert nv.describe_exit(1).ok and nv.describe_exit(1).reboot_required
    assert amd.describe_exit(0).ok
    for code in [*UNKNOWN, 1, 2]:
        if code != 1:
            assert not nv.describe_exit(code).ok
        assert not amd.describe_exit(code).ok
        assert "nieznanym kodem" in amd.describe_exit(code).message


@pytest.mark.parametrize(
    ("code", "text"),
    [
        (0, "kod 0"),
        (14, "kod 14"),
        (65535, "kod 65535"),
        (-1, "kod -1 / 0xFFFFFFFF"),
        (-2147024891, "0x80070005"),
        (0xFFFFFFFF, "0xFFFFFFFF"),
    ],
)
def test_code_text(code: int, text: str) -> None:
    assert text in drivers._code_text(code)


# ---------------------------------------- real processes: Job Object tree kill

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
BASE_PYTHON = getattr(sys, "_base_executable", None) or sys.executable
_SYNC_TERMINATE = 0x00100000 | 0x0001


def wait_dead(pid: int, timeout: float) -> bool:
    handle = _k32.OpenProcess(_SYNC_TERMINATE, False, pid)
    if not handle:
        return True
    try:
        return _k32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
    finally:
        _k32.CloseHandle(handle)


def kill_pid(pid: int) -> None:
    handle = _k32.OpenProcess(_SYNC_TERMINATE, False, pid)
    if handle:
        _k32.TerminateProcess(handle, 1)
        _k32.CloseHandle(handle)


PARENT = textwrap.dedent(
    """
    import subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    print("GRANDCHILD", child.pid, flush=True)
    time.sleep(60)
    """
)


class TreeRun:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.pids: list[int] = []
        self.seen = threading.Event()

    def on_line(self, text: str) -> None:
        self.lines.append(text)
        if text.startswith("GRANDCHILD"):
            self.pids.append(int(text.split()[1]))
            self.seen.set()


@pytest.fixture
def tree() -> Any:
    run = TreeRun()
    yield run
    for pid in run.pids:
        kill_pid(pid)


def test_timeout_kills_installer_and_its_children(tree: TreeRun) -> None:
    t0 = time.monotonic()
    result = drivers.run_process(
        [BASE_PYTHON, "-c", PARENT],
        on_line=tree.on_line,
        on_replace_last=tree.on_line,
        cancel_event=threading.Event(),
        timeout=1.5,
    )
    assert result.timed_out and not result.cancelled and result.returncode == -1
    assert tree.pids, tree.lines
    assert wait_dead(tree.pids[0], 3.0), "grandchild survived the timeout"
    assert time.monotonic() - t0 < 8


def test_cancel_kills_installer_and_its_children(tree: TreeRun) -> None:
    cancel = threading.Event()

    def on_line(text: str) -> None:
        tree.on_line(text)
        if tree.seen.is_set():
            cancel.set()

    result = drivers.run_process(
        [BASE_PYTHON, "-c", PARENT],
        on_line=on_line,
        on_replace_last=on_line,
        cancel_event=cancel,
        timeout=60,
    )
    assert result.cancelled and not result.timed_out
    assert wait_dead(tree.pids[0], 3.0), "grandchild survived the cancel"


def test_without_kill_tree_the_grandchild_survives(tree: TreeRun) -> None:
    """Control for the two tests above: proves the grandchild check can fail."""
    cancel = threading.Event()

    def on_line(text: str) -> None:
        tree.on_line(text)
        if tree.seen.is_set():
            cancel.set()

    result = drivers.run_process(
        [BASE_PYTHON, "-c", PARENT],
        on_line=on_line,
        on_replace_last=on_line,
        cancel_event=cancel,
        timeout=60,
        kill_tree=False,
    )
    assert result.cancelled
    assert not wait_dead(tree.pids[0], 0.3)


def test_normal_exit_code_and_output(tmp_path: Path) -> None:
    lines: list[str] = []
    result = drivers.run_process(
        [BASE_PYTHON, "-c", "import os; print('zażółć', os.getcwd()); raise SystemExit(14)"],
        on_line=lines.append,
        on_replace_last=lines.append,
        cancel_event=None,
        timeout=30,
        cwd=tmp_path,
    )
    assert result == ProcessResult(14)
    assert lines and lines[0].startswith("zażółć") and str(tmp_path) in lines[0]


def test_job_object_failure_only_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drivers, "_job_for", lambda _p: None)
    lines: list[str] = []
    result = drivers.run_process(
        [BASE_PYTHON, "-c", "print('hi')"],
        on_line=lines.append,
        on_replace_last=lines.append,
        cancel_event=None,
        timeout=30,
    )
    assert result.returncode == 0
    assert any("procesy potomne" in x for x in lines) and "hi" in lines


def test_missing_program_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        drivers.run_process(
            [str(tmp_path / "missing.exe")],
            on_line=print,
            on_replace_last=print,
            cancel_event=None,
            timeout=5,
        )


def test_stream_process_without_on_start_is_unchanged() -> None:
    lines: list[str] = []
    outcome = winget.stream_process(
        [BASE_PYTHON, "-c", "print('a'); print('b'); raise SystemExit(3)"],
        on_line=lines.append,
        on_replace_last=lines.append,
    )
    assert outcome == winget.ProcessOutcome(3, False)
    assert lines == ["a", "b"]


def test_stream_process_on_start_failure_stops_the_process() -> None:
    started: list[Any] = []

    def on_start(proc: Any) -> None:
        started.append(proc)
        raise RuntimeError("job failed")

    with pytest.raises(RuntimeError, match="job failed"):
        winget.stream_process(
            [BASE_PYTHON, "-c", "import time; time.sleep(30)"],
            on_line=print,
            on_replace_last=print,
            on_start=on_start,
        )
    assert started and started[0].poll() is not None


# --------------------------------------------- install_latest end-to-end (fake)


def intel_dsa_for(payload: bytes) -> bytes:
    from tests.unit.test_drivers_parsers import dsa_arc, dsa_bytes

    arc = dsa_arc()
    arc["Files"][0]["Hash"] = hashlib.sha1(payload).hexdigest()
    arc["Files"][0]["Size"] = len(payload)
    return dsa_bytes([arc])


def vendor_routes(vendor: str, payload: bytes) -> dict[str, Any]:
    if vendor == "nvidia":
        return {
            drivers.NVIDIA_LOOKUP_URL: FakeResponse(200, data("nvidia_lookup.json")),
            NV_URL: exe_response(payload),
        }
    if vendor == "amd":
        return {
            drivers.AMD_PRODUCT_URL: FakeResponse(200, data("amd_product_page.html")),
            AMD_NOTES: FakeResponse(200, data("amd_relnotes.html")),
            AMD_URL: exe_response(payload),
        }
    return {
        drivers.INTEL_DSA_URL: FakeResponse(200, intel_dsa_for(payload)),
        INTEL_URL: exe_response(payload),
    }


DOWNLOAD_URL = {"nvidia": NV_URL, "amd": AMD_URL, "intel": INTEL_URL}
VENDOR_IDS = ["nvidia", "amd", "intel"]


@dataclass
class Flow:
    vendor: str
    payload: bytes
    session: FakeSession
    run: FakeRun
    events: list[str]
    work: Path
    lines: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)

    def go(self) -> drivers.DriverInstallResult:
        return drivers.install_latest(
            self.vendor,
            log=self.lines.append,
            replace_last=self.replaced.append,
            cancel_event=self.cancel,
            session=self.session,
            work_dir=self.work,
        )

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.work.iterdir())


@pytest.fixture
def flow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[[str], Flow]:
    monkeypatch.setattr(drivers, "MIN_INSTALLER_BYTES", SMALL_MIN)
    monkeypatch.setattr(drivers, "free_space", lambda _p: 10**15)

    def make(vendor: str) -> Flow:
        events: list[str] = []
        payload = installer_bytes(seed=len(vendor))
        session = FakeSession(vendor_routes(vendor, payload))
        real_get, real_head = session.get, session.head

        def get(url: str, **kw: Any) -> Any:
            events.append("download" if url == DOWNLOAD_URL[vendor] else "meta")
            return real_get(url, **kw)

        def head(url: str, **kw: Any) -> Any:
            events.append("meta")
            return real_head(url, **kw)

        session.get, session.head = get, head  # type: ignore[method-assign]
        run = FakeRun(ps_lines=valid_signature_lines(SUBJECTS[vendor]), events=events)
        monkeypatch.setattr(drivers, "run_process", run)
        work = tmp_path / vendor
        work.mkdir()
        return Flow(vendor, payload, session, run, events, work)

    return make


@pytest.mark.parametrize("vendor", VENDOR_IDS)
def test_happy_path(flow: Callable[[str], Flow], vendor: str) -> None:
    f = flow(vendor)
    result = f.go()
    assert result.ok and not result.cancelled and result.returncode == 0
    assert (
        result.version == {"nvidia": "621.24", "amd": "26.11.2", "intel": "32.0.101.9175"}[vendor]
    )
    assert "Zainstalowano sterownik" in result.message and "restart" in result.message
    # order: metadata -> download -> signature -> installer
    assert f.events[-3:] == ["download", "verify", "install"]
    assert set(f.events[:-3]) == {"meta"}
    # the file that ran is exactly the downloaded and verified one, and it is gone now
    assert len(f.run.executed) == 1
    executed, digest = f.run.executed[0]
    assert f.run.verified == [executed]
    assert digest == hashlib.sha256(f.payload).hexdigest()
    assert Path(executed).parent == f.work
    assert f.leftovers() == []
    assert any(x.startswith("Najnowsza wersja:") for x in f.lines)
    assert any(x.startswith("Podpis cyfrowy prawidłowy") for x in f.lines)
    assert any("100%" in x for x in [*f.lines, *f.replaced])
    if vendor == "intel":
        assert "Suma kontrolna zgodna z danymi producenta." in f.lines


def break_resolve(f: Flow) -> None:
    for url in list(f.session.routes):
        if url != DOWNLOAD_URL[f.vendor]:
            f.session.routes[url] = FakeResponse(503)
    for url in (
        drivers.NVIDIA_PROCESS_FIND_URL,
        drivers.AMD_DRIVERS_URL,
        drivers.INTEL_PAGE_URL,
        AMD_NOTES,
    ):
        f.session.routes[url] = OSError("network unreachable")


def foreign_redirect(f: Flow) -> None:
    f.session.routes[DOWNLOAD_URL[f.vendor]] = FakeResponse(
        302, headers={"Location": "https://cdn.evil.example/driver.exe"}
    )


def http_404(f: Flow) -> None:
    f.session.routes[DOWNLOAD_URL[f.vendor]] = FakeResponse(404)


def no_mz(f: Flow) -> None:
    f.session.routes[DOWNLOAD_URL[f.vendor]] = exe_response(b"ZM" + f.payload[2:])


def truncated(f: Flow) -> None:
    f.session.routes[DOWNLOAD_URL[f.vendor]] = exe_response(
        f.payload[: len(f.payload) // 2], length=len(f.payload)
    )


def tampered(f: Flow) -> None:  # same size, other bytes: only a hash or signature can tell
    f.session.routes[DOWNLOAD_URL[f.vendor]] = exe_response(installer_bytes(len(f.payload), 99))
    f.run.ps_lines = [sig_json(Status="HashMismatch", Subject=SUBJECTS[f.vendor])]


def unsigned(f: Flow) -> None:
    f.run.ps_lines = [sig_json(Status="NotSigned", Subject="")]


def other_publisher(f: Flow) -> None:
    f.run.ps_lines = [sig_json(Subject="CN=Evil, O=Evil Ltd, C=XX")]


def signature_tool_broken(f: Flow) -> None:
    f.run.ps_result = OSError("powershell missing")


def signature_garbage(f: Flow) -> None:
    f.run.ps_lines = ["oops"]


def network_drop(f: Flow) -> None:
    f.session.routes[DOWNLOAD_URL[f.vendor]] = exe_response(
        f.payload, fail_after=3, fail_with=TimeoutError("read timed out")
    )


FAILURES = [
    break_resolve,
    foreign_redirect,
    http_404,
    no_mz,
    truncated,
    tampered,
    unsigned,
    other_publisher,
    signature_tool_broken,
    signature_garbage,
    network_drop,
]


@pytest.mark.parametrize("vendor", VENDOR_IDS)
@pytest.mark.parametrize("breaker", FAILURES, ids=[b.__name__ for b in FAILURES])
def test_installer_never_runs_after_a_failed_step(
    flow: Callable[[str], Flow], vendor: str, breaker: Callable[[Flow], None]
) -> None:
    f = flow(vendor)
    breaker(f)
    with pytest.raises(DriverError) as info:
        f.go()
    assert not isinstance(info.value, DriverCancelled)
    assert f.run.executed == [], "installer started although a check failed"
    assert "install" not in f.events
    assert info.value.vendor_url == drivers.VENDORS[vendor].vendor_url
    assert info.value.vendor_url in info.value.user_message()
    assert f.leftovers() == []
    assert info.value.message.strip()


def test_intel_hash_mismatch_blocks_before_signature(flow: Callable[[str], Flow]) -> None:
    # NVIDIA and AMD publish no size/hash; there only host + signature protect.
    f = flow("intel")
    other = installer_bytes(len(f.payload), 7)  # same size, different content
    f.session.routes[INTEL_URL] = exe_response(other)
    with pytest.raises(DriverError, match="SHA1"):
        f.go()
    assert f.run.verified == [] and f.run.executed == [] and f.leftovers() == []


def test_intel_size_mismatch(flow: Callable[[str], Flow]) -> None:
    f = flow("intel")
    f.session.routes[INTEL_URL] = exe_response(f.payload + b"\x00" * 10)
    with pytest.raises(DriverError, match="nie zgadza"):
        f.go()
    assert f.run.executed == [] and f.leftovers() == []


@pytest.mark.parametrize("vendor", VENDOR_IDS)
def test_installer_failure_codes(flow: Callable[[str], Flow], vendor: str) -> None:
    f = flow(vendor)
    f.run.installer_result = ProcessResult(8 if vendor == "intel" else 2)
    with pytest.raises(DriverError, match="nie powiodła się") as info:
        f.go()
    assert info.value.vendor_url
    assert f.leftovers() == []


def test_intel_code_8_message(flow: Callable[[str], Flow]) -> None:
    f = flow("intel")
    f.run.installer_result = ProcessResult(8)
    with pytest.raises(DriverError, match="brak zgodnej grafiki Intel"):
        f.go()


def test_nvidia_code_1_is_success_with_reboot(flow: Callable[[str], Flow]) -> None:
    f = flow("nvidia")
    f.run.installer_result = ProcessResult(1)
    result = f.go()
    assert result.ok and result.reboot_required and result.returncode == 1


@pytest.mark.parametrize("vendor", VENDOR_IDS)
def test_install_timeout_is_an_error(flow: Callable[[str], Flow], vendor: str) -> None:
    f = flow(vendor)
    f.run.installer_result = ProcessResult(-1, timed_out=True)
    with pytest.raises(DriverError, match="nie zakończył pracy") as info:
        f.go()
    minutes = int(drivers.VENDORS[vendor].install_timeout // 60)
    assert f"{minutes} min" in info.value.message
    assert f.leftovers() == []


def test_cancel_during_install(flow: Callable[[str], Flow]) -> None:
    f = flow("amd")
    f.run.installer_result = ProcessResult(-1, cancelled=True)
    result = f.go()
    assert result.cancelled and not result.ok
    assert "Przerwano instalację" in result.message
    assert f.leftovers() == []
    assert any("stanie pośrednim" in x for x in f.lines)


def test_cancel_during_signature_check(flow: Callable[[str], Flow]) -> None:
    f = flow("nvidia")
    f.run.ps_result = ProcessResult(-1, cancelled=True)
    result = f.go()
    assert result.cancelled and f.run.executed == [] and f.leftovers() == []


def test_cancel_during_download(flow: Callable[[str], Flow]) -> None:
    f = flow("intel")
    f.session.routes[INTEL_URL] = exe_response(
        f.payload, on_chunk=lambda n: n == 2 and f.cancel.set()
    )
    result = f.go()
    assert result.cancelled and result.version == "32.0.101.9175"
    assert f.run.verified == [] and f.run.executed == [] and f.leftovers() == []


def test_cancel_before_start(flow: Callable[[str], Flow]) -> None:
    f = flow("nvidia")
    f.cancel.set()
    result = f.go()
    assert result.cancelled and f.session.calls == [] and f.events == []


@pytest.mark.parametrize("stage", ["after_resolve", "after_download", "after_signature"])
def test_cancel_between_stages(flow: Callable[[str], Flow], stage: str) -> None:
    f = flow("nvidia")
    trigger = {
        "after_resolve": "Najnowsza wersja",
        "after_download": "Pobrano ",
        "after_signature": "Podpis cyfrowy prawidłowy",
    }[stage]

    def log(text: str) -> None:
        f.lines.append(text)
        if text.startswith(trigger):
            f.cancel.set()

    result = drivers.install_latest(
        "nvidia",
        log=log,
        replace_last=f.replaced.append,
        cancel_event=f.cancel,
        session=f.session,
        work_dir=f.work,
    )
    assert result.cancelled and f.run.executed == [] and f.leftovers() == []


def test_unexpected_exception_still_removes_the_file(flow: Callable[[str], Flow]) -> None:
    f = flow("nvidia")
    f.run.installer_result = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        f.go()
    assert f.leftovers() == []


def test_locked_file_cleanup_failure_is_only_logged(
    flow: Callable[[str], Flow], monkeypatch: pytest.MonkeyPatch
) -> None:
    f = flow("nvidia")

    def locked(_p: Path) -> None:
        raise PermissionError(32, "in use")

    monkeypatch.setattr(drivers, "remove_file", locked)
    result = f.go()
    assert result.ok
    assert any("nie udało się usunąć" in x for x in f.lines)


def test_no_network_at_all_is_a_driver_error(flow: Callable[[str], Flow]) -> None:
    import requests

    for vendor in VENDOR_IDS:
        f = flow(vendor)
        f.session.routes = {u: requests.ConnectTimeout("timed out") for u in f.session.routes}
        for url in (
            drivers.NVIDIA_PROCESS_FIND_URL,
            drivers.AMD_DRIVERS_URL,
            drivers.INTEL_PAGE_URL,
        ):
            f.session.routes[url] = requests.ConnectionError("no route")
        with pytest.raises(DriverError, match="Nie udało się ustalić") as info:
            f.go()
        assert info.value.vendor_url


def test_plan_b_used_end_to_end(flow: Callable[[str], Flow]) -> None:
    f = flow("nvidia")
    f.session.routes[drivers.NVIDIA_LOOKUP_URL] = FakeResponse(500)
    f.session.routes[drivers.NVIDIA_PROCESS_FIND_URL] = FakeResponse(
        200, data("nvidia_processfind.html")
    )
    f.session.routes[NV_URL] = [FakeResponse(200), exe_response(f.payload)]  # HEAD, then GET
    result = f.go()
    assert result.ok
    assert any("plan B" in x for x in f.lines)


def test_file_cannot_be_swapped_between_verification_and_launch(
    flow: Callable[[str], Flow],
) -> None:
    f = flow("nvidia")
    evil = b"MZ" + b"\xcc" * (len(f.payload) - 2)

    def swap_after_verification(text: str) -> None:
        f.lines.append(text)
        if text.startswith("Podpis cyfrowy prawidłowy"):
            target = Path(f.run.verified[0])
            # what any process of the same user could do; OSError = file locked (good)
            with contextlib.suppress(OSError):
                target.write_bytes(evil)

    with contextlib.suppress(DriverError):  # refusing to run a modified file is fine too
        drivers.install_latest(
            "nvidia",
            log=swap_after_verification,
            replace_last=f.replaced.append,
            cancel_event=f.cancel,
            session=f.session,
            work_dir=f.work,
        )
    for _path, digest in f.run.executed:
        assert digest == hashlib.sha256(f.payload).hexdigest(), "a swapped file was executed"


# ----------------------------------------------------------- download_dir


def test_download_dir_prefers_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drivers.tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
    (tmp_path / "temp").mkdir()
    folder = drivers.download_dir(tmp_path / "app")  # a new random folder each time
    assert folder.parent == tmp_path / "temp"
    assert folder.name.startswith(drivers.DOWNLOAD_SUBDIR + "-")
    assert list(folder.iterdir()) == []


@pytest.mark.parametrize("sub", ["SystemTemp", "Temp", "SYSTEMTEMP"])
def test_download_dir_skips_system_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sub: str
) -> None:
    windir = tmp_path / "Windows"
    (windir / sub).mkdir(parents=True)
    monkeypatch.setenv("SYSTEMROOT", str(windir))
    monkeypatch.setattr(drivers.tempfile, "gettempdir", lambda: str(windir / sub))
    app = tmp_path / "app"
    assert drivers.download_dir(app).parent == app
    assert list((windir / sub).iterdir()) == []


def test_download_dir_without_any_writable_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(drivers.tempfile, "gettempdir", lambda: str(blocker))
    with pytest.raises(DriverError, match="prawem zapisu"):
        drivers.download_dir(blocker)


def test_install_latest_default_folder_is_removed_when_empty(
    flow: Callable[[str], Flow], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = flow("nvidia")
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(drivers.tempfile, "gettempdir", lambda: str(temp))
    result = drivers.install_latest(
        "nvidia", log=f.lines.append, replace_last=f.replaced.append, session=f.session
    )
    assert result.ok
    assert Path(f.run.executed[0][0]).parent.parent == temp
    assert Path(f.run.executed[0][0]).parent.name.startswith(drivers.DOWNLOAD_SUBDIR + "-")
    assert list(temp.iterdir()) == []


# --------------------------------------------------------------- progress


def test_progress_reporter_throttles_and_always_shows_100() -> None:
    now = [0.0]
    log: list[str] = []
    replaced: list[str] = []
    rep = drivers.ProgressReporter(log.append, replaced.append, interval=0.2, clock=lambda: now[0])
    rep(1, 100)
    for n in range(2, 100):
        now[0] += 0.01
        rep(n, 100)
    now[0] += 0.01
    rep(100, 100)
    assert len(log) == 1 and log[0].startswith("Pobieranie: 1%")
    assert 3 <= len(replaced) <= 7
    assert replaced[-1].startswith("Pobieranie: 100%")


def test_progress_without_total_and_zero_elapsed() -> None:
    assert drivers.format_progress(0, None, 0.0) == "Pobieranie: 0.0 MB · 0.0 MB/s"
    assert drivers.format_progress(5, 0, 0.0).startswith("Pobieranie: 0.0 MB")
    assert drivers.format_progress(2 * 1024**2, 1024**2, 1.0).startswith("Pobieranie: 100%")
    text = drivers.format_progress(512 * 1024**2, 1024**3, 4.0)
    assert text == "Pobieranie: 50% · 512.0 / 1024.0 MB · 128.0 MB/s"
    log: list[str] = []
    rep = drivers.ProgressReporter(log.append, log.append, clock=lambda: 0.0)
    rep(10, None)
    rep(20, None)  # same instant, unknown total: throttled
    assert len(log) == 1
