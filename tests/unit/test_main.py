"""winstaller.__main__: flags, smoke check, elevation and the UI hand-off.

The real ``winstaller.ui`` is never imported: every test that reaches the UI
import puts a stand-in (or ``None``) into ``sys.modules`` first.
"""

from __future__ import annotations

import importlib.abc
import subprocess
import sys
import types

import pytest

from winstaller import __main__ as entry
from winstaller.core import platform as plat
from winstaller.core.version import get_version_info


@pytest.fixture
def elevation(monkeypatch: pytest.MonkeyPatch):
    state = types.SimpleNamespace(admin=False, relaunch_result=True, relaunch_calls=0)

    def relaunch(args=None) -> bool:
        state.relaunch_calls += 1
        return state.relaunch_result

    monkeypatch.setattr(plat, "is_admin", lambda: state.admin)
    monkeypatch.setattr(plat, "relaunch_as_admin", relaunch)
    return state


@pytest.fixture
def fake_ui(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict] = []
    package = types.ModuleType("winstaller.ui")
    app = types.ModuleType("winstaller.ui.app")

    def run(*, dry_run: bool) -> int:
        calls.append({"dry_run": dry_run})
        return 5

    app.run = run  # type: ignore[attr-defined]
    package.app = app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "winstaller.ui", package)
    monkeypatch.setitem(sys.modules, "winstaller.ui.app", app)
    return calls


@pytest.fixture
def no_ui(monkeypatch: pytest.MonkeyPatch) -> None:
    # None in sys.modules makes the import fail with ModuleNotFoundError, whether
    # or not winstaller/ui exists on disk.
    monkeypatch.setitem(sys.modules, "winstaller.ui", None)
    monkeypatch.setitem(sys.modules, "winstaller.ui.app", None)


def test_version(capsys, elevation) -> None:
    assert entry.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == get_version_info()
    assert elevation.relaunch_calls == 0


def test_smoke(capsys, elevation) -> None:
    assert entry.main(["--smoke"]) == 0
    out = capsys.readouterr().out
    assert get_version_info() in out
    assert "catalog: 8 kategorii" in out
    assert "core modules: 7 OK" in out
    assert elevation.relaunch_calls == 0


def test_smoke_failure_returns_1(capsys, monkeypatch, elevation) -> None:
    from winstaller.core import catalog

    def broken():
        raise catalog.CatalogError("zepsuty katalog")

    monkeypatch.setattr(catalog, "load_catalog", broken)
    assert entry.main(["--smoke"]) == 1
    assert "smoke FAILED: CatalogError: zepsuty katalog" in capsys.readouterr().err


def test_unknown_flag_exits_2(capsys) -> None:
    with pytest.raises(SystemExit) as info:
        entry.main(["--bogus"])
    assert info.value.code == 2


@pytest.mark.parametrize("flag", ["--dry-run", "--no-elevate"])
def test_missing_ui_exits_2_without_elevation(capsys, elevation, no_ui, flag) -> None:
    assert entry.main([flag]) == 2
    assert "winstaller.ui" in capsys.readouterr().err
    assert elevation.relaunch_calls == 0


@pytest.mark.parametrize(("flags", "dry_run"), [(["--dry-run"], True), (["--no-elevate"], False)])
def test_ui_run_contract(elevation, fake_ui, flags, dry_run) -> None:
    assert entry.main(flags) == 5
    assert fake_ui == [{"dry_run": dry_run}]
    assert elevation.relaunch_calls == 0


def test_not_admin_relaunches_and_exits(elevation, fake_ui) -> None:
    assert entry.main([]) == 0
    assert elevation.relaunch_calls == 1
    assert fake_ui == []


def test_relaunch_refused(capsys, elevation, fake_ui) -> None:
    elevation.relaunch_result = False
    assert entry.main([]) == 1
    assert "uprawnień administratora" in capsys.readouterr().err
    assert fake_ui == []


def test_admin_starts_ui_directly(elevation, fake_ui) -> None:
    elevation.admin = True
    assert entry.main([]) == 5
    assert elevation.relaunch_calls == 0
    assert fake_ui == [{"dry_run": False}]


def test_missing_ui_dependency_is_not_masked(monkeypatch, elevation) -> None:
    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "winstaller.ui" or name.startswith("winstaller.ui."):
                raise ModuleNotFoundError("No module named 'PySide6'", name="PySide6")
            return None

    monkeypatch.delitem(sys.modules, "winstaller.ui", raising=False)
    monkeypatch.delitem(sys.modules, "winstaller.ui.app", raising=False)
    monkeypatch.setattr(sys, "meta_path", [Finder(), *sys.meta_path])
    with pytest.raises(ModuleNotFoundError) as info:
        entry.main(["--no-elevate"])
    assert info.value.name == "PySide6"


def test_python_m_winstaller_smoke_subprocess() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "winstaller", "--smoke"], capture_output=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert get_version_info().encode() in result.stdout


def test_python_m_winstaller_version_subprocess() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "winstaller", "--version"], capture_output=True, timeout=60
    )
    assert result.returncode == 0
    assert result.stdout.decode().strip() == get_version_info()
