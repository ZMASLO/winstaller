"""System actions with the system layer replaced by fakes (nothing real runs)."""

from __future__ import annotations

import base64
import contextlib
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support import make_ctx, make_item
from winstaller.core import actions, winget
from winstaller.core.actions import (
    ACTIONS,
    BATTERY_REPORT_NAME,
    BLOATWARE,
    THEME_REG_PATH,
    THEME_VALUES,
    ActionContext,
    ActionError,
    ActionOutcome,
    build_remove_bloat_script,
    normalized_bloat_patterns,
)
from winstaller.core.catalog import Item, ItemKind
from winstaller.core.winget import ProcessOutcome, WingetResult


@pytest.fixture
def desktop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "Desktop"
    path.mkdir()
    monkeypatch.setattr(actions, "desktop_dir", lambda: path)
    return path


@pytest.fixture
def app_dir(tmp_path: Path) -> Path:
    path = tmp_path / "app"
    path.mkdir()
    return path


def completed(
    code: int = 0, stdout: bytes = b"", stderr: bytes = b""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout, stderr)


class Recorder:
    def __init__(self, result=None) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result(*args, **kwargs) if callable(self.result) else self.result


# ------------------------------------------------------------------- theme


class TestTheme:
    @pytest.fixture
    def registry(self, monkeypatch: pytest.MonkeyPatch) -> tuple[Recorder, Recorder]:
        writes, broadcasts = Recorder(), Recorder()
        monkeypatch.setattr(actions, "set_user_dword", writes)
        monkeypatch.setattr(actions, "broadcast_setting_change", broadcasts)
        return writes, broadcasts

    @pytest.mark.parametrize(
        ("func", "value", "message"),
        [
            (actions.theme_dark, 0, "Ustawiono ciemny motyw."),
            (actions.theme_light, 1, "Ustawiono jasny motyw."),
        ],
    )
    def test_sets_both_values(self, registry, app_dir, func, value, message) -> None:
        writes, broadcasts = registry
        ctx, _ = make_ctx(app_dir)
        assert func(ctx) == message
        assert [args for args, _ in writes.calls] == [
            (THEME_REG_PATH, "AppsUseLightTheme", value),
            (THEME_REG_PATH, "SystemUsesLightTheme", value),
        ]
        assert [args for args, _ in broadcasts.calls] == [("ImmersiveColorSet",)]

    def test_registry_error_becomes_action_error(self, registry, app_dir) -> None:
        writes, broadcasts = registry
        writes.result = PermissionError(5, "Odmowa dostępu")
        with pytest.raises(ActionError, match="Nie udało się zmienić motywu w rejestrze"):
            actions.theme_light(make_ctx(app_dir)[0])
        assert broadcasts.calls == []

    def test_theme_values_constant(self) -> None:
        assert THEME_VALUES == ("AppsUseLightTheme", "SystemUsesLightTheme")

    def test_set_user_dword_uses_create_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import winreg

        calls: list[tuple] = []

        @contextlib.contextmanager
        def create_key_ex(root, path, reserved, access):
            calls.append(("CreateKeyEx", root, path, reserved, access))
            yield "KEY"

        monkeypatch.setattr(winreg, "CreateKeyEx", create_key_ex)
        monkeypatch.setattr(winreg, "SetValueEx", lambda *a: calls.append(("SetValueEx", *a)))
        actions.set_user_dword("Software\\X", "Name", 1)
        assert calls == [
            ("CreateKeyEx", winreg.HKEY_CURRENT_USER, "Software\\X", 0, winreg.KEY_SET_VALUE),
            ("SetValueEx", "KEY", "Name", 0, winreg.REG_DWORD, 1),
        ]

    def test_broadcast_setting_change_arguments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[tuple] = []
        fake_user32 = SimpleNamespace(SendMessageTimeoutW=lambda *a: seen.append(a) or 1)
        fake_ctypes = SimpleNamespace(
            windll=SimpleNamespace(user32=fake_user32),
            c_size_t=actions.ctypes.c_size_t,
            c_wchar_p=actions.ctypes.c_wchar_p,
            byref=actions.ctypes.byref,
        )
        monkeypatch.setattr(actions, "ctypes", fake_ctypes)
        actions.broadcast_setting_change("ImmersiveColorSet")
        (args,) = seen
        assert args[:3] == (0xFFFF, 0x001A, 0)
        assert args[3].value == "ImmersiveColorSet"
        assert args[4:6] == (0x0002, 1000)

    def test_broadcast_failure_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(actions, "ctypes", SimpleNamespace())  # no windll at all
        actions.broadcast_setting_change("ImmersiveColorSet")


