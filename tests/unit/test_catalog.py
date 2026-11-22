"""Catalog: the real catalog.toml, validation, availability and the query API."""

from __future__ import annotations

import copy
import re
import string
from pathlib import Path
from typing import Any

import pytest

from tests.support import make_catalog, make_item, minimal_data
from winstaller.core import catalog as cat_mod
from winstaller.core.actions import ACTIONS
from winstaller.core.catalog import (
    DEFAULT_CATALOG_PATH,
    Availability,
    Catalog,
    CatalogError,
    Category,
    CategoryKind,
    ItemKind,
    Profile,
    load_catalog,
    parse_catalog,
    resolve_relative,
)

# Display names of 1.1.8 (core/config.py CATEGORIES), hard-coded on purpose: the old
# module is removed in stage 3 and must not be imported by tests.
OLD_CATEGORIES: dict[str, list[str]] = {
    "Podstawowe programy": [
        "Google Chrome",
        "Telegram",
        "Messenger",
        "Discord",
        "TeamSpeak3",
        "7-Zip",
        "Windows Terminal",
    ],
    "Launchery": ["Steam", "Epic Games Store", "Ubisoft Connect", "EA Desktop", "Battle.net"],
    "Narzędzia diagnostyczne": [
        "HW Monitor",
        "HW Info",
        "CPU-Z",
        "GPU-Z",
        "DisplayCal",
        "DisplayDriverUninstaller",
        "NVCleanstall",
    ],
    "Narzędzia testowe": ["DirectX 9", "MSI Afterburner", "Rivatuner", "CapFrameX", "UL Procyon"],
    "Aplikacje profesjonalne": [
        "Davinci Resolve Studio",
        "Creative Cloud",
        "Blender",
        "LM Studio",
    ],
    "Konfiguracja systemu": [
        "Usuń bloatware z Windows",
        "Ciemny motyw Windows",
        "Jasny motyw Windows",
        "Zainstaluj OneDrive",
        "Odinstaluj OneDrive",
    ],
    "Dodatkowe": [
        "Kopiuj BenchmarkTools na pulpit",
        "Kopiuj winstaller na pulpit",
        "Instaluj lokalne oprogramowanie",
    ],
}
OLD_ARM_EXTRA = ("Dodatkowe", "Kopiuj BenchmarkToolsARM na pulpit")
# New in 2.0 (owner's request): GPU drivers resolved on the vendors' sites at run time.
NEW_CATEGORIES = {
    "Aktualizacje": ["Windows Update", "Aktualizacje Microsoft Store"],
    "Sterowniki GPU": ["NVIDIA GeForce Game Ready", "AMD Radeon Adrenalin", "Intel Arc"],
}
# New in 2.0 (owner's request): updates run first in the benchmark profile.
NEW_PROFILE_PREFIX = ["Windows Update", "Aktualizacje Microsoft Store"]
NEW_DRIVER_IDS = ("driver-nvidia", "driver-amd", "driver-intel")
OLD_SPLIT_ITEM = "Instaluj lokalne oprogramowanie"
# Deliberate 2.0 changes to the 1.1.8 list (requested by the project owner).
OLD_REMOVED = {"Telegram", "Messenger", OLD_SPLIT_ITEM}
NEW_ITEMS = {"Signal": "OpenWhisperSystems.Signal"}
CHANGED_WINGET_IDS = {"DisplayCal": "ErkanOzgurYilmaz.DisplayCAL"}
REMOVED_IDS = ("telegram", "messenger", "local-rtss", "local-capframex")
OLD_SPECIAL_BUTTONS = ["Restart do BIOS", "Restart zaawansowany", "Raport baterii"]

OLD_BENCHMARK_PROGRAMS = [
    "HW Monitor",
    "7-Zip",
    "HW Info",
    "Google Chrome",
    "Steam",
    "Epic Games Store",
    "Ubisoft Connect",
    "DirectX 9",
    "Rivatuner",
    "CapFrameX",
    "Windows Terminal",
    "Kopiuj BenchmarkTools na pulpit",
    "Usuń bloatware z Windows",
    "CPU-Z",
    "GPU-Z",
    "Ciemny motyw Windows",
    "Blender",
    "Kopiuj winstaller na pulpit",
    "Odinstaluj OneDrive",
    "LM Studio",
    "UL Procyon",
    "DisplayDriverUninstaller",
]

