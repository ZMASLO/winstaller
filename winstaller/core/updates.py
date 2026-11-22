"""Windows Update and Microsoft Store app updates (no Qt, synchronous).

* Windows Update: :func:`scan_windows_updates` runs a read-only WUA search (all
  updates: regular, optional/Preview and drivers) in PowerShell; then
  :func:`start_windows_update_install` starts a detached, hidden PowerShell process
  that downloads and installs them through the same API and exits. Winstaller never
  waits for it and never restarts the machine; it follows it read-only through the
  status file (:func:`read_install_progress`, :func:`find_background_install`).
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
import dataclasses
import functools
import html
import json
import logging
import math
import msvcrt
import os
import re
import secrets
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
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

#: Where the user sees the background installation (Windows Settings does not show an
#: installation started through the WUA API).
PROGRESS_HINT = "Postęp: zakładka Przebieg (blok „W tle”)"

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


def process_create_time(pid: int) -> datetime | None:
    """Creation time (aware, local) of process ``pid`` (``GetProcessTimes``); ``None``
    when it cannot be queried."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 4
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        times = [ctypes.c_uint64() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        filetime = times[0].value  # 100 ns since 1601-01-01 UTC
    finally:
        kernel32.CloseHandle(handle)
    if not filetime:
        return None
    seconds = filetime / 10_000_000 - 11_644_473_600
    try:
        return datetime.fromtimestamp(seconds).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


#: The installer writes ``started`` itself right after it starts: its process must be
#: created at most this much before ...
INSTALLER_START_BEFORE = timedelta(minutes=10)
#: ... and not after (clock rounding); a later process reuses the PID of a dead one.
INSTALLER_START_AFTER = timedelta(seconds=60)


def is_installer_process(pid: int, started: datetime | None) -> bool:
    """``pid`` is a live ``powershell.exe`` that can be the installer whose status says
    ``started``: created during this boot and within [started - 10 min, started + 60 s]
    (a PID reused by another PowerShell - even Winstaller's own scan - is not it)."""
    if not is_live_powershell(pid):
        return False
    if started is None:
        return True
    try:
        if started < system_boot_time() - timedelta(seconds=5):
            return False
    except (OSError, OverflowError, ValueError, AttributeError):
        pass
    created = process_create_time(pid)
    if created is None:
        return True  # cannot be disproved (the image check passed)
    return started - INSTALLER_START_BEFORE <= created <= started + INSTALLER_START_AFTER


def background_exit_code(pid: int) -> int | None:
    """Exit code of a background process we started (``None``: running or unknown)."""
    for proc in _BACKGROUND:
        if proc.pid == pid:
            return proc.poll()
    return None


_GENERIC_READ = 0x80000000
#: FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
_SHARE_ALL = 0x7
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_HANDLE = ctypes.c_void_p(-1).value
MAX_STATUS_BYTES = 1_000_000
MAX_LOG_BYTES = 4_000_000


def read_shared(path: Path, limit: int) -> bytes:
    """Up to ``limit + 1`` bytes of ``path``, opened with read/write/delete sharing.

    Python's ``open`` does not share delete access, and ``File.Replace`` in the
    background script needs it: a status file held open by our reader would make the
    script's write fail. The script also appends to the log while we read it. Raises
    ``OSError`` (``FileNotFoundError`` when missing)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = create(
        str(path), _GENERIC_READ, _SHARE_ALL, None, _OPEN_EXISTING, _FILE_ATTRIBUTE_NORMAL, None
    )
    if not handle or handle == _INVALID_HANDLE:
        error = ctypes.get_last_error()
        raise ctypes.WinError(error) if error not in (2, 3) else FileNotFoundError(error, str(path))
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        kernel32.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as fh:
        return fh.read(limit + 1)


def system_boot_time() -> datetime:
    """Local, timezone-aware time of the last system start (``GetTickCount64``, which
    keeps counting during sleep; a Fast Startup "shutdown" is a hibernation and does
    not reset it, a restart does)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetTickCount64.restype = ctypes.c_uint64
    return datetime.now().astimezone() - timedelta(milliseconds=kernel32.GetTickCount64())


def read_status(path: Path) -> dict[str, Any] | None:
    """The background script's status JSON, or ``None`` (missing, partial, garbage)."""
    try:
        raw = read_shared(path, MAX_STATUS_BYTES)
    except OSError:
        return None
    if len(raw) > MAX_STATUS_BYTES:
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
                and is_installer_process(pid, _local_time(data.get("started")))
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


# ------------------------------------------------------------ background install progress

#: "unknown" is a final state: the installation's state cannot be established (status
#: "running" without a PID and no result in the log, the newest status file unreadable,
#: the followed status file lost).
InstallState = Literal["running", "finished", "stale", "unknown"]
#: "unknown": a status file from before progress was reported (no ``phase`` field).
InstallPhase = Literal["searching", "downloading", "installing", "finished", "unknown"]
ItemStatus = Literal[
    "pending",
    "downloading",
    "downloaded",
    "installing",
    "succeeded",
    "succeeded_with_errors",
    "failed",
    "aborted",
    "not_downloaded",
    "error",
    "unknown",
    "skipped",
]
ITEM_STATUSES: frozenset[str] = frozenset(ItemStatus.__args__)  # type: ignore[attr-defined]
ITEM_OK: frozenset[str] = frozenset({"succeeded", "succeeded_with_errors"})
#: "unknown" (a result code other than 2-5 after a synchronous Install()) counts as a
#: failure: a false failure is better than a false success.
ITEM_FAILED: frozenset[str] = frozenset({"failed", "aborted", "not_downloaded", "error", "unknown"})
#: Finished updates. "skipped" (closed by the script's ``finally``: never got its turn)
#: is not among them: like "pending" it counts as skipped after the end.
ITEM_DONE: frozenset[str] = ITEM_OK | ITEM_FAILED
_PHASES = frozenset({"searching", "downloading", "installing", "finished"})
_BUSY = frozenset({"downloading", "installing"})
#: Read attempts of a status file (the script renames over it; a reader may hit the
#: moment of the rename: "not found" or a sharing violation for microseconds).
READ_ATTEMPTS = 5
READ_RETRY_DELAY = 0.05
#: Newest status files :func:`find_background_install` tries.
FIND_CANDIDATES = 5
#: A live installer whose last status write is older than this while its log has newer
#: lines: its status writes fail, the view is completed from the log.
LOG_AHEAD = timedelta(minutes=2)
LOST_MESSAGE = "Nie można odczytać stanu instalacji — sprawdź log."


@dataclass(frozen=True, slots=True)
class InstallItem:
    """One update of a background installation (from the status file or the log)."""

    title: str
    status: ItemStatus = "pending"
    id: str = ""  # lowercase GUID; "" when known only from the log
    revision: int = 0
    hresult: str | None = None
    reboot_required: bool = False

    @property
    def done(self) -> bool:
        return self.status in ITEM_DONE

    @property
    def ok(self) -> bool:
        return self.status in ITEM_OK

    @property
    def failed(self) -> bool:
        return self.status in ITEM_FAILED


@dataclass(frozen=True, slots=True)
class InstallProgress:
    """Snapshot of the background Windows Update installation.

    ``state``: ``running`` (status says so and, after :func:`read_install_progress`,
    the process is the live installer: ``alive``), ``finished`` (the script wrote its
    result), ``stale`` (status says running but the process is gone: it ended without
    writing the result; ``from_log`` when the result was recovered from the log),
    ``unknown`` (final: the state cannot be established, ``error`` says why).
    ``current``/``total``: 1..N in install order; ``download_current``/
    ``download_total``: the downloads only. ``from_log`` on a running one: the status
    file lags behind the log and the view comes from the log.
    """

    state: InstallState
    phase: InstallPhase = "unknown"
    total: int = 0
    current: int = 0
    current_title: str = ""
    download_total: int = 0
    download_current: int = 0
    items: tuple[InstallItem, ...] = ()
    reboot_required: bool = False
    exit_code: int | None = None
    error: str | None = None
    started: datetime | None = None
    finished: datetime | None = None
    updated: datetime | None = None
    phase_started: datetime | None = None
    step_started: datetime | None = None
    log_path: Path | None = None
    status_path: Path | None = None
    pid: int | None = None
    alive: bool = False
    from_log: bool = False

    @property
    def done(self) -> bool:
        """Nothing more will happen (result written, recovered, lost or unknown)."""
        return self.state in ("finished", "stale", "unknown")

    @property
    def ok_count(self) -> int:
        return sum(1 for i in self.items if i.ok)

    @property
    def failed_count(self) -> int:
        """Failed updates; after the end also those caught mid-download/installation."""
        return sum(1 for i in self.items if i.failed or (self.done and i.status in _BUSY))

    @property
    def skipped_count(self) -> int:
        """After the end: updates that never got their turn (pending, downloaded,
        skipped)."""
        if not self.done:
            return 0
        return sum(1 for i in self.items if not i.done and i.status not in _BUSY)

    @property
    def count(self) -> int:
        return max(self.total, len(self.items))

    @property
    def needs_restart(self) -> bool:
        """Global flag, or any single update asks for a restart."""
        return self.reboot_required or any(i.reboot_required for i in self.items)

    @property
    def succeeded(self) -> bool:
        """A clean success: ended with exit code 0, nothing failed, nothing left out."""
        return (
            self.state in ("finished", "stale")
            and self.exit_code == 0
            and self.failed_count == 0
            and self.skipped_count == 0
        )

    @property
    def steps_total(self) -> int:
        return self.download_total + self.count

    @property
    def steps_done(self) -> int:
        """Finished steps (downloads, then installations) for a step progress bar."""
        if self.done:
            return self.steps_total
        if self.phase in ("installing", "finished"):
            downloads = self.download_total
            installs = sum(1 for i in self.items if i.done)
        else:
            busy = any(i.status == "downloading" for i in self.items)
            downloads = max(0, self.download_current - (1 if busy else 0))
            # A failed download is already counted as a finished download step.
            installs = sum(1 for i in self.items if i.done and i.status != "not_downloaded")
        return min(self.steps_total, downloads + installs)

    @property
    def key(self) -> str:
        """Stable identity of this installation (status file name), e.g. for "hide"."""
        if self.status_path is not None:
            return self.status_path.name
        if self.log_path is not None:
            return self.log_path.name
        return ""

    @property
    def reference_time(self) -> datetime | None:
        """When it last did something: finished, else last status write, else start."""
        return self.finished or self.updated or self.started


@dataclass(frozen=True, slots=True)
class InstallLogSummary:
    """What :func:`parse_install_log` found in a background installation log.

    ``positions[k]``: 1-based position in the status ``updates[]`` of ``items[k]``
    (the ``[i]`` marker), ``None`` in logs written before the marker. ``phase``,
    ``current_index``, ``current_title``: the last step started and not finished
    ("downloading"/"installing"), ``None`` when none is open."""

    exit_code: int | None = None  # last "Koniec (kod N)."
    items: tuple[InstallItem, ...] = ()
    positions: tuple[int | None, ...] = ()
    reboot_required: bool = False
    pid: int | None = None
    found: int | None = None
    started: datetime | None = None
    finished: datetime | None = None  # time of the "Koniec" line
    last: datetime | None = None  # time of the last line of ours
    phase: Literal["downloading", "installing"] | None = None
    current_index: int | None = None
    current_title: str = ""
    download_current: int = 0
    download_total: int = 0

    @property
    def ok_count(self) -> int:
        return sum(1 for i in self.items if i.ok)

    @property
    def failed_count(self) -> int:
        return sum(1 for i in self.items if i.failed)


def _local_time(value: object) -> datetime | None:
    """ISO 8601 text -> aware local datetime (naive text is taken as local time)."""
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    try:
        return parsed.astimezone()
    except (OverflowError, OSError, ValueError):
        return None


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return min(value, 100_000)


def _item_from(entry: object) -> InstallItem | None:
    if not isinstance(entry, dict):
        return None
    title = _clean_text(entry.get("title"))
    if not title:
        return None
    raw_id = entry.get("id")
    update_id = raw_id.strip().lower() if isinstance(raw_id, str) else ""
    raw_status = entry.get("status", entry.get("result"))  # "result": before 2.0 progress
    status = raw_status if isinstance(raw_status, str) and raw_status in ITEM_STATUSES else None
    if status is None:
        status = "pending" if raw_status is None else "unknown"
    revision = entry.get("revision")
    return InstallItem(
        title=title,
        status=status,  # type: ignore[arg-type]
        id=update_id if _GUID.fullmatch(update_id) else "",
        revision=revision if isinstance(revision, int) and not isinstance(revision, bool) else 0,
        hresult=format_hresult(hresult_value(entry.get("hresult"))),
        reboot_required=_bool(entry.get("rebootRequired")),
    )


@_parser
def parse_install_status(text: str, *, path: Path | None = None) -> InstallProgress:
    """The background script's status JSON -> :class:`InstallProgress` (pure: ``alive``
    is always False here, see :func:`read_install_progress`). Accepts files written
    before progress was reported (no ``phase``/``total``/``status`` fields: ``phase``
    is then "unknown", item results come from ``result``). Not our JSON, a ``state``
    other than running/finished, or garbage -> :class:`UpdatesError`."""
    if not isinstance(text, str) or len(text) > MAX_STATUS_BYTES:
        raise UpdatesError("Nieprawidłowy plik statusu instalacji.", kind="format")
    try:
        data = json.loads(text.lstrip("﻿"))
    except ValueError:
        raise UpdatesError(
            "Plik statusu instalacji nie jest poprawnym JSON-em.", kind="format"
        ) from None
    if not isinstance(data, dict) or data.get("winstaller") != "wu-install":
        raise UpdatesError(FOREIGN_STATUS, kind="format")
    state = data.get("state")
    if state not in ("running", "finished"):
        raise UpdatesError("Nieznany stan w pliku statusu instalacji.", kind="format")
    raw_items = data.get("updates")
    if isinstance(raw_items, dict):  # ConvertTo-Json may unroll a one-element array
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        raw_items = []
    items = tuple(i for i in (_item_from(e) for e in raw_items[:1000]) if i is not None)
    phase = data.get("phase")
    if phase not in _PHASES:
        phase = "finished" if state == "finished" else "unknown"
    total = max(_count(data.get("total")), len(items))
    download_total = _count(data.get("downloadTotal"))
    pid = data.get("pid")
    exit_code = data.get("exitCode")
    error = data.get("error")
    log_field = data.get("log")
    if path is not None:
        log_path: Path | None = path.with_suffix(".log")
    elif isinstance(log_field, str) and Path(log_field).is_absolute():
        log_path = Path(log_field)
    else:
        log_path = None
    return InstallProgress(
        state=state,
        phase=phase,
        total=total,
        current=min(_count(data.get("current")), total),
        current_title="" if state == "finished" else _clean_text(data.get("currentTitle")),
        download_total=download_total,
        download_current=min(_count(data.get("downloadCurrent")), download_total),
        items=items,
        reboot_required=_bool(data.get("rebootRequired")),
        exit_code=exit_code
        if isinstance(exit_code, int) and not isinstance(exit_code, bool)
        else None,
        error=_clean_text(error) or None,
        started=_local_time(data.get("started")),
        finished=_local_time(data.get("finished")),
        updated=_local_time(data.get("updated")),
        phase_started=_local_time(data.get("phaseStarted")),
        step_started=_local_time(data.get("stepStarted")),
        log_path=log_path,
        status_path=path,
        pid=pid if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 else None,
    )


FOREIGN_STATUS = "To nie jest plik statusu instalacji Winstallera."

# Every pattern is anchored on the WHOLE message (after the time stamp): an update
# title inside one of our lines can never pass for a result, a restart or an end.
_LOG_LINE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)")
_POS = r"(?: \[(\d{1,5})\])?"  # "[i]": position in updates[] (since the marker exists)
_LOG_INSTALL = re.compile(rf"Instalacja{_POS}: (.+)")
_LOG_DOWNLOAD = re.compile(rf"Pobieranie (\d{{1,5}}) z (\d{{1,5}}){_POS}: (.+)")
_LOG_DOWNLOADED = re.compile(r"-> (pobrano|nie pobrano \(0x[0-9A-Fa-f]{8}\))")
_LOG_SKIP = re.compile(rf"Nie pobrano, pomijam instalację{_POS}: (.+)")
_LOG_RESULT = re.compile(
    r"-> (?:\[(\d{1,5})\] )?(.+?) \((0x[0-9A-Fa-f]{8})\)(: .*|:|, wymaga restartu)?"
)
_LOG_END = re.compile(r"Koniec \(kod (-?\d{1,10})\)\.")
_LOG_START = re.compile(r"Start instalacji aktualizacji w tle \(PID (\d{1,10})\)\.")
_LOG_FOUND = re.compile(r"Znaleziono (\d{1,6}) aktualizacji\.")
_LOG_RESTART = (
    "Aby dokończyć instalację, uruchom ponownie komputer (Winstaller nie robi tego automatycznie)."
)
_LOG_RESULT_STATUS: Mapping[str, ItemStatus] = {
    "OK": "succeeded",
    "OK z błędami": "succeeded_with_errors",
    "błąd": "failed",
    "przerwano": "aborted",
}


