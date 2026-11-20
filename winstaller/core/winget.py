"""winget integration: streaming process runner, output parser, return codes.

:class:`StreamParser` is pure (no I/O) and turns raw console bytes into
:class:`OutputEvent` objects. :func:`stream_process` runs any console program
with that parser and delivers events on the *calling* thread.
"""

from __future__ import annotations

import codecs
import contextlib
import functools
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import IO, Literal

from winstaller.core import platform as plat

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


# CSI (ESC [ ... final), OSC (ESC ] ... BEL | ESC \), nF (ESC ( B) and two-byte ESC
# sequences (Fp 0x30-0x3F, Fe 0x40-0x5F, Fs 0x60-0x7E; "[" excluded: it starts CSI).
# An OSC body stops at BEL, ESC or a line break, so an unterminated OSC is dropped up
# to the end of its line instead of swallowing the following lines.
_ANSI_COMPLETE = re.compile(
    rb"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b\r\n]*(?:\x07|\x1b\\)?|[ -/]+[0-~]|[0-Z\\-~])"
)
# An escape sequence that started but has not finished at the end of the buffer
# (matched from the last ESC: an OSC body never contains ESC or a line break).
_ANSI_PARTIAL_TAIL = re.compile(rb"\x1b(?:\[[0-?]*[ -/]*|\][^\x07\x1b\r\n]*|[ -/]+)?")
_SEPARATORS = re.compile(rb"(\r|\n)")
_CONTROL_CHARS = re.compile(r"[\x00-\x07\x0b\x0c\x0e-\x1f\x7f]")
#: Longest unfinished CSI/nF sequence carried to the next ``feed``. An unfinished OSC
#: is never carried whole: its body is removed anyway, so only ``ESC ]`` is kept.
_MAX_ESCAPE_CARRY = 4096
_OSC_INTRO = b"\x1b]"

#: Code pages of Polish consoles, tried after the system OEM/ANSI pages.
_POLISH_CODEPAGES = ("cp852", "cp1250")
_POLISH_LETTERS = frozenset("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")


@functools.cache
def system_codepages() -> tuple[str, ...]:
    """Codec names of the console (OEM) and ANSI code pages, in that order.

    Console tools (shutdown, powercfg, winget writing to a pipe) usually emit the
    OEM page (852 on Polish Windows); some programs use the ANSI page (1250).
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        pages = (int(kernel32.GetOEMCP()), int(kernel32.GetACP()))
    except (AttributeError, OSError):
        return ()
    names: list[str] = []
    for page in pages:
        name = f"cp{page}"
        try:
            codecs.lookup(name)
        except LookupError:
            continue
        if name not in names:
            names.append(name)
    return tuple(names)


def decoding_score(text: str) -> int:
    """Plausibility of decoded text: Polish letters score high, symbols and box
    drawing characters (typical mojibake of the wrong code page) score negative."""
    score = 0
    for ch in text:
        if ch < "\x80":
            continue
        if ch in _POLISH_LETTERS:
            score += 3
        elif ch.isalpha():
            score += 1
        else:
            score -= 3
    return score


def decode_console_bytes(raw: bytes, *, codepages: Sequence[str] | None = None) -> str:
    """Decode console output; never raises.

    Valid UTF-8 is used as is. Otherwise each candidate code page (``codepages``,
    default :func:`system_codepages`; CP852 and CP1250 are always added after them)
    that decodes the bytes is scored with :func:`decoding_score` and the best one
    wins; ties go to the earlier candidate (the console OEM page first).
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    candidates = system_codepages() if codepages is None else tuple(codepages)
    best: str | None = None
    best_score = 0
    for encoding in dict.fromkeys((*candidates, *_POLISH_CODEPAGES)):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        score = decoding_score(text)
        if best is None or score > best_score:
            best, best_score = text, score
    return best if best is not None else raw.decode("utf-8", errors="replace")


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
        last_esc = data.rfind(b"\x1b")
        if last_esc >= 0 and _ANSI_PARTIAL_TAIL.fullmatch(data, last_esc):
            tail = data[last_esc:]
            if tail.startswith(_OSC_INTRO):
                self._escape_carry = _OSC_INTRO  # the body is dropped either way
                data = data[:last_esc]
            elif len(tail) <= _MAX_ESCAPE_CARRY:
                self._escape_carry = tail
                data = data[:last_esc]
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
_FINAL_DRAIN_SECONDS = 0.2


def _reader(pipe: IO[bytes], out: queue.Queue[object], stop: threading.Event) -> None:
    parser = StreamParser()
    try:
        while not stop.is_set():
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


_THREAD_TERMINATE = 0x0001  # access right required by CancelSynchronousIo
_ABORT_ATTEMPTS = 20


