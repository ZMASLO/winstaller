"""Windows Update and Microsoft Store app updates (no Qt, synchronous).

* Windows Update: :func:`scan_windows_updates` runs a read-only WUA search (all
  updates: regular, optional/Preview and drivers) in PowerShell; then
  :func:`start_windows_update_install` starts a detached, hidden PowerShell process
  that downloads and installs them through the same API and exits. Winstaller never
  waits for it and never restarts the machine.
* Microsoft Store: :func:`update_store_apps` asks the Store install service
  (``AppInstallManager.SearchForAllUpdatesAsync``) to find and queue every app update;
  fallback ``store.exe updates --apply``. The service keeps downloading after we return.

Every contact with the system goes through the small functions of the "system layer"
section (``run_powershell``, ``start_background``, ``mutex_exists``, ``run_store_cli``,
``log_dir``), so tests replace them with ``monkeypatch``. The ``parse_*`` functions are
pure and raise only :class:`UpdatesError` on any input. Scripts: ``update_scripts``.
Background on the mechanisms, threats and known unknowns: docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import functools
import html
import json
import logging
import math
import os
import re
import secrets
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from winstaller.core import platform as plat
from winstaller.core import update_scripts, winget

LineCallback = Callable[[str], None]
_log = logging.getLogger(__name__)

#: Named mutex held by the background install process (one instance per machine).
MUTEX_NAME = r"Global\Winstaller.WindowsUpdate"
SCAN_TIMEOUT = 180.0  # seconds; a WUA search normally takes 5-30 s
STORE_TIMEOUT = 180.0
STORE_CLI_TIMEOUT = 300.0
#: CreateProcess limit for the whole command line (characters, incl. the terminator).
MAX_COMMAND_LINE = 32767
#: Longest allow-list passed in WINSTALLER_WU_IDS (one variable may hold 32 767
#: characters); a longer one switches to ``install_all`` with a warning.
MAX_IDS_CHARS = 30_000
#: How long :func:`verify_background_start` waits for the first status write.
START_CONFIRM_TIMEOUT = 10.0
#: Defaults of the "skip drivers" options. Owner's decision: install everything,
#: drivers (also the Display class) included; the switches stay for later.
SKIP_DRIVERS = False
SKIP_DISPLAY_DRIVERS = False
#: Skip updates flagged InstallationBehavior.CanRequestUserInput. Off: WUA flags most
#: drivers so (3 of 3 on the test machine) although Windows Update installs them quietly.
SKIP_INTERACTIVE = False

ENV_WU_LOG = "WINSTALLER_WU_LOG"
ENV_WU_STATUS = "WINSTALLER_WU_STATUS"
ENV_WU_IDS = "WINSTALLER_WU_IDS"
ENV_WU_SKIP_DRIVERS = "WINSTALLER_WU_SKIP_DRIVERS"
ENV_WU_SKIP_DISPLAY_DRIVERS = "WINSTALLER_WU_SKIP_DISPLAY_DRIVERS"
ENV_WU_SKIP_INTERACTIVE = "WINSTALLER_WU_SKIP_INTERACTIVE"
ENV_WU_ALL = "WINSTALLER_WU_ALL"  # "1" = install everything found (no allow-list)
ENV_STORE_MODE = "WINSTALLER_STORE_MODE"
ENV_STORE_TIMEOUT = "WINSTALLER_STORE_TIMEOUT"

LOG_PREFIX = "windows-update-"
TIMESTAMP_FORMAT = "%Y.%m.%d-%H%M%S"  # RRRR.MM.DD-GGMMSS

STORE_MANUAL_HINT = (
    "Zaktualizuj aplikacje ręcznie: Microsoft Store → Pobrane (Biblioteka) → "
    "„Pobierz aktualizacje”."
)

ErrorKind = Literal[
    "network",
    "service_disabled",
    "policy",
    "busy",
    "reboot",
    "server",
    "not_elevated",
    "timeout",
    "cancelled",
    "format",
    "start",
    "store",
    "blocked",
    "other",
]


class UpdatesError(Exception):
    """Searching/starting updates failed. ``message`` (= ``str(exc)``) is Polish."""

    def __init__(
        self, message: str, *, kind: ErrorKind = "other", hresult: str | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind: ErrorKind = kind
        self.hresult = hresult


class UpdatesCancelled(UpdatesError):
    """The user cancelled (the PowerShell process was killed)."""

    def __init__(self, message: str = "Przerwano przez użytkownika.") -> None:
        super().__init__(message, kind="cancelled")


# ------------------------------------------------------------------ error mapping

#: HRESULT (unsigned) -> error class. Source: WUA error code list (wuerror.h) and
#: WinHTTP errors seen in practice.
HRESULT_KINDS: Mapping[int, ErrorKind] = {
    0x80072EE2: "network",  # WinHTTP timeout
    0x80072EE7: "network",  # name not resolved
    0x80072EFD: "network",  # cannot connect
    0x80072EFE: "network",  # connection aborted
    0x8024402C: "network",  # WU_E_PT_WINHTTP_NAME_NOT_RESOLVED
    0x8024401B: "network",  # proxy authentication required
    0x8024001F: "network",  # WU_E_NO_CONNECTION
    0x80246005: "network",  # WU_E_DM_NONETWORK
    0x80070422: "service_disabled",  # service disabled
    0x8024A000: "service_disabled",  # WU_E_AU_NOSERVICE
    0x8024002E: "policy",  # WU_E_WU_DISABLED (managed: no access to Microsoft servers)
    0x8024002F: "policy",  # DisableWindowsUpdateAccess
    0x80240025: "policy",  # WU_E_USER_ACCESS_DISABLED
    0x80244019: "policy",  # HTTP 404 (typically a wrong WSUS address)
    0x80244018: "policy",  # HTTP 403
    0x80244022: "server",  # HTTP 503
    0x80240009: "busy",  # WU_E_OPERATIONINPROGRESS
    0x80240016: "busy",  # WU_E_INSTALL_NOT_ALLOWED (another installation running)
    0x8024001B: "busy",  # WU_E_SELFUPDATE_IN_PROGRESS
    0x8024A004: "busy",  # WU_E_AU_PAUSED
    0x80242014: "reboot",  # WU_E_UH_POSTREBOOTSTILLPENDING
    0x80070005: "not_elevated",  # E_ACCESSDENIED
}

_KIND_MESSAGES: Mapping[str, str] = {
    "network": "Brak połączenia z serwerami Windows Update. Sprawdź połączenie z internetem "
    "(także serwer proxy) i spróbuj ponownie.",
    "service_disabled": "Usługa Windows Update jest wyłączona. Włącz usługę „Windows Update” "
    "(wuauserv) i spróbuj ponownie.",
    "policy": "Windows Update jest zablokowany albo zarządzany przez zasady organizacji "
    "(WSUS/zasady grupy) — aktualizacje są dostępne tylko przez administratora sieci.",
    "busy": "Windows Update jest zajęty inną operacją (np. trwa inna instalacja). "
    "Spróbuj ponownie za kilka minut.",
    "reboot": "Windows czeka na ponowne uruchomienie po wcześniejszej instalacji. "
    "Uruchom ponownie komputer i spróbuj jeszcze raz.",
    "server": "Serwer Windows Update jest chwilowo niedostępny. Spróbuj ponownie później.",
    "not_elevated": "Odmowa dostępu do Windows Update — uruchom Winstaller jako administrator.",
}


def hresult_value(text: object) -> int | None:
    """``"0x8024402C"`` / ``-2145107924`` -> unsigned int; anything else -> ``None``."""
    if isinstance(text, bool):
        return None
    if isinstance(text, int):
        return text & 0xFFFFFFFF
    if isinstance(text, str) and re.fullmatch(r"0[xX][0-9A-Fa-f]{1,8}", text.strip()):
        return int(text.strip(), 16)
    return None


def format_hresult(value: int | None) -> str | None:
    return None if value is None else f"0x{value & 0xFFFFFFFF:08X}"


def error_kind(hresult: object) -> ErrorKind:
    value = hresult_value(hresult)
    return HRESULT_KINDS.get(value, "other") if value is not None else "other"


def describe_wu_error(hresult: object, detail: str = "") -> UpdatesError:
    """Polish :class:`UpdatesError` for a WUA failure (class from :data:`HRESULT_KINDS`)."""
    code = format_hresult(hresult_value(hresult))
    kind = error_kind(hresult)
    base = _KIND_MESSAGES.get(kind)
    if base is None:
        detail = _one_line(detail).rstrip(". ")
        base = "Nie udało się wyszukać aktualizacji Windows" + (f": {detail}" if detail else "")
    suffix = f" (kod {code})" if code else ""
    message = base.rstrip(".") + suffix + "."
    return UpdatesError(message, kind=kind, hresult=code)


# ------------------------------------------------------------------ data

UpdateType = Literal["software", "driver"]


@dataclass(frozen=True, slots=True)
class WindowsUpdateInfo:
    id: str  # Identity.UpdateID (lowercase GUID)
    revision: int
    title: str
    kb: tuple[str, ...] = ()  # "KB5132057"
    type: UpdateType = "software"
    optional: bool = False  # DeploymentAction = OptionalInstallation (Preview, optional drivers)
    browse_only: bool = False
    downloaded: bool = False
    categories: tuple[str, ...] = ()
    #: InstallationBehavior.CanRequestUserInput (skipped only with ``skip_interactive``).
    interactive: bool = False
    driver_class: str | None = None
    driver_model: str | None = None
    driver_vendor: str | None = None

    @property
    def is_driver(self) -> bool:
        return self.type == "driver"

    @property
    def key(self) -> tuple[str, int]:
        return (self.id, self.revision)

    def label(self) -> str:
        """``KB5132057 — 2026-11 Aktualizacja … [opcjonalna] [sterownik]`` (no sizes)."""
        text = f"{self.kb[0]} — {self.title}" if self.kb else self.title
        if self.optional:
            text += " [opcjonalna]"
        if self.is_driver:
            text += " [sterownik]"
        return text


@dataclass(frozen=True, slots=True)
class WindowsUpdateScan:
    updates: tuple[WindowsUpdateInfo, ...]
    reboot_pending: bool = False
    #: Elevation of the scanning process; ``None`` = unknown (not reported).
    is_admin: bool | None = None
    seconds: float = 0.0
    criteria: str = ""
    result_code: int | None = None
    #: Polish warnings for the log (skipped malformed entries, ResultCode 3...).
    warnings: tuple[str, ...] = ()
    #: Number of entries skipped because they were malformed (never installed).
    skipped: int = 0

    @property
    def optional_count(self) -> int:
        return sum(1 for u in self.updates if u.optional)

    @property
    def driver_count(self) -> int:
        return sum(1 for u in self.updates if u.is_driver)


@dataclass(frozen=True, slots=True)
class BackgroundInstall:
    log_path: Path
    status_path: Path | None
    pid: int | None
    #: Another install was already running: nothing new was started, ``log_path`` is
    #: that run's newest log (or the log folder when none is found).
    already_running: bool = False
    #: The allow-list did not fit, everything found is installed (see MAX_IDS_CHARS).
    install_all: bool = False
    #: Set by :func:`verify_background_start`: the process already finished with code 0.
    finished: bool = False


@dataclass(frozen=True, slots=True)
class StoreAppInfo:
    product_id: str
    package_family_name: str = ""
    install_type: str = ""
    state: str = ""
    error_code: str = ""  # "0x80070005" when the item reports an error

    @property
    def failed(self) -> bool:
        return bool(self.error_code) or self.state.casefold() == "error"

    @property
    def name(self) -> str:
        """Readable name: package name without the publisher hash, else the product id."""
        if self.package_family_name:
            return self.package_family_name.rsplit("_", 1)[0] or self.package_family_name
        return self.product_id or "?"

    def label(self) -> str:
        extra = f" ({self.product_id})" if self.product_id and self.package_family_name else ""
        return f"{self.name}{extra}"


StoreMethod = Literal["AppInstallManager", "store-cli"]


@dataclass(frozen=True, slots=True)
class StoreUpdateResult:
    method: StoreMethod
    items: tuple[StoreAppInfo, ...] = ()
    #: store-cli: cleaned output lines (for the log); AppInstallManager: empty.
    lines: tuple[str, ...] = ()
    #: store-cli: number of updates reported ("(9 found)"), when present.
    found_count: int | None = None
    auto_update_setting: str | None = None
    queue: tuple[StoreAppInfo, ...] = ()
    #: AppInstallManager: items the Store returned in an error state (not queued).
    failed: tuple[StoreAppInfo, ...] = ()

    @property
    def confirmed(self) -> bool:
        """True when the Store itself returned the queued items (WinRT); ``store.exe``
        output is never treated as a confirmation."""
        return self.method == "AppInstallManager"

    @property
    def count(self) -> int:
        if self.method == "store-cli":
            return self.found_count or 0
        return len(self.items)


# ------------------------------------------------------------------ pure helpers


def _parser[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Contract of every ``parse_*``: bad input -> :class:`UpdatesError`, never anything
    else (``RecursionError`` from deep JSON, ``MemoryError``, type errors...)."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except UpdatesError:
            raise
        except Exception as exc:
            raise UpdatesError(
                f"Nieprawidłowy wynik PowerShella ({type(exc).__name__}).", kind="format"
            ) from None

    return wrapper


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
#: Zero-width and bidirectional formatting characters (could disguise a log line).
_INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CLIXML_STRING = re.compile(r'<S S="Error">(.*?)</S>', re.DOTALL)
_CLIXML_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")


def _one_line(text: object, limit: int = 300) -> str:
    value = str(text) if text is not None else ""
    value = " ".join(_CONTROL.sub(" ", _INVISIBLE.sub("", value)).split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _clean_text(value: object, limit: int = 300) -> str:
    return _one_line(value, limit) if isinstance(value, str) else ""


def clean_powershell_stderr(text: str) -> str:
    """Readable text from PowerShell's stderr, which may be a CLIXML document
    (``#< CLIXML <Objs …><S S="Error">…_x000D__x000A_</S>``) or plain text."""
    if not isinstance(text, str):
        return ""
    body = text.lstrip("\ufeff").strip()
    if body.startswith("#< CLIXML"):
        parts = _CLIXML_STRING.findall(body)
        if not parts:
            return ""
        decoded = "".join(
            _CLIXML_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), html.unescape(p)) for p in parts
        )
        body = decoded
    return _one_line(body, 500)


