"""Latest GPU drivers straight from the vendors (NVIDIA, AMD, Intel).

Flow of :func:`install_latest`: resolve the newest gaming driver on the vendor's site
(source A, then a plan B) -> download the installer (only https, only the vendor's
download host, size/hash checks) -> verify its Authenticode signature -> run it silently
with a timeout -> map its exit code. The downloaded file is always removed.

By the owner's decision there is NO graphics card detection and NO compatibility check:
the vendor's installer decides on its own whether it can install anything.

Testability: every network call goes through an injectable ``session`` (anything with
``requests.Session``-like ``get``/``head``); processes, free space and file removal go
through the small module-level functions in the "system layer" section, which tests
``monkeypatch``. Parsers (``parse_*``) are pure functions over the raw responses and
raise :class:`DriverError` (never a bare ``KeyError``/``ValueError``) on unexpected input.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import functools
import hashlib
import html
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from winstaller.core import platform as plat
from winstaller.core import winget
from winstaller.core.version import get_version

LineCallback = Callable[[str], None]
ProgressCallback = Callable[[int, "int | None"], None]  # (received bytes, total or None)

META_TIMEOUT: tuple[float, float] = (10.0, 30.0)
DOWNLOAD_TIMEOUT: tuple[float, float] = (15.0, 60.0)
MAX_REDIRECTS = 5
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_DSA_ZIP_BYTES = 64 * 1024 * 1024
MAX_DSA_JSON_BYTES = 64 * 1024 * 1024
#: Free space needed per byte of installer: the download plus its unpacked contents.
FREE_SPACE_FACTOR = 3
#: Every GPU driver package is hundreds of MB; anything below this is not an installer.
MIN_INSTALLER_BYTES = 10 * 1024 * 1024
#: Hard upper limit of a download (the biggest package today is 1.8 GB); applies also
#: when the server sends no Content-Length.
MAX_DOWNLOAD_BYTES = 4 * 1024**3
SIGNATURE_TIMEOUT = 300.0
PROGRESS_INTERVAL = 0.2  # at most ~5 progress updates per second
DOWNLOAD_SUBDIR = "winstaller-drivers"
_CHUNK = 1024 * 1024

_MB = 1024 * 1024


class DriverError(Exception):
    """A driver step failed; ``str(exc)`` is a Polish message for the user.

    ``vendor_url`` is the vendor's manual download page, shown so the user can get the
    driver by hand.
    """

    def __init__(self, message: str, *, vendor_url: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.vendor_url = vendor_url

    def user_message(self) -> str:
        if self.vendor_url:
            return f"{self.message} Sterownik można pobrać ręcznie: {self.vendor_url}"
        return self.message


class DriverCancelled(DriverError):
    """Cancelled by the user."""


@dataclass(frozen=True, slots=True)
class DriverRelease:
    vendor: str
    version: str
    url: str
    #: Exact size in bytes from the vendor's metadata (verified after download).
    size: int | None = None
    sha1: str | None = None  # lowercase hex
    sha512: str | None = None  # lowercase hex
    release_date: str | None = None  # ISO date when parseable, otherwise the vendor's text
    source: str = ""  # which source answered (A or plan B), for the log
    #: Rough size (bytes) from a "1.04 GB" style label; only for the log and space check.
    approx_size: int | None = None
    notes: tuple[str, ...] = ()  # warnings to show in the log


@dataclass(frozen=True, slots=True)
class ExitCodeInfo:
    ok: bool
    message: str  # Polish; for success: what about the restart
    reboot_required: bool = False


@dataclass(frozen=True, slots=True)
class SignatureInfo:
    status: str  # Get-AuthenticodeSignature Status: "Valid", "NotSigned", "HashMismatch"...
    subject: str  # signer certificate subject ("" when unsigned)
    status_message: str = ""
    thumbprint: str = ""
    signature_type: str = ""


@dataclass(frozen=True, slots=True)
class ProcessResult:
    returncode: int
    cancelled: bool = False  # stopped because the user cancelled
    timed_out: bool = False  # stopped because the timeout passed
    #: The main process exited normally but processes it started were still running
    #: after POST_EXIT_GRACE_SECONDS; they were left alone.
    left_running: bool = False


@dataclass(frozen=True, slots=True)
class DriverInstallResult:
    ok: bool
    message: str
    cancelled: bool = False
    reboot_required: bool = False
    version: str | None = None
    returncode: int | None = None


# ------------------------------------------------------------------ helpers (pure)


def _parser[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Contract of every ``parse_*``: unexpected input -> :class:`DriverError`, never
    anything else (zlib/lzma errors, bad UTF-8, ``RecursionError`` from deeply nested
    JSON, ``MemoryError``...), so a broken source A always falls back to plan B."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except DriverError:
            raise
        except Exception as exc:
            raise DriverError(
                f"Nieprawidłowy format odpowiedzi producenta ({type(exc).__name__})."
            ) from None

    return wrapper


_VERSION = re.compile(r"\d+(?:\.\d+)+")


def version_key(text: str) -> tuple[int, ...]:
    """``"621.24"`` -> ``(621, 24)``; anything else raises :class:`DriverError`."""
    value = text.strip() if isinstance(text, str) else ""
    if not _VERSION.fullmatch(value):
        raise DriverError(f"Nieprawidłowy numer wersji sterownika: {text!r}.")
    return tuple(int(part) for part in value.split("."))


def _valid_version(text: Any) -> str | None:
    if isinstance(text, str) and _VERSION.fullmatch(text.strip()):
        return text.strip()
    return None


#: A DNS name: ASCII labels of letters/digits/hyphens (no hyphen at either end), at least
#: two labels, the last one starting with a letter (so IP addresses never match).
_HOSTNAME = re.compile(
    r"(?=.{1,253}\Z)(?:(?!-)[a-z0-9-]{1,63}(?<!-)\.)+(?!-)[a-z][a-z0-9-]{0,62}(?<!-)\Z"
)
#: Characters that different URL parsers treat differently (``\`` is a path separator for
#: urllib3/browsers but part of the host for urlsplit) or that no real URL contains.
_UNSAFE_URL_CHARS = re.compile(r"[\\\s\x00-\x1f\x7f]")


def _client_host(url: str) -> tuple[str, str, int | None, bool] | None:
    """(scheme, host, port, has_auth) as the HTTP client (urllib3, used by requests) sees it."""
    try:
        from urllib3.util import parse_url

        parsed = parse_url(url)
    except Exception:  # LocationParseError and friends: the client would refuse it too
        return None
    host = (parsed.host or "").lower().rstrip(".")
    return (parsed.scheme or "").lower(), host, parsed.port, bool(parsed.auth)


def host_allowed(url: str, allowed: Iterable[str]) -> bool:
    """True for an ``https`` URL whose host is in ``allowed``.

    An entry starting with ``.`` is a suffix (``.download.nvidia.com`` matches
    ``us.download.nvidia.com``). Rejected: user info, ports other than 443, ``%`` in the
    authority, backslashes, whitespace/control and non-ASCII characters anywhere, hosts
    that are not plain DNS names. The host must be the same for :func:`urlsplit` and for
    urllib3 (what ``requests`` really connects to), so parser differences cannot be used
    to slip past the list.
    """
    if not isinstance(url, str) or not url or not url.isascii() or _UNSAFE_URL_CHARS.search(url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() != "https" or port not in (None, 443):
        return False
    if "@" in parts.netloc or "%" in parts.netloc:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not _HOSTNAME.match(host):
        return False
    client = _client_host(url)
    if client is None or client != ("https", host, port, False):
        return False
    for entry in allowed:
        pattern = entry.lower()
        if pattern.startswith("."):
            if host.endswith(pattern) and len(host) > len(pattern):
                return True
        elif host == pattern:
            return True
    return False


_SIZE_TEXT = re.compile(r"([\d]+(?:[.,]\d+)?)\s*(KB|MB|GB)\b", re.IGNORECASE)
_UNITS = {"kb": 1024, "mb": 1024**2, "gb": 1024**3}


def parse_size_text(text: str | None) -> int | None:
    """``"1.04 GB"`` / ``"887 MB"`` -> approximate bytes; None when not recognized."""
    if not text:
        return None
    m = _SIZE_TEXT.search(text)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return int(value * _UNITS[m.group(2).lower()]) or None


def _iso_date(text: str | None, formats: Sequence[str]) -> str | None:
    if not text:
        return None
    cleaned = " ".join(text.split())
    for fmt in formats:
        with contextlib.suppress(ValueError):
            return datetime.strptime(cleaned, fmt).date().isoformat()
    return cleaned or None


_TAG = re.compile(r"<[^>]+>")


def _strip_tags(fragment: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", fragment)).split())


def sanitize_filename(url: str, fallback: str = "driver.exe") -> str:
    """Safe local file name (``[A-Za-z0-9._-]``, ``.exe``) from the last URL path segment."""
    try:
        name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    except ValueError:
        name = ""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._")[:120]
    if not name.lower().endswith(".exe"):
        name = fallback
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        name = f"_{name}"  # CON.exe & co. are devices, not files, on Windows 10
    return name


_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
)


def format_mb(size: int | float) -> str:
    return f"{size / _MB:.1f} MB"


def format_progress(received: int, total: int | None, elapsed: float | None) -> str:
    """``Pobieranie: 45% · 470.2 / 1043.6 MB · 38.4 MB/s`` (pure).

    ``elapsed=None`` leaves the speed out (too early to say anything meaningful)."""
    speed = ""
    if elapsed is not None:
        speed = f" · {received / elapsed / _MB if elapsed > 0 else 0.0:.1f} MB/s"
    if total:
        percent = min(100, int(received * 100 / total))
        return f"Pobieranie: {percent}% · {received / _MB:.1f} / {total / _MB:.1f} MB{speed}"
    return f"Pobieranie: {received / _MB:.1f} MB{speed}"


class ProgressReporter:
    """Turns download progress into log lines: the first one is appended, later ones
    replace it, at most once per ``interval`` seconds (the final 100% always shows).

    The first line has no speed (nothing measured yet). :meth:`finish` shows the last
    state even when the total is unknown and the last update was throttled away."""

    def __init__(
        self,
        log: LineCallback,
        replace_last: LineCallback,
        *,
        interval: float = PROGRESS_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._log = log
        self._replace = replace_last
        self._interval = interval
        self._clock = clock
        self._start = clock()
        self._last: float | None = None
        self._shown = False
        self._latest: tuple[int, int | None] | None = None  # last values received
        self._displayed: tuple[int, int | None] | None = None  # last values shown

    def __call__(self, received: int, total: int | None) -> None:
        now = self._clock()
        self._latest = (received, total)
        done = total is not None and received >= total
        if self._last is not None and now - self._last < self._interval and not done:
            return
        self._show(now, received, total)

    def finish(self) -> None:
        """Show the final state if the last update was throttled away."""
        if self._latest is not None and self._latest != self._displayed:
            self._show(self._clock(), *self._latest)

    def _show(self, now: float, received: int, total: int | None) -> None:
        self._last = now
        self._displayed = (received, total)
        if self._shown:
            self._replace(format_progress(received, total, now - self._start))
        else:
            self._log(format_progress(received, total, None))
            self._shown = True


# ----------------------------------------------------------------- NVIDIA parsers

NVIDIA_LOOKUP_URL = (
    "https://gfwsl.geforce.com/services_toolkit/services/com/nvidia/services/"
    "AjaxDriverService.php?func=DriverManualLookup&psid=131&pfid=1066&osID=135"
    "&languageCode=1033&beta=0&isWHQL=1&dltype=-1&dch=1&upCRD=0&qnf=0&sort1=0"
    "&numberOfResults=5"
)
NVIDIA_PROCESS_FIND_URL = (
    "https://www.nvidia.com/Download/processFind.aspx?psid=131&pfid=1066&osid=135&lid=1"
    "&whql=1&lang=en-us&ctk=0&dtcid=1"
)
NVIDIA_URL_TEMPLATES = (
    "https://us.download.nvidia.com/Windows/{v}/{v}-desktop-win10-win11-64bit-international-dch-whql.exe",
    "https://international.download.nvidia.com/Windows/{v}/{v}-desktop-win10-win11-64bit-international-dch-whql.exe",
)
NVIDIA_META_HOSTS = ("gfwsl.geforce.com", "www.nvidia.com")
NVIDIA_DOWNLOAD_HOSTS = (".download.nvidia.com",)
NVIDIA_PAGE = "https://www.nvidia.com/pl-pl/drivers/"
_NVIDIA_DATE_FORMATS = ("%a %b %d, %Y", "%B %d, %Y", "%b %d, %Y", "%Y-%m-%d")


def _nv_field(info: Mapping[str, Any], key: str) -> str:
    value = info.get(key)
    return unquote(value) if isinstance(value, str) else ""


@_parser
def parse_nvidia_lookup(text: str | bytes) -> DriverRelease:
    """Newest Game Ready driver from a ``DriverManualLookup`` JSON answer.

    Keeps entries with ``IsBeta == "0"``, ``IsCRD == "0"`` (not Studio), "Game Ready"
    in the name, no ``-nsd-`` (Studio) in the URL and an https URL on
    ``*.download.nvidia.com``; the highest version wins.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError, RecursionError, MemoryError) as exc:
        raise DriverError("Nieprawidłowa odpowiedź serwisu sterowników NVIDIA.") from exc
    if not isinstance(data, dict):
        raise DriverError("Nieprawidłowa odpowiedź serwisu sterowników NVIDIA.")
    if str(data.get("Success", "")) == "0":
        raise DriverError("Serwis NVIDIA nie znalazł sterownika dla wybranej karty.")
    entries = data.get("IDS")
    if not isinstance(entries, list):
        raise DriverError("Nieprawidłowa odpowiedź serwisu sterowników NVIDIA (brak listy).")
    best: DriverRelease | None = None
    for entry in entries:
        info = entry.get("downloadInfo") if isinstance(entry, dict) else None
        if not isinstance(info, dict):
            continue
        name = _nv_field(info, "Name")
        url = _nv_field(info, "DownloadURL").strip()
        version = _valid_version(_nv_field(info, "Version"))
        if (
            version is None
            or _nv_field(info, "IsBeta") != "0"
            or _nv_field(info, "IsCRD") != "0"
            or "game ready" not in name.lower()
            or "-nsd-" in url.lower()
            or not host_allowed(url, NVIDIA_DOWNLOAD_HOSTS)
        ):
            continue
        candidate = DriverRelease(
            vendor="nvidia",
            version=version,
            url=url,
            release_date=_iso_date(_nv_field(info, "ReleaseDateTime"), _NVIDIA_DATE_FORMATS),
            source="NVIDIA DriverManualLookup",
            approx_size=parse_size_text(_nv_field(info, "DownloadURLFileSize")),
        )
        if best is None or version_key(candidate.version) > version_key(best.version):
            best = candidate
    if best is None:
        raise DriverError("W odpowiedzi NVIDIA nie ma sterownika Game Ready (WHQL).")
    return best


