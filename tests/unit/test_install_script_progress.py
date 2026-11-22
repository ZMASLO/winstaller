"""Static analysis of the progress reporting in ``WU_INSTALL_SCRIPT`` (nothing runs
PowerShell): the status contract shared with ``parse_install_status``, the per-update
download/install loops (index alignment between ``$todo`` and ``$entries``), error
handling around ``Download()``/``Install()`` and the final status."""

from __future__ import annotations

import ast
import inspect
import re
import subprocess

import pytest

from tests.unit.test_update_scripts import code_of
from winstaller.core import updates
from winstaller.core.update_scripts import WU_INSTALL_SCRIPT
from winstaller.core.updates import ITEM_STATUSES, powershell_args

CODE = code_of(WU_INSTALL_SCRIPT)
RAW = WU_INSTALL_SCRIPT


def mask(script: str) -> str:
    """``script`` with braces inside strings and comments replaced by ``_`` (same length,
    so positions map 1:1 to the raw text)."""
    out = list(script)
    i, n = 0, len(script)
    while i < n:
        ch = script[i]
        if ch == "#" or script.startswith("<#", i):
            block = script.startswith("<#", i)
            end = script.index("#>", i) + 2 if block else script.find("\n", i)
            end = n if end < 0 else end
            for j in range(i, end):
                if out[j] in "{}":
                    out[j] = "_"
            i = end
        elif ch in "'\"":
            j = i + 1
            while j < n:
                c = script[j]
                if ch == '"' and c == "`":
                    j += 2
                    continue
                if c == ch:
                    if j + 1 < n and script[j + 1] == ch:
                        j += 2
                        continue
                    break
                if c in "{}":
                    out[j] = "_"
                j += 1
            i = j + 1
        else:
            i += 1
    return "".join(out)


MASK = mask(RAW)


def block_after(start: int) -> str:
    """Raw text of the ``{ ... }`` block that opens at or after raw position ``start``."""
    i = MASK.index("{", start)
    depth = 0
    for j in range(i, len(MASK)):
        if MASK[j] == "{":
            depth += 1
        elif MASK[j] == "}":
            depth -= 1
            if depth == 0:
                return RAW[i : j + 1]
    raise AssertionError("unbalanced braces")


def function_body(name: str) -> str:
    return block_after(RAW.index(f"function {name}"))


def function_start(name: str) -> int:
    return RAW.index(f"function {name}")


def loop_bodies() -> tuple[str, str]:
    """(download loop, install loop) of Invoke-WindowsUpdate."""
    start = MASK.index("{", function_start("Invoke-WindowsUpdate"))
    end = start + len(function_body("Invoke-WindowsUpdate"))
    loops = [m.start() for m in re.finditer(r"foreach \(\$u in \$todo\)", RAW[start:end])]
    assert len(loops) == 2, loops
    return block_after(start + loops[0]), block_after(start + loops[1])


def enclosing_try_blocks(pos: int) -> list[str]:
    """``try { }`` blocks of the script that contain ``pos`` (raw position)."""
    found = []
    for m in re.finditer(r"\btry\s*\{", MASK):
        block = block_after(m.start())
        start = MASK.index("{", m.start())
        if start < pos < start + len(block):
            found.append(block)
    return found


# ------------------------------------------------------------- string literals of the script


def status_hash_text() -> str:
    """Body of ``$status = [ordered]@{ ... }`` (initial fields)."""
    start = RAW.index("$status = [ordered]@{") + len("$status = [ordered]@{")
    return RAW[start : RAW.index("}", start)]


def script_statuses() -> set[str]:
    raw = WU_INSTALL_SCRIPT
    found = set(re.findall(r"status = '(\w+)'", raw))
    found |= set(re.findall(r"status -eq '(\w+)'", raw))
    found |= set(re.findall(r"Set-Step \$\w+ '(\w+)'", raw))
    results = raw[raw.index("$Results = @{") : raw.index("}", raw.index("$Results = @{"))]
    found |= set(re.findall(r"= '(\w+)', '", results))
    found |= set(re.findall(r"\$r = '(\w+)', ", raw))
    found |= set(
        re.findall(r"\$\(if \(\$u\.IsDownloaded\) \{ '(\w+)' \} else \{ '(\w+)' \}\)", raw)[0]
    )
    return found


def test_item_statuses_match_the_parser() -> None:
    assert script_statuses() == set(ITEM_STATUSES)