# ------------------------------------------------------------------- bloat


class TestBloatPatterns:
    def test_real_list_normalization(self) -> None:
        patterns = normalized_bloat_patterns()
        assert len(BLOATWARE) == 69 and len(patterns) == 66
        assert set(BLOATWARE) - set(patterns) == {
            "Microsoft.Office.Sway",
            "AdobeSystemsIncorporated.AdobePhotoshopExpress",
            "HiddenCityMysteryofShadows",
        }

    def test_invariants(self) -> None:
        patterns = normalized_bloat_patterns()
        lowered = [p.lower() for p in patterns]
        assert len(set(lowered)) == len(lowered)
        assert not any("*" in p for p in patterns)
        for a in lowered:
            assert not any(b != a and b in a for b in lowered)
        # Nothing the original list matched is lost: every original *p* is covered.
        for original in BLOATWARE:
            assert any(p in original.lower() for p in lowered), original

    def test_wildcards_duplicates_and_order(self) -> None:
        raw = ["*Foo*", "foo", "FooBar", " Bar ", "**", "", "*Baz", "Qux*", "barX"]
        assert normalized_bloat_patterns(raw) == ["Foo", "Bar", "Baz", "Qux"]

    def test_empty(self) -> None:
        assert normalized_bloat_patterns([]) == []


class TestBloatScript:
    def test_quotes_are_escaped(self) -> None:
        script = build_remove_bloat_script(["O'Brien", "Plain"])
        assert "$patterns = @('O''Brien','Plain')" in script

    def test_no_double_wildcards_and_utf8(self) -> None:
        script = build_remove_bloat_script(normalized_bloat_patterns())
        assert "**" not in script
        assert script.startswith("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8")
        assert script.count("Get-AppxPackage") == 1
        assert script.count("Remove-AppxPackage") == 1
        assert '-Name "*$p*"' in script

    def test_every_pattern_once(self) -> None:
        patterns = normalized_bloat_patterns()
        line = next(
            ln
            for ln in build_remove_bloat_script(patterns).splitlines()
            if ln.startswith("$patterns")
        )
        for p in patterns:
            assert line.count("'" + p.replace("'", "''") + "'") == 1

    def test_output_protocol_lines(self) -> None:
        script = build_remove_bloat_script(["x"])
        for tag in ('"NONE`t$p"', '"OK`t$p`t$($pkg.Name)"', '"ERR`t$p`t$($pkg.Name)`t$m"'):
            assert tag in script

    def test_powershell_dollar_and_backtick_are_inert_in_single_quotes(self) -> None:
        script = build_remove_bloat_script(["a$b`c"])
        assert "@('a$b`c')" in script