def _position(text: str | None) -> int | None:
    return int(text) if text else None


@_parser
def parse_install_log(text: str) -> InstallLogSummary:
    """Result of a background installation from its log (pure). Used when the process
    ended without writing its status (and to catch up a lagging status): the last
    ``Koniec (kod N).``, every ``Instalacja [i]: <title>`` with its ``-> [i] <result>
    (0x…)`` line, skipped downloads, the restart notice and the last open step. Lines
    that are not ours are ignored; non-text input -> :class:`UpdatesError`."""
    if not isinstance(text, str):
        raise UpdatesError("Nieprawidłowy log instalacji.", kind="format")
    items: list[InstallItem] = []
    positions: list[int | None] = []
    pending: tuple[str, int | None] | None = None  # an "Instalacja" waiting for its result
    exit_code: int | None = None
    reboot = False
    pid: int | None = None
    found: int | None = None
    started: datetime | None = None
    finished: datetime | None = None
    last: datetime | None = None
    download: tuple[str, int | None] | None = None  # an open "Pobieranie"
    dl_current = dl_total = 0

    def add(title: str, pos: int | None, status: ItemStatus, **fields: Any) -> None:
        items.append(InstallItem(title, status, **fields))
        positions.append(pos)

    for raw in text.lstrip("﻿").split("\n"):
        match = _LOG_LINE.fullmatch(raw.strip())
        if not match:
            continue
        stamp, body = _local_time(match.group(1)), match.group(2).strip()
        started = started or stamp
        if m := _LOG_INSTALL.fullmatch(body):
            if pending is not None:  # no result line: the process died during it
                add(pending[0], pending[1], "unknown")
            pending = (_clean_text(m.group(2)) or "?", _position(m.group(1)))
            download = None
        elif m := _LOG_DOWNLOAD.fullmatch(body):
            dl_current, dl_total = int(m.group(1)), int(m.group(2))
            download = (_clean_text(m.group(4)) or "?", _position(m.group(3)))
        elif download is not None and _LOG_DOWNLOADED.fullmatch(body):
            download = None
        elif m := _LOG_SKIP.fullmatch(body):
            add(_clean_text(m.group(2)) or "?", _position(m.group(1)), "not_downloaded")
        elif pending is not None and (m := _LOG_RESULT.fullmatch(body)):
            pos, word, code, tail = _position(m.group(1)), m.group(2), m.group(3), m.group(4)
            tail = tail or ""
            if word == "błąd" and tail.startswith(":"):
                status: ItemStatus = "error"
            else:
                status = _LOG_RESULT_STATUS.get(word, "unknown")
            restart = tail == ", wymaga restartu"
            hresult = format_hresult(hresult_value(code))
            title, opened = pending
            add(
                title,
                opened if pos is None else pos,
                status,
                hresult=hresult,
                reboot_required=restart,
            )
            reboot = reboot or restart
            pending = None
        elif m := _LOG_END.fullmatch(body):
            exit_code = int(m.group(1))
            finished = stamp
        elif m := _LOG_START.fullmatch(body):
            pid = int(m.group(1))
        elif m := _LOG_FOUND.fullmatch(body):
            found = int(m.group(1))
        elif body == _LOG_RESTART:
            reboot = True
        else:
            continue
        last = stamp or last
    phase: Literal["downloading", "installing"] | None = None
    current: tuple[str, int | None] | None = None
    if pending is not None:
        if exit_code is None:
            phase, current = "installing", pending
        add(pending[0], pending[1], "installing" if exit_code is None else "unknown")
    elif download is not None and exit_code is None:
        phase, current = "downloading", download
    return InstallLogSummary(
        exit_code=exit_code,
        items=tuple(items),
        positions=tuple(positions),
        reboot_required=reboot,
        pid=pid,
        found=found,
        started=started,
        finished=finished,
        last=last,
        phase=phase,
        current_index=current[1] if current else None,
        current_title=current[0] if current else "",
        download_current=dl_current,
        download_total=dl_total,
    )


