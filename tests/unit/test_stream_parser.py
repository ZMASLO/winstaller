"""StreamParser and decode_console_bytes: console bytes in, log events out."""

from __future__ import annotations

import random

import pytest

from tests.support import (
    SAMPLE_FILES,
    as_tuples,
    bytewise,
    parse_chunks,
    parse_whole,
    random_chunks,
    sample,
    split_at,
)
from winstaller.core.winget import OutputEvent, OutputKind, StreamParser, decode_console_bytes

A = "append"
R = "replace_last"

POLISH = ["Zażółć gęślą jaźń", "Pomyślnie zainstalowano", "Łódź, Gdańsk, Świnoujście"]


def _bar(full: int) -> str:
    return "█" * full + "▒" * (30 - full)


INSTALL_EXPECTED = [
    (A, "-"),
    (R, "\\"),
    (R, "|"),
    (R, "/"),
    # The spinner and the first message share one physical line (no \n in between).
    (R, "Znaleziono 7-Zip [7zip.7zip] Wersja 24.09"),
    (A, "Ta aplikacja jest licencjonowana dla Ciebie przez jej właściciela."),
    (
        A,
        "Firma Microsoft nie jest odpowiedzialna za pakiety innych firm ani nie udziela "
        "do nich żadnych licencji.",
    ),
    (A, "Pobieranie https://7-zip.org/a/7z2409-x64.exe"),
    (A, _bar(0) + "  0 B / 1.56 MB"),
    (R, _bar(9) + "  512 KB / 1.56 MB"),
    (R, _bar(19) + "  1.00 MB / 1.56 MB"),
    (R, _bar(30) + "  1.56 MB / 1.56 MB"),
    (A, "Pomyślnie zweryfikowano skrót instalatora"),
    (A, "Rozpoczynanie instalacji pakietu..."),
    (A, "Pomyślnie zainstalowano"),
]


# ------------------------------------------------------------ decoding


class TestDecodeConsoleBytes:
    def test_empty(self) -> None:
        assert decode_console_bytes(b"") == ""

    @pytest.mark.parametrize("encoding", ["utf-8", "cp1250", "cp852"])
    @pytest.mark.parametrize("text", POLISH)
    def test_polish_text_in_supported_encodings(self, encoding: str, text: str) -> None:
        assert decode_console_bytes(text.encode(encoding)) == text

    def test_ascii_is_identity(self) -> None:
        assert decode_console_bytes(b"Found 7-Zip [7zip.7zip]") == "Found 7-Zip [7zip.7zip]"

    def test_never_raises_on_arbitrary_bytes(self) -> None:
        rng = random.Random(1234)
        for _ in range(500):
            raw = bytes(rng.randrange(256) for _ in range(rng.randint(1, 40)))
            assert isinstance(decode_console_bytes(raw), str)

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG decode_console_bytes: CP852 (OEM code page of Polish consoles, used by "
            "shutdown.exe/powercfg/PowerShell without UTF-8 output) is tried after CP1250. "
            "CP852 bytes that are also valid CP1250 decode as CP1250 mojibake: "
            "'Nie można znaleźć' -> 'Nie moľna znale«†'."
        ),
    )
    @pytest.mark.parametrize("text", ["Nie można znaleźć pliku", "pięć", "Ąę"])
    def test_cp852_text_that_is_also_valid_cp1250(self, text: str) -> None:
        assert decode_console_bytes(text.encode("cp852")) == text


# -------------------------------------------------------- whole samples


class TestSamples:
    def test_synthetic_install_sample(self) -> None:
        assert parse_whole(sample("winget_install_synthetic.bin")) == INSTALL_EXPECTED

    @pytest.mark.parametrize("name", ["winget_show_7zip.bin", "winget_search_chrome.bin"])
    def test_real_winget_captures_give_one_append_per_line(self, name: str) -> None:
        raw = sample(name)
        expected = [(A, line.strip()) for line in raw.decode("utf-8").split("\r\n") if line.strip()]
        assert parse_whole(raw) == expected

    def test_winget_show_content(self) -> None:
        events = parse_whole(sample("winget_show_7zip.bin"))
        assert events[0] == (A, "Found 7-Zip [7zip.7zip]")
        assert any(text.startswith("Copyright: Copyright ©") for _, text in events)

    def test_winget_search_table(self) -> None:
        texts = [t for _, t in parse_whole(sample("winget_search_chrome.bin"))]
        assert texts[0].split()[:2] == ["Name", "Id"]
        assert set(texts[1]) == {"-"}
        assert any("Google.Chrome" in t for t in texts)

    @pytest.mark.parametrize("encoding", ["utf8", "cp1250", "cp852"])
    def test_polish_samples(self, encoding: str) -> None:
        events = parse_whole(sample(f"polish_{encoding}.bin"))
        assert events == [(A, text) for text in POLISH]

    @pytest.mark.parametrize("name", SAMPLE_FILES)
    def test_no_escape_or_control_residue(self, name: str) -> None:
        for _, text in parse_whole(sample(name)):
            assert "\x1b" not in text
            assert "]9;4" not in text
            assert not any(ord(ch) < 32 and ch != "\t" for ch in text)
            assert text == text.strip() and text


