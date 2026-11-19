"""Perf fixtures: baseline.json, machine calibration, slack, and an end-of-run summary."""

from __future__ import annotations

import json
import os
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.perf import corpus

BASELINE_PATH = Path(__file__).resolve().parent / "baseline.json"

#: Collected by tests, printed by pytest_terminal_summary.
REPORT: list[tuple[str, str, str, str]] = []


def slack() -> float:
    """``WINSTALLER_PERF_SLACK`` loosens every threshold (default 1.0, e.g. 2 on slow CI)."""
    try:
        value = float(os.environ.get("WINSTALLER_PERF_SLACK", "1"))
    except ValueError:
        return 1.0
    return max(1.0, value)


def median_seconds(fn: Callable[[], Any], repeat: int, warmup: int = 1) -> float:
    for _ in range(warmup):
        fn()
    runs = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        runs.append(time.perf_counter() - t0)
    return statistics.median(runs)


def bench_median(benchmark: Any, fn: Callable[[], Any], rounds: int) -> float:
    """Median seconds of ``fn`` via pytest-benchmark (own timing if it is disabled)."""
    benchmark.pedantic(fn, rounds=rounds, iterations=1, warmup_rounds=1)
    stats = getattr(benchmark, "stats", None)
    if stats is None:
        return median_seconds(fn, rounds, warmup=0)
    return float(stats.stats.median)


def report(metric: str, new: str, old: str = "", note: str = "") -> None:
    REPORT.append((metric, new, old, note))


@pytest.fixture(scope="session")
def baseline() -> dict[str, Any]:
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert data["version"] == "1.1.8"
    return data


@pytest.fixture(scope="session")
def machine_factor(baseline: dict[str, Any]) -> float:
    """How many times slower this machine runs the calibration workload than the
    machine that produced baseline.json (>1 = slower). Clamped to [0.2, 10]."""
    now = median_seconds(corpus.calibration_workload, repeat=15, warmup=2)
    factor = now / baseline["calibration_s"]["median"]
    factor = min(10.0, max(0.2, factor))
    report("machine factor (calibration)", f"{factor:.2f}x", "1.00x", "scales baseline numbers")
    return factor


def pytest_terminal_summary(terminalreporter: Any) -> None:
    if not REPORT:
        return
    tr = terminalreporter
    tr.section("Winstaller perf: 2.0 vs 1.1.8 baseline")
    width = max(len(r[0]) for r in REPORT)
    tr.write_line(f"{'metric'.ljust(width)}  {'2.0':>16}  {'1.1.8':>16}  note")
    for metric, new, old, note in REPORT:
        tr.write_line(f"{metric.ljust(width)}  {new:>16}  {old:>16}  {note}")
    tr.write_line(f"WINSTALLER_PERF_SLACK = {slack():g}")