def _read_text(path: Path, limit: int, attempts: int = READ_ATTEMPTS) -> str:
    """Shared read with retries (the script may be renaming over the file right now).
    Raises :class:`UpdatesError` only."""
    last: Exception | None = None
    for attempt in range(attempts):
        if attempt:
            time.sleep(READ_RETRY_DELAY)
        try:
            raw = read_shared(path, limit)
        except OSError as exc:
            last = exc
            continue
        if len(raw) > limit:
            raise UpdatesError(f"Plik {path.name} jest zbyt duży.", kind="format")
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            last = exc
    if isinstance(last, FileNotFoundError):
        raise UpdatesError(f"Nie znaleziono pliku {path}.", kind="format")
    raise UpdatesError(f"Nie udało się odczytać pliku {path} ({last}).", kind="format")


def _read_log(progress: InstallProgress) -> InstallLogSummary | None:
    """This run's log (``None``: none, unreadable, or another PID's)."""
    if progress.log_path is None:
        return None
    try:
        summary = parse_install_log(_read_text(progress.log_path, MAX_LOG_BYTES))
    except UpdatesError:
        return None
    if summary.pid is not None and progress.pid is not None and summary.pid != progress.pid:
        return None
    return summary


def read_install_progress(status_path: Path) -> InstallProgress:
    """Current state of the installation whose status file is ``status_path``
    (read-only). ``running`` is confirmed against the process (:func:`is_installer_process`:
    a live ``powershell.exe`` with that PID, created this boot right before ``started``:
    ``alive``); a dead one makes it ``stale``, with the result recovered from the log
    next to the status when the log has it (``from_log``). ``running`` without a PID:
    the log decides, else ``unknown`` (final). A live installer whose status lags
    behind its log by more than :data:`LOG_AHEAD` is completed from the log. Never
    raises anything but :class:`UpdatesError`."""
    progress: InstallProgress | None = None
    last: UpdatesError | None = None
    for attempt in range(READ_ATTEMPTS):
        if attempt:
            time.sleep(READ_RETRY_DELAY)
        try:
            progress = parse_install_status(
                _read_text(status_path, MAX_STATUS_BYTES, attempts=1), path=status_path
            )
            break
        except UpdatesError as exc:
            last = exc
    if progress is None:
        raise last or UpdatesError(f"Nie udało się odczytać {status_path}.", kind="format")
    if progress.state != "running":
        return progress
    if progress.pid is None:
        ended = _stale(progress)
        if ended.from_log and ended.exit_code is not None:
            return ended
        return dataclasses.replace(ended, state="unknown", error=LOST_MESSAGE)
    try:
        alive = is_installer_process(progress.pid, progress.started)
    except OSError:
        alive = False
    if not alive:
        return _stale(progress)
    return _catch_up(dataclasses.replace(progress, alive=True))