# winget ids used by 1.1.8 core/installers.py.
OLD_WINGET_IDS = {
    "Google Chrome": "Google.Chrome",
    "Telegram": "9NZTWSQNTD0S",
    "Messenger": "9WZDNCRF0083",
    "Discord": "Discord.Discord",
    "TeamSpeak3": "TeamSpeakSystems.TeamSpeakClient",
    "7-Zip": "7zip.7zip",
    "Windows Terminal": "9N0DX20HK701",
    "Steam": "Valve.Steam",
    "Epic Games Store": "EpicGames.EpicGamesLauncher",
    "Ubisoft Connect": "Ubisoft.Connect",
    "EA Desktop": "ElectronicArts.EADesktop",
    "Battle.net": "Blizzard.BattleNet",
    "HW Monitor": "CPUID.HWMonitor",
    "HW Info": "REALiX.HWiNFO",
    "CPU-Z": "CPUID.CPU-Z",
    "GPU-Z": "TechPowerUp.GPU-Z",
    "DisplayCal": "FlorianHoech.DisplayCAL",
    "DisplayDriverUninstaller": "Wagnardsoft.DisplayDriverUninstaller",
    "NVCleanstall": "TechPowerUp.NVCleanstall",
    "DirectX 9": "Microsoft.DirectX",
    "MSI Afterburner": "Guru3D.Afterburner",
    "Rivatuner": "Guru3D.RTSS",
    "CapFrameX": "CXWorld.CapFrameX",
    "Creative Cloud": "XPDLPKWG9SW2WD",
    "Blender": "BlenderFoundation.Blender",
    "LM Studio": "ElementLabs.LMStudio",
    "Zainstaluj OneDrive": "Microsoft.OneDrive",
    "Odinstaluj OneDrive": "Microsoft.OneDrive",
}


@pytest.fixture(scope="module")
def real() -> Catalog:
    return load_catalog()


# ------------------------------------------------------------ real catalog


class TestRealCatalog:
    def test_default_path_points_at_package_file(self) -> None:
        assert DEFAULT_CATALOG_PATH.is_file()
        assert DEFAULT_CATALOG_PATH.name == "catalog.toml"

    def test_ids_are_unique_stable_slugs(self, real: Catalog) -> None:
        slug = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        for ids in (
            [c.id for c in real.categories()],
            [i.id for i in real.items()],
            [p.id for p in real.profiles()],
        ):
            assert len(ids) == len(set(ids))
            assert all(slug.match(i) for i in ids), ids

    def test_items_are_well_formed(self, real: Catalog) -> None:
        categories = {c.id for c in real.categories()}
        for item in real.items():
            assert item.category in categories
            assert isinstance(item.kind, ItemKind)
            assert item.name.strip() and item.description.strip(), item.id
            if item.kind in (ItemKind.WINGET, ItemKind.WINGET_UNINSTALL):
                assert item.winget_id and " " not in item.winget_id
            if item.kind == ItemKind.ACTION:
                assert item.action in ACTIONS
            else:
                assert item.action is None
            if item.kind == ItemKind.LOCAL_EXE:
                assert item.path
            assert item.arch and set(item.arch) <= {"x64", "arm64"}

    def test_every_action_is_used_and_exists(self, real: Catalog) -> None:
        used = {i.action for i in real.items() if i.kind == ItemKind.ACTION}
        assert used == set(ACTIONS)

    def test_copy_dir_items_have_folder_argument_and_requirement(self, real: Catalog) -> None:
        for item in real.items():
            if item.action == "copy_dir_to_desktop":
                assert len(item.args) == 1
                assert item.requires_files == item.args

    def test_profiles_reference_existing_items(self, real: Catalog) -> None:
        for profile in real.profiles():
            for arch in ("x64", "arm64"):
                for item_id in profile.items_for(arch):
                    assert item_id in real
                    assert real.item(item_id).supports(arch)

    def test_category_order_and_kinds(self, real: Catalog) -> None:
        assert [c.id for c in real.categories()] == [
            "updates",
            "basic",
            "launchers",
            "diagnostics",
            "testing",
            "gpu-drivers",
            "pro",
            "system",
            "extras",
            "tools",
        ]
        assert [c.id for c in real.categories(CategoryKind.TOOLS)] == ["tools"]
        assert [c.order for c in real.categories()] == list(range(10))

    def test_tools_and_destructive_items(self, real: Catalog) -> None:
        assert [i.id for i in real.tools()] == ["reboot-bios", "reboot-advanced", "battery-report"]
        destructive = {i.id for i in real.items() if i.destructive}
        assert destructive == {
            "windows-update",
            "remove-bloat",
            "reboot-bios",
            "reboot-advanced",
            *NEW_DRIVER_IDS,
        }

    def test_known_special_cases(self, real: Catalog) -> None:
        assert not any(i.unavailable for i in real.items())
        assert real.item("copy-benchmark-tools-arm").arch == ("arm64",)
        assert real.item("copy-benchmark-tools").arch == ("x64", "arm64")
        assert real.item("steam").post_launch == r"%ProgramFiles(x86)%\Steam\Steam.exe"
        assert real.item("davinci-resolve-studio").kind == ItemKind.LOCAL_EXE
        assert real.item("onedrive-uninstall").kind == ItemKind.WINGET_UNINSTALL


