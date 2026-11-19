"""Self-update from GitHub Releases (no UI).

Compatible with 1.x: tags are ``vX.Y.Z`` and assets are named
``winstaller-x64.exe`` / ``winstaller-arm64.exe``. The swap is done in Python:
the running exe is renamed to ``<exe>.old`` (Windows allows renaming a running
image), the new file is moved into place, the new process is started and the
caller exits. ``cleanup_old`` removes the leftover on next start.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from winstaller.core import platform as plat
from winstaller.core.platform import Arch
from winstaller.core.version import get_version

GITHUB_REPO = "ZMASLO/winstaller"
API_LATEST_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
USER_AGENT = "winstaller-updater"
CHECK_TIMEOUT: tuple[float, float] = (5.0, 10.0)
DOWNLOAD_TIMEOUT: tuple[float, float] = (10.0, 30.0)
_CHUNK = 64 * 1024

ProgressCallback = Callable[[int, int | None], None]  # (downloaded bytes, total or None)


class UpdateError(Exception):
    """Update failed; ``str(exc)`` is a user-facing Polish message."""


class UpdateCancelled(UpdateError):
    """Download cancelled by the user."""


# ----------------------------------------------------------------- versions

_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def parse_version(text: str) -> tuple[int, int, int, int, str]:
    """Sortable key for ``X.Y.Z[-pre]`` (optional ``v`` prefix).

    A pre-release sorts below its release (``2.0.0-dev < 2.0.0``).
    Raises ``ValueError`` for anything else.
    """
    m = _SEMVER.match(text.strip())
    if not m:
        raise ValueError(f"Nieprawidłowy numer wersji: {text!r}")
    major, minor, patch, pre = m.groups()
    return int(major), int(minor), int(patch), 0 if pre else 1, pre or ""


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


# ------------------------------------------------------------------- models


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    name: str
    url: str
    size: int
    sha256: str | None = None  # lowercase hex, from the API "digest" field


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    tag: str
    version: str
    name: str
    notes: str
    html_url: str
    assets: tuple[ReleaseAsset, ...]

    def asset_named(self, name: str) -> ReleaseAsset | None:
        return next((a for a in self.assets if a.name.lower() == name.lower()), None)


@dataclass(frozen=True, slots=True)
class UpdateCheck:
    current: str
    latest: str
    update_available: bool
    #: Asset for this architecture; None when the release has no matching file.
    asset: ReleaseAsset | None
    release: ReleaseInfo


def asset_name_for(arch: Arch) -> str:
    return f"winstaller-{arch}.exe"


def _parse_digest(value: Any) -> str | None:
    if isinstance(value, str) and value.lower().startswith("sha256:"):
        hexpart = value.split(":", 1)[1].strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", hexpart):
            return hexpart
    return None


def parse_release(data: dict[str, Any]) -> ReleaseInfo:
    """Build :class:`ReleaseInfo` from a GitHub ``releases/latest`` JSON object."""
    tag = data.get("tag_name")
    if not isinstance(tag, str):
        raise UpdateError("Odpowiedź GitHub nie zawiera numeru wersji.")
    try:
        parse_version(tag)
    except ValueError:
        raise UpdateError(f"Nieprawidłowy tag wydania: {tag}") from None
    assets = tuple(
        ReleaseAsset(
            name=str(a.get("name", "")),
            url=str(a.get("browser_download_url", "")),
            size=int(a.get("size") or 0),
            sha256=_parse_digest(a.get("digest")),
        )
        for a in data.get("assets") or []
        if isinstance(a, dict) and a.get("browser_download_url")
    )
    return ReleaseInfo(
        tag=tag,
        version=tag.lstrip("vV"),
        name=str(data.get("name") or tag),
        notes=str(data.get("body") or ""),
        html_url=str(data.get("html_url") or ""),
        assets=assets,
    )


def _session(session: Any | None) -> Any:
    if session is not None:
        return session
    import requests

    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


# -------------------------------------------------------------------- check


def check(
    session: Any | None = None,
    *,
    current: str | None = None,
    arch: Arch | None = None,
    url: str = API_LATEST_URL,
) -> UpdateCheck:
    """Query the latest release. Never reports a downgrade as an update.

    ``session`` is anything with a ``requests.Session``-like ``get``.
    Raises :class:`UpdateError` on network/HTTP/format problems.
    """
    import requests

    cur = current or get_version()
    sess = _session(session)
    try:
        response = sess.get(
            url, timeout=CHECK_TIMEOUT, headers={"Accept": "application/vnd.github+json"}
        )
    except requests.RequestException as exc:
        raise UpdateError(f"Nie udało się połączyć z GitHub: {exc}") from exc
    if response.status_code == 404:
        raise UpdateError("Nie znaleziono żadnego opublikowanego wydania.")
    if response.status_code == 403:
        raise UpdateError("GitHub odrzucił zapytanie (limit zapytań). Spróbuj później.")
    if response.status_code != 200:
        raise UpdateError(f"GitHub zwrócił błąd HTTP {response.status_code}.")
    try:
        data = response.json()
    except ValueError as exc:
        raise UpdateError("Nieprawidłowa odpowiedź GitHub.") from exc
    if not isinstance(data, dict):
        raise UpdateError("Nieprawidłowa odpowiedź GitHub.")
    release = parse_release(data)
    try:
        available = is_newer(release.version, cur)
    except ValueError as exc:
        raise UpdateError(str(exc)) from exc
    asset = release.asset_named(asset_name_for(arch or plat.get_arch()))
    return UpdateCheck(cur, release.version, available, asset, release)


# ----------------------------------------------------------------- download


def download(
    asset: ReleaseAsset,
    dest: str | os.PathLike[str],
    progress_cb: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    *,
    session: Any | None = None,
) -> Path:
    """Stream ``asset`` to ``dest``; verify size and SHA256 (when known).

    Data goes to ``<dest>.part`` first and is renamed only after verification;
    the partial file is removed on any error or cancellation.
    """
    import requests

    target = Path(dest)
    part = target.with_name(target.name + ".part")
    sess = _session(session)
    digest = hashlib.sha256()
    received = 0
    try:
        try:
            response = sess.get(asset.url, stream=True, timeout=DOWNLOAD_TIMEOUT)
        except requests.RequestException as exc:
            raise UpdateError(f"Nie udało się pobrać aktualizacji: {exc}") from exc
        try:
            if response.status_code != 200:
                raise UpdateError(f"Pobieranie nie powiodło się (HTTP {response.status_code}).")
            header_total = int(response.headers.get("content-length") or 0)
            total = asset.size or header_total or None
            try:
                with part.open("wb") as fh:
                    for chunk in response.iter_content(chunk_size=_CHUNK):
                        if cancel_event is not None and cancel_event.is_set():
                            raise UpdateCancelled("Pobieranie aktualizacji przerwane.")
                        if not chunk:
                            continue
                        fh.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                        if progress_cb is not None:
                            progress_cb(received, total)
            except requests.RequestException as exc:
                raise UpdateError(f"Przerwano pobieranie aktualizacji: {exc}") from exc
        finally:
            close = getattr(response, "close", None)
            if close is not None:
                close()
        if asset.size and received != asset.size:
            raise UpdateError(
                f"Pobrany plik ma nieprawidłowy rozmiar ({received} z {asset.size} bajtów)."
            )
        if asset.sha256 and digest.hexdigest() != asset.sha256:
            raise UpdateError("Suma kontrolna SHA256 pobranego pliku się nie zgadza.")
        os.replace(part, target)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return target


# -------------------------------------------------------------------- apply


def old_path_for(exe: Path) -> Path:
    return exe.with_name(exe.name + ".old")


def _spawn(exe: Path, args: list[str]) -> None:
    env = dict(os.environ)
    # PyInstaller >= 6.9: start the new onefile exe as an independent instance
    # instead of letting it reuse this process's unpacked _MEI directory.
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(
        [str(exe), *args],
        env=env,
        close_fds=True,
        creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    )


def apply(
    new_exe: str | os.PathLike[str],
    *,
    current_exe: str | os.PathLike[str] | None = None,
    args: list[str] | None = None,
    launch: bool = True,
) -> bool:
    """Replace the running exe with ``new_exe`` and start it.

    Returns True when the caller must quit now. On failure the original exe is
    restored and :class:`UpdateError` is raised. Without ``current_exe`` this
    works only in a frozen build.
    """
    if current_exe is None:
        if not plat.is_frozen():
            raise UpdateError("Aktualizacja działa tylko w zbudowanej wersji programu (.exe).")
        import sys

        current_exe = sys.executable
    current = Path(current_exe)
    new = Path(new_exe)
    old = old_path_for(current)
    if not new.is_file():
        raise UpdateError(f"Brak pobranego pliku aktualizacji: {new}")
    try:
        old.unlink(missing_ok=True)
    except OSError as exc:
        raise UpdateError(f"Nie można usunąć poprzedniej kopii {old.name}: {exc}") from exc
    try:
        os.replace(current, old)
    except OSError as exc:
        raise UpdateError(f"Nie można zmienić nazwy działającego programu: {exc}") from exc
    try:
        os.replace(new, current)
    except OSError as exc:
        os.replace(old, current)
        raise UpdateError(f"Nie można podmienić pliku programu: {exc}") from exc
    if launch:
        try:
            _spawn(current, args or [])
        except OSError as exc:
            os.replace(current, new)
            os.replace(old, current)
            raise UpdateError(f"Nie udało się uruchomić nowej wersji: {exc}") from exc
    return True


def cleanup_old(
    exe: str | os.PathLike[str] | None = None, *, wait: float = 0.0, interval: float = 0.25
) -> bool:
    """Delete ``<exe>.old`` left by a previous update.

    The previous process may still be exiting; ``wait`` > 0 retries for that
    many seconds (call it off the GUI thread then). Returns True when no
    leftover remains. Does nothing in dev runs unless ``exe`` is given.
    """
    if exe is None:
        if not plat.is_frozen():
            return True
        import sys

        exe = sys.executable
    old = old_path_for(Path(exe))
    deadline = time.monotonic() + wait
    while True:
        try:
            old.unlink(missing_ok=True)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(interval)
