"""stream_process with real, harmless child processes (``sys.executable -c``)."""

from __future__ import annotations

import ctypes
import os
import signal
import sys
import textwrap
import threading
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from winstaller.core.winget import ProcessOutcome, stream_process

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
BASE_PYTHON = getattr(sys, "_base_executable", None) or sys.executable
_SYNCHRONIZE = 0x00100000
_WAIT_TIMEOUT = 0x102


def process_alive(pid: int) -> bool:
    handle = _kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return _kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        _kernel32.CloseHandle(handle)


class Collector:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.threads: set[int] = set()

    def line(self, text: str) -> None:
        self.threads.add(threading.get_ident())
        self.events.append(("line", text))

    def replace(self, text: str) -> None:
        self.threads.add(threading.get_ident())
        self.events.append(("replace", text))

    @property
    def lines(self) -> list[str]:
        return [t for k, t in self.events if k == "line"]


def py(code: str, exe: str = sys.executable) -> list[str]:
    return [exe, "-c", textwrap.dedent(code)]


def run(code: str, exe: str = sys.executable, **kwargs) -> tuple[ProcessOutcome, Collector]:
    col = Collector()
    outcome = stream_process(py(code, exe), on_line=col.line, on_replace_last=col.replace, **kwargs)
    return outcome, col


def test_lines_in_order_and_stderr_merged() -> None:
    outcome, col = run(
        """
        import sys
        for i in range(5):
            print(f"line {i}", flush=True)
        print("to stderr", file=sys.stderr, flush=True)
        print("last", flush=True)
        """
    )
    assert outcome == ProcessOutcome(0, False)
    assert col.lines == [f"line {i}" for i in range(5)] + ["to stderr", "last"]


def test_carriage_return_progress_becomes_replace_last() -> None:
    outcome, col = run(r"""import sys; sys.stdout.buffer.write(b"10%\r20%\r30%\ndone\n")""")
    assert outcome.returncode == 0
    assert col.events == [("line", "10%"), ("replace", "20%"), ("replace", "30%"), ("line", "done")]


def test_callbacks_run_on_calling_thread() -> None:
    _, col = run("print('a'); print('b\\rc')")
    assert col.threads == {threading.get_ident()}


@pytest.mark.parametrize("code", [0, 1, 7, 255])
def test_exit_code_propagates(code: int) -> None:
    outcome, _ = run(f"import sys; print('x'); sys.exit({code})")
    assert outcome == ProcessOutcome(code, False)


def test_negative_int32_exit_code_is_reported_as_unsigned_dword() -> None:
    # winget reports HRESULTs; Windows exit codes are DWORDs.
    outcome, _ = run("import os; os._exit(-1978335135)")  # 0x8A150061
    assert outcome.returncode & 0xFFFFFFFF == 0x8A150061
    assert not outcome.cancelled


def test_output_without_trailing_newline_is_flushed() -> None:
    _, col = run("import sys; sys.stdout.write('tail without newline')")
    assert col.lines == ["tail without newline"]


def test_polish_utf8_output() -> None:
    _, col = run(r"""import sys; sys.stdout.buffer.write("Zażółć gęślą jaźń\n".encode())""")
    assert col.lines == ["Zażółć gęślą jaźń"]


def test_cwd_is_used(tmp_path: Path) -> None:
    _, col = run("import os; print(os.getcwd())", cwd=str(tmp_path))
    assert Path(col.lines[0]).resolve() == tmp_path.resolve()


def test_many_lines_are_not_lost() -> None:
    _, col = run(
        """
        import sys
        out = sys.stdout
        for i in range(20000):
            out.write(f"{i}\\n")
        """
    )
    assert col.lines == [str(i) for i in range(20000)]


def test_cancel_during_run_kills_process_quickly() -> None:
    cancel = threading.Event()
    col = Collector()
    pids: list[int] = []

    def on_line(text: str) -> None:
        col.line(text)
        if text.startswith("pid "):
            pids.append(int(text.split()[1]))
            cancel.set()

    # The base interpreter (not the venv launcher) so the printed pid is the very
    # process stream_process started and must kill.
    args = [
        BASE_PYTHON,
        "-c",
        "import os, time; print('pid', os.getpid(), flush=True); time.sleep(60)",
    ]
    started = time.monotonic()
    outcome = stream_process(
        args, on_line=on_line, on_replace_last=col.replace, cancel_event=cancel
    )
    elapsed = time.monotonic() - started
    assert outcome == ProcessOutcome(-1, True)
    assert elapsed < 5.0, f"cancellation took {elapsed:.2f}s"
    assert pids
    try:
        assert not process_alive(pids[0])
    finally:
        if process_alive(pids[0]):
            os.kill(pids[0], signal.SIGTERM)