class TestParityWith118:
    def test_category_names_and_order(self, real: Catalog) -> None:
        names = [c.name for c in real.categories(CategoryKind.INSTALL)]
        assert [n for n in names if n not in NEW_CATEGORIES] == list(OLD_CATEGORIES)

    def test_every_old_item_exists_in_same_category(self, real: Catalog) -> None:
        by_name = {i.name: i for i in real.items()}
        for category_name, names in OLD_CATEGORIES.items():
            for name in names:
                if name in OLD_REMOVED:
                    continue
                assert name in by_name, name
                assert real.category(by_name[name].category).name == category_name

    def test_old_items_keep_relative_order(self, real: Catalog) -> None:
        for category in real.categories(CategoryKind.INSTALL):
            if category.name in NEW_CATEGORIES:
                continue
            new_names = [i.name for i in real.items(category.id)]
            old = [n for n in OLD_CATEGORIES[category.name] if n not in OLD_REMOVED]
            assert [n for n in new_names if n in old] == old

    def test_removed_and_replaced_items(self, real: Catalog) -> None:
        for item_id in REMOVED_IDS:
            assert real.get(item_id) is None, item_id
        signal = real.item("signal")
        assert (signal.category, signal.kind) == ("basic", ItemKind.WINGET)
        assert signal.winget_id == NEW_ITEMS["Signal"]

    def test_arm_extra_and_special_buttons(self, real: Catalog) -> None:
        category_name, name = OLD_ARM_EXTRA
        item = next(i for i in real.items() if i.name == name)
        assert real.category(item.category).name == category_name
        assert [i.name for i in real.tools()] == OLD_SPECIAL_BUTTONS

    def test_no_unexpected_new_items(self, real: Catalog) -> None:
        old = {n for names in OLD_CATEGORIES.values() for n in names}
        old |= {OLD_ARM_EXTRA[1], *OLD_SPECIAL_BUTTONS}
        new = {i.name for i in real.items()}
        new_drivers = {n for names in NEW_CATEGORIES.values() for n in names}
        assert new - old == set(NEW_ITEMS) | new_drivers

    def test_winget_ids_unchanged(self, real: Catalog) -> None:
        by_name = {i.name: i for i in real.items()}
        for name, winget_id in OLD_WINGET_IDS.items():
            if name in OLD_REMOVED:
                continue
            assert by_name[name].winget_id == CHANGED_WINGET_IDS.get(name, winget_id), name

    def test_benchmark_profile_matches_old_benchmark_programs(self, real: Catalog) -> None:
        profile = real.profile("benchmark-starter")
        assert [real.item(i).name for i in profile.items_for("x64")] == [
            *NEW_PROFILE_PREFIX,
            *OLD_BENCHMARK_PROGRAMS,
        ]
        assert [real.item(i).name for i in profile.items_for("arm64")] == [
            *NEW_PROFILE_PREFIX,
            *OLD_BENCHMARK_PROGRAMS,
            OLD_ARM_EXTRA[1],
        ]


# ------------------------------------------------------------- validation


def _bad(data: dict[str, Any], match: str, **kwargs: Any) -> None:
    with pytest.raises(CatalogError, match=match):
        parse_catalog(data, **kwargs)


def _item(**fields: Any) -> dict[str, Any]:
    entry = {"id": "x", "name": "X", "category": "cat", "kind": "winget", "winget_id": "V.X"}
    entry.update(fields)
    return {k: v for k, v in entry.items() if v is not None}


