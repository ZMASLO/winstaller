"""Single source of truth for which catalog items are selected for the install queue."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from PySide6.QtCore import QObject, Signal

from winstaller.core.catalog import Catalog


class SelectionModel(QObject):
    """Selected item ids. Only selectable (available, non-tool) items can be selected.

    ``changed`` fires once per mutating call that actually changed something;
    ``locked`` blocks changes while a run is in progress.
    """

    changed = Signal()
    lockedChanged = Signal(bool)

    def __init__(
        self,
        catalog: Catalog,
        selectable: Callable[[str], bool],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._catalog = catalog
        self._selectable = selectable
        self._ids: set[str] = set()
        self._locked = False

    # -- queries
    def is_selected(self, item_id: str) -> bool:
        return item_id in self._ids

    def is_selectable(self, item_id: str) -> bool:
        return self._selectable(item_id)

    def ids(self) -> list[str]:
        """Selected ids in catalog order."""
        return self._catalog.in_catalog_order(self._ids)

    def count(self, among: Iterable[str] | None = None) -> int:
        if among is None:
            return len(self._ids)
        return sum(1 for i in among if i in self._ids)

    def all_selected(self, item_ids: Iterable[str]) -> bool:
        """True when every selectable id of ``item_ids`` is selected (and there is one)."""
        usable = [i for i in item_ids if self._selectable(i)]
        return bool(usable) and all(i in self._ids for i in usable)

    @property
    def locked(self) -> bool:
        return self._locked

    def set_locked(self, locked: bool) -> None:
        if locked != self._locked:
            self._locked = locked
            self.lockedChanged.emit(locked)

    # -- mutations
    def set_selected(self, item_id: str, selected: bool) -> None:
        if selected:
            self.select(item_id)
        else:
            self.deselect(item_id)

    def toggle(self, item_id: str) -> None:
        self.set_selected(item_id, item_id not in self._ids)

    def select(self, *item_ids: str) -> None:
        self.select_many(item_ids)

    def deselect(self, *item_ids: str) -> None:
        self.deselect_many(item_ids)

    def select_many(self, item_ids: Iterable[str]) -> None:
        if self._locked:
            return
        new = {i for i in item_ids if self._selectable(i)} - self._ids
        if new:
            self._ids |= new
            self.changed.emit()

    def deselect_many(self, item_ids: Iterable[str]) -> None:
        if self._locked:
            return
        gone = self._ids & set(item_ids)
        if gone:
            self._ids -= gone
            self.changed.emit()

    def toggle_group(self, item_ids: Iterable[str]) -> bool:
        """Select the whole group, or deselect it when it is already fully selected.

        Returns the new state of the group (True = selected).
        """
        ids = list(item_ids)
        if self.all_selected(ids):
            self.deselect_many(ids)
            return False
        self.select_many(ids)
        return True

    def clear(self) -> None:
        if self._locked or not self._ids:
            return
        self._ids.clear()
        self.changed.emit()