class TestRemoveBloat:
    @pytest.fixture
    def stream(self, monkeypatch: pytest.MonkeyPatch):
        state = SimpleNamespace(lines=[], outcome=ProcessOutcome(0, False), calls=[])

        def fake(ctx, args, *, on_line=None):
            state.calls.append(list(args))
            for line in state.lines:
                (on_line or ctx.log)(line)
            if isinstance(state.outcome, BaseException):
                raise state.outcome
            return state.outcome

        monkeypatch.setattr(actions, "stream_command", fake)
        return state

    def test_single_encoded_powershell_call(self, stream, app_dir) -> None:
        actions.remove_bloat(make_ctx(app_dir)[0])
        assert len(stream.calls) == 1
        args = stream.calls[0]
        assert args[:6] == [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
        ]
        assert len(args) == 7
        decoded = base64.b64decode(args[6]).decode("utf-16-le")
        assert decoded == build_remove_bloat_script(normalized_bloat_patterns())

    def test_parses_result_lines(self, stream, app_dir) -> None:
        stream.lines = [
            "OK\tCandyCrush\tking.com.CandyCrushSaga",
            "OK\tCandyCrush\tking.com.CandyCrushFriends",
            "ERR\tXbox\tMicrosoft.XboxApp\tOdmowa dostępu",
            "ERR\tX\tPkgWithoutReason",
            "NONE\tHulu",
            "NONE\tNetflix",
            "NONE\tDisney",
            "unexpected text",
            "OK\tshort",
        ]
        ctx, rec = make_ctx(app_dir)
        outcome = actions.remove_bloat(ctx)
        assert outcome == ActionOutcome(
            True, "Usunięto 2 pakietów, nie znaleziono 3 wzorców, błędy: 2 (szczegóły w dzienniku)."
        )
        assert rec.lines == [
            "Usuwanie zbędnych aplikacji (66 wzorców)...",
            "Usunięto: king.com.CandyCrushSaga",
            "Usunięto: king.com.CandyCrushFriends",
            "Błąd usuwania Microsoft.XboxApp: Odmowa dostępu",
            "Błąd usuwania PkgWithoutReason",
            "Nie znaleziono: Hulu",
            "Nie znaleziono: Netflix",
            "Nie znaleziono: Disney",
            "unexpected text",
            "OK\tshort",
        ]

    def test_no_errors_summary(self, stream, app_dir) -> None:
        stream.lines = ["NONE\tA"]
        assert actions.remove_bloat(make_ctx(app_dir)[0]).message == (
            "Usunięto 0 pakietów, nie znaleziono 1 wzorców."
        )

    def test_nonzero_exit_code_is_error(self, stream, app_dir) -> None:
        stream.outcome = ProcessOutcome(1, False)
        with pytest.raises(ActionError, match=r"PowerShell zakończył się błędem \(kod 1\)"):
            actions.remove_bloat(make_ctx(app_dir)[0])

    def test_cancelled(self, stream, app_dir) -> None:
        stream.outcome = ProcessOutcome(-1, True)
        outcome = actions.remove_bloat(make_ctx(app_dir)[0])
        assert outcome.cancelled and not outcome.ok

    def test_powershell_missing(self, stream, app_dir) -> None:
        stream.outcome = FileNotFoundError(2, "powershell.exe")
        with pytest.raises(ActionError, match="Nie udało się uruchomić PowerShell"):
            actions.remove_bloat(make_ctx(app_dir)[0])


# ----------------------------------------------------------------- copying


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


class TestCopyDirToDesktop:
    def _ctx(self, app_dir: Path, *args: str) -> ActionContext:
        item = make_item("copy", kind=ItemKind.ACTION, action="copy_dir_to_desktop", args=args)
        return make_ctx(app_dir, item)[0]

    def test_copies_tree(self, desktop, app_dir) -> None:
        src = app_dir / "BenchmarkTools"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_bytes(b"A")
        (src / "sub" / "b.bin").write_bytes(b"\x00\x01")
        assert actions.copy_dir_to_desktop(self._ctx(app_dir, "BenchmarkTools")) == (
            "Skopiowano folder BenchmarkTools na pulpit."
        )
        assert _tree(desktop / "BenchmarkTools") == _tree(src)

    def test_merges_into_existing_target(self, desktop, app_dir) -> None:
        src = app_dir / "BenchmarkTools"
        src.mkdir()
        (src / "a.txt").write_bytes(b"new")
        target = desktop / "BenchmarkTools"
        target.mkdir()
        (target / "a.txt").write_bytes(b"old")
        (target / "mine.txt").write_bytes(b"keep")
        actions.copy_dir_to_desktop(self._ctx(app_dir, "BenchmarkTools"))
        assert _tree(target) == {"a.txt": b"new", "mine.txt": b"keep"}

    def test_missing_args(self, desktop, app_dir) -> None:
        with pytest.raises(ActionError, match="Brak nazwy folderu"):
            actions.copy_dir_to_desktop(self._ctx(app_dir))

    @pytest.mark.parametrize("name", ["..", r"..\Windows", r"C:\Windows", r"\x", "D:x", "a/../.."])
    def test_folder_outside_app_dir_rejected(self, desktop, app_dir, name: str) -> None:
        with pytest.raises(ActionError, match="Nieprawidłowa nazwa folderu"):
            actions.copy_dir_to_desktop(self._ctx(app_dir, name))
        assert list(desktop.iterdir()) == []

    @pytest.mark.parametrize("make_file", [False, True])
    def test_missing_source(self, desktop, app_dir, make_file: bool) -> None:
        if make_file:
            (app_dir / "BenchmarkToolsARM").write_bytes(b"not a dir")
        with pytest.raises(ActionError, match="Folder BenchmarkToolsARM nie istnieje"):
            actions.copy_dir_to_desktop(self._ctx(app_dir, "BenchmarkToolsARM"))
        assert not (desktop / "BenchmarkToolsARM").is_dir()

    def test_copy_error_names_the_real_folder(self, desktop, app_dir, monkeypatch) -> None:
        (app_dir / "BenchmarkToolsARM").mkdir()

        def broken(*args, **kwargs):
            raise shutil.Error([("a", "b", "disk full")])

        monkeypatch.setattr(shutil, "copytree", broken)
        with pytest.raises(ActionError, match="Nie udało się skopiować folderu BenchmarkToolsARM"):
            actions.copy_dir_to_desktop(self._ctx(app_dir, "BenchmarkToolsARM"))