def compact_script(script: str) -> str:
    """Drop blank lines, full-line ``#`` comments and indentation (the scripts contain
    no here-strings or multi-line string literals, so this does not change them);
    keeps ``-EncodedCommand`` well below the command line limit."""
    lines = []
    for raw in script.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            lines.append(line)
    return "\n".join(lines) + "\n"


def powershell_args(script: str, *, hidden: bool = False) -> list[str]:
    """PowerShell (absolute System32 path, never searched by name) running ``script``
    via ``-EncodedCommand``. Raises :class:`UpdatesError` when the command line would
    exceed :data:`MAX_COMMAND_LINE`, ``FileNotFoundError`` when PowerShell is missing."""
    encoded = base64.b64encode(compact_script(script).encode("utf-16-le")).decode("ascii")
    args = [
        plat.system32_path(plat.POWERSHELL_EXE),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
    ]
    if hidden:
        args += ["-WindowStyle", "Hidden"]
    args += ["-EncodedCommand", encoded]
    if len(subprocess.list2cmdline(args)) >= MAX_COMMAND_LINE:
        raise UpdatesError("Skrypt PowerShella jest zbyt długi dla wiersza poleceń.", kind="start")
    return args


def _json_lines(text: str, marker: str) -> dict[str, Any] | None:
    """Last line of ``text`` that is a JSON object with ``"winstaller": marker``."""
    # Only \n separates lines: splitlines() would also split on U+2028/U+2029/U+0085
    # inside a JSON string (ConvertTo-Json in PowerShell 5.1 leaves them raw).
    for raw in reversed(text.lstrip("\ufeff").split("\n")):
        line = raw.strip().lstrip("\ufeff")
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("winstaller") == marker:
            return data
    return None