def _catch_up(progress: InstallProgress) -> InstallProgress:
    """A live installer whose status writes fail while the log goes on: take the
    current step and the finished results from the log (``from_log``)."""
    updated = progress.updated or progress.started
    try:
        if updated is None or datetime.now().astimezone() - updated < LOG_AHEAD:
            return progress
    except (TypeError, OverflowError):
        return progress
    summary = _read_log(progress)
    if summary is None or summary.last is None or summary.last - updated < LOG_AHEAD:
        return progress
    items = _merge_items(progress.items, summary)
    if summary.phase is not None and summary.current_index is not None:
        k = summary.current_index - 1
        if 0 <= k < len(items) and not items[k].done:
            status: ItemStatus = "installing" if summary.phase == "installing" else "downloading"
            items = items[:k] + (dataclasses.replace(items[k], status=status),) + items[k + 1 :]
    fields: dict[str, Any] = {"items": items, "from_log": True}
    if summary.phase is not None:
        fields.update(
            phase=summary.phase,
            current_title=summary.current_title,
            current=summary.current_index or progress.current,
            step_started=summary.last,
        )
        if summary.phase == "downloading":
            fields.update(
                download_current=summary.download_current,
                download_total=max(progress.download_total, summary.download_total),
            )
    merged = dataclasses.replace(progress, **fields)
    return dataclasses.replace(merged, total=max(merged.total, len(items)))


