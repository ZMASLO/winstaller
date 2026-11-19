"""winget integration: streaming process runner, output parser, return codes.

:class:`StreamParser` is pure (no I/O) and turns raw console bytes into
:class:`OutputEvent` objects. :func:`stream_process` runs any console program
with that parser and delivers events on the *calling* thread.
"""

from __future__ import annotations

import contextlib
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import IO, Literal

LineCallback = Callable[[str], None]

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

WINGET_MSIXBUNDLE_URL = (
    "https://github.com/microsoft/winget-cli/releases/latest/download/"
    "Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle"
)

# ------------------------------------------------------------------ parser


class OutputKind(StrEnum):
    APPEND = "append"  # add a new line to the log
    REPLACE_LAST = "replace_last"  # overwrite the last log line (progress bars, spinners)


@dataclass(frozen=True, slots=True)
class OutputEvent:
    kind: OutputKind
    text: str


# CSI (ESC [ ... final), OSC (ESC ] ... BEL | ESC \), nF (ESC ( B) and two-byte ESC sequences.
_ANSI_COMPLETE = re.compile(
    rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[ -/]+[0-~]|[@-Z\\-_])"
)
# An escape sequence that started but has not finished at the end of the buffer.
_ANSI_PARTIAL_TAIL = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b]*\x1b?|[ -/]+)?\Z")
_SEPARATORS = re.compile(rb"(\r|\n)")
_CONTROL_CHARS = re.compile(r"[\x00-\x07\x0b\x0c\x0e-\x1f\x7f]")
_MAX_ESCAPE_CARRY = 4096
_ENCODINGS = ("utf-8", "cp1250", "cp852", "iso-8859-2")


def decode_console_bytes(raw: bytes) -> str:
    """Decode console output trying UTF-8, then Polish code pages (CP1250, CP852)."""
    if not raw:
        return ""
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _apply_backspaces(text: str) -> str:
    if "\b" not in text:
        return text
    out: list[str] = []
    for ch in text:
        if ch == "\b":
            if out:
                out.pop()
        else:
            out.append(ch)
    return "".join(out)


def _clean_segment(raw: bytes) -> str:
    text = _apply_backspaces(decode_console_bytes(raw))
    return _CONTROL_CHARS.sub("", text).strip()


class StreamParser:
    """Incremental parser of console output (bytes in, events out).

    Semantics: ``\\n`` ends a line; ``\\r`` returns to the start of the current
    line, so the next text overwrites it (emitted as ``REPLACE_LAST``). ANSI
    escape sequences are removed. Escape sequences and multi-byte characters
    split across ``feed`` calls are handled. Whitespace-only segments are dropped.
    Work per byte is constant, so total cost is linear in input size.
    """

    __slots__ = ("_escape_carry", "_segment", "_line_shown")

    def __init__(self) -> None:
        self._escape_carry = b""
        self._segment = bytearray()
        # Whether something was already emitted for the current physical line.
        self._line_shown = False

    def feed(self, chunk: bytes) -> list[OutputEvent]:
        data = self._escape_carry + chunk if self._escape_carry else chunk
        self._escape_carry = b""
        partial = _ANSI_PARTIAL_TAIL.search(data, max(0, len(data) - _MAX_ESCAPE_CARRY))
        if partial is not None:
            self._escape_carry = data[partial.start() :]
            data = data[: partial.start()]
        if b"\x1b" in data:
            data = _ANSI_COMPLETE.sub(b"", data).replace(b"\x1b", b"")
        events: list[OutputEvent] = []
        for piece in _SEPARATORS.split(data):
            if piece == b"\n":
                self._end_segment(events, newline=True)
            elif piece == b"\r":
                self._end_segment(events, newline=False)
            elif piece:
                self._segment += piece
        return events

    def flush(self) -> list[OutputEvent]:
        """Emit whatever is buffered (call once at end of stream)."""
        self._escape_carry = b""
        events: list[OutputEvent] = []
        self._end_segment(events, newline=True)
        return events

    def _end_segment(self, events: list[OutputEvent], *, newline: bool) -> None:
        text = _clean_segment(bytes(self._segment)) if self._segment else ""
        self._segment.clear()
        if text:
            kind = OutputKind.REPLACE_LAST if self._line_shown else OutputKind.APPEND
            events.append(OutputEvent(kind, text))
            self._line_shown = True
        if newline:
            self._line_shown = False


# ---------------------------------------------------------- process runner


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    returncode: int
    cancelled: bool


_EOF = object()
_POLL_SECONDS = 0.05
_TERMINATE_WAIT_SECONDS = 2.0
_DRAIN_AFTER_EXIT_SECONDS = 3.0


