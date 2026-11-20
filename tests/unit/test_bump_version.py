"""bump_version.py: the pure release logic (no git is ever called here)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import bump_version as bv
from bump_version import BumpError, RepoState

VERSION_SOURCE = 'def get_version() -> str:\n    return "1.1.8"\n'

CHANGELOG = """# Changelog

Opis projektu.

## [1.1.7] - 2026-01-02
- Siódma

## [1.1.8] - 2026-02-03
- Ósma
  wieloliniowa

## [Unreleased]
- Coś w toku
"""

CLEAN = RepoState(branch="main", dirty_files=(), existing_tags=frozenset({"v1.1.8"}))


@pytest.fixture(autouse=True)
def no_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests must never reach git (the guard would block it anyway)."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("bump_version tests must not run git")

    monkeypatch.setattr(bv, "_git", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)


# ----------------------------------------------------------------- versions


@pytest.mark.parametrize(
    ("version", "part", "expected"),
    [
        ("1.1.8", "patch", "1.1.9"),
        ("1.1.8", "minor", "1.2.0"),
        ("1.1.8", "major", "2.0.0"),
        ("v1.9.9", "patch", "1.9.10"),
        (" 0.0.0 ", "minor", "0.1.0"),
    ],
)
def test_bump(version: str, part: str, expected: str) -> None:
    assert bv.bump(version, part) == expected


@pytest.mark.parametrize("version", ["1.2", "1.2.3.4", "x", "", "1.2.3-rc"])
def test_bump_rejects_bad_version(version: str) -> None:
    with pytest.raises(BumpError, match="Nieprawidłowa wersja"):
        bv.bump(version, "patch")


def test_bump_rejects_bad_part() -> None:
    with pytest.raises(BumpError, match="major, minor, patch"):
        bv.bump("1.0.0", "build")


def test_read_version() -> None:
    assert bv.read_version(VERSION_SOURCE) == "1.1.8"


@pytest.mark.parametrize(
    "source", ["def f():\n    return '1.0.0'\n", VERSION_SOURCE * 2, 'return "1.0"']
)
def test_read_version_requires_exactly_one_literal(source: str) -> None:
    with pytest.raises(BumpError, match="dokładnie jednego"):
        bv.read_version(source)


def test_read_version_of_the_real_file() -> None:
    real = (Path(bv.ROOT) / bv.VERSION_FILE).read_text(encoding="utf-8")
    from winstaller.core.version import get_version

    assert bv.read_version(real) == get_version()


def test_replace_version() -> None:
    out = bv.replace_version(VERSION_SOURCE, "2.0.0")
    assert out == 'def get_version() -> str:\n    return "2.0.0"\n'
    assert bv.read_version(out) == "2.0.0"


@pytest.mark.parametrize("new", ["2.0", "abc"])
def test_replace_version_rejects_bad_new(new: str) -> None:
    with pytest.raises(BumpError):
        bv.replace_version(VERSION_SOURCE, new)


def test_replace_version_rejects_ambiguous_source() -> None:
    with pytest.raises(BumpError):
        bv.replace_version(VERSION_SOURCE * 2, "2.0.0")


# ---------------------------------------------------------------- changelog


def _headings(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("## [")]


def test_add_changelog_entry_sorts_newest_first_and_keeps_bodies() -> None:
    out = bv.add_changelog_entry(CHANGELOG, "2.0.0", "2026-11-19", ["Nowy UI", "  ", "Testy"])
    assert out.startswith("# Changelog\n\nOpis projektu.\n\n## [Unreleased]")
    assert _headings(out) == [
        "## [Unreleased]",
        "## [2.0.0] - 2026-11-19",
        "## [1.1.8] - 2026-02-03",
        "## [1.1.7] - 2026-01-02",
    ]
    assert "## [2.0.0] - 2026-11-19\n- Nowy UI\n- Testy\n" in out
    assert "## [1.1.8] - 2026-02-03\n- Ósma\n  wieloliniowa\n" in out
    assert out.endswith("\n") and not out.endswith("\n\n")


def test_add_changelog_entry_normalizes_crlf() -> None:
    out = bv.add_changelog_entry(CHANGELOG.replace("\n", "\r\n"), "1.2.0", "2026-03-01", ["x"])
    assert "\r" not in out
    assert _headings(out)[1] == "## [1.2.0] - 2026-03-01"


def test_add_changelog_entry_to_empty_file() -> None:
    assert bv.add_changelog_entry("", "1.0.0", "2026-01-01", ["Start"]) == (
        "## [1.0.0] - 2026-01-01\n- Start\n"
    )


def test_add_changelog_entry_rejects_duplicates_and_empty_changes() -> None:
    with pytest.raises(BumpError, match="zawiera już wpis"):
        bv.add_changelog_entry(CHANGELOG, "1.1.8", "2026-01-01", ["x"])
    with pytest.raises(BumpError, match="co najmniej jeden"):
        bv.add_changelog_entry(CHANGELOG, "2.0.0", "2026-01-01", ["", "  "])
    with pytest.raises(BumpError, match="Nieprawidłowa wersja"):
        bv.add_changelog_entry(CHANGELOG, "2.0", "2026-01-01", ["x"])


def test_real_changelog_is_newest_first() -> None:
    text = (Path(bv.ROOT) / bv.CHANGELOG_FILE).read_text(encoding="utf-8")
    labels = [h.split("]")[0].removeprefix("## [") for h in _headings(text)]
    versions = [bv.parse_semver(label) for label in labels if label != "Unreleased"]
    assert versions == sorted(versions, reverse=True)
    assert len(set(versions)) == len(versions)


# --------------------------------------------------------------- repo checks


def test_check_repo_ok() -> None:
    assert bv.check_repo(CLEAN, "v1.2.0", allowed_branches=["main"], allow_dirty=False) == []


def test_check_repo_reports_every_problem() -> None:
    state = RepoState("2.0", (" M a.py", "?? b.txt"), frozenset({"v1.2.0"}))
    problems = bv.check_repo(state, "v1.2.0", allowed_branches=["main"], allow_dirty=False)
    assert len(problems) == 3
    assert "Working tree nie jest czysty" in problems[0] and "?? b.txt" in problems[0]
    assert "'2.0'" in problems[1]
    assert "Tag v1.2.0 już istnieje" in problems[2]


def test_check_repo_allow_dirty_and_any_branch() -> None:
    state = RepoState("feature", (" M a.py",), frozenset())
    assert bv.check_repo(state, "v9.0.0", allowed_branches=(), allow_dirty=True) == []


# --------------------------------------------------------------------- plan


def _plan(**kwargs: object) -> bv.ReleasePlan:
    args: dict[str, object] = {
        "part": "minor",
        "changes": ["Zmiana"],
        "version_source": VERSION_SOURCE,
        "changelog_source": CHANGELOG,
        "state": CLEAN,
        "date": "2026-11-19",
    }
    args.update(kwargs)
    return bv.plan_release(**args)  # type: ignore[arg-type]


def test_plan_release() -> None:
    plan = _plan()
    assert (plan.current, plan.new, plan.tag) == ("1.1.8", "1.2.0", "v1.2.0")
    assert plan.files[bv.VERSION_FILE] == (VERSION_SOURCE, VERSION_SOURCE.replace("1.1.8", "1.2.0"))
    old, new = plan.files[bv.CHANGELOG_FILE]
    assert old == CHANGELOG and "## [1.2.0] - 2026-11-19\n- Zmiana" in new
    paths = ["winstaller/core/version.py", "CHANGELOG.md"]
    assert plan.commands == [
        ["git", "add", "--", *paths],
        ["git", "commit", "-m", "release 1.2.0", "--", *paths],
        ["git", "tag", "-a", "v1.2.0", "-m", "Wersja 1.2.0"],
    ]
    assert plan.warnings == []
    rendered = bv.render_plan(plan)
    assert "Wersja: 1.1.8 -> 1.2.0" in rendered and '+    return "1.2.0"' in rendered


def test_plan_release_set_version_and_no_tag() -> None:
    plan = _plan(part=None, set_version="v2.0.0", tag_enabled=False)
    assert plan.new == "2.0.0"
    assert all(cmd[1] != "tag" for cmd in plan.commands)
    assert "git push origin v2.0.0" not in bv.push_hint(plan, "main", tagged=False)
    assert "git push origin v2.0.0" in bv.push_hint(plan, "main", tagged=True)


@pytest.mark.parametrize("version", ["1.1.8", "1.0.0"])
def test_plan_release_set_version_must_increase(version: str) -> None:
    with pytest.raises(BumpError, match="musi być wyższa"):
        _plan(part=None, set_version=version)


def test_plan_release_requires_part_or_version() -> None:
    with pytest.raises(BumpError, match="Podaj typ zmiany"):
        _plan(part=None)


def test_plan_release_blocked_by_repo_state() -> None:
    state = RepoState("2.0", (), frozenset({"v1.2.0"}))
    with pytest.raises(BumpError) as exc:
        _plan(state=state)
    assert "Bieżąca gałąź" in str(exc.value) and "Tag v1.2.0" in str(exc.value)


def test_plan_release_existing_tag_ignored_without_tagging() -> None:
    state = RepoState("main", (), frozenset({"v1.2.0"}))
    assert _plan(state=state, tag_enabled=False).new == "1.2.0"


def test_plan_release_dirty_allowed_warns() -> None:
    state = RepoState("main", (" M x",), frozenset())
    plan = _plan(state=state, allow_dirty=True, allowed_branches=["main", "2.0"])
    assert plan.warnings == ["Working tree ma inne zmiany; nie zostaną zacommitowane."]


def test_plan_release_without_changes_fails() -> None:
    with pytest.raises(BumpError, match="co najmniej jeden"):
        _plan(changes=[])
