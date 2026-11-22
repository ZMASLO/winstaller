"""Windows Update / Store: pure parsers and the HRESULT -> Polish message mapping.

Contract under test: every ``parse_*`` returns a correct result or raises
:class:`UpdatesError`, never anything else, whatever the input. Real samples
(tests/data/updates) plus mutations and a seeded fuzz.
"""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from tests.updates_support import (
    DATA,
    GUID_A,
    GUID_B,
    real_scan_data,
    scan_line,
    scan_with,
    store_cli_sample,
    store_line,
    update_entry,
)
from winstaller.core import updates
from winstaller.core.updates import (
    HRESULT_KINDS,
    UpdatesError,
    WindowsUpdateScan,
    clean_powershell_stderr,
    clean_store_lines,
    describe_wu_error,
    error_kind,
    format_hresult,
    hresult_value,
    parse_store_cli_output,
    parse_store_update,
    parse_wu_scan,
)

# ------------------------------------------------------------- real samples


def test_real_scan_sample() -> None:
    scan = parse_wu_scan(scan_line())
    assert len(scan.updates) == 5
    assert scan.criteria == "IsInstalled=0 and IsHidden=0 and DeploymentAction=*"
    assert scan.seconds == 7.2 and scan.result_code == 2
    assert not scan.reboot_pending and not scan.is_admin
    assert scan.optional_count == 5 and scan.driver_count == 3
    lcu = scan.updates[3]
    assert lcu.id == GUID_A and lcu.kb == ("KB5132057",) and not lcu.is_driver
    assert lcu.label() == (
        "KB5132057 — 2026-11 Aktualizacja wersji Preview (KB5132057) (26300.10075) [opcjonalna]"
    )
    hp = scan.updates[0]
    assert hp.is_driver and hp.driver_class == "OtherHardware"
    assert hp.driver_vendor == "Hewlett-Packard"
    # Double spaces in the WUA title collapse; the driver tag is appended.
    assert hp.label().startswith("Hewlett-Packard - Other hardware")
    assert hp.label().endswith("[opcjonalna] [sterownik]")
    assert scan.updates[1].downloaded and not scan.updates[0].downloaded


def test_real_sample_without_marker_is_rejected() -> None:
    raw = (DATA / "wu-scan-output.json").read_text(encoding="utf-8")
    with pytest.raises(UpdatesError) as exc:
        parse_wu_scan(raw)
    assert exc.value.kind == "format"


def test_real_store_list_sample() -> None:
    raw = (DATA / "store-listonly-output.json").read_text(encoding="utf-8")
    data = {"winstaller": "store-update", **json.loads(raw)}
    result = parse_store_update(json.dumps(data))
    assert result.method == "AppInstallManager"
    assert result.count == 0 and result.items == () and result.queue == ()
    assert result.auto_update_setting == "Enabled"


def test_real_store_cli_prompt_sample_is_an_error() -> None:
    with pytest.raises(UpdatesError) as exc:
        parse_store_cli_output(store_cli_sample())
    assert exc.value.kind == "store"
    assert "non-interactive" in exc.value.message


def test_clean_store_lines_on_real_sample() -> None:
    lines = clean_store_lines(store_cli_sample())
    assert lines[0] == "Checking for updates…"
    assert "Updates available (9 found)" in lines[1]
    assert "Name | Publisher | Version | Date" in lines
    assert "Aplikacja Łącze z | Microsoft Corporation | 1.26092.119.0 | 2026-11-06" in lines
    assert not any(set(line) & set("│┌┐└┘├┤┬┴┼") for line in lines)
    assert lines[-1] == "Failed to read input in non-interactive mode."
    assert all(line == line.strip() and line for line in lines)


# ------------------------------------------------------------- wu-scan: envelope


@pytest.mark.parametrize(
    "prefix,suffix",
    [
        ("", ""),
        ("\ufeff", ""),
        ("WARNING: coś\r\nśmieci { nie json\r\n", "\r\n\r\n"),
        ('{"winstaller": "inny", "ok": true}\n', "\nkoniec\n"),
        ("   ", "   "),
    ],
)
def test_scan_line_found_among_garbage(prefix: str, suffix: str) -> None:
    scan = parse_wu_scan(prefix + scan_line() + suffix)
    assert len(scan.updates) == 5


