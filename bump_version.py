#!/usr/bin/env python3
"""Bump the Winstaller version, add a CHANGELOG entry, commit and tag ``vX.Y.Z``.

Usage::

    python bump_version.py patch "Opis zmiany"
    python bump_version.py minor "Zmiana 1" "Zmiana 2" -m "Zmiana 3"
    python bump_version.py major "..." --dry-run        # show what would happen, change nothing

Then push the branch and the tag (the tag push starts the release workflow, which creates a
draft release with winstaller-x64.exe / winstaller-arm64.exe)::

    git push origin main
    git push origin vX.Y.Z

Source of truth for the version: ``winstaller/core/version.py`` (``return "X.Y.Z"``).
Only that file and ``CHANGELOG.md`` are committed. The script refuses to run with a dirty
working tree, on an unexpected branch, or when the tag already exists (it never deletes tags).

The pure functions (``bump``, ``read_version``, ``replace_version``, ``add_changelog_entry``,
``plan_release``) have no side effects and are meant to be unit tested.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION_FILE = Path("winstaller/core/version.py")
CHANGELOG_FILE = Path("CHANGELOG.md")
DEFAULT_BRANCH = "main"
PARTS = ("major", "minor", "patch")

_VERSION_LITERAL = re.compile(r'return "(\d+)\.(\d+)\.(\d+)"')
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_CHANGELOG_HEADING = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)


class BumpError(Exception):
    """User-facing error; the message is printed and the script exits with code 1."""


# ------------------------------------------------------------------ pure logic


def parse_semver(version: str) -> tuple[int, int, int]:
    m = _SEMVER.match(version.strip().removeprefix("v"))
    if not m:
        raise BumpError(f"Nieprawidłowa wersja: {version!r} (oczekiwano X.Y.Z)")
    major, minor, patch = (int(g) for g in m.groups())
    return major, minor, patch


def bump(version: str, part: str) -> str:
    """``bump("1.1.8", "minor") == "1.2.0"``."""
    major, minor, patch = parse_semver(version)
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise BumpError(f"Typ zmiany musi być jednym z: {', '.join(PARTS)} (podano {part!r})")


def read_version(source: str) -> str:
    """Version from the text of ``version.py``; exactly one ``return "X.Y.Z"`` is required."""
    matches = _VERSION_LITERAL.findall(source)
    if len(matches) != 1:
        raise BumpError(
            f'{VERSION_FILE}: oczekiwano dokładnie jednego `return "X.Y.Z"`, '
            f"znaleziono {len(matches)}"
        )
    return ".".join(matches[0])


def replace_version(source: str, new_version: str) -> str:
    parse_semver(new_version)
    read_version(source)  # validates there is exactly one literal
    return _VERSION_LITERAL.sub(f'return "{new_version}"', source, count=1)


def _version_key(label: str) -> tuple[int, tuple[int, int, int]]:
    """Sort key for changelog headings; ``Unreleased`` and unknown labels go first."""
    try:
        return (1, parse_semver(label))
    except BumpError:
        return (2, (0, 0, 0))


def add_changelog_entry(text: str, version: str, date: str, changes: Sequence[str]) -> str:
    """Return the changelog with a new ``## [version] - date`` section, newest first.

    The text before the first ``## [`` heading (title, description) is kept as is. Existing
    sections are kept verbatim but re-sorted newest first (the 1.x script appended entries at the
    end, so the file was out of order). ``## [Unreleased]`` stays on top.
    """
    parse_semver(version)
    cleaned = [c.strip() for c in changes if c.strip()]
    if not cleaned:
        raise BumpError("Podaj co najmniej jeden opis zmiany.")

    text = text.replace("\r\n", "\n")
    headings = list(_CHANGELOG_HEADING.finditer(text))
    preamble = text[: headings[0].start()] if headings else text
    sections: list[tuple[str, str]] = []
    for i, h in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        sections.append((h.group(1).strip(), text[h.start() : end].strip("\n")))

    if any(label == version for label, _ in sections):
        raise BumpError(f"{CHANGELOG_FILE} zawiera już wpis dla wersji {version}.")

    entry = f"## [{version}] - {date}\n" + "".join(f"- {c}\n" for c in cleaned)
    sections.append((version, entry.strip("\n")))
    # stable sort: equal keys (several "Unreleased"/unknown labels) keep their order
    sections.sort(key=lambda s: _version_key(s[0]), reverse=True)

    preamble = preamble.rstrip("\n") + "\n\n" if preamble.strip() else ""
    return preamble + "\n\n".join(body for _, body in sections) + "\n"


@dataclass(frozen=True)
class RepoState:
    """What ``plan_release`` needs to know about the repository (read by ``read_repo_state``)."""

    branch: str
    dirty_files: tuple[str, ...]
    existing_tags: frozenset[str]


@dataclass(frozen=True)
class ReleasePlan:
    current: str
    new: str
    tag: str
    files: dict[Path, tuple[str, str]]  # path -> (old text, new text)
    commands: list[list[str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def check_repo(
    state: RepoState, tag: str, *, allowed_branches: Sequence[str], allow_dirty: bool
) -> list[str]:
    """Return blocking problems (empty list = OK to release)."""
    problems: list[str] = []
    if state.dirty_files and not allow_dirty:
        listed = "\n  ".join(state.dirty_files[:20])
        problems.append(
            "Working tree nie jest czysty (zacommituj albo schowaj zmiany, "
            f"albo użyj --allow-dirty):\n  {listed}"
        )
    if allowed_branches and state.branch not in allowed_branches:
        problems.append(
            f"Bieżąca gałąź to {state.branch!r}, dozwolone: {', '.join(allowed_branches)} "
            "(zmień gałąź albo użyj --branch)."
        )
    if tag in state.existing_tags:
        problems.append(
            f"Tag {tag} już istnieje. Skrypt nie usuwa tagów; jeśli to celowe, usuń go ręcznie "
            f"(git tag -d {tag}; git push origin :refs/tags/{tag}) albo podnieś inną część wersji."
        )
    return problems


def plan_release(
    *,
    part: str | None,
    changes: Sequence[str],
    version_source: str,
    changelog_source: str,
    state: RepoState,
    date: str,
    set_version: str | None = None,
    allowed_branches: Sequence[str] = (DEFAULT_BRANCH,),
    allow_dirty: bool = False,
    tag_enabled: bool = True,
) -> ReleasePlan:
    """Compute everything the release would do. Raises ``BumpError`` on any problem."""
    current = read_version(version_source)
    if set_version is not None:
        new = ".".join(str(p) for p in parse_semver(set_version))
        if parse_semver(new) <= parse_semver(current):
            raise BumpError(f"Nowa wersja {new} musi być wyższa niż bieżąca {current}.")
    elif part is not None:
        new = bump(current, part)
    else:
        raise BumpError("Podaj typ zmiany (major/minor/patch) albo --set-version.")
    tag = f"v{new}"

    problems = check_repo(
        state,
        tag if tag_enabled else "",
        allowed_branches=allowed_branches,
        allow_dirty=allow_dirty,
    )
    if problems:
        raise BumpError("\n".join(problems))

    files = {
        VERSION_FILE: (version_source, replace_version(version_source, new)),
        CHANGELOG_FILE: (
            changelog_source,
            add_changelog_entry(changelog_source, new, date, changes),
        ),
    }
    paths = [p.as_posix() for p in files]
    commands = [
        ["git", "add", "--", *paths],
        # pathspec limits the commit to these files even if something else is staged
        ["git", "commit", "-m", f"release {new}", "--", *paths],
    ]
    if tag_enabled:
        commands.append(["git", "tag", "-a", tag, "-m", f"Wersja {new}"])
    warnings = []
    if state.dirty_files and allow_dirty:
        warnings.append("Working tree ma inne zmiany; nie zostaną zacommitowane.")
    return ReleasePlan(
        current=current, new=new, tag=tag, files=files, commands=commands, warnings=warnings
    )


def render_plan(plan: ReleasePlan) -> str:
    out = [f"Wersja: {plan.current} -> {plan.new}", ""]
    for path, (old, new) in plan.files.items():
        diff = difflib.unified_diff(
            old.replace("\r\n", "\n").splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path.as_posix()}",
            tofile=f"b/{path.as_posix()}",
            n=2,
        )
        out.append("".join(diff).rstrip("\n"))
        out.append("")
    out.append("Polecenia:")
    out.extend("  " + subprocess.list2cmdline(cmd) for cmd in plan.commands)
    out.extend(f"Uwaga: {w}" for w in plan.warnings)
    return "\n".join(out)


def push_hint(plan: ReleasePlan, branch: str, tagged: bool) -> str:
    lines = [
        "Aby wypchnąć (tag uruchamia workflow wydania i tworzy draft release):",
        f"  git push origin {branch}",
    ]
    if tagged:
        lines.append(f"  git push origin {plan.tag}")
    return "\n".join(lines)


# ------------------------------------------------------------------ git / IO


def _git(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except FileNotFoundError as exc:
        raise BumpError("Nie znaleziono polecenia git.") from exc
    except subprocess.CalledProcessError as exc:
        raise BumpError(f"git {' '.join(args)}: {exc.stderr.strip() or exc}") from exc
    return result.stdout


def read_repo_state(root: Path) -> RepoState:
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    dirty = tuple(line for line in _git(root, "status", "--porcelain").splitlines() if line)
    tags = frozenset(t.strip() for t in _git(root, "tag", "--list").splitlines() if t.strip())
    return RepoState(branch=branch, dirty_files=dirty, existing_tags=tags)


def remote_tag_exists(root: Path, tag: str, remote: str = "origin") -> bool:
    try:
        out = _git(root, "ls-remote", "--tags", remote, f"refs/tags/{tag}")
    except BumpError:
        return False  # offline or no remote: the local check still applies
    return bool(out.strip())


def apply_plan(root: Path, plan: ReleasePlan) -> None:
    for path, (_, new) in plan.files.items():
        (root / path).write_text(new, encoding="utf-8", newline="\n")
    for cmd in plan.commands:
        _git(root, *cmd[1:])


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bump_version.py",
        description="Podnosi wersję, dopisuje CHANGELOG, commituje i tworzy tag vX.Y.Z.",
    )
    parser.add_argument("part", nargs="?", choices=PARTS, help="która część wersji")
    parser.add_argument("changes", nargs="*", help="opisy zmian (każdy to osobny punkt)")
    parser.add_argument(
        "-m",
        "--change",
        action="append",
        default=[],
        dest="extra",
        help="kolejny opis zmiany (można powtarzać)",
    )
    parser.add_argument(
        "--set-version", help="ustaw wersję X.Y.Z zamiast podnosić (opisy wtedy przez -m)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="pokaż zmiany i polecenia, niczego nie zapisuj"
    )
    parser.add_argument(
        "--branch",
        action="append",
        dest="branches",
        help=f"dozwolona gałąź (domyślnie {DEFAULT_BRANCH}; można powtarzać)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="pozwól na inne niezacommitowane zmiany (nie trafią do commita)",
    )
    parser.add_argument("--no-tag", action="store_true", help="tylko commit, bez tagu")
    parser.add_argument("--date", help="data wpisu RRRR-MM-DD (domyślnie dziś)")
    parser.add_argument("--repo", type=Path, default=ROOT, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    args = _parse_args(argv)
    root: Path = args.repo.resolve()
    try:
        if args.part is None and args.set_version is None:
            raise BumpError('Użycie: bump_version.py [major|minor|patch] "opis zmian" ...')
        changes = [*args.changes, *args.extra]
        state = read_repo_state(root)
        version_source = (root / VERSION_FILE).read_text(encoding="utf-8")
        changelog_path = root / CHANGELOG_FILE
        changelog_source = (
            changelog_path.read_text(encoding="utf-8") if changelog_path.exists() else ""
        )
        plan = plan_release(
            part=args.part,
            changes=changes,
            version_source=version_source,
            changelog_source=changelog_source,
            state=state,
            date=args.date or dt.date.today().isoformat(),
            set_version=args.set_version,
            allowed_branches=args.branches or (DEFAULT_BRANCH,),
            allow_dirty=args.allow_dirty,
            tag_enabled=not args.no_tag,
        )
        if not args.no_tag and remote_tag_exists(root, plan.tag):
            raise BumpError(f"Tag {plan.tag} już istnieje na origin. Skrypt nie usuwa tagów.")
    except (BumpError, OSError) as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 1

    print(render_plan(plan))
    if args.dry_run:
        print("\n--dry-run: niczego nie zmieniono.")
        return 0
    try:
        apply_plan(root, plan)
    except (BumpError, OSError) as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        print("Sprawdź stan repozytorium (git status) przed ponowieniem.", file=sys.stderr)
        return 1
    tagged = "" if args.no_tag else f" i utworzono tag {plan.tag}"
    print(f"\nZacommitowano wersję {plan.new}{tagged}.")
    print(push_hint(plan, state.branch, tagged=not args.no_tag))
    return 0


if __name__ == "__main__":
    sys.exit(main())
