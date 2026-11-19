"""Performance of the 2.0 core, compared with the 1.1.8 numbers in baseline.json.

Thresholds are relative to the baseline scaled by ``machine_factor`` where both
sides measure the same thing, otherwise absolute budgets with a wide margin.
``WINSTALLER_PERF_SLACK`` multiplies every budget. See tests/perf/README.md.
"""

from __future__ import annotations

import json
import re
import statistics
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest

from tests.perf import corpus
from tests.perf.conftest import bench_median, median_seconds, report, slack
from winstaller.core.actions import ActionOutcome
from winstaller.core.catalog import Catalog, Category, Item, ItemKind, load_catalog
from winstaller.core.runner import NullListener, TaskResult, TaskRunner
from winstaller.core.winget import StreamParser, stream_process

pytestmark = pytest.mark.perf

CREATE_NO_WINDOW = 0x08000000

#: Absolute floors/budgets (already generous; multiplied by WINSTALLER_PERF_SLACK).
MIN_PARSER_MB_S = 2.0
MAX_LOAD_CATALOG_S = 0.050
MAX_RUNNER_PER_TASK_S = 200e-6
MIN_STREAM_LINES_PER_S = 20_000
MAX_SMOKE_S = 1.5

CORE_MODULES = (
    "winstaller.core.version",
    "winstaller.core.platform",
    "winstaller.core.catalog",
    "winstaller.core.winget",
    "winstaller.core.actions",
    "winstaller.core.runner",
    "winstaller.core.updater",
)
HEAVY_MODULES = ("PySide6", "shiboken6", "requests", "urllib3", "tkinter", "customtkinter")


def parse_all(chunks: list[bytes]) -> int:
    parser = StreamParser()
    events = 0
    for chunk in chunks:
        events += len(parser.feed(chunk))
    return events + len(parser.flush())


def run_python(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=True,
        creationflags=CREATE_NO_WINDOW,
    )


def process_seconds(args: list[str], repeat: int) -> list[float]:
    runs = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        run_python(*args)
        runs.append(time.perf_counter() - t0)
    return runs


# ---------------------------------------------------------- stream parser