def test_last_marker_line_wins() -> None:
    first = scan_with(update_entry(id=GUID_A))
    last = scan_with(update_entry(id=GUID_B))
    scan = parse_wu_scan(first + "\n" + last)
    assert [u.id for u in scan.updates] == [GUID_B]


def test_nonzero_exit_with_valid_json_still_parses() -> None:
    scan = parse_wu_scan(scan_line(), stderr="cokolwiek", returncode=1)
    assert len(scan.updates) == 5


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\ufeff",
        "\r\n\r\n",
        "null",
        "[]",
        '"wu-scan"',
        '{"winstaller": "wu-scan"',  # truncated
        '{"ok": true, "updates": []}',  # no marker
        '{"winstaller": "store-update", "ok": true}',
        "[" * 50_000,
        '{"winstaller": "wu-scan", "ok": true, "x": ' + "[" * 50_000 + "]" * 50_000 + "}",
        "\x00\x01\x02 \udcff",
    ],
)
def test_scan_garbage_is_format_error(text: str) -> None:
    with pytest.raises(UpdatesError) as exc:
        parse_wu_scan(text)
    assert exc.value.kind == "format"


@pytest.mark.parametrize("value", [None, 5, b"{}", ["a"], object()])
def test_scan_non_string_input(value: Any) -> None:
    with pytest.raises(UpdatesError):
        parse_wu_scan(value)  # type: ignore[arg-type]


def test_no_output_message_uses_exit_code_and_clixml_stderr() -> None:
    stderr = (
        '#< CLIXML\r\n<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/'
        '2004/04"><S S="Error">Nie mo&#380;na za&#322;adowa&#263;_x000D__x000A_</S>'
        '<S S="Error">modu&#322;u_x000D__x000A_</S></Objs>'
    )
    with pytest.raises(UpdatesError) as exc:
        parse_wu_scan("", stderr=stderr, returncode=-1073741510)
    assert exc.value.kind == "format"
    assert "(kod -1073741510)" in exc.value.message
    assert exc.value.message.endswith(": Nie można załadować modułu")
    with pytest.raises(UpdatesError) as exc:
        parse_wu_scan("", returncode=0)
    assert "(brak wyniku)." in exc.value.message


def test_huge_input_is_handled_quickly() -> None:
    big_title = "Ż" * 2_000_000
    scan = parse_wu_scan("x" * 3_000_000 + "\n" + scan_with(update_entry(title=big_title)))
    assert len(scan.updates[0].title) == 300
    with pytest.raises(UpdatesError):
        parse_wu_scan("{" * 3_000_000)


# ------------------------------------------------------------- wu-scan: failures


@pytest.mark.parametrize(
    "hresult,kind,fragment",
    [
        ("0x80072EE2", "network", "Brak połączenia"),
        ("0x8024402C", "network", "Brak połączenia"),
        ("0x80070422", "service_disabled", "wuauserv"),
        ("0x8024002E", "policy", "zasady organizacji"),
        ("0x80240009", "busy", "zajęty"),
        ("0x80242014", "reboot", "ponowne uruchomienie"),
        ("0x80244022", "server", "chwilowo niedostępny"),
        ("0x80070005", "not_elevated", "administrator"),
        ("0x8024FFFF", "other", "Nie udało się wyszukać aktualizacji Windows: Coś nie tak"),
        ("0x8024402c", "network", "Brak połączenia"),  # lowercase hex
        (-2145107924, "network", "Brak połączenia"),  # int32 (0x8024402C)
        (2149859372, "network", "Brak połączenia"),  # uint32
    ],
)
def test_scan_failure_kinds(hresult: object, kind: str, fragment: str) -> None:
    text = scan_line(ok=False, updates=[], error={"hresult": hresult, "message": "Coś nie tak."})
    with pytest.raises(UpdatesError) as exc:
        parse_wu_scan(text, returncode=1)
    err = exc.value
    assert err.kind == kind
    assert fragment in err.message
    code = format_hresult(hresult_value(hresult))
    assert err.hresult == code
    assert err.message.endswith(f" (kod {code}).")
    assert ".." not in err.message