@dataclass(frozen=True, slots=True)
class NvidiaListing:
    version: str
    release_date: str | None


_NV_ROW = re.compile(r"<tr\s+id=[\"']driverList[\"'][^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_NV_NAME = re.compile(r"class=[\"'][^\"']*driverName[^\"']*[\"'][^>]*>(.*?)</td>", re.I | re.S)
_TD = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)


@_parser
def parse_nvidia_process_find(text: str) -> NvidiaListing:
    """Newest "Game Ready" row of the ``processFind.aspx`` HTML table (plan B)."""
    best: NvidiaListing | None = None
    for row in _NV_ROW.finditer(text or ""):
        body = row.group(1)
        name_m = _NV_NAME.search(body)
        name = _strip_tags(name_m.group(1)) if name_m else ""
        if "game ready" not in name.lower():
            continue
        cells = [_strip_tags(c) for c in _TD.findall(body)]
        version = next((c for c in cells if _valid_version(c)), None)
        if version is None:
            continue
        date_text = next((c for c in cells if re.fullmatch(r"[A-Za-z]+ \d{1,2}, \d{4}", c)), None)
        listing = NvidiaListing(version, _iso_date(date_text, _NVIDIA_DATE_FORMATS))
        if best is None or version_key(version) > version_key(best.version):
            best = listing
    if best is None:
        raise DriverError("Na stronie wyszukiwania NVIDIA nie znaleziono sterownika Game Ready.")
    return best


def nvidia_url_candidates(version: str) -> list[str]:
    """Download URLs built from the known file name pattern (plan B)."""
    version_key(version)  # only digits and dots may go into the URL
    return [t.format(v=version) for t in NVIDIA_URL_TEMPLATES]


# -------------------------------------------------------------------- AMD parsers

AMD_PRODUCT_URL = (
    "https://www.amd.com/en/support/downloads/drivers.html/graphics/radeon-rx/"
    "radeon-rx-9000-series/amd-radeon-rx-9070-xt.html"
)
AMD_DRIVERS_URL = "https://www.amd.com/en/support/download/drivers.html"
AMD_NOTES_URL_TEMPLATE = (
    "https://www.amd.com/en/resources/support-articles/release-notes/RN-RAD-WIN-{dashed}.html"
)
AMD_META_HOSTS = ("www.amd.com",)
AMD_DOWNLOAD_HOSTS = ("drivers.amd.com",)
AMD_PAGE = AMD_DRIVERS_URL  # the /pl/ variant answers 404 (checked 2026-11-20)
_AMD_DATE_FORMATS = ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y")


@dataclass(frozen=True, slots=True)
class AmdProductInfo:
    version: str
    label: str  # e.g. "WHQL (Recommended)", "(Optional)"
    release_date: str | None
    package_url: str | None  # full package from the product page (RX 7000/9000 only)
    notes_url: str | None
    approx_size: int | None


@dataclass(frozen=True, slots=True)
class AmdReleaseNotes:
    url: str  # universal package for all RDNA products
    version: str | None
    release_date: str | None


_AMD_ARTICLE = re.compile(
    r"<article\b[^>]*driver-download-details[^>]*>(.*?)</article>", re.IGNORECASE | re.DOTALL
)
_AMD_FIELD = r"{label}\s*</strong>\s*<p>\s*(.*?)\s*</p>"
_AMD_REVISION = re.compile(r"Adrenalin\s+(\d+(?:\.\d+){1,3})\b\s*(.*)", re.IGNORECASE)
_AMD_EXE_HREF = re.compile(r"href=[\"'](https://drivers\.amd\.com/[^\"']+?\.exe)[\"']", re.I)
_AMD_NOTES_HREF = re.compile(r"href=[\"']([^\"']*RN-RAD-WIN-[^\"']*?\.html)[\"']", re.I)


def _amd_field(block: str, label: str) -> str | None:
    m = re.search(_AMD_FIELD.format(label=label), block, re.IGNORECASE | re.DOTALL)
    return _strip_tags(m.group(1)) if m else None


def _amd_page_url(href: str) -> str | None:
    url = urljoin("https://www.amd.com/", html.unescape(href).strip())
    return url if host_allowed(url, AMD_META_HOSTS) else None


@_parser
def parse_amd_product_page(text: str) -> AmdProductInfo:
    """Newest Adrenalin entry of an AMD product download page.

    Each ``driver-download-details`` block has a "Revision Number" such as
    ``Adrenalin 26.11.2 WHQL (Recommended)`` (labels vary: ``(Optional)``,
    ``(WHQL Recommended)``...). The newest version wins regardless of the label. The
    auto-detect web installer (``minimalsetup``) is never taken as the package.
    """
    best: AmdProductInfo | None = None
    for article in _AMD_ARTICLE.finditer(text or ""):
        block = article.group(1)
        revision = _amd_field(block, "Revision Number")
        m = _AMD_REVISION.search(revision or "")
        if not m:
            continue
        version = m.group(1)
        packages = [
            html.unescape(u)
            for u in _AMD_EXE_HREF.findall(block)
            if "minimalsetup" not in u.lower()
            and host_allowed(html.unescape(u), AMD_DOWNLOAD_HOSTS)
        ]
        notes = _AMD_NOTES_HREF.search(block)
        info = AmdProductInfo(
            version=version,
            label=m.group(2).strip(),
            release_date=_iso_date(_amd_field(block, "Release Date"), _AMD_DATE_FORMATS),
            package_url=packages[0] if packages else None,
            notes_url=_amd_page_url(notes.group(1)) if notes else None,
            approx_size=parse_size_text(_amd_field(block, "File Size")),
        )
        if best is None or version_key(version) > version_key(best.version):
            best = info
    if best is None:
        raise DriverError("Na stronie AMD nie znaleziono wersji sterownika Adrenalin.")
    return best


_AMD_MINIMAL = re.compile(r"amd-software-adrenalin-edition-(\d+(?:\.\d+){1,3})-minimalsetup", re.I)


@_parser
def parse_amd_drivers_page(text: str) -> str:
    """Plan B: version from the auto-detect installer's file name on the general page."""
    versions = [v for v in _AMD_MINIMAL.findall(text or "") if _valid_version(v)]
    if not versions:
        raise DriverError("Na ogólnej stronie sterowników AMD nie znaleziono numeru wersji.")
    return max(versions, key=version_key)


