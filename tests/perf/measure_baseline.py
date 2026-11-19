"""Measure Winstaller 1.1.8 and write ``tests/perf/baseline.json``.

Run manually (it is not a test). The old code is not importable from the repo
once stage 3 removes it, so extract it from the ``main`` branch first and use a
separate venv with the old dependencies::

    $B = "$env:TEMP\\winstaller-baseline"
    New-Item -ItemType Directory -Force "$B\\old118" | Out-Null
    git archive main | tar -x -C "$B\\old118"
    py -3.12 -m venv "$B\\venv"
    & "$B\\venv\\Scripts\\python" -m pip install "customtkinter==5.2.2" darkdetect requests
    & "$B\\venv\\Scripts\\python" tests\\perf\\measure_baseline.py --old-root "$B\\old118"

Measured (all on the old code, on this machine):

* ``stream``: throughput of 1.1.8 ``core.installers._read_stream`` fed from an
  in-memory pipe (``read(512)`` like the real pipe) with the shared corpus
  (tests/perf/corpus.py), with the original ``time.sleep(0.01)`` per chunk
  (real 1.x behaviour) and with the sleep disabled (pure CPU cost);
* ``stream_complexity``: the same function on a progress-bar-only stream at
  1x/4x/16x size (1.1.8 re-scans its whole buffer per chunk);
* ``import``: ``python -X importtime -c "import core.config"`` / ``"import gui.app"``
  in fresh processes, cold (old ``__pycache__`` deleted first, so it includes
  byte-compiling the old modules; the OS file cache is *not* flushed) and warm;
* ``python_startup``: ``python -c pass``, to separate interpreter start-up;
* ``window``: a fresh process builds ``gui.app.ModernApp`` plus all checkboxes
  exactly like ``main.py`` and calls ``update_idletasks()`` (no ``mainloop``, no
  elevation, no winget check). The window appears for a moment;
* ``config``: re-executing ``core.config`` (``importlib.reload``) = building the
  1.x configuration structure, the closest equivalent of ``load_catalog()``;
* ``calibration``: ``corpus.calibration_workload()``; perf tests time it again to
  scale baseline numbers to the machine they run on.

Nothing here touches the system besides starting Python processes and showing
the old window briefly.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime as dt
import importlib
import io
import json
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import corpus  # noqa: E402  (sibling module, deliberately not a package import)

CREATE_NO_WINDOW = 0x08000000
SCHEMA = 1


# ------------------------------------------------------------------ stats


def summarize(values: Sequence[float], unit: str) -> dict[str, Any]:
    vals = sorted(values)
    return {
        "unit": unit,
        "n": len(vals),
        "median": statistics.median(vals),
        "min": vals[0],
        "max": vals[-1],
        "stdev": statistics.stdev(vals) if len(vals) > 1 else 0.0,
        "runs": [round(v, 6) for v in values],
    }


def timed(fn: Callable[[], Any], repeat: int, warmup: int = 1) -> list[float]:
    for _ in range(warmup):
        fn()
    out = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t0)
    return out


# ---------------------------------------------------------------- machine


def _cpu_name() -> str:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        ) as key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except OSError:
        return platform.processor()


def _ram_gb() -> float | None:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    try:
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return round(status.ullTotalPhys / 2**30, 1)
    except (OSError, AttributeError):
        pass
    return None


def machine_info(python: str) -> dict[str, Any]:
    probe = subprocess.run(
        [python, "-c", "import sys, platform; print(sys.version); print(platform.machine())"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    try:
        import customtkinter

        ctk_version = customtkinter.__version__
    except ImportError:
        ctk_version = None
    return {
        "cpu": _cpu_name(),
        "logical_cpus": os.cpu_count(),
        "ram_gb": _ram_gb(),
        "os": platform.platform(),
        "machine": platform.machine(),
        "python": probe[0],
        "python_machine": probe[1],
        "customtkinter": ctk_version,
    }


# ----------------------------------------------------------------- stream


class _Sink:
    def __init__(self) -> None:
        self.writes = 0
        self.replaces = 0

    def write(self, text: str) -> None:
        self.writes += 1

    def replace_last_line(self, text: str) -> None:
        self.replaces += 1


def _run_old_stream(installers: Any, data: bytes) -> _Sink:
    sink = _Sink()
    installers._read_stream(io.BytesIO(data), sink)
    return sink


@contextlib.contextmanager
def _sleep_disabled(installers: Any):
    original = installers.time
    installers.time = SimpleNamespace(sleep=lambda seconds: None)
    try:
        yield
    finally:
        installers.time = original


def measure_stream(installers: Any, repeat: int) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for label, size, sleep in (("cpu_only", 512 * 1024, False), ("with_sleep", 24 * 1024, True)):
        data = corpus.corpus(size)
        lines = data.count(b"\n")
        ctx = contextlib.nullcontext() if sleep else _sleep_disabled(installers)
        with ctx:
            runs = timed(lambda d=data: _run_old_stream(installers, d), repeat if not sleep else 3)
            sink = _run_old_stream(installers, data)
        results[label] = {
            "input_bytes": len(data),
            "input_lines": lines,
            "events": sink.writes + sink.replaces,
            "seconds": summarize(runs, "s"),
            "mb_per_s": statistics.median(len(data) / 1e6 / r for r in runs),
            "lines_per_s": statistics.median(lines / r for r in runs),
            "sleep_per_chunk_s": 0.01 if sleep else 0.0,
        }
    return results


def measure_stream_complexity(installers: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    with _sleep_disabled(installers):
        for label, updates in corpus.PROGRESS_SCALES.items():
            data = corpus.progress_stream(updates)
            runs = timed(lambda d=data: _run_old_stream(installers, d), 3, warmup=0)
            out[label] = {
                "updates": updates,
                "input_bytes": len(data),
                "seconds": summarize(runs, "s"),
            }
    base = out["1x"]["seconds"]["median"]
    for label in out:
        out[label]["ratio_to_1x"] = out[label]["seconds"]["median"] / base
    return out


# ---------------------------------------------------------------- imports


_IMPORTTIME = re.compile(r"^import time:\s+(\d+)\s+\|\s+(\d+)\s+\|(\s*)(\S+)\s*$")


def importtime_cumulative_us(stderr: str, module: str) -> int:
    for line in stderr.splitlines():
        m = _IMPORTTIME.match(line)
        if m and m.group(4) == module and len(m.group(3)) == 1:
            return int(m.group(2))
    raise RuntimeError(f"no importtime entry for {module}")


def _clear_pycache(root: Path) -> None:
    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def measure_imports(python: str, old_root: Path, repeat: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for module in ("core.config", "gui.app"):
        cold_wall, cold_import, warm_wall, warm_import = [], [], [], []
        for n in range(1 + repeat):
            if n == 0:
                _clear_pycache(old_root)
            t0 = time.perf_counter()
            proc = subprocess.run(
                [python, "-X", "importtime", "-c", f"import {module}"],
                cwd=old_root,
                capture_output=True,
                text=True,
                creationflags=CREATE_NO_WINDOW,
                check=True,
            )
            wall = time.perf_counter() - t0
            cumulative = importtime_cumulative_us(proc.stderr, module) / 1e6
            if n == 0:
                cold_wall.append(wall)
                cold_import.append(cumulative)
            else:
                warm_wall.append(wall)
                warm_import.append(cumulative)
        out[module] = {
            "cold_import_s": cold_import[0],
            "cold_process_s": cold_wall[0],
            "warm_import_s": summarize(warm_import, "s"),
            "warm_process_s": summarize(warm_wall, "s"),
        }
    return out


def measure_python_startup(python: str, repeat: int) -> dict[str, Any]:
    def once() -> None:
        subprocess.run([python, "-c", "pass"], check=True, creationflags=CREATE_NO_WINDOW)

    return summarize(timed(once, repeat), "s")


# ----------------------------------------------------------------- window


_WINDOW_CHILD = textwrap.dedent(
    """
    import json, sys, time
    t0 = time.perf_counter()
    sys.path.insert(0, {root!r})
    from gui.app import ModernApp
    from core.config import CATEGORIES
    t_import = time.perf_counter()
    app = ModernApp()
    t_ctor = time.perf_counter()
    count = 0
    for category_name, items in CATEGORIES.items():
        app.create_category_label(category_name)
        for item_name in items:
            app.create_checkbox(item_name)
            count += 1
    t_boxes = time.perf_counter()
    app.update_idletasks()
    t_idle = time.perf_counter()
    sys.__stdout__.write(json.dumps({{
        "import_s": t_import - t0,
        "construct_s": t_ctor - t_import,
        "checkboxes_s": t_boxes - t_ctor,
        "idletasks_s": t_idle - t_boxes,
        "checkboxes": count,
    }}) + "\\n")
    sys.__stdout__.flush()
    app.destroy()
    """
)


def measure_window(python: str, old_root: Path, repeat: int) -> dict[str, Any]:
    walls: list[float] = []
    parts: list[dict[str, float]] = []
    code = _WINDOW_CHILD.format(root=str(old_root))
    for _ in range(repeat):
        t0 = time.perf_counter()
        # CREATE_NO_WINDOW: ModernApp hides the console of its process; with no console
        # GetConsoleWindow() returns 0 and the caller's console stays visible.
        proc = subprocess.Popen(
            [python, "-c", code],
            cwd=old_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
        )
        assert proc.stdout is not None
        line = proc.stdout.readline()
        walls.append(time.perf_counter() - t0)
        _, err = proc.communicate(timeout=60)
        if not line:
            raise RuntimeError(f"window child failed: {err.decode(errors='replace')}")
        parts.append(json.loads(line))
    breakdown = {
        key: summarize([p[key] for p in parts], "s")
        for key in ("import_s", "construct_s", "checkboxes_s", "idletasks_s")
    }
    return {
        "time_to_window_s": summarize(walls, "s"),
        "checkboxes": parts[0]["checkboxes"],
        "breakdown": breakdown,
    }


# ----------------------------------------------------------------- config


def measure_config(repeat: int) -> dict[str, Any]:
    import core.config as config

    runs = timed(lambda: importlib.reload(config), repeat)
    names = sum(len(v) for v in config.CATEGORIES.values())
    return {"reload_s": summarize(runs, "s"), "entries": names}


# ------------------------------------------------------------------- main


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--old-root", required=True, type=Path, help="extracted 1.1.8 code")
    parser.add_argument("--out", type=Path, default=HERE / "baseline.json")
    parser.add_argument("--repeat", type=int, default=7, help="repetitions per measurement")
    args = parser.parse_args(argv)

    old_root: Path = args.old_root.resolve()
    if not (old_root / "core" / "installers.py").is_file():
        parser.error(f"{old_root} does not contain the 1.1.8 code (core/installers.py)")
    sys.path.insert(0, str(old_root))
    import core.installers as installers

    python = sys.executable
    repeat = args.repeat
    print("calibration...", flush=True)
    calibration = summarize(timed(corpus.calibration_workload, max(repeat, 15)), "s")
    print("stream...", flush=True)
    stream = measure_stream(installers, repeat)
    print("stream complexity...", flush=True)
    complexity = measure_stream_complexity(installers)
    print("python startup...", flush=True)
    startup = measure_python_startup(python, max(repeat, 10))
    print("imports...", flush=True)
    imports = measure_imports(python, old_root, repeat)
    print("window...", flush=True)
    window = measure_window(python, old_root, repeat)
    print("config...", flush=True)
    config = measure_config(max(repeat, 30))

    result = {
        "schema": SCHEMA,
        "version": "1.1.8",
        "date": dt.date.today().strftime("%Y.%m.%d"),
        "machine": machine_info(python),
        "repeat": repeat,
        "corpus": {
            "files": list(corpus.SAMPLE_FILES),
            "sha256": corpus.corpus_sha256(),
            "read_size": corpus.OLD_READ_SIZE,
        },
        "calibration_s": calibration,
        "stream": stream,
        "stream_complexity": complexity,
        "python_startup_s": startup,
        "import": imports,
        "window": window,
        "config": config,
    }
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"written {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