class TestStreamParser:
    def test_corpus_is_the_one_baseline_was_measured_with(self, baseline: dict[str, Any]) -> None:
        assert baseline["corpus"]["sha256"] == corpus.corpus_sha256(), (
            "tests/data samples changed: re-run tests/perf/measure_baseline.py"
        )
        assert baseline["corpus"]["read_size"] == corpus.OLD_READ_SIZE

    def test_throughput_not_below_118_cpu_cost(
        self, benchmark, baseline: dict[str, Any], machine_factor: float
    ) -> None:
        old = baseline["stream"]["cpu_only"]
        data = corpus.corpus(512 * 1024)
        assert len(data) == old["input_bytes"]
        chunks = corpus.chunks(data)  # same 512-byte reads as 1.1.8
        seconds = bench_median(benchmark, lambda: parse_all(chunks), rounds=9)
        mb_s = len(data) / 1e6 / seconds
        lines_s = data.count(b"\n") / seconds
        old_here = old["mb_per_s"] / machine_factor
        report(
            "parser MB/s (512 B reads)",
            f"{mb_s:.1f}",
            f"{old['mb_per_s']:.1f}",
            f"1.1.8 CPU-only, x{mb_s / old_here:.1f} after calibration",
        )
        report("parser input lines/s", f"{lines_s:,.0f}", f"{old['lines_per_s']:,.0f}", "CPU-only")
        benchmark.extra_info.update(mb_per_s=mb_s, old_mb_per_s_scaled=old_here)
        assert mb_s >= 0.8 * old_here / slack(), f"{mb_s:.2f} MB/s vs 1.1.8 {old_here:.2f} MB/s"
        assert mb_s >= MIN_PARSER_MB_S / slack()

    def test_far_faster_than_118_real_reader_with_sleep(self, baseline: dict[str, Any]) -> None:
        # 1.1.8 slept 10 ms per 512-byte read: its real throughput was bounded by the sleep.
        old = baseline["stream"]["with_sleep"]
        data = corpus.corpus(old["input_bytes"])
        chunks = corpus.chunks(data)
        seconds = median_seconds(lambda: parse_all(chunks), repeat=7)
        mb_s = len(data) / 1e6 / seconds
        report(
            "parser MB/s vs 1.1.8 real reader",
            f"{mb_s:.1f}",
            f"{old['mb_per_s']:.3f}",
            "1.1.8 incl. time.sleep(0.01)/chunk",
        )
        assert mb_s >= 10 * old["mb_per_s"] / slack()

    def test_large_reads_not_slower_than_small(self) -> None:
        data = corpus.corpus(512 * 1024)
        small = median_seconds(lambda: parse_all(corpus.chunks(data, 512)), repeat=5)
        large = median_seconds(lambda: parse_all(corpus.chunks(data, 65536)), repeat=5)
        report("parser MB/s (64 KiB reads, as 2.0 reads)", f"{len(data) / 1e6 / large:.1f}")
        assert large <= small * 1.5 * slack()

    def test_linear_in_input_size(self, baseline: dict[str, Any]) -> None:
        # Equal total bytes per measurement: 1x input fed 16 times, 4x 4 times, 16x once.
        per_byte: dict[str, float] = {}
        scales = corpus.PROGRESS_SCALES
        for label, updates in scales.items():
            data = corpus.progress_stream(updates)
            chunks = corpus.chunks(data)
            repeats = scales["16x"] // updates

            def work(c: list[bytes] = chunks, n: int = repeats) -> None:
                for _ in range(n):
                    parse_all(c)

            runs = []
            for _ in range(7):
                t0 = time.perf_counter()
                work()
                runs.append(time.perf_counter() - t0)
            per_byte[label] = min(runs) / (len(data) * repeats)
        growth_4 = per_byte["4x"] / per_byte["1x"]
        growth_16 = per_byte["16x"] / per_byte["1x"]
        old = baseline["stream_complexity"]
        old_ratio = old["16x"]["seconds"]["median"] / old["1x"]["seconds"]["median"]
        size_ratio = len(corpus.progress_stream(scales["16x"])) / len(
            corpus.progress_stream(scales["1x"])
        )
        new_ratio = growth_16 * size_ratio
        report(
            "progress stream time 16x / 1x",
            f"{new_ratio:.1f}",
            f"{old_ratio:.1f}",
            "linear = ~16, quadratic = ~256",
        )
        # Linear: cost per byte does not grow with the input. Quadratic 1.1.8: x14 per byte.
        assert growth_4 <= 2.0 * slack(), per_byte
        assert growth_16 <= 3.0 * slack(), per_byte


# ----------------------------------------------------------------- catalog


def test_load_catalog_budget(benchmark, baseline: dict[str, Any], machine_factor: float) -> None:
    seconds = bench_median(benchmark, load_catalog, rounds=30)
    old = baseline["config"]["reload_s"]["median"]
    report(
        "load_catalog()",
        f"{seconds * 1e3:.2f} ms",
        f"{old * 1e3:.2f} ms",
        "1.1.8: reload of a dict literal, no file/validation",
    )
    assert seconds <= MAX_LOAD_CATALOG_S * max(1.0, machine_factor) * slack()


# ----------------------------------------------------------------- startup


def test_core_import_does_not_load_ui_or_network_modules() -> None:
    code = textwrap.dedent(
        f"""
        import io, json, sys
        import {", ".join(CORE_MODULES)}
        after_import = sorted(m for m in sys.modules if m.split(".")[0] in {HEAVY_MODULES!r})
        from winstaller.__main__ import main
        out, sys.stdout = sys.stdout, io.StringIO()
        code = main(["--smoke"])
        sys.stdout = out
        after_smoke = sorted(m for m in sys.modules if m.split(".")[0] in {HEAVY_MODULES!r})
        print(json.dumps({{"code": code, "import": after_import, "smoke": after_smoke}}))
        """
    )
    result = json.loads(run_python("-c", code).stdout)
    assert result == {"code": 0, "import": [], "smoke": []}


_IMPORTTIME = re.compile(r"^import time:\s+\d+\s+\|\s+(\d+)\s+\|( )(\S+)\s*$")


def _core_import_seconds() -> float:
    stderr = run_python("-X", "importtime", "-c", "import " + ", ".join(CORE_MODULES)).stderr
    total_us = 0
    for line in stderr.splitlines():
        m = _IMPORTTIME.match(line)
        if m and m.group(3).startswith("winstaller"):
            total_us += int(m.group(1))
    assert total_us > 0
    return total_us / 1e6