class TestValidation:
    def test_minimal_is_valid(self) -> None:
        catalog = parse_catalog(minimal_data())
        assert [i.id for i in catalog.items()] == ["a"]

    def test_real_file_data_round_trips(self) -> None:
        import tomllib

        data = tomllib.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
        assert [i.id for i in parse_catalog(data).items()] == [i.id for i in load_catalog().items()]

    @pytest.mark.parametrize(
        ("section", "entries", "match"),
        [
            ("categories", [{"id": "cat", "name": "A"}, {"id": "cat", "name": "B"}], "kategorii"),
            ("items", [_item(id="a"), _item(id="a")], "pozycji: a"),
            ("profiles", [{"id": "p", "name": "P"}, {"id": "p", "name": "Q"}], "profili: p"),
        ],
    )
    def test_duplicates(self, section: str, entries: list, match: str) -> None:
        _bad(minimal_data(**{section: entries}), match)

    def test_unknown_item_category(self) -> None:
        _bad(minimal_data(items=[_item(category="nope")]), "nieznana kategoria 'nope'")

    def test_unknown_item_kind(self) -> None:
        _bad(minimal_data(items=[_item(kind="msi")]), "nieznany rodzaj 'msi'")

    def test_unknown_category_kind(self) -> None:
        _bad(minimal_data(categories=[{"id": "cat", "name": "C", "kind": "magic"}]), "magic")

    def test_unknown_action(self) -> None:
        _bad(minimal_data(items=[_item(kind="action", action="format_c")]), "format_c")

    def test_known_actions_override(self) -> None:
        data = minimal_data(items=[_item(kind="action", action="custom", winget_id=None)])
        catalog = parse_catalog(data, known_actions={"custom"})
        assert catalog.item("x").action == "custom"
        _bad(
            minimal_data(items=[_item(kind="action", action="theme_dark")]),
            "theme_dark",
            known_actions={"custom"},
        )

    def test_action_field_only_for_action_kind(self) -> None:
        _bad(minimal_data(items=[_item(action="theme_dark")]), "tylko dla rodzaju 'action'")

    @pytest.mark.parametrize(
        ("fields", "match"),
        [
            ({"winget_id": None}, "winget_id"),
            ({"kind": "winget_uninstall", "winget_id": None}, "winget_id"),
            ({"kind": "local_exe", "winget_id": None}, "'path'"),
            ({"kind": "action", "winget_id": None}, "'action'"),
            ({"winget_id": ""}, "winget_id"),
        ],
    )
    def test_kind_specific_required_fields(self, fields: dict, match: str) -> None:
        _bad(minimal_data(items=[_item(**fields)]), match)

    @pytest.mark.parametrize("field_name", ["id", "name", "category", "kind"])
    def test_missing_item_field(self, field_name: str) -> None:
        entry = _item()
        del entry[field_name]
        _bad(minimal_data(items=[entry]), f"brak wymaganego pola '{field_name}'")

    @pytest.mark.parametrize("field_name", ["id", "name"])
    def test_missing_category_field(self, field_name: str) -> None:
        entry = {"id": "cat", "name": "C"}
        del entry[field_name]
        _bad(minimal_data(categories=[entry], items=[]), field_name)

    def test_missing_profile_name(self) -> None:
        _bad(minimal_data(profiles=[{"id": "p", "items": ["a"]}]), "'name'")

    @pytest.mark.parametrize(
        ("fields", "match"),
        [
            ({"name": 5}, "'name' musi być niepustym tekstem"),
            ({"name": "   "}, "'name' musi być niepustym tekstem"),
            ({"description": ["x"]}, "'description'"),
            ({"args": "/S"}, "'args' musi być listą tekstów"),
            ({"args": ["/S", 1]}, "'args'"),
            ({"arch": "x64"}, "'arch'"),
            ({"arch": ["x86"]}, "nieznana architektura"),
            ({"requires_files": {"a": 1}}, "'requires_files'"),
            ({"confirm": True}, "'confirm'"),
        ],
    )
    def test_wrong_types(self, fields: dict, match: str) -> None:
        _bad(minimal_data(items=[_item(**fields)]), match)

    @pytest.mark.parametrize(
        "rel",
        ["C:\\Windows\\notepad.exe", "\\Windows\\x.exe", "/etc/x", "..\\x.exe", "a/../../x.exe"],
    )
    @pytest.mark.parametrize("field_name", ["path", "requires_files"])
    def test_paths_must_be_relative_and_inside(self, rel: str, field_name: str) -> None:
        if field_name == "path":
            entry = _item(kind="local_exe", winget_id=None, path=rel)
        else:
            entry = _item(requires_files=[rel])
        _bad(minimal_data(items=[entry]), "musi być względna")

    def test_drive_relative_path_rejected(self, tmp_path: Path) -> None:
        other = next(d for d in string.ascii_uppercase if f"{d}:" != tmp_path.drive.upper())
        entry = _item(kind="local_exe", winget_id=None, path=f"{other}:setup.exe")
        _bad(minimal_data(items=[entry]), "musi być względna")

    @pytest.mark.parametrize(
        ("section", "entry"),
        [
            ("items", _item(color="red")),
            ("categories", {"id": "cat", "name": "C", "icon": "x"}),
            ("profiles", {"id": "p", "name": "P", "order": 1}),
        ],
    )
    def test_unknown_fields(self, section: str, entry: dict) -> None:
        data = minimal_data()
        data[section] = [entry] if section != "categories" else [entry]
        _bad(data, "nieznane pola")

    def test_unknown_top_level_section(self) -> None:
        _bad(minimal_data(settings={}), "Nieznane sekcje")

    @pytest.mark.parametrize("section", ["categories", "items", "profiles"])
    def test_section_must_be_array_of_tables(self, section: str) -> None:
        _bad(minimal_data(**{section: {"id": "x"}}), "musi być tablicą tabel")

    @pytest.mark.parametrize("section", ["categories", "items", "profiles"])
    def test_non_table_entries_raise_catalog_error(self, section: str) -> None:
        with pytest.raises(CatalogError):
            parse_catalog(minimal_data(**{section: [1]}))

    def test_no_categories(self) -> None:
        _bad({"items": []}, "żadnych kategorii")

    def test_profile_with_missing_items(self) -> None:
        _bad(minimal_data(profiles=[{"id": "p", "name": "P", "items": ["a", "ghost"]}]), "ghost")

    def test_profile_arch_items_missing(self) -> None:
        profile = {"id": "p", "name": "P", "items": [], "arch_items": {"arm64": ["ghost"]}}
        _bad(minimal_data(profiles=[profile]), "ghost")

    def test_profile_arch_items_unknown_arch(self) -> None:
        profile = {"id": "p", "name": "P", "items": [], "arch_items": {"x86": ["a"]}}
        _bad(minimal_data(profiles=[profile]), "nieznana architektura")

    def test_profile_arch_items_must_be_table_of_lists(self) -> None:
        _bad(minimal_data(profiles=[{"id": "p", "name": "P", "arch_items": ["a"]}]), "tabelą")
        _bad(
            minimal_data(profiles=[{"id": "p", "name": "P", "arch_items": {"arm64": "a"}}]),
            "arch_items.arm64",
        )

    def test_profile_arch_item_must_support_arch(self) -> None:
        data = minimal_data(
            items=[_item(id="x64only", arch=["x64"])],
            profiles=[{"id": "p", "name": "P", "arch_items": {"arm64": ["x64only"]}}],
        )
        _bad(data, "nie wspierają architektury arm64")

    def test_arch_is_normalized_to_canonical_order(self) -> None:
        catalog = parse_catalog(minimal_data(items=[_item(arch=["arm64", "x64"])]))
        assert catalog.item("x").arch == ("x64", "arm64")

    def test_error_names_the_offending_entry(self) -> None:
        with pytest.raises(CatalogError, match=r"Pozycja 'broken'"):
            parse_catalog(minimal_data(items=[_item(id="broken", kind="nope")]))
        with pytest.raises(CatalogError, match=r"Pozycja #2"):
            parse_catalog(minimal_data(items=[_item(id="ok"), {"name": "no id"}]))

    def test_input_is_not_mutated(self) -> None:
        data = minimal_data(profiles=[{"id": "p", "name": "P", "items": ["a"]}])
        before = copy.deepcopy(data)
        parse_catalog(data)
        assert data == before