@pytest.mark.parametrize(
    "error",
    [
        None,
        {},
        "tekst",
        ["0x80072EE2"],
        {"hresult": None, "message": None},
        {"hresult": "8024402C", "message": "bez 0x"},
        {"hresult": "-2145107924", "message": "dziesiętnie w napisie"},
        {"hresult": True, "message": 5},
        {"hresult": "0x" + "F" * 9, "message": "za długi"},
    ],
)
def test_scan_failure_with_odd_error_objects(error: object) -> None:
    with pytest.raises(UpdatesError) as exc:
        parse_wu_scan(scan_line(ok=False, error=error))
    assert exc.value.kind == "other"
    assert exc.value.message.startswith("Nie udało się wyszukać aktualizacji Windows")
    assert exc.value.message.endswith(".")


@pytest.mark.parametrize("ok", [None, "true", 1, False, [True]])
def test_ok_must_be_exactly_true(ok: object) -> None:
    with pytest.raises(UpdatesError):
        parse_wu_scan(scan_line(ok=ok))


def test_failure_detail_is_one_line() -> None:
    message = "Linia 1\r\n2026-11-22 12:00:00 Koniec (kod 0).\x1b[31m\u0085x"
    with pytest.raises(UpdatesError) as exc:
        parse_wu_scan(scan_line(ok=False, error={"hresult": "0x80241234", "message": message}))
    assert "\n" not in exc.value.message and "\r" not in exc.value.message
    assert "\x1b" not in exc.value.message and "\x85" not in exc.value.message


@pytest.mark.parametrize("code", [4, 5])
def test_failed_search_result_code_is_not_up_to_date(code: int) -> None:
    with pytest.raises(UpdatesError):
        parse_wu_scan(scan_with(resultCode=code))


# ------------------------------------------------------------- wu-scan: entries


def test_zero_updates() -> None:
    for updates_value in ([], None):
        scan = parse_wu_scan(scan_with(updates=updates_value, resultCode=2))
        assert scan.updates == () and scan.optional_count == 0


def test_single_object_instead_of_list() -> None:
    data = json.loads(scan_with())
    data["updates"] = update_entry()
    assert len(parse_wu_scan(json.dumps(data)).updates) == 1


@pytest.mark.parametrize("value", ["abc", 5, True, {"a": 1} and "x"])
def test_updates_of_wrong_type(value: object) -> None:
    data = json.loads(scan_with())
    data["updates"] = value
    with pytest.raises(UpdatesError) as exc:
        parse_wu_scan(json.dumps(data))
    assert exc.value.kind == "format"


def test_duplicates_removed_by_id_and_revision() -> None:
    a1 = update_entry(id=GUID_A, revision=1)
    a1_upper = update_entry(id=GUID_A.upper(), revision=1, title="Inny tytuł")
    a2 = update_entry(id=GUID_A, revision=2)
    scan = parse_wu_scan(scan_with(a1, a1_upper, a2, a1))
    assert [u.key for u in scan.updates] == [(GUID_A, 1), (GUID_A, 2)]


@pytest.mark.parametrize(
    "bad_id",
    [
        None,
        "",
        5,
        "{" + GUID_A + "}",
        GUID_A + "'; calc; '",
        GUID_A + "\n",  # stripped -> valid; see test below
        "71ab9cfc4bdf48b7b7ad7f4ce4605cd7",
        "zzab9cfc-4bdf-48b7-b7ad-7f4ce4605cd7",
    ],
)
def test_invalid_update_id_skips_only_that_entry(bad_id: object) -> None:
    entry = update_entry(id=bad_id)
    if isinstance(bad_id, str) and bad_id.strip() == GUID_A:
        assert parse_wu_scan(scan_with(entry)).updates[0].id == GUID_A
        return
    scan = parse_wu_scan(scan_with(entry, update_entry(id=GUID_B)))
    assert [u.id for u in scan.updates] == [GUID_B] and scan.skipped == 1
    assert "nieprawidłowy identyfikator" in scan.warnings[0]
    assert "nie trafi na listę do instalacji" in scan.warnings[0]


