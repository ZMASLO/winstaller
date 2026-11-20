"""Everything the UI needs from the outside world, injectable for tests and screenshots.

:meth:`Services.create` wires the real core functions (or their dry-run simulations);
tests construct :class:`Services` directly or override single fields with
``dataclasses.replace``.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QWidget

from winstaller.core import platform as plat
from winstaller.core import updater, winget
from winstaller.core.catalog import Catalog, load_catalog
from winstaller.core.platform import Arch
from winstaller.core.runner import DryRunConfig, RunnerListener, TaskRunner
from winstaller.core.updater import ReleaseAsset, ReleaseInfo, UpdateCancelled, UpdateCheck
from winstaller.core.version import get_version
from winstaller.core.winget import WingetResult

ENV_FAKE_UPDATE = "WINSTALLER_FAKE_UPDATE"  # dry-run only: pretend this version is released

LineCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int | None], None]
RunnerFactory = Callable[[RunnerListener], TaskRunner]
CheckUpdateFn = Callable[[], UpdateCheck]
DownloadFn = Callable[[ReleaseAsset, Path, ProgressCallback, threading.Event], Path]
ApplyFn = Callable[[Path], bool]
EnsureWingetFn = Callable[[LineCallback, LineCallback, threading.Event], WingetResult]


@dataclass(frozen=True, slots=True)
class ConfirmRequest:
    """A yes/no question; ``details`` are (title, text) pairs listed in the dialog."""

    title: str
    message: str
    details: tuple[tuple[str, str], ...] = ()
    accept_text: str = "Kontynuuj"
    reject_text: str = "Anuluj"


ConfirmFn = Callable[[QWidget, ConfirmRequest], bool]


@dataclass(slots=True)
class Services:
    catalog: Catalog
    runner_factory: RunnerFactory  # install queue
    tool_runner_factory: RunnerFactory  # one-off tools (Narzędzia)
    dry_run: bool = False
    arch: Arch = "x64"
    app_dir: Path = field(default_factory=plat.app_dir)
    version: str = field(default_factory=get_version)
    #: None disables update checks entirely (dry-run without WINSTALLER_FAKE_UPDATE).
    check_update: CheckUpdateFn | None = None
    download_update: DownloadFn | None = None
    #: None = simulated update: download only, never swap the executable.
    apply_update: ApplyFn | None = None
    update_dest: Callable[[ReleaseAsset], Path] | None = None
    cleanup_old: Callable[[], Any] | None = None
    winget_available: Callable[[], bool] | None = None
    ensure_winget: EnsureWingetFn | None = None
    open_path: Callable[[Path], Any] | None = None
    open_url: Callable[[str], Any] | None = None
    #: None = the built-in modal confirmation dialog.
    confirm: ConfirmFn | None = None
    auto_check_updates: bool = True
    check_winget_on_start: bool = True

    @classmethod
    def create(
        cls,
        *,
        dry_run: bool,
        catalog: Catalog | None = None,
        dry_run_config: DryRunConfig | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Services:
        """Real wiring (``dry_run=False``) or the safe simulation (``dry_run=True``)."""
        env = os.environ if environ is None else environ
        cat = catalog or load_catalog()
        arch = plat.get_arch()
        app_dir = plat.app_dir()
        config = dry_run_config or DryRunConfig.from_env(env)

        def runner_factory(listener: RunnerListener) -> TaskRunner:
            return TaskRunner(
                cat,
                dry_run=dry_run,
                listener=listener,
                dry_run_config=config,
                arch=arch,
                app_dir=app_dir,
            )

        def tool_runner_factory(listener: RunnerListener) -> TaskRunner:
            return TaskRunner(
                cat,
                dry_run=dry_run,
                listener=listener,
                dry_run_config=config,
                arch=arch,
                app_dir=app_dir,
                preserve_order=True,
            )

        services = cls(
            catalog=cat,
            runner_factory=runner_factory,
            tool_runner_factory=tool_runner_factory,
            dry_run=dry_run,
            arch=arch,
            app_dir=app_dir,
            winget_available=winget.is_available,
        )
        if dry_run:
            services.ensure_winget = _fake_ensure_winget(config.delay)
            fake_version = env.get(ENV_FAKE_UPDATE, "").strip()
            if fake_version:
                services.check_update = _fake_check(fake_version, arch)
                services.download_update = _fake_download(config.delay)
                services.update_dest = lambda asset: Path(tempfile.gettempdir()) / asset.name
            services.cleanup_old = lambda: True
        else:
            services.ensure_winget = _real_ensure_winget
            services.check_update = lambda: updater.check(arch=arch)
            services.download_update = _real_download
            services.apply_update = lambda path: updater.apply(path, args=sys.argv[1:])
            services.update_dest = _update_destination
            services.cleanup_old = lambda: updater.cleanup_old(wait=10.0)
        return services


# ------------------------------------------------------------------ real wiring


def _real_ensure_winget(
    on_line: LineCallback, on_replace: LineCallback, cancel: threading.Event
) -> WingetResult:
    return winget.ensure_installed(on_line=on_line, on_replace_last=on_replace, cancel_event=cancel)


def _real_download(
    asset: ReleaseAsset, dest: Path, progress: ProgressCallback, cancel: threading.Event
) -> Path:
    return updater.download(asset, dest, progress_cb=progress, cancel_event=cancel)


def _update_destination(asset: ReleaseAsset) -> Path:
    """Next to the exe when possible (the tool often runs from a USB stick): the final
    swap is then a plain rename. ``updater.apply`` also handles a download on another
    volume (it copies it next to the exe first), so %TEMP% is a valid fallback."""
    if plat.is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        probe = exe_dir / ".winstaller-write-test"
        try:
            probe.write_bytes(b"")
            probe.unlink()
            return exe_dir / f"{Path(sys.executable).stem}.update.exe"
        except OSError:
            pass
    folder = Path(tempfile.gettempdir()) / "winstaller-update"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / asset.name


# ------------------------------------------------------------- dry-run fakes


def _fake_check(version: str, arch: Arch) -> CheckUpdateFn:
    def check() -> UpdateCheck:
        current = get_version()
        try:
            available = updater.is_newer(version, current)
        except ValueError as exc:
            raise updater.UpdateError(str(exc)) from exc
        asset = ReleaseAsset(
            name=updater.asset_name_for(arch),
            url="dry-run://winstaller",
            size=48 * 1024 * 1024,
        )
        release = ReleaseInfo(
            tag=f"v{version}",
            version=version,
            name=f"Winstaller {version}",
            notes="Symulowane wydanie (dry-run).",
            html_url=f"https://github.com/{updater.GITHUB_REPO}/releases",
            assets=(asset,),
        )
        return UpdateCheck(current, version, available, asset, release)

    return check


def _fake_download(delay: float) -> DownloadFn:
    def download(
        asset: ReleaseAsset, dest: Path, progress: ProgressCallback, cancel: threading.Event
    ) -> Path:
        steps = 60
        step_delay = min(delay, 0.05) if delay > 0 else 0.0
        for n in range(1, steps + 1):
            if cancel.is_set() or (step_delay and cancel.wait(step_delay)):
                raise UpdateCancelled("Pobieranie aktualizacji przerwane.")
            progress(asset.size * n // steps, asset.size)
        return dest

    return download


def _fake_ensure_winget(delay: float) -> EnsureWingetFn:
    def ensure(
        on_line: LineCallback, on_replace: LineCallback, cancel: threading.Event
    ) -> WingetResult:
        for text in (
            "[dry-run] Rejestrowanie App Installer (winget)...",
            "[dry-run] Add-AppxPackage -RegisterByFamilyName (symulacja)",
        ):
            on_line(text)
            if delay and cancel.wait(delay * 4):
                return WingetResult(-1, False, True, "Przerwano przez użytkownika.")
        return WingetResult(0, True, False, "Zainstalowano winget (dry-run).")

    return ensure
