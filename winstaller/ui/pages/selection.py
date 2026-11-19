"""Selection pages (Aplikacje, System): searchable category sections of toggle tiles."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from winstaller.core.catalog import Availability, Catalog, Category, Profile
from winstaller.core.platform import Arch
from winstaller.ui.selection import SelectionModel
from winstaller.ui.theme import Role, ThemeManager, fonts
from winstaller.ui.widgets.common import (
    PAGE_MARGIN_H,
    PAGE_MARGIN_TOP,
    PageHeader,
    button,
    hbox,
    label,
    section_title,
    vbox,
)
from winstaller.ui.widgets.item_tile import ItemTile

GRID_SPACING = 8


def _columns_for(width: int) -> int:
    if width < 640:
        return 1
    if width < 1120:
        return 2
    return 3


def plural_hits(n: int) -> str:
    if n == 1:
        return "1 trafienie"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} trafienia"
    return f"{n} trafień"


class CategorySection(QWidget):
    """``// NAME   2 / 7`` header, description, toggle-all button and a grid of tiles."""

    toggleAllRequested = Signal(str)

    def __init__(
        self, category: Category, tiles: Sequence[ItemTile], parent: QWidget | None = None
    ):
        super().__init__(parent)
        self.category = category
        self.tiles = list(tiles)
        self.setObjectName(f"section_{category.id}")
        self.title = section_title(category.name.upper())
        self.counter = label(
            "", Role.LABEL_SM, tone="muted", object_name=f"sectionCount_{category.id}"
        )
        self.toggle_button = button(
            "Zaznacz wszystkie",
            variant="ghost",
            role=Role.LABEL_SM,
            on_click=lambda: self.toggleAllRequested.emit(category.id),
            object_name=f"sectionToggle_{category.id}",
        )
        self.description = label(category.description, Role.SMALL, tone="muted")
        self.description.setVisible(bool(category.description))

        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(GRID_SPACING)
        self._grid.setVerticalSpacing(GRID_SPACING)
        self._columns = 0
        for tile in self.tiles:
            tile.setParent(self._grid_host)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addLayout(hbox(self.title, 10, self.counter, None, self.toggle_button))
        lay.addWidget(self.description)
        lay.addSpacing(6)
        lay.addWidget(self._grid_host)
        self.relayout(2)

    def visible_tiles(self) -> list[ItemTile]:
        return [t for t in self.tiles if not t.isHidden()]

    def relayout(self, columns: int, *, force: bool = False) -> None:
        if columns == self._columns and not force:
            return
        self._columns = columns
        while self._grid.count():
            self._grid.takeAt(0)
        for n, tile in enumerate(self.visible_tiles()):
            self._grid.addWidget(tile, n // columns, n % columns)
        for c in range(4):
            self._grid.setColumnStretch(c, 1 if c < columns else 0)

    def apply_filter(self, needle: str) -> int:
        shown = 0
        for tile in self.tiles:
            match = tile.matches(needle)
            tile.setHidden(not match)
            shown += match
        self.setHidden(shown == 0)
        self.relayout(self._columns or 2, force=True)
        return shown

    def refresh(self, selection: SelectionModel) -> None:
        ids = [t.item.id for t in self.tiles]
        usable = [i for i in ids if selection.is_selectable(i)]
        selected = selection.count(ids)
        self.counter.setText(f"{selected} / {len(usable)}")
        self.toggle_button.setText(
            "Odznacz wszystkie" if selection.all_selected(ids) else "Zaznacz wszystkie"
        )
        self.toggle_button.setEnabled(bool(usable) and not selection.locked)


class SelectionPage(QWidget):
    """A page of install categories bound to the shared :class:`SelectionModel`.

    Public hooks: ``tiles`` (id -> ItemTile), ``sections`` (category id -> section),
    ``search`` (QLineEdit), ``profile_buttons`` (profile id -> chip), ``set_filter``,
    ``focus_search``, ``visible_ids``; signal ``filterChanged(text, matches)``.
    """

    filterChanged = Signal(str, int)
    jumpRequested = Signal(str)  # needle to search on the other page

    def __init__(
        self,
        *,
        page_id: str,
        title: str,
        subtitle: str,
        categories: Sequence[Category],
        catalog: Catalog,
        availability: Mapping[str, Availability],
        selection: SelectionModel,
        theme: ThemeManager,
        profiles: Sequence[Profile] = (),
        arch: Arch = "x64",
        search_placeholder: str = "Szukaj…",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(f"page_{page_id}")
        self.page_id = page_id
        self._selection = selection
        self._arch = arch
        self._cross_counter: Callable[[str], int] | None = None
        self._cross_label = ""
        self.tiles: dict[str, ItemTile] = {}
        self.sections: dict[str, CategorySection] = {}
        self.profile_buttons: dict[str, QPushButton] = {}
        self._profiles = {p.id: p for p in profiles}

        # -- header
        self.header = PageHeader(title, subtitle)
        self.search = QLineEdit()
        self.search.setObjectName(f"search_{page_id}")
        self.search.setPlaceholderText(search_placeholder)
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(440)
        self.search.setMinimumWidth(240)
        fonts.apply(self.search, Role.BODY)
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(60)
        self._filter_timer.timeout.connect(lambda: self.set_filter(self.search.text()))
        self.search.textChanged.connect(lambda _t: self._filter_timer.start())
        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.search)
        esc.setContext(Qt.ShortcutContext.WidgetShortcut)
        esc.activated.connect(self.search.clear)

        tools_row = hbox(self.search, spacing=10)
        tools_row.addStretch(1)
        if profiles:
            tools_row.addWidget(label("Profil", Role.LABEL_SM, tone="muted"))
            for prof in profiles:
                chip = self._make_chip(prof)
                self.profile_buttons[prof.id] = chip
                tools_row.addWidget(chip)

        head = QWidget()
        head_lay = QVBoxLayout(head)
        head_lay.setContentsMargins(PAGE_MARGIN_H, PAGE_MARGIN_TOP, PAGE_MARGIN_H, 12)
        head_lay.setSpacing(18)
        head_lay.addWidget(self.header)
        head_lay.addLayout(tools_row)

        # -- body
        body = QWidget()
        body.setObjectName("scrollBody")
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(PAGE_MARGIN_H, 12, PAGE_MARGIN_H - 10, 28)
        body_lay.setSpacing(30)
        for cat in categories:
            tiles = []
            for item in catalog.items(cat.id):
                tile = ItemTile(item, availability[item.id], theme)
                tile.toggleRequested.connect(self._selection.toggle)
                tile.navigate.connect(self._navigate)
                self.tiles[item.id] = tile
                tiles.append(tile)
            section = CategorySection(cat, tiles)
            section.toggleAllRequested.connect(self._toggle_category)
            self.sections[cat.id] = section
            body_lay.addWidget(section)

        self.empty_state = QWidget()
        self.empty_state.setObjectName(f"emptyState_{page_id}")
        self.empty_title = label("", Role.H2, object_name=f"emptyTitle_{page_id}")
        self.empty_hint = label(
            "Sprawdź pisownię albo wyczyść pole wyszukiwania (Esc).", Role.BODY, tone="muted"
        )
        self.cross_button = button(
            "", variant="link", role=Role.LABEL, on_click=self._jump, object_name=f"jump_{page_id}"
        )
        self.cross_button.setVisible(False)
        empty_lay = vbox(
            section_title("BRAK WYNIKÓW"), self.empty_title, self.empty_hint, spacing=8
        )
        self.empty_state.setLayout(empty_lay)
        self.empty_state.setVisible(False)
        body_lay.addWidget(self.empty_state)
        body_lay.addWidget(self.cross_button, 0, Qt.AlignmentFlag.AlignLeft)
        body_lay.addStretch(1)

        self.scroll = QScrollArea()
        self.scroll.setObjectName(f"scroll_{page_id}")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(body)
        self.scroll.viewport().installEventFilter(self)
        self._body = body
        self._body_lay = body_lay

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(head)
        lay.addWidget(self.scroll, 1)

        selection.changed.connect(self.refresh)
        selection.lockedChanged.connect(lambda _l: self.refresh())
        self.refresh()

    # -- chips
    def _make_chip(self, prof: Profile) -> QPushButton:
        ids = prof.items_for(self._arch)
        usable = [i for i in ids if self._selection.is_selectable(i)]
        chip = button(
            f"{prof.name}  {len(usable)}",
            variant="chip",
            on_click=lambda: self._selection.toggle_group(prof.items_for(self._arch)),
            object_name="profileChip",
            tooltip=(
                f"{prof.description}\n{len(usable)} pozycji (aplikacje i system). "
                "Kliknij ponownie, aby odznaczyć ten zestaw."
            ),
        )
        chip.setProperty("profileId", prof.id)
        chip.setCheckable(True)
        # The checked state mirrors the model; the click itself must not flip it.
        chip.clicked.connect(lambda _c=False, c=chip, p=prof: self._sync_chip(c, p))
        return chip

    def _sync_chip(self, chip: QPushButton, prof: Profile) -> None:
        chip.setChecked(self._selection.all_selected(prof.items_for(self._arch)))

    # -- state
    def refresh(self) -> None:
        locked = self._selection.locked
        for item_id, tile in self.tiles.items():
            tile.set_checked(self._selection.is_selected(item_id))
            tile.set_locked(locked)
        for section in self.sections.values():
            section.refresh(self._selection)
        for prof_id, chip in self.profile_buttons.items():
            self._sync_chip(chip, self._profiles[prof_id])
            chip.setEnabled(not locked)

    def item_ids(self) -> list[str]:
        return list(self.tiles)

    def selected_count(self) -> int:
        return self._selection.count(self.tiles)

    def _toggle_category(self, category_id: str) -> None:
        section = self.sections[category_id]
        self._selection.toggle_group(t.item.id for t in section.tiles)

    # -- search
    def set_cross_page_counter(self, counter: Callable[[str], int], page_label: str) -> None:
        """Show "N trafień na stronie X →" for searches that match items elsewhere."""
        self._cross_counter = counter
        self._cross_label = page_label

    def count_matches(self, needle: str) -> int:
        return sum(1 for t in self.tiles.values() if t.matches(needle))

    def set_filter(self, text: str) -> int:
        needle = text.strip()
        if self.search.text() != text:
            self.search.blockSignals(True)
            self.search.setText(text)
            self.search.blockSignals(False)
        matches = sum(s.apply_filter(needle) for s in self.sections.values())
        self.empty_state.setVisible(matches == 0)
        self.empty_title.setText(f"Nic nie pasuje do „{needle}”.")
        other = self._cross_counter(needle) if (self._cross_counter and needle) else 0
        if other:
            self.cross_button.setText(f"{plural_hits(other)} na stronie {self._cross_label} →")
        self.cross_button.setVisible(bool(other))
        self._relayout(force=True)
        self.filterChanged.emit(needle, matches)
        return matches

    def _jump(self) -> None:
        self.jumpRequested.emit(self.search.text())

    def focus_search(self) -> None:
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def visible_ids(self) -> list[str]:
        return [i for i, t in self.tiles.items() if not t.isHidden() and t.isVisibleTo(self)]

    # -- layout
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._relayout()
        return super().eventFilter(watched, event)

    def _relayout(self, *, force: bool = False) -> None:
        # Keep the tile grid's right edge aligned with the header whether or not the
        # scrollbar (inside the viewport's margin) is shown.
        bar_width = max(0, self.scroll.width() - self.scroll.viewport().width())
        right = max(0, PAGE_MARGIN_H - bar_width)
        m = self._body_lay.contentsMargins()
        if m.right() != right:
            self._body_lay.setContentsMargins(m.left(), m.top(), right, m.bottom())
        width = self.scroll.viewport().width() - 2 * PAGE_MARGIN_H
        cols = _columns_for(width)
        for section in self.sections.values():
            section.relayout(cols, force=force)

    # -- keyboard navigation between tiles (geometric, across sections)
    def _navigate(self, tile: ItemTile, key: int) -> None:
        candidates = [
            t
            for s in self.sections.values()
            if not s.isHidden()
            for t in s.visible_tiles()
            if t is not tile and t.focusPolicy() != Qt.FocusPolicy.NoFocus
        ]
        if not candidates:
            return
        origin = tile.mapTo(self._body, QPoint(tile.width() // 2, tile.height() // 2))
        best: ItemTile | None = None
        best_score: tuple[int, ...] | None = None
        for cand in candidates:
            c = cand.mapTo(self._body, QPoint(cand.width() // 2, cand.height() // 2))
            dx, dy = c.x() - origin.x(), c.y() - origin.y()
            same_row = abs(dy) <= 4
            score: tuple[int, ...] | None = None
            if key == Qt.Key.Key_Right:
                if same_row and dx > 4:
                    score = (0, dx)
                elif dy > 4:  # wrap to the first tile of a following row
                    score = (1, dy, c.x())
            elif key == Qt.Key.Key_Left:
                if same_row and dx < -4:
                    score = (0, -dx)
                elif dy < -4:
                    score = (1, -dy, -c.x())
            elif key == Qt.Key.Key_Down and dy > 4:
                score = (dy, abs(dx))
            elif key == Qt.Key.Key_Up and dy < -4:
                score = (-dy, abs(dx))
            if score is not None and (best_score is None or score < best_score):
                best, best_score = cand, score
        if best is not None:
            best.setFocus(Qt.FocusReason.TabFocusReason)
            self.scroll.ensureWidgetVisible(best, 0, 24)
