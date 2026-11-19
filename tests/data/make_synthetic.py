"""Regenerate the synthetic console samples in this directory.

Run manually: ``python tests/data/make_synthetic.py``. The real winget samples
(``winget_show_7zip.bin``, ``winget_search_chrome.bin``) were recorded with the
read-only commands ``winget show --id 7zip.7zip -e`` and ``winget search chrome``
(stdout+stderr piped, winget v1.29, English locale) and are not regenerated here.

The synthetic install sample mimics what winget writes to a VT console during an
install: OSC 9;4 taskbar progress (BEL and ST terminated), CSI cursor/erase
sequences, a ``\\r`` spinner, a ``\\r`` progress bar made of multi-byte block
characters and a ``\\b`` spinner. ``tests/unit/test_stream_parser.py`` holds the
expected events for it.
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent

POLISH_LINES = (
    "Zażółć gęślą jaźń",
    "Pomyślnie zainstalowano",
    "Łódź, Gdańsk, Świnoujście",
)


def progress_bar(percent: int, width: int = 30) -> str:
    filled = width * percent // 100
    return "█" * filled + "▒" * (width - filled)


PROGRESS_STEPS = ((0, "0 B"), (33, "512 KB"), (66, "1.00 MB"), (100, "1.56 MB"))


def install_sample() -> bytes:
    parts: list[bytes] = [
        b"\x1b]9;4;3;0\x1b\\",
        b"\r   - \r   \\ \r   | \r   / \r",
        b"\x1b[2K\x1b[0G",
        b"Znaleziono 7-Zip [7zip.7zip] Wersja 24.09\r\n",
        "Ta aplikacja jest licencjonowana dla Ciebie przez jej właściciela.\r\n".encode(),
        (
            "Firma Microsoft nie jest odpowiedzialna za pakiety innych firm ani nie "
            "udziela do nich żadnych licencji.\r\n"
        ).encode(),
        b"Pobieranie https://7-zip.org/a/7z2409-x64.exe\r\n",
    ]
    for percent, received in PROGRESS_STEPS:
        line = f"\x1b]9;4;1;{percent}\x07\x1b[0G  {progress_bar(percent)}  {received} / 1.56 MB\r"
        parts.append(line.encode())
    parts += [
        b"\x1b]9;4;0;0\x07\x1b[2K\r\n",
        "Pomyślnie zweryfikowano skrót instalatora\r\n".encode(),
        b"Rozpoczynanie instalacji pakietu...\r\n",
        b"\x1b[?25l  -\b\\\b|\b/\b\x1b[?25h\r\x1b[2K",
        "Pomyślnie zainstalowano\r\n".encode(),
    ]
    return b"".join(parts)


def polish_sample(encoding: str) -> bytes:
    return "".join(line + "\r\n" for line in POLISH_LINES).encode(encoding)


def main() -> None:
    (HERE / "winget_install_synthetic.bin").write_bytes(install_sample())
    for encoding in ("utf-8", "cp1250", "cp852"):
        name = encoding.replace("-", "")
        (HERE / f"polish_{name}.bin").write_bytes(polish_sample(encoding))


if __name__ == "__main__":
    main()