def _str_list(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return ()
    return tuple(_clean_text(v, 120) for v in value if isinstance(v, str) and v.strip())


def _bool(value: object) -> bool:
    return value is True


def _no_output_error(what: str, returncode: int | None, stderr: str) -> UpdatesError:
    detail = clean_powershell_stderr(stderr)
    code = f"kod {returncode}" if returncode not in (None, 0) else "brak wyniku"
    message = f"{what} ({code})"
    return UpdatesError(message + (f": {detail}" if detail else "."), kind="format")


@_parser
def parse_wu_scan(text: str, *, stderr: str = "", returncode: int | None = 0) -> WindowsUpdateScan:
    """The JSON line of ``WU_SCAN_SCRIPT``. A failed scan -> :class:`UpdatesError` with
    the class and Polish message of its HRESULT; garbage, no JSON or a malformed entry
    -> :class:`UpdatesError` (``kind="format"``), never another exception."""
    data = _json_lines(text if isinstance(text, str) else "", "wu-scan")
    if data is None:
        raise _no_output_error(
            "Nie udało się odczytać wyniku wyszukiwania aktualizacji", returncode, stderr
        )
    if data.get("ok") is not True:
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        raise describe_wu_error(error.get("hresult"), _clean_text(error.get("message")))
    result_code = data.get("resultCode")
    result_code = (
        result_code if isinstance(result_code, int) and not isinstance(result_code, bool) else None
    )
    if result_code in (4, 5):
        what = "nie powiodło się" if result_code == 4 else "zostało przerwane"
        raise UpdatesError(
            f"Wyszukiwanie aktualizacji Windows {what} (ResultCode {result_code}) — "
            "lista aktualizacji jest niepełna, spróbuj ponownie.",
            kind="other",
        )
    raw_updates = data.get("updates")
    if raw_updates is None:
        raw_updates = []
    if isinstance(raw_updates, dict):  # a single object, if ConvertTo-Json unrolled it
        raw_updates = [raw_updates]
    if not isinstance(raw_updates, list):
        raise UpdatesError("Nieprawidłowa lista aktualizacji w wyniku wyszukiwania.", kind="format")
    warnings: list[str] = []
    if result_code == 3:
        warnings.append(
            "Ostrzeżenie: wyszukiwanie zakończyło się z błędami (ResultCode 3) — lista może "
            "być niepełna."
        )
    updates: list[WindowsUpdateInfo] = []
    seen: set[tuple[str, int]] = set()
    skipped = 0
    for entry in raw_updates:
        try:
            info = _update_from(entry)
        except UpdatesError as exc:
            skipped += 1
            warnings.append(
                f"Ostrzeżenie: pominięto pozycję wyniku wyszukiwania ({exc.message.rstrip('.')}); "
                "nie trafi na listę do instalacji."
            )
            continue
        if info.key not in seen:
            seen.add(info.key)
            updates.append(info)
    seconds = data.get("seconds")
    valid_seconds = (
        isinstance(seconds, int | float)
        and not isinstance(seconds, bool)
        and math.isfinite(seconds)
        and seconds >= 0
    )
    return WindowsUpdateScan(
        updates=tuple(updates),
        reboot_pending=_bool(data.get("rebootPending")),
        is_admin=data.get("isAdmin") if isinstance(data.get("isAdmin"), bool) else None,
        seconds=float(seconds) if valid_seconds else 0.0,
        criteria=_clean_text(data.get("criteria"), 400),
        result_code=result_code,
        warnings=tuple(warnings[:20]),
        skipped=skipped,
    )


def _update_from(entry: object) -> WindowsUpdateInfo:
    if not isinstance(entry, dict):
        raise UpdatesError("nieprawidłowy format pozycji", kind="format")
    raw_id = entry.get("id")
    update_id = raw_id.strip().lower() if isinstance(raw_id, str) else ""
    if not _GUID.fullmatch(update_id):
        raise UpdatesError("nieprawidłowy identyfikator aktualizacji", kind="format")
    revision = entry.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        revision = 0
    title = _clean_text(entry.get("title"))
    if not title:
        raise UpdatesError(f"aktualizacja {update_id} bez nazwy", kind="format")
    kb = tuple(k for k in _str_list(entry.get("kb")) if re.fullmatch(r"KB\d+", k))
    is_driver = entry.get("type") == "driver"

    def driver_field(name: str) -> str | None:
        return (_clean_text(entry.get(name), 120) or None) if is_driver else None

    return WindowsUpdateInfo(
        id=update_id,
        revision=revision,
        title=title,
        kb=kb,
        type="driver" if is_driver else "software",
        optional=_bool(entry.get("optional")),
        browse_only=_bool(entry.get("browseOnly")),
        downloaded=_bool(entry.get("downloaded")),
        categories=_str_list(entry.get("categories")),
        interactive=_bool(entry.get("canRequestUserInput")),
        driver_class=driver_field("driverClass"),
        driver_model=driver_field("driverModel"),
        driver_vendor=driver_field("driverVendor"),
    )


def select_updates(
    updates: Iterable[WindowsUpdateInfo],
    *,
    skip_drivers: bool = SKIP_DRIVERS,
    skip_display_drivers: bool = SKIP_DISPLAY_DRIVERS,
    skip_interactive: bool = SKIP_INTERACTIVE,
) -> list[WindowsUpdateInfo]:
    """Updates the background process will install (same rules as the script): all,
    unless asked to skip (display) drivers or updates that may ask for input."""
    selected = []
    for update in updates:
        if update.interactive and skip_interactive:
            continue
        if update.is_driver and skip_drivers:
            continue
        # PowerShell's -eq is case-insensitive: compare the same way.
        display = (update.driver_class or "").casefold() == "display"
        if update.is_driver and skip_display_drivers and display:
            continue
        selected.append(update)
    return selected


def _store_item(entry: object) -> StoreAppInfo | None:
    if not isinstance(entry, dict):
        return None
    product = _clean_text(entry.get("productId"), 60)
    family = _clean_text(entry.get("packageFamilyName"), 200)
    if not product and not family:
        return None
    return StoreAppInfo(
        product_id=product,
        package_family_name=family,
        install_type=_clean_text(entry.get("installType"), 40),
        state=_clean_text(entry.get("state"), 40),
        error_code=format_hresult(hresult_value(entry.get("errorCode"))) or "",
    )


def _store_items(value: object) -> tuple[StoreAppInfo, ...]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return ()
    return tuple(i for i in (_store_item(v) for v in value) if i is not None)


@_parser
def parse_store_update(
    text: str, *, stderr: str = "", returncode: int | None = 0
) -> StoreUpdateResult:
    """The JSON line of ``STORE_SCRIPT`` -> :class:`StoreUpdateResult`; failure or
    garbage -> :class:`UpdatesError` (``kind`` "policy", "store" or "format")."""
    data = _json_lines(text if isinstance(text, str) else "", "store-update")
    if data is None:
        raise _no_output_error(
            "Nie udało się odczytać odpowiedzi Microsoft Store", returncode, stderr
        )
    if data.get("ok") is not True:
        error = data.get("error") if isinstance(data.get("error"), dict) else {}
        code = format_hresult(hresult_value(error.get("hresult")))
        if data.get("blockedByPolicy") is True:
            raise UpdatesError("Microsoft Store jest zablokowany przez zasady.", kind="policy")
        if error.get("timeout") is True:
            raise UpdatesError(
                "Microsoft Store nie odpowiedział w wyznaczonym czasie.", kind="timeout"
            )
        detail = _clean_text(error.get("message")) or "nieznany błąd"
        suffix = f" (kod {code})" if code else ""
        raise UpdatesError(
            f"Usługa Microsoft Store odrzuciła zlecenie: {detail.rstrip('.')}{suffix}.",
            kind="store",
            hresult=code,
        )
    found = _store_items(data.get("found"))
    return StoreUpdateResult(
        method="AppInstallManager",
        items=tuple(i for i in found if not i.failed),
        failed=tuple(i for i in found if i.failed),
        auto_update_setting=_clean_text(data.get("autoUpdateSetting"), 40) or None,
        queue=_store_items(data.get("queue")),
    )


_BOX_CHARS = "│┃┌┐└┘├┤┬┴┼─━═║╔╗╚╝╠╣╦╩╬╭╮╯╰"
_BORDER_ONLY = re.compile(rf"^[\s{_BOX_CHARS}]*$")
_STORE_FOUND = re.compile(r"\((\d+)\s+found\)", re.IGNORECASE)
#: Any of these in the output of ``store.exe updates --apply`` means failure (its exit
#: code is 0 even after errors). There is no "success" heuristic: without a failure
#: marker the result is only "requested, not confirmed".
_STORE_FAILURE = re.compile(
    r"\[y/n\]|0x[0-9a-f]{8}|timed? ?out|\berrors?\b|\bfail|blocked|sign ?in|exception|"
    r"denied|unable to|could not|cannot|can't|non-interactive|not available|"
    r"zainstalowano 0 z|błąd|błęd|nie udało|nie można|nie powiodło|zablokowan|zaloguj|"
    r"limit czasu|przekroczono",
    re.IGNORECASE,
)
_STORE_NONE = re.compile(
    r"no updates|up to date|all apps are up|brak aktualizacji|aktualn", re.IGNORECASE
)
#: Words of an operation in progress (only reported in the log, never a success test).
_STORE_STARTED = re.compile(
    r"\binstalling\b|\bupdating\b|\bdownloading\b|\bqueued\b|\bstarted\b|\bapplying\b|"
    r"\bsucce|instalowanie|aktualizowanie|pobieranie|zainstalowano",
    re.IGNORECASE,
)


def clean_store_lines(text: str) -> list[str]:
    """``store.exe`` output for the log: no ANSI, box-drawing borders dropped, cell
    separators turned into ``|``, blank lines removed. Not a table parser."""
    parser = winget.StreamParser()
    raw = text.encode("utf-8", "replace") if isinstance(text, str) else b""
    events = parser.feed(raw) + parser.flush()
    lines: list[str] = []
    for event in events:
        if event.kind == winget.OutputKind.REPLACE_LAST and lines:
            lines.pop()
        line = event.text
        if _BORDER_ONLY.match(line):
            continue
        for ch in "│┃║":
            line = line.replace(ch, "|")
        line = " ".join(line.strip(" |").split())
        line = re.sub(r"\s*\|\s*", " | ", line)
        if line:
            lines.append(_one_line(line, 200))
    return lines


@_parser
def parse_store_cli_output(text: str, returncode: int | None = 0) -> StoreUpdateResult:
    """Judge ``store.exe updates --apply`` by its text (its exit code is 0 even after a
    failure). Any failure marker (see ``_STORE_FAILURE``) or no output at all ->
    :class:`UpdatesError`. Otherwise the request is *not confirmed*
    (``confirmed`` is False): ``found_count`` from "(N found)", 0 for "no updates", else
    ``None``; the caller says the result must be checked in the Store."""
    if not isinstance(text, str):
        raise UpdatesError("Polecenie store.exe nie zwróciło żadnego wyniku.", kind="store")
    lines = clean_store_lines(text)
    if not lines:
        raise UpdatesError("Polecenie store.exe nie zwróciło żadnego wyniku.", kind="store")
    failures = [line for line in lines if _STORE_FAILURE.search(line)]
    if failures:
        raise UpdatesError(
            "Polecenie store.exe zgłosiło błąd: " + " / ".join(failures[:3]), kind="store"
        )
    joined = "\n".join(lines)
    match = _STORE_FOUND.search(joined)
    found = int(match.group(1)) if match else None
    if found is None and _STORE_NONE.search(joined):
        found = 0
    return StoreUpdateResult(method="store-cli", lines=tuple(lines), found_count=found)


def log_file_names(now: datetime) -> tuple[str, str]:
    stamp = now.strftime(TIMESTAMP_FORMAT)
    return f"{LOG_PREFIX}{stamp}.log", f"{LOG_PREFIX}{stamp}.json"


def validate_update_ids(update_ids: Iterable[str]) -> list[str]:
    ids = []
    for raw in update_ids:
        value = raw.strip().lower() if isinstance(raw, str) else ""
        if not _GUID.fullmatch(value):
            raise UpdatesError(f"Nieprawidłowy identyfikator aktualizacji: {raw!r}.", kind="format")
        if value not in ids:
            ids.append(value)
    return ids


# ------------------------------------------------------------------ system layer
# Thin wrappers around the OS. Tests replace these with monkeypatch.

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_SYNCHRONIZE = 0x00100000
_ERROR_ACCESS_DENIED = 5
_KILL_WAIT_SECONDS = 5.0
_POLL_SECONDS = 0.1
#: Popen objects of background processes (keeps them referenced: no ResourceWarning).
_BACKGROUND: list[subprocess.Popen[bytes]] = []


#: The only variables a child (PowerShell, store.exe) inherits from Winstaller, matched
#: case-insensitively. Everything else (COR_*/COMPlus_*/DOTNET_* runtime switches, a user
#: PSModulePath, PATH entries...) could load foreign code into the elevated process.
CHILD_ENV_ALLOWED: tuple[str, ...] = (
    "SystemRoot",
    "windir",
    "SystemDrive",
    "ComSpec",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "ProgramData",
    "ProgramFiles",
    "ProgramFiles(x86)",
    "ProgramW6432",
    "CommonProgramFiles",
    "CommonProgramFiles(x86)",
    "CommonProgramW6432",
    "USERNAME",
    "USERDOMAIN",
    "COMPUTERNAME",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
    "PUBLIC",
    "ALLUSERSPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
)
CHILD_ENV_PREFIXES: tuple[str, ...] = ("WINSTALLER_",)


def _child_env(env: Mapping[str, str] | None) -> dict[str, str]:
    """Environment of a child process: the allow-list above from Winstaller's own
    environment, ``PATH`` and ``PSModulePath`` set to the system folders only, then
    ``env`` (our own parameters) on top."""
    allowed = {name.upper(): name for name in CHILD_ENV_ALLOWED}
    merged: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in allowed:
            merged[allowed[upper]] = value
        elif upper.startswith(CHILD_ENV_PREFIXES):
            merged[key] = value
    system = plat.windows_system_dir()
    merged["PATH"] = ";".join(
        str(d) for d in (system, system / "Wbem", system / "WindowsPowerShell" / "v1.0")
    )
    merged["PSModulePath"] = str(system / "WindowsPowerShell" / "v1.0" / "Modules")
    for key, value in (env or {}).items():
        for existing in [k for k in merged if k.upper() == key.upper()]:
            del merged[existing]
        merged[key] = value
    return merged


def _kill(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=_KILL_WAIT_SECONDS)


def run_powershell(
    script: str,
    *,
    env: Mapping[str, str] | None = None,
    timeout: float,
    cancel_event: threading.Event | None = None,
) -> tuple[int, str, str]:
    """Run ``script`` hidden and wait: ``(returncode, stdout, stderr)`` (decoded).

    Cancel -> the process is killed and :class:`UpdatesCancelled` raised; ``timeout``
    seconds passed -> killed and :class:`UpdatesError` (``kind="timeout"``). Cannot
    start -> :class:`UpdatesError` (``kind="start"``). An already set ``cancel_event``
    raises :class:`UpdatesCancelled` without starting anything.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise UpdatesCancelled()
    try:
        proc = subprocess.Popen(
            powershell_args(script),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=winget.CREATE_NO_WINDOW,
            env=_child_env(env),
        )
    except OSError as exc:
        raise UpdatesError(f"Nie udało się uruchomić PowerShella: {exc}", kind="start") from exc
    result: dict[str, tuple[bytes, bytes]] = {}

    def communicate() -> None:
        try:
            result["out"] = proc.communicate()
        except (OSError, ValueError):
            result["out"] = (b"", b"")

    reader = threading.Thread(target=communicate, name="winstaller-updates-ps", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    while reader.is_alive():
        reader.join(_POLL_SECONDS)
        if not reader.is_alive():
            break
        if cancel_event is not None and cancel_event.is_set():
            _kill(proc)
            reader.join(_KILL_WAIT_SECONDS)
            raise UpdatesCancelled()
        if time.monotonic() > deadline:
            _kill(proc)
            reader.join(_KILL_WAIT_SECONDS)
            raise UpdatesError(
                f"Przekroczono limit czasu ({int(timeout)} s) — operacja przerwana.", kind="timeout"
            )
    stdout, stderr = result.get("out", (b"", b""))
    return (
        proc.returncode if proc.returncode is not None else -1,
        winget.decode_console_bytes(stdout or b""),
        winget.decode_console_bytes(stderr or b""),
    )


def start_background(args: Sequence[str], env: Mapping[str, str]) -> int:
    """Start ``args`` detached from Winstaller: hidden, own process group, outside
    Winstaller's job (when the job allows breakaway), no inherited handles. Returns the
    PID. Raises ``OSError``."""
    flags = winget.CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": _child_env(env),
        "cwd": str(plat.windows_system_dir()),
    }
    try:
        proc = subprocess.Popen(
            list(args), creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, **kwargs
        )
    except OSError as exc:
        # In a job without JOB_OBJECT_LIMIT_BREAKAWAY_OK breakaway is refused with
        # ERROR_ACCESS_DENIED; start inside the job then (it may die with Winstaller).
        if getattr(exc, "winerror", None) != _ERROR_ACCESS_DENIED:
            raise
        proc = subprocess.Popen(list(args), creationflags=flags, **kwargs)
    _BACKGROUND.append(proc)
    return proc.pid


def mutex_exists(name: str) -> bool:
    """True when a named mutex ``name`` exists (also when it exists but we may not open it)."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return False
    open_mutex = kernel32.OpenMutexW
    open_mutex.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    open_mutex.restype = ctypes.c_void_p
    handle = open_mutex(_SYNCHRONIZE, 0, name)
    if handle:
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() == _ERROR_ACCESS_DENIED


def store_cli_path() -> Path:
    """``store.exe``: an App Execution Alias in the user's profile (like winget)."""
    return plat.local_app_data_dir() / "Microsoft" / "WindowsApps" / "store.exe"


_IO_REPARSE_TAG_APPEXECLINK = 0x8000001B


def run_store_cli(
    args: Sequence[str],
    *,
    timeout: float = STORE_CLI_TIMEOUT,
    cancel_event: threading.Event | None = None,
) -> tuple[int, str]:
    """Run ``store.exe <args>`` without a console and with stdin closed (it must never
    wait for an answer): ``(returncode, output)``. Only an App Execution Alias is run
    (reparse tag check). Cancel/timeout as in :func:`run_powershell`."""
    exe = store_cli_path()
    try:
        info = os.lstat(exe)
    except OSError as exc:
        raise UpdatesError(
            "Nie znaleziono polecenia store.exe (Microsoft Store).", kind="start"
        ) from exc
    if getattr(info, "st_reparse_tag", None) != _IO_REPARSE_TAG_APPEXECLINK:
        raise UpdatesError(
            f"{exe} nie jest aliasem aplikacji Microsoft Store — nie uruchamiam go.", kind="start"
        )
    try:
        proc = subprocess.Popen(
            [str(exe), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=winget.CREATE_NO_WINDOW,
            env=_child_env({"NO_COLOR": "1"}),
        )
    except OSError as exc:
        raise UpdatesError(f"Nie udało się uruchomić store.exe: {exc}", kind="start") from exc
    result: dict[str, bytes] = {}

    def communicate() -> None:
        try:
            result["out"] = proc.communicate()[0] or b""
        except (OSError, ValueError):
            result["out"] = b""

    reader = threading.Thread(target=communicate, name="winstaller-store-cli", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    while reader.is_alive():
        reader.join(_POLL_SECONDS)
        if not reader.is_alive():
            break
        if cancel_event is not None and cancel_event.is_set():
            _kill(proc)
            reader.join(_KILL_WAIT_SECONDS)
            raise UpdatesCancelled()
        if time.monotonic() > deadline:
            _kill(proc)
            reader.join(_KILL_WAIT_SECONDS)
            raise UpdatesError(f"store.exe nie zakończył się w {int(timeout)} s.", kind="timeout")
    return (
        proc.returncode if proc.returncode is not None else -1,
        winget.decode_console_bytes(result.get("out", b"")),
    )


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


SID_ADMINISTRATORS = "S-1-5-32-544"
SID_SYSTEM = "S-1-5-18"
SID_TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
#: Owners accepted for an existing log folder.
TRUSTED_OWNERS = frozenset({SID_ADMINISTRATORS, SID_SYSTEM, SID_TRUSTED_INSTALLER})
#: The only SIDs an existing log folder may grant any write-type right to.
TRUSTED_WRITERS = frozenset({SID_ADMINISTRATORS, SID_SYSTEM})
#: Protected DACL (no inheritance) set when the folder is created: full control for
#: Administrators and SYSTEM, read/list for Users. No owner in the SDDL: an elevated
#: token's default owner is the Administrators group.
LOG_DIR_SDDL = "D:P(A;OICI;FA;;;BA)(A;OICI;FA;;;SY)(A;OICI;0x1200a9;;;BU)"
_WRITE_RIGHTS = (
    0x0002  # FILE_WRITE_DATA / FILE_ADD_FILE
    | 0x0004  # FILE_APPEND_DATA / FILE_ADD_SUBDIRECTORY
    | 0x0010  # FILE_WRITE_EA
    | 0x0040  # FILE_DELETE_CHILD
    | 0x0100  # FILE_WRITE_ATTRIBUTES
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x02000000  # MAXIMUM_ALLOWED
    | 0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
)
_ERROR_ALREADY_EXISTS = 183


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_uint32),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    ]


def create_secure_dir(path: Path, sddl: str = LOG_DIR_SDDL) -> bool:
    """``CreateDirectoryW`` with the security descriptor ``sddl`` applied atomically at
    creation. True = created; False = something already exists at ``path`` (it is
    never modified). Other failures raise ``OSError``."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
    convert.restype = ctypes.c_int
    kernel32.CreateDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
    kernel32.CreateDirectoryW.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    descriptor = ctypes.c_void_p()
    if not convert(sddl, 1, ctypes.byref(descriptor), None):  # SDDL_REVISION_1
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, 0)
        if kernel32.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            return True
        error = ctypes.get_last_error()
        if error == _ERROR_ALREADY_EXISTS:
            return False
        raise ctypes.WinError(error)
    finally:
        kernel32.LocalFree(descriptor)


def _sid_string(sid: int | None) -> str:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    out = ctypes.c_wchar_p()
    if not sid or not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(out)):
        return ""
    try:
        return out.value or ""
    finally:
        kernel32.LocalFree(ctypes.cast(out, ctypes.c_void_p))


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("AceType", ctypes.c_ubyte),
        ("AceFlags", ctypes.c_ubyte),
        ("AceSize", ctypes.c_ushort),
    ]


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("AceCount", ctypes.c_uint32),
        ("AclBytesInUse", ctypes.c_uint32),
        ("AclBytesFree", ctypes.c_uint32),
    ]


def read_dir_security(path: Path) -> tuple[str, list[tuple[int, int, str]]] | None:
    """``(owner SID, [(ace type, access mask, SID), ...])`` of ``path`` through
    ``GetNamedSecurityInfoW``; ``None`` when it cannot be read or the DACL is NULL
    (which allows everyone everything)."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = advapi32.GetNamedSecurityInfoW
    get_info.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_uint32] + [ctypes.c_void_p] * 5
    get_info.restype = ctypes.c_uint32
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    advapi32.GetAce.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    # SE_FILE_OBJECT = 1; OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION = 1 | 4
    if get_info(
        str(path),
        1,
        5,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    ):
        return None
    try:
        if not dacl.value:
            return None
        size = _AclSizeInformation()
        if not advapi32.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
            return None
        aces: list[tuple[int, int, str]] = []
        for index in range(size.AceCount):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                return None
            header = _AceHeader.from_address(ace.value)
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            sid = _sid_string(ace.value + 8) if header.AceType in (0, 1) else ""
            aces.append((header.AceType, mask, sid))
        return _sid_string(owner.value), aces
    finally:
        kernel32.LocalFree(descriptor)