def url_has_version(url: str, version: str) -> bool:
    """True when the file name in ``url`` contains ``version`` as a whole number sequence
    (``...-26.11.2-win11-c.exe`` for ``26.11.2``; not ``26.11.20`` or ``126.11.2``)."""
    try:
        version_key(version)
        name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    except (DriverError, ValueError):
        return False
    return re.search(rf"(?<![\d.]){re.escape(version)}(?!\d|\.\d)", name) is not None


def amd_release_notes_url(version: str) -> str:
    version_key(version)
    return AMD_NOTES_URL_TEMPLATE.format(dashed=version.replace(".", "-"))


_P = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_A = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_UNIVERSAL_LABEL = re.compile(r"systems\s+with\s+rdna\W{0,3}\s*series\s+graphics", re.I)
_NOTES_TITLE = re.compile(
    r"<title>[^<]*?Adrenalin\s+Edition\s+(\d+(?:\.\d+){1,3})\b", re.IGNORECASE | re.DOTALL
)
_NOTES_UPDATED = re.compile(
    r"Last\s+Updated\s*(?:</b>)?\s*:?\s*([A-Z][a-z]+)\s+(\d{1,2})(?:<sup>\w*</sup>|st|nd|rd|th)?"
    r",?\s*(\d{4})",
    re.IGNORECASE,
)


@_parser
def parse_amd_release_notes(text: str, expected_version: str | None = None) -> AmdReleaseNotes:
    """Universal package link ("Systems with RDNA series graphics products: <a ...>").

    File names are irregular (``-win11-c.exe``, ``-win10-win11-june-rdna.exe``...), so the
    URL always comes from the page. Raises :class:`DriverError` when the page is for a
    different version, or when there is no (or more than one distinct) universal link.
    """
    text = text or ""
    title = _NOTES_TITLE.search(text)
    version = title.group(1) if title else None
    if expected_version and version and version_key(version) != version_key(expected_version):
        raise DriverError(
            f"Informacje o wydaniu AMD dotyczą wersji {version}, a nie {expected_version}."
        )
    urls: list[str] = []
    for para in _P.finditer(text):
        body = para.group(1)
        if "drivers.amd.com" not in body.lower():
            continue
        first_link = _A.search(body)
        label = _strip_tags(body[: first_link.start()] if first_link else body)
        if not _UNIVERSAL_LABEL.search(label):
            continue
        for href in _A.findall(body):
            url = html.unescape(href).strip()
            if (
                url.lower().endswith(".exe")
                and "minimalsetup" not in url.lower()
                and host_allowed(url, AMD_DOWNLOAD_HOSTS)
                and url not in urls
            ):
                urls.append(url)
    if not urls:
        raise DriverError("W informacjach o wydaniu AMD nie ma linku do pakietu dla kart RDNA.")
    if len(urls) > 1:
        raise DriverError("W informacjach o wydaniu AMD jest kilka różnych pakietów dla RDNA.")
    date = None
    if m := _NOTES_UPDATED.search(text):
        date = _iso_date(f"{m.group(1)} {m.group(2)}, {m.group(3)}", _AMD_DATE_FORMATS)
    return AmdReleaseNotes(urls[0], version, date)


# ------------------------------------------------------------------ Intel parsers

INTEL_DSA_URL = "https://dsadata.intel.com/data/en"
INTEL_PAGE_URL = "https://www.intel.com/content/www/us/en/download/785597/intel-arc-iris-xe-graphics-windows.html"
INTEL_GROUP_ID = "785597"
INTEL_META_HOSTS = ("dsadata.intel.com", "www.intel.com")
INTEL_DOWNLOAD_HOSTS = ("downloadmirror.intel.com",)
INTEL_PAGE = INTEL_PAGE_URL
INTEL_DSA_MEMBER = "software-configurations.json"
_INTEL_DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d")
_HEX40 = re.compile(r"[0-9a-fA-F]{40}")
_HEX128 = re.compile(r"[0-9a-fA-F]{128}")


@_parser
def read_intel_dsa_zip(data: bytes, *, max_member: int = MAX_DSA_JSON_BYTES) -> bytes:
    """``software-configurations.json`` from the DSA data ZIP (nothing else is read)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            member = next(
                (
                    m
                    for m in archive.infolist()
                    if m.filename.replace("\\", "/").rsplit("/", 1)[-1].lower() == INTEL_DSA_MEMBER
                ),
                None,
            )
            if member is None:
                raise DriverError("W danych Intel DSA brakuje pliku software-configurations.json.")
            if member.file_size > max_member:
                raise DriverError("Plik danych Intel DSA jest podejrzanie duży.")
            with archive.open(member) as fh:
                content = fh.read(max_member + 1)
    except DriverError:
        raise
    except Exception as exc:  # BadZipFile, zlib.error, LZMAError, bad UTF-8 names, ...
        # NotImplementedError = unsupported compression, RuntimeError = encrypted member.
        raise DriverError("Nieprawidłowe archiwum danych Intel DSA.") from exc
    if len(content) > max_member:
        raise DriverError("Plik danych Intel DSA jest podejrzanie duży.")
    return content


def _intel_version(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    m = re.match(r"\s*(\d+(?:\.\d+){1,3})\b", text)
    return m.group(1) if m else None


def _is_64bit_windows(oses: Any) -> bool:
    if not oses:
        return True  # no list: the package is not restricted
    if not isinstance(oses, list):
        return False
    return any(
        isinstance(o, str) and o.lower().startswith("windows") and o.endswith("-64") for o in oses
    )


@_parser
def parse_intel_dsa(data: bytes | str) -> DriverRelease:
    """Newest non-beta Intel Arc driver (group 785597) from DSA data.

    ``data`` is ``software-configurations.json`` (UTF-8 with BOM) or the whole DSA ZIP.
    """
    if isinstance(data, bytes) and data[:2] == b"PK":
        data = read_intel_dsa_zip(data)
    try:
        text = data.decode("utf-8-sig") if isinstance(data, bytes) else data.lstrip("\ufeff")
        entries = json.loads(text)
    except (UnicodeDecodeError, ValueError, RecursionError, MemoryError) as exc:
        raise DriverError("Nieprawidłowe dane Intel DSA (JSON).") from exc
    if not isinstance(entries, list):
        raise DriverError("Nieprawidłowe dane Intel DSA (oczekiwano listy).")
    best: DriverRelease | None = None
    for entry in entries:
        if not isinstance(entry, dict) or str(entry.get("GroupId", "")) != INTEL_GROUP_ID:
            continue
        if entry.get("IsBeta") is not False:
            continue
        version = _intel_version(entry.get("Version"))
        files = entry.get("Files")
        if version is None or not isinstance(files, list):
            continue
        for item in files:
            if not isinstance(item, dict):
                continue
            url = item.get("Url")
            sha1 = item.get("Hash")
            size = item.get("Size")
            if (
                not isinstance(url, str)
                or not url.lower().endswith(".exe")
                or not host_allowed(url, INTEL_DOWNLOAD_HOSTS)
                or not isinstance(sha1, str)
                or not _HEX40.fullmatch(sha1)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size <= 0
                or not _is_64bit_windows(item.get("OperatingSystems"))
            ):
                continue
            date = entry.get("DisplayReleaseDate")
            candidate = DriverRelease(
                vendor="intel",
                version=version,
                url=url,
                size=size,
                sha1=sha1.lower(),
                release_date=date[:10] if isinstance(date, str) and date else None,
                source="Intel DSA (dsadata.intel.com)",
            )
            if best is None or version_key(version) > version_key(best.version):
                best = candidate
            break
    if best is None:
        raise DriverError("W danych Intel DSA nie ma sterownika Intel Arc dla Windows 64-bit.")
    return best


_INTEL_BLOCK = re.compile(r"cmp-button-list__container(.*?)(?=cmp-button-list__container|$)", re.S)
_INTEL_EXE = re.compile(r"href=[\"'](https://downloadmirror\.intel\.com/[^\"']+?\.exe)[\"']", re.I)
_INTEL_SHA512 = re.compile(r"SHA512:\s*(?:<[^>]*>\s*)*([0-9a-fA-F]{128})\b")
_INTEL_PAGE_VERSION = re.compile(r"Version\s+(\d+(?:\.\d+){2,3})\b")
_INTEL_META = re.compile(r"cmp-button-list__meta[\"'][^>]*>(.*?)</div>", re.IGNORECASE | re.DOTALL)
_INTEL_PAGE_DATE = re.compile(r"cmp-teaser__required-info[^>]*>\s*([A-Z][a-z]+ \d{1,2}, \d{4})\s*<")


@_parser
def parse_intel_page(text: str) -> DriverRelease:
    """Plan B: version, installer URL and SHA512 from the Intel download page."""
    text = text or ""
    version_m = _INTEL_PAGE_VERSION.search(text)
    if not version_m:
        raise DriverError("Na stronie Intel nie znaleziono numeru wersji sterownika.")
    version = version_m.group(1)
    short = ".".join(version.split(".")[-2:])  # 101.9175 in gfx_win_101.9175.exe
    found: list[tuple[str, str, int | None]] = []
    for block_m in _INTEL_BLOCK.finditer(text):
        block = block_m.group(1)
        exe = _INTEL_EXE.search(block)
        sha = _INTEL_SHA512.search(block)
        if not exe or not sha:
            continue
        url = html.unescape(exe.group(1))
        meta = _INTEL_META.search(block)
        if host_allowed(url, INTEL_DOWNLOAD_HOSTS):
            approx = parse_size_text(_strip_tags(meta.group(1))) if meta else None
            found.append((url, sha.group(1).lower(), approx))
    if not found:
        raise DriverError("Na stronie Intel nie znaleziono instalatora z sumą SHA512.")
    url, sha512, approx = next((f for f in found if short in f[0]), found[0])
    date_m = _INTEL_PAGE_DATE.search(text)
    return DriverRelease(
        vendor="intel",
        version=version,
        url=url,
        sha512=sha512,
        release_date=_iso_date(date_m.group(1) if date_m else None, _INTEL_DATE_FORMATS),
        source="strona Intel (plan B)",
        approx_size=approx,
    )


# ------------------------------------------------------------------- network layer


def user_agent() -> str:
    return f"Winstaller/{get_version()}"


def default_session() -> Any:
    """``requests.Session`` with our User-Agent (www.amd.com tarpits python-requests)."""
    import requests

    session = requests.Session()
    session.headers["User-Agent"] = user_agent()
    session.headers["Accept-Language"] = "en-US,en;q=0.8"
    return session


def _close(response: Any) -> None:
    close = getattr(response, "close", None)
    if close is not None:
        with contextlib.suppress(Exception):
            close()


_REDIRECTS = {301, 302, 303, 307, 308}


def _short(exc: BaseException, limit: int = 160) -> str:
    """Exception text for the log: ``requests`` messages repeat the whole URL and pool."""
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"


def open_url(
    session: Any,
    url: str,
    *,
    allowed_hosts: Sequence[str],
    headers: Mapping[str, str] | None = None,
    timeout: tuple[float, float] = META_TIMEOUT,
    stream: bool = False,
    method: str = "GET",
    vendor_url: str | None = None,
) -> Any:
    """GET/HEAD following redirects by hand: every hop must be https on ``allowed_hosts``.

    Returns the final non-redirect response (any status; the caller checks it).
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not host_allowed(current, allowed_hosts):
            raise DriverError(f"Niedozwolony adres pobierania: {current}", vendor_url=vendor_url)
        request = session.head if method == "HEAD" else session.get
        try:
            response = request(
                current,
                headers=dict(headers or {}),
                timeout=timeout,
                stream=stream,
                allow_redirects=False,
            )
        except OSError as exc:  # requests.RequestException derives from OSError
            host = urlsplit(current).hostname
            raise DriverError(
                f"Błąd połączenia z {host}: {_short(exc)}", vendor_url=vendor_url
            ) from exc
        status = getattr(response, "status_code", 0)
        if status in _REDIRECTS:
            location = (getattr(response, "headers", None) or {}).get("location")
            _close(response)
            if not location:
                raise DriverError(f"Przekierowanie bez adresu docelowego: {current}")
            target = urljoin(current, location)
            if "download-incomplete" in target.lower():
                raise DriverError(
                    "Serwer producenta odrzucił pobieranie (przekierowanie na stronę "
                    "„Download Incomplete”).",
                    vendor_url=vendor_url,
                )
            current = target
            continue
        return response
    raise DriverError(f"Zbyt wiele przekierowań: {url}", vendor_url=vendor_url)


