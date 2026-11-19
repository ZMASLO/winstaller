"""Application version.

``bump_version.py`` rewrites the literal in ``get_version`` with the regex
``return "\\d+\\.\\d+\\.\\d+"``, so keep that exact shape.
"""

from __future__ import annotations

APP_NAME = "Winstaller"


def get_version() -> str:
    return "1.1.8"


VERSION = get_version()


def get_version_info() -> str:
    """Human readable name with version, e.g. ``"Winstaller 1.1.8"``."""
    return f"{APP_NAME} {VERSION}"