class TestCopySelfToDesktop:
    def test_not_frozen(self, desktop, app_dir, monkeypatch) -> None:
        monkeypatch.setattr(actions, "is_frozen", lambda: False)
        with pytest.raises(ActionError, match="tylko w zbudowanej wersji"):
            actions.copy_self_to_desktop(make_ctx(app_dir)[0])
        assert list(desktop.iterdir()) == []

    def test_copies_exe(self, desktop, app_dir, monkeypatch) -> None:
        exe = app_dir / "winstaller.exe"
        exe.write_bytes(b"MZ-new")
        monkeypatch.setattr(actions, "is_frozen", lambda: True)
        monkeypatch.setattr(actions, "current_executable", lambda: exe)
        (desktop / "winstaller.exe").write_bytes(b"MZ-old")
        assert (
            actions.copy_self_to_desktop(make_ctx(app_dir)[0])
            == "Skopiowano winstaller.exe na pulpit."
        )
        assert (desktop / "winstaller.exe").read_bytes() == b"MZ-new"

    def test_already_on_desktop(self, desktop, app_dir, monkeypatch) -> None:
        exe = desktop / "winstaller.exe"
        exe.write_bytes(b"MZ")
        monkeypatch.setattr(actions, "is_frozen", lambda: True)
        monkeypatch.setattr(actions, "current_executable", lambda: exe)
        monkeypatch.setattr(shutil, "copy2", Recorder(AssertionError("must not copy")))
        assert actions.copy_self_to_desktop(make_ctx(app_dir)[0]) == (
            "Program już jest uruchomiony z pulpitu."
        )

    def test_copy_error(self, desktop, app_dir, monkeypatch) -> None:
        exe = app_dir / "winstaller.exe"
        exe.write_bytes(b"MZ")
        monkeypatch.setattr(actions, "is_frozen", lambda: True)
        monkeypatch.setattr(actions, "current_executable", lambda: exe)
        monkeypatch.setattr(shutil, "copy2", Recorder(PermissionError(13, "zajęty")))
        with pytest.raises(ActionError, match="Nie udało się skopiować winstaller.exe"):
            actions.copy_self_to_desktop(make_ctx(app_dir)[0])

    def test_system_layer_defaults(self, monkeypatch) -> None:
        import sys

        assert actions.current_executable() == Path(sys.executable)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert actions.is_frozen()


# ---------------------------------------------------------------- restarts


class TestRestarts:
    @pytest.mark.parametrize(
        ("func", "args", "message"),
        [
            (actions.reboot_to_bios, ["/r", "/fw", "/t", "1"], "Zlecono restart do BIOS/UEFI."),
            (
                actions.reboot_advanced,
                ["/r", "/o", "/t", "1"],
                "Zlecono restart do opcji zaawansowanych.",
            ),
        ],
    )
    def test_command_and_message(self, monkeypatch, app_dir, func, args, message) -> None:
        run = Recorder(completed(0))
        monkeypatch.setattr(actions, "run_command", run)
        assert func(make_ctx(app_dir)[0]) == message
        assert run.calls == [((["shutdown.exe", *args],), {"timeout": 30})]

    def test_nonzero_code_with_detail(self, monkeypatch, app_dir) -> None:
        stderr = "Brak obsługi oprogramowania układowego.(1)".encode()
        monkeypatch.setattr(actions, "run_command", Recorder(completed(1, b"", stderr)))
        with pytest.raises(ActionError) as info:
            actions.reboot_to_bios(make_ctx(app_dir)[0])
        assert str(info.value) == (
            "Nie udało się zlecić restartu do BIOS/UEFI: Brak obsługi oprogramowania układowego.(1)"
        )

    def test_nonzero_code_falls_back_to_stdout_then_nothing(self, monkeypatch, app_dir) -> None:
        monkeypatch.setattr(actions, "run_command", Recorder(completed(5, b"  from stdout \r\n")))
        with pytest.raises(ActionError, match="opcji zaawansowanych: from stdout$"):
            actions.reboot_advanced(make_ctx(app_dir)[0])
        monkeypatch.setattr(actions, "run_command", Recorder(completed(5)))
        with pytest.raises(ActionError) as info:
            actions.reboot_advanced(make_ctx(app_dir)[0])
        assert str(info.value) == "Nie udało się zlecić restartu do opcji zaawansowanych"

    @pytest.mark.parametrize(
        "exc", [FileNotFoundError(2, "shutdown.exe"), subprocess.TimeoutExpired("shutdown", 30)]
    )
    def test_cannot_run(self, monkeypatch, app_dir, exc) -> None:
        monkeypatch.setattr(actions, "run_command", Recorder(exc))
        with pytest.raises(ActionError, match="Nie udało się uruchomić polecenia shutdown"):
            actions.reboot_to_bios(make_ctx(app_dir)[0])


