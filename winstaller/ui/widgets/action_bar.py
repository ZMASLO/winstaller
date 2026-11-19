"""Bottom bar on the selection pages: counter, clear, primary "Zainstaluj (N)"."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QWidget

from winstaller.ui.theme import Role
from winstaller.ui.widgets.common import button, label


class ActionBar(QWidget):
    installRequested = Signal()
    clearRequested = Signal()
    showRunRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("actionBar")
        self.setFixedHeight(68)
        self.count_label = label("", Role.LABEL, object_name="selectedCount")
        self.hint_label = label("", Role.LABEL_SM, tone="muted", object_name="actionHint")
        self.clear_button = button(
            "Odznacz wszystkie",
            variant="ghost",
            on_click=self.clearRequested.emit,
            object_name="clearSelection",
        )
        self.run_link = button(
            "Trwa instalacja → Przebieg",
            variant="link",
            on_click=self.showRunRequested.emit,
            object_name="showRunLink",
        )
        self.run_link.setVisible(False)
        self.install_button = button(
            "Zainstaluj",
            variant="primary",
            on_click=self.installRequested.emit,
            object_name="installButton",
            tooltip="Uruchom kolejkę (Ctrl+Enter)",
        )
        self.install_button.setMinimumWidth(190)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(40, 0, 24, 0)
        lay.setSpacing(16)
        lay.addWidget(self.count_label)
        lay.addWidget(self.hint_label)
        lay.addStretch(1)
        lay.addWidget(self.run_link)
        lay.addWidget(self.clear_button)
        lay.addWidget(self.install_button)
        self.update_state(0, busy=False)

    def update_state(self, count: int, *, busy: bool, on_other_pages: int = 0) -> None:
        self.count_label.setText(f"Zaznaczono {count}")
        if busy:
            self.hint_label.setText("Wybór zablokowany na czas instalacji")
        elif count and on_other_pages:
            self.hint_label.setText(f"w tym {on_other_pages} na innej stronie")
        elif not count:
            self.hint_label.setText("Kliknij pozycję lub wybierz profil")
        else:
            self.hint_label.setText("Ctrl+Enter")
        self.install_button.setText(f"Zainstaluj ({count})  →" if count else "Zainstaluj")
        self.install_button.setEnabled(count > 0 and not busy)
        self.clear_button.setEnabled(count > 0 and not busy)
        self.run_link.setVisible(busy)
