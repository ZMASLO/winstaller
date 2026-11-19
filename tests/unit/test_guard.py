"""The autouse system guard itself: real system access must be blocked."""

from __future__ import annotations

import contextlib
import ctypes
import os
import socket
import subprocess
import sys
import winreg

import pytest
import requests

from tests.conftest import Guard, SystemAccessBlocked


def _expect_blocked(guard: Guard, func, *args, **kwargs) -> None:
    with pytest.raises(SystemAccessBlocked):
        func(*args, **kwargs)
    assert guard.violations, "violation must be recorded"
    guard.violations.clear()


def test_popen_of_system_programs_is_blocked(system_guard: Guard) -> None:
    for argv in (["winget", "--version"], ["powershell.exe", "-Command", "1"], ["shutdown"]):
        _expect_blocked(system_guard, subprocess.Popen, argv)
    _expect_blocked(system_guard, subprocess.run, ["powercfg", "/batteryreport"])
    _expect_blocked(system_guard, subprocess.run, "winget --version")
    _expect_blocked(system_guard, subprocess.run, [sys.executable, "-c", "0"], shell=True)


def test_blocked_error_is_not_swallowed_by_except_exception(system_guard: Guard) -> None:
    def careless() -> None:
        with contextlib.suppress(Exception):  # what sloppy core code might do
            subprocess.run(["winget"])

    _expect_blocked(system_guard, careless)


def test_python_child_and_missing_absolute_path_are_allowed(tmp_path) -> None:
    out = subprocess.run([sys.executable, "-c", "print(42)"], capture_output=True, check=True)
    assert out.stdout.strip() == b"42"
    with pytest.raises(FileNotFoundError):
        subprocess.run([str(tmp_path / "missing.exe")])


def test_os_level_process_starts_are_blocked(system_guard: Guard) -> None:
    _expect_blocked(system_guard, os.system, "echo hi")
    _expect_blocked(system_guard, os.startfile, "notepad.exe")


def test_registry_writes_are_blocked(system_guard: Guard) -> None:
    _expect_blocked(system_guard, winreg.CreateKeyEx, winreg.HKEY_CURRENT_USER, "Software\\x")
    _expect_blocked(system_guard, winreg.SetValueEx, None, "x", 0, winreg.REG_DWORD, 1)
    _expect_blocked(system_guard, winreg.DeleteValue, None, "x")


def test_network_is_blocked(system_guard: Guard) -> None:
    _expect_blocked(system_guard, requests.get, "https://api.github.com/", timeout=1)
    _expect_blocked(system_guard, socket.create_connection, ("93.184.216.34", 80), 1)


def test_ctypes_uac_and_broadcast_are_blocked(system_guard: Guard) -> None:
    _expect_blocked(
        system_guard, ctypes.windll.shell32.ShellExecuteW, None, "runas", "x", "", "", 1
    )
    _expect_blocked(system_guard, ctypes.windll.user32.SendMessageTimeoutW, 0xFFFF, 0x1A)


def test_actions_desktop_dir_is_blocked(system_guard: Guard) -> None:
    from winstaller.core import actions

    _expect_blocked(system_guard, actions.desktop_dir)


def test_winstaller_environment_is_cleared() -> None:
    for name in ("WINSTALLER_ARCH", "WINSTALLER_APP_DIR", "WINSTALLER_DRY_RUN_FAIL"):
        assert name not in os.environ