def security_is_trusted(owner: str, aces: Sequence[tuple[int, int, str]]) -> bool:
    """Pure rule for an existing log folder: owner in :data:`TRUSTED_OWNERS`; every
    allow ACE granting any write-type right is for :data:`TRUSTED_WRITERS`; ACE types
    other than plain allow (0) / deny (1) make it untrusted."""
    if owner not in TRUSTED_OWNERS:
        return False
    for ace_type, mask, sid in aces:
        if ace_type == 1:
            continue
        if ace_type != 0:
            return False
        if mask & _WRITE_RIGHTS and sid not in TRUSTED_WRITERS:
            return False
    return True


def dir_is_trusted(path: Path) -> bool:
    """An existing folder may hold the elevated installer's files: a real directory
    (not a reparse point), trusted owner and nobody but Administrators/SYSTEM can write."""
    if _is_reparse_point(path) or not path.is_dir():
        return False
    security = read_dir_security(path)
    return security is not None and security_is_trusted(*security)


def _write_probe(folder: Path) -> bool:
    probe = folder / f".winstaller-probe-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        with open(probe, "x", encoding="ascii"):
            pass
        probe.unlink()
    except OSError:
        return False
    return True


def _secure_level(path: Path) -> bool:
    """Create ``path`` with the protected DACL, or accept it when it already exists and
    is trusted. Never follows or repairs anything that is there."""
    if _is_reparse_point(path):
        return False
    try:
        created = create_secure_dir(path)
    except OSError:
        return False
    return created or dir_is_trusted(path)