def _read_limited(response: Any, limit: int) -> bytes:
    data = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if chunk:
                data += chunk
                if len(data) > limit:
                    raise DriverError("Odpowiedź serwera jest podejrzanie duża.")
    except OSError as exc:
        raise DriverError(f"Przerwano odczyt odpowiedzi serwera: {_short(exc)}") from exc
    finally:
        _close(response)
    return bytes(data)


def fetch_bytes(
    session: Any,
    url: str,
    *,
    allowed_hosts: Sequence[str],
    headers: Mapping[str, str] | None = None,
    limit: int = MAX_PAGE_BYTES,
) -> bytes:
    """GET ``url`` (HTTP 200 required) and return at most ``limit`` bytes."""
    response = open_url(session, url, allowed_hosts=allowed_hosts, headers=headers, stream=True)
    status = getattr(response, "status_code", 0)
    if status != 200:
        _close(response)
        raise DriverError(f"Serwer {urlsplit(url).hostname} zwrócił błąd HTTP {status}.")
    return _read_limited(response, limit)


def fetch_text(session: Any, url: str, **kwargs: Any) -> str:
    return fetch_bytes(session, url, **kwargs).decode("utf-8", errors="replace")


def _content_length(response: Any) -> int | None:
    raw = (getattr(response, "headers", None) or {}).get("content-length")
    try:
        value = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    return value if value is not None and value >= 0 else None


# ------------------------------------------------------------------------ resolvers


def _combine_errors(vendor: str, attempts: list[str], vendor_url: str) -> DriverError:
    tried = "; ".join(attempts)
    return DriverError(
        f"Nie udało się ustalić najnowszego sterownika {vendor}. Próbowano: {tried}.",
        vendor_url=vendor_url,
    )


def resolve_nvidia(session: Any) -> DriverRelease:
    attempts: list[str] = []
    try:
        text = fetch_text(session, NVIDIA_LOOKUP_URL, allowed_hosts=NVIDIA_META_HOSTS)
        return parse_nvidia_lookup(text)
    except DriverError as exc:
        attempts.append(f"serwis DriverManualLookup ({exc.message})")
    try:
        text = fetch_text(session, NVIDIA_PROCESS_FIND_URL, allowed_hosts=NVIDIA_META_HOSTS)
        listing = parse_nvidia_process_find(text)
        for url in nvidia_url_candidates(listing.version):
            response = open_url(session, url, allowed_hosts=NVIDIA_DOWNLOAD_HOSTS, method="HEAD")
            status = getattr(response, "status_code", 0)
            length = _content_length(response)
            _close(response)
            if status == 200:
                return DriverRelease(
                    vendor="nvidia",
                    version=listing.version,
                    url=url,
                    release_date=listing.release_date,
                    source="NVIDIA processFind + wzorzec adresu (plan B)",
                    approx_size=length,
                )
        raise DriverError(f"plik sterownika {listing.version} nie istnieje pod znanym adresem")
    except DriverError as exc:
        attempts.append(f"wyszukiwarka processFind ({exc.message})")
    raise _combine_errors("NVIDIA", attempts, NVIDIA_PAGE)


def _amd_headers() -> dict[str, str]:
    return {"User-Agent": user_agent(), "Accept-Language": "en-US,en;q=0.8"}


def resolve_amd(session: Any) -> DriverRelease:
    attempts: list[str] = []
    headers = _amd_headers()
    product: AmdProductInfo | None = None
    version: str | None = None
    date: str | None = None
    source = "strona produktu AMD"
    try:
        product = parse_amd_product_page(
            fetch_text(session, AMD_PRODUCT_URL, allowed_hosts=AMD_META_HOSTS, headers=headers)
        )
        version, date = product.version, product.release_date
    except DriverError as exc:
        attempts.append(f"strona produktu ({exc.message})")
        try:
            version = parse_amd_drivers_page(
                fetch_text(session, AMD_DRIVERS_URL, allowed_hosts=AMD_META_HOSTS, headers=headers)
            )
            source = "ogólna strona sterowników AMD (plan B)"
        except DriverError as exc2:
            attempts.append(f"ogólna strona sterowników ({exc2.message})")
            raise _combine_errors("AMD", attempts, AMD_PAGE) from None
    assert version is not None
    notes_url = (product.notes_url if product else None) or amd_release_notes_url(version)
    try:
        notes = parse_amd_release_notes(
            fetch_text(session, notes_url, allowed_hosts=AMD_META_HOSTS, headers=headers),
            expected_version=version,
        )
        # Ties the package to the version even when the notes' <title> has no version.
        if not url_has_version(notes.url, version):
            raise DriverError(f"link do pakietu nie dotyczy wersji {version}")
        warnings: tuple[str, ...] = ()
        if product and product.label and "recommended" not in product.label.lower():
            warnings = (f"Wydanie AMD oznaczone jako: {product.label}.",)
        return DriverRelease(
            vendor="amd",
            version=version,
            url=notes.url,
            release_date=date or notes.release_date,
            source=f"{source} + informacje o wydaniu",
            notes=warnings,
        )
    except DriverError as exc:
        attempts.append(f"informacje o wydaniu ({exc.message})")
        if product and product.package_url and url_has_version(product.package_url, version):
            return DriverRelease(
                vendor="amd",
                version=version,
                url=product.package_url,
                release_date=date,
                source="strona produktu AMD (pakiet pełny, plan B)",
                approx_size=product.approx_size,
                notes=(
                    "Nie udało się odczytać pakietu uniwersalnego z informacji o wydaniu; "
                    "użyto pakietu ze strony produktu, który obsługuje tylko karty "
                    "Radeon RX 7000/9000.",
                ),
            )
        raise _combine_errors("AMD", attempts, AMD_PAGE) from None


def _intel_page_headers() -> dict[str, str]:
    # Akamai in front of www.intel.com answers 403 unless the User-Agent starts with
    # "python-requests/" or "curl/" (checked 2026-11-20).
    import requests

    return {"User-Agent": f"python-requests/{requests.__version__} ({user_agent()})"}


def resolve_intel(session: Any) -> DriverRelease:
    attempts: list[str] = []
    try:
        raw = fetch_bytes(
            session, INTEL_DSA_URL, allowed_hosts=INTEL_META_HOSTS, limit=MAX_DSA_ZIP_BYTES
        )
        return parse_intel_dsa(raw)
    except DriverError as exc:
        attempts.append(f"dane Intel DSA ({exc.message})")
    try:
        text = fetch_text(
            session, INTEL_PAGE_URL, allowed_hosts=INTEL_META_HOSTS, headers=_intel_page_headers()
        )
        return parse_intel_page(text)
    except DriverError as exc:
        attempts.append(f"strona pobierania Intel ({exc.message})")
    raise _combine_errors("Intel", attempts, INTEL_PAGE)


# ------------------------------------------------------------------ vendor registry


def _ok(message: str, reboot: bool = False) -> ExitCodeInfo:
    return ExitCodeInfo(True, message, reboot)


def _err(message: str) -> ExitCodeInfo:
    return ExitCodeInfo(False, message)


_RESTART_RECOMMENDED = "Zalecany restart komputera."
_RESTART_REQUIRED = "Uruchom ponownie komputer, aby dokończyć instalację."


@dataclass(frozen=True)
class DriverVendor:
    id: str
    name: str  # "NVIDIA"
    product: str  # "GeForce Game Ready"
    resolve: Callable[[Any], DriverRelease]
    download_hosts: tuple[str, ...]
    #: Required ``ATTR=value`` of the signer subject (value may be followed by ", Inc.").
    signer: str
    install_args: tuple[str, ...]
    exit_codes: Mapping[int, ExitCodeInfo]
    install_timeout: float  # seconds
    vendor_url: str  # manual download page shown on errors
    approx_size: int  # bytes; space check before the real size is known
    download_headers: Mapping[str, str] = field(default_factory=dict)

    def describe_exit(self, returncode: int) -> ExitCodeInfo:
        """Exit code -> outcome; unknown codes are errors."""
        if returncode in self.exit_codes:
            return self.exit_codes[returncode]
        return _err("Instalator zakończył się nieznanym kodem błędu.")


