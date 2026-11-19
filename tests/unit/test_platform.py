"""Platform helpers with Win32 calls replaced where they would have side effects."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from winstaller.core import platform as plat


@pytest.fixture
def fresh_arch_cache():
    plat._detect_arch.cache_clear()
    yield
    plat._detect_arch.cache_clear()


# -------------------------------------------------------------------- arch


class TestArch:
    @pytest.mark.parametrize(
        ("value", "arch"),
        [
            ("arm64", "arm64"),
            (" ARM64 ", "arm64"),
            ("aarch64", "arm64"),
            ("ARMv8", "arm64"),
            ("arm64ec", "arm64"),
            ("x64", "x64"),
            ("AMD64", "x64"),
            ("x86_64", "x64"),
            ("EM64T", "x64"),
        ],
    )
    def test_override_is_normalized(self, monkeypatch, value: str, arch: str) -> None:
        monkeypatch.setenv(plat.ENV_ARCH, value)
        assert plat.get_arch() == arch

    @pytest.mark.parametrize("value", ["x86", "sparc", "", "  "])
    def test_invalid_override_falls_back_to_detection(
        self, monkeypatch, fresh_arch_cache, value
    ) -> None:
        monkeypatch.setenv(plat.ENV_ARCH, value)
        monkeypatch.setattr(plat, "_native_machine_via_wow64", lambda: "arm64")
        assert plat.get_arch() == "arm64"

    def test_native_detection_on_this_machine(self, fresh_arch_cache) -> None:
        assert plat._native_machine_via_wow64() in ("x64", "arm64")
        assert plat.get_arch() in plat.ALL_ARCHES

    def test_detection_is_cached(self, monkeypatch, fresh_arch_cache) -> None:
        calls: list[int] = []
        monkeypatch.setattr(plat, "_native_machine_via_wow64", lambda: calls.append(1) or "x64")
        assert plat.get_arch() == plat.get_arch() == "x64"
        assert calls == [1]

    @pytest.mark.parametrize(
        ("env", "machine", "arch"),
        [
            (
                {"PROCESSOR_ARCHITEW6432": "ARM64", "PROCESSOR_ARCHITECTURE": "AMD64"},
                "AMD64",
                "arm64",
            ),
            ({"PROCESSOR_ARCHITECTURE": "ARM64"}, "AMD64", "arm64"),
            ({"PROCESSOR_ARCHITECTURE": "AMD64"}, "ARM64", "x64"),
            ({"PROCESSOR_ARCHITECTURE": "x86"}, "ARM64", "arm64"),
            ({}, "aarch64", "arm64"),
            ({}, "AMD64", "x64"),
            ({}, "riscv", "x64"),
            ({}, "", "x64"),
        ],
    )
    def test_fallbacks_without_iswow64process2(
        self, monkeypatch, fresh_arch_cache, env, machine, arch
    ) -> None:
        monkeypatch.setattr(plat, "_native_machine_via_wow64", lambda: None)
        for var in ("PROCESSOR_ARCHITEW6432", "PROCESSOR_ARCHITECTURE"):
            monkeypatch.delenv(var, raising=False)
        for var, value in env.items():
            monkeypatch.setenv(var, value)
        monkeypatch.setattr(plat._stdlib_platform, "machine", lambda: machine)
        assert plat._detect_arch() == arch

    def _fake_kernel32(self, native: int, ok: bool = True):
        def iswow64process2(process, process_machine, native_machine):
            native_machine._obj.value = native
            return 1 if ok else 0

        return SimpleNamespace(
            IsWow64Process2=_Callable(iswow64process2),
            GetCurrentProcess=_Callable(lambda: 0),
        )

    @pytest.mark.parametrize(
        ("native", "ok", "expected"),
        [
            (0xAA64, True, "arm64"),
            (0x8664, True, "x64"),
            (0x014C, True, None),
            (0xAA64, False, None),
        ],
    )
    def test_iswow64process2_mapping(self, monkeypatch, native, ok, expected) -> None:
        kernel32 = self._fake_kernel32(native, ok)
        monkeypatch.setattr(plat.ctypes, "WinDLL", lambda *a, **k: kernel32)
        assert plat._native_machine_via_wow64() == expected

    def test_iswow64process2_missing(self, monkeypatch) -> None:
        def no_dll(*args, **kwargs):
            raise OSError("no kernel32")

        monkeypatch.setattr(plat.ctypes, "WinDLL", no_dll)
        assert plat._native_machine_via_wow64() is None
        monkeypatch.setattr(plat.ctypes, "WinDLL", lambda *a, **k: SimpleNamespace())
        assert plat._native_machine_via_wow64() is None


class _Callable:
    """Function stand-in that accepts ctypes ``argtypes``/``restype`` assignments."""

    def __init__(self, fn) -> None:
        self._fn = fn
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._fn(*args)


# ---------------------------------------------------------- admin / frozen


class TestAdminAndFrozen:
    def test_is_admin_true_false(self, monkeypatch) -> None:
        for value, expected in ((1, True), (0, False)):
            fake = SimpleNamespace(
                windll=SimpleNamespace(shell32=SimpleNamespace(IsUserAnAdmin=lambda v=value: v))
            )
            monkeypatch.setattr(plat, "ctypes", fake)
            assert plat.is_admin() is expected

    def test_is_admin_without_api(self, monkeypatch) -> None:
        monkeypatch.setattr(plat, "ctypes", SimpleNamespace())
        assert plat.is_admin() is False

    def test_is_admin_real_call_returns_bool(self) -> None:
        assert isinstance(plat.is_admin(), bool)

    def test_is_frozen(self, monkeypatch) -> None:
        monkeypatch.delattr(sys, "frozen", raising=False)
        assert plat.is_frozen() is False
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert plat.is_frozen() is True


class TestRelaunchAsAdmin:
    @pytest.fixture
    def shell(self, monkeypatch):
        calls: list[tuple] = []
        state = SimpleNamespace(rc=42, calls=calls, exc=None)

        def shell_execute(*args):
            calls.append(args)
            if state.exc:
                raise state.exc
            return state.rc

        fake = SimpleNamespace(
            windll=SimpleNamespace(shell32=SimpleNamespace(ShellExecuteW=shell_execute))
        )
        monkeypatch.setattr(plat, "ctypes", fake)
        return state

    def test_dev_mode_runs_module(self, shell, monkeypatch, tmp_path) -> None:
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.chdir(tmp_path)
        assert plat.relaunch_as_admin(["--dry-run", "C:\\Program Files\\x"]) is True
        assert shell.calls == [
            (
                None,
                "runas",
                sys.executable,
                '-m winstaller --dry-run "C:\\Program Files\\x"',
                str(tmp_path),
                1,
            )
        ]

    def test_frozen_runs_exe_with_args(self, shell, monkeypatch, tmp_path) -> None:
        exe = str(tmp_path / "winstaller.exe")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", exe)
        assert plat.relaunch_as_admin(["--no-elevate"]) is True
        (call,) = shell.calls
        assert call[2] == exe and call[3] == "--no-elevate" and call[1] == "runas"

    def test_default_args_from_argv(self, shell, monkeypatch) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "argv", ["winstaller.exe", "--dry-run", "a b"])
        plat.relaunch_as_admin()
        assert shell.calls[0][3] == subprocess.list2cmdline(["--dry-run", "a b"])

    def test_empty_args(self, shell, monkeypatch) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        plat.relaunch_as_admin([])
        assert shell.calls[0][3] == ""

    @pytest.mark.parametrize(("rc", "expected"), [(33, True), (32, False), (5, False), (0, False)])
    def test_return_code(self, shell, rc, expected) -> None:
        shell.rc = rc
        assert plat.relaunch_as_admin([]) is expected

    def test_api_failure(self, shell) -> None:
        shell.exc = OSError("denied")
        assert plat.relaunch_as_admin([]) is False


# ------------------------------------------------------------------- paths


class TestPaths:
    def test_app_dir_env_override(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv(plat.ENV_APP_DIR, str(tmp_path))
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert plat.app_dir() == tmp_path

    def test_app_dir_frozen(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(tmp_path / "dist" / "winstaller.exe"))
        assert plat.app_dir() == (tmp_path / "dist").resolve()

    def test_app_dir_dev_is_repo_root(self, monkeypatch) -> None:
        monkeypatch.delattr(sys, "frozen", raising=False)
        root = plat.app_dir()
        assert (root / "pyproject.toml").is_file()
        assert (root / "winstaller" / "core" / "catalog.toml").is_file()

    def test_resource_path_dev(self, monkeypatch) -> None:
        monkeypatch.delattr(sys, "frozen", raising=False)
        path = plat.resource_path("core/catalog.toml")
        assert path.is_file() and path.parent.name == "core"
        assert plat.resource_path(Path("core") / "catalog.toml") == path

    def test_resource_path_frozen(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert (
            plat.resource_path("core/catalog.toml")
            == tmp_path / "winstaller" / "core" / "catalog.toml"
        )

    def test_resource_path_meipass_ignored_when_not_frozen(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delattr(sys, "frozen", raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
        assert tmp_path not in plat.resource_path("x").parents

    def test_resource_path_frozen_without_meipass(self, monkeypatch) -> None:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.delattr(sys, "_MEIPASS", raising=False)
        assert plat.resource_path("core/catalog.toml").is_file()

    def test_desktop_dir_real_lookup_is_read_only(self) -> None:
        desktop = plat.desktop_dir()
        assert isinstance(desktop, Path) and desktop.is_absolute()

    def test_known_folder_invalid_guid(self) -> None:
        assert plat._known_folder_path("not-a-guid") is None

    def test_desktop_dir_fallback_to_userprofile(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(plat, "_known_folder_path", lambda folder_id: None)
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        assert plat.desktop_dir() == tmp_path / "Desktop"

    def test_desktop_dir_fallback_to_home(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(plat, "_known_folder_path", lambda folder_id: None)
        monkeypatch.delenv("USERPROFILE", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert plat.desktop_dir() == tmp_path / "Desktop"

    def test_known_folder_without_windll(self, monkeypatch) -> None:
        monkeypatch.setattr(
            plat,
            "ctypes",
            SimpleNamespace(
                Structure=plat.ctypes.Structure,
                c_ulong=plat.ctypes.c_ulong,
                c_ushort=plat.ctypes.c_ushort,
                c_ubyte=plat.ctypes.c_ubyte,
            ),
        )
        assert plat._known_folder_path(plat._FOLDERID_DESKTOP) is None