@pytest.mark.parametrize("title", [None, "", "   ", "\r\n\t", 5, ["a"]])
def test_update_without_title_is_skipped(title: object) -> None:
    scan = parse_wu_scan(scan_with(update_entry(title=title)))
    assert scan.updates == () and scan.skipped == 1 and len(scan.warnings) == 1


def test_title_cannot_forge_log_lines() -> None:
    title = "Zwykła\r\n--- Windows Update ---\nSystem jest aktualny\x00\x1b[2K\u2028Koniec\u2029"
    data = json.loads(scan_with(update_entry(title=title, kb=[])))
    scan = parse_wu_scan(json.dumps(data, ensure_ascii=True))  # escaped, as ConvertTo-Json
    label = scan.updates[0].label()
    assert label.splitlines() == [label]
    assert label == "Zwykła --- Windows Update --- System jest aktualny [2K Koniec [opcjonalna]"


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_raw_unicode_line_separator_in_title(separator: str) -> None:
    text = scan_with(update_entry(title=f"Sterownik{separator}X", kb=[]))
    assert separator in text  # raw, not escaped
    assert parse_wu_scan(text).updates[0].title == "Sterownik X"


def test_polish_title_kept() -> None:
    title = "Aktualizacja zabezpieczeń — ŻÓŁW ąęśćźńół (KB5000001)"
    scan = parse_wu_scan(scan_with(update_entry(title=title, kb="KB5000001")))
    assert scan.updates[0].title == title
    assert scan.updates[0].kb == ("KB5000001",)


@pytest.mark.parametrize(
    "kb,expected",
    [
        (["KB1", "KB2"], ("KB1", "KB2")),
        ("KB5132057", ("KB5132057",)),
        (None, ()),
        ([], ()),
        ([5132057, None, "", "KB", "5132057", "KB12 34", "kb123", "KB999"], ("KB999",)),
        ({"KB": 1}, ()),
    ],
)
def test_kb_variants(kb: object, expected: tuple[str, ...]) -> None:
    assert parse_wu_scan(scan_with(update_entry(kb=kb))).updates[0].kb == expected


def test_label_without_kb_and_not_optional() -> None:
    entry = update_entry(kb=[], optional=False, type="software")
    assert parse_wu_scan(scan_with(entry)).updates[0].label() == entry["title"]


@pytest.mark.parametrize("revision,expected", [(3, 3), (-1, 0), (True, 0), ("2", 0), (1.5, 0)])
def test_revision_types(revision: object, expected: int) -> None:
    assert parse_wu_scan(scan_with(update_entry(revision=revision))).updates[0].revision == expected


def test_boolean_fields_require_real_booleans() -> None:
    entry = update_entry(
        optional="true", browseOnly=1, downloaded="yes", canRequestUserInput=None, type="Driver"
    )
    u = parse_wu_scan(scan_with(entry, rebootPending="true", isAdmin=1)).updates[0]
    assert not (u.optional or u.browse_only or u.downloaded or u.interactive or u.is_driver)


def test_driver_fields_only_for_drivers() -> None:
    software = update_entry(type="software", driverClass="Display", driverModel="X")
    driver = update_entry(type="driver", driverClass="Display", driverModel="", driverVendor=5)
    scan = parse_wu_scan(scan_with(software, update_entry(id=GUID_B, **driver_dict(driver))))
    assert scan.updates[0].driver_class is None
    d = scan.updates[1]
    assert d.driver_class == "Display" and d.driver_model is None and d.driver_vendor is None


def driver_dict(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in entry.items() if k != "id"}


def test_extra_fields_are_ignored() -> None:
    entry = update_entry(extra={"deep": [1, 2, {"x": None}]}, maxDownloadBytes=97258511172)
    scan = parse_wu_scan(scan_with(entry, extra="x", rebootPending=True, isAdmin=True))
    assert scan.reboot_pending and scan.is_admin and len(scan.updates) == 1


@pytest.mark.parametrize(
    "seconds,expected",
    [(7.25, 7.25), (-1, 0.0), ("7", 0.0), (None, 0.0), (0, 0.0)],
)
def test_seconds_field(seconds: object, expected: float) -> None:
    assert parse_wu_scan(scan_with(seconds=seconds)).seconds == expected


