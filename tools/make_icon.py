"""Generate ``packaging/winstaller.ico`` from the icon drawn in ``winstaller/ui/icons.py``.

Usage::

    .venv\\Scripts\\python tools\\make_icon.py [output.ico]

Renders the same picture as the running application (offscreen Qt, no window) at
16-256 px and writes a multi-resolution ICO with PNG-compressed entries (supported by
Windows Vista and later). ``winstaller.spec`` picks ``packaging/winstaller.ico`` up
automatically.
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from winstaller.ui.icons import _render  # noqa: E402

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
DEFAULT_OUTPUT = REPO_ROOT / "packaging" / "winstaller.ico"


def png_bytes(size: int) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not _render(size).toImage().save(buffer, "PNG"):
        raise RuntimeError(f"PNG encoding failed for {size}px")
    buffer.close()
    return bytes(data.data())


def build_ico(images: list[tuple[int, bytes]]) -> bytes:
    """ICONDIR + ICONDIRENTRY[] + PNG payloads."""
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, payloads = [], []
    for size, png in images:
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO format
        entries.append(struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset))
        payloads.append(png)
        offset += len(png)
    return header + b"".join(entries) + b"".join(payloads)


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUTPUT
    app = QGuiApplication.instance() or QGuiApplication(argv[:1])  # noqa: F841
    data = build_ico([(size, png_bytes(size)) for size in SIZES])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    print(f"{out}  {len(data)} B  sizes: {', '.join(map(str, SIZES))}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
