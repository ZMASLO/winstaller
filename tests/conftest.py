"""Shared fixtures and the system-access guard.

Every test runs under :func:`system_guard` (autouse). It makes any accidental
contact with the real system fail the test:

* ``subprocess.Popen`` (and therefore ``run``/``check_output``) of anything other
  than this Python interpreter (``sys.executable -c ...`` stays allowed for tests
  that deliberately run a harmless child process) or an absolute path that does
  not exist (lets "program not found" paths be tested; ``CreateProcess`` fails);
  ``shell=True`` and string command lines are always blocked;
* ``os.system`` / ``os.startfile`` / ``os.popen``;
* registry writes through ``winreg``;
* DNS lookups and socket connections to non-loopback hosts (covers ``requests``);
* ``ShellExecute*`` (UAC), window broadcasts and shutdown APIs through ``ctypes``;
* ``winstaller.core.actions.desktop_dir`` (tests must point it at ``tmp_path``).

The guard raises :class:`SystemAccessBlocked` (a ``BaseException``, so core code
catching ``Exception``/``OSError`` cannot swallow it) and also records the
violation, failing the test in teardown even if something did swallow it.

Marker ``live`` (GPU driver vendor checks over the real network) is skipped unless
``WINSTALLER_LIVE_TESTS=1``; only then, and only for such a test, DNS lookups of the
vendor hosts in :data:`LIVE_HOSTS` and connections to the addresses they resolved to
are let through. Everything else stays blocked.
"""

from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import winreg
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

# Qt tests (tests/ui, the in-process perf tests) always run offscreen, before any
# QApplication exists. The offscreen platform has no font database of its own on
# Windows: point it at the system fonts (as tools/screenshots.py does).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "QT_QPA_FONTDIR", os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "Fonts")
)

_WINSTALLER_ENV = (
    "WINSTALLER_ARCH",
    "WINSTALLER_APP_DIR",
    "WINSTALLER_DRY_RUN_FAIL",
    "WINSTALLER_DRY_RUN_DELAY",
    "WINSTALLER_FAKE_UPDATE",
)

_REGISTRY_WRITERS = (
    "CreateKey",
    "CreateKeyEx",
    "DeleteKey",
    "DeleteKeyEx",
    "DeleteValue",
    "SetValue",
    "SetValueEx",
    "SaveKey",
    "LoadKey",
)

_CTYPES_BLOCKED = {
    "shell32": ("ShellExecuteW", "ShellExecuteA", "ShellExecuteExW"),
    "user32": (
        "SendMessageTimeoutW",
        "SendMessageW",
        "PostMessageW",
        "BroadcastSystemMessageW",
        "ExitWindowsEx",
    ),
    "advapi32": ("InitiateSystemShutdownExW", "InitiateShutdownW"),
}

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "0:0:0:0:0:0:0:1"}

