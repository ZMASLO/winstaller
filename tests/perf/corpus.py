"""Inputs shared by the 1.1.8 baseline script and the 2.0 perf tests.

Pure standard library and no ``winstaller`` imports: ``measure_baseline.py`` runs
it in a separate venv next to the old code.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

#: Console captures (see tests/data/make_synthetic.py); order matters for the hash.
SAMPLE_FILES = (
    "winget_show_7zip.bin",
    "winget_search_chrome.bin",
    "winget_install_synthetic.bin",
    "polish_utf8.bin",
    "polish_cp1250.bin",
    "polish_cp852.bin",
)

#: Chunk size of a pipe read in 1.1.8 (``pipe.read(512)``); used for both versions.
OLD_READ_SIZE = 512

#: Progress-bar-only streams for the complexity comparison (number of ``\r`` updates).
PROGRESS_SCALES = {"1x": 64, "4x": 256, "16x": 1024}


def samples_bytes() -> bytes:
    return b"".join((DATA_DIR / name).read_bytes() for name in SAMPLE_FILES)


def corpus(target_bytes: int) -> bytes:
    """Whole repetitions of all samples, at least ``target_bytes`` long."""
    unit = samples_bytes()
    repeats = max(1, -(-target_bytes // len(unit)))
    return unit * repeats


def corpus_sha256() -> str:
    return hashlib.sha256(samples_bytes()).hexdigest()


def chunks(data: bytes, size: int = OLD_READ_SIZE) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


def progress_stream(updates: int) -> bytes:
    """A winget-like download: one physical line rewritten with ``\\r`` many times.

    1.1.8 re-scanned its whole buffer on every read until a ``\\n`` arrived, so
    this input is quadratic for it and linear for the 2.0 parser.
    """
    parts = [b"Pobieranie https://example.invalid/setup.exe\r\n"]
    width = 30
    for n in range(updates):
        filled = width * n // max(1, updates - 1)
        bar = "█" * filled + "▒" * (width - filled)
        parts.append(f"\r\x1b[0G  {bar}  {n * 16} KB / {updates * 16} KB".encode())
    parts.append("\r\nPomyślnie zainstalowano\r\n".encode())
    return b"".join(parts)


_WORDS = re.compile(rb"[A-Za-z]+")


def calibration_workload() -> int:
    """Fixed CPU workload (bytecode loop + regex + bytes ops) used to compare machines.

    Timed in the same session as a measurement, it turns "this machine is N times
    slower than the one that produced baseline.json" into a number.
    """
    data = corpus(64 * 1024)
    total = 0
    for _ in range(24):
        for line in data.split(b"\n"):
            total += len(line.strip())
        total += sum(data[i] for i in range(0, len(data), 3))
        total += len(_WORDS.findall(data))
        total += len(data.decode("utf-8", errors="replace"))
    return total