def test_core_import_time_vs_118(baseline: dict[str, Any], machine_factor: float) -> None:
    runs = [_core_import_seconds() for _ in range(5)]
    seconds = statistics.median(runs)
    old = baseline["import"]["core.config"]["warm_import_s"]["median"]
    report(
        "import time: winstaller.core.* (all)",
        f"{seconds * 1e3:.0f} ms",
        f"{old * 1e3:.0f} ms",
        "1.1.8: import core.config, warm",
    )
    assert seconds <= old * machine_factor * slack()


def test_smoke_cold_start_vs_118(baseline: dict[str, Any], machine_factor: float) -> None:
    bare = statistics.median(process_seconds(["-c", "pass"], repeat=5))
    runs = process_seconds(["-m", "winstaller", "--smoke"], repeat=5)
    smoke = statistics.median(runs)
    old_process = baseline["import"]["core.config"]["warm_process_s"]["median"]
    old_bare = baseline["python_startup_s"]["median"]
    new_excess = max(0.0, smoke - bare)
    old_excess = max(0.0, old_process - old_bare)
    report(
        "python -m winstaller --smoke",
        f"{smoke * 1e3:.0f} ms",
        f"{old_process * 1e3:.0f} ms",
        "1.1.8: python -c 'import core.config'",
    )
    report(
        "  ...minus bare interpreter start",
        f"{new_excess * 1e3:.0f} ms",
        f"{old_excess * 1e3:.0f} ms",
        f"first run {runs[0] * 1e3:.0f} ms",
    )
    assert smoke <= MAX_SMOKE_S * slack()
    assert new_excess <= old_excess * machine_factor * slack()


# ------------------------------------------------------------------ runner


class _Counter(NullListener):
    def __init__(self) -> None:
        self.started = 0
        self.finished = 0
        self.lines = 0

    def task_started(self, task) -> None:
        self.started += 1

    def output_line(self, text: str) -> None:
        self.lines += 1

    def task_finished(self, result: TaskResult) -> None:
        self.finished += 1


def test_runner_overhead_per_task(benchmark, tmp_path: Path) -> None:
    items = [
        Item(id=f"i{n}", name=f"Item {n}", category="c", kind=ItemKind.WINGET, winget_id=f"V.I{n}")
        for n in range(1000)
    ]
    catalog = Catalog([Category("c", "C", "", 0)], items, [])
    ids = [i.id for i in items]
    outcome = ActionOutcome(True, "ok")
    counters: list[_Counter] = []

    def run_all() -> None:
        listener = _Counter()
        runner = TaskRunner(
            catalog, listener=listener, executor=lambda i, c: outcome, arch="x64", app_dir=tmp_path
        )
        summary = runner.run(ids)
        assert summary.ok_count == 1000
        counters.append(listener)

    seconds = bench_median(benchmark, run_all, rounds=9)
    per_task = seconds / 1000
    report("TaskRunner overhead per task", f"{per_task * 1e6:.1f} us", "", "1000 no-op tasks")
    last = counters[-1]
    assert (last.started, last.finished, last.lines) == (1000, 1000, 2000)
    assert per_task <= MAX_RUNNER_PER_TASK_S * slack()


# ---------------------------------------------------------- stream_process


_EMIT_LINES = textwrap.dedent(
    """
    import sys
    w = sys.stdout.write
    for i in range(50000):
        w(f"line {i} Pomyślnie zainstalowano pakiet\\n")
    """
)


def test_stream_process_end_to_end_50k_lines() -> None:
    expected = [f"line {i} Pomyślnie zainstalowano pakiet" for i in range(50000)]
    rates = []
    for _ in range(3):
        lines: list[str] = []
        replaced: list[str] = []
        t0 = time.perf_counter()
        outcome = stream_process(
            [sys.executable, "-X", "utf8", "-c", _EMIT_LINES],
            on_line=lines.append,
            on_replace_last=replaced.append,
        )
        seconds = time.perf_counter() - t0
        assert outcome.returncode == 0 and not outcome.cancelled
        assert lines == expected and replaced == []
        rates.append(len(lines) / seconds)
    rate = statistics.median(rates)
    report("stream_process lines/s (50k lines, incl. start)", f"{rate:,.0f}", "", "")
    assert rate >= MIN_STREAM_LINES_PER_S / slack()
