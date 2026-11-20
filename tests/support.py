"""Helpers shared by unit and perf tests (no fixtures here; see conftest.py)."""

from __future__ import annotations

import random
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.perf import corpus
from winstaller.core.actions import ActionContext
from winstaller.core.catalog import Catalog, Category, CategoryKind, Item, ItemKind, Profile
from winstaller.core.runner import NullListener, RunSummary, Task, TaskResult
from winstaller.core.winget import OutputEvent, StreamParser

DATA_DIR = corpus.DATA_DIR
#: Console captures used by parser unit tests (and, concatenated, by the perf corpus).
SAMPLE_FILES = corpus.SAMPLE_FILES


def sample(name: str) -> bytes:
    return (DATA_DIR / name).read_bytes()


Event = tuple[str, str]


def as_tuples(events: Iterable[OutputEvent]) -> list[Event]:
    return [(e.kind.value, e.text) for e in events]


def parse_chunks(chunks: Iterable[bytes]) -> list[Event]:
    parser = StreamParser()
    events: list[OutputEvent] = []
    for chunk in chunks:
        events += parser.feed(chunk)
    events += parser.flush()
    return as_tuples(events)


def parse_whole(data: bytes) -> list[Event]:
    return parse_chunks([data])


def split_at(data: bytes, cuts: Sequence[int]) -> list[bytes]:
    points = [0, *sorted(cuts), len(data)]
    return [data[a:b] for a, b in zip(points, points[1:], strict=False)]


def random_chunks(data: bytes, seed: int, max_size: int = 64) -> list[bytes]:
    rng = random.Random(seed)
    out: list[bytes] = []
    pos = 0
    while pos < len(data):
        size = rng.randint(1, max_size)
        out.append(data[pos : pos + size])
        pos += size
    return out


def bytewise(data: bytes) -> list[bytes]:
    return [data[i : i + 1] for i in range(len(data))]


# ----------------------------------------------------------------- catalog


def make_item(
    item_id: str = "item",
    *,
    kind: ItemKind = ItemKind.WINGET,
    category: str = "cat",
    **fields: Any,
) -> Item:
    if kind in (ItemKind.WINGET, ItemKind.WINGET_UNINSTALL):
        fields.setdefault("winget_id", f"Vendor.{item_id}")
    if kind == ItemKind.LOCAL_EXE:
        fields.setdefault("path", f"{item_id}.exe")
    if kind == ItemKind.ACTION:
        fields.setdefault("action", "theme_dark")
    if kind == ItemKind.DRIVER:
        fields.setdefault("vendor", "nvidia")
    fields.setdefault("name", item_id.upper())
    return Item(id=item_id, category=category, kind=kind, **fields)


def make_catalog(
    items: Iterable[Item],
    *,
    categories: Iterable[Category] | None = None,
    profiles: Iterable[Profile] = (),
) -> Catalog:
    items = list(items)
    if categories is None:
        cat_ids = list(dict.fromkeys(i.category for i in items)) or ["cat"]
        categories = [Category(c, c.title(), "", n) for n, c in enumerate(cat_ids)]
    return Catalog(categories, items, profiles)


def minimal_data(**overrides: Any) -> dict[str, Any]:
    """Smallest valid ``parse_catalog`` input; override sections as needed."""
    data: dict[str, Any] = {
        "categories": [{"id": "cat", "name": "Kategoria"}],
        "items": [
            {"id": "a", "name": "A", "category": "cat", "kind": "winget", "winget_id": "V.A"},
        ],
        "profiles": [],
    }
    data.update(overrides)
    return data


TOOLS = CategoryKind.TOOLS


# ----------------------------------------------------------------- contexts


@dataclass
class CtxRecorder:
    lines: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)

    def log(self, text: str) -> None:
        self.lines.append(text)

    def replace_last(self, text: str) -> None:
        self.replaced.append(text)


def make_ctx(
    app_dir: Path,
    item: Item | None = None,
    *,
    cancel_event: threading.Event | None = None,
    arch: str = "x64",
) -> tuple[ActionContext, CtxRecorder]:
    rec = CtxRecorder()
    ctx = ActionContext(
        item=item or make_item(),
        log=rec.log,
        replace_last=rec.replace_last,
        cancel_event=cancel_event or threading.Event(),
        app_dir=app_dir,
        arch=arch,  # type: ignore[arg-type]
    )
    return ctx, rec


# ----------------------------------------------------------------- listener


class RecordingListener(NullListener):
    """Records every listener call as ``(method, payload)``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.threads: set[int] = set()

    def _rec(self, name: str, payload: Any) -> None:
        self.threads.add(threading.get_ident())
        self.events.append((name, payload))

    def run_started(self, tasks: Sequence[Task]) -> None:
        self._rec("run_started", [t.item_id for t in tasks])

    def task_started(self, task: Task) -> None:
        self._rec("task_started", task.item_id)

    def output_line(self, text: str) -> None:
        self._rec("output_line", text)

    def output_replace_last(self, text: str) -> None:
        self._rec("output_replace_last", text)

    def task_finished(self, result: TaskResult) -> None:
        self._rec("task_finished", (result.item_id, result.status.value))

    def run_finished(self, summary: RunSummary) -> None:
        self._rec("run_finished", summary)

    def names(self, *, skip_output: bool = True) -> list[tuple[str, Any]]:
        return [
            (n, p)
            for n, p in self.events
            if not (skip_output and n in ("output_line", "output_replace_last"))
        ]