def _secure_tree(root: Path) -> Path | None:
    logs = root / "logs"
    if not _secure_level(root) or not _secure_level(logs):
        return None
    return logs if _write_probe(logs) else None


def _fresh_tree(base: Path) -> Path | None:
    for _attempt in range(5):
        root = base / f"Winstaller-{secrets.token_hex(4)}"
        try:
            if not create_secure_dir(root) or not create_secure_dir(root / "logs"):
                continue
        except OSError:
            return None
        if _write_probe(root / "logs"):
            return root / "logs"
    return None


def _plain_tree(root: Path) -> Path | None:
    """Non-elevated: ``root\\logs`` without ACL changes; no level may be a link."""
    logs = root / "logs"
    for level in (root, logs):
        if _is_reparse_point(level):
            return None
        try:
            level.mkdir(exist_ok=True)
        except OSError:
            return None
        if _is_reparse_point(level) or not level.is_dir():
            return None
    return logs if _write_probe(logs) else None


def log_dir() -> Path:
    """Folder for background update logs.

    Elevated: ``%ProgramData%\\Winstaller\\logs``, each level created by us with a
    protected DACL (``LOG_DIR_SDDL``) or, when it exists, used only if
    :func:`dir_is_trusted`. An untrusted existing tree is never repaired: a fresh
    ``%ProgramData%\\Winstaller-<random>\\logs`` is created instead (warning in the
    Python log). Not elevated (dev ``--no-elevate``; no installation is started then):
    ``%LOCALAPPDATA%\\Winstaller\\logs``. Raises :class:`UpdatesError`.
    """
    if not plat.is_admin():
        root = plat.local_app_data_dir() / "Winstaller"
        folder = _plain_tree(root)
        if folder is None:
            raise UpdatesError(
                f"Nie udało się przygotować katalogu na log aktualizacji ({root}).", kind="start"
            )
        return folder
    base = plat.program_data_dir()
    folder = _secure_tree(base / "Winstaller")
    if folder is not None:
        return folder
    _log.warning("Untrusted %s; using a fresh folder for update logs", base / "Winstaller")
    folder = _fresh_tree(base)
    if folder is None:
        raise UpdatesError(
            "Nie udało się przygotować bezpiecznego katalogu na log aktualizacji w "
            f"{base} ({base / 'Winstaller'} jest niezaufany lub niedostępny).",
            kind="start",
        )
    return folder