class TestSystemLayer:
    def test_run_command(self, monkeypatch) -> None:
        run = Recorder(completed(0, b"x"))
        monkeypatch.setattr(subprocess, "run", run)
        assert actions.run_command(("a", "b"), timeout=7).stdout == b"x"
        assert run.calls == [
            (
                (["a", "b"],),
                {"capture_output": True, "timeout": 7, "creationflags": actions.CREATE_NO_WINDOW},
            )
        ]

    def test_stream_command_defaults_to_context(self, monkeypatch, app_dir) -> None:
        calls: list[dict] = []

        def fake(args, **kwargs):
            calls.append({"args": args, **kwargs})
            return ProcessOutcome(0, False)

        monkeypatch.setattr(winget, "stream_process", fake)
        ctx, _ = make_ctx(app_dir)
        actions.stream_command(ctx, ["x.exe"])
        assert calls[0]["on_line"] == ctx.log
        assert calls[0]["on_replace_last"] == ctx.replace_last
        assert calls[0]["cancel_event"] is ctx.cancel_event
        custom = calls.append
        actions.stream_command(ctx, ["y.exe"], on_line=custom)
        assert calls[1]["on_line"] is custom

    def test_start_detached(self, monkeypatch) -> None:
        popen = Recorder()
        monkeypatch.setattr(subprocess, "Popen", popen)
        actions.start_detached(("C:\\x.exe", "/arg"))
        assert popen.calls == [
            ((["C:\\x.exe", "/arg"],), {"close_fds": True, "creationflags": 0x208})
        ]

    def test_desktop_dir_delegates_to_platform(self, monkeypatch, tmp_path, system_guard) -> None:
        from winstaller.core import platform as plat

        monkeypatch.setattr(plat, "desktop_dir", lambda: tmp_path)
        assert system_guard.originals["actions.desktop_dir"]() == tmp_path


# ----------------------------------------------------------------- battery


class TestBatteryReport:
    def test_writes_to_desktop(self, monkeypatch, desktop, app_dir) -> None:
        def powercfg(args, *, timeout):
            Path(args[3]).write_text("<html>", encoding="utf-8")
            return completed(0)

        run = Recorder(powercfg)
        monkeypatch.setattr(actions, "run_command", run)
        outcome = actions.battery_report(make_ctx(app_dir)[0])
        target = desktop / BATTERY_REPORT_NAME
        assert outcome == ActionOutcome(True, f"Zapisano raport baterii: {target}", artifact=target)
        assert run.calls == [
            ((["powercfg", "/batteryreport", "/output", str(target)],), {"timeout": 60})
        ]

    @pytest.mark.parametrize("desktop_code", [1, 0])
    def test_falls_back_to_app_dir(self, monkeypatch, desktop, app_dir, desktop_code) -> None:
        def powercfg(args, *, timeout):
            target = Path(args[3])
            if target.parent == desktop:
                return completed(desktop_code)  # code 0 but no file also counts as failure
            target.write_text("<html>", encoding="utf-8")
            return completed(0)

        monkeypatch.setattr(actions, "run_command", Recorder(powercfg))
        outcome = actions.battery_report(make_ctx(app_dir)[0])
        assert outcome.artifact == app_dir / BATTERY_REPORT_NAME
        assert outcome.ok

    def test_both_locations_fail(self, monkeypatch, desktop, app_dir) -> None:
        message = "Nie można wykonać operacji: brak baterii.".encode()
        monkeypatch.setattr(actions, "run_command", Recorder(completed(1, message)))
        with pytest.raises(ActionError) as info:
            actions.battery_report(make_ctx(app_dir)[0])
        assert str(info.value) == (
            "Nie udało się wygenerować raportu baterii. Nie można wykonać operacji: brak baterii."
        )

    def test_powercfg_missing(self, monkeypatch, desktop, app_dir) -> None:
        monkeypatch.setattr(actions, "run_command", Recorder(FileNotFoundError(2, "powercfg")))
        with pytest.raises(ActionError, match="Nie udało się uruchomić powercfg"):
            actions.battery_report(make_ctx(app_dir)[0])