def test_criteria_and_result_code_types() -> None:
    scan = parse_wu_scan(scan_with(criteria=["x"], resultCode="2"))
    assert scan.criteria == "" and scan.result_code is None


def test_dataclass_properties() -> None:
    scan = WindowsUpdateScan(updates=())
    assert scan.optional_count == 0 and scan.driver_count == 0


# ------------------------------------------------------------- wu-scan: fuzz


def _mutate(value: Any, rng: random.Random, depth: int = 0) -> Any:
    choices: list[Any] = [
        None,
        True,
        False,
        0,
        -1,
        2**40,
        1.5,
        "",
        "x",
        "Ż\n\x00",
        [],
        {},
        [None],
        {"a": [1]},
    ]
    if isinstance(value, dict) and depth < 4:
        out = dict(value)
        for key in list(out):
            roll = rng.random()
            if roll < 0.15:
                del out[key]
            elif roll < 0.35:
                out[key] = rng.choice(choices)
            elif roll < 0.6:
                out[key] = _mutate(out[key], rng, depth + 1)
        if rng.random() < 0.2:
            out[f"extra{rng.randint(0, 9)}"] = rng.choice(choices)
        return out
    if isinstance(value, list) and depth < 4:
        out_list = [_mutate(v, rng, depth + 1) for v in value]
        if out_list and rng.random() < 0.3:
            out_list.append(out_list[0])  # duplicate
        if rng.random() < 0.1:
            out_list = []
        return out_list
    return value if rng.random() < 0.7 else rng.choice(choices)


def test_fuzz_scan_never_raises_anything_but_updates_error() -> None:
    rng = random.Random(20261122)
    base = real_scan_data()
    results = {"ok": 0, "err": 0}
    for _ in range(600):
        mutated = _mutate(base, rng)
        mutated["winstaller"] = "wu-scan" if rng.random() < 0.9 else rng.choice([None, 1])
        text = json.dumps(mutated, ensure_ascii=rng.random() < 0.5)
        roll = rng.random()
        if roll < 0.15:
            cut = rng.randint(0, len(text))
            text = text[:cut]
        elif roll < 0.3:
            pos = rng.randint(0, len(text))
            text = text[:pos] + rng.choice(["\n", "\x00", "}", "\ufeff", '"']) + text[pos:]
        try:
            scan = parse_wu_scan(text, stderr=rng.choice(["", "#< CLIXML <Objs>"]))
        except UpdatesError as exc:
            assert exc.message and isinstance(exc.message, str)
            results["err"] += 1
        else:
            assert isinstance(scan, WindowsUpdateScan)
            for u in scan.updates:
                assert updates._GUID.fullmatch(u.id) and u.title and "\n" not in u.label()
            assert len({u.key for u in scan.updates}) == len(scan.updates)
            results["ok"] += 1
    assert results["ok"] > 50 and results["err"] > 50


# ------------------------------------------------------------- store (WinRT JSON)


def test_store_found_items() -> None:
    found = [
        {"productId": "9WZDNCRFJBMP", "packageFamilyName": "Microsoft.WindowsStore_8wekyb3d8bbwe"},
        {"productId": "9NBLGGH4NNS1", "packageFamilyName": ""},
        {"productId": "", "packageFamilyName": "Contoso.App_abc"},
        {"productId": "", "packageFamilyName": ""},  # dropped
        "śmieci",
        None,
    ]
    result = parse_store_update(store_line(found=found, queue={"productId": "X1"}))
    assert result.count == 3
    assert [i.label() for i in result.items] == [
        "Microsoft.WindowsStore (9WZDNCRFJBMP)",
        "9NBLGGH4NNS1",
        "Contoso.App",
    ]
    assert [q.product_id for q in result.queue] == ["X1"]


def test_store_name_fallbacks() -> None:
    assert updates.StoreAppInfo("", "_hash").name == "_hash"
    assert updates.StoreAppInfo("", "").name == "?"


def test_store_policy_block() -> None:
    text = store_line(ok=False, blockedByPolicy=True, error={"hresult": None, "message": "x"})
    with pytest.raises(UpdatesError) as exc:
        parse_store_update(text)
    assert exc.value.kind == "policy"
    assert "zablokowany przez zasady" in exc.value.message


