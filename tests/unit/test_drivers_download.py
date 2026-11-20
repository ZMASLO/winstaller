"""GPU drivers: host allow-list, redirects, file names, download checks, resolvers.

Everything goes through :class:`FakeSession`; nothing touches the network.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest
import urllib3

from tests.drivers_support import (
    AMD_B_URL,
    AMD_NOTES,
    AMD_URL,
    INTEL_URL,
    NV_URL,
    SMALL_MIN,
    FakeResponse,
    FakeSession,
    data,
    exe_response,
    installer_bytes,
    redirect,
    release,
)
from winstaller.core import drivers
from winstaller.core.drivers import DriverCancelled, DriverError

NV = drivers.NVIDIA_DOWNLOAD_HOSTS
AMD = drivers.AMD_DOWNLOAD_HOSTS
INTEL = drivers.INTEL_DOWNLOAD_HOSTS
ALL_HOSTS = (*NV, *AMD, *INTEL)


@pytest.fixture
def small_min(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(drivers, "MIN_INSTALLER_BYTES", SMALL_MIN)
    return SMALL_MIN


@pytest.fixture
def plenty_of_space(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    asked: list[Path] = []

    def free(path: Path) -> int:
        asked.append(path)
        return 10**15

    monkeypatch.setattr(drivers, "free_space", free)
    return asked


# -------------------------------------------------------------- host_allowed


@pytest.mark.parametrize(
    ("url", "hosts"),
    [
        (NV_URL, NV),
        ("https://international.download.nvidia.com/x.exe", NV),
        ("https://US.Download.NVIDIA.com/x.exe", NV),
        ("https://us.download.nvidia.com./x.exe", NV),
        ("https://us.download.nvidia.com:443/x.exe", NV),
        ("HTTPS://drivers.amd.com/x.exe", AMD),
        (AMD_URL, AMD),
        (INTEL_URL, INTEL),
    ],
)
def test_host_allowed_accepts(url: str, hosts: tuple[str, ...]) -> None:
    assert drivers.host_allowed(url, hosts)


@pytest.mark.parametrize(
    ("url", "hosts"),
    [
        ("http://us.download.nvidia.com/x.exe", NV),
        ("ftp://us.download.nvidia.com/x.exe", NV),
        ("//us.download.nvidia.com/x.exe", NV),
        ("us.download.nvidia.com/x.exe", NV),
        ("https://download.nvidia.com/x.exe", NV),  # suffix entry needs a subdomain
        ("https://evil-download.nvidia.com.attacker.net/x.exe", NV),
        ("https://download.nvidia.com.evil.com/x.exe", NV),
        ("https://xdownload.nvidia.com/x.exe", NV),
        ("https://us.download.nvidia.com.evil.com./x.exe", NV),
        ("https://drivers.amd.com@evil.com/x.exe", AMD),
        ("https://user:pw@drivers.amd.com/x.exe", AMD),
        ("https://drivers.amd.com\\@evil.com/x.exe", AMD),
        ("https://drivers.amd.com:8443/x.exe", AMD),
        ("https://drivers.amd.com:80/x.exe", AMD),
        ("https://drivers.amd.com:99999/x.exe", AMD),
        ("https://drivers.amd.com:0/x.exe", AMD),
        ("https://xn--drivers-amd-xyz.com/x.exe", AMD),
        ("https://drivers.amd.com.evil.com/x.exe", AMD),
        ("https://sub.drivers.amd.com/x.exe", AMD),  # exact entry: no subdomains
        ("https://drivers․amd.com/x.exe", AMD),
        ("https://evil.com#.us.download.nvidia.com/x.exe", NV),
        ("https://evil.com?.us.download.nvidia.com/x.exe", NV),
        ("https://evil.com/.us.download.nvidia.com/x.exe", NV),
        ("https://[::1]/x.exe", NV),
        ("https://127.0.0.1/x.exe", NV),
        ("", NV),
        ("https://", NV),
        ("https://[bad/x.exe", NV),
        (INTEL_URL, NV),
        (NV_URL, ()),
    ],
)
def test_host_allowed_rejects(url: str, hosts: tuple[str, ...]) -> None:
    assert not drivers.host_allowed(url, hosts)


TRICKY_URLS = [
    "https://evil.com\\.us.download.nvidia.com/x.exe",
    "https://evil.com\\@us.download.nvidia.com/x.exe",
    "https://evil.com%2f.us.download.nvidia.com/x.exe",
    "https://evil.com .us.download.nvidia.com/x.exe",
    "https://evil.com\t.us.download.nvidia.com/x.exe",
    "https://evil.com%00.us.download.nvidia.com/x.exe",
    " https://us.download.nvidia.com/x.exe",
    "https://us.download.nvidia.com\n/x.exe",
]


@pytest.mark.parametrize("url", TRICKY_URLS)
def test_allow_list_agrees_with_the_http_client(url: str) -> None:
    """Whatever host_allowed accepts must be a URL whose host *urllib3* (what requests
    connects to) also considers allowed - otherwise the check can be bypassed."""
    if not drivers.host_allowed(url, NV):
        return
    try:
        host = urllib3.util.parse_url(url.strip()).host or ""
    except Exception:
        return  # the HTTP client refuses the URL: nothing is fetched
    if any(ch in host for ch in " \t\n\x00%"):
        return  # not resolvable; requests raises InvalidURL
    assert drivers.host_allowed(f"https://{host}/x.exe", NV), (url, host)


def test_backslash_host_is_rejected() -> None:
    assert not drivers.host_allowed("https://evil.com\\.us.download.nvidia.com/x.exe", NV)


def test_backslash_url_from_api_never_reaches_the_session(
    tmp_path: Path, small_min: int, plenty_of_space: list[Path]
) -> None:
    evil = "https://evil.com\\.us.download.nvidia.com/621.24.exe"
    session = FakeSession({evil: exe_response(installer_bytes())})
    with pytest.raises(DriverError):
        drivers.download(release("nvidia", url=evil), tmp_path, session=session)
    assert session.calls == []


# ----------------------------------------------------------------- open_url


def test_open_url_follows_allowed_redirects_by_hand() -> None:
    session = FakeSession(
        {
            NV_URL: redirect("https://international.download.nvidia.com/a.exe", 301),
            "https://international.download.nvidia.com/a.exe": redirect("/b.exe", 307),
            "https://international.download.nvidia.com/b.exe": FakeResponse(200),
        }
    )
    response = drivers.open_url(session, NV_URL, allowed_hosts=NV, headers={"X": "1"})
    assert response.status_code == 200
    assert session.urls() == [
        NV_URL,
        "https://international.download.nvidia.com/a.exe",
        "https://international.download.nvidia.com/b.exe",
    ]
    assert all(c.kwargs["allow_redirects"] is False for c in session.calls)
    assert all(c.headers == {"X": "1"} for c in session.calls)
    assert all(r.closed for r in session.responses[:2])  # redirect bodies released


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.com/x.exe",
        "http://us.download.nvidia.com/x.exe",
        "//evil.com/x.exe",
        "https://us.download.nvidia.com@evil.com/x.exe",
        "https://us.download.nvidia.com:8080/x.exe",
        "https://download.nvidia.com.evil.com/x.exe",
    ],
)
@pytest.mark.parametrize("hop", [0, 2])
def test_every_redirect_hop_is_checked(location: str, hop: int) -> None:
    chain = [f"https://us.download.nvidia.com/{n}.exe" for n in range(hop + 1)]
    routes: dict[str, object] = {u: redirect(n) for u, n in zip(chain, chain[1:], strict=False)}
    routes[chain[-1]] = redirect(location)
    session = FakeSession(routes)  # type: ignore[arg-type]
    with pytest.raises(DriverError, match="Niedozwolony adres"):
        drivers.open_url(session, chain[0], allowed_hosts=NV, vendor_url="https://v")
    assert len(session.calls) == hop + 1  # the foreign host is never contacted


def test_redirect_loop_and_limit() -> None:
    loop = FakeSession({NV_URL: redirect(NV_URL)})
    with pytest.raises(DriverError, match="Zbyt wiele"):
        drivers.open_url(loop, NV_URL, allowed_hosts=NV)
    assert len(loop.calls) == drivers.MAX_REDIRECTS + 1
    hops = [f"https://us.download.nvidia.com/{n}.exe" for n in range(drivers.MAX_REDIRECTS + 1)]
    routes: dict[str, object] = {a: redirect(b) for a, b in zip(hops, hops[1:], strict=False)}
    routes[hops[-1]] = FakeResponse(200)
    ok = FakeSession(routes)  # type: ignore[arg-type]
    assert drivers.open_url(ok, hops[0], allowed_hosts=NV).status_code == 200  # exactly 5 hops


def test_redirect_without_location() -> None:
    with pytest.raises(DriverError, match="bez adresu"):
        drivers.open_url(FakeSession({NV_URL: FakeResponse(302)}), NV_URL, allowed_hosts=NV)


def test_amd_download_incomplete_redirect_is_an_error() -> None:
    session = FakeSession(
        {AMD_URL: redirect("https://www.amd.com/en/support/download/Download-Incomplete.html")}
    )
    with pytest.raises(DriverError, match="Download Incomplete") as info:
        drivers.open_url(session, AMD_URL, allowed_hosts=AMD, vendor_url=drivers.AMD_PAGE)
    assert info.value.vendor_url == drivers.AMD_PAGE
    assert len(session.calls) == 1


def test_network_error_becomes_driver_error() -> None:
    import requests

    session = FakeSession({NV_URL: requests.ConnectionError("x" * 500)})
    with pytest.raises(DriverError, match="us.download.nvidia.com") as info:
        drivers.open_url(session, NV_URL, allowed_hosts=NV)
    assert len(info.value.message) < 300  # long requests messages are shortened


def test_disallowed_start_url_is_never_requested() -> None:
    session = FakeSession()
    with pytest.raises(DriverError):
        drivers.open_url(session, "http://us.download.nvidia.com/x.exe", allowed_hosts=NV)
    assert session.calls == []


def test_fetch_bytes_status_and_limit() -> None:
    url = drivers.NVIDIA_LOOKUP_URL
    with pytest.raises(DriverError, match="HTTP 503"):
        drivers.fetch_bytes(
            FakeSession({url: FakeResponse(503)}), url, allowed_hosts=("gfwsl.geforce.com",)
        )
    big = FakeResponse(200, b"x" * 5000)
    with pytest.raises(DriverError, match="podejrzanie duża"):
        drivers.fetch_bytes(
            FakeSession({url: big}), url, allowed_hosts=("gfwsl.geforce.com",), limit=1000
        )
    assert big.closed
    broken = FakeResponse(200, b"x" * 10, fail_after=0)
    with pytest.raises(DriverError, match="Przerwano odczyt"):
        drivers.fetch_bytes(FakeSession({url: broken}), url, allowed_hosts=("gfwsl.geforce.com",))
    ok = FakeResponse(200, "zażółć".encode())
    assert (
        drivers.fetch_text(FakeSession({url: ok}), url, allowed_hosts=("gfwsl.geforce.com",))
        == "zażółć"
    )


# ---------------------------------------------------------- sanitize_filename


@pytest.mark.parametrize(
    ("url", "name"),
    [
        (NV_URL, NV_URL.rsplit("/", 1)[1]),
        ("https://x/..%5C..%5Cevil.exe", "evil.exe"),
        ("https://x/a%2F..%2Fb.exe", "a_.._b.exe"),
        ("https://x/a%5Cb.exe", "a_b.exe"),
        ("https://x/" + "a" * 300 + ".exe", "fb.exe"),
        ("https://x/.exe", "fb.exe"),
        ("https://x/", "fb.exe"),
        ("https://x/a.scr", "fb.exe"),
        ("https://x/a.exe%00.txt", "fb.exe"),
        ("https://x/a.exe?x=/b.txt", "a.exe"),
        ("https://x/a.exe.", "a.exe"),
        ("https://x/a.exe%20", "a.exe"),
        ("https://x/%C5%BC%C3%B3%C5%82w.exe", "w.exe"),
        ("https://[bad/a.exe", "fb.exe"),
    ],
)
def test_sanitize_filename(url: str, name: str) -> None:
    assert drivers.sanitize_filename(url, "fb.exe") == name


@pytest.mark.parametrize(
    "url",
    [
        "https://x/..%2F..%2F..%2Fwindows%2Fsystem32%2Fevil.exe",
        "https://x/C:%5CWindows%5Cevil.exe",
        "https://x/%2E%2E%5C%2E%2E%5Cx.exe",
        "https://x/con.exe",
        "https://x/" + "%E2%80%AE" * 50 + "exe.txt.exe",
        "https://x/a" + "%20" * 200 + ".exe",
    ],
)
def test_sanitized_name_stays_inside_dest_dir(url: str, tmp_path: Path) -> None:
    name = drivers.sanitize_filename(url)
    assert name.lower().endswith(".exe") and len(name) <= 120
    assert (tmp_path / name).resolve().parent == tmp_path.resolve()
    assert set(name) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


@pytest.mark.parametrize("stem", ["CON", "nul", "COM1", "LPT9", "AUX", "PRN"])
def test_sanitize_avoids_reserved_device_names(stem: str) -> None:
    name = drivers.sanitize_filename(f"https://x/{stem}.exe")
    assert name.split(".")[0].upper() not in {"CON", "NUL", "COM1", "LPT9", "AUX", "PRN"}


# ------------------------------------------------------------------ download


def files_in(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir())


def run_download(
    tmp_path: Path,
    response: FakeResponse | BaseException,
    vendor: str = "nvidia",
    **release_fields: object,
) -> tuple[Path, FakeSession, list[tuple[int, int | None]]]:
    rel = release(vendor, **release_fields)
    session = FakeSession({rel.url: response})
    progress: list[tuple[int, int | None]] = []
    path = drivers.download(
        rel, tmp_path, session=session, progress_cb=lambda r, t: progress.append((r, t))
    )
    return path, session, progress


@pytest.mark.usefixtures("small_min", "plenty_of_space")
class TestDownload:
    def test_success_with_progress(self, tmp_path: Path) -> None:
        payload = installer_bytes()
        path, session, progress = run_download(tmp_path, exe_response(payload))
        assert path == tmp_path / NV_URL.rsplit("/", 1)[1]
        assert path.read_bytes() == payload
        assert files_in(tmp_path) == [path.name]  # no .part left
        assert progress[-1] == (len(payload), len(payload))
        assert [r for r, _ in progress] == sorted(r for r, _ in progress)
        call = session.calls[0]
        assert call.kwargs["stream"] is True and call.kwargs["timeout"] == drivers.DOWNLOAD_TIMEOUT
        assert call.headers["User-Agent"].startswith("Winstaller/")
        assert "Referer" not in call.headers
        assert session.responses[0].closed

    def test_amd_sends_referer(self, tmp_path: Path) -> None:
        _path, session, _ = run_download(tmp_path, exe_response(installer_bytes()), "amd")
        assert session.calls[0].headers["Referer"] == "https://www.amd.com/"

    def test_without_content_length(self, tmp_path: Path) -> None:
        payload = installer_bytes()
        path, _s, progress = run_download(tmp_path, exe_response(payload, length=None))
        assert path.read_bytes() == payload
        assert progress[-1] == (len(payload), None)

    def test_tiny_content_length_is_ignored(self, tmp_path: Path) -> None:
        # drivers.amd.com announces "Content-Length: 20" for a 1.7 GB file
        payload = installer_bytes()
        path, _s, _p = run_download(tmp_path, exe_response(payload, length=20), "amd")
        assert path.read_bytes() == payload

    def test_intel_sha1_and_size_ok(self, tmp_path: Path) -> None:
        payload = installer_bytes()
        sha1 = hashlib.sha1(payload).hexdigest()
        path, _s, _p = run_download(
            tmp_path, exe_response(payload), "intel", size=len(payload), sha1=sha1
        )
        assert path.read_bytes() == payload

    def test_intel_sha512_ok(self, tmp_path: Path) -> None:
        payload = installer_bytes()
        sha = hashlib.sha512(payload).hexdigest()
        path, _s, _p = run_download(tmp_path, exe_response(payload), "intel", sha512=sha)
        assert path.exists()

    def test_overwrites_leftovers_from_a_previous_attempt(self, tmp_path: Path) -> None:
        name = NV_URL.rsplit("/", 1)[1]
        (tmp_path / name).write_bytes(b"old")
        (tmp_path / f"{name}.part").write_bytes(b"old part")
        payload = installer_bytes()
        path, _s, _p = run_download(tmp_path, exe_response(payload))
        assert path.read_bytes() == payload and files_in(tmp_path) == [name]

    @pytest.mark.parametrize(
        ("make", "fields", "match"),
        [
            (lambda p: FakeResponse(403), {}, "HTTP 403"),
            (lambda p: FakeResponse(404), {}, "HTTP 404"),
            (lambda p: FakeResponse(429), {}, "HTTP 429"),
            (lambda p: FakeResponse(500), {}, "HTTP 500"),
            (lambda p: FakeResponse(206, chunks=[p]), {}, "HTTP 206"),
            (lambda p: exe_response(b"PK" + p[2:]), {}, "MZ"),
            (lambda p: exe_response(p, headers={"Content-Type": "text/html"}), {}, "stronę"),
            (lambda p: exe_response(p, headers={"Content-Type": "application/json"}), {}, "stronę"),
            (lambda p: exe_response(p, headers={"Content-Type": "TEXT/PLAIN; x"}), {}, "stronę"),
            (lambda p: exe_response(b"<!DOCTYPE html>" + p), {}, "MZ"),
            (lambda p: exe_response(p[: SMALL_MIN - 1]), {}, "za mały"),
            (lambda p: exe_response(b""), {}, "za mały"),
            (lambda p: FakeResponse(200, chunks=[]), {}, "za mały"),
            (lambda p: exe_response(p[:-10], length=len(p)), {}, "niepełny"),
            (lambda p: exe_response(p + b"x", length=len(p)), {}, "więcej danych"),
            (lambda p: exe_response(p), {"size": 3 * SMALL_MIN + 1}, "nie zgadza"),
            (lambda p: exe_response(p, length=None), {"size": 3 * SMALL_MIN + 1}, "niepełny"),
            (lambda p: exe_response(p), {"sha1": "0" * 40}, "SHA1"),
            (lambda p: exe_response(p), {"sha512": "0" * 128}, "SHA512"),
            (lambda p: exe_response(p, fail_after=2), {}, "Przerwano pobieranie"),
            (
                lambda p: exe_response(p, fail_after=2, fail_with=ConnectionResetError(10054, "x")),
                {},
                "Przerwano pobieranie",
            ),
            (lambda p: OSError("network down"), {}, "Błąd połączenia"),
            (lambda p: TimeoutError("timed out"), {}, "Błąd połączenia"),
            (lambda p: redirect("https://evil.example/a.exe"), {}, "Niedozwolony"),
        ],
    )
    def test_failures_leave_nothing_behind(self, tmp_path, make, fields, match) -> None:
        payload = installer_bytes()
        with pytest.raises(DriverError, match=match) as info:
            run_download(tmp_path, make(payload), **fields)
        assert not isinstance(info.value, DriverCancelled)
        assert files_in(tmp_path) == []

    def test_uppercase_hash_from_caller(self, tmp_path: Path) -> None:
        # Parsers lowercase hashes; a caller passing uppercase gets a (safe) mismatch.
        payload = installer_bytes()
        sha1 = hashlib.sha1(payload).hexdigest().upper()
        with pytest.raises(DriverError, match="SHA1"):
            run_download(tmp_path, exe_response(payload), "intel", sha1=sha1)
        assert files_in(tmp_path) == []

    def test_gzip_content_encoding_is_not_special(self, tmp_path: Path) -> None:
        # requests decodes Content-Encoding; the MZ/size/hash checks see decoded bytes.
        payload = installer_bytes()
        response = exe_response(payload, headers={"Content-Encoding": "gzip"})
        path, _s, _p = run_download(tmp_path, response)
        assert path.read_bytes() == payload

    def test_cancel_before_first_chunk(self, tmp_path: Path) -> None:
        rel = release()
        cancel = threading.Event()
        cancel.set()
        session = FakeSession({rel.url: exe_response(installer_bytes())})
        with pytest.raises(DriverCancelled):
            drivers.download(rel, tmp_path, session=session, cancel_event=cancel)
        assert files_in(tmp_path) == []

    def test_cancel_mid_download(self, tmp_path: Path) -> None:
        rel = release()
        cancel = threading.Event()

        def on_chunk(n: int) -> None:
            if n == 3:
                cancel.set()

        session = FakeSession({rel.url: exe_response(installer_bytes(), on_chunk=on_chunk)})
        with pytest.raises(DriverCancelled, match="przerwane"):
            drivers.download(rel, tmp_path, session=session, cancel_event=cancel)
        assert files_in(tmp_path) == []
        assert session.responses[0].closed

    def test_cancel_after_last_chunk(self, tmp_path: Path) -> None:
        rel = release()
        cancel = threading.Event()
        payload = installer_bytes()
        last = len(payload) // 1024 - 1

        def on_chunk(n: int) -> None:
            if n == last:
                cancel.set()

        session = FakeSession({rel.url: exe_response(payload, on_chunk=on_chunk)})
        with pytest.raises(DriverCancelled):
            drivers.download(rel, tmp_path, session=session, cancel_event=cancel)
        assert files_in(tmp_path) == []

    def test_progress_callback_exception_cleans_up(self, tmp_path: Path) -> None:
        rel = release()
        session = FakeSession({rel.url: exe_response(installer_bytes())})

        def boom(_r: int, _t: int | None) -> None:
            raise RuntimeError("ui gone")

        with pytest.raises(RuntimeError):
            drivers.download(rel, tmp_path, session=session, progress_cb=boom)
        assert files_in(tmp_path) == []


def test_real_minimum_size_is_10_mib(tmp_path: Path, plenty_of_space: list[Path]) -> None:
    assert drivers.MIN_INSTALLER_BYTES == 10 * 1024 * 1024
    payload = installer_bytes(drivers.MIN_INSTALLER_BYTES - 1)
    with pytest.raises(DriverError, match="za mały"):
        run_download(tmp_path, exe_response(payload))
    assert files_in(tmp_path) == []


@pytest.mark.usefixtures("small_min")
class TestFreeSpace:
    def test_not_enough_space_before_download(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(drivers, "free_space", lambda _p: 100)
        session = FakeSession()
        with pytest.raises(DriverError, match="Za mało miejsca"):
            drivers.download(release(), tmp_path, session=session)
        assert session.calls == [] and files_in(tmp_path) == []

    def test_estimate_uses_vendor_size_times_three(self, tmp_path, monkeypatch) -> None:
        seen: list[int] = []

        def free(_p: Path) -> int:
            seen.append(1)
            return 3 * drivers.VENDORS["nvidia"].approx_size

        monkeypatch.setattr(drivers, "free_space", free)
        path, _s, _p = run_download(tmp_path, exe_response(installer_bytes()))
        assert path.exists() and len(seen) == 2  # before, and again after Content-Length

    def test_real_content_length_rechecked(self, tmp_path, monkeypatch) -> None:
        payload = installer_bytes()
        answers = iter([10**15, 3 * len(payload) - 1])
        monkeypatch.setattr(drivers, "free_space", lambda _p: next(answers))
        with pytest.raises(DriverError, match="Za mało miejsca"):
            run_download(tmp_path, exe_response(payload))
        assert files_in(tmp_path) == []

    def test_unknown_free_space_does_not_block(self, tmp_path, monkeypatch) -> None:
        def broken(_p: Path) -> int:
            raise OSError("no such volume")

        monkeypatch.setattr(drivers, "free_space", broken)
        path, _s, _p = run_download(tmp_path, exe_response(installer_bytes()))
        assert path.exists()

    def test_disk_full_while_writing(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(drivers, "free_space", lambda _p: 10**15)
        real_open = Path.open

        class Full:
            def __init__(self, fh) -> None:
                self.fh = fh

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                self.fh.close()

            def write(self, _b: bytes) -> None:
                raise OSError(28, "No space left on device")

        def fake_open(self, mode="r", *a, **kw):
            fh = real_open(self, mode, *a, **kw)
            return Full(fh) if self.suffix == ".part" else fh

        monkeypatch.setattr(Path, "open", fake_open)
        with pytest.raises(DriverError, match="Przerwano pobieranie"):
            run_download(tmp_path, exe_response(installer_bytes()))
        assert files_in(tmp_path) == []


@pytest.mark.usefixtures("small_min", "plenty_of_space")
def test_stale_target_removed_when_download_fails(tmp_path: Path) -> None:
    name = NV_URL.rsplit("/", 1)[1]
    (tmp_path / name).write_bytes(b"MZ stale")
    with pytest.raises(DriverError):
        run_download(tmp_path, FakeResponse(500))
    assert files_in(tmp_path) == []


# ---------------------------------------------------------------- resolvers

NV_META = drivers.NVIDIA_LOOKUP_URL
NV_FIND = drivers.NVIDIA_PROCESS_FIND_URL
NV_INTL = drivers.NVIDIA_URL_TEMPLATES[1].format(v="621.24")


class TestResolveNvidia:
    def test_source_a(self) -> None:
        session = FakeSession({NV_META: FakeResponse(200, data("nvidia_lookup.json"))})
        rel = drivers.resolve("nvidia", session)
        assert (rel.version, rel.url) == ("621.24", NV_URL)
        assert session.urls() == [NV_META]

    @pytest.mark.parametrize(
        "failure",
        [
            FakeResponse(200, "<html>maintenance</html>"),
            FakeResponse(503),
            OSError("dns"),
            FakeResponse(200, data("nvidia_lookup_notfound.json")),
        ],
    )
    def test_plan_b_with_head_check(self, failure) -> None:
        session = FakeSession(
            {
                NV_META: failure,
                NV_FIND: FakeResponse(200, data("nvidia_processfind.html")),
                NV_URL: FakeResponse(404),
                NV_INTL: FakeResponse(200, headers={"Content-Length": "1038168736"}),
            }
        )
        rel = drivers.resolve("nvidia", session)
        assert (rel.version, rel.url, rel.approx_size) == ("621.24", NV_INTL, 1038168736)
        assert "plan B" in rel.source
        assert [c.method for c in session.calls] == ["GET", "GET", "HEAD", "HEAD"]

    def test_both_sources_fail(self) -> None:
        session = FakeSession(
            {
                NV_META: FakeResponse(500),
                NV_FIND: FakeResponse(200, data("nvidia_processfind.html")),
                NV_URL: FakeResponse(404),
                NV_INTL: FakeResponse(403),
            }
        )
        with pytest.raises(DriverError) as info:
            drivers.resolve("nvidia", session)
        assert "DriverManualLookup" in info.value.message and "processFind" in info.value.message
        assert info.value.vendor_url == drivers.NVIDIA_PAGE
        assert drivers.NVIDIA_PAGE in info.value.user_message()


AMD_PRODUCT = drivers.AMD_PRODUCT_URL
AMD_GENERAL = drivers.AMD_DRIVERS_URL


class TestResolveAmd:
    def test_product_page_then_notes(self) -> None:
        session = FakeSession(
            {
                AMD_PRODUCT: FakeResponse(200, data("amd_product_page.html")),
                AMD_NOTES: FakeResponse(200, data("amd_relnotes.html")),
            }
        )
        rel = drivers.resolve("amd", session)
        assert (rel.version, rel.url, rel.release_date) == ("26.11.2", AMD_URL, "2026-11-18")
        assert rel.notes == ()  # "Recommended" release: no warning
        for call in session.calls:  # www.amd.com tarpits python-requests
            assert call.headers["User-Agent"].startswith("Winstaller/")

    def test_optional_release_is_warned_about(self) -> None:
        page = data("amd_product_page.html").replace(b"WHQL (Recommended)", b"(Optional)")
        session = FakeSession(
            {
                AMD_PRODUCT: FakeResponse(200, page),
                AMD_NOTES: FakeResponse(200, data("amd_relnotes.html")),
            }
        )
        rel = drivers.resolve("amd", session)
        assert rel.notes and "(Optional)" in rel.notes[0]

    def test_general_page_plan_b(self) -> None:
        session = FakeSession(
            {
                AMD_PRODUCT: OSError("tarpit"),
                AMD_GENERAL: FakeResponse(200, data("amd_drivers.html")),
                AMD_NOTES: FakeResponse(200, data("amd_relnotes.html")),
            }
        )
        rel = drivers.resolve("amd", session)
        assert (rel.version, rel.url) == ("26.11.2", AMD_URL)
        assert "plan B" in rel.source

    @pytest.mark.parametrize(
        "notes",
        [
            FakeResponse(404),
            FakeResponse(
                200,
                data("amd_relnotes.html").replace(
                    b"26.11.2 Driver Release", b"26.11.1 Driver Release"
                ),
            ),
            FakeResponse(
                200, data("amd_relnotes.html").replace(b"Systems with RDNA", b"Systems with GCN")
            ),
        ],
    )
    def test_notes_fail_falls_back_to_b_package(self, notes: FakeResponse) -> None:
        session = FakeSession(
            {AMD_PRODUCT: FakeResponse(200, data("amd_product_page.html")), AMD_NOTES: notes}
        )
        rel = drivers.resolve("amd", session)
        assert rel.url == AMD_B_URL
        assert rel.notes and "7000/9000" in rel.notes[0]

    def test_everything_fails(self) -> None:
        session = FakeSession(
            {
                AMD_PRODUCT: FakeResponse(200, "<html></html>"),
                AMD_GENERAL: FakeResponse(200, data("amd_drivers.html")),
                AMD_NOTES: FakeResponse(500),
            }
        )
        with pytest.raises(DriverError) as info:
            drivers.resolve("amd", session)
        assert info.value.vendor_url == drivers.AMD_PAGE
        assert "informacje o wydaniu" in info.value.message

    def test_both_version_sources_fail(self) -> None:
        session = FakeSession({AMD_PRODUCT: FakeResponse(403), AMD_GENERAL: FakeResponse(403)})
        with pytest.raises(DriverError, match="AMD"):
            drivers.resolve("amd", session)
        assert len(session.calls) == 2


INTEL_DSA = drivers.INTEL_DSA_URL
INTEL_PAGE = drivers.INTEL_PAGE_URL


class TestResolveIntel:
    def test_dsa_zip(self) -> None:
        from tests.unit.test_drivers_parsers import dsa_zip

        session = FakeSession({INTEL_DSA: FakeResponse(200, dsa_zip(data("intel_dsa.json")))})
        rel = drivers.resolve("intel", session)
        assert rel.sha1 and rel.size == 904495280 and rel.url == INTEL_URL

    def test_page_plan_b_uses_python_requests_user_agent(self) -> None:
        session = FakeSession(
            {
                INTEL_DSA: FakeResponse(200, b"PK broken"),
                INTEL_PAGE: FakeResponse(200, data("intel_page.html")),
            }
        )
        rel = drivers.resolve("intel", session)
        assert rel.sha512 and rel.url == INTEL_URL
        assert session.calls[1].headers["User-Agent"].startswith("python-requests/")
        assert "Winstaller/" in session.calls[1].headers["User-Agent"]

    def test_both_fail(self) -> None:
        session = FakeSession({INTEL_DSA: FakeResponse(404), INTEL_PAGE: FakeResponse(403)})
        with pytest.raises(DriverError) as info:
            drivers.resolve("intel", session)
        assert info.value.vendor_url == drivers.INTEL_PAGE

    def test_corrupted_dsa_zip_falls_back_to_page(self) -> None:
        from tests.unit.test_drivers_parsers import dsa_zip

        raw = bytearray(dsa_zip(b"[" + b'{"a": 1},' * 2000 + b"{}]"))
        offset = raw.find(b"data/software-configurations.json") + 33
        for i in range(offset + 10, offset + 40):
            raw[i] ^= 0xFF
        session = FakeSession(
            {
                INTEL_DSA: FakeResponse(200, bytes(raw)),
                INTEL_PAGE: FakeResponse(200, data("intel_page.html")),
            }
        )
        assert drivers.resolve("intel", session).sha512


def test_resolve_rechecks_the_final_url(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = drivers.VENDORS["intel"]
    bad = release("intel", url="https://evil.example/gfx.exe")
    monkeypatch.setitem(
        drivers.VENDORS,  # type: ignore[arg-type]
        "intel",
        drivers.DriverVendor(**{**spec.__dict__, "resolve": lambda _s: bad}),
    )
    with pytest.raises(DriverError, match="Niedozwolony") as info:
        drivers.resolve("intel", FakeSession())
    assert info.value.vendor_url == drivers.INTEL_PAGE


def test_unknown_vendor() -> None:
    for vid in ("", None, "matrox", "NVIDIA"):
        with pytest.raises(DriverError, match="Nieznany producent"):
            drivers.vendor(vid)
