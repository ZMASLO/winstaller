"""Platform helpers: architecture, elevation, application/resource/desktop paths.

Windows only. Functions degrade gracefully (fallbacks) instead of raising when a
Win32 API is unavailable, so the module stays importable in tests.
"""

from __future__ import annotations

import ctypes
import os
import platform as _stdlib_platform
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

Arch = Literal["x64", "arm64"]
ALL_ARCHES: tuple[Arch, ...] = ("x64", "arm64")

#: Environment overrides, mainly for manual UI testing and tests.
ENV_ARCH = "WINSTALLER_ARCH"
ENV_APP_DIR = "WINSTALLER_APP_DIR"

_IMAGE_FILE_MACHINE_AMD64 = 0x8664
_IMAGE_FILE_MACHINE_ARM64 = 0xAA64

_FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # .../winstaller
_REPO_ROOT = _PACKAGE_ROOT.parent


def _normalize_arch(value: str) -> Arch | None:
    v = value.strip().lower()
    if v in ("arm64", "aarch64", "armv8", "arm64ec"):
        return "arm64"
    if v in ("x64", "amd64", "x86_64", "em64t"):
        return "x64"
    return None


def _native_machine_via_wow64() -> Arch | None:
    # IsWow64Process2 reports the *native* machine even for an x64 process emulated
    # on ARM64, where PROCESSOR_ARCHITECTURE and platform.machine() say AMD64.
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = kernel32.IsWow64Process2
    except (OSError, AttributeError):
        return None
    fn.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ushort),
        ctypes.POINTER(ctypes.c_ushort),
    ]
    fn.restype = ctypes.c_int
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    process_machine = ctypes.c_ushort(0)
    native_machine = ctypes.c_ushort(0)
    if not fn(
        kernel32.GetCurrentProcess(), ctypes.byref(process_machine), ctypes.byref(native_machine)
    ):
        return None
    if native_machine.value == _IMAGE_FILE_MACHINE_ARM64:
        return "arm64"
    if native_machine.value == _IMAGE_FILE_MACHINE_AMD64:
        return "x64"
    return None


@lru_cache(maxsize=1)
def _detect_arch() -> Arch:
    detected = _native_machine_via_wow64()
    if detected:
        return detected
    for var in ("PROCESSOR_ARCHITEW6432", "PROCESSOR_ARCHITECTURE"):
        value = os.environ.get(var)
        if value and (arch := _normalize_arch(value)):
            return arch
    return _normalize_arch(_stdlib_platform.machine()) or "x64"


def get_arch() -> Arch:
    """Native OS architecture: ``"x64"`` or ``"arm64"``.

    ``WINSTALLER_ARCH`` (``x64``/``arm64``) overrides detection.
    """
    override = os.environ.get(ENV_ARCH)
    if override and (arch := _normalize_arch(override)):
        return arch
    return _detect_arch()


def is_admin() -> bool:
    """True when the current process runs elevated."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (OSError, AttributeError):
        return False


def is_frozen() -> bool:
    """True when running from a PyInstaller-built executable."""
    return bool(getattr(sys, "frozen", False))


def relaunch_as_admin(args: list[str] | None = None) -> bool:
    """Start an elevated copy of the application via UAC (``runas``).

    ``args`` are the application arguments (default: ``sys.argv[1:]``).
    Frozen: runs ``sys.executable args``; dev: runs ``python -m winstaller args``.
    Returns True when the elevated process was started; the caller should then exit.
    """
    app_args = list(sys.argv[1:] if args is None else args)
    params = app_args if is_frozen() else ["-m", "winstaller", *app_args]
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, subprocess.list2cmdline(params), str(Path.cwd()), 1
        )
    except (OSError, AttributeError):
        return False
    # ShellExecuteW returns a value > 32 on success.
    return int(rc) > 32


def app_dir() -> Path:
    """Directory where local companion files are looked up.

    Frozen: the directory containing the exe. Dev: the repository root.
    ``WINSTALLER_APP_DIR`` overrides both. Files such as ``BenchmarkTools``,
    ``RTSSSetup734.exe`` or ``procyon\\procyon-setup.exe`` are resolved against it.
    """
    override = os.environ.get(ENV_APP_DIR)
    if override:
        return Path(override)
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return _REPO_ROOT


def _known_folder_path(folder_id: str) -> Path | None:
    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    try:
        ole32 = ctypes.windll.ole32
        shell32 = ctypes.windll.shell32
    except (OSError, AttributeError):
        return None
    guid = _GUID()
    if ole32.CLSIDFromString(ctypes.c_wchar_p(folder_id), ctypes.byref(guid)) != 0:
        return None
    out = ctypes.c_wchar_p()
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    hr = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out))
    try:
        if hr != 0 or not out.value:
            return None
        return Path(out.value)
    finally:
        ole32.CoTaskMemFree(out)


def desktop_dir() -> Path:
    """The user's real Desktop folder (handles OneDrive/redirected desktops)."""
    path = _known_folder_path(_FOLDERID_DESKTOP)
    if path is not None:
        return path
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"


def resource_path(relative: str | os.PathLike[str]) -> Path:
    """Path of a resource bundled inside the ``winstaller`` package.

    ``relative`` is relative to the package root, e.g. ``"core/catalog.toml"``.
    Frozen builds must ship such files under ``winstaller/...`` in ``_MEIPASS``.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    base = Path(meipass) / "winstaller" if is_frozen() and meipass else _PACKAGE_ROOT
    return base / relative