@pytest.mark.parametrize(
    "error,expected",
    [
        (
            {"hresult": "0x80070005", "message": "Odmowa dostępu."},
            "Odmowa dostępu (kod 0x80070005).",
        ),
        ({"hresult": -2147024891, "message": ""}, "nieznany błąd (kod 0x80070005)."),
        (None, "nieznany błąd."),
        ("x", "nieznany błąd."),
    ],
)
def test_store_failure(error: object, expected: str) -> None:
    with pytest.raises(UpdatesError) as exc:
        parse_store_update(store_line(ok=False, error=error))
    assert exc.value.kind == "store"
    assert exc.value.message == "Usługa Microsoft Store odrzuciła zlecenie: " + expected


@pytest.mark.parametrize(
    "text", ["", "garbage", '{"winstaller": "wu-scan", "ok": true}', "[" * 20000]
)
def test_store_garbage(text: str) -> None:
    with pytest.raises(UpdatesError) as exc:
        parse_store_update(text, returncode=1)
    assert exc.value.kind == "format"


def test_store_fuzz() -> None:
    rng = random.Random(7)
    base = json.loads(
        store_line(
            found=[{"productId": "A", "packageFamilyName": "B_c", "state": "Pending"}],
            queue=[{"productId": "Q"}],
        )
    )
    for _ in range(300):
        mutated = _mutate(base, rng)
        mutated["winstaller"] = "store-update"
        try:
            result = parse_store_update(json.dumps(mutated))
        except UpdatesError:
            continue
        assert result.method == "AppInstallManager"
        assert result.count == len(result.items)


# ------------------------------------------------------------- store.exe text

FOUND_TABLE = (
    "Checking for updates…\r\n\r\n── Updates available (2 found) ──────\r\n"
    "┌──────┬──────┐\r\n│ Name │ Publisher │\r\n├──────┼──────┤\r\n"
    "│ Paint │ Microsoft Corporation │\r\n│ XBOX │ Microsoft Corporation │\r\n"
    "└──────┴──────┘\r\n"
)


@pytest.mark.parametrize(
    "text",
    [
        "No updates available.",
        "Checking for updates…\r\nAll apps are up to date.\r\n",
        "Brak aktualizacji.",
        "\x1b[32mUpdates available (0 found)\x1b[0m",
    ],
)
def test_store_cli_nothing_to_do(text: str) -> None:
    result = parse_store_cli_output(text)
    assert result.method == "store-cli" and result.count == 0 and result.found_count == 0


@pytest.mark.parametrize(
    "tail",
    ["Pobieranie aktualizacji…", "Aktualizowanie 2 aplikacji", "Zainstalowano 2 aktualizacje"],
)
def test_store_cli_started_polish(tail: str) -> None:
    result = parse_store_cli_output(FOUND_TABLE + tail + "\r\n")
    assert result.found_count == 2 and result.count == 2
    assert "Paint | Microsoft Corporation" in result.lines


@pytest.mark.parametrize(
    "tail",
    ["Installing 2 updates...", "Updating apps", "Downloading", "Update queued", "Succeeded"],
)
def test_store_cli_started_english(tail: str) -> None:
    result = parse_store_cli_output(FOUND_TABLE + tail + "\r\n")
    assert result.found_count == 2


def test_store_started_pattern_has_no_control_characters() -> None:
    patterns = [updates._STORE_FAILURE, updates._STORE_NONE, updates._STORE_FOUND]
    for pattern in patterns:
        assert not updates._CONTROL.search(pattern.pattern), pattern.pattern


def test_store_started_pattern_is_clean() -> None:
    assert not updates._CONTROL.search(updates._STORE_STARTED.pattern)