def existing_log_dirs() -> list[Path]:
    """Trusted log folders that already exist (read-only: nothing is created or
    changed). Used to find the status file of a running installation."""
    found: list[Path] = []
    base = plat.program_data_dir()
    try:
        roots = [base / "Winstaller", *sorted(base.glob("Winstaller-*"))]
    except OSError:
        roots = [base / "Winstaller"]
    for root in roots:
        logs = root / "logs"
        if dir_is_trusted(root) and dir_is_trusted(logs):
            found.append(logs)
    if not plat.is_admin():
        logs = plat.local_app_data_dir() / "Winstaller" / "logs"
        if logs.is_dir() and not _is_reparse_point(logs.parent) and not _is_reparse_point(logs):
            found.append(logs)
    return found


_STILL_ACTIVE = 259


def process_image(pid: int) -> str | None:
    """File name (lower case) of the running process ``pid``; ``None`` when it does not
    run (or cannot be queried)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
        ctypes.c_void_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        code = ctypes.c_uint32()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        if code.value != _STILL_ACTIVE:
            return None
        buffer = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_uint32(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return Path(buffer.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)


def process_running(pid: int) -> bool:
    for proc in _BACKGROUND:
        if proc.pid == pid:
            return proc.poll() is None
    return process_image(pid) is not None


def is_live_powershell(pid: int) -> bool:
    return process_image(pid) == "powershell.exe"


def background_exit_code(pid: int) -> int | None:
    """Exit code of a background process we started (``None``: running or unknown)."""
    for proc in _BACKGROUND:
        if proc.pid == pid:
            return proc.poll()
    return None


def read_status(path: Path) -> dict[str, Any] | None:
    """The background script's status JSON, or ``None`` (missing, partial, garbage)."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(1_000_001)
    except OSError:
        return None
    if len(raw) > 1_000_000:
        return None
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("winstaller") != "wu-install":
        return None
    return data


# ------------------------------------------------------------------ Windows Update


def scan_windows_updates(
    *, cancel_event: threading.Event | None = None, timeout: float = SCAN_TIMEOUT
) -> WindowsUpdateScan:
    """Read-only search for every applicable update (regular, optional/Preview,
    drivers). Raises :class:`UpdatesError` (:class:`UpdatesCancelled` on cancel)."""
    returncode, stdout, stderr = run_powershell(
        update_scripts.WU_SCAN_SCRIPT, timeout=timeout, cancel_event=cancel_event
    )
    return parse_wu_scan(stdout, stderr=stderr, returncode=returncode)