# --------------------------------------------- chunking invariance (property)


class TestChunkInvariance:
    @pytest.mark.parametrize("name", ["winget_install_synthetic.bin", "winget_show_7zip.bin"])
    def test_every_single_cut_point(self, name: str) -> None:
        data = sample(name)
        whole = parse_whole(data)
        for cut in range(len(data) + 1):
            assert parse_chunks(split_at(data, [cut])) == whole, f"cut at {cut}"

    def test_every_cut_point_of_search_sample_with_stride(self) -> None:
        data = sample("winget_search_chrome.bin")
        whole = parse_whole(data)
        for cut in range(0, len(data) + 1, 7):
            assert parse_chunks(split_at(data, [cut])) == whole, f"cut at {cut}"

    @pytest.mark.parametrize("name", SAMPLE_FILES)
    def test_byte_by_byte(self, name: str) -> None:
        data = sample(name)
        assert parse_chunks(bytewise(data)) == parse_whole(data)

    @pytest.mark.parametrize("name", SAMPLE_FILES)
    @pytest.mark.parametrize("seed", range(10))
    def test_random_chunks(self, name: str, seed: int) -> None:
        data = sample(name)
        assert parse_chunks(random_chunks(data, seed, max_size=37)) == parse_whole(data)

    def test_all_samples_concatenated_random_multi_cuts(self) -> None:
        data = b"".join(sample(n) for n in SAMPLE_FILES)
        whole = parse_whole(data)
        rng = random.Random(99)
        for _ in range(200):
            cuts = rng.sample(range(len(data)), rng.randint(1, 12))
            assert parse_chunks(split_at(data, cuts)) == whole

    def test_empty_feeds_are_harmless(self) -> None:
        data = sample("winget_install_synthetic.bin")
        chunks = [b""]
        for piece in random_chunks(data, 5):
            chunks += [piece, b""]
        assert parse_chunks(chunks) == INSTALL_EXPECTED

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG (low) StreamParser: the escape carry is capped at 4096 bytes, so an OSC "
            "string longer than that is removed when fed at once but leaks into the log "
            "as text when fed in small chunks (output depends on chunking)."
        ),
    )
    def test_osc_longer_than_carry_limit(self) -> None:
        data = b"\x1b]0;" + b"x" * 5000 + b"\x07tail\n"
        assert parse_chunks(random_chunks(data, 3, max_size=512)) == parse_whole(data)


# --------------------------------------------------------- boundaries


class TestBoundaries:
    @pytest.mark.parametrize(
        "chunks",
        [
            [b"\x1b", b"[31mred\x1b[0m\n"],
            [b"\x1b[", b"31mred\x1b[0m\n"],
            [b"\x1b[3", b"1mred\x1b[0m\n"],
            [b"\x1b[31", b"mred\x1b[0m\n"],
            [b"\x1b[31mred\x1b", b"[0m\n"],
            [b"\x1b[31mred\x1b[0", b"m\n"],
        ],
    )
    def test_csi_split_across_chunks(self, chunks: list[bytes]) -> None:
        assert parse_chunks(chunks) == [(A, "red")]

    @pytest.mark.parametrize(
        "chunks",
        [
            [b"\x1b]9;4;1;", b"50\x07ok\n"],
            [b"\x1b]", b"9;4;1;50\x07ok\n"],
            [b"\x1b]9;4;1;50", b"\x07ok\n"],
            [b"\x1b]0;title\x1b", b"\\ok\n"],
            [b"\x1b]0;title", b"\x1b\\ok\n"],
        ],
    )
    def test_osc_split_across_chunks(self, chunks: list[bytes]) -> None:
        assert parse_chunks(chunks) == [(A, "ok")]

    @pytest.mark.parametrize("text", ["żółw", "█▒", "Świnoujście"])
    def test_multibyte_character_split_at_every_byte(self, text: str) -> None:
        data = (text + "\n").encode("utf-8")
        for cut in range(len(data) + 1):
            assert parse_chunks(split_at(data, [cut])) == [(A, text)]

    def test_escape_split_inside_multibyte_neighbourhood(self) -> None:
        data = "ż\x1b[1mó\x1b[0mł\n".encode()
        for chunks in (bytewise(data), *(split_at(data, [c]) for c in range(len(data)))):
            assert parse_chunks(chunks) == [(A, "żół")]


# ------------------------------------------------------------ semantics


