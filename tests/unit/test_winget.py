"""winget command lines, return codes, ensure_installed/is_available (all mocked)."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from winstaller.core import winget
from winstaller.core.winget import ProcessOutcome, WingetResult


@dataclass
class FakeStream:
    """Replacement for ``winget.stream_process`` recording its calls."""

    outcomes: list[ProcessOutcome | BaseException] = field(default_factory=list)
    output: list[tuple[str, str]] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def __call__(self, args, *, on_line, on_replace_last, cancel_event=None, cwd=None):
        self.calls.append({"args": list(args), "cancel_event": cancel_event, "cwd": cwd})
        for kind, text in self.output:
            (on_line if kind == "line" else on_replace_last)(text)
        result = self.outcomes.pop(0) if self.outcomes else ProcessOutcome(0, False)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def fake_stream(monkeypatch: pytest.MonkeyPatch) -> FakeStream:
    fake = FakeStream()
    monkeypatch.setattr(winget, "stream_process", fake)
    return fake


# --------------------------------------------------------------- commands


def test_install_command_line(fake_stream: FakeStream) -> None:
    cancel = threading.Event()
    result = winget.install("7zip.7zip", cancel_event=cancel)
    assert fake_stream.calls == [
        {
            "args": [
                "winget",
                "install",
                "--id",
                "7zip.7zip",
                "--exact",
                "--silent",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--disable-interactivity",
            ],
            "cancel_event": cancel,
            "cwd": None,
        }
    ]
    assert result == WingetResult(0, True, False, "Zainstalowano.")


def test_uninstall_command_line(fake_stream: FakeStream) -> None:
    result = winget.uninstall("Microsoft.OneDrive")
    assert fake_stream.calls[0]["args"] == [
        "winget",
        "uninstall",
        "--id",
        "Microsoft.OneDrive",
        "--exact",
        "--silent",
        "--accept-source-agreements",
        "--disable-interactivity",
    ]
    assert result == WingetResult(0, True, False, "Odinstalowano.")


def test_package_id_is_passed_as_single_argument(fake_stream: FakeStream) -> None:
    winget.install("Evil.Id --override /x & calc")
    args = fake_stream.calls[0]["args"]
    assert args[args.index("--id") + 1] == "Evil.Id --override /x & calc"


def test_output_is_forwarded(fake_stream: FakeStream) -> None:
    fake_stream.output = [("line", "a"), ("replace", "b")]
    lines: list[str] = []
    replaced: list[str] = []
    winget.install("X", on_line=lines.append, on_replace_last=replaced.append)
    assert lines == ["a"] and replaced == ["b"]


def test_replace_last_falls_back_to_on_line(fake_stream: FakeStream) -> None:
    fake_stream.output = [("line", "a"), ("replace", "b")]
    lines: list[str] = []
    winget.install("X", on_line=lines.append)
    assert lines == ["a", "b"]


def test_no_callbacks_is_fine(fake_stream: FakeStream) -> None:
    fake_stream.output = [("line", "a"), ("replace", "b")]
    assert winget.install("X").ok


# ------------------------------------------------------------ return codes


@pytest.mark.parametrize(
    ("code", "operation", "ok"),
    [
        (0, "install", True),
        (0, "uninstall", True),
        (0x8A15002B, "install", True),
        (0x8A150061, "install", True),
        (0x8A15010D, "install", True),
        (0x8A15010E, "install", True),
        (0x8A150109, "install", True),
        (0x8A15010B, "install", True),
        (0x8A150014, "uninstall", True),
        # Success codes are per operation.
        (0x8A150014, "install", False),
        (0x8A150061, "uninstall", False),
        (1, "install", False),
        (-1, "install", False),
        (0x8A150101, "install", False),
        (0x8A15FFFF, "install", False),
    ],
)
def test_describe_returncode(code: int, operation: str, ok: bool) -> None:
    result_ok, message = winget.describe_returncode(code, operation)  # type: ignore[arg-type]
    assert result_ok is ok
    assert message


@pytest.mark.parametrize("code", [0x8A150061, 0x8A150014, 0x8A150101, 0x8A15FFFF])
def test_negative_int32_and_uint32_forms_are_equivalent(code: int) -> None:
    signed = code - (1 << 32)
    for operation in ("install", "uninstall"):
        assert winget.describe_returncode(signed, operation) == winget.describe_returncode(
            code, operation
        )


def test_known_error_message_includes_hex_code() -> None:
    ok, message = winget.describe_returncode(0x8A150101 - (1 << 32), "install")
    assert not ok
    assert (
        message
        == "Program jest uruchomiony. Zamknij go i spróbuj ponownie. (kod winget 0x8A150101)"
    )


def test_unknown_code_message() -> None:
    assert winget.describe_returncode(5, "uninstall") == (
        False,
        "Nieznany błąd (kod winget 0x00000005)",
    )


def test_already_installed_message_is_polish() -> None:
    assert winget.describe_returncode(-1978335135, "install") == (
        True,
        "Program jest już zainstalowany.",
    )


def test_success_codes_table_matches_docs() -> None:
    assert set(winget.SUCCESS_CODES["install"]) == {
        0x8A15002B,
        0x8A150061,
        0x8A15010D,
        0x8A15010E,
        0x8A150109,
        0x8A15010B,
    }
    assert set(winget.SUCCESS_CODES["uninstall"]) == {0x8A150014}
    # A success code must never also be described as an error for the same operation.
    for operation, codes in winget.SUCCESS_CODES.items():
        for code in codes:
            assert winget.describe_returncode(code, operation)[0]


@pytest.mark.parametrize(("code", "ok"), [(0x8A150061, True), (0x8A150101, False), (3, False)])
def test_install_maps_process_returncode(fake_stream: FakeStream, code: int, ok: bool) -> None:
    fake_stream.outcomes = [ProcessOutcome(code - (1 << 32) if code > 0x7FFFFFFF else code, False)]
    result = winget.install("X")
    assert result.ok is ok
    assert not result.cancelled


def test_install_cancelled(fake_stream: FakeStream) -> None:
    fake_stream.outcomes = [ProcessOutcome(-1, True)]
    assert winget.install("X") == WingetResult(-1, False, True, "Przerwano przez użytkownika.")


def test_uninstall_cancelled(fake_stream: FakeStream) -> None:
    fake_stream.outcomes = [ProcessOutcome(-1, True)]
    result = winget.uninstall("X")
    assert result.cancelled and not result.ok


def test_install_without_winget(fake_stream: FakeStream) -> None:
    fake_stream.outcomes = [FileNotFoundError(2, "not found")]
    assert winget.install("X") == WingetResult(-1, False, False, "Nie znaleziono programu winget.")


def test_install_other_oserror(fake_stream: FakeStream) -> None:
    fake_stream.outcomes = [PermissionError(13, "denied")]
    result = winget.uninstall("X")
    assert not result.ok and not result.cancelled
    assert result.message.startswith("Nie udało się uruchomić winget:")


def test_install_without_winget_real_process(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # Real stream_process, but "winget" resolves to a missing absolute path.
    real = winget.stream_process

    def redirect(args, **kwargs):
        return real([str(tmp_path / "winget.exe"), *args[1:]], **kwargs)

    monkeypatch.setattr(winget, "stream_process", redirect)
    assert winget.install("X").message == "Nie znaleziono programu winget."


# -------------------------------------------------------------- availability


@dataclass
class FakeRun:
    results: list[object]
    calls: list[tuple[list[str], dict]] = field(default_factory=list)

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return subprocess.CompletedProcess(args, result, b"v1.29.380", b"")


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (0, True),
        (1, False),
        (FileNotFoundError(), False),
        (subprocess.TimeoutExpired("winget", 1), False),
        (PermissionError(), False),
    ],
)
def test_is_available(monkeypatch: pytest.MonkeyPatch, result: object, expected: bool) -> None:
    fake = FakeRun([result])
    monkeypatch.setattr(subprocess, "run", fake)
    assert winget.is_available(timeout=3.5) is expected
    args, kwargs = fake.calls[0]
    assert args == ["winget", "--version"]
    assert kwargs["timeout"] == 3.5
    assert kwargs["creationflags"] == winget.CREATE_NO_WINDOW


def _availability(values: list[bool]) -> Callable[..., bool]:
    seq = list(values)

    def fake(timeout: float = 15.0) -> bool:
        return seq.pop(0)

    return fake


def test_ensure_installed_when_available(monkeypatch, fake_stream: FakeStream) -> None:
    monkeypatch.setattr(winget, "is_available", _availability([True]))
    assert winget.ensure_installed() == WingetResult(0, True, False, "winget jest dostępny.")
    assert fake_stream.calls == []


def test_ensure_installed_register_step_is_enough(monkeypatch, fake_stream: FakeStream) -> None:
    monkeypatch.setattr(winget, "is_available", _availability([False, True]))
    lines: list[str] = []
    result = winget.ensure_installed(on_line=lines.append)
    assert result.ok and result.message == "Zainstalowano winget."
    assert len(fake_stream.calls) == 1
    args = fake_stream.calls[0]["args"]
    powershell = winget.plat.system32_path(winget.plat.POWERSHELL_EXE)
    assert args[:4] == [powershell, "-NoProfile", "-NonInteractive", "-Command"]
    assert "-RegisterByFamilyName" in args[4]
    assert "Microsoft.DesktopAppInstaller_8wekyb3d8bbwe" in args[4]
    assert lines == ["Rejestrowanie App Installer (winget)..."]


def test_ensure_installed_falls_back_to_msixbundle(monkeypatch, fake_stream: FakeStream) -> None:
    monkeypatch.setattr(winget, "is_available", _availability([False, False, True]))
    fake_stream.outcomes = [ProcessOutcome(1, False), ProcessOutcome(0, False)]
    assert winget.ensure_installed().ok
    second = fake_stream.calls[1]["args"][4]
    # Contract (stage 3): every PowerShell we start forces UTF-8 output first.
    assert second == (
        winget.POWERSHELL_UTF8_PREAMBLE + f"Add-AppxPackage -Path '{winget.WINGET_MSIXBUNDLE_URL}'"
    )
    assert "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8" in second


def test_ensure_installed_all_steps_fail(monkeypatch, fake_stream: FakeStream) -> None:
    monkeypatch.setattr(winget, "is_available", _availability([False, False, False]))
    fake_stream.outcomes = [ProcessOutcome(1, False), ProcessOutcome(5, False)]
    assert winget.ensure_installed() == WingetResult(
        5, False, False, "Nie udało się zainstalować winget."
    )


def test_ensure_installed_cancelled(monkeypatch, fake_stream: FakeStream) -> None:
    monkeypatch.setattr(winget, "is_available", _availability([False]))
    fake_stream.outcomes = [ProcessOutcome(-1, True)]
    cancel = threading.Event()
    result = winget.ensure_installed(cancel_event=cancel)
    assert result.cancelled and not result.ok
    assert fake_stream.calls[0]["cancel_event"] is cancel
    assert len(fake_stream.calls) == 1


def test_ensure_installed_without_powershell(monkeypatch, fake_stream: FakeStream) -> None:
    monkeypatch.setattr(winget, "is_available", _availability([False]))
    fake_stream.outcomes = [FileNotFoundError(2, "powershell")]
    result = winget.ensure_installed()
    assert not result.ok
    assert result.message.startswith("Nie udało się uruchomić PowerShell:")