class TestLoadCatalog:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(CatalogError, match="Nie znaleziono pliku katalogu"):
            load_catalog(tmp_path / "missing.toml")

    def test_syntax_error(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.toml"
        path.write_text("[[categories]\nid = ", encoding="utf-8")
        with pytest.raises(CatalogError, match="Błąd składni w bad.toml"):
            load_catalog(path)

    def test_custom_file(self, tmp_path: Path) -> None:
        path = tmp_path / "c.toml"
        path.write_text(
            '[[categories]]\nid = "c"\nname = "Ć"\n\n'
            '[[items]]\nid = "i"\nname = "Żółw"\ncategory = "c"\nkind = "local_exe"\n'
            "path = 'sub\\setup.exe'\n",
            encoding="utf-8",
        )
        catalog = load_catalog(str(path))
        assert catalog.item("i").name == "Żółw"
        assert catalog.item("i").path == "sub\\setup.exe"


# ----------------------------------------------------------- availability


class TestAvailability:
    def test_available_without_requirements(self, tmp_path: Path) -> None:
        catalog = make_catalog([make_item("a")])
        assert catalog.availability("a", arch="x64", base_dir=tmp_path) == Availability(True)

    def test_unavailable_note_wins_over_everything(self, tmp_path: Path) -> None:
        item = make_item("a", arch=("arm64",), requires_files=("nope",), unavailable="Wycofany.")
        catalog = make_catalog([item])
        assert catalog.availability(item, arch="x64", base_dir=tmp_path) == Availability(
            False, "Wycofany."
        )

    def test_architecture(self, tmp_path: Path) -> None:
        catalog = make_catalog([make_item("a", arch=("arm64",)), make_item("b", arch=("x64",))])
        result = catalog.availability("a", arch="x64", base_dir=tmp_path)
        assert result == Availability(False, "Niedostępne na architekturze x64 (tylko ARM64).")
        assert catalog.availability("a", arch="arm64", base_dir=tmp_path).available
        assert "ARM64 (tylko x64)" in (catalog.availability("b", arch="arm64").reason or "")

    def test_architecture_checked_before_files(self, tmp_path: Path) -> None:
        catalog = make_catalog([make_item("a", arch=("arm64",), requires_files=("Tools",))])
        assert "architekturze" in (
            catalog.availability("a", arch="x64", base_dir=tmp_path).reason or ""
        )

    def test_requires_files_missing_and_present(self, tmp_path: Path) -> None:
        catalog = make_catalog([make_item("a", requires_files=("BenchmarkTools", "x.dll"))])
        result = catalog.availability("a", arch="x64", base_dir=tmp_path)
        assert not result.available
        assert result.reason == (
            f"Brak wymaganego pliku: BenchmarkTools, x.dll (szukano w {tmp_path})."
        )
        (tmp_path / "BenchmarkTools").mkdir()
        (tmp_path / "x.dll").write_bytes(b"")
        assert catalog.availability("a", arch="x64", base_dir=tmp_path).available

    def test_local_exe_path_with_subdirectory(self, tmp_path: Path) -> None:
        item = make_item("p", kind=ItemKind.LOCAL_EXE, path="procyon\\procyon-setup.exe")
        catalog = make_catalog([item])
        assert "procyon\\procyon-setup.exe" in (
            catalog.availability(item, arch="x64", base_dir=tmp_path).reason or ""
        )
        (tmp_path / "procyon").mkdir()
        (tmp_path / "procyon" / "procyon-setup.exe").write_bytes(b"MZ")
        assert catalog.availability(item, arch="x64", base_dir=tmp_path).available
        assert (
            catalog.resolve_path(item, base_dir=tmp_path)
            == tmp_path / "procyon" / "procyon-setup.exe"
        )

    def test_path_of_non_local_item_is_not_required(self, tmp_path: Path) -> None:
        catalog = make_catalog([make_item("a", path="missing.exe")])
        assert catalog.availability("a", arch="x64", base_dir=tmp_path).available

    def test_glob_picks_lexicographically_last(self, tmp_path: Path) -> None:
        pattern = "DaVinci_Resolve_Studio_*_Windows.exe"
        item = make_item("d", kind=ItemKind.LOCAL_EXE, path=pattern)
        catalog = make_catalog([item])
        assert not catalog.availability(item, arch="x64", base_dir=tmp_path).available
        assert catalog.resolve_path(item, base_dir=tmp_path) is None
        for version in ("18.6.5", "19.1.4", "19.0"):
            (tmp_path / f"DaVinci_Resolve_Studio_{version}_Windows.exe").write_bytes(b"")
        (tmp_path / "DaVinci_Resolve_Studio_20.0_Linux.run").write_bytes(b"")
        assert catalog.availability(item, arch="x64", base_dir=tmp_path).available
        assert catalog.resolve_path(item, base_dir=tmp_path) == (
            tmp_path / "DaVinci_Resolve_Studio_19.1.4_Windows.exe"
        )

    def test_glob_in_directory_with_brackets(self, tmp_path: Path) -> None:
        base = tmp_path / "Pobrane [2]"
        base.mkdir()
        (base / "DaVinci_Resolve_Studio_19.1_Windows.exe").write_bytes(b"")
        assert resolve_relative(base, "DaVinci_Resolve_Studio_*_Windows.exe") is not None

    def test_plain_path_in_directory_with_brackets(self, tmp_path: Path) -> None:
        base = tmp_path / "Pobrane [2]"
        base.mkdir()
        (base / "RTSSSetup734.exe").write_bytes(b"")
        assert resolve_relative(base, "RTSSSetup734.exe") == base / "RTSSSetup734.exe"

    def test_resolve_path_without_path(self, tmp_path: Path) -> None:
        catalog = make_catalog([make_item("a")])
        assert catalog.resolve_path(catalog.item("a"), base_dir=tmp_path) is None

    def test_defaults_come_from_platform(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WINSTALLER_ARCH", "arm64")
        monkeypatch.setenv("WINSTALLER_APP_DIR", str(tmp_path))
        catalog = make_catalog(
            [
                make_item("arm", arch=("arm64",), requires_files=("f",)),
                make_item("x", arch=("x64",)),
            ]
        )
        assert not catalog.availability("x").available
        assert str(tmp_path) in (catalog.availability("arm").reason or "")
        (tmp_path / "f").write_bytes(b"")
        assert catalog.availability("arm").available

    def test_real_catalog_local_items_without_files(self, real: Catalog, tmp_path: Path) -> None:
        expected_missing = {
            "ul-procyon",
            "davinci-resolve-studio",
            "copy-benchmark-tools",
        }
        missing = {
            i.id
            for i in real.items()
            if not real.availability(i, arch="x64", base_dir=tmp_path).available
        }
        assert missing == expected_missing | {"copy-benchmark-tools-arm"}

    def test_unknown_item_id(self) -> None:
        with pytest.raises(CatalogError):
            make_catalog([make_item("a")]).availability("ghost")


# ---------------------------------------------------------------- query API


@pytest.fixture
def small() -> Catalog:
    categories = [
        Category("second", "Druga", "", 1),
        Category("first", "Pierwsza", "", 0),
        Category("tools", "Narzędzia", "", 2, CategoryKind.TOOLS),
    ]
    items = [
        make_item("b", category="first"),
        make_item("a", category="first"),
        make_item("c", category="second"),
        make_item("t", category="tools", kind=ItemKind.ACTION),
    ]
    profiles = [Profile("p", "P", "", ("c", "a"), {"arm64": ("t", "a")})]
    return make_catalog(items, categories=categories, profiles=profiles)


class TestQueries:
    def test_categories_sorted_by_order(self, small: Catalog) -> None:
        assert [c.id for c in small.categories()] == ["first", "second", "tools"]
        assert [c.id for c in small.categories(CategoryKind.INSTALL)] == ["first", "second"]

    def test_category_lookup(self, small: Catalog) -> None:
        assert small.category("tools").kind == CategoryKind.TOOLS
        with pytest.raises(CatalogError, match="Nieznana kategoria"):
            small.category("ghost")

    def test_items(self, small: Catalog) -> None:
        assert [i.id for i in small.items()] == ["b", "a", "c", "t"]
        assert [i.id for i in small.items("first")] == ["b", "a"]
        with pytest.raises(CatalogError):
            small.items("ghost")

    def test_items_returns_a_copy(self, small: Catalog) -> None:
        small.items().clear()
        small.profiles().clear()
        assert len(small.items()) == 4 and len(small.profiles()) == 1

    def test_item_get_contains(self, small: Catalog) -> None:
        assert small.item("a").id == "a"
        assert small.get("ghost") is None
        assert "a" in small and "ghost" not in small and 5 not in small
        with pytest.raises(CatalogError, match="Nieznana pozycja"):
            small.item("ghost")

    def test_tools(self, small: Catalog) -> None:
        assert [i.id for i in small.tools()] == ["t"]
        assert small.is_tool(small.item("t"))
        assert not small.is_tool(small.item("a"))

    def test_in_catalog_order(self, small: Catalog) -> None:
        assert small.in_catalog_order(["t", "a", "c", "a", "b"]) == ["b", "a", "c", "t"]
        assert small.in_catalog_order([]) == []
        with pytest.raises(CatalogError):
            small.in_catalog_order(["a", "ghost"])

    def test_profile_items_for(self, small: Catalog) -> None:
        profile = small.profile("p")
        assert profile.items_for("x64") == ("c", "a")
        # Extras already present in the base list are not repeated.
        assert profile.items_for("arm64") == ("c", "a", "t")
        assert [i.id for i in small.profile_items("p", "arm64")] == ["c", "a", "t"]

    def test_profile_items_default_arch(self, small: Catalog, monkeypatch) -> None:
        monkeypatch.setenv("WINSTALLER_ARCH", "arm64")
        assert [i.id for i in small.profile_items("p")] == ["c", "a", "t"]
        monkeypatch.setenv("WINSTALLER_ARCH", "x64")
        assert [i.id for i in small.profile_items("p")] == ["c", "a"]

    def test_profile_unknown(self, small: Catalog) -> None:
        with pytest.raises(CatalogError, match="Nieznany profil"):
            small.profile("ghost")

    def test_profile_default_arch_items(self) -> None:
        assert Profile("p", "P", "", ("a",)).items_for("arm64") == ("a",)

    def test_item_properties(self) -> None:
        item = make_item("a", confirm="Na pewno?", arch=("x64",))
        assert item.destructive and item.supports("x64") and not item.supports("arm64")
        assert not make_item("b").destructive

    def test_module_exports(self) -> None:
        assert cat_mod.ItemKind("local_exe") is ItemKind.LOCAL_EXE
        assert CategoryKind("tools") is CategoryKind.TOOLS


# ------------------------------------------------------- stage 3 fixes


class TestStage3Validation:
    @pytest.mark.parametrize(
        "rel", ["C:setup.exe", r"c:sub\setup.exe", r"\\server\share\x.exe", r"a\..\..\x"]
    )
    def test_drive_unc_and_dotdot_variants_rejected(self, rel: str) -> None:
        entry = _item(kind="local_exe", winget_id=None, path=rel)
        _bad(minimal_data(items=[entry]), "musi być względna")

    @pytest.mark.parametrize("rel", ["setup.exe", r"procyon\procyon-setup.exe", "a/b.exe", "x..y"])
    def test_ordinary_relative_paths_accepted(self, rel: str) -> None:
        entry = _item(kind="local_exe", winget_id=None, path=rel)
        assert parse_catalog(minimal_data(items=[entry])).item("x").path == rel

    def test_non_table_entry_message_names_section(self) -> None:
        _bad(minimal_data(items=[_item(), "oops"]), r"Sekcja 'items': wpis #2 musi być tabelą")

    def test_duplicate_profile_items(self) -> None:
        profile = {"id": "p", "name": "P", "items": ["a", "a"]}
        _bad(minimal_data(profiles=[profile]), r"Profil 'p': zduplikowane pozycje w 'items': a")

    def test_duplicate_profile_arch_items(self) -> None:
        profile = {"id": "p", "name": "P", "items": [], "arch_items": {"x64": ["a", "a"]}}
        _bad(minimal_data(profiles=[profile]), r"zduplikowane pozycje w 'arch_items.x64'")

    def test_arch_items_type_error_names_the_field(self) -> None:
        profile = {"id": "p", "name": "P", "arch_items": {"arm64": "a"}}
        with pytest.raises(CatalogError) as exc:
            parse_catalog(minimal_data(profiles=[profile]))
        assert str(exc.value) == ("Profil 'p': pole 'arch_items.arm64' musi być listą tekstów.")

    @pytest.mark.parametrize("confirm", ["", "   "])
    def test_blank_confirm_is_not_destructive(self, confirm: str) -> None:
        item = parse_catalog(minimal_data(items=[_item(confirm=confirm)])).item("x")
        assert item.confirm is None and not item.destructive
        assert not make_item("y", confirm="").destructive

    def test_copy_dir_action_requires_folder_argument(self) -> None:
        entry = _item(kind="action", winget_id=None, action="copy_dir_to_desktop")
        _bad(minimal_data(items=[entry]), "wymaga nazwy folderu")

    @pytest.mark.parametrize("arg", ["..", r"..\Windows", r"C:\Users", r"\Windows", "D:x"])
    def test_copy_dir_action_argument_must_stay_inside(self, arg: str) -> None:
        entry = _item(kind="action", winget_id=None, action="copy_dir_to_desktop", args=[arg])
        _bad(minimal_data(items=[entry]), "musi być względna")

    def test_other_actions_args_are_not_paths(self) -> None:
        entry = _item(kind="action", winget_id=None, action="theme_dark", args=["..", "C:x"])
        assert parse_catalog(minimal_data(items=[entry])).item("x").args == ("..", "C:x")


class TestNaturalVersionGlob:
    def test_highest_version_wins_not_lexicographic(self, tmp_path: Path) -> None:
        for version in ("19.9", "19.10", "19.1.4", "9.99"):
            (tmp_path / f"DaVinci_Resolve_Studio_{version}_Windows.exe").write_bytes(b"")
        found = resolve_relative(tmp_path, "DaVinci_Resolve_Studio_*_Windows.exe")
        assert found == tmp_path / "DaVinci_Resolve_Studio_19.10_Windows.exe"

    def test_natural_key(self) -> None:
        names = ["a_19.10", "a_19.9", "a_2", "A_19.9b", "a_19"]
        assert sorted(names, key=cat_mod.natural_key) == [
            "a_2",
            "a_19",
            "a_19.9",
            "A_19.9b",
            "a_19.10",
        ]

    def test_glob_in_subdirectory_with_brackets(self, tmp_path: Path) -> None:
        base = tmp_path / "[x] Pobrane"
        (base / "sub").mkdir(parents=True)
        (base / "sub" / "Setup_1.exe").write_bytes(b"")
        (base / "sub" / "Setup_12.exe").write_bytes(b"")
        assert resolve_relative(base, r"sub\Setup_*.exe") == base / "sub" / "Setup_12.exe"