VENDORS: Mapping[str, DriverVendor] = {
    "nvidia": DriverVendor(
        id="nvidia",
        name="NVIDIA",
        product="GeForce Game Ready",
        resolve=lambda s: resolve_nvidia(s),
        download_hosts=NVIDIA_DOWNLOAD_HOSTS,
        signer="O=NVIDIA Corporation",
        install_args=("-s", "-noreboot", "-noeula"),
        exit_codes={
            0: _ok(_RESTART_RECOMMENDED),
            1: _ok(_RESTART_REQUIRED, reboot=True),
        },
        install_timeout=30 * 60,
        vendor_url=NVIDIA_PAGE,
        approx_size=1100 * _MB,
    ),
    "amd": DriverVendor(
        id="amd",
        name="AMD",
        product="Radeon Adrenalin",
        resolve=lambda s: resolve_amd(s),
        download_hosts=AMD_DOWNLOAD_HOSTS,
        signer="O=Advanced Micro Devices",
        install_args=("-INSTALL",),
        exit_codes={0: _ok(_RESTART_RECOMMENDED)},
        install_timeout=40 * 60,
        vendor_url=AMD_PAGE,
        approx_size=1800 * _MB,
        # Without the Referer drivers.amd.com redirects to "Download-Incomplete.html".
        download_headers={"Referer": "https://www.amd.com/"},
    ),
    "intel": DriverVendor(
        id="intel",
        name="Intel",
        product="Arc",
        resolve=lambda s: resolve_intel(s),
        download_hosts=INTEL_DOWNLOAD_HOSTS,
        signer="O=Intel Corporation",
        install_args=("-s", "--terminateProcesses"),
        exit_codes={
            0: _ok(_RESTART_RECOMMENDED),
            2: _ok("Instalator właśnie uruchamia komputer ponownie.", reboot=True),
            14: _ok(_RESTART_REQUIRED, reboot=True),
            17: _ok("Instalator właśnie wyłącza komputer.", reboot=True),
            18: _ok(
                "Wymagane pełne wyłączenie i ponowne włączenie komputera (nie sam restart).",
                reboot=True,
            ),
            1: _err("Ogólny błąd instalatora Intel (szczegóły w dzienniku IntelGFX.log)."),
            5: _err("Instalator Intel nie obsługuje tej platformy ani systemu."),
            6: _err("Instalator Intel został zamknięty."),
            7: _err("Instalator Intel nie przyjął parametrów wywołania."),
            8: _err(
                "Instalator Intel nie znalazł sterownika pasującego do urządzeń w tym "
                "komputerze (brak zgodnej grafiki Intel?)."
            ),
            9: _err("Pliki sterownika Intel nie mają prawidłowego podpisu cyfrowego."),
            10: _err("Dodatki sterownika Intel nie mają prawidłowego podpisu cyfrowego."),
            11: _err("Za mało miejsca na dysku."),
            13: _err(
                "Przed instalacją wymagany jest restart komputera. Uruchom go ponownie "
                "i spróbuj jeszcze raz."
            ),
            15: _err("Zamknij programy blokujące instalację i spróbuj ponownie."),
            16: _err("Trwa inna instalacja. Spróbuj ponownie później."),
        },
        install_timeout=30 * 60,
        vendor_url=INTEL_PAGE,
        approx_size=900 * _MB,
    ),
}


def vendor(vendor_id: str | None) -> DriverVendor:
    try:
        return VENDORS[vendor_id or ""]
    except KeyError:
        raise DriverError(f"Nieznany producent sterownika: {vendor_id!r}.") from None


def resolve(vendor_id: str, session: Any | None = None) -> DriverRelease:
    """Newest gaming driver of ``vendor_id`` (source A, then plan B)."""
    spec = vendor(vendor_id)
    release = spec.resolve(session if session is not None else default_session())
    if not host_allowed(release.url, spec.download_hosts):
        raise DriverError(
            f"Niedozwolony adres pobierania: {release.url}", vendor_url=spec.vendor_url
        )
    return release


# ------------------------------------------------------------------- system layer
# Small OS wrappers; tests replace them with monkeypatch.


def free_space(path: Path) -> int:
    return shutil.disk_usage(path).free


def remove_file(path: Path) -> None:
    """Delete ``path``; retries briefly (an installer that was just killed may hold it)."""
    for attempt in range(10):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.3)


_SYSTEM_TEMP_DIRS = ("SystemTemp", "Temp")
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_system_temp(path: Path) -> bool:
    windir = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
    try:
        resolved = os.path.normcase(str(path.resolve()))
    except OSError:
        resolved = os.path.normcase(str(path))
    for name in _SYSTEM_TEMP_DIRS:
        root = os.path.normcase(os.path.join(windir, name))
        if resolved == root or resolved.startswith(root + os.sep):
            return True
    return False


def _is_reparse_point(path: Path) -> bool:
    """Junction, symlink or other reparse point (never followed for our folders)."""
    attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def download_dir(app_dir: Path | None = None) -> Path:
    """A NEW, empty folder with a random name for one installer.

    Created with :func:`tempfile.mkdtemp` (owner-only ACL on Windows; when elevated the
    owner is the Administrators group, so a non-elevated process of the same user cannot
    write into it) under the user's %TEMP%, else next to the program. A folder that
    already exists is never reused, and junctions/symlinks are refused (as the base and
    as the result). ``C:\\Windows\\SystemTemp`` (TEMP of SYSTEM) is skipped: the Intel
    package reportedly does not install from there. The caller removes the folder.
    """
    bases = [Path(tempfile.gettempdir())]
    if app_dir is not None:
        bases.append(app_dir)
    for base in bases:
        if _is_system_temp(base):
            continue
        try:
            base.mkdir(parents=True, exist_ok=True)
            if _is_reparse_point(base):
                continue
            folder = Path(tempfile.mkdtemp(prefix=f"{DOWNLOAD_SUBDIR}-", dir=base))
            if _is_reparse_point(folder) or folder.parent != base:
                with contextlib.suppress(OSError):
                    folder.rmdir()
                continue
            return folder
        except OSError:
            continue
    raise DriverError("Nie znaleziono katalogu z prawem zapisu na instalator sterownika.")


class _StopSignal(threading.Event):
    """Looks "set" once the user cancels or the deadline passes (polled by stream_process)."""

    def __init__(self, cancel_event: threading.Event | None, deadline: float) -> None:
        super().__init__()
        self._cancel = cancel_event
        self._deadline = deadline

    def is_set(self) -> bool:
        return (self._cancel is not None and self._cancel.is_set()) or (
            time.monotonic() >= self._deadline
        )


# Win32 constants (winnt.h / winbase.h / tlhelp32.h).
_GENERIC_READ = 0x80000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_CREATE_SUSPENDED = 0x00000004
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_THREAD_SUSPEND_RESUME = 0x0002
_TH32CS_SNAPTHREAD = 0x00000004
_TH32CS_SNAPPROCESS = 0x00000002
_JOB_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_BASIC_PROCESS_ID_LIST = 3
_MAX_LISTED_PIDS = 64


class _JobAccounting(ctypes.Structure):  # JOBOBJECT_BASIC_ACCOUNTING_INFORMATION
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _JobPidList(ctypes.Structure):  # JOBOBJECT_BASIC_PROCESS_ID_LIST (fixed capacity)
    _fields_ = [
        ("NumberOfAssignedProcesses", ctypes.c_uint32),
        ("NumberOfProcessIdsInList", ctypes.c_uint32),
        ("ProcessIdList", ctypes.c_size_t * _MAX_LISTED_PIDS),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_int32),
        ("tpDeltaPri", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
    ]


class _ProcessEntry(ctypes.Structure):  # PROCESSENTRY32W
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class _FileInfo(ctypes.Structure):  # BY_HANDLE_FILE_INFORMATION
    _fields_ = [
        ("dwFileAttributes", ctypes.c_uint32),
        ("ftCreationTime", ctypes.c_uint64),
        ("ftLastAccessTime", ctypes.c_uint64),
        ("ftLastWriteTime", ctypes.c_uint64),
        ("dwVolumeSerialNumber", ctypes.c_uint32),
        ("nFileSizeHigh", ctypes.c_uint32),
        ("nFileSizeLow", ctypes.c_uint32),
        ("nNumberOfLinks", ctypes.c_uint32),
        ("nFileIndexHigh", ctypes.c_uint32),
        ("nFileIndexLow", ctypes.c_uint32),
    ]


@functools.cache
def _kernel32() -> Any:
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    k.QueryInformationJobObject.restype = wintypes.BOOL
    k.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    k.Process32FirstW.restype = wintypes.BOOL
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k.Process32NextW.restype = wintypes.BOOL
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.TerminateJobObject.restype = wintypes.BOOL
    k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenThread.restype = wintypes.HANDLE
    k.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.ResumeThread.restype = wintypes.DWORD
    k.ResumeThread.argtypes = [wintypes.HANDLE]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Thread32First.restype = wintypes.BOOL
    k.Thread32First.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k.Thread32Next.restype = wintypes.BOOL
    k.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k.GetFileInformationByHandle.restype = wintypes.BOOL
    k.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k.CloseHandle.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


def _win_error(what: str) -> OSError:
    code = ctypes.get_last_error()
    return OSError(code, f"{what}: {ctypes.FormatError(code).strip()}", None, code)