def _merge_items(
    items: tuple[InstallItem, ...], summary: InstallLogSummary
) -> tuple[InstallItem, ...]:
    """Results from the log onto the status items: by the ``[i]`` position when the log
    has it, else by order of appearance (the k-th "Instalacja: T" is the k-th update
    titled T - updates often share a title, e.g. drivers). Log results that match
    nothing are appended."""
    if not items:
        return summary.items
    result = list(items)
    by_title: dict[str, list[int]] = {}
    for n, item in enumerate(result):
        by_title.setdefault(item.title, []).append(n)
    taken: dict[str, int] = {}
    extra: list[InstallItem] = []
    for pos, log_item in zip(summary.positions, summary.items, strict=True):
        if pos is not None and 1 <= pos <= len(result) and result[pos - 1].title == log_item.title:
            n: int | None = pos - 1
        else:
            k = taken.get(log_item.title, 0)
            taken[log_item.title] = k + 1
            slots = by_title.get(log_item.title, [])
            n = slots[k] if k < len(slots) else None
        if n is None:
            extra.append(log_item)
        else:
            old = result[n]
            result[n] = dataclasses.replace(
                log_item, id=old.id, revision=old.revision, title=old.title
            )
    return tuple(result) + tuple(extra)


def _stale(progress: InstallProgress) -> InstallProgress:
    stale = dataclasses.replace(progress, state="stale", current_title="")
    summary = _read_log(progress)
    if summary is None:
        return stale
    items = _merge_items(progress.items, summary)
    return dataclasses.replace(
        stale,
        phase="finished" if summary.exit_code is not None else progress.phase,
        items=items,
        total=max(progress.total, len(items)),
        exit_code=summary.exit_code,
        reboot_required=progress.reboot_required or summary.reboot_required,
        finished=summary.finished,
        from_log=summary.exit_code is not None or bool(summary.items),
    )


