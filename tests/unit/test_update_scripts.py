"""Static checks of the PowerShell scripts in ``update_scripts`` and of how they are
handed to ``powershell.exe`` (``powershell_args``, ``compact_script``).

Nothing here runs PowerShell: the scripts are read as text (a small tokenizer for the
quoting rules that matter) and the command lines are only built and decoded.
"""

from __future__ import annotations

import inspect
import re
import subprocess
from pathlib import Path

import pytest

from tests.updates_support import decode_script
from winstaller.core import platform as plat
from winstaller.core import update_scripts, updates
from winstaller.core.update_scripts import (
    ALL_SCRIPTS,
    STORE_SCRIPT,
    WU_CRITERIA,
    WU_FALLBACK_CRITERIA,
    WU_INSTALL_SCRIPT,
    WU_SCAN_SCRIPT,
)
from winstaller.core.updates import UpdatesError, compact_script, powershell_args

PS_PATH = str(plat.windows_system_dir() / plat.POWERSHELL_EXE)

# ------------------------------------------------------------- tiny tokenizer

_SINGLE = "'\u2018\u2019\u201a\u201b"
_DOUBLE = '"\u201c\u201d\u201e'


def tokenize(script: str) -> tuple[list[str], list[str], str]:
    """``(string literals, comments, code without strings/comments)`` by PowerShell's
    quoting rules for '...' / "..." (backtick escapes, doubled quotes, typographic
    quotes), ``#`` line comments, ``<# #>`` blocks and here-strings."""
    strings: list[str] = []
    comments: list[str] = []
    code: list[str] = []
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        if script.startswith(("@'\n", '@"\n', "@'\r\n", '@"\r\n'), i):
            end = script.index("\n" + script[i + 1] + "@", i)
            strings.append(script[i:end])
            i = end + 3
            code.append(" S ")
        elif script.startswith("<#", i):
            end = script.index("#>", i) + 2
            comments.append(script[i:end])
            i = end
        elif ch == "#":
            end = script.find("\n", i)
            end = n if end < 0 else end
            comments.append(script[i:end])
            i = end
        elif ch in _SINGLE or ch in _DOUBLE:
            quotes = _SINGLE if ch in _SINGLE else _DOUBLE
            j, value = i + 1, []
            while True:
                c = script[j]
                if quotes is _DOUBLE and c == "`":
                    value.append(script[j : j + 2])
                    j += 2
                    continue
                if c in quotes:
                    if j + 1 < n and script[j + 1] in quotes:
                        value.append(c)
                        j += 2
                        continue
                    break
                value.append(c)
                j += 1
            strings.append("".join(value))
            code.append(" S ")
            i = j + 1
        else:
            code.append(ch)
            i += 1
    return strings, comments, "".join(code)


def code_of(script: str) -> str:
    return tokenize(script)[2]


# ------------------------------------------------------------- powershell_args


@pytest.mark.parametrize("name", list(ALL_SCRIPTS))
def test_encoded_command_round_trip(name: str) -> None:
    script = ALL_SCRIPTS[name]
    args = powershell_args(script)
    assert args[0] == PS_PATH and Path(args[0]).is_absolute()
    assert args[1:6] == ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", args[5]]
    assert args[1:-2] == ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
    assert decode_script(args) == compact_script(script)
    assert len(subprocess.list2cmdline(args)) < updates.MAX_COMMAND_LINE


def test_hidden_adds_window_style() -> None:
    args = powershell_args(WU_INSTALL_SCRIPT, hidden=True)
    assert args[:8] == [
        PS_PATH,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-EncodedCommand",
    ]
    assert len(args) == 9
    assert decode_script(args) == compact_script(WU_INSTALL_SCRIPT)


def test_never_bare_powershell_name() -> None:
    for script in ALL_SCRIPTS.values():
        exe = powershell_args(script)[0]
        assert exe.lower().endswith(r"\system32\windowspowershell\v1.0\powershell.exe")
        assert exe.lower() != "powershell.exe" and exe.lower() != "powershell"