class FileLock:
    """Read-only handles that keep a file (and its folder) from changing.

    The file is opened with ``FILE_SHARE_READ`` only: while the handle exists nobody can
    open it for writing, delete or rename it, but reading and executing (CreateProcess,
    a self-extractor reading its own payload) still work. The folder is opened without
    ``FILE_SHARE_DELETE``, so it cannot be renamed or deleted and replaced by another
    folder of the same name. Reparse points (symlinks) are refused.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handles: list[int] = []
        k = _kernel32()
        try:
            self._handles.append(
                self._open(
                    k,
                    path.parent,
                    _FILE_SHARE_READ | _FILE_SHARE_WRITE,
                    _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
                )
            )
            handle = self._open(k, path, _FILE_SHARE_READ, _FILE_FLAG_OPEN_REPARSE_POINT)
            self._handles.append(handle)
            info = _FileInfo()
            if not k.GetFileInformationByHandle(handle, ctypes.byref(info)):
                raise _win_error("GetFileInformationByHandle")
            if info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise OSError(f"{path} jest dowiązaniem, a nie zwykłym plikiem")
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _open(k: Any, path: Path, share: int, flags: int) -> int:
        handle = k.CreateFileW(str(path), _GENERIC_READ, share, None, _OPEN_EXISTING, flags, None)
        if not handle or handle == _INVALID_HANDLE:
            raise _win_error(f"CreateFileW({path})")
        if flags & _FILE_FLAG_BACKUP_SEMANTICS:
            info = _FileInfo()
            ok = k.GetFileInformationByHandle(handle, ctypes.byref(info))
            if not ok or info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                k.CloseHandle(handle)
                raise OSError(f"{path} jest dowiązaniem albo jest niedostępny")
        return int(handle)

    def close(self) -> None:
        while self._handles:
            with contextlib.suppress(OSError, AttributeError):
                _kernel32().CloseHandle(self._handles.pop())

    def __enter__(self) -> FileLock:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def lock_file(path: Path) -> FileLock:
    """System layer: see :class:`FileLock`. Raises ``OSError`` when it cannot lock."""
    return FileLock(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _job_for(proc: subprocess.Popen[bytes]) -> int | None:
    """New Job Object holding ``proc`` (still suspended), WITHOUT ``KILL_ON_JOB_CLOSE``.

    The job is used to kill the whole tree on cancel/timeout and to wait for children
    after a normal exit; closing it (also when Winstaller itself crashes or is killed)
    never kills a driver installation in progress. The process is opened by its public
    ``pid`` (the Popen object keeps the process alive, so the id cannot be reused
    meanwhile). Returns None when anything fails.
    """
    k = _kernel32()
    job = k.CreateJobObjectW(None, None)
    if not job:
        return None
    process = None
    try:
        process = k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, proc.pid)
        if not process or not k.AssignProcessToJobObject(job, process):
            raise _win_error("AssignProcessToJobObject")
        return int(job)
    except OSError:
        k.CloseHandle(job)
        return None
    finally:
        if process:
            k.CloseHandle(process)


def _resume(proc: subprocess.Popen[bytes]) -> None:
    """Resume every thread of a process started with ``CREATE_SUSPENDED``.

    Popen closes the primary thread handle, so the threads are found by owner pid.
    Raises ``OSError`` when no thread could be resumed."""
    k = _kernel32()
    snapshot = k.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == _INVALID_HANDLE:
        raise _win_error("CreateToolhelp32Snapshot")
    resumed = 0
    try:
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = k.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == proc.pid:
                thread = k.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread:
                    try:
                        if k.ResumeThread(thread) != 0xFFFFFFFF:
                            resumed += 1
                    finally:
                        k.CloseHandle(thread)
            more = k.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    if not resumed:
        raise OSError(f"Nie udało się wznowić procesu {proc.pid}.")


def _terminate_job(job: int) -> None:
    with contextlib.suppress(OSError, AttributeError):
        _kernel32().TerminateJobObject(job, 1)


def _close_job(job: int) -> None:
    with contextlib.suppress(OSError, AttributeError):
        _kernel32().CloseHandle(job)


def _job_active_processes(job: int) -> int | None:
    """Number of processes of the job still running (None when it cannot be read)."""
    info = _JobAccounting()
    try:
        ok = _kernel32().QueryInformationJobObject(
            job, _JOB_BASIC_ACCOUNTING_INFORMATION, ctypes.byref(info), ctypes.sizeof(info), None
        )
    except (OSError, AttributeError):
        return None
    return int(info.ActiveProcesses) if ok else None


#: Started by Windows for every console program (also in our job); it ends by itself
#: with its last client, so it is neither waited for nor reported.
_IGNORED_JOB_PROCESSES = frozenset({"conhost.exe"})


def _job_processes(job: int) -> list[str] | None:
    """Exe names (one per running process) of the job's processes, without console
    hosts; None when the job or the process list cannot be read."""
    k = _kernel32()
    pids = _JobPidList()
    try:
        ok = k.QueryInformationJobObject(
            job, _JOB_BASIC_PROCESS_ID_LIST, ctypes.byref(pids), ctypes.sizeof(pids), None
        )
    except (OSError, AttributeError):
        return None
    # ERROR_MORE_DATA (more than _MAX_LISTED_PIDS) still fills the list we have room for.
    if not ok and ctypes.get_last_error() != 234:
        return None
    exe_names = _process_names()
    if not exe_names:
        return None
    names: list[str] = []
    # The list also holds processes that already exited (handles still open, e.g. the
    # main process held by Popen); only those in the live snapshot are running.
    for pid in pids.ProcessIdList[: min(pids.NumberOfProcessIdsInList, _MAX_LISTED_PIDS)]:
        name = exe_names.get(int(pid))
        if name is not None and name.lower() not in _IGNORED_JOB_PROCESSES:
            names.append(name)
    return names


def _process_names() -> dict[int, str]:
    """pid -> exe name of all processes (Toolhelp32 snapshot; needs no process access,
    unlike QueryFullProcessImageNameW, which is refused for some installer children)."""
    k = _kernel32()
    snapshot = k.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == _INVALID_HANDLE:
        return {}
    names: dict[int, str] = {}
    try:
        entry = _ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = k.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            names[int(entry.th32ProcessID)] = entry.szExeFile
            more = k.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    return names


def _job_remaining(job: int) -> tuple[int, list[str]]:
    """(count, distinct names) of the job's processes that are worth waiting for."""
    active = _job_active_processes(job)
    if active == 0:
        return 0, []
    names = _job_processes(job)
    if names is None:  # cannot tell which ones: trust the job's own counter
        return active or 0, []
    return len(names), sorted(set(names))


#: How long to wait, after the installer's main process exited, for processes it left in
#: its job (the real setup.exe behind a self-extractor, a finishing helper). Then the
#: result is returned and those processes keep running - they are never killed for this.
POST_EXIT_GRACE_SECONDS = 180.0
_WAIT_POLL_SECONDS = 0.2


def _wait_for_children(
    job: int,
    stop: _StopSignal,
    cancel_event: threading.Event | None,
    on_line: LineCallback,
) -> str:
    """After a normal exit: wait for the rest of the job.

    Returns ``"done"`` (nothing left), ``"cancelled"`` (user), ``"timeout"`` (overall
    deadline) or ``"left"`` (grace over, processes still running and left alone)."""
    active, names = _job_remaining(job)
    if not active:
        return "done"
    listed = f": {', '.join(names)}" if names else ""
    on_line(
        "Główny proces instalatora zakończył pracę; czekam na zakończenie pozostałych "
        f"procesów instalatora{listed}..."
    )
    grace_end = time.monotonic() + POST_EXIT_GRACE_SECONDS
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return "cancelled"
        if stop.is_set():
            return "timeout"
        active, current = _job_remaining(job)
        if not active:
            return "done"
        if time.monotonic() >= grace_end:
            names = current or names
            listed = f" ({', '.join(names)})" if names else ""
            on_line(
                f"Ostrzeżenie: procesy instalatora nadal działają w tle{listed}. Winstaller "
                "nie czeka dłużej i ich nie przerywa; poczekaj na ich zakończenie przed "
                "ponownym uruchomieniem komputera."
            )
            return "left"
        if cancel_event is not None:
            cancel_event.wait(_WAIT_POLL_SECONDS)
        else:
            time.sleep(_WAIT_POLL_SECONDS)


def run_process(
    args: Sequence[str],
    *,
    on_line: LineCallback,
    on_replace_last: LineCallback,
    cancel_event: threading.Event | None,
    timeout: float,
    cwd: Path | None = None,
    kill_tree: bool = True,
) -> ProcessResult:
    """Run ``args`` streaming output; stop it on cancel or after ``timeout`` seconds.

    With ``kill_tree`` the process is started suspended, put into a Job Object and only
    then resumed, so no child can start outside the job. Cancel/timeout (also while
    waiting) terminates every process of the job. After a normal exit of the main
    process the remaining processes of the job are waited for, up to
    :data:`POST_EXIT_GRACE_SECONDS`; after that they are left running
    (``left_running=True``). The exit code is always the main process's. Raises
    ``OSError`` when the program cannot start.
    """
    stop = _StopSignal(cancel_event, time.monotonic() + timeout)
    job: int | None = None

    def on_start(proc: subprocess.Popen[bytes]) -> None:
        nonlocal job
        job = _job_for(proc)
        if job is None:
            on_line("Ostrzeżenie: po przerwaniu procesy potomne instalatora mogą działać dalej.")
        _resume(proc)

    completed = False
    state = "done"
    try:
        outcome = winget.stream_process(
            list(args),
            on_line=on_line,
            on_replace_last=on_replace_last,
            cancel_event=stop,
            cwd=str(cwd) if cwd is not None else None,
            on_start=on_start if kill_tree else None,
            creationflags=_CREATE_SUSPENDED if kill_tree else 0,
        )
        completed = True
        if job is not None and not outcome.cancelled:
            state = _wait_for_children(job, stop, cancel_event, on_line)
    finally:
        if job is not None:
            # Stopped (cancel/timeout) or broken while starting: kill the whole tree.
            if not completed or outcome.cancelled or state in ("cancelled", "timeout"):
                _terminate_job(job)
            _close_job(job)  # no KILL_ON_JOB_CLOSE: closing never kills anything
    if state == "cancelled":
        return ProcessResult(-1, cancelled=True)
    if state == "timeout":
        return ProcessResult(-1, timed_out=True)
    user_cancel = cancel_event is not None and cancel_event.is_set()
    return ProcessResult(
        outcome.returncode,
        cancelled=outcome.cancelled and user_cancel,
        timed_out=outcome.cancelled and not user_cancel,
        left_running=state == "left",
    )


# ----------------------------------------------------------------------- download


def _check_free_space(folder: Path, size: int) -> None:
    needed = size * FREE_SPACE_FACTOR
    try:
        free = free_space(folder)
    except OSError:
        return  # cannot tell; the download itself will fail with a clear error
    if free < needed:
        raise DriverError(
            f"Za mało miejsca na dysku: potrzeba ok. {needed / 1024**3:.1f} GB "
            f"(pobranie i rozpakowanie), wolne {free / 1024**3:.1f} GB w {folder}."
        )


_BAD_CONTENT_TYPES = ("text/", "html", "json", "xml")


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    path: Path
    sha256: str  # of the bytes received (re-checked from disk before running the file)
    size: int


