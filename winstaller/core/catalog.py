"""Declarative catalog of installable items, categories and profiles.

The data lives in ``catalog.toml`` next to this module; see docs/ARCHITECTURE.md
for the format. Loading validates the file and raises :class:`CatalogError`
with a readable (Polish) message on any problem.
"""

from __future__ import annotations

import glob
import os
import re
import tomllib
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Any

from winstaller.core import platform as plat
from winstaller.core.platform import ALL_ARCHES, Arch


class CatalogError(Exception):
    """Invalid catalog file or unknown catalog id."""


class ItemKind(StrEnum):
    WINGET = "winget"  # winget install <winget_id>
    WINGET_UNINSTALL = "winget_uninstall"  # winget uninstall <winget_id>
    LOCAL_EXE = "local_exe"  # run <path> (relative to app_dir, glob allowed) with <args>
    ACTION = "action"  # call actions.ACTIONS[<action>] with the item's <args>


class CategoryKind(StrEnum):
    INSTALL = "install"  # items are selected and queued together
    TOOLS = "tools"  # immediate one-off actions (UI "Narzędzia" page)


@dataclass(frozen=True, slots=True)
class Category:
    id: str
    name: str
    description: str
    order: int
    kind: CategoryKind = CategoryKind.INSTALL


@dataclass(frozen=True, slots=True)
class Item:
    id: str
    name: str
    category: str
    kind: ItemKind
    description: str = ""
    winget_id: str | None = None
    action: str | None = None
    path: str | None = None
    args: tuple[str, ...] = ()
    arch: tuple[Arch, ...] = ALL_ARCHES
    requires_files: tuple[str, ...] = ()
    post_launch: str | None = None
    confirm: str | None = None
    unavailable: str | None = None

    @property
    def destructive(self) -> bool:
        """True when the UI must ask for confirmation (non-blank text in :attr:`confirm`)."""
        return bool(self.confirm and self.confirm.strip())

    def supports(self, arch: Arch) -> bool:
        return arch in self.arch


@dataclass(frozen=True, slots=True)
class Profile:
    id: str
    name: str
    description: str
    items: tuple[str, ...]
    arch_items: Mapping[Arch, tuple[str, ...]] = field(default_factory=dict)

    def items_for(self, arch: Arch) -> tuple[str, ...]:
        """Base items followed by the extra items for ``arch``."""
        return self.items + tuple(i for i in self.arch_items.get(arch, ()) if i not in self.items)


@dataclass(frozen=True, slots=True)
class Availability:
    available: bool
    reason: str | None = None


_AVAILABLE = Availability(True, None)
_ARCH_LABEL: dict[str, str] = {"x64": "x64", "arm64": "ARM64"}


_NUMBER_RUN = re.compile(r"(\d+)")


def natural_key(text: str) -> tuple[tuple[int, int, str], ...]:
    """Sort key comparing digit runs numerically (``19.10`` > ``19.9``), text case-insensitively."""
    return tuple(
        (0, int(part), "") if n % 2 else (1, 0, part.casefold())
        for n, part in enumerate(_NUMBER_RUN.split(text))
        if part
    )


def resolve_relative(base: Path, rel: str) -> Path | None:
    """Resolve ``rel`` (may contain glob wildcards) against ``base``.

    Only ``rel`` is a pattern: ``[``/``]`` in ``base`` are literal. With several
    matches the highest version wins (natural sort: ``19.10`` > ``19.9``), so the
    newest ``..._19.1_...`` style versioned installer is picked.
    """
    candidate = base / rel
    if glob.has_magic(rel):
        matches = glob.glob(os.path.join(glob.escape(str(base)), rel))
        if not matches:
            return None
        return Path(max(matches, key=lambda m: natural_key(os.path.relpath(m, base))))
    return candidate if candidate.exists() else None