def test_status_and_item_fields_match_the_parser() -> None:
    raw = WU_INSTALL_SCRIPT
    status_hash = status_hash_text()
    written = set(re.findall(r"(\w+) = ", status_hash)) | set(re.findall(r"\$status\.(\w+) =", raw))
    entry_hash = re.search(r"\$entries\.Add\(\[ordered\]@\{(.*?)\}\)", raw)
    assert entry_hash is not None
    item_written = set(re.findall(r"(\w+) = ", entry_hash.group(1)))
    item_written |= set(re.findall(r"\$(?:entry|Entry)\.(\w+) =", raw))

    source = inspect.getsource(updates.parse_install_status) + inspect.getsource(updates._item_from)
    read: dict[str, set[str]] = {"data": set(), "entry": set()}
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in read
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            read[node.func.value.id].add(node.args[0].value)
    assert read["data"] == written, read["data"] ^ written
    assert read["entry"] - {"result"} == item_written, read["entry"] ^ item_written


def test_harness_fixtures_have_exactly_the_written_fields() -> None:
    """The fixtures from the PowerShell 5.1 harness contain every field the script writes
    (``updated``/``stepStarted`` are added by Save-Status/Set-Step)."""
    import json

    from tests.updates_support import DATA

    data = json.loads((DATA / "progress" / "harness-08-finished.json").read_text("utf-8"))
    status_hash = status_hash_text()
    written = set(re.findall(r"(\w+) = ", status_hash)) | {"updated", "stepStarted"}
    assert set(data) == written
    assert set(data["updates"][0]) == {
        "id",
        "revision",
        "title",
        "status",
        "hresult",
        "rebootRequired",
    }


# ------------------------------------------------------------------ loops and indexes


def test_todo_and_entries_are_filled_together() -> None:
    """``$entries[$i]`` must describe the same update as the i-th of ``$todo``: both are
    appended in the same iteration, with no ``continue`` or anything that can fail
    between them."""
    body = function_body("Invoke-WindowsUpdate")
    add_todo = body.index("[void]$todo.Add($u)")
    add_entry = body.index("[void]$entries.Add(")
    between = body[add_todo + len("[void]$todo.Add($u)") : add_entry]
    assert between.strip() == "", between
    assert body.count("$todo.Add(") == 1 and body.count("$entries.Add(") == 1
    assert body.count("$todo.Remove") == 0 and body.count("$entries.Remove") == 0


@pytest.mark.parametrize("which", [0, 1])
def test_loop_index_advances_before_any_continue(which: int) -> None:
    loop = loop_bodies()[which]
    statements = [s.strip() for s in re.split(r"[;\n]", loop.strip("{} \n"))]
    assert statements[0] == "$entry = $entries[$i]" and statements[1] == "$i++", statements[:3]
    first_continue = loop.find("continue")
    assert first_continue == -1 or first_continue > loop.index("$i++")
    assert loop.count("$i++") == 1 and "$i--" not in loop and "$i =" not in loop


def test_loop_counters_start_at_zero() -> None:
    body = function_body("Invoke-WindowsUpdate")
    first = body.index("foreach ($u in $todo)")
    second = body.index("foreach ($u in $todo)", first + 1)
    assert body[:first].rstrip().endswith("$i = 0; $k = 0")
    assert body[:second].rstrip().endswith("$i = 0")


def test_download_loop() -> None:
    loop = loop_bodies()[0]
    assert "if ($entry.status -ne 'pending') { continue }" in loop
    assert loop.index("Set-Step $entry 'downloading'") < loop.index(".Download()")
    # Result by IsDownloaded, set after the try/catch on every path (never left 'downloading').
    after_try = loop[loop.index("} catch {") :]
    assert "$entry.status = 'downloaded'" in after_try
    assert "$entry.status = 'not_downloaded'" in after_try
    assert after_try.index("Save-Status") > after_try.index("'not_downloaded'")
    assert "break" not in loop and "return" not in loop and "throw" not in loop


def test_install_loop_skips_not_downloaded_and_reports_each() -> None:
    loop = loop_bodies()[1]
    skip = loop.index("if (-not $u.IsDownloaded)")
    install = loop.index(".Install()")
    assert skip < install
    base = RAW.index(loop)
    skip_branch = block_after(base + skip)
    assert "$entry.status = 'not_downloaded'" in skip_branch and ".Install()" not in skip_branch
    assert "$anyFailed = $true" in skip_branch
    catch = block_after(base + loop.index("} catch {") + 2)
    assert "$entry.status = 'error'" in catch and "$anyFailed = $true" in catch
    assert loop.rstrip("} \n").rstrip().endswith("Save-Status")
    assert "break" not in loop and "return" not in loop and "throw" not in loop


@pytest.mark.parametrize("call", [".Download()", ".Install()"])
def test_every_download_and_install_is_in_a_try(call: str) -> None:
    positions = [m.start() for m in re.finditer(re.escape(call), MASK)]
    assert positions
    for pos in positions:
        assert enclosing_try_blocks(pos), f"{call} at {pos} outside try"