# -------------------------------------------------------- local installers


class TestLocalInstaller:
    @pytest.fixture
    def stream(self, monkeypatch):
        state = SimpleNamespace(outcome=ProcessOutcome(0, False), calls=[])

        def fake(ctx, args, *, on_line=None):
            state.calls.append(list(args))
            if isinstance(state.outcome, BaseException):
                raise state.outcome
            return state.outcome

        monkeypatch.setattr(actions, "stream_command", fake)
        return state

    def test_runs_last_glob_match_with_args(self, stream, app_dir) -> None:
        for name in ("Setup_1.0.exe", "Setup_2.0.exe"):
            (app_dir / name).write_bytes(b"MZ")
        ctx, rec = make_ctx(app_dir)
        outcome = actions.run_local_installer(ctx, "Setup_*.exe", ("/i", "/q"))
        assert outcome == ActionOutcome(True, "Zainstalowano z Setup_2.0.exe.")
        assert stream.calls == [[str(app_dir / "Setup_2.0.exe"), "/i", "/q"]]
        assert rec.lines == ["Uruchamianie instalatora Setup_2.0.exe..."]

    def test_glob_picks_highest_version_numerically(self, stream, app_dir) -> None:
        for name in ("Setup_19.9.exe", "Setup_19.10.exe", "Setup_9.99.exe"):
            (app_dir / name).write_bytes(b"MZ")
        outcome = actions.run_local_installer(make_ctx(app_dir)[0], "Setup_*.exe", ())
        assert outcome.message == "Zainstalowano z Setup_19.10.exe."

    def test_subdirectory_path(self, stream, app_dir) -> None:
        (app_dir / "procyon").mkdir()
        (app_dir / "procyon" / "procyon-setup.exe").write_bytes(b"MZ")
        actions.run_local_installer(make_ctx(app_dir)[0], "procyon\\procyon-setup.exe", ["/silent"])
        assert stream.calls[0] == [str(app_dir / "procyon" / "procyon-setup.exe"), "/silent"]

    def test_missing_file(self, stream, app_dir) -> None:
        with pytest.raises(ActionError, match=r"Brak pliku instalatora RTSSSetup734.exe w "):
            actions.run_local_installer(make_ctx(app_dir)[0], "RTSSSetup734.exe", ["/S"])
        assert stream.calls == []

    def test_nonzero_exit(self, stream, app_dir) -> None:
        (app_dir / "x.exe").write_bytes(b"MZ")
        stream.outcome = ProcessOutcome(1603, False)
        with pytest.raises(ActionError, match="Instalator x.exe zakończył się kodem 1603"):
            actions.run_local_installer(make_ctx(app_dir)[0], "x.exe", [])

    def test_cancelled(self, stream, app_dir) -> None:
        (app_dir / "x.exe").write_bytes(b"MZ")
        stream.outcome = ProcessOutcome(-1, True)
        outcome = actions.run_local_installer(make_ctx(app_dir)[0], "x.exe", [])
        assert outcome == ActionOutcome(False, "Przerwano przez użytkownika.", cancelled=True)

    def test_cannot_start(self, stream, app_dir) -> None:
        (app_dir / "x.exe").write_bytes(b"not really an exe")
        stream.outcome = OSError(193, "%1 nie jest prawidłową aplikacją")
        with pytest.raises(ActionError, match="Nie udało się uruchomić x.exe"):
            actions.run_local_installer(make_ctx(app_dir)[0], "x.exe", [])

    def test_real_stream_with_missing_target_raises_action_error(
        self, monkeypatch, app_dir
    ) -> None:
        # Resolution finds the file, but it vanishes before start: real stream_process.
        (app_dir / "gone.exe").write_bytes(b"")
        real_resolve = actions.resolve_relative

        def resolve_then_delete(base, rel):
            path = real_resolve(base, rel)
            path.unlink()
            return path

        monkeypatch.setattr(actions, "resolve_relative", resolve_then_delete)
        with pytest.raises(ActionError, match="Nie udało się uruchomić gone.exe"):
            actions.run_local_installer(make_ctx(app_dir)[0], "gone.exe", [])