LIVE_ENV = "WINSTALLER_LIVE_TESTS"
#: Vendor hosts a ``live`` test may reach (metadata and download servers).
LIVE_HOSTS = frozenset(
    {
        "gfwsl.geforce.com",
        "www.nvidia.com",
        "us.download.nvidia.com",
        "international.download.nvidia.com",
        "www.amd.com",
        "drivers.amd.com",
        "dsadata.intel.com",
        "www.intel.com",
        "downloadmirror.intel.com",
    }
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", f"live: real requests to GPU vendor sites (only with {LIVE_ENV}=1)"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(LIVE_ENV) == "1":
        return
    skip = pytest.mark.skip(reason=f"live test: set {LIVE_ENV}=1 to run")
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(skip)


class SystemAccessBlocked(BaseException):
    """A unit/perf test tried to touch the real system."""


@dataclass
class Guard:
    violations: list[str] = field(default_factory=list)
    #: Unpatched callables replaced by the guard (for tests of the wrappers themselves).
    originals: dict[str, object] = field(default_factory=dict)

    def block(self, what: str) -> SystemAccessBlocked:
        self.violations.append(what)
        return SystemAccessBlocked(f"test tried to touch the real system: {what}")


def _python_executables() -> set[str]:
    names = {sys.executable, getattr(sys, "_base_executable", sys.executable)}
    out = set()
    for name in names:
        if name:
            out.add(os.path.normcase(os.path.abspath(name)))
            out.add(os.path.normcase(os.path.realpath(name)))
    return out


_PYTHONS = _python_executables()


def _popen_allowed(args: object, kwargs: dict[str, object]) -> tuple[bool, str]:
    if kwargs.get("shell"):
        return False, f"Popen(shell=True): {args!r}"
    executable = kwargs.get("executable")
    if executable is None:
        if isinstance(args, (str, bytes, os.PathLike)):
            return False, f"Popen with a command-line string: {args!r}"
        seq = list(args)  # type: ignore[call-overload]
        if not seq:
            return False, "Popen([])"
        executable = seq[0]
    exe = os.fspath(executable)  # type: ignore[arg-type]
    if isinstance(exe, bytes):
        exe = exe.decode(errors="replace")
    normalized = os.path.normcase(os.path.abspath(exe))
    if os.path.isabs(exe) and normalized in _PYTHONS:
        return True, ""
    if os.path.isabs(exe) and not os.path.exists(exe):
        return True, ""  # CreateProcess fails with FileNotFoundError; nothing runs
    return False, f"Popen({args!r})"


@pytest.fixture(autouse=True)
def system_guard(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[Guard]:
    guard = Guard()
    live = os.environ.get(LIVE_ENV) == "1" and request.node.get_closest_marker("live") is not None
    live_addresses: set[str] = set()

    for name in _WINSTALLER_ENV:
        monkeypatch.delenv(name, raising=False)

    real_popen = subprocess.Popen

    class GuardedPopen(real_popen):  # type: ignore[valid-type, misc]
        def __init__(self, args, *a, **kw):
            ok, what = _popen_allowed(args, kw)
            if not ok:
                raise guard.block(what)
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)

    def blocked(what: str):
        def _raise(*args: object, **kwargs: object) -> None:
            raise guard.block(f"{what}{args!r}")

        return _raise

    for fn in ("system", "startfile", "popen"):
        if hasattr(os, fn):
            monkeypatch.setattr(os, fn, blocked(f"os.{fn}"))

    for fn in _REGISTRY_WRITERS:
        monkeypatch.setattr(winreg, fn, blocked(f"winreg.{fn}"))

    for dll_name, functions in _CTYPES_BLOCKED.items():
        dll = getattr(ctypes.windll, dll_name)
        for fn in functions:
            monkeypatch.setattr(dll, fn, blocked(f"{dll_name}.{fn}"), raising=False)

    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _host_of(address: object) -> str:
        if isinstance(address, tuple) and address:
            return str(address[0])
        return str(address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        name = str(host).lower().rstrip(".") if host is not None else None
        if live and name in LIVE_HOSTS:
            result = real_getaddrinfo(host, *args, **kwargs)
            live_addresses.update(str(entry[4][0]) for entry in result)
            return result
        if host is not None and str(host).lower() not in _LOOPBACK:
            raise guard.block(f"DNS lookup of {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def _connect_allowed(address: object) -> bool:
        host = _host_of(address).lower()
        return host in _LOOPBACK or (live and host in live_addresses)

    def guarded_connect(self, address):
        if not _connect_allowed(address):
            raise guard.block(f"socket connect to {address!r}")
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        if not _connect_allowed(address):
            raise guard.block(f"socket connect to {address!r}")
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)

    from winstaller.core import actions

    guard.originals["actions.desktop_dir"] = actions.desktop_dir
    monkeypatch.setattr(actions, "desktop_dir", blocked("actions.desktop_dir (use tmp_path)"))

    yield guard

    if guard.violations:
        pytest.fail(
            "System access attempted during the test:\n  " + "\n  ".join(guard.violations),
            pytrace=False,
        )


@pytest.fixture(scope="session")
def qapp() -> Iterator[object]:
    """pytest-qt's application, created exactly as ``winstaller.ui.app`` does (Fusion,
    fonts). Imported lazily: tests that do not use Qt never load PySide6."""
    from winstaller.ui.app import create_application

    yield create_application(["winstaller-tests"])
