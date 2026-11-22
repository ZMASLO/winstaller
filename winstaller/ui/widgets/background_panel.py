"""The "W tle" block of the Przebieg page: the detached Windows Update installation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
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
    QSizePolicy,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from winstaller.core.updates import InstallItem, InstallProgress, describe_progress
from winstaller.ui.services import ClockFn
from winstaller.ui.theme import Role, ThemeManager, font, fonts
from winstaller.ui.widgets.common import (
    PAGE_MARGIN_H,
    CursorBlock,
    button,
    format_clock,
    hbox,
    hsep,
    label,
    section_title,
    set_prop,
)

#: item status -> (tag, tone); tone: accent (in progress), text, muted, faint, error.
ITEM_TAGS: dict[str, tuple[str, str]] = {
    "pending": ("[OCZEKUJE]", "faint"),
    "downloading": ("[POBIERANIE]", "accent"),
    "downloaded": ("[POBRANO]", "muted"),
    "installing": ("[INSTALACJA]", "accent"),
    "succeeded": ("[OK]", "text"),
    "succeeded_with_errors": ("[OK]", "text"),
    "failed": ("[BŁĄD]", "error"),
    "aborted": ("[BŁĄD]", "error"),
    "error": ("[BŁĄD]", "error"),
    "unknown": ("[BŁĄD]", "error"),
    "not_downloaded": ("[BŁĄD]", "error"),
    "skipped": ("[POMINIĘTO]", "muted"),
}
#: A live installer whose current step runs longer than this gets a warning (nothing
#: is changed or stopped: the cumulative update alone takes ~5 min on real hardware).
STALL_SECONDS = 90 * 60


def item_tag(item: InstallItem, finished: bool) -> tuple[str, str]:
    """Tag and tone of one update; after the end, an update that never got its turn
    is "skipped" and one caught mid-way "interrupted"."""
    if finished and not item.done:
        if item.status in ("downloading", "installing"):
            return "[PRZERWANO]", "error"
        return "[POMINIĘTO]", "muted"
    return ITEM_TAGS.get(item.status, ("[?]", "muted"))


def item_detail(item: InstallItem) -> str:
    """Right-hand note: error code, "nie pobrano", restart."""
    if item.status == "not_downloaded":
        return f"nie pobrano · {item.hresult}" if item.hresult else "nie pobrano"
    if item.failed:
        return item.hresult or ""
    if item.status == "succeeded_with_errors":
        return "z błędami" + (" · restart" if item.reboot_required else "")
    if item.ok and item.reboot_required:
        return "wymaga restartu"
    return ""


class BackgroundItemModel(QAbstractListModel):
    TagRole = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: tuple[InstallItem, ...] = ()
        self._finished = False

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._items)

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = 0) -> Any:
        if not index.isValid():
            return None
        item = self._items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return item.title
        if role == self.TagRole:
            return item_tag(item, self._finished)[0]
        if role == Qt.ItemDataRole.ToolTipRole:
            tag = item_tag(item, self._finished)[0]
            return f"{item.title}\n{tag} {item_detail(item)}".strip()
        return None

    def set_items(self, items: tuple[InstallItem, ...], finished: bool) -> None:
        if (items, finished) == (self._items, self._finished):
            return
        self.beginResetModel()
        self._items, self._finished = items, finished
        self.endResetModel()

    def items(self) -> tuple[InstallItem, ...]:
        return self._items

    @property
    def finished(self) -> bool:
        return self._finished


class BackgroundItemDelegate(QStyledItemDelegate):
    ROW_HEIGHT = 30
    TAG_W = 118

    def __init__(self, model: BackgroundItemModel, theme: ThemeManager, parent: QWidget | None):
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
        item = self._model.items()[index.row()]
        tag, tone = item_tag(item, self._model.finished)
        r: QRect = option.rect  # type: ignore[attr-defined]
        painter.save()
        if tone == "accent":
            painter.fillRect(r, QColor(pal.surface2))
        painter.fillRect(QRect(r.left(), r.bottom(), r.width(), 1), QColor(pal.border))
        colors = {
            "accent": pal.accent_fg,
            "text": pal.text,
            "muted": pal.muted,
            "faint": pal.faint,
            "error": pal.error_fg,
        }
        mid = Qt.AlignmentFlag.AlignVCenter
        x = r.left() + 12
        painter.setFont(font(Role.LABEL_SM))
        painter.setPen(QColor(colors[tone]))
        painter.drawText(QRect(x, r.top(), self.TAG_W, r.height()), mid, tag)
        x += self.TAG_W
        right = r.right() - 12
        detail = item_detail(item)
        if detail:
            mono = font(Role.MONO)
            painter.setFont(mono)
            w = QFontMetrics(mono).horizontalAdvance(detail) + 2
            painter.setPen(QColor(pal.error_fg if item.failed else pal.muted))
            painter.drawText(
                QRect(right - w, r.top(), w, r.height()), mid | Qt.AlignmentFlag.AlignRight, detail
            )
            right -= w + 16
        body = font(Role.BODY)
        painter.setFont(body)
        dimmed = tone in ("faint", "muted")
        painter.setPen(QColor(pal.muted if dimmed else pal.text))
        width = max(40, right - x)
        title = QFontMetrics(body).elidedText(item.title, Qt.TextElideMode.ElideRight, width)
        painter.drawText(QRect(x, r.top(), width, r.height()), mid, title)
        painter.restore()


class BackgroundUpdatePanel(QWidget):
    """``// W TLE — WINDOWS UPDATE``: state in large type, current update, step
    progress (finished steps / all; while one long step runs only a blinking cursor and
    the step's own clock — no invented percentage), the list of updates, the result
    with counts and the restart notice, "Otwórz log". Hidden until :meth:`set_progress`.

    Hooks (objectName): ``wuPanel``, ``wuPanelHeader``, ``wuPanelTitle``,
    ``wuPanelSubtitle``, ``wuPanelClock``, ``wuPanelProgress``, ``wuPanelStats``,
    ``wuPanelRestart``, ``wuPanelList``, ``wuPanelListToggle``, ``wuPanelLog``. Signal
    ``openLogRequested``. The list is folded in compact mode (queue view shown) except
    for failed updates; ``toggle_list`` / the "Lista (N)" button opens it.
    """

    openLogRequested = Signal()
    MAX_ROWS = 8
    MAX_ROWS_COMPACT = 4

    def __init__(
        self,
        theme: ThemeManager,
        clock: ClockFn,
        parent: QWidget | None = None,
        *,
        path_exists: Callable[[Path], bool] = Path.exists,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("wuPanel")
        self._theme = theme
        self._clock = clock
        self._path_exists = path_exists
        self._subtitle = ""
        self._compact = False
        self._secondary = False
        self._list_open: bool | None = None  # None: open unless compact
        self.progress: InstallProgress | None = None
        # Always its natural height; the queue view below takes what is left.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        body = QWidget()
        body.setObjectName("wuPanelBody")
        inner = QVBoxLayout(body)
        inner.setContentsMargins(PAGE_MARGIN_H, 24, PAGE_MARGIN_H, 18)
        inner.setSpacing(10)
        lay.addWidget(body)
        lay.addWidget(hsep())

        self.header = section_title("W TLE — WINDOWS UPDATE")
        self.header.setObjectName("wuPanelHeader")
        self.clock_label = label("", Role.LABEL_SM, tone="muted", object_name="wuPanelClock")
        self.log_button = button(
            "Otwórz log",
            variant="ghost",
            role=Role.LABEL_SM,
            on_click=self.openLogRequested.emit,
            object_name="wuPanelLog",
        )
        self.list_button = button(
            "Lista",
            variant="ghost",
            role=Role.LABEL_SM,
            on_click=self.toggle_list,
            object_name="wuPanelListToggle",
        )
        inner.addLayout(
            hbox(self.header, 12, self.clock_label, None, self.list_button, self.log_button)
        )

        self.title_label = label("", Role.H1, object_name="wuPanelTitle")
        self.cursor = CursorBlock(theme, QSize(10, 22))
        title_row = hbox(self.title_label, 6, self.cursor, None, spacing=0)
        title_row.setAlignment(self.cursor, Qt.AlignmentFlag.AlignVCenter)
        inner.addLayout(title_row)
        self.subtitle_label = label(
            "", Role.BODY, tone="muted", wrap=True, object_name="wuPanelSubtitle"
        )
        inner.addWidget(self.subtitle_label)

        # Not yellow (nothing is in progress) and not red (not an error): outlined text.
        self.restart_label = QFrame()
        self.restart_label.setObjectName("wuPanelRestart")
        notice = hbox(
            label("[WYMAGANY RESTART]", Role.LABEL, object_name="wuPanelRestartTag"),
            12,
            label(
                "Uruchom ponownie komputer, aby dokończyć instalację. Winstaller nie robi "
                "tego sam.",
                Role.BODY,
                object_name="wuPanelRestartText",
            ),
            None,
        )
        notice.setContentsMargins(14, 9, 14, 9)
        self.restart_label.setLayout(notice)
        inner.addWidget(self.restart_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("wuPanelProgress")
        self.progress_bar.setTextVisible(False)
        inner.addWidget(self.progress_bar)

        self.stats_label = label("", Role.LABEL, object_name="wuPanelStats")
        self.stats_label.setTextFormat(Qt.TextFormat.RichText)
        inner.addWidget(self.stats_label)

        self.model = BackgroundItemModel(self)
        self.item_list = QListView()
        self.item_list.setObjectName("wuPanelList")
        self.item_list.setModel(self.model)
        self.item_list.setItemDelegate(BackgroundItemDelegate(self.model, theme, self.item_list))
        self.item_list.setUniformItemSizes(True)
        self.item_list.setSelectionMode(QListView.SelectionMode.NoSelection)
        self.item_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.item_list.setFrameShape(QFrame.Shape.NoFrame)
        self.item_list.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.item_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner.addWidget(self.item_list)

        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._update_clock)
        theme.changed.connect(self._on_theme)
        self.setVisible(False)

    # -- public API (GUI thread)
    def set_compact(self, compact: bool) -> None:
        """Compact while the queue view is shown: smaller title and the list folded
        (only failed updates stay listed) unless the user opened it."""
        if compact != self._compact:
            self._compact = compact
            fonts.apply(self.title_label, Role.H2 if compact else Role.H1)
            self.cursor.setFixedSize(QSize(8, 16) if compact else QSize(10, 22))
            self._refresh_list()

    @property
    def compact(self) -> bool:
        return self._compact

    @property
    def list_open(self) -> bool:
        return self._list_open if self._list_open is not None else not self._compact

    def toggle_list(self) -> None:
        self._list_open = not self.list_open
        self._refresh_list()

    def _refresh_list(self) -> None:
        p = self.progress
        items = p.items if p is not None else ()
        # "unknown": the last known statuses stay as they were (no "interrupted"/"skipped"
        # verdict that nobody confirmed).
        done = p is not None and p.done and p.state != "unknown"
        failed = tuple(i for i in items if item_tag(i, True)[1] == "error") if done else ()
        shown = items if self.list_open else failed
        self.model.set_items(shown, done)
        self.item_list.setVisible(bool(shown))
        self.list_button.setVisible(bool(items))
        self.list_button.setText("Zwiń listę" if self.list_open else f"Lista ({len(items)})")
        self._fit_list()
        self.updateGeometry()

    def set_secondary(self, secondary: bool) -> None:
        """While the queue runs, the queue owns the accent (cursor, bar): this block's
        cursor stays still and its bar is grey, so only one thing blinks."""
        if secondary != self._secondary:
            self._secondary = secondary
            self._apply_activity()

    @property
    def secondary(self) -> bool:
        return self._secondary

    def _apply_activity(self) -> None:
        running = self.progress is not None and not self.progress.done
        self.cursor.setVisible(running and not self._secondary)
        self.cursor.set_blinking(running and not self._secondary)
        set_prop(self.progress_bar, "tone", "muted" if self._secondary else "")

    def set_progress(self, progress: InstallProgress | None) -> None:
        self.progress = progress
        if progress is None:
            self._tick.stop()
            self._apply_activity()
            self.setVisible(False)
            return
        title, subtitle = describe_progress(progress, mention_restart=False)
        self.title_label.setText(title)
        self._subtitle = subtitle
        done = progress.done
        self.restart_label.setVisible(done and progress.needs_restart)
        self.progress_bar.setVisible(not done)
        self.progress_bar.setRange(0, max(1, progress.steps_total))
        self.progress_bar.setValue(progress.steps_done)
        self.stats_label.setVisible(done and progress.count > 0 and progress.state != "unknown")
        self._render_stats()
        self._refresh_list()
        self._apply_activity()
        self.log_button.setEnabled(self._log_exists(progress))
        if done:
            self._tick.stop()
        elif not self._tick.isActive():
            self._tick.start()
        self._update_clock()
        self.setVisible(True)
        # Changes made while hidden do not reach the parent layout's cached size hint.
        self.updateGeometry()

    def elapsed_step(self) -> float | None:
        """Seconds the current step has been running (None when not running)."""
        p = self.progress
        if p is None or p.done:
            return None
        since = p.step_started if p.phase in ("downloading", "installing") else None
        since = since or p.phase_started or p.started
        if since is None:
            return None
        try:
            return max(0.0, (self._clock() - since).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None

    # -- internals
    def _fit_list(self) -> None:
        rows = min(self.model.rowCount(), self.MAX_ROWS_COMPACT if self._compact else self.MAX_ROWS)
        self.item_list.setFixedHeight(rows * BackgroundItemDelegate.ROW_HEIGHT + 1)

    def _on_theme(self, _palette: object) -> None:
        self.item_list.viewport().update()
        self._render_stats()

    def _render_stats(self) -> None:
        p = self.progress
        if p is None or not p.done:
            self.stats_label.setText("")
            return
        pal = self._theme.palette
        skipped = p.skipped_count

        def stat(tag: str, n: int, color: str) -> str:
            return f'<span style="color:{color}">{tag}</span>&nbsp;{n}'

        parts = [
            stat("[OK]", p.ok_count, pal.text),
            stat("[BŁĄD]", p.failed_count, pal.error_fg if p.failed_count else pal.muted),
        ]
        if skipped:
            parts.append(stat("[POMINIĘTO]", skipped, pal.muted))
        sep = f'<span style="color:{pal.faint}">&nbsp;&nbsp;·&nbsp;&nbsp;</span>'
        self.stats_label.setText(sep.join(parts))

    def _log_exists(self, progress: InstallProgress) -> bool:
        if progress.log_path is None:
            return False
        try:
            return bool(self._path_exists(progress.log_path))
        except OSError:
            return False

    def _when(self, value: datetime) -> str:
        """``15:55`` today, ``2026.11.21 15:55`` for another day."""
        try:
            today = self._clock().date()
        except (TypeError, ValueError, OverflowError):
            today = value.date()
        return f"{value:%H:%M}" if value.date() == today else f"{value:%Y.%m.%d %H:%M}"

    def stalled(self) -> bool:
        """The installer is alive but its current step has run for over
        :data:`STALL_SECONDS` (only a warning: nothing is changed)."""
        elapsed = self.elapsed_step()
        return elapsed is not None and elapsed > STALL_SECONDS

    def _update_clock(self) -> None:
        p = self.progress
        if p is None:
            self.clock_label.setText("")
            return
        parts = []
        if p.started is not None:
            parts.append(f"START {self._when(p.started)}")
        if p.done:
            if p.finished is not None:
                parts.append(f"KONIEC {self._when(p.finished)}")
            if p.from_log:
                parts.append("WG LOGU")
        else:
            elapsed = self.elapsed_step()
            if elapsed is not None:
                parts.append(f"KROK {format_clock(elapsed)}")
        self.clock_label.setText("  ·  ".join(parts))
        subtitle = self._subtitle
        if self.stalled():
            elapsed = self.elapsed_step() or 0.0
            warning = f"Brak postępu od {format_clock(elapsed)} — sprawdź log."
            subtitle = f"{subtitle} · {warning}" if subtitle else warning
        if self.subtitle_label.text() != subtitle:
            self.subtitle_label.setText(subtitle)
        self.subtitle_label.setVisible(bool(subtitle))
