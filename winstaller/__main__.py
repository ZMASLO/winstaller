"""Entry point: ``python -m winstaller`` / ``winstaller.exe``.

Flags:
  --smoke       import core, load the catalog, print version/arch, exit 0 (CI/build check)
  --dry-run     start the UI with a simulated runner (nothing is installed)
  --version     print the version and exit
  --no-elevate  do not ask for administrator rights (dev convenience)

The UI contract is ``winstaller.ui.app.run(*, dry_run: bool) -> int``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="winstaller", description="Winstaller")
    parser.add_argument("--smoke", action="store_true", help="szybki test uruchomienia i wyjście")
    parser.add_argument("--dry-run", action="store_true", help="symulacja bez zmian w systemie")
    parser.add_argument("--version", action="store_true", help="wypisz wersję i zakończ")
    parser.add_argument("--no-elevate", action="store_true", help="nie proś o uprawnienia admina")
    # Diagnostics only (tests/perf): exit right after the first frame of the window.
    parser.add_argument("--exit-after-show", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def smoke() -> int:
    from winstaller.core import (
        actions,
        catalog,
        drivers,
        platform,
        runner,
        updater,
        version,
        winget,
    )

    cat = catalog.load_catalog()
    modules = (actions, catalog, drivers, platform, runner, updater, version, winget)
    print(version.get_version_info())
    print(f"arch: {platform.get_arch()}")
    print(f"frozen: {platform.is_frozen()}")
    print(
        f"catalog: {len(cat.categories())} kategorii, {len(cat.items())} pozycji, "
        f"{len(cat.profiles())} profili"
    )
    print(f"core modules: {len(modules)} OK")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    args = _parse_args(argv)

    if args.version:
        from winstaller.core.version import get_version_info

        print(get_version_info())
        return 0

    if args.smoke:
        try:
            return smoke()
        except Exception as exc:
            print(f"smoke FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

    from winstaller.core import platform

    if not args.dry_run and not args.no_elevate and not platform.is_admin():
        if platform.relaunch_as_admin():
            return 0
        print("Uruchomienie wymaga uprawnień administratora.", file=sys.stderr)
        return 1

    try:
        from winstaller.ui.app import run
    except ModuleNotFoundError as exc:
        if exc.name and (exc.name == "winstaller.ui" or exc.name.startswith("winstaller.ui.")):
            print(
                "Interfejs graficzny (winstaller.ui) nie jest jeszcze dostępny. "
                "Użyj --smoke, aby sprawdzić rdzeń.",
                file=sys.stderr,
            )
            return 2
        raise
    if args.exit_after_show:
        return int(run(dry_run=args.dry_run, exit_after_show=True))
    return int(run(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
