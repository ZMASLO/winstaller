"""Measure size and start-up time of a built ``winstaller.exe`` and check them against budgets.

Runs ONLY ``<exe> --smoke`` (never anything that could ask for UAC or open a window).

* cold start: first run of a freshly copied exe (new path => not in Defender's cache yet,
  onefile archive not yet in the file cache); each repetition uses its own copy;
* warm start: repeated runs of one exe after a warm-up run.

The onefile bootloader unpacks to a new ``_MEI*`` directory on every start, so "warm" still
includes unpacking; it only benefits from OS caches.

Usage::

    python tools/measure_exe.py dist/winstaller.exe --json measure.json
    python tools/measure_exe.py dist/winstaller.exe --max-size-mb 40 --max-cold-s 6 --max-warm-s 3

Budgets default to the environment variables ``WINSTALLER_BUDGET_SIZE_MB``,
``WINSTALLER_BUDGET_COLD_S`` and ``WINSTALLER_BUDGET_WARM_S`` (unset = not checked).
When ``GITHUB_STEP_SUMMARY`` is set a markdown table is appended to it.
Exit code: 0 OK, 1 budget exceeded, 2 the smoke run failed.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

SMOKE_MARKER = "core modules:"
ENV_SIZE = "WINSTALLER_BUDGET_SIZE_MB"
ENV_COLD = "WINSTALLER_BUDGET_COLD_S"
ENV_WARM = "WINSTALLER_BUDGET_WARM_S"


@dataclass
class Measurement:
    exe: str
    arch: str
    size_bytes: int
    size_mb: float
    cold_runs_s: list[float] = field(default_factory=list)
    warm_runs_s: list[float] = field(default_factory=list)
    cold_median_s: float | None = None
    warm_median_s: float | None = None
    smoke_output: str = ""
    budgets: dict[str, float | None] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)


class SmokeFailed(RuntimeError):
    pass


def run_smoke(exe: Path, *, timeout: float) -> tuple[float, str]:
    """Run ``exe --smoke`` once; return (seconds, stdout). Raises SmokeFailed."""
    start = time.perf_counter()
    try:
        # Piped std handles are inherited by the windowed onefile child, so stdout is captured
        # even though the exe has no console.
        proc = subprocess.run(
            [str(exe), "--smoke"],
            capture_output=True,
            timeout=timeout,
            cwd=exe.parent,
        )
    except subprocess.TimeoutExpired as exc:
        raise SmokeFailed(f"{exe.name} --smoke: timeout after {timeout:.0f} s") from exc
    elapsed = time.perf_counter() - start
    out = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace")
        raise SmokeFailed(f"{exe.name} --smoke: exit code {proc.returncode}\n{out}\n{err}")
    if SMOKE_MARKER not in out:
        raise SmokeFailed(f"{exe.name} --smoke: marker {SMOKE_MARKER!r} missing in output:\n{out}")
    return elapsed, out


def check_budgets(m: Measurement, budgets: dict[str, float | None]) -> list[str]:
    """Pure: list of human readable budget violations."""
    violations: list[str] = []
    checks = (
        ("size_mb", m.size_mb, "MB"),
        ("cold_s", m.cold_median_s, "s"),
        ("warm_s", m.warm_median_s, "s"),
    )
    for key, value, unit in checks:
        limit = budgets.get(key)
        if limit is not None and value is not None and value > limit:
            violations.append(f"{key}: {value:.2f} {unit} > budget {limit:.2f} {unit}")
    return violations


def markdown_table(m: Measurement) -> str:
    def fmt(value: float | None, unit: str) -> str:
        return "-" if value is None else f"{value:.2f} {unit}"

    rows = [
        ("Rozmiar", fmt(m.size_mb, "MB"), fmt(m.budgets.get("size_mb"), "MB")),
        ("Zimny start (mediana)", fmt(m.cold_median_s, "s"), fmt(m.budgets.get("cold_s"), "s")),
        ("Ciepły start (mediana)", fmt(m.warm_median_s, "s"), fmt(m.budgets.get("warm_s"), "s")),
    ]
    lines = [
        f"### winstaller.exe ({m.arch})",
        "",
        "| Metryka | Wynik | Budżet |",
        "|---|---|---|",
        *(f"| {a} | {b} | {c} |" for a, b, c in rows),
        "",
        "Wynik: "
        + ("**przekroczony budżet**: " + "; ".join(m.violations) if m.violations else "OK"),
        "",
    ]
    return "\n".join(lines)


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else None


def measure(exe: Path, *, cold: int, warm: int, timeout: float) -> Measurement:
    size = exe.stat().st_size
    m = Measurement(
        exe=str(exe),
        arch=platform.machine().lower().replace("amd64", "x64"),
        size_bytes=size,
        size_mb=round(size / (1024 * 1024), 2),
    )
    with tempfile.TemporaryDirectory(prefix="winstaller-measure-") as tmp:
        for i in range(cold):
            copy_dir = Path(tmp) / f"cold{i}"
            copy_dir.mkdir()
            copy = copy_dir / exe.name
            shutil.copy2(exe, copy)
            elapsed, out = run_smoke(copy, timeout=timeout)
            m.cold_runs_s.append(round(elapsed, 3))
            m.smoke_output = out
        if warm:
            run_smoke(exe, timeout=timeout)  # warm-up, not counted
            for _ in range(warm):
                elapsed, out = run_smoke(exe, timeout=timeout)
                m.warm_runs_s.append(round(elapsed, 3))
                m.smoke_output = out
    if m.cold_runs_s:
        m.cold_median_s = round(statistics.median(m.cold_runs_s), 3)
    if m.warm_runs_s:
        m.warm_median_s = round(statistics.median(m.warm_runs_s), 3)
    return m


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("exe", type=Path)
    parser.add_argument("--cold", type=int, default=3, help="cold runs (default 3)")
    parser.add_argument("--warm", type=int, default=5, help="warm runs (default 5)")
    parser.add_argument("--timeout", type=float, default=120.0, help="per run, seconds")
    parser.add_argument("--json", type=Path, help="write results to this JSON file")
    parser.add_argument("--max-size-mb", type=float, default=_env_float(ENV_SIZE))
    parser.add_argument("--max-cold-s", type=float, default=_env_float(ENV_COLD))
    parser.add_argument("--max-warm-s", type=float, default=_env_float(ENV_WARM))
    args = parser.parse_args(argv)

    exe: Path = args.exe.resolve()
    if not exe.is_file():
        print(f"error: {exe} not found", file=sys.stderr)
        return 2
    try:
        m = measure(exe, cold=args.cold, warm=args.warm, timeout=args.timeout)
    except SmokeFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    m.budgets = {"size_mb": args.max_size_mb, "cold_s": args.max_cold_s, "warm_s": args.max_warm_s}
    m.violations = check_budgets(m, m.budgets)

    print(f"exe:   {m.exe}")
    print(f"size:  {m.size_mb:.2f} MB ({m.size_bytes} B)")
    print(f"cold:  median {m.cold_median_s} s  runs {m.cold_runs_s}")
    print(f"warm:  median {m.warm_median_s} s  runs {m.warm_runs_s}")
    print("smoke: " + " | ".join(m.smoke_output.strip().splitlines()))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(asdict(m), indent=2), encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(markdown_table(m) + "\n")

    for v in m.violations:
        print(f"BUDGET EXCEEDED: {v}", file=sys.stderr)
    return 1 if m.violations else 0


if __name__ == "__main__":
    sys.exit(main())