def background_install_running() -> bool:
    return mutex_exists(MUTEX_NAME)


_STAMP_NAME = re.compile(
    rf"{re.escape(LOG_PREFIX)}(\d{{4}})\.(\d{{2}})\.(\d{{2}})-(\d{{6}})(?:-(\d{{1,2}}))?\.(?:log|json)"
)


def _log_sort_key(path: Path) -> tuple[tuple[int, ...], float]:
    """Natural order of ``windows-update-<stamp>[-n]`` (``-2`` after the plain name),
    then modification time."""
    match = _STAMP_NAME.fullmatch(path.name)
    natural = tuple(int(g or 1) for g in match.groups()) if match else (0,)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return natural, mtime


def latest_log(folder: Path) -> Path | None:
    try:
        logs = sorted(folder.glob(f"{LOG_PREFIX}*.log"), key=_log_sort_key)
    except OSError:
        return None
    return logs[-1] if logs else None


BLOCKED_MESSAGE = (
    "Inny proces blokuje aktualizację Windows: istnieje blokada {name}, ale nie znaleziono "
    "trwającej instalacji Winstallera (pliku statusu z działającym procesem PowerShell). "
    "Uruchom ponownie komputer albo zamknij program, który ją trzyma, i spróbuj ponownie."
)


def running_install() -> BackgroundInstall:
    """The installation holding the mutex, confirmed by our own status file
    (``state = running`` with the PID of a live ``powershell.exe``) in a trusted log
    folder. Read-only. Not confirmed -> :class:`UpdatesError` (``kind="blocked"``)."""
    for folder in existing_log_dirs():
        try:
            statuses = sorted(folder.glob(f"{LOG_PREFIX}*.json"), key=_log_sort_key, reverse=True)
        except OSError:
            continue
        for status in statuses[:20]:
            data = read_status(status)
            pid = data.get("pid") if data else None
            if (
                data is not None
                and data.get("state") == "running"
                and isinstance(pid, int)
                and not isinstance(pid, bool)
                and is_live_powershell(pid)
            ):
                return BackgroundInstall(
                    log_path=status.with_suffix(".log"),
                    status_path=status,
                    pid=pid,
                    already_running=True,
                )
    raise UpdatesError(BLOCKED_MESSAGE.format(name=MUTEX_NAME), kind="blocked")


#: Polish messages for the background script's exit codes (see update_scripts).
EXIT_MESSAGES: Mapping[int, str] = {
    1: "Proces instalacji aktualizacji zakończył się nieoczekiwanym błędem.",
    2: "Część aktualizacji nie została zainstalowana.",
    3: "Instalacja aktualizacji Windows już trwa w innym procesie.",
    4: "Instalacja aktualizacji Windows wymaga uprawnień administratora.",
    5: "Windows wymaga ponownego uruchomienia komputera przed instalacją kolejnych aktualizacji.",
    6: "Windows Update jest zajęty inną instalacją — spróbuj ponownie za kilka minut.",
    7: "Nieprawidłowe parametry procesu instalacji (błąd Winstallera).",
}


def describe_exit(code: int | None, log_path: Path) -> UpdatesError:
    if code is None:
        text = "Proces instalacji aktualizacji zakończył się od razu, bez zapisania statusu."
    else:
        text = EXIT_MESSAGES.get(
            code, f"Proces instalacji aktualizacji zakończył się błędem (kod {code})."
        )
        if code in EXIT_MESSAGES:
            text = text.rstrip(".") + f" (kod {code})."
    return UpdatesError(f"{text} Log: {log_path}", kind="start")


def verify_background_start(
    started: BackgroundInstall,
    *,
    cancel_event: threading.Event | None = None,
    timeout: float = START_CONFIRM_TIMEOUT,
    poll: float = 0.1,
) -> BackgroundInstall:
    """Wait (up to ``timeout`` s, reacting to cancel) for the first status write of a
    freshly started background process. Confirmed when the status says ``running``
    for this PID and the process is alive, or ``finished`` with code 0 (returned with
    ``finished=True``). An early exit (code 3-7, other, or no status at all) or no
    confirmation in time -> :class:`UpdatesError` with the log path; cancel ->
    :class:`UpdatesCancelled` (the process may still be running)."""
    if started.already_running or started.pid is None or started.status_path is None:
        return started
    pid, status_path = started.pid, started.status_path
    deadline = time.monotonic() + timeout

    def judge(data: dict[str, Any] | None) -> BackgroundInstall | None:
        if data is None or data.get("pid") != pid:
            return None
        if data.get("state") == "finished":
            code = data.get("exitCode")
            code = code if isinstance(code, int) and not isinstance(code, bool) else None
            if code == 0:
                return BackgroundInstall(
                    started.log_path,
                    status_path,
                    pid,
                    install_all=started.install_all,
                    finished=True,
                )
            raise describe_exit(code, started.log_path)
        if data.get("state") == "running" and process_running(pid):
            return started
        return None

    while True:
        confirmed = judge(read_status(status_path))
        if confirmed is not None:
            return confirmed
        if not process_running(pid):
            confirmed = judge(read_status(status_path))  # it may have just finished
            if confirmed is not None:
                return confirmed
            raise describe_exit(background_exit_code(pid), started.log_path)
        if time.monotonic() > deadline:
            raise UpdatesError(
                f"Proces instalacji aktualizacji (PID {pid}) nie potwierdził startu w "
                f"{int(timeout)} s. Sprawdź log: {started.log_path}",
                kind="start",
            )
        if cancel_event is not None:
            if cancel_event.wait(poll):
                raise UpdatesCancelled(
                    "Przerwano oczekiwanie na potwierdzenie startu; instalacja aktualizacji "
                    f"mogła już ruszyć w tle (log: {started.log_path})."
                )
        else:
            time.sleep(poll)


_START_LOCK = threading.Lock()
_start_generation = 0
_last_started: BackgroundInstall | None = None