class TestLaunchAfterInstall:
    def test_missing_file_only_warns(self, monkeypatch, app_dir) -> None:
        start = Recorder()
        monkeypatch.setattr(actions, "start_detached", start)
        ctx, rec = make_ctx(app_dir)
        actions.launch_after_install(ctx, str(app_dir / "Steam.exe"))
        assert start.calls == []
        assert rec.lines == [
            f"Ostrzeżenie: nie znaleziono {app_dir / 'Steam.exe'}, pomijam uruchomienie."
        ]

    def test_directory_is_not_launched(self, monkeypatch, app_dir) -> None:
        start = Recorder()
        monkeypatch.setattr(actions, "start_detached", start)
        actions.launch_after_install(make_ctx(app_dir)[0], str(app_dir))
        assert start.calls == []

    def test_expands_environment_and_starts(self, monkeypatch, app_dir) -> None:
        (app_dir / "Steam").mkdir()
        exe = app_dir / "Steam" / "Steam.exe"
        exe.write_bytes(b"MZ")
        monkeypatch.setenv("WINSTALLER_TEST_PF", str(app_dir))
        start = Recorder()
        monkeypatch.setattr(actions, "start_detached", start)
        ctx, rec = make_ctx(app_dir)
        actions.launch_after_install(ctx, r"%WINSTALLER_TEST_PF%\Steam\Steam.exe")
        assert start.calls == [(([str(exe)],), {})]
        assert rec.lines == ["Uruchomiono Steam.exe."]

    def test_start_failure_only_warns(self, monkeypatch, app_dir) -> None:
        exe = app_dir / "a.exe"
        exe.write_bytes(b"MZ")
        monkeypatch.setattr(actions, "start_detached", Recorder(OSError(5, "odmowa")))
        ctx, rec = make_ctx(app_dir)
        actions.launch_after_install(ctx, str(exe))
        assert rec.lines[0].startswith("Ostrzeżenie: nie udało się uruchomić a.exe")


# ------------------------------------------------------------------ execute


