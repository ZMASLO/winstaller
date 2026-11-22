"""Everything the UI needs from the outside world, injectable for tests and screenshots.

:meth:`Services.create` wires the real core functions (or their dry-run simulations);
tests construct :class:`Services` directly or override single fields with
``dataclasses.replace``.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QWidget

from winstaller.core import platform as plat
from winstaller.core import updater, updates, winget
from winstaller.core.catalog import Catalog, load_catalog
from winstaller.core.platform import Arch
from winstaller.core.runner import DryRunConfig, RunnerListener, TaskRunner
from winstaller.core.updater import ReleaseAsset, ReleaseInfo, UpdateCancelled, UpdateCheck
from winstaller.core.updates import InstallItem, InstallProgress, WindowsUpdateInfo
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
FindInstallFn = Callable[[], InstallProgress | None]
ReadInstallFn = Callable[[Path], InstallProgress]
ClockFn = Callable[[], datetime]


def local_now() -> datetime:
    """Timezone-aware local time (the status file's timestamps are aware)."""
    return datetime.now().astimezone()


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
    #: Background Windows Update installation ("W tle" block). ``None`` = not followed:
    #: no detection at start-up / no status file reading after the queue item.
    find_background_install: FindInstallFn | None = None
    read_install_progress: ReadInstallFn | None = None
    #: Last system start ("recently finished" = finished after it); None = no limit.
    boot_time: ClockFn | None = None
    clock: ClockFn = local_now
    background_poll_ms: int = 1000
    #: "Otwórz log" is offered only for an existing file.
    path_exists: Callable[[Path], bool] = Path.exists

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
            simulation = SimulatedBackgroundInstall(config.delay)
            services.read_install_progress = simulation.read
            services.find_background_install = lambda: None
            services.boot_time = lambda: local_now() - timedelta(hours=1)
        else:
            services.ensure_winget = _real_ensure_winget
            services.check_update = lambda: updater.check(arch=arch)
            services.download_update = _real_download
            services.apply_update = lambda path: updater.apply(path, args=sys.argv[1:])
            services.update_dest = _update_destination
            services.cleanup_old = lambda: updater.cleanup_old(wait=10.0)
            services.find_background_install = updates.find_background_install
            services.read_install_progress = updates.read_install_progress
            services.boot_time = updates.system_boot_time
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


#: Simulated background installation: index of the update already downloaded and how
#: many steps each installation takes (the cumulative update is the long one).
_SIM_PREDOWNLOADED = frozenset({2})
_SIM_INSTALL_STEPS = (3, 1, 1, 1)
_SIM_STEP_FACTOR = 8  # one simulated step = 8 x WINSTALLER_DRY_RUN_DELAY


def simulated_progress(
    steps: float,
    *,
    start: datetime,
    path: Path | None = None,
    sample: Sequence[WindowsUpdateInfo] = updates.DRY_RUN_WINDOWS_UPDATES,
    step_seconds: float = 1.0,
    pid: int = 4242,
) -> InstallProgress:
    """State of the simulated background installation ``steps`` steps after ``start``:
    1 step search, 1 per download, :data:`_SIM_INSTALL_STEPS` per installation, then
    finished with a restart required. Pure (no files, no processes); also used by
    tools/screenshots.py for fixed states."""
    n = len(sample)
    statuses = ["downloaded" if i in _SIM_PREDOWNLOADED else "pending" for i in range(n)]
    downloads = [i for i in range(n) if i not in _SIM_PREDOWNLOADED]
    reboot = [u.optional and bool(u.kb) for u in sample]  # Preview LCU / .NET

    def at(offset: float) -> datetime:
        return start + timedelta(seconds=offset * step_seconds)

    def build(**fields: Any) -> InstallProgress:
        items = tuple(
            InstallItem(
                title=u.title,
                status=statuses[i],  # type: ignore[arg-type]
                id=u.id,
                revision=u.revision,
                hresult="0x00000000" if statuses[i] in updates.ITEM_DONE else None,
                reboot_required=reboot[i] and statuses[i] == "succeeded",
            )
            for i, u in enumerate(sample)
        )
        base: dict[str, Any] = {
            "state": "running",
            "total": n,
            "download_total": len(downloads),
            "items": items,
            "started": start,
            "log_path": path.with_suffix(".log") if path else updates.DRY_RUN_LOG_PATH,
            "status_path": path,
            "pid": pid,
            "alive": True,
        }
        base.update(fields)
        return InstallProgress(**base)

    if steps < 1:
        return build(phase="searching", phase_started=start, updated=start)
    clock = 1.0
    for k, i in enumerate(downloads, start=1):
        if steps < clock + 1:
            statuses[i] = "downloading"
            return build(
                phase="downloading",
                current=i + 1,
                download_current=k,
                current_title=sample[i].title,
                phase_started=at(1),
                step_started=at(clock),
                updated=at(clock),
            )
        statuses[i] = "downloaded"
        clock += 1
    install_start = clock
    for i, length in enumerate(_SIM_INSTALL_STEPS[:n]):
        if steps < clock + length:
            statuses[i] = "installing"
            return build(
                phase="installing",
                current=i + 1,
                download_current=len(downloads),
                current_title=sample[i].title,
                phase_started=at(install_start),
                step_started=at(clock),
                updated=at(clock),
            )
        statuses[i] = "succeeded"
        clock += length
    return build(
        state="finished",
        phase="finished",
        current=n,
        download_current=len(downloads),
        reboot_required=any(reboot),
        exit_code=0,
        phase_started=at(install_start),
        finished=at(clock),
        updated=at(clock),
        alive=False,
    )


class SimulatedBackgroundInstall:
    """Dry-run source for ``Services.read_install_progress``: the first read of a
    status path starts its timeline, one step lasting ``8 x delay`` seconds (delay 0:
    finished at once). Thread-safe (the monitor reads on worker threads)."""

    def __init__(self, delay: float, clock: ClockFn = local_now) -> None:
        self.step_seconds = max(0.0, delay) * _SIM_STEP_FACTOR
        self._clock = clock
        self._starts: dict[Path, datetime] = {}
        self._lock = threading.Lock()

    def read(self, path: Path) -> InstallProgress:
        now = self._clock()
        with self._lock:
            start = self._starts.setdefault(path, now)
        if self.step_seconds <= 0:
            steps = math.inf
        else:
            steps = (now - start).total_seconds() / self.step_seconds
        return simulated_progress(
            steps, start=start, path=path, step_seconds=self.step_seconds or 1.0
        )


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