def test_unknown_result_code_is_a_failure_in_the_script() -> None:
    loop = loop_bodies()[1]
    assert "if ($code -ge 4) { $anyFailed = $true }" not in loop
    assert (
        re.search(r"if \(\$code -(?:ne|notin)", loop)
        or "'unknown'" in loop.split("$anyFailed = $true")[0]
    )


def test_all_entries_listed_right_after_the_search() -> None:
    body = function_body("Invoke-WindowsUpdate")
    assign = body.index("$status.updates = $entries")
    assert body.index("$status.total = $todo.Count") > assign
    assert body.index("Save-Status", assign) < body.index("if ($todo.Count -eq 0)")
    assert body.count("$status.updates =") == 1  # never replaced by a partial list


def test_finally_writes_the_final_status() -> None:
    final = block_after(RAW.index("} finally {", RAW.index("$exitCode = 1")) + 2)
    for line in (
        "$status.state = 'finished'",
        "$status.phase = 'finished'",
        "$status.currentTitle = $null",
        "$status.exitCode = $exitCode",
    ):
        assert line in final, line
    assert final.index("$status.exitCode = $exitCode") < final.index("Save-Status")
    assert final.index("Save-Status") < final.index("Write-Log")  # status before "Koniec"


def test_reboot_flags() -> None:
    loop = loop_bodies()[1]
    assert (
        "if ($ir.RebootRequired -or $ur.RebootRequired) { $status.rebootRequired = $true }" in loop
    )
    assert "$status.rebootRequired = $true" in block_after(
        RAW.index("if ($installer.RebootRequiredBeforeInstallation)")
    )


def test_exit_codes_reach_the_status_and_the_parser_messages() -> None:
    body = function_body("Invoke-WindowsUpdate")
    returned = {int(c) for c in re.findall(r"return (\d+)", body)}
    main = RAW[RAW.index("$exitCode = 1") :]
    assigned = {int(c) for c in re.findall(r"\$exitCode = (\d+)", main)}
    early = {int(c) for c in re.findall(r"\bexit (\d+)", CODE)} - {0}
    used = returned | assigned | early
    assert used == {0, 1, 2, 3, 4, 5, 6, 7}
    assert set(updates.EXIT_MESSAGES) == used - {0}


# ---------------------------------------------------------------- .NET calls and output


def test_no_null_passed_to_dotnet_methods() -> None:
    """``$null`` for a .NET ``string`` parameter arrives as ``""`` (the File.Replace bug):
    no static or instance method call of the install script gets a bare ``$null``."""
    for m in re.finditer(r"(?:\]::|\.)\w+\(([^()]*(?:\([^()]*\)[^()]*)*)\)", CODE):
        args = [a.strip() for a in m.group(1).split(",")]
        assert "$null" not in args, m.group(0)
    assert "[NullString]::Value" in CODE


def test_function_output_cannot_pollute_the_exit_code() -> None:
    """``Invoke-WindowsUpdate | Select-Object -Last 1`` takes the last object written:
    every ``.Add(`` whose result is not used is ``[void]``."""
    for name in ("Invoke-WindowsUpdate", "Save-Status", "Set-Step", "Set-Phase"):
        body = function_body(name)
        for m in re.finditer(r"(\S*)\$\w+\.Add\(", body):
            assert m.group(1).endswith("[void]"), (name, m.group(0))
    assert "[int](Invoke-WindowsUpdate | Select-Object -Last 1)" in CODE


def test_status_write_retries_and_reports() -> None:
    save = function_body("Save-Status")
    assert "for ($n = 1; ; $n++)" in save and "if ($n -ge 20) { throw }" in save
    assert "Start-Sleep -Milliseconds 50" in save
    assert "Write-Log" in save.split("} catch {")[-1]
    assert save.index("$status.updated = Get-Now") < save.index("ConvertTo-Json")


def test_no_restart_eval_or_network_in_the_install_script() -> None:
    lowered = CODE.lower()
    for bad in (
        "invoke-expression",
        "iex ",
        "restart-computer",
        "shutdown",
        "invoke-webrequest",
        "net.webclient",
        "start-process",
        "usoclient",
        "searchforallupdatesasync",
        "store",
    ):
        assert bad not in lowered, bad


def test_encoded_command_headroom_is_recorded() -> None:
    length = len(subprocess.list2cmdline(powershell_args(WU_INSTALL_SCRIPT, hidden=True)))
    ratio = length / updates.MAX_COMMAND_LINE
    assert ratio < 0.85, ratio
    # Room left before the 85 % guard, in characters of the script (UTF-16 + base64 = x8/3).
    print(f"command line {length} = {ratio:.1%} of {updates.MAX_COMMAND_LINE}")