class Catalog:
    """Immutable, validated view of the catalog."""

    def __init__(
        self,
        categories: Iterable[Category],
        items: Iterable[Item],
        profiles: Iterable[Profile],
    ) -> None:
        self._categories = tuple(sorted(categories, key=lambda c: c.order))
        self._items = tuple(items)
        self._profiles = tuple(profiles)
        self._category_by_id = {c.id: c for c in self._categories}
        self._item_by_id = {i.id: i for i in self._items}
        self._profile_by_id = {p.id: p for p in self._profiles}
        self._order = {i.id: n for n, i in enumerate(self._items)}

    # -- categories
    def categories(self, kind: CategoryKind | None = None) -> list[Category]:
        """Categories in display order, optionally filtered by kind."""
        return [c for c in self._categories if kind is None or c.kind == kind]

    def category(self, category_id: str) -> Category:
        try:
            return self._category_by_id[category_id]
        except KeyError:
            raise CatalogError(f"Nieznana kategoria: {category_id!r}") from None

    # -- items
    def items(self, category_id: str | None = None) -> list[Item]:
        """All items (catalog order), or only those of one category."""
        if category_id is None:
            return list(self._items)
        self.category(category_id)
        return [i for i in self._items if i.category == category_id]

    def item(self, item_id: str) -> Item:
        try:
            return self._item_by_id[item_id]
        except KeyError:
            raise CatalogError(f"Nieznana pozycja katalogu: {item_id!r}") from None

    def get(self, item_id: str) -> Item | None:
        return self._item_by_id.get(item_id)

    def __contains__(self, item_id: object) -> bool:
        return item_id in self._item_by_id

    def tools(self) -> list[Item]:
        """Items in categories of kind ``tools`` (immediate actions)."""
        tool_cats = {c.id for c in self.categories(CategoryKind.TOOLS)}
        return [i for i in self._items if i.category in tool_cats]

    def is_tool(self, item: Item) -> bool:
        return self.category(item.category).kind == CategoryKind.TOOLS

    def in_catalog_order(self, item_ids: Iterable[str]) -> list[str]:
        """Deduplicated ids sorted by catalog order. Raises on unknown ids."""
        unique = dict.fromkeys(item_ids)
        for item_id in unique:
            self.item(item_id)
        return sorted(unique, key=self._order.__getitem__)

    # -- profiles
    def profiles(self) -> list[Profile]:
        return list(self._profiles)

    def profile(self, profile_id: str) -> Profile:
        try:
            return self._profile_by_id[profile_id]
        except KeyError:
            raise CatalogError(f"Nieznany profil: {profile_id!r}") from None

    def profile_items(self, profile_id: str, arch: Arch | None = None) -> list[Item]:
        """Items of a profile for ``arch`` (default: current architecture)."""
        ids = self.profile(profile_id).items_for(arch or plat.get_arch())
        return [self._item_by_id[i] for i in ids]

    # -- availability
    def availability(
        self,
        item: Item | str,
        *,
        arch: Arch | None = None,
        base_dir: Path | None = None,
    ) -> Availability:
        """Whether ``item`` can run here, with a Polish reason when it cannot.

        Checks, in order: static ``unavailable`` note, architecture, required
        local files (``requires_files`` and the ``local_exe`` path) under
        ``base_dir`` (default :func:`platform.app_dir`).
        """
        it = self.item(item) if isinstance(item, str) else item
        if it.unavailable:
            return Availability(False, it.unavailable)
        current = arch or plat.get_arch()
        if not it.supports(current):
            wanted = ", ".join(_ARCH_LABEL[a] for a in it.arch)
            return Availability(
                False, f"Niedostępne na architekturze {_ARCH_LABEL[current]} (tylko {wanted})."
            )
        base = base_dir or plat.app_dir()
        required = list(it.requires_files)
        if it.kind == ItemKind.LOCAL_EXE and it.path:
            required.append(it.path)
        missing = [rel for rel in required if resolve_relative(base, rel) is None]
        if missing:
            names = ", ".join(missing)
            return Availability(False, f"Brak wymaganego pliku: {names} (szukano w {base}).")
        return _AVAILABLE

    def resolve_path(self, item: Item, *, base_dir: Path | None = None) -> Path | None:
        """Resolved ``local_exe`` path (glob expanded), or None when missing."""
        if not item.path:
            return None
        return resolve_relative(base_dir or plat.app_dir(), item.path)


# ---------------------------------------------------------------- loading

DEFAULT_CATALOG_PATH = plat.resource_path("core/catalog.toml")

_ITEM_KEYS = {
    "id",
    "name",
    "category",
    "kind",
    "description",
    "winget_id",
    "action",
    "path",
    "args",
    "arch",
    "requires_files",
    "post_launch",
    "confirm",
    "unavailable",
}
_CATEGORY_KEYS = {"id", "name", "description", "kind"}
_PROFILE_KEYS = {"id", "name", "description", "items", "arch_items"}


def _str(entry: Mapping[str, Any], key: str, where: str, *, required: bool = True) -> str | None:
    value = entry.get(key)
    if value is None:
        if required:
            raise CatalogError(f"{where}: brak wymaganego pola {key!r}.")
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise CatalogError(f"{where}: pole {key!r} musi być niepustym tekstem.")
    return value