class TestSemantics:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (b"a\r\nb\r\n", [(A, "a"), (A, "b")]),
            (b"a\nb\n", [(A, "a"), (A, "b")]),
            (b"a\rb\r", [(A, "a"), (R, "b")]),
            (b"a\rb\n", [(A, "a"), (R, "b")]),
            (b"a\r\rb\n", [(A, "a"), (R, "b")]),
            (b"a\r\n\r\nb\n", [(A, "a"), (A, "b")]),
            (b"\rx\r", [(A, "x")]),
        ],
    )
    def test_line_endings(self, data: bytes, expected: list[tuple[str, str]]) -> None:
        assert parse_whole(data) == expected

    def test_progress_bar_semantics_from_docs(self) -> None:
        assert parse_whole(b"\r##  \r### \r####\n") == [(A, "##"), (R, "###"), (R, "####")]

    @pytest.mark.parametrize("data", [b"", b"   \n", b"\t\r\n", b" \r \r \n", b"\n\n\r\r"])
    def test_blank_segments_produce_nothing(self, data: bytes) -> None:
        assert parse_whole(data) == []

    def test_blank_overwrite_keeps_previous_line(self) -> None:
        # "\r   \r" (clearing a line) must not emit an empty replace event.
        assert parse_whole(b"50%\r   \rdone\n") == [(A, "50%"), (R, "done")]

    def test_backspace_emulation(self) -> None:
        assert parse_whole(b"abc\b\bX\n") == [(A, "aX")]
        assert parse_whole(b"\b\bok\n") == [(A, "ok")]
        assert parse_whole(b"spin -\b\\\b|\b/\b\n") == [(A, "spin")]

    def test_control_characters_removed_but_tab_kept(self) -> None:
        assert parse_whole(b"a\x07b\x00c\x7f\n") == [(A, "abc")]
        assert parse_whole(b"OK\tpattern\tpkg\n") == [(A, "OK\tpattern\tpkg")]

    @pytest.mark.parametrize(
        "data",
        [
            b"\x1b[?25lplain\x1b[?25h\n",
            b"\x1b[2K\x1b[0Gplain\n",
            b"\x1b[38;5;196mplain\x1b[m\n",
            b"\x1b(Bplain\n",
            b"\x1bMplain\n",
            b"\x1b]9;4;3;0\x07plain\n",
            b"\x1b]8;;https://x\x1b\\plain\x1b]8;;\x1b\\\n",
        ],
    )
    def test_escape_sequences_removed(self, data: bytes) -> None:
        assert parse_whole(data) == [(A, "plain")]

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG (low) _ANSI_COMPLETE: only Fe two-byte escapes (ESC 0x40-0x5F) are "
            "removed; Fp/Fs ones such as ESC 7 / ESC 8 (save/restore cursor), ESC = / ESC > "
            "(keypad mode) and ESC c (reset) leave their final byte in the log text."
        ),
    )
    @pytest.mark.parametrize("data", [b"\x1b7plain\x1b8\n", b"\x1b=plain\x1b>\n", b"\x1bcplain\n"])
    def test_two_byte_fp_fs_escapes_removed(self, data: bytes) -> None:
        assert parse_whole(data) == [(A, "plain")]

    def test_lone_escape(self) -> None:
        assert parse_whole(b"\x1b") == []
        assert parse_whole(b"a\x1b\nb\n") == [(A, "a"), (A, "b")]
        assert parse_chunks([b"abc\x1b", b""]) == [(A, "abc")]

    def test_unfinished_escape_at_end_of_stream_is_dropped_on_flush(self) -> None:
        parser = StreamParser()
        assert parser.feed(b"done\x1b[") == []
        assert as_tuples(parser.flush()) == [(A, "done")]

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "BUG StreamParser: an OSC that is never terminated swallows everything after "
            "it, newlines included (_ANSI_PARTIAL_TAIL matches [^\\x07\\x1b]* up to the end), "
            "and flush() discards the carry, so 'real line' below is silently lost."
        ),
    )
    def test_unterminated_osc_does_not_eat_following_lines(self) -> None:
        events = parse_whole(b"\x1b]9;4;1;50 garbage\nreal line\n")
        assert (A, "real line") in events

    def test_very_long_line_without_separator(self) -> None:
        parser = StreamParser()
        line = b"x" * 1_000_000
        for start in range(0, len(line), 4096):
            assert parser.feed(line[start : start + 4096]) == []
        events = parser.flush()
        assert len(events) == 1
        assert events[0].kind is OutputKind.APPEND
        assert len(events[0].text) == len(line)

    def test_flush_emits_pending_overwrite_and_resets_state(self) -> None:
        parser = StreamParser()
        assert as_tuples(parser.feed(b"a\rx")) == [(A, "a")]
        assert as_tuples(parser.flush()) == [(R, "x")]
        assert parser.flush() == []
        # After flush a new physical line starts: no replace of the old one.
        assert as_tuples(parser.feed(b"b\n")) == [(A, "b")]

    def test_events_are_immutable_values(self) -> None:
        event = OutputEvent(OutputKind.APPEND, "x")
        assert event == OutputEvent(OutputKind.APPEND, "x")
        with pytest.raises(AttributeError):
            event.text = "y"  # type: ignore[misc]

    def test_mixed_encodings_are_decoded_per_line(self) -> None:
        # The CP852 line contains "ś" (0x98), a byte undefined in CP1250, so it is
        # unambiguous; see test_cp852_text_that_is_also_valid_cp1250 for the other case.
        data = (
            "Zażółć\n".encode() + "Pomyślnie\n".encode("cp1250") + "Świnoujście\n".encode("cp852")
        )
        assert parse_whole(data) == [(A, "Zażółć"), (A, "Pomyślnie"), (A, "Świnoujście")]
