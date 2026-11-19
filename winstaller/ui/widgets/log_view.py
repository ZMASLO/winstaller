"""Read-only, line-limited view of a :class:`LogStore` (batched updates)."""

from __future__ import annotations

from PySide6.QtCore import Slot
from PySide6.QtGui import QTextCursor, QTextOption
from PySide6.QtWidgets import QPlainTextEdit, QWidget

from winstaller.ui.bridge import LogStore
from winstaller.ui.theme import Role, fonts


class LogView(QPlainTextEdit):
    """Mirrors a LogStore. Follows the tail unless the user scrolled up."""

    def __init__(
        self, store: LogStore, object_name: str = "logView", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._store = store
        self.setObjectName(object_name)
        self.setProperty("log", True)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setMaximumBlockCount(store.max_lines)
        self.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        fonts.apply(self, Role.MONO)
        self._empty = True
        lines = store.lines()
        if lines:
            self._append(False, lines)
        store.batch.connect(self._append)
        store.cleared.connect(self._clear)

    @property
    def store(self) -> LogStore:
        return self._store

    def line_count(self) -> int:
        return 0 if self._empty else self.document().blockCount()

    def last_line(self) -> str:
        if self._empty:
            return ""
        return self.document().lastBlock().text()

    @Slot(bool, list)
    def _append(self, replace_last: bool, lines: list[str]) -> None:
        if not lines:
            return
        bar = self.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 4
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        text = "\n".join(lines)
        if self._empty:
            cursor.insertText(text)
        elif replace_last:
            cursor.movePosition(
                QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor
            )
            cursor.insertText(text)
        else:
            cursor.insertText("\n" + text)
        cursor.endEditBlock()
        self._empty = False
        if follow:
            bar.setValue(bar.maximum())

    @Slot()
    def _clear(self) -> None:
        self.clear()
        self._empty = True