def start_windows_update_install(
    *,
    update_ids: Sequence[str] = (),
    install_all: bool = False,
    skip_drivers: bool = SKIP_DRIVERS,
    skip_display_drivers: bool = SKIP_DISPLAY_DRIVERS,
    skip_interactive: bool = SKIP_INTERACTIVE,
    now: datetime | None = None,
) -> BackgroundInstall:
    """Start the detached download + install process (``WU_INSTALL_SCRIPT``).

    It searches again itself and installs only ``update_ids``; installing everything
    found requires an explicit ``install_all=True`` (an empty list alone is an error).
    An allow-list longer than :data:`MAX_IDS_CHARS` switches to ``install_all``
    (``BackgroundInstall.install_all``). When an install is already running (mutex
    confirmed by its status file), nothing new starts and ``already_running`` is set;
    an unconfirmed mutex -> :class:`UpdatesError` (``kind="blocked"``). Concurrent
    calls in this process start one process (lock + generation check). The start is
    not verified here: see :func:`verify_background_start`. Raises
    :class:`UpdatesError`.
    """
    global _start_generation, _last_started
    ids = validate_update_ids(update_ids)
    if not ids and not install_all:
        raise UpdatesError(
            "Brak listy aktualizacji do instalacji (pusta lista bez install_all).", kind="format"
        )
    joined = ",".join(ids)
    if len(joined) > MAX_IDS_CHARS:
        install_all, ids, joined = True, [], ""
    generation = _start_generation
    if background_install_running():
        return running_install()
    with _START_LOCK:
        # Re-check inside the critical section: another thread may have started the
        # process while we waited (its mutex may not exist yet, PowerShell starts slowly).
        if _start_generation != generation and _last_started is not None:
            last = _last_started
            return BackgroundInstall(
                last.log_path, last.status_path, last.pid, already_running=True
            )
        folder = log_dir()
        if not folder.is_absolute():
            raise UpdatesError(
                f"Katalog logów musi być ścieżką bezwzględną: {folder}", kind="start"
            )
        stamp_time = now or datetime.now()
        log_name, status_name = log_file_names(stamp_time)
        log_path = folder / log_name
        for n in range(2, 100):
            if not log_path.exists() and not (folder / status_name).exists():
                break
            stem = f"{LOG_PREFIX}{stamp_time.strftime(TIMESTAMP_FORMAT)}-{n}"
            log_path, status_name = folder / f"{stem}.log", f"{stem}.json"
        status_path = folder / status_name
        count = "wszystkie" if install_all else str(len(ids))
        try:
            # Exclusive create: never write through a file (or link) that already exists.
            with open(log_path, "x", encoding="utf-8-sig", newline="\r\n") as fh:
                fh.write(
                    f"{stamp_time:%Y-%m-%d %H:%M:%S} Winstaller uruchamia instalację "
                    f"aktualizacji Windows w tle ({count} pozycji).\n"
                )
                if folder.parent.name.startswith("Winstaller-"):
                    fh.write(
                        f"{stamp_time:%Y-%m-%d %H:%M:%S} Uwaga: katalog Winstaller w "
                        f"{folder.parent.parent} był niezaufany — log zapisywany w {folder}.\n"
                    )
        except OSError as exc:
            raise UpdatesError(
                f"Nie udało się utworzyć logu {log_path}: {exc}", kind="start"
            ) from exc
        env = {
            ENV_WU_LOG: str(log_path),
            ENV_WU_STATUS: str(status_path),
            ENV_WU_IDS: joined,
            ENV_WU_ALL: "1" if install_all else "0",
            ENV_WU_SKIP_DRIVERS: "1" if skip_drivers else "0",
            ENV_WU_SKIP_DISPLAY_DRIVERS: "1" if skip_display_drivers else "0",
            ENV_WU_SKIP_INTERACTIVE: "1" if skip_interactive else "0",
        }
        try:
            args = powershell_args(update_scripts.WU_INSTALL_SCRIPT, hidden=True)
            pid = start_background(args, env)
        except (OSError, UpdatesError) as exc:
            with contextlib.suppress(OSError):
                log_path.unlink()  # no orphaned log of a process that never ran
            if isinstance(exc, UpdatesError):
                raise
            if isinstance(exc, FileNotFoundError):
                raise UpdatesError(f"Nie znaleziono PowerShella: {exc}", kind="start") from exc
            raise UpdatesError(
                f"Nie udało się uruchomić instalacji aktualizacji w tle: {exc}", kind="start"
            ) from exc
        result = BackgroundInstall(
            log_path=log_path, status_path=status_path, pid=pid, install_all=install_all
        )
        _last_started = result
        _start_generation += 1
        return result


# ------------------------------------------------------------------ Microsoft Store


def _store_env(mode: Literal["list", "update"], timeout: float) -> dict[str, str]:
    return {ENV_STORE_MODE: mode, ENV_STORE_TIMEOUT: str(max(1, int(timeout)))}


def list_store_queue(
    *, cancel_event: threading.Event | None = None, timeout: float = STORE_TIMEOUT
) -> StoreUpdateResult:
    """Read the Store install queue only (no search, no changes)."""
    returncode, stdout, stderr = run_powershell(
        update_scripts.STORE_SCRIPT,
        env=_store_env("list", timeout),
        timeout=timeout + 30,
        cancel_event=cancel_event,
    )
    return parse_store_update(stdout, stderr=stderr, returncode=returncode)


def update_store_apps(
    *,
    cancel_event: threading.Event | None = None,
    timeout: float = STORE_TIMEOUT,
    log: LineCallback | None = None,
) -> StoreUpdateResult:
    """Ask the Store to find and install every app update; returns right away.

    Primary: ``AppInstallManager.SearchForAllUpdatesAsync`` (PowerShell, WinRT);
    fallback ``store.exe updates --apply`` only when the primary failed before the
    request (load/COM error, rejected request, no output) — never after a timeout, when
    the request may already have been accepted. Both failing -> :class:`UpdatesError`
    with the manual steps. Policy block -> :class:`UpdatesError` without the fallback.
    """
    note = log or (lambda _text: None)
    try:
        returncode, stdout, stderr = run_powershell(
            update_scripts.STORE_SCRIPT,
            env=_store_env("update", timeout),
            timeout=timeout + 30,
            cancel_event=cancel_event,
        )
        return parse_store_update(stdout, stderr=stderr, returncode=returncode)
    except UpdatesCancelled:
        raise
    except UpdatesError as exc:
        if exc.kind == "policy":
            raise UpdatesError(f"{exc.message} {STORE_MANUAL_HINT}", kind="policy") from exc
        if exc.kind == "timeout":
            # The request may already have reached the Store: running store.exe as well
            # could queue everything twice, so stop here.
            raise UpdatesError(
                "Microsoft Store nie potwierdził zlecenia w wyznaczonym czasie — aktualizacje "
                "mogły zostać zlecone, ale nie udało się tego potwierdzić. Sprawdź Microsoft "
                "Store → Pobrane.",
                kind="timeout",
            ) from exc
        first = exc
    note(f"Usługa Microsoft Store nie przyjęła zlecenia ({first.message}). Próbuję store.exe…")
    try:
        returncode, output = run_store_cli(
            ["updates", "--apply"], timeout=STORE_CLI_TIMEOUT, cancel_event=cancel_event
        )
        return parse_store_cli_output(output, returncode)
    except UpdatesCancelled:
        raise
    except UpdatesError as exc:
        raise UpdatesError(
            f"Nie udało się zlecić aktualizacji aplikacji Microsoft Store ({first.message}; "
            f"{exc.message}). {STORE_MANUAL_HINT}",
            kind="store",
        ) from exc


# ------------------------------------------------------------------ dry-run samples

#: Data for the dry-run simulation (runner); shaped like a real scan on Windows 11.
DRY_RUN_WINDOWS_UPDATES: tuple[WindowsUpdateInfo, ...] = (
    WindowsUpdateInfo(
        id="71ab9cfc-4bdf-48b7-b7ad-7f4ce4605cd7",
        revision=1,
        title="2026-11 Aktualizacja wersji Preview (KB5132057) (26300.10075)",
        kb=("KB5132057",),
        optional=True,
    ),
    WindowsUpdateInfo(
        id="9e5d0dcd-ff37-4266-a310-a874988f9bd0",
        revision=1,
        title="2026-11 Aktualizacja wersji Preview platformy .NET Framework (KB5137006)",
        kb=("KB5137006",),
        optional=True,
    ),
    WindowsUpdateInfo(
        id="2f6e1f3a-5b7c-4d8e-9f10-1a2b3c4d5e6f",
        revision=200,
        title="Aktualizacja analizy zabezpieczeń dla programu Microsoft Defender (KB2267602)",
        kb=("KB2267602",),
    ),
    WindowsUpdateInfo(
        id="702e6ed7-4f31-4f3b-9f49-de333467baf8",
        revision=1,
        title="Microsoft Corporation AudioProcessingObject Driver Update (1.0.4.55794)",
        type="driver",
        optional=True,
        driver_class="OtherHardware",
    ),
)
DRY_RUN_STORE_APPS: tuple[StoreAppInfo, ...] = (
    StoreAppInfo("9WZDNCRFJBMP", "Microsoft.WindowsStore_8wekyb3d8bbwe", "Update", "Pending"),
    StoreAppInfo("9MSMLRH6LZF3", "Microsoft.Paint_8wekyb3d8bbwe", "Update", "Pending"),
    StoreAppInfo("9MV0B5HZVK9Z", "Microsoft.GamingApp_8wekyb3d8bbwe", "Update", "Pending"),
)
DRY_RUN_LOG_PATH = Path(r"C:\ProgramData\Winstaller\logs\windows-update-2026.11.22-120000.log")