def _foreign(path: Path) -> bool:
    """A JSON file that is clearly not one of our status files (skipped by the finder,
    unlike an unreadable one)."""
    try:
        data = json.loads(_read_text(path, MAX_STATUS_BYTES, attempts=1).lstrip("﻿"))
    except (UpdatesError, ValueError, RecursionError):
        return False
    return not isinstance(data, dict) or data.get("winstaller") != "wu-install"


def find_background_install() -> InstallProgress | None:
    """The newest background installation in the trusted log folders
    (:func:`existing_log_dirs`), read with :func:`read_install_progress`. Files that
    are not ours are skipped; when the newest of ours cannot be read, the result is
    ``unknown`` for it (an older, finished run is never shown in its place). ``None``
    when there is none. Creates and changes nothing."""
    candidates: list[Path] = []
    for folder in existing_log_dirs():
        try:
            candidates.extend(folder.glob(f"{LOG_PREFIX}*.json"))
        except OSError:
            continue
    candidates.sort(key=_log_sort_key, reverse=True)
    for status in candidates[:FIND_CANDIDATES]:
        if not _STAMP_NAME.fullmatch(status.name):
            continue
        try:
            return read_install_progress(status)
        except UpdatesError as exc:
            if _foreign(status):
                continue
            return InstallProgress(
                state="unknown",
                error=f"{LOST_MESSAGE} ({exc.message.rstrip('.')})",
                log_path=status.with_suffix(".log"),
                status_path=status,
                updated=_mtime(status),
            )
    return None


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    except (OSError, OverflowError, ValueError):
        return None


