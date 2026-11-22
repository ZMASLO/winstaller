"""System actions and item execution.

Every side effect goes through the small module-level functions in the
"system layer" section (``run_command``, ``start_detached``, ``set_user_dword``,
...), so tests can ``monkeypatch`` them instead of touching the real system.

Actions take an :class:`ActionContext` and return an :class:`ActionOutcome`
(or a plain success message / ``None``). Failures are reported by raising
:class:`ActionError` with a Polish message.
"""

from __future__ import annotations

import base64
import ctypes
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from winstaller.core import drivers, updates, winget
from winstaller.core import platform as plat
from winstaller.core.catalog import Item, ItemKind, is_relative_inside, resolve_relative
from winstaller.core.platform import Arch


class ActionError(Exception):
    """An action failed; ``str(exc)`` is a user-facing Polish message."""


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    ok: bool
    message: str
    cancelled: bool = False
    #: File produced by the action (e.g. the battery report), if any.
    artifact: Path | None = None


@dataclass(slots=True)
class ActionContext:
    item: Item
    log: Callable[[str], None]
    replace_last: Callable[[str], None]
    cancel_event: threading.Event = field(default_factory=threading.Event)
    app_dir: Path = field(default_factory=plat.app_dir)
    arch: Arch = field(default_factory=plat.get_arch)

    @property
    def args(self) -> tuple[str, ...]:
        return self.item.args


ActionFunc = Callable[[ActionContext], "ActionOutcome | str | None"]

# ------------------------------------------------------------ system layer
# Thin wrappers around the OS. Tests replace these with monkeypatch.

CREATE_NO_WINDOW = winget.CREATE_NO_WINDOW
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def run_command(
    args: Sequence[str], *, timeout: float | None = 120
) -> subprocess.CompletedProcess[bytes]:
    """Run a short command without a console window, capturing output."""
    return subprocess.run(
        list(args), capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW
    )


def stream_command(
    ctx: ActionContext,
    args: Sequence[str],
    *,
    on_line: Callable[[str], None] | None = None,
) -> winget.ProcessOutcome:
    """Run a long command streaming its output (default: into the context log)."""
    return winget.stream_process(
        args,
        on_line=on_line or ctx.log,
        on_replace_last=ctx.replace_last,
        cancel_event=ctx.cancel_event,
    )


def start_detached(args: Sequence[str]) -> None:
    """Start a GUI program that outlives Winstaller."""
    subprocess.Popen(
        list(args),
        close_fds=True,
        creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP,
    )


def set_user_dword(key_path: str, name: str, value: int) -> None:
    """Create/overwrite a REG_DWORD under HKEY_CURRENT_USER."""
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)


def broadcast_setting_change(area: str) -> None:
    """Notify running apps (Explorer, taskbar) that a setting changed."""
    hwnd_broadcast = 0xFFFF
    wm_settingchange = 0x001A
    smto_abortifhung = 0x0002
    try:
        result = ctypes.c_size_t()
        ctypes.windll.user32.SendMessageTimeoutW(
            hwnd_broadcast,
            wm_settingchange,
            0,
            ctypes.c_wchar_p(area),
            smto_abortifhung,
            1000,
            ctypes.byref(result),
        )
    except (OSError, AttributeError):
        pass


def desktop_dir() -> Path:
    return plat.desktop_dir()


def is_frozen() -> bool:
    return plat.is_frozen()


def current_executable() -> Path:
    return Path(sys.executable)


# ------------------------------------------------------------------ helpers


def _decode(raw: bytes | None) -> str:
    return winget.decode_console_bytes(raw or b"").strip()