def test_missing_powershell_raises_file_not_found(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(plat, "windows_system_dir", lambda: tmp_path)
    with pytest.raises(FileNotFoundError):
        powershell_args(WU_SCAN_SCRIPT)


def test_command_line_limit_boundary(monkeypatch) -> None:
    monkeypatch.setattr(plat, "system32_path", lambda name: r"C:\Windows\System32\ps.exe")

    def length(n: int) -> int:
        args = [
            r"C:\Windows\System32\ps.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            updates.base64.b64encode(("x" * n + "\n").encode("utf-16-le")).decode(),
        ]
        return len(subprocess.list2cmdline(args))

    lo, hi = 1, 40_000
    while lo < hi:  # largest n whose command line is still below the limit
        mid = (lo + hi + 1) // 2
        if length(mid) < updates.MAX_COMMAND_LINE:
            lo = mid
        else:
            hi = mid - 1
    ok = powershell_args("x" * lo)
    assert len(subprocess.list2cmdline(ok)) < updates.MAX_COMMAND_LINE
    assert length(lo + 1) >= updates.MAX_COMMAND_LINE
    with pytest.raises(UpdatesError) as exc:
        powershell_args("x" * (lo + 1))
    assert exc.value.kind == "start"
    with pytest.raises(UpdatesError):
        powershell_args("x" * 100_000)


def test_install_script_has_headroom_under_the_limit() -> None:
    length = len(subprocess.list2cmdline(powershell_args(WU_INSTALL_SCRIPT, hidden=True)))
    assert length < updates.MAX_COMMAND_LINE * 0.85, length


def test_smoke_builds_all_scripts(capsys) -> None:
    from winstaller import __main__ as entry

    assert entry.smoke() == 0
    out = capsys.readouterr().out
    assert re.search(r"update scripts: 3 OK \(max \d+ znaków\)", out)


# ------------------------------------------------------------- compact_script


def test_compact_script_rules() -> None:
    source = "\n  # comment\n\n\t$a = 'x # y'\r\n    if ($a) {  \n  }\n#end"
    assert compact_script(source) == "$a = 'x # y'\nif ($a) {\n}\n"
    assert compact_script(compact_script(source)) == compact_script(source)
    assert compact_script("") == "\n"


def test_compact_script_would_break_here_strings() -> None:
    # Documented limitation: the scripts must never contain here-strings.
    source = "$t = @'\n  # keep me\n  indented\n'@\n"
    assert compact_script(source) != source.replace("  ", "")
    assert "# keep me" not in compact_script(source)


@pytest.mark.parametrize("name", list(ALL_SCRIPTS))
def test_scripts_are_safe_to_compact(name: str) -> None:
    script = ALL_SCRIPTS[name]
    strings, comments, code = tokenize(script)
    assert not any("\n" in s for s in strings), "multi-line string literal"
    assert not any(c.startswith("<#") for c in comments), "block comment"
    assert "@'" not in code and '@"' not in code, "here-string"
    assert not re.search(r"`\s*$", code, re.MULTILINE), "backtick line continuation"
    compacted = compact_script(script)
    assert tokenize(compacted)[0] == strings  # every literal survives unchanged
    assert code_of(compacted).split() == code.split()


@pytest.mark.parametrize("name", list(ALL_SCRIPTS))
def test_no_typographic_quotes(name: str) -> None:
    # PowerShell treats ‘ ’ ‚ ‛ “ ” „ as quote characters: inside a literal they end it.
    assert not set(ALL_SCRIPTS[name]) & set(_SINGLE[1:] + _DOUBLE[1:])


# ------------------------------------------------------------- no injection


def test_scripts_are_constants_without_placeholders() -> None:
    for script in ALL_SCRIPTS.values():
        code = code_of(script)
        assert "{WU_" not in script and "%s" not in script
        assert "Invoke-Expression" not in code and not re.search(r"\biex\b", code, re.I)
    assert set(ALL_SCRIPTS) == {"wu-scan", "wu-install", "store"}
    assert update_scripts.ALL_SCRIPTS["wu-install"] is WU_INSTALL_SCRIPT


@pytest.mark.parametrize("name", list(ALL_SCRIPTS))
def test_parameters_only_from_environment(name: str) -> None:
    code = code_of(ALL_SCRIPTS[name])
    for forbidden in ("$args", "param(", "Read-Host", "$input", "GetEnvironmentVariable"):
        assert forbidden.lower() not in code.lower()
    used = set(re.findall(r"\$env:(\w+)", code))
    expected = {
        "wu-scan": set(),
        "wu-install": {
            updates.ENV_WU_LOG,
            updates.ENV_WU_STATUS,
            updates.ENV_WU_IDS,
            updates.ENV_WU_ALL,
            updates.ENV_WU_SKIP_DRIVERS,
            updates.ENV_WU_SKIP_DISPLAY_DRIVERS,
            updates.ENV_WU_SKIP_INTERACTIVE,
        },
        "store": {updates.ENV_STORE_MODE, updates.ENV_STORE_TIMEOUT},
    }[name]
    assert used == expected


# ------------------------------------------------------------- forbidden operations

FORBIDDEN = [
    r"Restart-Computer",
    r"Stop-Computer",
    r"\bshutdown\b",
    r"UsoClient",
    r"Invoke-Expression",
    r"\biex\b",
    r"Invoke-WebRequest",
    r"Invoke-RestMethod",
    r"\biwr\b",
    r"WebClient",
    r"Start-BitsTransfer",
    r"DownloadString",
    r"DownloadFile",
    r"Start-Process",
    r"\bcmd(\.exe)?\b",
    r"Remove-Item",
    r"Set-ItemProperty",
    r"New-ItemProperty",
    r"Set-ExecutionPolicy",
    r"reg(\.exe)?\s+add",
    r"Commit\(",
    r"RebootRequiredBeforeInstallation\s*=",
]


@pytest.mark.parametrize("name", list(ALL_SCRIPTS))
def test_no_forbidden_operations(name: str) -> None:
    code = code_of(ALL_SCRIPTS[name])
    for pattern in FORBIDDEN:
        assert not re.search(pattern, code, re.IGNORECASE), pattern
    # The call operator would run an external program; scripts use only cmdlets/.NET/COM.
    assert not re.search(r"(^|[\s;(])&\s*[\"'$\w]", code, re.MULTILINE)


def test_only_the_install_script_downloads_or_installs() -> None:
    for name in ("wu-scan", "store"):
        code = code_of(ALL_SCRIPTS[name])
        for word in ("Download(", "Install(", "AcceptEula", "CreateUpdateInstaller"):
            assert word not in code, (name, word)
    assert "SearchForAllUpdatesAsync" not in code_of(WU_SCAN_SCRIPT)


@pytest.mark.parametrize("name", list(ALL_SCRIPTS))
def test_preferences_set_first(name: str) -> None:
    lines = [ln.strip() for ln in compact_script(ALL_SCRIPTS[name]).splitlines()]
    assert lines[0] == "$ErrorActionPreference = 'Stop'"
    assert lines[1] == "$ProgressPreference = 'SilentlyContinue'"


@pytest.mark.parametrize(
    "script,marker", [(WU_SCAN_SCRIPT, "wu-scan"), (STORE_SCRIPT, "store-update")]
)
def test_reporting_scripts_write_one_compact_json_line(script: str, marker: str) -> None:
    assert f"winstaller = '{marker}'" in script
    writes = re.findall(r"\[Console\]::Out\.WriteLine\((.*)\)", script)
    assert len(writes) == 1 and "ConvertTo-Json" in writes[0] and "-Compress" in writes[0]
    depth = int(re.search(r"-Depth (\d+)", writes[0]).group(1))  # type: ignore[union-attr]
    assert depth >= 4
    assert script.index("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false")
    assert script.index("OutputEncoding") < script.index("[Console]::Out.WriteLine")
    assert re.findall(r"\bexit (\S+)", code_of(script)) == ["0", "1"]


# ------------------------------------------------------------- search criteria


def test_criteria_cover_regular_and_optional_updates() -> None:
    terms = [t.strip() for t in WU_CRITERIA.split(" or ")]
    assert terms == [
        "IsInstalled=0 and IsHidden=0 and DeploymentAction='Installation'",
        "IsInstalled=0 and IsHidden=0 and DeploymentAction='OptionalInstallation'",
    ]
    assert WU_FALLBACK_CRITERIA == "IsInstalled=0 and IsHidden=0 and DeploymentAction=*"
    assert "$" not in WU_CRITERIA and '"' not in WU_CRITERIA and "'" not in WU_FALLBACK_CRITERIA


def test_scan_and_install_use_identical_criteria_and_fallback_rule() -> None:
    fallback_rule = "if ($hr -ne '0x80240032' -and $hr -ne '0x80070057') { throw }"
    for script in (WU_SCAN_SCRIPT, WU_INSTALL_SCRIPT):
        assert f'$Criteria = "{WU_CRITERIA}"' in script
        assert f"$FallbackCriteria = '{WU_FALLBACK_CRITERIA}'" in script
        assert script.count(fallback_rule) == 1
        primary = script.index("$searcher.Search($Criteria)")
        fallback = script.index("$searcher.Search($FallbackCriteria)")
        assert primary < script.index(fallback_rule) < fallback
        assert "$searcher.Online = $true" in script


# ------------------------------------------------------------- background install


def _main_body(script: str) -> str:
    return script[script.index("$exitCode = 1") :]


def test_install_script_mutex_lifecycle() -> None:
    body = _main_body(WU_INSTALL_SCRIPT)
    assert f"$MutexName = '{updates.MUTEX_NAME}'" in WU_INSTALL_SCRIPT
    create = body.index("New-Object System.Threading.Mutex($true, $MutexName, [ref]$createdNew)")
    assert create < body.index("Save-Status") < body.index("Invoke-WindowsUpdate")
    assert body.index("$owned = $createdNew") < body.index("if (-not $owned)")
    finally_block = body[body.index("} finally {") :]
    assert "$mutex.ReleaseMutex()" in finally_block and "$mutex.Dispose()" in finally_block
    assert finally_block.index("if ($owned)") < finally_block.index("ReleaseMutex")


def test_install_script_always_finishes_status_and_exits_with_code() -> None:
    body = _main_body(WU_INSTALL_SCRIPT)
    assert body.startswith("$exitCode = 1\n")
    finally_block = body[body.index("} finally {") : body.rindex("exit $exitCode")]
    for statement in (
        "$status.state = 'finished'",
        "$status.finished = (Get-Date).ToString('o')",
        "$status.exitCode = $exitCode",
        "Save-Status",
    ):
        assert statement in finally_block
    catch_block = body[body.index("} catch {") : body.index("} finally {")]
    assert "$exitCode = 1" in catch_block
    assert compact_script(WU_INSTALL_SCRIPT).rstrip().splitlines()[-1] == "exit $exitCode"
    code = code_of(WU_INSTALL_SCRIPT)
    assert re.findall(r"\bexit\b\s*(\S+)", code) == ["7", "$exitCode"]


def test_install_script_exit_codes_match_documentation() -> None:
    source = inspect.getsource(update_scripts)
    doc = source[source.index("#: Exit codes:") : source.index("WU_INSTALL_SCRIPT = (")]
    documented = {int(n) for n in re.findall(r"(\d) [A-Za-z]", doc)}
    code = code_of(WU_INSTALL_SCRIPT)
    used = {int(n) for n in re.findall(r"\breturn (\d)\b", code)}
    used |= {int(n) for n in re.findall(r"\$exitCode = (\d)\b", code)}
    used |= {int(n) for n in re.findall(r"\bexit (\d)\b", code)}
    assert documented == used == set(range(8))


def test_install_script_installs_quietly_one_by_one() -> None:
    code = code_of(WU_INSTALL_SCRIPT)
    assert "$inst.ForceQuiet = $true" in WU_INSTALL_SCRIPT
    assert "$inst.AllowSourcePrompts = $false" in WU_INSTALL_SCRIPT
    assert not re.search(r"AllowSourcePrompts\s*=\s*\$true", WU_INSTALL_SCRIPT, re.I)
    assert code.count(".Install()") == 1
    loop = WU_INSTALL_SCRIPT[WU_INSTALL_SCRIPT.rindex("foreach ($u in $todo) {") :]
    install = loop.index("$inst.Install()")
    assert loop.index("try {") < install < loop.index("} catch {")
    assert loop.index("$one = New-Object -ComObject Microsoft.Update.UpdateColl") < install
    assert loop.index("Save-Status") > loop.index("} catch {")


def test_install_script_validates_paths_and_reads_allow_list() -> None:
    script = WU_INSTALL_SCRIPT
    assert "[IO.Path]::IsPathRooted($LogPath)" in script
    assert "[IO.Path]::IsPathRooted($StatusPath)" in script
    assert script.index("{ exit 7 }") < script.index("function Write-Log")
    assert "$IdList.Count -gt 0 -and $IdList -notcontains $id" in script
    assert "ToLowerInvariant()" in script[script.index("$IdList") : script.index("$SkipDrivers")]


def test_skip_rules_match_python() -> None:
    script = WU_INSTALL_SCRIPT
    for env_name, ps_var in (
        (updates.ENV_WU_SKIP_DRIVERS, "$SkipDrivers"),
        (updates.ENV_WU_SKIP_DISPLAY_DRIVERS, "$SkipDisplayDrivers"),
        (updates.ENV_WU_SKIP_INTERACTIVE, "$SkipInteractive"),
    ):
        assert f"{ps_var} = ([string]$env:{env_name} -eq '1')" in script
    assert "if ([int]$u.Type -eq 2) {" in script  # utDriver, as "driver" in the scan
    assert "[string]$u.DriverClass -eq 'Display'" in script
    assert "$SkipInteractive -and $u.InstallationBehavior.CanRequestUserInput" in script
    assert "$isDriver = ([int]$u.Type -eq 2)" in WU_SCAN_SCRIPT
    assert "canRequestUserInput = [bool]$u.InstallationBehavior.CanRequestUserInput" in (
        WU_SCAN_SCRIPT
    )


def test_status_writes_are_atomic_and_refuse_existing_temp() -> None:
    save = WU_INSTALL_SCRIPT[WU_INSTALL_SCRIPT.index("function Save-Status") :]
    save = save[: save.index("function Invoke-WindowsUpdate")]
    assert save.index("[IO.File]::Delete($tmp)") < save.index("[IO.FileMode]::CreateNew")
    assert "[IO.FileShare]::None" in save
    # $null for a string parameter arrives as '' ("invalid path"): seen on real hardware.
    assert "[IO.File]::Replace($tmp, $StatusPath, [NullString]::Value)" in save
    assert "Replace($tmp, $StatusPath, $null)" not in save
    assert "[IO.File]::Move($tmp, $StatusPath)" in save
    assert save.index("try {") < save.index("} catch {")


def test_install_script_never_restarts() -> None:
    code = code_of(WU_INSTALL_SCRIPT)
    assert "RebootRequiredBeforeInstallation" in code  # only read, then exit 5
    assert not re.search(r"Restart|Reboot\(|shutdown|InitiateSystemShutdown", code)


# ------------------------------------------------------------- store script


def test_store_script_options() -> None:
    script = STORE_SCRIPT
    assert "$opts.AllowForcedAppRestart = $false" in script
    assert "$opts.AutomaticallyDownloadAndInstallUpdateIfFound = $true" in script
    assert (
        "$Mode = $(if ([string]$env:WINSTALLER_STORE_MODE -eq 'update') { 'update' } "
        "else { 'list' })"
    ) in script
    search = script.index("SearchForAllUpdatesAsync")
    assert script.index("if ($Mode -eq 'update') {") < search
    assert script.index("IsStoreBlockedByPolicyAsync") < search
    assert "$task.Wait([TimeSpan]::FromSeconds($TimeoutSeconds))" in script
    assert "blockedByPolicy = $false" in script


def test_store_env_values() -> None:
    assert updates._store_env("update", 180.0) == {
        updates.ENV_STORE_MODE: "update",
        updates.ENV_STORE_TIMEOUT: "180",
    }
    assert updates._store_env("list", 0.2)[updates.ENV_STORE_TIMEOUT] == "1"