@pytest.mark.parametrize(
    "text",
    [
        FOUND_TABLE + "Access is denied.",
        FOUND_TABLE + "Installing...\r\nError: 0x80070005",
        FOUND_TABLE + "Failed to install Paint",
        FOUND_TABLE + "Unable to reach the Microsoft Store",
        FOUND_TABLE + "Nie udało się zainstalować",
        FOUND_TABLE + "Exception: something",
        "Microsoft Store could not check for updates",
        "Nie można połączyć się z Microsoft Store. Brak aktualizacji.",
    ],
)
def test_store_cli_failure_words(text: str) -> None:
    with pytest.raises(UpdatesError) as exc:
        parse_store_cli_output(text)
    assert exc.value.kind == "store"
    assert exc.value.message.startswith("Polecenie store.exe zgłosiło błąd: ")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\r\n\r\n   \r\n",
        "┌────┐\r\n└────┘",
        FOUND_TABLE + "Would you like to install the 2 Store update(s) now? [y/n] (y): ",
    ],
)
def test_store_cli_empty_or_prompt_is_error(text: str) -> None:
    with pytest.raises(UpdatesError) as exc:
        parse_store_cli_output(text, returncode=0)
    assert exc.value.kind == "store"


# False SUCCESS is worse than a false error: the user is told the apps are updating (or
# up to date) while nothing happened. Each output below is a failure.
FALSE_SUCCESS_OUTPUTS = [
    pytest.param(FOUND_TABLE + "Pobieranie...\r\nThe operation timed out.\r\n", id="timed-out"),
    pytest.param(
        FOUND_TABLE + "Aktualizowanie 2 aplikacji...\r\nCoś poszło nie tak (0x80070005).\r\n",
        id="hresult-code",
    ),
    pytest.param(FOUND_TABLE + "Pobieranie...\r\nCompleted with 2 errors.\r\n", id="plural-errors"),
    pytest.param(
        FOUND_TABLE + "Would you like to install the 2 Store update(s) now? [y/n] (y): "
        "\r\nPobieranie\r\n",
        id="yn-prompt",
    ),
    pytest.param(
        "Microsoft Store is blocked by your organization. No updates were checked.",
        id="blocked-no-updates",
    ),
    pytest.param("Sign in to Microsoft Store. No updates were installed.", id="sign-in-no-updates"),
    pytest.param("No updates available\r\n0x803F8001\r\n", id="no-updates-with-code"),
    pytest.param(FOUND_TABLE + "Zainstalowano 0 z 2.\r\n", id="installed-zero"),
]


@pytest.mark.parametrize("text", FALSE_SUCCESS_OUTPUTS)
def test_store_cli_failure_never_reported_as_success(text: str) -> None:
    with pytest.raises(UpdatesError):
        parse_store_cli_output(text)


def test_store_cli_fuzz_never_crashes() -> None:
    rng = random.Random(99)
    sample = store_cli_sample()
    pieces = [
        "(3 found)",
        "Installing",
        "Pobieranie",
        "error",
        "No updates",
        "\x1b[2K",
        "\r",
        "\n",
        "│",
        "─",
        "\x00",
        "ż",
        "[y/n]",
    ]
    for _ in range(300):
        text = sample[: rng.randint(0, len(sample))]
        for _ in range(rng.randint(0, 5)):
            pos = rng.randint(0, len(text))
            text = text[:pos] + rng.choice(pieces) + text[pos:]
        try:
            result = parse_store_cli_output(text)
        except UpdatesError:
            continue
        assert result.method == "store-cli" and result.count >= 0


@pytest.mark.parametrize("value", [None, b"bytes", 5])
def test_store_cli_non_string(value: object) -> None:
    with pytest.raises(UpdatesError):
        parse_store_cli_output(value)  # type: ignore[arg-type]


# ------------------------------------------------------------- stderr cleaning


def test_clixml_stderr_decoded() -> None:
    text = (
        '\ufeff#< CLIXML\r\n<Objs Version="1.1.0.1"><S S="Error">Błąd: &lt;x&gt; &amp; '
        '_x000D__x000A_</S><S S="Error">druga linia_x000D__x000A_</S>'
        '<S S="Verbose">pomiń</S></Objs>'
    )
    assert clean_powershell_stderr(text) == "Błąd: <x> & druga linia"


def test_clixml_progress_only_is_empty() -> None:
    text = '#< CLIXML\r\n<Objs Version="1.1.0.1"><Obj S="progress" RefId="0"></Obj></Objs>'
    assert clean_powershell_stderr(text) == ""


def test_plain_stderr_is_one_line_and_limited() -> None:
    assert clean_powershell_stderr("a\r\nb\tc\x07") == "a b c"
    assert len(clean_powershell_stderr("x" * 5000)) == 500
    assert clean_powershell_stderr(None) == ""  # type: ignore[arg-type]