def describe_progress(
    progress: InstallProgress, *, mention_restart: bool = True
) -> tuple[str, str]:
    """Short Polish ``(title, subtitle)`` for the UI, e.g. ``("Pobieranie 2 z 5",
    "<title>")`` or ``("Zakończono — wymagany restart", "Zainstalowano 6 z 6. …")``.
    ``mention_restart=False``: the subtitle leaves the restart out (shown separately)."""
    p = progress
    restart = (
        " Uruchom ponownie komputer, aby dokończyć instalację."
        if p.needs_restart and mention_restart
        else ""
    )
    if p.state == "running":
        note = "Stan wg logu (plik statusu nie jest aktualizowany). " if p.from_log else ""
        if p.phase == "searching":
            return "Wyszukiwanie aktualizacji", "Proces w tle ponownie wyszukuje aktualizacje."
        if p.phase == "downloading":
            n, total = max(1, p.download_current), max(p.download_total, 1)
            return f"Pobieranie {n} z {total}", f"{note}{p.current_title}".strip()
        if p.phase == "installing":
            title = f"Instalacja {max(1, p.current)} z {max(p.count, 1)}"
            return title, f"{note}{p.current_title}".strip()
        return "Instalacja w toku", "Szczegóły w logu."
    if p.state == "unknown":
        return "Stan instalacji nieznany", f"{LOST_MESSAGE}{restart}"
    source = "Wynik odczytany z logu (proces nie zapisał statusu). " if p.from_log else ""
    if p.state == "stale" and p.exit_code is None:
        return (
            "Instalacja przerwana",
            "Proces w tle zakończył się bez zapisania wyniku (został zamknięty albo komputer "
            f"uruchomiono ponownie) — sprawdź log.{restart}",
        )
    n, ok, failed, skipped = p.count, p.ok_count, p.failed_count, p.skipped_count
    if p.exit_code in EXIT_MESSAGES and p.exit_code not in (1, 2) and not ok:
        detail = EXIT_MESSAGES[p.exit_code].rstrip(".")
        return "Nie zainstalowano aktualizacji", f"{source}{detail} (kod {p.exit_code}).{restart}"
    if failed or p.exit_code not in (0, None):
        title = (
            f"Zakończono z błędami ({failed} z {n} nieudane)"
            if failed
            else f"Zakończono z błędem (kod {p.exit_code})"
        )
        return title, f"{source}Zainstalowano {ok} z {n}.{restart}".strip()
    if n == 0:
        if p.exit_code is None:
            return (
                "Zakończono — wynik niepotwierdzony",
                f"{source}Proces nie podał kodu zakończenia — sprawdź log.{restart}",
            )
        return "Zakończono — brak aktualizacji do zainstalowania", f"{source}{restart}".strip()
    if skipped or p.exit_code is None:
        rest = f" Pominięte: {skipped}." if skipped else " Proces nie podał kodu zakończenia."
        return (
            f"Zakończono niepełne ({ok} z {n} zainstalowano)",
            f"{source}Zainstalowano {ok} z {n}.{rest}{restart}",
        )
    if p.needs_restart:
        return "Zakończono — wymagany restart", f"{source}Zainstalowano {ok} z {n}.{restart}"
    return "Zakończono", f"{source}Zainstalowano {ok} z {n}."


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