class TestExecute:
    @pytest.fixture
    def launches(self, monkeypatch) -> Recorder:
        rec = Recorder()
        monkeypatch.setattr(actions, "launch_after_install", rec)
        return rec

    def _winget(self, monkeypatch, name: str, result: WingetResult) -> list[tuple]:
        calls: list[tuple] = []

        def fake(package_id, *, on_line=None, on_replace_last=None, cancel_event=None):
            calls.append((package_id, on_line, on_replace_last, cancel_event))
            return result

        monkeypatch.setattr(winget, name, fake)
        return calls

    def test_winget_success_with_post_launch(self, monkeypatch, app_dir, launches) -> None:
        calls = self._winget(monkeypatch, "install", WingetResult(0, True, False, "Zainstalowano."))
        item = make_item(
            "steam", name="Steam", winget_id="Valve.Steam", post_launch="%X%\\Steam.exe"
        )
        ctx, rec = make_ctx(app_dir, item)
        assert actions.execute(item, ctx) == ActionOutcome(True, "Zainstalowano.")
        assert calls == [("Valve.Steam", ctx.log, ctx.replace_last, ctx.cancel_event)]
        assert rec.lines == ["Instalowanie Steam (Valve.Steam)..."]
        assert launches.calls == [((ctx, "%X%\\Steam.exe"), {})]

    @pytest.mark.parametrize(
        "result",
        [
            WingetResult(1, False, False, "Nieznany błąd"),
            WingetResult(-1, False, True, "Przerwano przez użytkownika."),
        ],
    )
    def test_winget_failure_does_not_launch(self, monkeypatch, app_dir, launches, result) -> None:
        self._winget(monkeypatch, "install", result)
        item = make_item("steam", post_launch="x.exe")
        outcome = actions.execute(item, make_ctx(app_dir, item)[0])
        assert outcome == ActionOutcome(False, result.message, cancelled=result.cancelled)
        assert launches.calls == []

    def test_winget_uninstall(self, monkeypatch, app_dir, launches) -> None:
        calls = self._winget(
            monkeypatch, "uninstall", WingetResult(0, True, False, "Odinstalowano.")
        )
        monkeypatch.setattr(winget, "install", Recorder(AssertionError("install called")))
        item = make_item(
            "od",
            name="OneDrive",
            kind=ItemKind.WINGET_UNINSTALL,
            winget_id="Microsoft.OneDrive",
            post_launch="x.exe",
        )
        ctx, rec = make_ctx(app_dir, item)
        assert actions.execute(item, ctx).ok
        assert calls[0][0] == "Microsoft.OneDrive"
        assert rec.lines == ["Odinstalowywanie OneDrive (Microsoft.OneDrive)..."]
        assert launches.calls == []  # post_launch makes no sense after uninstall

    def test_local_exe_success_launches(self, monkeypatch, app_dir, launches) -> None:
        run = Recorder(ActionOutcome(True, "Zainstalowano z x.exe."))
        monkeypatch.setattr(actions, "run_local_installer", run)
        item = make_item(
            "l", kind=ItemKind.LOCAL_EXE, path="x.exe", args=("/S",), post_launch="y.exe"
        )
        ctx, _ = make_ctx(app_dir, item)
        assert actions.execute(item, ctx).ok
        assert run.calls == [((ctx, "x.exe", ("/S",)), {})]
        assert launches.calls == [((ctx, "y.exe"), {})]

    def test_local_exe_failure_propagates_and_does_not_launch(
        self, monkeypatch, app_dir, launches
    ) -> None:
        monkeypatch.setattr(actions, "run_local_installer", Recorder(ActionError("kod 1")))
        item = make_item("l", kind=ItemKind.LOCAL_EXE, post_launch="y.exe")
        with pytest.raises(ActionError):
            actions.execute(item, make_ctx(app_dir, item)[0])
        assert launches.calls == []

    def test_local_exe_cancel_does_not_launch(self, monkeypatch, app_dir, launches) -> None:
        cancelled = ActionOutcome(False, "Przerwano przez użytkownika.", cancelled=True)
        monkeypatch.setattr(actions, "run_local_installer", Recorder(cancelled))
        item = make_item("l", kind=ItemKind.LOCAL_EXE, post_launch="y.exe")
        assert actions.execute(item, make_ctx(app_dir, item)[0]) == cancelled
        assert launches.calls == []

    @pytest.mark.parametrize(
        ("returned", "expected"),
        [
            ("Zrobione.", ActionOutcome(True, "Zrobione.")),
            (None, ActionOutcome(True, "Gotowe.")),
            ("", ActionOutcome(True, "Gotowe.")),
            (
                ActionOutcome(False, "nie", cancelled=True),
                ActionOutcome(False, "nie", cancelled=True),
            ),
        ],
    )
    def test_action_return_values(self, monkeypatch, app_dir, returned, expected) -> None:
        seen: list[ActionContext] = []
        monkeypatch.setitem(ACTIONS, "test_action", lambda ctx: seen.append(ctx) or returned)
        item = make_item("a", kind=ItemKind.ACTION, action="test_action", args=("x",))
        ctx, _ = make_ctx(app_dir, item)
        assert actions.execute(item, ctx) == expected
        assert seen == [ctx] and ctx.args == ("x",)

    def test_action_error_propagates(self, monkeypatch, app_dir) -> None:
        def failing(ctx):
            raise ActionError("Nie da się.")

        monkeypatch.setitem(ACTIONS, "test_action", failing)
        item = make_item("a", kind=ItemKind.ACTION, action="test_action")
        with pytest.raises(ActionError, match="Nie da się."):
            actions.execute(item, make_ctx(app_dir, item)[0])

    def test_unknown_action(self, app_dir) -> None:
        item = make_item("a", kind=ItemKind.ACTION, action="no_such_action")
        with pytest.raises(ActionError, match="Nieznana akcja: no_such_action"):
            actions.execute(item, make_ctx(app_dir, item)[0])

    def test_unsupported_kind(self, app_dir) -> None:
        item = Item(id="w", name="W", category="c", kind="weird")  # type: ignore[arg-type]
        with pytest.raises(ActionError, match="Nieobsługiwany rodzaj pozycji"):
            actions.execute(item, make_ctx(app_dir, item)[0])

    def test_registry_contents(self) -> None:
        assert set(ACTIONS) == {
            "remove_bloat",
            "theme_dark",
            "theme_light",
            "copy_dir_to_desktop",
            "copy_self_to_desktop",
            "reboot_to_bios",
            "reboot_advanced",
            "battery_report",
        }

    def test_context_defaults(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("WINSTALLER_APP_DIR", str(tmp_path))
        monkeypatch.setenv("WINSTALLER_ARCH", "arm64")
        ctx = ActionContext(item=make_item("a", args=("q",)), log=print, replace_last=print)
        assert ctx.app_dir == tmp_path and ctx.arch == "arm64"
        assert isinstance(ctx.cancel_event, threading.Event) and not ctx.cancel_event.is_set()
        assert ctx.args == ("q",)