def _reader(pipe: IO[bytes], out: queue.Queue[object]) -> None:
    parser = StreamParser()
    try:
        while True:
            try:
                chunk = pipe.read1(65536)  # type: ignore[attr-defined]
            except (OSError, ValueError):
                break
            if not chunk:
                break
            for event in parser.feed(chunk):
                out.put(event)
    finally:
        for event in parser.flush():
            out.put(event)
        out.put(_EOF)


def _stop_process(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(OSError):
        proc.terminate()
    try:
        proc.wait(timeout=_TERMINATE_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def stream_process(
    args: Sequence[str],
    *,
    on_line: LineCallback,
    on_replace_last: LineCallback,
    cancel_event: threading.Event | None = None,
    cwd: str | None = None,
) -> ProcessOutcome:
    """Run ``args`` with merged stdout/stderr, streaming parsed output.

    Callbacks are invoked on the calling thread. On cancellation the process is
    terminated, then killed after a grace period; returncode is then ``-1``.
    Raises ``OSError`` (e.g. ``FileNotFoundError``) when the program cannot start.
    """
    proc = subprocess.Popen(
        list(args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        creationflags=CREATE_NO_WINDOW,
    )
    assert proc.stdout is not None
    events: queue.Queue[object] = queue.Queue()
    reader = threading.Thread(target=_reader, args=(proc.stdout, events), daemon=True)
    reader.start()

    cancelled = False
    exited_at: float | None = None
    while True:
        try:
            item = events.get(timeout=_POLL_SECONDS)
        except queue.Empty:
            item = None
        if item is _EOF:
            break
        if isinstance(item, OutputEvent):
            if item.kind == OutputKind.APPEND:
                on_line(item.text)
            else:
                on_replace_last(item.text)
        if not cancelled and cancel_event is not None and cancel_event.is_set():
            _stop_process(proc)
            cancelled = True
        # A child of the process (e.g. an installer) may inherit the pipe and keep it
        # open after the process exits; do not wait for EOF forever in that case.
        if proc.poll() is not None:
            exited_at = exited_at or time.monotonic()
            if time.monotonic() - exited_at > _DRAIN_AFTER_EXIT_SECONDS:
                break
    while True:
        try:
            proc.wait(timeout=_POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            if not cancelled and cancel_event is not None and cancel_event.is_set():
                _stop_process(proc)
                cancelled = True
    return ProcessOutcome(-1 if cancelled else proc.returncode, cancelled)


# ------------------------------------------------------------ return codes

Operation = Literal["install", "uninstall"]


def _u32(code: int) -> int:
    return code & 0xFFFFFFFF


#: winget HRESULTs (``winget error <code>``) treated as success, per operation.
#: Symbols: 0x8A15002B UPDATE_NOT_APPLICABLE, 0x8A150061 PACKAGE_ALREADY_INSTALLED,
#: 0x8A15010D INSTALL_ALREADY_INSTALLED, 0x8A15010E INSTALL_DOWNGRADE,
#: 0x8A150109 INSTALL_REBOOT_REQUIRED_TO_FINISH, 0x8A15010B INSTALL_REBOOT_INITIATED,
#: 0x8A150014 NO_APPLICATIONS_FOUND (uninstall of a package that is not installed).
SUCCESS_CODES: dict[Operation, dict[int, str]] = {
    "install": {
        0x8A15002B: "Program jest już zainstalowany (brak nowszej wersji).",
        0x8A150061: "Program jest już zainstalowany.",
        0x8A15010D: "Inna wersja programu jest już zainstalowana.",
        0x8A15010E: "Zainstalowana jest nowsza wersja programu.",
        0x8A150109: "Zainstalowano. Uruchom ponownie komputer, aby dokończyć.",
        0x8A15010B: "Zainstalowano. Komputer zostanie uruchomiony ponownie.",
    },
    "uninstall": {
        0x8A150014: "Program nie był zainstalowany.",  # NO_APPLICATIONS_FOUND
    },
}

ERROR_MESSAGES: dict[int, str] = {
    0x8A150014: "Nie znaleziono pakietu w źródłach winget.",
    0x8A150010: "Brak instalatora zgodnego z tym systemem.",
    0x8A150056: "Instalatora nie można uruchomić jako administrator.",
    0x8A150101: "Program jest uruchomiony. Zamknij go i spróbuj ponownie.",
    0x8A150102: "Trwa inna instalacja. Spróbuj ponownie później.",
    0x8A150103: "Plik jest w użyciu. Zamknij program i spróbuj ponownie.",
    0x8A150104: "Brakuje zależności wymaganej przez pakiet.",
    0x8A150105: "Brak miejsca na dysku.",
    0x8A150106: "Za mało pamięci do instalacji.",
    0x8A150107: "Instalacja wymaga połączenia z internetem.",
    0x8A15010A: "Uruchom ponownie komputer i spróbuj jeszcze raz.",
    0x8A15010C: "Instalacja została anulowana.",
    0x8A150110: "Nie udało się zainstalować zależności pakietu.",
    0x8A150113: "Pakiet nie jest obsługiwany przez ten system.",
}


def describe_returncode(returncode: int, operation: Operation) -> tuple[bool, str]:
    """Map a winget exit code to ``(ok, Polish message)``. Non-zero is an error
    unless listed in :data:`SUCCESS_CODES` for ``operation``."""
    if returncode == 0:
        return True, "Zainstalowano." if operation == "install" else "Odinstalowano."
    code = _u32(returncode)
    if code in SUCCESS_CODES[operation]:
        return True, SUCCESS_CODES[operation][code]
    detail = ERROR_MESSAGES.get(code, "Nieznany błąd")
    return False, f"{detail} (kod winget 0x{code:08X})"


# --------------------------------------------------------------- winget API


@dataclass(frozen=True, slots=True)
class WingetResult:
    returncode: int
    ok: bool
    cancelled: bool
    message: str


def _noop(_: str) -> None:
    pass


def _run_winget(
    args: list[str],
    operation: Operation,
    *,
    on_line: LineCallback | None,
    on_replace_last: LineCallback | None,
    cancel_event: threading.Event | None,
) -> WingetResult:
    try:
        outcome = stream_process(
            ["winget", *args],
            on_line=on_line or _noop,
            on_replace_last=on_replace_last or on_line or _noop,
            cancel_event=cancel_event,
        )
    except FileNotFoundError:
        return WingetResult(-1, False, False, "Nie znaleziono programu winget.")
    except OSError as exc:
        return WingetResult(-1, False, False, f"Nie udało się uruchomić winget: {exc}")
    if outcome.cancelled:
        return WingetResult(outcome.returncode, False, True, "Przerwano przez użytkownika.")
    ok, message = describe_returncode(outcome.returncode, operation)
    return WingetResult(outcome.returncode, ok, False, message)


def install(
    package_id: str,
    *,
    on_line: LineCallback | None = None,
    on_replace_last: LineCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> WingetResult:
    """``winget install --id <package_id> -e --silent`` with agreements accepted."""
    args = [
        "install",
        "--id",
        package_id,
        "--exact",
        "--silent",
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--disable-interactivity",
    ]
    return _run_winget(
        args, "install", on_line=on_line, on_replace_last=on_replace_last, cancel_event=cancel_event
    )


def uninstall(
    package_id: str,
    *,
    on_line: LineCallback | None = None,
    on_replace_last: LineCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> WingetResult:
    """``winget uninstall --id <package_id> -e --silent``."""
    args = [
        "uninstall",
        "--id",
        package_id,
        "--exact",
        "--silent",
        "--accept-source-agreements",
        "--disable-interactivity",
    ]
    return _run_winget(
        args,
        "uninstall",
        on_line=on_line,
        on_replace_last=on_replace_last,
        cancel_event=cancel_event,
    )


def is_available(timeout: float = 15.0) -> bool:
    """True when ``winget --version`` runs successfully."""
    try:
        result = subprocess.run(
            ["winget", "--version"],
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _powershell(command: str) -> list[str]:
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]


def ensure_installed(
    *,
    on_line: LineCallback | None = None,
    on_replace_last: LineCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> WingetResult:
    """Make sure winget is usable, installing App Installer when it is not.

    First re-registers the preinstalled App Installer package (common cause of a
    missing ``winget`` on fresh accounts), then falls back to installing the
    msixbundle from GitHub with ``Add-AppxPackage`` (as 1.x did).
    """
    if is_available():
        return WingetResult(0, True, False, "winget jest dostępny.")
    log = on_line or _noop
    steps = [
        (
            "Rejestrowanie App Installer (winget)...",
            "Add-AppxPackage -RegisterByFamilyName -MainPackage "
            "Microsoft.DesktopAppInstaller_8wekyb3d8bbwe",
        ),
        (
            "Instalowanie winget z GitHub...",
            f"Add-AppxPackage -Path '{WINGET_MSIXBUNDLE_URL}'",
        ),
    ]
    last_code = -1
    for label, command in steps:
        log(label)
        try:
            outcome = stream_process(
                _powershell(command),
                on_line=log,
                on_replace_last=on_replace_last or log,
                cancel_event=cancel_event,
            )
        except OSError as exc:
            return WingetResult(-1, False, False, f"Nie udało się uruchomić PowerShell: {exc}")
        if outcome.cancelled:
            return WingetResult(-1, False, True, "Przerwano przez użytkownika.")
        last_code = outcome.returncode
        if is_available():
            return WingetResult(0, True, False, "Zainstalowano winget.")
    return WingetResult(last_code, False, False, "Nie udało się zainstalować winget.")