# ------------------------------------------------------------- HRESULT helpers


@pytest.mark.parametrize(
    "value,expected",
    [
        ("0x8024402C", 0x8024402C),
        ("0X8024402c ", 0x8024402C),
        (" 0x1", 1),
        (-2145107924, 0x8024402C),
        (0x8024402C, 0x8024402C),
        (-1, 0xFFFFFFFF),
        (0, 0),
        ("8024402C", None),
        ("-2145107924", None),
        ("0x123456789", None),
        ("0x", None),
        ("0xZZ", None),
        (True, None),
        (None, None),
        (1.0, None),
        ([0x80070005], None),
    ],
)
def test_hresult_value(value: object, expected: int | None) -> None:
    assert hresult_value(value) == expected


def test_format_hresult() -> None:
    assert format_hresult(None) is None
    assert format_hresult(5) == "0x00000005"
    assert format_hresult(0x8024402C) == "0x8024402C"
    assert format_hresult(-2145107924) == "0x8024402C"


@pytest.mark.parametrize("code,kind", sorted(HRESULT_KINDS.items()))
def test_every_table_code(code: int, kind: str) -> None:
    signed = code - 2**32
    for form in (code, signed, f"0x{code:08X}", f"0x{code:08x}"):
        assert error_kind(form) == kind
    err = describe_wu_error(f"0x{code:08x}", "szczegóły")
    assert err.kind == kind and err.hresult == f"0x{code:08X}"
    assert err.message.endswith(f" (kod 0x{code:08X}).")
    assert "szczegóły" not in err.message  # known classes use their own message
    assert err.message == err.message.strip() and ".." not in err.message


def test_every_table_kind_has_a_polish_message() -> None:
    for kind in set(HRESULT_KINDS.values()):
        assert kind in updates._KIND_MESSAGES
        message = updates._KIND_MESSAGES[kind]
        assert any(ch in message for ch in "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")


@pytest.mark.parametrize("value", [None, "", "abc", "0x80240032", 0, True])
def test_unknown_codes_are_other(value: object) -> None:
    assert error_kind(value) == "other"


def test_describe_unknown_without_code_and_detail() -> None:
    err = describe_wu_error(None)
    assert err.message == "Nie udało się wyszukać aktualizacji Windows."
    assert err.kind == "other" and err.hresult is None
    long = describe_wu_error("0x80241234", "x" * 1000 + ".")
    assert len(long.message) < 400 and long.message.endswith("(kod 0x80241234).")


def test_errors_are_exceptions_with_message() -> None:
    err = UpdatesError("Wiadomość", kind="policy", hresult="0x1")
    assert str(err) == err.message == "Wiadomość" and err.kind == "policy"
    cancelled = updates.UpdatesCancelled()
    assert isinstance(cancelled, UpdatesError) and cancelled.kind == "cancelled"
    assert cancelled.message == "Przerwano przez użytkownika."


# ------------------------------------------------------------- misc pure helpers


def test_log_file_names() -> None:
    from datetime import datetime

    log, status = updates.log_file_names(datetime(2026, 1, 2, 3, 4, 5))
    assert log == "windows-update-2026.01.02-030405.log"
    assert status == "windows-update-2026.01.02-030405.json"


def test_select_updates_defaults_install_everything() -> None:
    from tests.updates_support import info

    items = [
        info(GUID_A),
        info(GUID_B, type="driver", driver_class="Display", interactive=True),
        info("2f6e1f3a-5b7c-4d8e-9f10-1a2b3c4d5e6f", type="driver", driver_class="Net"),
    ]
    assert updates.SKIP_DRIVERS is False
    assert updates.SKIP_DISPLAY_DRIVERS is False
    assert updates.SKIP_INTERACTIVE is False
    assert updates.select_updates(items) == items
    assert updates.select_updates(items, skip_drivers=True) == items[:1]
    assert updates.select_updates(items, skip_display_drivers=True) == [items[0], items[2]]
    assert updates.select_updates(items, skip_interactive=True) == [items[0], items[2]]
    assert updates.select_updates(iter(items)) == items
