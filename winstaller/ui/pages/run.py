"""Przebieg: live queue status, progress, Stop, log; summary with retry when done."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QElapsedTimer,
    QModelIndex,
    QPersistentModelIndex,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QListView,
    QProgressBar,
    QStackedWidget,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from winstaller.core.catalog import Item
from winstaller.core.runner import RunSummary, TaskResult, TaskStatus
from winstaller.ui.bridge import LogStore
from winstaller.ui.theme import Role, ThemeManager, font
from winstaller.ui.widgets.common import (
    CursorBlock,
    button,
    format_clock,
    format_duration,
    hbox,
    label,
    page_layout,
    section_title,
)
from winstaller.ui.widgets.log_view import LogView

STATUS_TAGS: dict[TaskStatus, str] = {
    TaskStatus.PENDING: "[OCZEKUJE]",
    TaskStatus.RUNNING: "[W TOKU]",
    TaskStatus.OK: "[OK]",
    TaskStatus.FAILED: "[BŁĄD]",
    TaskStatus.SKIPPED: "[POMINIĘTO]",
    TaskStatus.CANCELLED: "[ANULOWANO]",
}


@dataclass(slots=True)
class TaskRow:
    item: Item
    status: TaskStatus = TaskStatus.PENDING
    message: str = ""
    duration: float | None = None


class RunTaskModel(QAbstractListModel):
    """One row per queued task; updated only from the GUI thread."""

    StatusRole = Qt.ItemDataRole.UserRole + 1
    MessageRole = Qt.ItemDataRole.UserRole + 2
    ItemIdRole = Qt.ItemDataRole.UserRole + 3

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[TaskRow] = []
        self._index: dict[str, int] = {}

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = 0) -> Any:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return row.item.name
        if role == self.StatusRole:
            return row.status
        if role == self.MessageRole:
            return row.message
        if role == self.ItemIdRole:
            return row.item.id
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{row.item.name}\n{STATUS_TAGS[row.status]} {row.message}".strip()
        return None

    def reset(self, items: list[Item]) -> None:
        self.beginResetModel()
        self._rows = [TaskRow(item) for item in items]
        self._index = {r.item.id: n for n, r in enumerate(self._rows)}
        self.endResetModel()

    def rows(self) -> list[TaskRow]:
        return list(self._rows)

    def row_for(self, item_id: str) -> TaskRow | None:
        n = self._index.get(item_id)
        return None if n is None else self._rows[n]

    def row_index(self, item_id: str) -> int:
        return self._index.get(item_id, -1)

    def set_status(
        self,
        item_id: str,
        status: TaskStatus,
        message: str | None = None,
        duration: float | None = None,
    ) -> None:
        n = self._index.get(item_id)
        if n is None:
            return
        row = self._rows[n]
        row.status = status
        if message is not None:
            row.message = message
        if duration is not None:
            row.duration = duration
        idx = self.index(n)
        self.dataChanged.emit(idx, idx)

    def count(self, *statuses: TaskStatus) -> int:
        return sum(1 for r in self._rows if r.status in statuses)


class TaskDelegate(QStyledItemDelegate):
    ROW_HEIGHT = 38
    TAG_W = 118
    NAME_W = 250

    def __init__(self, model: RunTaskModel, theme: ThemeManager, parent: QWidget | None = None):
        super().__init__(parent)
        self._model = model
        self._theme = theme

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        return QSize(400, self.ROW_HEIGHT)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        pal = self._theme.palette
        row = self._model.rows()[index.row()]
        r: QRect = option.rect  # type: ignore[attr-defined]
        painter.save()
        status = row.status
        if status == TaskStatus.RUNNING:
            painter.fillRect(r, QColor(pal.surface2))
            painter.fillRect(QRect(r.left(), r.top(), 3, r.height()), QColor(pal.accent_fg))
        painter.fillRect(QRect(r.left(), r.bottom(), r.width(), 1), QColor(pal.border))

        tag_color = {
            TaskStatus.PENDING: pal.faint,
            TaskStatus.RUNNING: pal.accent_fg,
            TaskStatus.OK: pal.text,
            TaskStatus.FAILED: pal.error_fg,
            TaskStatus.SKIPPED: pal.muted,
            TaskStatus.CANCELLED: pal.muted,
        }[status]
        dimmed = status in (TaskStatus.SKIPPED, TaskStatus.CANCELLED, TaskStatus.PENDING)
        x = r.left() + 16
        mid = Qt.AlignmentFlag.AlignVCenter
        tag_font = font(Role.LABEL_SM)
        painter.setFont(tag_font)
        painter.setPen(QColor(tag_color))
        painter.drawText(QRect(x, r.top(), self.TAG_W, r.height()), mid, STATUS_TAGS[status])
        x += self.TAG_W

        # duration (right)
        right = r.right() - 16
        dur_text = format_duration(row.duration) if row.duration else ""
        if dur_text:
            mono = font(Role.MONO)
            painter.setFont(mono)
            fm = QFontMetrics(mono)
            w = fm.horizontalAdvance(dur_text) + 2
            painter.setPen(QColor(pal.muted))
            painter.drawText(
                QRect(right - w, r.top(), w, r.height()),
                mid | Qt.AlignmentFlag.AlignRight,
                dur_text,
            )
            right -= w + 16

        name_font = font(Role.BODY_STRONG)
        painter.setFont(name_font)
        painter.setPen(QColor(pal.muted if dimmed else pal.text))
        name_w = min(self.NAME_W, max(60, right - x))
        name = QFontMetrics(name_font).elidedText(
            row.item.name, Qt.TextElideMode.ElideRight, name_w
        )
        painter.drawText(QRect(x, r.top(), name_w, r.height()), mid, name)
        x += name_w + 16

        if row.message and right - x > 40:
            small = font(Role.SMALL)
            painter.setFont(small)
            painter.setPen(QColor(pal.error_fg if status == TaskStatus.FAILED else pal.muted))
            msg = QFontMetrics(small).elidedText(
                row.message, Qt.TextElideMode.ElideRight, right - x
            )
            painter.drawText(QRect(x, r.top(), right - x, r.height()), mid, msg)
        painter.restore()


class RunPage(QWidget):
    """Public hooks: ``model``, ``stop_button``, ``retry_button``, ``back_button``,
    ``progress``, ``counter_label``, ``current_label``, ``summary_label``, ``log_view``,
    ``log_toggle``; signals ``stopRequested``, ``retryRequested``, ``backRequested``,
    ``browseRequested``.
    """

    stopRequested = Signal()
    retryRequested = Signal()
    backRequested = Signal()
    browseRequested = Signal()

    def __init__(self, log: LogStore, theme: ThemeManager, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page_run")
        self._theme = theme
        self.model = RunTaskModel(self)
        self._total = 0
        self._done = 0
        self._started_wall = 0.0
        self._elapsed = QElapsedTimer()
        self._final_duration: float | None = None
        self._tick = QTimer(self)
        self._tick.setInterval(500)
        self._tick.timeout.connect(self._update_clock)
        self._status_text = ""

        self.stack = QStackedWidget()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.stack)

        # -- idle (nothing run yet)
        idle = QWidget()
        idle.setObjectName("runIdle")
        idle_lay = page_layout(idle, top=48)
        idle_lay.addWidget(section_title("KOLEJKA PUSTA"))
        idle_lay.addWidget(label("Nic jeszcze nie uruchomiono.", Role.H1))
        idle_lay.addWidget(
            label(
                "Zaznacz pozycje na stronach Aplikacje i System, potem kliknij Zainstaluj. "
                "Tutaj zobaczysz postęp każdego zadania i log na żywo.",
                Role.BODY,
                tone="muted",
                wrap=True,
            )
        )
        idle_lay.addSpacing(8)
        idle_lay.addLayout(
            hbox(
                button(
                    "Przejdź do wyboru  →",
                    on_click=self.browseRequested.emit,
                    object_name="browseButton",
                ),
                None,
            )
        )
        idle_lay.addStretch(1)
        self.stack.addWidget(idle)

        # -- active / finished
        active = QWidget()
        active.setObjectName("runActive")
        lay = page_layout(active, top=28, bottom=20)
        lay.setSpacing(12)

        self.counter_label = section_title("KOLEJKA")
        self.counter_label.setObjectName("runCounter")
        self.clock_label = label("", Role.LABEL_SM, tone="muted", object_name="runClock")
        self.stop_button = button(
            "Stop", on_click=self.stopRequested.emit, object_name="stopButton", tooltip="Przerwij"
        )
        self.stop_button.setMinimumWidth(120)
        self.retry_button = button(
            "Ponów nieudane",
            variant="primary",
            on_click=self.retryRequested.emit,
            object_name="retryButton",
        )
        self.back_button = button(
            "Wróć do wyboru", on_click=self.backRequested.emit, object_name="backButton"
        )
        lay.addLayout(
            hbox(
                self.counter_label,
                12,
                self.clock_label,
                None,
                self.retry_button,
                self.back_button,
                self.stop_button,
            )
        )

        self.current_label = label("", Role.H1, object_name="currentTask")
        self.cursor = CursorBlock(theme, QSize(12, 26))
        title_row = hbox(self.current_label, 6, self.cursor, None, spacing=0)
        title_row.setAlignment(self.cursor, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(title_row)
        self.status_label = label("", Role.BODY, tone="muted", object_name="runStatus")
        lay.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setObjectName("runProgress")
        self.progress.setTextVisible(False)
        lay.addWidget(self.progress)

        self.summary_label = label("", Role.LABEL, object_name="summaryStats")
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        self.summary_label.setVisible(False)
        lay.addWidget(self.summary_label)
        lay.addSpacing(4)

        self.task_list = QListView()
        self.task_list.setObjectName("taskList")
        self.task_list.setModel(self.model)
        self.task_list.setItemDelegate(TaskDelegate(self.model, theme, self.task_list))
        self.task_list.setUniformItemSizes(True)
        self.task_list.setSelectionMode(QListView.SelectionMode.NoSelection)
        self.task_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.task_list.setFrameShape(QFrame.Shape.NoFrame)
        self.task_list.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        lay.addWidget(self.task_list, 3)

        self.log_toggle = button(
            "Zwiń log",
            variant="ghost",
            role=Role.LABEL_SM,
            on_click=self.toggle_log,
            object_name="logToggle",
        )
        self.log_header = hbox(section_title("LOG NA ŻYWO"), None, self.log_toggle)
        lay.addLayout(self.log_header)
        self.log_view = LogView(log, "runLog")
        lay.addWidget(self.log_view, 2)
        self.stack.addWidget(active)
        self.last_summary: RunSummary | None = None
        theme.changed.connect(self._on_theme)
        self._set_buttons(running=False, finished=False)

    def _on_theme(self, _palette: object) -> None:
        self.task_list.viewport().update()
        self._render_stats()

    # -- public state API (GUI thread)
    @property
    def has_run(self) -> bool:
        return self.stack.currentIndex() == 1

    def begin(self, items: list[Item]) -> None:
        self.stack.setCurrentIndex(1)
        self.model.reset(items)
        self._total = len(items)
        self._done = 0
        self._final_duration = None
        self._elapsed.start()
        self._started_wall = time.time()
        self._tick.start()
        self.progress.setRange(0, max(1, self._total))
        self.progress.setValue(0)
        self.summary_label.setVisible(False)
        self.status_label.setVisible(True)
        self.progress.setVisible(True)
        self.current_label.setText("Przygotowanie kolejki")
        self._status_text = "Uruchamianie…"
        self.cursor.setVisible(True)
        self.cursor.set_blinking(True)
        self._set_buttons(running=True, finished=False)
        self.stop_button.setEnabled(False)  # enabled after run_started (see set_running)
        self._update_counter()
        self._update_clock()

    def set_running(self) -> None:
        """``run_started`` arrived: cancellation is now possible."""
        self.stop_button.setEnabled(True)
        self.stop_button.setText("Stop")

    def set_stopping(self) -> None:
        self.stop_button.setEnabled(False)
        self.stop_button.setText("Zatrzymywanie…")
        self._status_text = "Zatrzymywanie — czekam na zakończenie bieżącego procesu…"
        self._update_clock()

    def task_started(self, item_id: str) -> None:
        self.model.set_status(item_id, TaskStatus.RUNNING)
        row = self.model.row_for(item_id)
        if row is not None:
            self.current_label.setText(row.item.name)
            if self.stop_button.isEnabled():
                self._status_text = f"W toku · {row.item.description or row.item.name}"
            self._update_clock()
            n = self.model.row_index(item_id)
            self.task_list.scrollTo(self.model.index(n), QListView.ScrollHint.EnsureVisible)

    def task_finished(self, result: TaskResult) -> None:
        self.model.set_status(result.item_id, result.status, result.message, result.duration)
        self._done += 1
        self.progress.setValue(self._done)
        self._update_counter()

    def finish(self, summary: RunSummary) -> None:
        self._tick.stop()
        self._final_duration = summary.duration
        self.cursor.set_blinking(False)
        self.cursor.setVisible(False)
        self.progress.setValue(self.progress.maximum())
        failed = summary.failed_count
        if summary.cancelled:
            title = "Zatrzymano"
        elif failed:
            title = "Zakończono z błędami"
        else:
            title = "Gotowe"
        self.current_label.setText(title)
        self.counter_label.setText(f"// PODSUMOWANIE — {self._done} / {self._total}")
        self.last_summary = summary
        self._render_stats()
        self.summary_label.setVisible(True)
        first_problem = next(
            (r.item_id for r in summary.results if r.status == TaskStatus.FAILED), None
        )
        if first_problem is not None:
            index = self.model.index(self.model.row_index(first_problem))
            self.task_list.scrollTo(index, QListView.ScrollHint.PositionAtTop)
        else:
            self.task_list.scrollToTop()
        self.status_label.setText(
            f"Czas {format_clock(summary.duration)} · start "
            f"{time.strftime('%H:%M:%S', time.localtime(self._started_wall))}"
        )
        self.clock_label.setText("")
        self.progress.setVisible(False)
        retry = len(summary.failed_ids)
        self.retry_button.setText(f"Ponów nieudane ({retry})")
        self._set_buttons(running=False, finished=True, can_retry=retry > 0)

    def _render_stats(self) -> None:
        summary = self.last_summary
        if summary is None:
            return
        pal = self._theme.palette
        failed = summary.failed_count

        def stat(tag: str, n: int, color: str) -> str:
            return f'<span style="color:{color}">{tag}</span>&nbsp;{n}'

        parts = [
            stat("[OK]", summary.ok_count, pal.text),
            stat("[BŁĄD]", failed, pal.error_fg if failed else pal.muted),
            stat("[POMINIĘTO]", summary.skipped_count, pal.muted),
            stat("[ANULOWANO]", summary.cancelled_count, pal.muted),
        ]
        sep = f'<span style="color:{pal.faint}">&nbsp;&nbsp;·&nbsp;&nbsp;</span>'
        self.summary_label.setText(sep.join(parts))

    def toggle_log(self) -> None:
        visible = not self.log_view.isVisible()
        self.log_view.setVisible(visible)
        self.log_toggle.setText("Zwiń log" if visible else "Rozwiń log")

    # -- internals
    def _set_buttons(self, *, running: bool, finished: bool, can_retry: bool = False) -> None:
        self.stop_button.setVisible(running)
        self.retry_button.setVisible(finished and can_retry)
        self.back_button.setVisible(finished)
        self.back_button.setProperty("variant", "secondary" if can_retry else "primary")
        self.back_button.style().unpolish(self.back_button)
        self.back_button.style().polish(self.back_button)

    def _update_counter(self) -> None:
        if self._final_duration is None:
            self.counter_label.setText(f"// KOLEJKA — {self._done} / {self._total}")

    def _update_clock(self) -> None:
        if self._final_duration is not None:
            return
        elapsed = self._elapsed.elapsed() / 1000 if self._elapsed.isValid() else 0.0
        self.clock_label.setText(format_clock(elapsed))
        self.status_label.setText(self._status_text)