def download(
    release: DriverRelease,
    dest_dir: str | os.PathLike[str],
    *,
    session: Any | None = None,
    progress_cb: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Like :func:`download_verified`, returning only the path."""
    return download_verified(
        release, dest_dir, session=session, progress_cb=progress_cb, cancel_event=cancel_event
    ).path


def download_verified(
    release: DriverRelease,
    dest_dir: str | os.PathLike[str],
    *,
    session: Any | None = None,
    progress_cb: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
) -> DownloadedFile:
    """Stream the installer into ``dest_dir`` and verify it.

    Checks: https + vendor host on every redirect hop, HTTP 200, no text/HTML content
    type, ``MZ`` signature, size against ``Content-Length`` and the vendor metadata,
    hard limit :data:`MAX_DOWNLOAD_BYTES` (also without ``Content-Length``), SHA1/SHA512
    when known, free space (size x 3) before writing. SHA-256 of the received bytes is
    returned for the later re-check. Data goes to ``<name>.part`` first; leftovers of
    an earlier run with the same name are removed first, the partial file on any error
    or cancellation. Raises :class:`DriverCancelled` when cancelled, :class:`DriverError`
    otherwise.
    """
    spec = vendor(release.vendor)
    folder = Path(dest_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / sanitize_filename(release.url, f"{spec.id}-driver.exe")
    part = target.with_name(target.name + ".part")
    for leftover in (target, part):  # never mistake an old file for this download
        with contextlib.suppress(OSError):
            leftover.unlink(missing_ok=True)
    if not host_allowed(release.url, spec.download_hosts):  # before any request
        raise DriverError(
            f"Niedozwolony adres pobierania: {release.url}", vendor_url=spec.vendor_url
        )
    sess = session if session is not None else default_session()
    _check_free_space(folder, release.size or release.approx_size or spec.approx_size)

    sha1 = hashlib.sha1(usedforsecurity=False) if release.sha1 else None
    sha512 = hashlib.sha512() if release.sha512 else None
    sha256 = hashlib.sha256()
    received = 0
    headers = {"User-Agent": user_agent(), **spec.download_headers}
    try:
        response = open_url(
            sess,
            release.url,
            allowed_hosts=spec.download_hosts,
            headers=headers,
            timeout=DOWNLOAD_TIMEOUT,
            stream=True,
            vendor_url=spec.vendor_url,
        )
        try:
            status = getattr(response, "status_code", 0)
            if status != 200:
                raise DriverError(
                    f"Pobieranie sterownika nie powiodło się (HTTP {status}).",
                    vendor_url=spec.vendor_url,
                )
            ctype = str((response.headers or {}).get("content-type") or "").lower()
            if any(bad in ctype for bad in _BAD_CONTENT_TYPES):
                raise DriverError(
                    f"Serwer zamiast instalatora zwrócił stronę ({ctype}).",
                    vendor_url=spec.vendor_url,
                )
            length = _content_length(response)
            if length is not None and length < MIN_INSTALLER_BYTES:
                # drivers.amd.com answers HEAD with "Content-Length: 20" for a 1.7 GB file;
                # such a value is not a size. Truncation is still caught (MZ, minimum
                # size, signature).
                length = None
            if release.size and length is not None and length != release.size:
                raise DriverError(
                    f"Rozmiar pliku na serwerze ({length} B) nie zgadza się z danymi producenta "
                    f"({release.size} B).",
                    vendor_url=spec.vendor_url,
                )
            total = release.size or length
            if total and total > MAX_DOWNLOAD_BYTES:
                raise DriverError(_too_big(total), vendor_url=spec.vendor_url)
            if total:
                _check_free_space(folder, total)
            with part.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=_CHUNK):
                    if cancel_event is not None and cancel_event.is_set():
                        raise DriverCancelled("Pobieranie sterownika przerwane.")
                    if not chunk:
                        continue
                    if received == 0 and not chunk.startswith(b"MZ"):
                        raise DriverError(
                            "Pobrany plik nie jest programem Windows (brak nagłówka MZ).",
                            vendor_url=spec.vendor_url,
                        )
                    received += len(chunk)
                    if total and received > total:
                        raise DriverError("Serwer wysłał więcej danych niż zapowiadał.")
                    if received > MAX_DOWNLOAD_BYTES:
                        raise DriverError(_too_big(received), vendor_url=spec.vendor_url)
                    fh.write(chunk)
                    sha256.update(chunk)
                    if sha1 is not None:
                        sha1.update(chunk)
                    if sha512 is not None:
                        sha512.update(chunk)
                    if progress_cb is not None:
                        progress_cb(received, total)
        except OSError as exc:  # network errors while reading, disk full while writing
            raise DriverError(f"Przerwano pobieranie sterownika: {_short(exc)}") from exc
        finally:
            _close(response)
        if cancel_event is not None and cancel_event.is_set():
            raise DriverCancelled("Pobieranie sterownika przerwane.")
        if received < MIN_INSTALLER_BYTES:
            raise DriverError(
                f"Pobrany plik jest za mały na instalator sterownika ({received} B).",
                vendor_url=spec.vendor_url,
            )
        if total and received != total:
            raise DriverError(
                f"Pobrano niepełny plik ({received} z {total} bajtów).", vendor_url=spec.vendor_url
            )
        if sha1 is not None and sha1.hexdigest() != release.sha1:
            raise DriverError("Suma kontrolna SHA1 pobranego sterownika się nie zgadza.")
        if sha512 is not None and sha512.hexdigest() != release.sha512:
            raise DriverError("Suma kontrolna SHA512 pobranego sterownika się nie zgadza.")
        try:
            os.replace(part, target)
        except OSError as exc:
            raise DriverError(f"Nie udało się zapisać instalatora: {_short(exc)}") from exc
    except BaseException:
        with contextlib.suppress(OSError):
            remove_file(part)
        raise
    return DownloadedFile(target, sha256.hexdigest(), received)


def _too_big(size: int) -> str:
    return (
        f"Plik na serwerze jest podejrzanie duży ({size / 1024**3:.1f} GB, limit "
        f"{MAX_DOWNLOAD_BYTES / 1024**3:.0f} GB)."
    )


# ---------------------------------------------------------------------- signature

#: Every character PowerShell accepts as a single quote (its own
#: ``CodeGeneration.EscapeSingleQuotedStringContent`` doubles exactly these).
_PS_SINGLE_QUOTES = "'‘’‚‛"


def ps_single_quoted(text: str) -> str:
    """``text`` as a PowerShell single-quoted literal: each of the five quote characters
    is doubled, nothing else is special inside such a literal (no ``$``/backtick
    expansion). Control characters (impossible in Windows paths) are refused."""
    if any(ord(ch) < 32 for ch in text):
        raise DriverError("Ścieżka pliku zawiera niedozwolone znaki sterujące.")
    return "'" + "".join(ch * 2 if ch in _PS_SINGLE_QUOTES else ch for ch in text) + "'"


def build_signature_script(path: str | os.PathLike[str]) -> str:
    """PowerShell printing the Authenticode result of ``path`` as one JSON line."""
    literal = ps_single_quoted(os.fspath(path))
    return (
        winget.POWERSHELL_UTF8_PREAMBLE + "\n"
        f"$s = Get-AuthenticodeSignature -LiteralPath {literal}\n"
        "$subject = ''; $thumb = ''\n"
        "if ($s.SignerCertificate) { $subject = $s.SignerCertificate.Subject; "
        "$thumb = $s.SignerCertificate.Thumbprint }\n"
        "[pscustomobject]@{ Status = [string]$s.Status; StatusMessage = [string]$s.StatusMessage; "
        "Subject = [string]$subject; Thumbprint = [string]$thumb; "
        "SignatureType = [string]$s.SignatureType } | ConvertTo-Json -Compress\n"
    )


def powershell_args(script: str) -> list[str]:
    """PowerShell (absolute System32 path, never searched by name) running ``script``."""
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


@_parser
def parse_signature_output(lines: Sequence[str]) -> SignatureInfo:
    """The JSON line written by :func:`build_signature_script`."""
    for line in reversed(lines):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        if isinstance(data, dict) and "Status" in data and isinstance(data["Status"], str | None):
            return SignatureInfo(
                status=data["Status"] or "",
                subject=str(data.get("Subject") or ""),
                status_message=str(data.get("StatusMessage") or ""),
                thumbprint=str(data.get("Thumbprint") or ""),
                signature_type=str(data.get("SignatureType") or ""),
            )
    raise DriverError("Nie udało się odczytać wyniku weryfikacji podpisu cyfrowego.")


def parse_distinguished_name(subject: str) -> list[tuple[str, str]]:
    """``CN=x, O="A, Inc.", C=US`` -> ``[("CN","x"), ("O","A, Inc."), ("C","US")]``."""
    parts: list[tuple[str, str]] = []
    i, n = 0, len(subject)
    while i < n:
        while i < n and subject[i] in " ,;+":
            i += 1
        eq = subject.find("=", i)
        if eq < 0:
            break
        key = subject[i:eq].strip().upper()
        i = eq + 1
        value: list[str] = []
        if i < n and subject[i] == '"':
            i += 1
            while i < n:
                if subject[i] == '"':
                    if i + 1 < n and subject[i + 1] == '"':
                        value.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                value.append(subject[i])
                i += 1
            while i < n and subject[i] not in ",;+":
                i += 1
        else:
            while i < n and subject[i] not in ",;+":
                if subject[i] == "\\" and i + 1 < n:
                    i += 1
                value.append(subject[i])
                i += 1
        parts.append((key, "".join(value).strip()))
    return parts


#: Closed list of accepted legal names per expected signer (exact, case-sensitive).
SIGNER_NAMES: Mapping[str, tuple[str, ...]] = {
    "O=NVIDIA Corporation": ("NVIDIA Corporation",),
    "O=Advanced Micro Devices": ("Advanced Micro Devices", "Advanced Micro Devices, Inc."),
    "O=Intel Corporation": ("Intel Corporation",),
}
SIGNER_COUNTRY = "US"


def subject_matches(subject: str, expected: str) -> bool:
    """True when the signer ``subject`` is the organisation ``expected`` (``"O=..."``).

    Requires exactly one ``O=`` attribute whose value is one of the exact legal names in
    :data:`SIGNER_NAMES` for ``expected`` (or exactly the value of ``expected`` when it
    is not listed), compared case-sensitively, and exactly one ``C=`` equal to
    :data:`SIGNER_COUNTRY`. Attribute names are case-insensitive (DN rules)."""
    key, sep, value = expected.partition("=")
    value = value.strip()
    if key.strip().upper() != "O" or not sep or not value:
        return False
    allowed = SIGNER_NAMES.get(f"O={value}", (value,))
    attributes = parse_distinguished_name(subject)
    organisations = [v for k, v in attributes if k == "O"]
    countries = [v for k, v in attributes if k == "C"]
    return len(organisations) == 1 and organisations[0] in allowed and countries == [SIGNER_COUNTRY]


def verify_signature(
    path: str | os.PathLike[str],
    expected_subject_fragment: str,
    *,
    cancel_event: threading.Event | None = None,
    timeout: float = SIGNATURE_TIMEOUT,
) -> SignatureInfo:
    """Authenticode check via ``Get-AuthenticodeSignature`` (PowerShell, UTF-8 output).

    Passes only when PowerShell exits with 0, the status is ``Valid`` and the signer
    subject matches ``expected_subject_fragment`` (see :func:`subject_matches`).
    Otherwise raises :class:`DriverError` (:class:`DriverCancelled` on cancel); the file
    must not be run.
    """
    lines: list[str] = []

    def replace_last(text: str) -> None:
        if lines:
            lines[-1] = text
        else:
            lines.append(text)

    try:
        result = run_process(
            powershell_args(build_signature_script(path)),
            on_line=lines.append,
            on_replace_last=replace_last,
            cancel_event=cancel_event,
            timeout=timeout,
            kill_tree=False,
        )
    except OSError as exc:
        raise DriverError(f"Nie udało się uruchomić weryfikacji podpisu: {exc}") from exc
    if result.cancelled:
        raise DriverCancelled("Przerwano weryfikację podpisu.")
    if result.timed_out:
        raise DriverError("Weryfikacja podpisu cyfrowego trwała zbyt długo.")
    if result.returncode != 0:
        raise DriverError(
            "Weryfikacja podpisu cyfrowego nie powiodła się "
            f"(PowerShell, {_code_text(result.returncode)})."
        )
    info = parse_signature_output(lines)
    if not info.status:
        raise DriverError(f"Nie udało się sprawdzić podpisu cyfrowego pliku {Path(path).name}.")
    if info.status != "Valid":
        detail = f" ({info.status_message})" if info.status_message else ""
        raise DriverError(f"Nieprawidłowy podpis cyfrowy instalatora: {info.status}{detail}.")
    if not subject_matches(info.subject, expected_subject_fragment):
        raise DriverError(
            f"Instalator jest podpisany przez innego wydawcę ({info.subject or 'brak'}), "
            f"oczekiwano {expected_subject_fragment}."
        )
    return info


# ----------------------------------------------------------------------- install


def install(
    spec: DriverVendor,
    path: Path,
    *,
    log: LineCallback,
    replace_last: LineCallback,
    cancel_event: threading.Event | None = None,
) -> ProcessResult:
    """Run the verified installer silently (``spec.install_args``) with the vendor timeout."""
    try:
        return run_process(
            [str(path), *spec.install_args],
            on_line=log,
            on_replace_last=replace_last,
            cancel_event=cancel_event,
            timeout=spec.install_timeout,
            cwd=path.parent,
        )
    except OSError as exc:
        raise DriverError(
            f"Nie udało się uruchomić instalatora {path.name}: {exc}", vendor_url=spec.vendor_url
        ) from exc


def _code_text(code: int) -> str:
    return f"kod {code}" if 0 <= code <= 0xFFFF else f"kod {code} / 0x{code & 0xFFFFFFFF:08X}"


def _release_summary(release: DriverRelease, spec: DriverVendor) -> str:
    date = f" ({release.release_date})" if release.release_date else ""
    size = release.size or release.approx_size
    size_text = f", rozmiar {'' if release.size else 'ok. '}{format_mb(size)}" if size else ""
    return f"Najnowsza wersja: {release.version}{date}{size_text}. Źródło: {release.source}."


_CANCELLED = "Przerwano przez użytkownika."


def install_latest(
    vendor_id: str,
    *,
    log: LineCallback,
    replace_last: LineCallback,
    cancel_event: threading.Event | None = None,
    session: Any | None = None,
    work_dir: Path | None = None,
    app_dir: Path | None = None,
) -> DriverInstallResult:
    """Resolve, download, verify and silently install the newest driver of ``vendor_id``.

    Never reboots. The downloaded installer is always deleted. Cancellation works at
    every stage (during installation it kills the installer's process tree). Raises
    :class:`DriverError` (always with ``vendor_url``) on failure; a cancel returns
    ``cancelled=True``.
    """
    spec = vendor(vendor_id)
    try:
        return _install_latest(
            spec,
            log=log,
            replace_last=replace_last,
            cancel=cancel_event or threading.Event(),
            session=session,
            work_dir=work_dir,
            app_dir=app_dir,
        )
    except DriverError as exc:
        exc.vendor_url = exc.vendor_url or spec.vendor_url
        raise


def _install_latest(
    spec: DriverVendor,
    *,
    log: LineCallback,
    replace_last: LineCallback,
    cancel: threading.Event,
    session: Any | None,
    work_dir: Path | None,
    app_dir: Path | None,
) -> DriverInstallResult:
    if cancel.is_set():
        return DriverInstallResult(False, _CANCELLED, cancelled=True)
    log(f"Ustalanie najnowszego sterownika {spec.name} {spec.product} u producenta...")
    release = resolve(spec.id, session)
    if cancel.is_set():
        return DriverInstallResult(False, _CANCELLED, cancelled=True)
    for note in release.notes:
        log(f"Ostrzeżenie: {note}")
    log(_release_summary(release, spec))
    log(f"Pobieranie {release.url}")

    folder = work_dir or download_dir(app_dir)
    path: Path | None = None
    lock: FileLock | None = None
    try:
        reporter = ProgressReporter(log, replace_last)
        try:
            downloaded = download_verified(
                release, folder, session=session, progress_cb=reporter, cancel_event=cancel
            )
        except DriverCancelled:
            return DriverInstallResult(False, _CANCELLED, cancelled=True, version=release.version)
        path = downloaded.path
        reporter.finish()
        log(f"Pobrano {path.name} ({format_mb(downloaded.size)}).")
        if release.sha1 or release.sha512:
            log("Suma kontrolna zgodna z danymi producenta.")
        if cancel.is_set():
            return DriverInstallResult(False, _CANCELLED, cancelled=True, version=release.version)

        # From here until the installer exits the file is held read-only (no one can
        # overwrite, rename or delete it) and its content is re-checked against what
        # was downloaded, so what gets verified and run is exactly the download.
        try:
            lock = lock_file(path)
        except OSError as exc:
            raise DriverError(
                f"Nie udało się zablokować pobranego instalatora przed zmianą: {_short(exc)}"
            ) from exc
        if sha256_file(path) != downloaded.sha256:
            raise DriverError(
                "Plik instalatora zmienił się po pobraniu, więc nie zostanie uruchomiony."
            )

        log("Weryfikacja podpisu cyfrowego...")
        try:
            info = verify_signature(path, spec.signer, cancel_event=cancel)
        except DriverCancelled:
            return DriverInstallResult(False, _CANCELLED, cancelled=True, version=release.version)
        log(f"Podpis cyfrowy prawidłowy: {info.subject}")
        if cancel.is_set():
            return DriverInstallResult(False, _CANCELLED, cancelled=True, version=release.version)

        log(f"Uruchamianie instalatora: {path.name} {' '.join(spec.install_args)}")
        log("Instalacja może potrwać kilka minut; obraz może na chwilę zgasnąć.")
        log(
            "Uwaga: przerwanie instalacji sterownika w trakcie może zostawić system "
            "w stanie pośrednim."
        )
        result = install(spec, path, log=log, replace_last=replace_last, cancel_event=cancel)
        if result.cancelled:
            return DriverInstallResult(
                False,
                "Przerwano instalację sterownika. Uruchom ponownie komputer i w razie problemów "
                "zainstaluj sterownik jeszcze raz.",
                cancelled=True,
                version=release.version,
            )
        if result.timed_out:
            raise DriverError(
                f"Instalator nie zakończył pracy w ciągu {int(spec.install_timeout // 60)} min "
                "i został przerwany. Uruchom ponownie komputer i spróbuj jeszcze raz.",
                vendor_url=spec.vendor_url,
            )
        log(f"Instalator zakończył pracę ({_code_text(result.returncode)}).")
        exit_info = spec.describe_exit(result.returncode)
        if not exit_info.ok:
            raise DriverError(
                f"Instalacja sterownika {spec.name} {release.version} nie powiodła się: "
                f"{exit_info.message} ({_code_text(result.returncode)})",
                vendor_url=spec.vendor_url,
            )
        return DriverInstallResult(
            True,
            f"Zainstalowano sterownik {spec.name} {release.version}. {exit_info.message}",
            reboot_required=exit_info.reboot_required,
            version=release.version,
            returncode=result.returncode,
        )
    finally:
        if lock is not None:
            lock.close()
        if path is not None:
            try:
                remove_file(path)
            except OSError as exc:
                log(
                    f"Ostrzeżenie: nie udało się usunąć pobranego pliku; pozostał w {path} "
                    f"({_short(exc)})."
                )
        if work_dir is None:
            with contextlib.suppress(OSError):
                folder.rmdir()  # only when empty


# --------------------------------------------------------------------- dry run


@dataclass(frozen=True, slots=True)
class _Sample:
    version: str
    date: str
    size: int
    url: str


#: Fixed example data for the dry-run simulation (no network); see runner.py.
DRY_RUN_SAMPLES: Mapping[str, _Sample] = {
    "nvidia": _Sample(
        "621.24",
        "2026-11-12",
        1_038_168_736,
        "https://us.download.nvidia.com/Windows/621.24/621.24-desktop-win10-win11-64bit-international-dch-whql.exe",
    ),
    "amd": _Sample(
        "26.11.2",
        "2026-11-18",
        1_759_847_960,
        "https://drivers.amd.com/drivers/whql-amd-software-adrenalin-edition-26.11.2-win11-c.exe",
    ),
    "intel": _Sample(
        "32.0.101.9175",
        "2026-11-04",
        904_495_280,
        "https://downloadmirror.intel.com/932449/gfx_win_101.9175.exe",
    ),
}


def dry_run_release(vendor_id: str | None) -> DriverRelease:
    """Sample release for the simulation (any vendor id, even unknown, gives a value)."""
    sample = DRY_RUN_SAMPLES.get(vendor_id or "", DRY_RUN_SAMPLES["nvidia"])
    return DriverRelease(
        vendor=vendor_id or "nvidia",
        version=sample.version,
        url=sample.url,
        size=sample.size,
        release_date=sample.date,
        source="symulacja (dry-run)",
    )


__all__ = [
    "VENDORS",
    "DriverCancelled",
    "DriverError",
    "DriverInstallResult",
    "DriverRelease",
    "DriverVendor",
    "DownloadedFile",
    "ExitCodeInfo",
    "FileLock",
    "SIGNER_NAMES",
    "ProcessResult",
    "ProgressReporter",
    "SignatureInfo",
    "download",
    "download_dir",
    "download_verified",
    "lock_file",
    "install",
    "install_latest",
    "parse_amd_drivers_page",
    "parse_amd_product_page",
    "parse_amd_release_notes",
    "parse_intel_dsa",
    "parse_intel_page",
    "parse_nvidia_lookup",
    "parse_nvidia_process_find",
    "resolve",
    "sha256_file",
    "subject_matches",
    "url_has_version",
    "verify_signature",
]