def test_cancel_set_before_start() -> None:
    cancel = threading.Event()
    cancel.set()
    started = time.monotonic()
    outcome, _ = run("import time; time.sleep(60)", cancel_event=cancel)
    assert outcome == ProcessOutcome(-1, True)
    assert time.monotonic() - started < 5.0


def test_cancel_from_another_thread() -> None:
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    try:
        started = time.monotonic()
        outcome, _ = run("import time; time.sleep(60)", cancel_event=cancel)
    finally:
        timer.cancel()
    assert outcome.cancelled
    assert 0.4 < time.monotonic() - started < 5.0


def test_missing_program_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        stream_process(
            [str(tmp_path / "no-such-program.exe")],
            on_line=lambda _: None,
            on_replace_last=lambda _: None,
        )


_CLOSES_OUTPUT_EARLY = """
    import os, time
    print("bye", flush=True)
    os.close(1)
    os.close(2)
    time.sleep({sleep})
    os._exit(4)
"""


def test_process_that_closes_output_early_is_still_awaited() -> None:
    started = time.monotonic()
    # Base interpreter: the venv launcher would keep its own copy of the pipe open.
    outcome, col = run(_CLOSES_OUTPUT_EARLY.format(sleep=1.0), BASE_PYTHON)
    assert outcome == ProcessOutcome(4, False)
    assert col.lines == ["bye"]
    assert time.monotonic() - started >= 0.9


def test_cancel_after_output_closed_still_kills() -> None:
    # Cancel arrives after EOF, while stream_process only waits for the exit code.
    cancel = threading.Event()
    timer = threading.Timer(0.7, cancel.set)
    timer.start()
    try:
        started = time.monotonic()
        outcome, col = run(_CLOSES_OUTPUT_EARLY.format(sleep=60), BASE_PYTHON, cancel_event=cancel)
    finally:
        timer.cancel()
    assert outcome == ProcessOutcome(-1, True)
    assert col.lines == ["bye"]
    assert time.monotonic() - started < 5.0


_LEAVES_GRANDCHILD = """
    import subprocess, sys
    grandchild = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=sys.stdout, stderr=sys.stderr, close_fds=False,
    )
    print("grandchild", grandchild.pid, flush=True)
    sys.exit({code})
"""


def _kill_grandchild(col: Collector) -> None:
    pid = int(col.lines[0].split()[1])
    if process_alive(pid):
        os.kill(pid, signal.SIGTERM)


def test_grandchild_holding_the_pipe_does_not_hang() -> None:
    # The child starts a grandchild that inherits stdout and outlives it (like an
    # installer started by winget). stream_process must give up waiting for EOF.
    started = time.monotonic()
    outcome, col = run(_LEAVES_GRANDCHILD.format(code=3), BASE_PYTHON)
    elapsed = time.monotonic() - started
    try:
        assert outcome == ProcessOutcome(3, False)
        assert elapsed < 10.0, f"stream_process waited {elapsed:.1f}s for EOF"
    finally:
        _kill_grandchild(col)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG winget.stream_process: a cancel that arrives after the process already exited "
        "(during the up-to-3 s wait for EOF while a grandchild holds the pipe, or while "
        "queued output is still delivered) still marks the run cancelled and returns -1, so "
        "a finished, successful winget install is reported as CANCELLED. Fix: only stop/mark "
        "cancelled when proc.poll() is None."
    ),
)
def test_cancel_after_process_exited_keeps_real_exit_code() -> None:
    cancel = threading.Event()
    timer = threading.Timer(1.0, cancel.set)  # child exits at ~0.1 s, drain lasts 3 s
    timer.start()
    try:
        outcome, col = run(_LEAVES_GRANDCHILD.format(code=0), BASE_PYTHON, cancel_event=cancel)
    finally:
        timer.cancel()
        _kill_grandchild(col)
    assert outcome == ProcessOutcome(0, False)