def _powershell_encoded(script: str) -> list[str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [
        plat.system32_path(plat.POWERSHELL_EXE),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-EncodedCommand",
        encoded,
    ]


# ------------------------------------------------------------------- theme

THEME_REG_PATH = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize"
THEME_VALUES = ("AppsUseLightTheme", "SystemUsesLightTheme")


def _set_theme(light: bool) -> str:
    try:
        for name in THEME_VALUES:
            set_user_dword(THEME_REG_PATH, name, 1 if light else 0)
    except OSError as exc:
        raise ActionError(f"Nie udało się zmienić motywu w rejestrze: {exc}") from exc
    broadcast_setting_change("ImmersiveColorSet")
    return "Ustawiono jasny motyw." if light else "Ustawiono ciemny motyw."


def theme_dark(ctx: ActionContext) -> str:
    return _set_theme(light=False)


def theme_light(ctx: ActionContext) -> str:
    return _set_theme(light=True)


# ------------------------------------------------------------------ bloat

#: Appx name patterns (matched as ``*pattern*``) removed by ``remove_bloat``.
BLOATWARE: tuple[str, ...] = (
    # Unnecessary built-in apps
    "Microsoft.3DBuilder",
    "Microsoft.Microsoft3DViewer",
    "Microsoft.AppConnector",
    "Microsoft.BingFinance",
    "Microsoft.BingNews",
    "Microsoft.BingSports",
    "Microsoft.BingTranslator",
    "Microsoft.BingWeather",
    "Microsoft.BingFoodAndDrink",
    "Microsoft.BingHealthAndFitness",
    "Microsoft.BingTravel",
    "Microsoft.MinecraftUWP",
    "Microsoft.GetHelp",
    "Microsoft.Getstarted",
    "Microsoft.Messaging",
    "Microsoft.MicrosoftSolitaireCollection",
    "Microsoft.NetworkSpeedTest",
    "Microsoft.News",
    "Microsoft.Office.Lens",
    "Microsoft.Office.Sway",
    "Microsoft.Office.OneNote",
    "Microsoft.OneConnect",
    "Microsoft.People",
    "Microsoft.Print3D",
    "Microsoft.SkypeApp",
    "Microsoft.Wallet",
    "Microsoft.Whiteboard",
    "microsoft.windowscommunicationsapps",
    "Microsoft.WindowsFeedbackHub",
    "Microsoft.WindowsMaps",
    "Microsoft.WindowsPhone",
    "Microsoft.WindowsSoundRecorder",
    "Microsoft.ConnectivityStore",
    "Microsoft.CommsPhone",
    "Microsoft.ZuneMusic",
    "Microsoft.ZuneVideo",
    "Microsoft.YourPhone",
    "Microsoft.MicrosoftOfficeHub",
    # Sponsored apps
    "EclipseManager",
    "ActiproSoftwareLLC",
    "AdobeSystemsIncorporated.AdobePhotoshopExpress",
    "Duolingo-LearnLanguagesforFree",
    "PandoraMediaInc",
    "CandyCrush",
    "BubbleWitch3Saga",
    "Wunderlist",
    "Flipboard",
    "Twitter",
    "Facebook",
    "Royal Revolt",
    "Sway",
    "Speed Test",
    "Dolby",
    "Viber",
    "ACGMediaPlayer",
    "Netflix",
    "OneCalendar",
    "LinkedInforWindows",
    "HiddenCityMysteryofShadows",
    "Hulu",
    "HiddenCity",
    "AdobePhotoshopExpress",
    "HotspotShieldFreeVPN",
    "Whatsapp",
    "Disney",
    "Instagram",
    "EPSN",
    "Prime Video",
    "Microsoft.Advertising.Xaml",
)


def normalized_bloat_patterns(patterns: Sequence[str] = BLOATWARE) -> list[str]:
    """Strip wildcards, drop duplicates and patterns already covered by a shorter one.

    ``*HiddenCity*`` matches everything ``*HiddenCityMysteryofShadows*`` does, so the
    longer one is redundant. Comparison is case-insensitive; order is kept.
    """
    unique: dict[str, str] = {}
    for raw in patterns:
        pattern = raw.strip().strip("*")
        if pattern:
            unique.setdefault(pattern.lower(), pattern)
    keys = list(unique)
    return [unique[k] for k in keys if not any(other != k and other in k for other in keys)]


def build_remove_bloat_script(patterns: Sequence[str]) -> str:
    """One PowerShell script removing all patterns, reporting one line per result.

    Output lines: ``OK<TAB>pattern<TAB>package``, ``ERR<TAB>pattern<TAB>package<TAB>msg``,
    ``NONE<TAB>pattern``.
    """
    quoted = ",".join("'" + p.replace("'", "''") + "'" for p in patterns)
    return (
        winget.POWERSHELL_UTF8_PREAMBLE + "\n"
        f"$patterns = @({quoted})\n"
        "foreach ($p in $patterns) {\n"
        '  $pkgs = @(Get-AppxPackage -Name "*$p*" -ErrorAction SilentlyContinue)\n'
        '  if ($pkgs.Count -eq 0) { Write-Output "NONE`t$p"; continue }\n'
        "  foreach ($pkg in $pkgs) {\n"
        "    try {\n"
        "      Remove-AppxPackage -Package $pkg.PackageFullName -ErrorAction Stop\n"
        '      Write-Output "OK`t$p`t$($pkg.Name)"\n'
        "    } catch {\n"
        '      $m = ($_.Exception.Message -replace "\\s+", " ")\n'
        '      Write-Output "ERR`t$p`t$($pkg.Name)`t$m"\n'
        "    }\n"
        "  }\n"
        "}\n"
    )


def remove_bloat(ctx: ActionContext) -> ActionOutcome:
    patterns = normalized_bloat_patterns()
    ctx.log(f"Usuwanie zbędnych aplikacji ({len(patterns)} wzorców)...")
    removed: list[str] = []
    errors: list[str] = []
    not_found = 0

    def on_line(line: str) -> None:
        nonlocal not_found
        parts = line.split("\t")
        tag = parts[0]
        if tag == "OK" and len(parts) >= 3:
            removed.append(parts[2])
            ctx.log(f"Usunięto: {parts[2]}")
        elif tag == "ERR" and len(parts) >= 3:
            errors.append(parts[2])
            reason = parts[3] if len(parts) > 3 else ""
            ctx.log(f"Błąd usuwania {parts[2]}: {reason}".rstrip(": "))
        elif tag == "NONE" and len(parts) >= 2:
            not_found += 1
            ctx.log(f"Nie znaleziono: {parts[1]}")
        else:
            ctx.log(line)

    try:
        outcome = stream_command(
            ctx, _powershell_encoded(build_remove_bloat_script(patterns)), on_line=on_line
        )
    except OSError as exc:
        raise ActionError(f"Nie udało się uruchomić PowerShell: {exc}") from exc
    if outcome.cancelled:
        return ActionOutcome(False, "Przerwano przez użytkownika.", cancelled=True)
    if outcome.returncode != 0:
        raise ActionError(f"PowerShell zakończył się błędem (kod {outcome.returncode}).")
    summary = f"Usunięto {len(removed)} pakietów, nie znaleziono {not_found} wzorców"
    if errors:
        summary += f", błędy: {len(errors)} (szczegóły w dzienniku)"
    return ActionOutcome(True, summary + ".")


# ---------------------------------------------------------------- copying


def copy_dir_to_desktop(ctx: ActionContext) -> str:
    """Copy ``<app_dir>/<args[0]>`` to the desktop, merging into an existing folder."""
    if not ctx.args:
        raise ActionError("Brak nazwy folderu do skopiowania.")
    name = ctx.args[0]
    if not is_relative_inside(name):
        raise ActionError(f"Nieprawidłowa nazwa folderu do skopiowania: {name}")
    source = ctx.app_dir / name
    if not source.is_dir():
        raise ActionError(f"Folder {name} nie istnieje w {ctx.app_dir}.")
    target = desktop_dir() / name
    ctx.log(f"Kopiowanie {source} -> {target}")
    try:
        shutil.copytree(source, target, dirs_exist_ok=True)
    except (OSError, shutil.Error) as exc:
        raise ActionError(f"Nie udało się skopiować folderu {name}: {exc}") from exc
    return f"Skopiowano folder {name} na pulpit."


def copy_self_to_desktop(ctx: ActionContext) -> str:
    if not is_frozen():
        raise ActionError(
            "Kopiowanie winstaller.exe działa tylko w zbudowanej wersji programu (.exe)."
        )
    source = current_executable()
    target = desktop_dir() / source.name
    if target.exists() and target.resolve() == source.resolve():
        return "Program już jest uruchomiony z pulpitu."
    ctx.log(f"Kopiowanie {source} -> {target}")
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        raise ActionError(f"Nie udało się skopiować {source.name}: {exc}") from exc
    return f"Skopiowano {source.name} na pulpit."


# ------------------------------------------------------------ restarts


def _shutdown(args: list[str], what: str) -> str:
    try:
        result = run_command([plat.system32_path("shutdown.exe"), *args], timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ActionError(f"Nie udało się uruchomić polecenia shutdown: {exc}") from exc
    if result.returncode != 0:
        detail = _decode(result.stderr) or _decode(result.stdout)
        raise ActionError(f"Nie udało się zlecić restartu {what}: {detail}".rstrip(": "))
    return f"Zlecono restart {what}."


def reboot_to_bios(ctx: ActionContext) -> str:
    return _shutdown(["/r", "/fw", "/t", "1"], "do BIOS/UEFI")


def reboot_advanced(ctx: ActionContext) -> str:
    return _shutdown(["/r", "/o", "/t", "1"], "do opcji zaawansowanych")


# ------------------------------------------------------------ battery


BATTERY_REPORT_NAME = "battery-report.html"


def battery_report(ctx: ActionContext) -> ActionOutcome:
    """Generate ``powercfg /batteryreport`` on the desktop (fallback: app_dir)."""
    last_error = ""
    for directory in (desktop_dir(), ctx.app_dir):
        target = directory / BATTERY_REPORT_NAME
        try:
            result = run_command(
                [plat.system32_path("powercfg.exe"), "/batteryreport", "/output", str(target)],
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ActionError(f"Nie udało się uruchomić powercfg: {exc}") from exc
        if result.returncode == 0 and target.exists():
            return ActionOutcome(True, f"Zapisano raport baterii: {target}", artifact=target)
        last_error = _decode(result.stdout) or _decode(result.stderr)
    raise ActionError(f"Nie udało się wygenerować raportu baterii. {last_error}".strip())


# ------------------------------------------------------- local installers


def run_local_installer(ctx: ActionContext, relative: str, args: Sequence[str]) -> ActionOutcome:
    """Run an installer from ``app_dir`` (``relative`` may contain wildcards)."""
    exe = resolve_relative(ctx.app_dir, relative)
    if exe is None:
        raise ActionError(f"Brak pliku instalatora {relative} w {ctx.app_dir}.")
    ctx.log(f"Uruchamianie instalatora {exe.name}...")
    try:
        outcome = stream_command(ctx, [str(exe), *args])
    except OSError as exc:
        raise ActionError(f"Nie udało się uruchomić {exe.name}: {exc}") from exc
    if outcome.cancelled:
        return ActionOutcome(False, "Przerwano przez użytkownika.", cancelled=True)
    if outcome.returncode != 0:
        raise ActionError(f"Instalator {exe.name} zakończył się kodem {outcome.returncode}.")
    return ActionOutcome(True, f"Zainstalowano z {exe.name}.")


def launch_after_install(ctx: ActionContext, path: str) -> None:
    """Start the installed program if its exe exists; only warn when it does not."""
    exe = Path(os.path.expandvars(path))
    if not exe.is_file():
        ctx.log(f"Ostrzeżenie: nie znaleziono {exe}, pomijam uruchomienie.")
        return
    try:
        start_detached([str(exe)])
        ctx.log(f"Uruchomiono {exe.name}.")
    except OSError as exc:
        ctx.log(f"Ostrzeżenie: nie udało się uruchomić {exe.name}: {exc}")


# ------------------------------------------------------------ GPU drivers


def install_driver(ctx: ActionContext, vendor_id: str | None) -> ActionOutcome:
    """Newest GPU driver of ``vendor_id`` from the vendor (see core/drivers.py)."""
    try:
        result = drivers.install_latest(
            vendor_id or "",
            log=ctx.log,
            replace_last=ctx.replace_last,
            cancel_event=ctx.cancel_event,
            app_dir=ctx.app_dir,
        )
    except drivers.DriverError as exc:
        raise ActionError(exc.user_message()) from exc
    return ActionOutcome(result.ok, result.message, cancelled=result.cancelled)


# ------------------------------------------------------------ updates

_CANCELLED_MESSAGE = "Przerwano przez użytkownika."


def windows_update(ctx: ActionContext) -> ActionOutcome:
    """Search all Windows updates (optional/Preview and drivers included) and start
    their download + install in a detached background process (see core/updates.py).
    The start is confirmed through the process's status file; the installation itself
    is never waited for and the machine is never restarted."""
    try:
        if updates.background_install_running():
            return _wu_already_running(ctx, updates.running_install())
    except updates.UpdatesError as exc:
        raise ActionError(exc.message) from exc
    ctx.log(
        "Wyszukiwanie aktualizacji Windows (także opcjonalnych, Preview i sterowników) — "
        "może to potrwać kilka minut..."
    )
    try:
        scan = updates.scan_windows_updates(cancel_event=ctx.cancel_event)
    except updates.UpdatesCancelled:
        return ActionOutcome(False, _CANCELLED_MESSAGE, cancelled=True)
    except updates.UpdatesError as exc:
        raise ActionError(exc.message) from exc
    for warning in scan.warnings:
        ctx.log(warning)
    if not scan.updates:
        if scan.skipped:
            raise ActionError(
                "Nie udało się odczytać żadnej ze znalezionych aktualizacji "
                f"({scan.skipped} pominiętych) — zainstaluj je w Ustawienia → Windows Update."
            )
        return ActionOutcome(True, "System jest aktualny — brak aktualizacji do zainstalowania.")
    took = f" ({scan.seconds:.0f} s)" if scan.seconds > 0 else ""
    ctx.log(f"Wyszukiwanie zakończone{took}. Znalezione aktualizacje:")
    for update in scan.updates:
        ctx.log(f"  {update.label()}")
    selected = updates.select_updates(scan.updates)
    if not selected:
        return ActionOutcome(
            True,
            "Wszystkie znalezione aktualizacje są wyłączone z automatycznej instalacji "
            "(ustawienia pomijania); zainstaluj je w Ustawienia → Windows Update.",
        )
    if scan.reboot_pending:
        ctx.log(
            "Ostrzeżenie: Windows czeka na ponowne uruchomienie po wcześniejszej instalacji — "
            "część aktualizacji może zostać zainstalowana dopiero po restarcie."
        )
    if scan.is_admin is False:
        raise ActionError(
            "Instalacja aktualizacji Windows wymaga uprawnień administratora — uruchom "
            "Winstaller jako administrator (lista znalezionych aktualizacji jest w dzienniku)."
        )
    if ctx.cancel_event.is_set():
        return ActionOutcome(False, _CANCELLED_MESSAGE, cancelled=True)
    try:
        started = updates.start_windows_update_install(update_ids=[u.id for u in selected])
    except updates.UpdatesError as exc:
        raise ActionError(exc.message) from exc
    if started.already_running:
        return _wu_already_running(ctx, started)
    if started.install_all:
        ctx.log(
            "Ostrzeżenie: lista aktualizacji jest zbyt długa, aby przekazać ją procesowi w tle — "
            "zostaną zainstalowane wszystkie aktualizacje, które znajdzie."
        )
    try:
        started = updates.verify_background_start(started, cancel_event=ctx.cancel_event)
    except updates.UpdatesCancelled as exc:
        return ActionOutcome(False, exc.message, cancelled=True, artifact=started.log_path)
    except updates.UpdatesError as exc:
        raise ActionError(exc.message) from exc
    ctx.log(f"Uruchomiono pobieranie i instalację w tle (PID {started.pid}).")
    ctx.log(f"Log: {started.log_path}")
    if started.status_path is not None:
        ctx.log(f"Plik statusu: {started.status_path}")
    optional = sum(1 for u in selected if u.optional)
    found = f"Znaleziono {len(selected)} aktualizacji (w tym {optional} opcjonalnych)"
    if started.finished:
        return ActionOutcome(
            True,
            f"{found}; proces instalacji zakończył się od razu bez błędu (kod 0). Szczegóły: "
            f"log {started.log_path}.",
            artifact=started.log_path,
        )
    return ActionOutcome(
        True,
        f"{found}; instalacja uruchomiona w tle. {updates.PROGRESS_HINT} i log "
        f"{started.log_path}. Po zakończeniu może być wymagany restart.",
        artifact=started.log_path,
    )


def _wu_already_running(ctx: ActionContext, running: updates.BackgroundInstall) -> ActionOutcome:
    ctx.log("Instalacja aktualizacji Windows już trwa w tle — nie uruchamiam drugiej.")
    return ActionOutcome(
        True,
        f"Instalacja aktualizacji już trwa w tle. Log: {running.log_path}",
        artifact=running.log_path,
    )


STORE_UNCONFIRMED = (
    "wyniku nie da się potwierdzić — sprawdź w Microsoft Store → Pobrane (Biblioteka)."
)


def store_update(ctx: ActionContext) -> ActionOutcome:
    """Ask the Microsoft Store to update every app; the download continues in the
    system's InstallService after this returns."""
    ctx.log("Zlecanie Microsoft Store wyszukania i instalacji aktualizacji aplikacji...")
    try:
        result = updates.update_store_apps(cancel_event=ctx.cancel_event, log=ctx.log)
    except updates.UpdatesCancelled:
        return ActionOutcome(False, _CANCELLED_MESSAGE, cancelled=True)
    except updates.UpdatesError as exc:
        raise ActionError(exc.message) from exc
    if not result.confirmed:  # store.exe: requested, never confirmed
        for line in result.lines:
            ctx.log(f"  {line}")
        if result.found_count == 0:
            return ActionOutcome(
                True,
                "Wiersz poleceń Sklepu (store.exe) nie zgłosił aktualizacji aplikacji; "
                + STORE_UNCONFIRMED,
            )
        what = f"{result.found_count} aplikacji" if result.found_count else "aplikacji"
        return ActionOutcome(
            True,
            f"Zlecono aktualizację {what} Microsoft Store przez wiersz poleceń Sklepu "
            f"(store.exe); {STORE_UNCONFIRMED}",
        )
    for item in result.items:
        ctx.log(f"  {item.label()}")
    for item in result.failed:
        code = f", kod {item.error_code}" if item.error_code else ""
        ctx.log(f"  Błąd: {item.label()} (stan {item.state or 'nieznany'}{code})")
    if result.failed and not result.items:
        raise ActionError(
            f"Microsoft Store nie zlecił żadnej aktualizacji: wszystkie {len(result.failed)} "
            f"pozycji zakończyło się błędem (szczegóły w dzienniku). {updates.STORE_MANUAL_HINT}"
        )
    if result.count == 0:
        return ActionOutcome(True, "Aplikacje Microsoft Store są aktualne.")
    message = (
        f"Zlecono aktualizację {result.count} aplikacji Microsoft Store; pobieranie i instalacja "
        "trwają w tle (postęp: Microsoft Store → Pobrane)."
    )
    if result.failed:
        message += f" Uwaga: {len(result.failed)} pozycji z błędem (szczegóły w dzienniku)."
    return ActionOutcome(True, message)


# ------------------------------------------------------------- registry

ACTIONS: Mapping[str, ActionFunc] = {
    "remove_bloat": remove_bloat,
    "theme_dark": theme_dark,
    "theme_light": theme_light,
    "copy_dir_to_desktop": copy_dir_to_desktop,
    "copy_self_to_desktop": copy_self_to_desktop,
    "reboot_to_bios": reboot_to_bios,
    "reboot_advanced": reboot_advanced,
    "battery_report": battery_report,
    "windows_update": windows_update,
    "store_update": store_update,
}


def as_outcome(value: ActionOutcome | str | None) -> ActionOutcome:
    """Normalize an :data:`ActionFunc` result (``str``/``None`` = success)."""
    if isinstance(value, ActionOutcome):
        return value
    return ActionOutcome(True, value or "Gotowe.")


def _from_winget(result: winget.WingetResult) -> ActionOutcome:
    return ActionOutcome(result.ok, result.message, cancelled=result.cancelled)


def execute(item: Item, ctx: ActionContext) -> ActionOutcome:
    """Execute one catalog item for real. May raise :class:`ActionError`."""
    match item.kind:
        case ItemKind.WINGET:
            assert item.winget_id
            ctx.log(f"Instalowanie {item.name} ({item.winget_id})...")
            outcome = _from_winget(
                winget.install(
                    item.winget_id,
                    on_line=ctx.log,
                    on_replace_last=ctx.replace_last,
                    cancel_event=ctx.cancel_event,
                )
            )
            if outcome.ok and item.post_launch:
                launch_after_install(ctx, item.post_launch)
            return outcome
        case ItemKind.WINGET_UNINSTALL:
            assert item.winget_id
            ctx.log(f"Odinstalowywanie {item.name} ({item.winget_id})...")
            return _from_winget(
                winget.uninstall(
                    item.winget_id,
                    on_line=ctx.log,
                    on_replace_last=ctx.replace_last,
                    cancel_event=ctx.cancel_event,
                )
            )
        case ItemKind.LOCAL_EXE:
            assert item.path
            outcome = run_local_installer(ctx, item.path, item.args)
            if outcome.ok and item.post_launch:
                launch_after_install(ctx, item.post_launch)
            return outcome
        case ItemKind.ACTION:
            assert item.action
            try:
                func = ACTIONS[item.action]
            except KeyError:
                raise ActionError(f"Nieznana akcja: {item.action}") from None
            return as_outcome(func(ctx))
        case ItemKind.DRIVER:
            return install_driver(ctx, item.vendor)
    raise ActionError(f"Nieobsługiwany rodzaj pozycji: {item.kind}")