def _cancel_synchronous_io(thread_id: int) -> None:
    """Abort a blocking ``ReadFile`` of another thread (Win32 ``CancelSynchronousIo``)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenThread(_THREAD_TERMINATE, False, thread_id)
    if not handle:
        return
    try:
        kernel32.CancelSynchronousIo(handle)
    finally:
        kernel32.CloseHandle(handle)


def _close_reader(reader: threading.Thread, pipe: IO[bytes], stop: threading.Event) -> None:
    """Stop the reader thread and close the pipe.

    When a grandchild still holds the pipe the reader is blocked in ``read1``; closing
    the pipe from here would block on the reader's buffer lock, so the pending read is
    cancelled first. If the thread cannot be stopped it is left alone (daemon) and the
    pipe stays open rather than risking a hang.
    """
    stop.set()
    for _ in range(_ABORT_ATTEMPTS):
        if not reader.is_alive():
            break
        if reader.native_id is not None:
            with contextlib.suppress(OSError, AttributeError):
                _cancel_synchronous_io(reader.native_id)
        reader.join(_POLL_SECONDS)
    if not reader.is_alive():
        with contextlib.suppress(OSError, ValueError):
            pipe.close()


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
    on_start: Callable[[subprocess.Popen[bytes]], None] | None = None,
    creationflags: int = 0,
) -> ProcessOutcome:
    """Run ``args`` with merged stdout/stderr, streaming parsed output.

    Callbacks are invoked on the calling thread. On cancellation the process is
    terminated, then killed after a grace period; returncode is then ``-1``.
    A cancel that arrives after the process already exited (while remaining output
    is delivered) does not change the outcome: the real exit code is returned and
    the wait for EOF ends early. Raises ``OSError`` (e.g. ``FileNotFoundError``)
    when the program cannot start. ``on_start(proc)`` is called right after the
    process starts (e.g. to put it in a Job Object); if it raises, the process is
    stopped and the exception propagates. ``creationflags`` are added to
    ``CREATE_NO_WINDOW`` (e.g. ``CREATE_SUSPENDED``; then ``on_start`` must resume it).
    """
    proc = subprocess.Popen(
        list(args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=cwd,
        creationflags=CREATE_NO_WINDOW | creationflags,
    )
    assert proc.stdout is not None
    if on_start is not None:
        try:
            on_start(proc)
        except BaseException:
            _stop_process(proc)
            with contextlib.suppress(OSError, ValueError):
                proc.stdout.close()
            raise
    events: queue.Queue[object] = queue.Queue()
    stop_reading = threading.Event()
    reader = threading.Thread(
        target=_reader,
        args=(proc.stdout, events, stop_reading),
        name="winstaller-stream-reader",
        daemon=True,
    )
    reader.start()

    def deliver(item: object) -> None:
        if isinstance(item, OutputEvent):
            if item.kind == OutputKind.APPEND:
                on_line(item.text)
            else:
                on_replace_last(item.text)

    def cancel_requested() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    cancelled = False
    exited_at: float | None = None
    try:
        while True:
            try:
                item = events.get(timeout=_POLL_SECONDS)
            except queue.Empty:
                item = None
            if item is _EOF:
                break
            deliver(item)
            exited = proc.poll() is not None
            if not exited and not cancelled and cancel_requested():
                _stop_process(proc)
                cancelled = True
                exited = True
            if exited:
                # A child of the process (e.g. an installer) may inherit the pipe and keep
                # it open after the process exits; do not wait for EOF forever then.
                exited_at = exited_at or time.monotonic()
                if cancel_requested() or time.monotonic() - exited_at > _DRAIN_AFTER_EXIT_SECONDS:
                    deadline = time.monotonic() + _FINAL_DRAIN_SECONDS
                    while True:  # deliver what is already queued, waiting only briefly
                        try:
                            item = events.get(timeout=max(0.0, deadline - time.monotonic()))
                        except queue.Empty:
                            break
                        if item is _EOF:
                            break
                        deliver(item)
                    break
        while True:
            try:
                proc.wait(timeout=_POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                if not cancelled and cancel_requested() and proc.poll() is None:
                    _stop_process(proc)
                    cancelled = True
    except BaseException:
        if proc.poll() is None:  # e.g. a callback raised: do not leave the process behind
            _stop_process(proc)
        raise
    finally:
        _close_reader(reader, proc.stdout, stop_reading)
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


#: Prefix for every PowerShell script we run: output (and errors) as UTF-8, so the
#: decoder never has to guess the console code page.
POWERSHELL_UTF8_PREAMBLE = (
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "$OutputEncoding = [System.Text.Encoding]::UTF8; "
    "$ProgressPreference = 'SilentlyContinue'; "
)


def _powershell(command: str) -> list[str]:
    """PowerShell by absolute System32 path (``FileNotFoundError`` when missing). winget
    itself stays a bare name: it is an App Execution Alias in the user's profile."""
    return [
        plat.system32_path(plat.POWERSHELL_EXE),
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        POWERSHELL_UTF8_PREAMBLE + command,
    ]


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
