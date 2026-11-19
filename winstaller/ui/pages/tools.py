"""Narzędzia: immediate one-off actions (reboot to BIOS, battery report, ...)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from winstaller.core.catalog import Item
from winstaller.core.runner import TaskResult, TaskStatus
from winstaller.ui.theme import Role
from winstaller.ui.widgets.common import (
    PageHeader,
    button,
    hbox,
    label,
    page_layout,
    section_title,
    set_prop,
)
from winstaller.ui.widgets.item_tile import kind_tag

ACTION_LABELS: dict[str, str] = {
    "reboot_to_bios": "Uruchom ponownie",
    "reboot_advanced": "Uruchom ponownie",
    "battery_report": "Generuj raport",
}


class ToolCard(QFrame):
    """One tool: title, description, action button and an inline result line."""

    runRequested = Signal(str)
    openRequested = Signal(object)  # Path

    def __init__(self, item: Item, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.item = item
        self.setObjectName("toolCard")
        self.setProperty("toolId", item.id)
        self.title = label(item.name, Role.H2)
        tags = kind_tag(item) + ("  [POTWIERDZENIE]" if item.confirm else "")
        self.tags = label(tags, Role.LABEL_SM, tone="muted")
        self.description = label(item.description, Role.BODY, tone="muted", wrap=True)
        self.action_text = ACTION_LABELS.get(item.action or "", "Uruchom")
        self.run_button = button(
            f"{self.action_text}  →",
            on_click=lambda: self.runRequested.emit(item.id),
            object_name=f"toolRun_{item.id}",
        )
        self.run_button.setMinimumWidth(190)

        self.result_frame = QFrame()
        self.result_frame.setObjectName("toolResult")
        self.result_tag = label("", Role.LABEL, object_name=f"toolResultTag_{item.id}")
        self.result_text = label("", Role.SMALL, tone="muted", wrap=True, selectable=True)
        self.result_text.setObjectName(f"toolResultText_{item.id}")
        self.open_button = button("Otwórz  →", variant="link", object_name=f"toolOpen_{item.id}")
        self.open_button.setVisible(False)
        self._artifact: Path | None = None
        self.open_button.clicked.connect(lambda: self.openRequested.emit(self._artifact))
        res = QHBoxLayout(self.result_frame)
        res.setContentsMargins(0, 12, 0, 0)
        res.setSpacing(12)
        res.addWidget(self.result_tag)
        res.addWidget(self.result_text, 1)
        res.addWidget(self.open_button)
        self.result_frame.setVisible(False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(6)
        left = QVBoxLayout()
        left.setSpacing(6)
        left.addLayout(hbox(self.title, 12, self.tags, None))
        left.addWidget(self.description)
        lay.addLayout(hbox(left, 24, self.run_button, spacing=0))
        lay.addWidget(self.result_frame)

    def set_busy(self, busy: bool) -> None:
        self.run_button.setEnabled(not busy)
        self.run_button.setText("Wykonywanie…" if busy else f"{self.action_text}  →")

    def show_result(self, result: TaskResult) -> None:
        tags = {
            TaskStatus.OK: ("[OK]", "accent"),
            TaskStatus.FAILED: ("[BŁĄD]", "error"),
            TaskStatus.CANCELLED: ("[ANULOWANO]", "muted"),
            TaskStatus.SKIPPED: ("[POMINIĘTO]", "muted"),
        }
        tag, tone = tags.get(result.status, (f"[{result.status.value.upper()}]", "muted"))
        self.result_tag.setText(tag)
        set_prop(self.result_tag, "role", tone)
        text = result.message
        self._artifact = result.artifact
        if result.artifact is not None:
            text = f"{text}\n{result.artifact}" if text else str(result.artifact)
        self.result_text.setText(text)
        self.open_button.setVisible(result.artifact is not None)
        self.result_frame.setVisible(True)


class ToolsPage(QWidget):
    """Public hooks: ``cards`` (id -> ToolCard); signals ``runRequested(id)``, ``openRequested``."""

    runRequested = Signal(str)
    openRequested = Signal(object)

    def __init__(self, tools: list[Item], *, dry_run: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("page_tools")
        lay = page_layout(self)
        lay.addWidget(
            PageHeader(
                "Narzędzia",
                "Akcje wykonywane od razu, poza kolejką instalacji.",
            )
        )
        if dry_run:
            lay.addWidget(
                label(
                    "[DRY-RUN]  akcje są tylko symulowane",
                    Role.LABEL_SM,
                    tone="accent",
                    object_name="toolsDryRunNote",
                )
            )
        self.lock_note = label(
            "Narzędzia są niedostępne, dopóki trwa instalacja.",
            Role.LABEL_SM,
            tone="muted",
            object_name="toolsLockNote",
        )
        self.lock_note.setVisible(False)
        lay.addWidget(self.lock_note)
        lay.addSpacing(4)
        lay.addWidget(section_title(f"AKCJE — {len(tools)}"))
        self.cards: dict[str, ToolCard] = {}
        for item in tools:
            card = ToolCard(item)
            card.runRequested.connect(self.runRequested)
            card.openRequested.connect(self.openRequested)
            self.cards[item.id] = card
            lay.addWidget(card)
        lay.addStretch(1)
        self._busy: set[str] = set()
        self._locked = False

    def set_busy(self, item_id: str, busy: bool) -> None:
        if busy:
            self._busy.add(item_id)
        else:
            self._busy.discard(item_id)
        self._sync()

    def set_locked(self, locked: bool) -> None:
        self._locked = locked
        self.lock_note.setVisible(locked)
        self._sync()

    def _sync(self) -> None:
        for item_id, card in self.cards.items():
            card.set_busy(item_id in self._busy)
            if self._locked and item_id not in self._busy:
                card.run_button.setEnabled(False)

    def show_result(self, item_id: str, result: TaskResult) -> None:
        card = self.cards.get(item_id)
        if card is not None:
            card.show_result(result)

    def buttons(self) -> list[QPushButton]:
        return [c.run_button for c in self.cards.values()]