def _str_list(entry: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = entry.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise CatalogError(f"{where}: pole {key!r} musi być listą tekstów.")
    return tuple(value)


def _check_keys(entry: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(entry) - allowed
    if unknown:
        raise CatalogError(f"{where}: nieznane pola {sorted(unknown)}.")


def _arch_list(values: tuple[str, ...], where: str) -> tuple[Arch, ...]:
    if not values:
        return ALL_ARCHES
    bad = [v for v in values if v not in ALL_ARCHES]
    if bad:
        raise CatalogError(f"{where}: nieznana architektura {bad} (dozwolone: x64, arm64).")
    return tuple(a for a in ALL_ARCHES if a in values)


def _parse_category(entry: Mapping[str, Any], order: int) -> Category:
    where = f"Kategoria #{order + 1}"
    _check_keys(entry, _CATEGORY_KEYS, where)
    cid = _str(entry, "id", where)
    where = f"Kategoria {cid!r}"
    kind_raw = _str(entry, "kind", where, required=False) or CategoryKind.INSTALL.value
    try:
        kind = CategoryKind(kind_raw)
    except ValueError:
        raise CatalogError(f"{where}: nieznany rodzaj kategorii {kind_raw!r}.") from None
    return Category(
        id=cid,  # type: ignore[arg-type]
        name=_str(entry, "name", where),  # type: ignore[arg-type]
        description=_str(entry, "description", where, required=False) or "",
        order=order,
        kind=kind,
    )


#: Actions whose ``args[0]`` is a folder relative to the program directory.
_FOLDER_ARG_ACTIONS = frozenset({"copy_dir_to_desktop"})


def is_relative_inside(rel: str) -> bool:
    """True for a path relative to the program directory that cannot leave it.

    Rejects absolute paths, drive-relative ones (``D:setup.exe``), rooted ones
    (``\\Windows``) and any ``..`` component (both separators count).
    """
    pure = PureWindowsPath(rel)
    return bool(rel.strip()) and not pure.drive and not pure.root and ".." not in pure.parts


def _check_relative(rel: str, where: str) -> None:
    if not is_relative_inside(rel):
        raise CatalogError(f"{where}: ścieżka {rel!r} musi być względna wobec katalogu programu.")


def _parse_item(entry: Mapping[str, Any], index: int, known_actions: Collection[str]) -> Item:
    where = f"Pozycja #{index + 1}"
    _check_keys(entry, _ITEM_KEYS, where)
    iid = _str(entry, "id", where)
    where = f"Pozycja {iid!r}"
    kind_raw = _str(entry, "kind", where)
    try:
        kind = ItemKind(kind_raw)
    except ValueError:
        raise CatalogError(f"{where}: nieznany rodzaj {kind_raw!r}.") from None

    winget_id = _str(entry, "winget_id", where, required=False)
    action = _str(entry, "action", where, required=False)
    path = _str(entry, "path", where, required=False)
    if kind in (ItemKind.WINGET, ItemKind.WINGET_UNINSTALL) and not winget_id:
        raise CatalogError(f"{where}: rodzaj {kind.value!r} wymaga pola 'winget_id'.")
    if kind == ItemKind.LOCAL_EXE and not path:
        raise CatalogError(f"{where}: rodzaj 'local_exe' wymaga pola 'path'.")
    if kind == ItemKind.ACTION:
        if not action:
            raise CatalogError(f"{where}: rodzaj 'action' wymaga pola 'action'.")
        if action not in known_actions:
            raise CatalogError(f"{where}: nieznana akcja {action!r}.")
    elif action:
        raise CatalogError(f"{where}: pole 'action' jest dozwolone tylko dla rodzaju 'action'.")

    args = _str_list(entry, "args", where)
    for rel in (*_str_list(entry, "requires_files", where), *([path] if path else [])):
        _check_relative(rel, where)
    if action in _FOLDER_ARG_ACTIONS:
        if not args:
            raise CatalogError(f"{where}: akcja {action!r} wymaga nazwy folderu w 'args'.")
        _check_relative(args[0], where)
    confirm = _str(entry, "confirm", where, required=False)

    return Item(
        id=iid,  # type: ignore[arg-type]
        name=_str(entry, "name", where),  # type: ignore[arg-type]
        category=_str(entry, "category", where),  # type: ignore[arg-type]
        kind=kind,
        description=_str(entry, "description", where, required=False) or "",
        winget_id=winget_id,
        action=action,
        path=path,
        args=args,
        arch=_arch_list(_str_list(entry, "arch", where), where),
        requires_files=_str_list(entry, "requires_files", where),
        post_launch=_str(entry, "post_launch", where, required=False),
        confirm=confirm if confirm and confirm.strip() else None,
        unavailable=_str(entry, "unavailable", where, required=False),
    )


def _parse_profile(entry: Mapping[str, Any], index: int) -> Profile:
    where = f"Profil #{index + 1}"
    _check_keys(entry, _PROFILE_KEYS, where)
    pid = _str(entry, "id", where)
    where = f"Profil {pid!r}"
    arch_raw = entry.get("arch_items", {})
    if not isinstance(arch_raw, dict):
        raise CatalogError(f"{where}: 'arch_items' musi być tabelą arch -> lista id.")
    arch_items: dict[Arch, tuple[str, ...]] = {}
    for arch, ids in arch_raw.items():
        (key,) = _arch_list((arch,), where)
        arch_items[key] = _str_list({f"arch_items.{arch}": ids}, f"arch_items.{arch}", where)
    return Profile(
        id=pid,  # type: ignore[arg-type]
        name=_str(entry, "name", where),  # type: ignore[arg-type]
        description=_str(entry, "description", where, required=False) or "",
        items=_str_list(entry, "items", where),
        arch_items=arch_items,
    )


def _unique(ids: list[str], what: str) -> None:
    dupes = sorted(i for i, n in Counter(ids).items() if n > 1)
    if dupes:
        raise CatalogError(f"Zduplikowane id {what}: {', '.join(dupes)}.")


def parse_catalog(
    data: Mapping[str, Any], *, known_actions: Collection[str] | None = None
) -> Catalog:
    """Build and validate a :class:`Catalog` from already parsed TOML data."""
    if known_actions is None:
        from winstaller.core.actions import ACTIONS

        known_actions = ACTIONS.keys()

    unknown_top = set(data) - {"categories", "items", "profiles"}
    if unknown_top:
        raise CatalogError(f"Nieznane sekcje katalogu: {sorted(unknown_top)}.")
    for section in ("categories", "items", "profiles"):
        if not isinstance(data.get(section, []), list):
            raise CatalogError(f"Sekcja {section!r} musi być tablicą tabel ([[{section}]]).")

    for section in ("categories", "items", "profiles"):
        for n, entry in enumerate(data.get(section, [])):
            if not isinstance(entry, dict):
                raise CatalogError(
                    f"Sekcja {section!r}: wpis #{n + 1} musi być tabelą ([[{section}]]), "
                    f"a jest {type(entry).__name__}."
                )
    categories = [_parse_category(e, n) for n, e in enumerate(data.get("categories", []))]
    items = [_parse_item(e, n, known_actions) for n, e in enumerate(data.get("items", []))]
    profiles = [_parse_profile(e, n) for n, e in enumerate(data.get("profiles", []))]
    if not categories:
        raise CatalogError("Katalog nie zawiera żadnych kategorii.")

    _unique([c.id for c in categories], "kategorii")
    _unique([i.id for i in items], "pozycji")
    _unique([p.id for p in profiles], "profili")

    category_ids = {c.id for c in categories}
    item_by_id = {i.id: i for i in items}
    for it in items:
        if it.category not in category_ids:
            raise CatalogError(f"Pozycja {it.id!r}: nieznana kategoria {it.category!r}.")
    for prof in profiles:
        for label, ids in (
            ("items", prof.items),
            *((f"arch_items.{a}", v) for a, v in prof.arch_items.items()),
        ):
            dupes = sorted(i for i, n in Counter(ids).items() if n > 1)
            if dupes:
                raise CatalogError(
                    f"Profil {prof.id!r}: zduplikowane pozycje w {label!r}: {', '.join(dupes)}."
                )
        refs = [*prof.items, *(i for ids in prof.arch_items.values() for i in ids)]
        missing = sorted({r for r in refs if r not in item_by_id})
        if missing:
            raise CatalogError(f"Profil {prof.id!r}: nieistniejące pozycje {', '.join(missing)}.")
        for arch, ids in prof.arch_items.items():
            wrong = [i for i in ids if not item_by_id[i].supports(arch)]
            if wrong:
                raise CatalogError(
                    f"Profil {prof.id!r}: pozycje {wrong} nie wspierają architektury {arch}."
                )
    return Catalog(categories, items, profiles)


def load_catalog(
    path: str | os.PathLike[str] | None = None,
    *,
    known_actions: Collection[str] | None = None,
) -> Catalog:
    """Load and validate the catalog (default: bundled ``catalog.toml``)."""
    source = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    try:
        with source.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise CatalogError(f"Nie znaleziono pliku katalogu: {source}") from None
    except tomllib.TOMLDecodeError as exc:
        raise CatalogError(f"Błąd składni w {source.name}: {exc}") from None
    return parse_catalog(data, known_actions=known_actions)
