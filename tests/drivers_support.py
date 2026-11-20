"""Fakes and data for the GPU driver tests (``winstaller.core.drivers``).

The driver code reaches the network only through a ``session`` with ``get``/``head``
(like ``requests.Session``); :class:`FakeSession` answers from a routing table and
records every request, so tests can check hosts, headers and the order of calls.
Real vendor answers from 2026-11-20 live in ``tests/data/drivers`` (large pages
trimmed to the part the parsers read).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from requests.structures import CaseInsensitiveDict

from winstaller.core import drivers

DRIVERS_DATA = Path(__file__).parent / "data" / "drivers"

NV_URL = (
    "https://us.download.nvidia.com/Windows/621.24/"
    "621.24-desktop-win10-win11-64bit-international-dch-whql.exe"
)
AMD_URL = "https://drivers.amd.com/drivers/whql-amd-software-adrenalin-edition-26.11.2-win11-c.exe"
AMD_B_URL = (
    "https://drivers.amd.com/drivers/whql-amd-software-adrenalin-edition-26.11.2-win11-b.exe"
)
AMD_NOTES = (
    "https://www.amd.com/en/resources/support-articles/release-notes/RN-RAD-WIN-26-11-2.html"
)
INTEL_URL = "https://downloadmirror.intel.com/932449/gfx_win_101.9175.exe"
URLS = {"nvidia": NV_URL, "amd": AMD_URL, "intel": INTEL_URL}
VERSIONS = {"nvidia": "621.24", "amd": "26.11.2", "intel": "32.0.101.9175"}
#: Size used for fake installers in tests that lower ``MIN_INSTALLER_BYTES``.
SMALL_MIN = 4096


def data(name: str) -> bytes:
    return (DRIVERS_DATA / name).read_bytes()


def text(name: str) -> str:
    return data(name).decode("utf-8")


def installer_bytes(size: int = 3 * SMALL_MIN, seed: int = 1) -> bytes:
    """Deterministic fake installer: ``MZ`` header + filler (never executed)."""
    body = hashlib.sha256(str(seed).encode()).digest() * (size // 32 + 1)
    return (b"MZ" + body)[:size]


def chunked(payload: bytes, size: int = 1024) -> list[bytes]:
    return [payload[i : i + size] for i in range(0, len(payload), size)] or [b""]


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        body: bytes | str = b"",
        *,
        headers: dict[str, str] | None = None,
        chunks: Iterable[bytes] | None = None,
        fail_after: int | None = None,
        fail_with: BaseException | None = None,
        on_chunk: Callable[[int], None] | None = None,
    ) -> None:
        self.status_code = status_code
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self._chunks = list(chunks) if chunks is not None else chunked(raw, 64 * 1024)
        self.headers: CaseInsensitiveDict[str] = CaseInsensitiveDict(headers or {})
        self._fail_after = fail_after
        self._fail_with = fail_with
        self._on_chunk = on_chunk
        self.closed = False

    def iter_content(self, chunk_size: int = 1):
        for n, chunk in enumerate(self._chunks):
            if self._fail_after is not None and n == self._fail_after:
                raise self._fail_with or OSError("connection reset")
            if self._on_chunk is not None:
                self._on_chunk(n)
            yield chunk
        if self._fail_after is not None and self._fail_after >= len(self._chunks):
            raise self._fail_with or OSError("connection reset")

    def close(self) -> None:
        self.closed = True


def redirect(location: str, status: int = 302) -> FakeResponse:
    return FakeResponse(status, headers={"Location": location})


def exe_response(payload: bytes, *, length: int | None | str = "auto", **kw: Any) -> FakeResponse:
    headers = {"Content-Type": "application/octet-stream"}
    if length == "auto":
        headers["Content-Length"] = str(len(payload))
    elif length is not None:
        headers["Content-Length"] = str(length)
    headers.update(kw.pop("headers", {}))
    return FakeResponse(200, chunks=chunked(payload, 1024), headers=headers, **kw)


Route = FakeResponse | BaseException | Callable[[str, dict[str, Any]], FakeResponse]


@dataclass
class Call:
    method: str
    url: str
    headers: dict[str, str]
    kwargs: dict[str, Any]


@dataclass
class FakeSession:
    """``routes``: URL -> response, exception, callable or a list (consumed in order)."""

    routes: dict[str, Route | list[Route]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    responses: list[FakeResponse] = field(default_factory=list)

    def _answer(self, method: str, url: str, kwargs: dict[str, Any]) -> FakeResponse:
        self.calls.append(Call(method, url, dict(kwargs.get("headers") or {}), kwargs))
        if url not in self.routes:
            raise AssertionError(f"unexpected {method} {url}")
        route = self.routes[url]
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        if isinstance(route, BaseException):
            raise route
        response = route(url, kwargs) if callable(route) else route
        self.responses.append(response)
        return response

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._answer("GET", url, kwargs)

    def head(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._answer("HEAD", url, kwargs)

    def urls(self) -> list[str]:
        return [c.url for c in self.calls]


def release(vendor: str = "nvidia", **overrides: Any) -> drivers.DriverRelease:
    fields: dict[str, Any] = {
        "vendor": vendor,
        "version": VERSIONS[vendor],
        "url": URLS[vendor],
        "source": "test",
    }
    fields.update(overrides)
    return drivers.DriverRelease(**fields)


def valid_signature_lines(subject: str) -> list[str]:
    import json

    return [
        json.dumps(
            {
                "Status": "Valid",
                "StatusMessage": "Signature verified.",
                "Subject": subject,
                "Thumbprint": "AB" * 20,
                "SignatureType": "Authenticode",
            }
        )
    ]


SUBJECTS = {
    "nvidia": "CN=NVIDIA Corporation, O=NVIDIA Corporation, L=Santa Clara, S=California, C=US",
    "amd": 'CN="Advanced Micro Devices, Inc.", O="Advanced Micro Devices, Inc.", L=Santa Clara, '
    "S=California, C=US",
    "intel": "CN=Intel Corporation, O=Intel Corporation, L=Santa Clara, S=California, C=US",
}
