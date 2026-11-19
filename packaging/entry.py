"""PyInstaller entry script for ``winstaller.exe``.

PyInstaller runs its entry script as ``__main__`` outside any package, so the real entry point
(``winstaller/__main__.py``) is imported as a regular module of the ``winstaller`` package. This
keeps absolute imports working and avoids loading ``winstaller.__main__`` twice.
"""

from __future__ import annotations

import sys

from winstaller.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
