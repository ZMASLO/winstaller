"""Helpers for the Windows Update / Microsoft Store tests (no fixtures here)."""

from __future__ import annotations

import base64
import copy
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from winstaller.core import updates
from winstaller.core.updates import WindowsUpdateInfo, WindowsUpdateScan

DATA = Path(__file__).parent / "data" / "updates"

GUID_A = "71ab9cfc-4bdf-48b7-b7ad-7f4ce4605cd7"
GUID_B = "9e5d0dcd-ff37-4266-a310-a874988f9bd0"
GUID_C = "702e6ed7-4f31-4f3b-9f49-de333467baf8"


def real_scan_data() -> dict[str, Any]:
    """The real scan of 2026-11-22 (5 updates). It was captured before the marker field
    existed, so the marker is added here exactly as ``WU_SCAN_SCRIPT`` writes it."""
    data = json.loads((DATA / "wu-scan-output.json").read_text(encoding="utf-8"))
    return {"winstaller": "wu-scan", **data}


def scan_line(**overrides: Any) -> str:
    data = real_scan_data()
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def update_entry(**overrides: Any) -> dict[str, Any]:
    entry = copy.deepcopy(real_scan_data()["updates"][3])  # the Preview LCU
    entry.update(overrides)
    return entry


def scan_with(*entries: dict[str, Any], **fields: Any) -> str:
    data = {"winstaller": "wu-scan", "ok": True, "updates": list(entries)}
    data.update(fields)
    return json.dumps(data, ensure_ascii=False)


def store_line(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "winstaller": "store-update",
        "ok": True,
        "mode": "update",
        "autoUpdateSetting": "Enabled",
        "blockedByPolicy": False,
        "found": [],
        "queue": [],
        "error": None,
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def store_cli_sample() -> str:
    return (DATA / "store-updates.txt").read_bytes().decode("utf-8")


def decode_script(args: Sequence[str]) -> str:
    assert args[-2] == "-EncodedCommand"
    return base64.b64decode(args[-1], validate=True).decode("utf-16-le")


def info(update_id: str = GUID_A, **fields: Any) -> WindowsUpdateInfo:
    fields.setdefault("revision", 1)
    fields.setdefault("title", f"Aktualizacja {update_id[:8]}")
    return WindowsUpdateInfo(id=update_id, **fields)


def real_scan() -> WindowsUpdateScan:
    return updates.parse_wu_scan(json.dumps(real_scan_data()))
