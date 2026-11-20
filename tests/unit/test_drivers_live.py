"""Live check of the vendor sources (marker ``live``; skipped unless
``WINSTALLER_LIVE_TESTS=1``). Resolves the newest driver of each vendor and asks the
download server for the first two bytes only - the installer is never downloaded."""

from __future__ import annotations

import socket

import pytest

from tests.conftest import Guard, SystemAccessBlocked
from winstaller.core import drivers


@pytest.mark.live
@pytest.mark.parametrize("vendor", ["nvidia", "amd", "intel"])
def test_vendor_source_still_answers(vendor: str) -> None:
    spec = drivers.VENDORS[vendor]
    session = drivers.default_session()
    release = drivers.resolve(vendor, session)
    drivers.version_key(release.version)
    assert drivers.host_allowed(release.url, spec.download_hosts), release.url
    assert release.url.lower().endswith(".exe")
    headers = {"User-Agent": drivers.user_agent(), **spec.download_headers, "Range": "bytes=0-1"}
    response = drivers.open_url(
        session,
        release.url,
        allowed_hosts=spec.download_hosts,
        headers=headers,
        timeout=drivers.META_TIMEOUT,
        stream=True,
        vendor_url=spec.vendor_url,
    )
    try:
        assert response.status_code in (200, 206), response.status_code
        content_type = str(response.headers.get("content-type", "")).lower()
        assert not any(bad in content_type for bad in ("text/", "html", "json", "xml"))
    finally:
        response.close()  # stream=True: the body is never read
    print(f"{vendor}: {release.version} {release.url} ({release.source}, {response.status_code})")


def test_vendor_hosts_stay_blocked_outside_live_tests(system_guard: Guard) -> None:
    """The pass-through applies only to ``live`` tests, whatever the environment says."""
    for host in ("www.amd.com", "us.download.nvidia.com", "dsadata.intel.com"):
        with pytest.raises(SystemAccessBlocked):
            socket.getaddrinfo(host, 443)
    assert system_guard.violations
    system_guard.violations.clear()
