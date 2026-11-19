"""Dziennik: the full session log with copy / save / clear."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFileDialog, QWidget

from winstaller.ui.bridge import LogStore
from winstaller.ui.theme import Role
from winstaller.ui.widgets.common import PageHeader, button, hbox, label, page_layout, plural
from winstaller.ui.widgets.log_view import LogView

SaveTargetFn = Callable[[QWidget], Path | None]


def ask_save_path(parent: QWidget) -> Path | None:
    path, _ = QFileDialog.getSaveFileName(
        parent, "Zapisz dziennik", "winstaller-log.txt", "Pliki tekstowe (*.txt);;Wszystkie (*)"
    )
    return Path(path) if path else None


class LogPage(QWidget):
    """Public hooks: ``view`` (LogView), ``copy``, ``save_to``, ``clear``; ``status`` label."""

    saved = Signal(object)  # Path

    def __init__(
        self,
        store: LogStore,
        *,
        save_target: SaveTargetFn = ask_save_path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("page_log")
        self._store = store
        self._save_target = save_target
        lay = page_layout(self)
        header = PageHeader("Dziennik", "Pełny zapis tej sesji: wyjście instalatorów i komunikaty.")
        self.copy_button = button("Kopiuj", on_click=self.copy, object_name="logCopy")
        self.save_button = button("Zapisz do pliku…", on_click=self.save, object_name="logSave")
        self.clear_button = button(
            "Wyczyść", variant="ghost", on_click=self.clear, object_name="logClear"
        )
        header.right.addWidget(self.clear_button)
        header.right.addWidget(self.copy_button)
        header.right.addWidget(self.save_button)
        lay.addWidget(header)
        self.count_label = label("", Role.LABEL_SM, tone="muted", object_name="logCount")
        self.status = label("", Role.LABEL_SM, tone="accent", object_name="logStatus")
        lay.addLayout(hbox(self.count_label, None, self.status))
        self.view = LogView(store, "journalLog")
        lay.addWidget(self.view, 1)
        store.batch.connect(lambda *_a: self._update_count())
        store.cleared.connect(self._update_count)
        self._update_count()

    def _update_count(self) -> None:
        n = len(self._store)
        word = plural(n, "linia", "linie", "linii")
        self.count_label.setText(f"{n} {word} · limit {self._store.max_lines}")
        has = n > 0
        self.copy_button.setEnabled(has)
        self.save_button.setEnabled(has)
        self.clear_button.setEnabled(has)

    def copy(self) -> None:
        self._store.flush()
        QGuiApplication.clipboard().setText(self._store.text())
        self.status.setText("Skopiowano do schowka")

    def save(self) -> None:
        path = self._save_target(self)
        if path is not None:
            self.save_to(path)

    def save_to(self, path: Path) -> bool:
        self._store.flush()
        try:
            path.write_text(self._store.text() + "\n", encoding="utf-8")
        except OSError as exc:
            self.status.setText(f"Nie udało się zapisać: {exc}")
            return False
        self.status.setText(f"Zapisano → {path}")
        self.saved.emit(path)
        return True

    def clear(self) -> None:
        self._store.clear()
        self.status.setText("")
