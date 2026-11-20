"""Updater: versions, release parsing, check/download with fake sessions, apply on tmp files."""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
import requests

from winstaller.core import updater
from winstaller.core.updater import (
    ReleaseAsset,
    ReleaseInfo,
    UpdateCancelled,
    UpdateError,
    asset_name_for,
    is_newer,
    old_path_for,
    parse_release,
    parse_version,
)

# ------------------------------------------------------------------ fakes


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        json_data: Any = None,
        *,
        json_exc: Exception | None = None,
        chunks: list[bytes] = (),  # type: ignore[assignment]
        fail_after: int | None = None,
        fail_with: BaseException | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._json = json_data
        self._json_exc = json_exc
        self._chunks = list(chunks)
        self._fail_after = fail_after
        self._fail_with = fail_with
        self.headers = headers or {}
        self.closed = False

    def json(self) -> Any:
        if self._json_exc is not None:
            raise self._json_exc
        return self._json

    def iter_content(self, chunk_size: int):
        for n, chunk in enumerate(self._chunks):
            if self._fail_after is not None and n == self._fail_after:
                raise self._fail_with  # type: ignore[misc]
            yield chunk
        if self._fail_after is not None and self._fail_after >= len(self._chunks):
            raise self._fail_with  # type: ignore[misc]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, result: FakeResponse | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


SHA = "a" * 64


def release_json(tag: str = "v2.0.0", **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tag_name": tag,
        "name": f"Winstaller {tag}",
        "body": "Zmiany: wszystko nowe.",
        "html_url": f"https://github.com/ZMASLO/winstaller/releases/tag/{tag}",
        "assets": [
            {
                "name": "winstaller-x64.exe",
                "browser_download_url": "https://example.invalid/winstaller-x64.exe",
                "size": 1000,
                "digest": f"sha256:{SHA}",
            },
            {
                "name": "winstaller-arm64.exe",
                "browser_download_url": "https://example.invalid/winstaller-arm64.exe",
                "size": 2000,
                "digest": None,
            },
        ],
    }
    data.update(extra)
    return data


# --------------------------------------------------------------- versions


class TestVersions:
    @pytest.mark.parametrize(
        ("text", "key"),
        [
            # Key format changed in stage 3: pre-release identifiers are compared
            # one by one as SemVer requires ((0, n, "") numeric, (1, 0, s) text).
            ("1.2.3", (1, 2, 3, 1, ())),
            ("v1.2.3", (1, 2, 3, 1, ())),
            ("V1.2.3", (1, 2, 3, 1, ())),
            (" 1.2.3 \n", (1, 2, 3, 1, ())),
            ("2.0.0-dev", (2, 0, 0, 0, ((1, 0, "dev"),))),
            ("2.0.0-rc.1+build.5", (2, 0, 0, 0, ((1, 0, "rc"), (0, 1, "")))),
            ("1.1.8+abc", (1, 1, 8, 1, ())),
        ],
    )
    def test_parse_version(self, text: str, key: tuple) -> None:
        assert parse_version(text) == key

    @pytest.mark.parametrize(
        "text",
        ["", "v", "1.2", "1.2.3.4", "abc", "1.2.x", "1.2.3-", "x1.2.3", "1.2.3 beta", "-1.2.3"],
    )
    def test_parse_version_rejects_garbage(self, text: str) -> None:
        with pytest.raises(ValueError, match="Nieprawidłowy numer wersji"):
            parse_version(text)

    @pytest.mark.parametrize(
        ("candidate", "current", "newer"),
        [
            ("2.0.0", "1.1.8", True),
            ("v2.0.0", "1.1.8", True),
            ("1.1.10", "1.1.9", True),  # numeric, not lexicographic
            ("10.0.0", "9.9.9", True),
            ("1.1.8", "1.1.8", False),
            ("v1.1.8", "1.1.8", False),
            ("1.1.7", "1.1.8", False),  # never a downgrade
            ("1.0.99", "1.1.0", False),
            ("2.0.0-dev", "2.0.0", False),
            ("2.0.0", "2.0.0-dev", True),
            ("2.0.0-beta", "2.0.0-alpha", True),
            ("2.0.0-dev", "1.1.8", True),
            ("1.1.8+build2", "1.1.8", False),
        ],
    )
    def test_is_newer(self, candidate: str, current: str, newer: bool) -> None:
        assert is_newer(candidate, current) is newer

    def test_numeric_prerelease_identifiers(self) -> None:
        assert is_newer("2.0.0-rc.10", "2.0.0-rc.2")

    def test_is_newer_propagates_value_error(self) -> None:
        with pytest.raises(ValueError):
            is_newer("latest", "1.1.8")

    def test_asset_names(self) -> None:
        assert asset_name_for("x64") == "winstaller-x64.exe"
        assert asset_name_for("arm64") == "winstaller-arm64.exe"

    def test_old_path(self, tmp_path: Path) -> None:
        assert old_path_for(tmp_path / "winstaller.exe") == tmp_path / "winstaller.exe.old"


# --------------------------------------------------------------- parsing


class TestParseRelease:
    def test_full_release(self) -> None:
        info = parse_release(release_json())
        assert info.tag == "v2.0.0" and info.version == "2.0.0"
        assert info.name == "Winstaller v2.0.0"
        assert info.notes == "Zmiany: wszystko nowe."
        assert info.html_url.endswith("/v2.0.0")
        assert info.assets == (
            ReleaseAsset(
                "winstaller-x64.exe", "https://example.invalid/winstaller-x64.exe", 1000, SHA
            ),
            ReleaseAsset(
                "winstaller-arm64.exe", "https://example.invalid/winstaller-arm64.exe", 2000, None
            ),
        )

    def test_minimal_release(self) -> None:
        info = parse_release({"tag_name": "1.2.3"})
        assert info == ReleaseInfo("1.2.3", "1.2.3", "1.2.3", "", "", ())

    @pytest.mark.parametrize("data", [{}, {"tag_name": None}, {"tag_name": 200}])
    def test_missing_or_non_string_tag(self, data: dict) -> None:
        with pytest.raises(UpdateError, match="nie zawiera numeru wersji"):
            parse_release(data)

    def test_invalid_tag(self) -> None:
        with pytest.raises(UpdateError, match="Nieprawidłowy tag wydania: nightly"):
            parse_release({"tag_name": "nightly"})

    def test_asset_filtering_and_defaults(self) -> None:
        info = parse_release(
            {
                "tag_name": "v1.0.0",
                "body": None,
                "name": "",
                "assets": [
                    "not a dict",
                    {"name": "no-url.exe", "size": 5},
                    {"name": "x.exe", "browser_download_url": "u", "size": None},
                    {"browser_download_url": "v"},
                ],
            }
        )
        assert info.name == "v1.0.0" and info.notes == ""
        assert info.assets == (ReleaseAsset("x.exe", "u", 0, None), ReleaseAsset("", "v", 0, None))

    @pytest.mark.parametrize("assets", [None, []])
    def test_no_assets(self, assets) -> None:
        assert parse_release({"tag_name": "v1.0.0", "assets": assets}).assets == ()

    @pytest.mark.parametrize(
        ("digest", "expected"),
        [
            (f"sha256:{'AB' * 32}", "ab" * 32),
            (f"SHA256: {'0f' * 32} ", "0f" * 32),
            (f"sha512:{'a' * 128}", None),
            ("sha256:xyz", None),
            (f"sha256:{'a' * 63}", None),
            (12345, None),
            ("", None),
        ],
    )
    def test_digest_parsing(self, digest: Any, expected: str | None) -> None:
        asset = {"name": "a", "browser_download_url": "u", "size": 1, "digest": digest}
        assert parse_release({"tag_name": "v1.0.0", "assets": [asset]}).assets[0].sha256 == expected

    def test_bad_size_is_update_error(self) -> None:
        asset = {"name": "a", "browser_download_url": "u", "size": "big"}
        with pytest.raises(UpdateError):
            parse_release({"tag_name": "v1.0.0", "assets": [asset]})

    def test_asset_named_is_case_insensitive(self) -> None:
        info = parse_release(release_json())
        assert info.asset_named("WINSTALLER-X64.EXE") is info.assets[0]
        assert info.asset_named("winstaller-x86.exe") is None


# ------------------------------------------------------------------ check


class TestCheck:
    def test_update_available_for_x64(self) -> None:
        session = FakeSession(FakeResponse(200, release_json("v2.0.0")))
        result = updater.check(session, current="1.1.8", arch="x64")
        assert result.update_available
        assert (result.current, result.latest) == ("1.1.8", "2.0.0")
        assert result.asset is not None and result.asset.name == "winstaller-x64.exe"
        assert result.release.tag == "v2.0.0"
        url, kwargs = session.calls[0]
        assert url == updater.API_LATEST_URL
        assert kwargs["timeout"] == updater.CHECK_TIMEOUT
        assert kwargs["headers"]["Accept"] == "application/vnd.github+json"

    def test_arm64_asset(self) -> None:
        result = updater.check(
            FakeSession(FakeResponse(200, release_json())), current="1.1.8", arch="arm64"
        )
        assert result.asset is not None and result.asset.name == "winstaller-arm64.exe"
        assert result.asset.size == 2000

    def test_default_arch_from_platform(self, monkeypatch) -> None:
        monkeypatch.setenv("WINSTALLER_ARCH", "arm64")
        result = updater.check(FakeSession(FakeResponse(200, release_json())), current="1.1.8")
        assert result.asset is not None and result.asset.name == "winstaller-arm64.exe"

    @pytest.mark.parametrize("tag", ["v1.1.8", "v1.1.7", "v1.0.0", "1.1.8"])
    def test_same_or_older_is_not_an_update(self, tag: str) -> None:
        result = updater.check(
            FakeSession(FakeResponse(200, release_json(tag))), current="1.1.8", arch="x64"
        )
        assert not result.update_available

    def test_default_current_version(self) -> None:
        from winstaller.core.version import get_version

        result = updater.check(FakeSession(FakeResponse(200, release_json("v99.0.0"))), arch="x64")
        assert result.current == get_version() and result.update_available

    def test_missing_asset_for_arch(self) -> None:
        data = release_json(assets=[release_json()["assets"][0]])
        result = updater.check(FakeSession(FakeResponse(200, data)), current="1.1.8", arch="arm64")
        assert result.update_available and result.asset is None

    def test_custom_url(self) -> None:
        session = FakeSession(FakeResponse(200, release_json()))
        updater.check(session, current="1.1.8", arch="x64", url="https://example.invalid/r")
        assert session.calls[0][0] == "https://example.invalid/r"

    @pytest.mark.parametrize(
        ("status", "match"),
        [
            (404, "Nie znaleziono żadnego opublikowanego wydania"),
            (403, "limit zapytań"),
            (500, "HTTP 500"),
            (502, "HTTP 502"),
            (301, "HTTP 301"),
        ],
    )
    def test_http_errors(self, status: int, match: str) -> None:
        with pytest.raises(UpdateError, match=match):
            updater.check(FakeSession(FakeResponse(status, {})), current="1.1.8", arch="x64")

    @pytest.mark.parametrize(
        "response",
        [
            FakeResponse(200, json_exc=ValueError("bad json")),
            FakeResponse(200, json_exc=requests.JSONDecodeError("x", "doc", 0)),
            FakeResponse(200, ["not", "a", "dict"]),
            FakeResponse(200, None),
        ],
    )
    def test_bad_json(self, response: FakeResponse) -> None:
        with pytest.raises(UpdateError, match="Nieprawidłowa odpowiedź GitHub"):
            updater.check(FakeSession(response), current="1.1.8", arch="x64")

    def test_bad_tag_in_response(self) -> None:
        with pytest.raises(UpdateError, match="Nieprawidłowy tag"):
            updater.check(
                FakeSession(FakeResponse(200, {"tag_name": "latest"})), current="1.1.8", arch="x64"
            )

    @pytest.mark.parametrize(
        "exc",
        [
            requests.ConnectionError("offline"),
            requests.Timeout("slow"),
            requests.TooManyRedirects(),
        ],
    )
    def test_network_errors(self, exc: Exception) -> None:
        with pytest.raises(UpdateError, match="Nie udało się połączyć z GitHub"):
            updater.check(FakeSession(exc), current="1.1.8", arch="x64")

    def test_invalid_current_version(self) -> None:
        with pytest.raises(UpdateError, match="Nieprawidłowy numer wersji"):
            updater.check(FakeSession(FakeResponse(200, release_json())), current="dev", arch="x64")

    def test_default_session_has_user_agent(self, monkeypatch) -> None:
        created: list[Any] = []

        class Session(FakeSession):
            def __init__(self) -> None:
                super().__init__(FakeResponse(200, release_json()))
                self.headers: dict[str, str] = {}
                created.append(self)

        monkeypatch.setattr(requests, "Session", Session)
        updater.check(current="1.1.8", arch="x64")
        assert created[0].headers["User-Agent"] == updater.USER_AGENT


# --------------------------------------------------------------- download


def _asset(data: bytes, *, size: int | None = None, sha: str | None | bool = True) -> ReleaseAsset:
    digest = hashlib.sha256(data).hexdigest() if sha is True else (sha or None)
    return ReleaseAsset(
        "winstaller-x64.exe",
        "https://example.invalid/w.exe",
        len(data) if size is None else size,
        digest,
    )


class TestDownload:
    def test_success(self, tmp_path: Path) -> None:
        data = b"ab" + b"cd" + b"ef"
        response = FakeResponse(200, chunks=[b"ab", b"", b"cd", b"ef"])
        session = FakeSession(response)
        progress: list[tuple[int, int | None]] = []
        dest = tmp_path / "new.exe"
        result = updater.download(
            _asset(data), dest, lambda r, t: progress.append((r, t)), session=session
        )
        assert result == dest and dest.read_bytes() == data
        assert progress == [(2, 6), (4, 6), (6, 6)]
        assert not (tmp_path / "new.exe.part").exists()
        assert response.closed
        url, kwargs = session.calls[0]
        assert url == "https://example.invalid/w.exe"
        assert kwargs == {"stream": True, "timeout": updater.DOWNLOAD_TIMEOUT}

    def test_total_from_content_length_or_none(self, tmp_path: Path) -> None:
        progress: list[tuple[int, int | None]] = []
        asset = ReleaseAsset("a", "u", 0, None)
        updater.download(
            asset,
            tmp_path / "a",
            lambda r, t: progress.append((r, t)),
            session=FakeSession(FakeResponse(200, chunks=[b"x"], headers={"content-length": "1"})),
        )
        updater.download(
            asset,
            tmp_path / "b",
            lambda r, t: progress.append((r, t)),
            session=FakeSession(FakeResponse(200, chunks=[b"y"])),
        )
        assert progress == [(1, 1), (1, None)]

    def test_without_digest_skips_hash_check(self, tmp_path: Path) -> None:
        dest = tmp_path / "n.exe"
        updater.download(
            _asset(b"xyz", sha=None), dest, session=FakeSession(FakeResponse(200, chunks=[b"xyz"]))
        )
        assert dest.read_bytes() == b"xyz"

    def test_overwrites_existing_destination(self, tmp_path: Path) -> None:
        dest = tmp_path / "n.exe"
        dest.write_bytes(b"old")
        (tmp_path / "n.exe.part").write_bytes(b"stale partial")
        updater.download(
            _asset(b"new"), dest, session=FakeSession(FakeResponse(200, chunks=[b"new"]))
        )
        assert dest.read_bytes() == b"new"
        assert not (tmp_path / "n.exe.part").exists()

    def _assert_failed_cleanly(self, tmp_path: Path, dest: Path, previous: bytes | None) -> None:
        assert not dest.with_name(dest.name + ".part").exists()
        if previous is None:
            assert not dest.exists()
        else:
            assert dest.read_bytes() == previous

    @pytest.mark.parametrize("previous", [None, b"keep me"])
    def test_wrong_size(self, tmp_path: Path, previous: bytes | None) -> None:
        dest = tmp_path / "n.exe"
        if previous:
            dest.write_bytes(previous)
        with pytest.raises(UpdateError, match=r"nieprawidłowy rozmiar \(3 z 10 bajtów\)"):
            updater.download(
                _asset(b"abc", size=10, sha=None),
                dest,
                session=FakeSession(FakeResponse(200, chunks=[b"abc"])),
            )
        self._assert_failed_cleanly(tmp_path, dest, previous)

    @pytest.mark.parametrize("previous", [None, b"keep me"])
    def test_wrong_checksum(self, tmp_path: Path, previous: bytes | None) -> None:
        dest = tmp_path / "n.exe"
        if previous:
            dest.write_bytes(previous)
        with pytest.raises(UpdateError, match="SHA256"):
            updater.download(
                _asset(b"abc", sha="0" * 64),
                dest,
                session=FakeSession(FakeResponse(200, chunks=[b"abc"])),
            )
        self._assert_failed_cleanly(tmp_path, dest, previous)

    def test_cancel(self, tmp_path: Path) -> None:
        cancel = threading.Event()
        dest = tmp_path / "n.exe"
        data = [b"a" * 10] * 5
        with pytest.raises(UpdateCancelled) as info:
            updater.download(
                _asset(b"".join(data)),
                dest,
                lambda r, t: cancel.set(),
                cancel,
                session=FakeSession(FakeResponse(200, chunks=data)),
            )
        assert isinstance(info.value, UpdateError)
        self._assert_failed_cleanly(tmp_path, dest, None)

    def test_cancel_before_start(self, tmp_path: Path) -> None:
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(UpdateCancelled):
            updater.download(
                _asset(b"abc"),
                tmp_path / "n.exe",
                cancel_event=cancel,
                session=FakeSession(FakeResponse(200, chunks=[b"abc"])),
            )
        assert list(tmp_path.iterdir()) == []

    @pytest.mark.parametrize(
        "exc",
        [
            requests.exceptions.ChunkedEncodingError("reset"),
            requests.ConnectionError("lost"),
            requests.ReadTimeout("slow"),
        ],
    )
    def test_network_error_mid_stream(self, tmp_path: Path, exc: Exception) -> None:
        dest = tmp_path / "n.exe"
        response = FakeResponse(200, chunks=[b"a", b"b", b"c"], fail_after=2, fail_with=exc)
        with pytest.raises(UpdateError, match="Przerwano pobieranie aktualizacji"):
            updater.download(_asset(b"abc"), dest, session=FakeSession(response))
        self._assert_failed_cleanly(tmp_path, dest, None)
        assert response.closed

    @pytest.mark.parametrize("exc", [OSError(28, "No space left"), KeyboardInterrupt()])
    def test_any_exception_mid_stream_removes_partial_file(
        self, tmp_path: Path, exc: BaseException
    ) -> None:
        dest = tmp_path / "n.exe"
        response = FakeResponse(200, chunks=[b"a", b"b"], fail_after=1, fail_with=exc)
        with pytest.raises(type(exc)):
            updater.download(_asset(b"ab"), dest, session=FakeSession(response))
        self._assert_failed_cleanly(tmp_path, dest, None)

    def test_progress_callback_exception_removes_partial_file(self, tmp_path: Path) -> None:
        def broken(received: int, total: int | None) -> None:
            raise RuntimeError("ui gone")

        dest = tmp_path / "n.exe"
        with pytest.raises(RuntimeError):
            updater.download(
                _asset(b"ab"), dest, broken, session=FakeSession(FakeResponse(200, chunks=[b"ab"]))
            )
        self._assert_failed_cleanly(tmp_path, dest, None)

    @pytest.mark.parametrize("status", [404, 403, 500])
    def test_http_error(self, tmp_path: Path, status: int) -> None:
        response = FakeResponse(status)
        with pytest.raises(UpdateError, match=f"HTTP {status}"):
            updater.download(_asset(b"x"), tmp_path / "n.exe", session=FakeSession(response))
        assert response.closed
        assert list(tmp_path.iterdir()) == []

    def test_connection_error(self, tmp_path: Path) -> None:
        with pytest.raises(UpdateError, match="Nie udało się pobrać aktualizacji"):
            updater.download(
                _asset(b"x"), tmp_path / "n.exe", session=FakeSession(requests.ConnectionError("x"))
            )
        assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------------ apply


@pytest.fixture
def exe_pair(tmp_path: Path) -> tuple[Path, Path]:
    current = tmp_path / "app" / "winstaller.exe"
    current.parent.mkdir()
    current.write_bytes(b"ORIGINAL")
    new = tmp_path / "download" / "winstaller_new.exe"
    new.parent.mkdir()
    new.write_bytes(b"NEW VERSION")
    return current, new


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, list[str]]]:
    calls: list[tuple[Path, list[str]]] = []
    monkeypatch.setattr(updater, "_spawn", lambda exe, args: calls.append((exe, args)))
    return calls


def _fail_replace(monkeypatch: pytest.MonkeyPatch, predicate) -> None:
    real = os.replace

    def replace(src, dst):
        if predicate(Path(src), Path(dst)):
            raise PermissionError(13, "Plik jest używany przez inny proces", str(src))
        return real(src, dst)

    monkeypatch.setattr(os, "replace", replace)


class TestApply:
    def test_success(self, exe_pair, spawned) -> None:
        current, new = exe_pair
        assert updater.apply(new, current_exe=current, args=["--after-update"]) is True
        assert current.read_bytes() == b"NEW VERSION"
        assert old_path_for(current).read_bytes() == b"ORIGINAL"
        assert not new.exists()
        assert spawned == [(current, ["--after-update"])]

    def test_without_launch(self, exe_pair, spawned) -> None:
        current, new = exe_pair
        assert updater.apply(new, current_exe=current, launch=False) is True
        assert spawned == [] and current.read_bytes() == b"NEW VERSION"

    def test_default_args_empty(self, exe_pair, spawned) -> None:
        current, new = exe_pair
        updater.apply(str(new), current_exe=str(current))
        assert spawned == [(current, [])]

    def test_stale_old_file_is_replaced(self, exe_pair, spawned) -> None:
        current, new = exe_pair
        old_path_for(current).write_bytes(b"VERY OLD")
        updater.apply(new, current_exe=current)
        assert old_path_for(current).read_bytes() == b"ORIGINAL"

    def test_missing_new_file(self, exe_pair, spawned, tmp_path: Path) -> None:
        current, _ = exe_pair
        with pytest.raises(UpdateError, match="Brak pobranego pliku aktualizacji"):
            updater.apply(tmp_path / "nope.exe", current_exe=current)
        assert current.read_bytes() == b"ORIGINAL" and spawned == []

    def test_new_path_is_a_directory(self, exe_pair, spawned, tmp_path: Path) -> None:
        current, _ = exe_pair
        with pytest.raises(UpdateError):
            updater.apply(tmp_path, current_exe=current)
        assert current.read_bytes() == b"ORIGINAL"

    def test_old_file_cannot_be_removed(self, exe_pair, spawned) -> None:
        current, new = exe_pair
        old = old_path_for(current)
        old.mkdir()  # a directory cannot be unlinked
        with pytest.raises(UpdateError, match="Nie można usunąć poprzedniej kopii"):
            updater.apply(new, current_exe=current)
        assert current.read_bytes() == b"ORIGINAL" and new.read_bytes() == b"NEW VERSION"

    def test_rename_of_running_exe_fails(self, exe_pair, spawned, monkeypatch) -> None:
        current, new = exe_pair
        _fail_replace(monkeypatch, lambda s, d: s == current)
        with pytest.raises(UpdateError, match="Nie można zmienić nazwy działającego programu"):
            updater.apply(new, current_exe=current)
        assert current.read_bytes() == b"ORIGINAL" and new.read_bytes() == b"NEW VERSION"
        assert spawned == []

    def test_move_of_new_file_fails_rolls_back(self, exe_pair, spawned, monkeypatch) -> None:
        current, new = exe_pair
        _fail_replace(monkeypatch, lambda s, d: s == new)
        with pytest.raises(UpdateError, match="Nie można podmienić pliku programu"):
            updater.apply(new, current_exe=current)
        assert current.read_bytes() == b"ORIGINAL"
        assert not old_path_for(current).exists()
        assert new.read_bytes() == b"NEW VERSION"
        assert spawned == []

    def test_launch_failure_rolls_back(self, exe_pair, monkeypatch) -> None:
        current, new = exe_pair

        def broken_spawn(exe, args):
            raise OSError(193, "%1 nie jest prawidłową aplikacją Win32")

        monkeypatch.setattr(updater, "_spawn", broken_spawn)
        with pytest.raises(UpdateError, match="Nie udało się uruchomić nowej wersji"):
            updater.apply(new, current_exe=current)
        assert current.read_bytes() == b"ORIGINAL"
        assert not old_path_for(current).exists()
        assert new.read_bytes() == b"NEW VERSION"

    def test_rollback_restores_original_even_if_new_file_is_locked(
        self, exe_pair, monkeypatch
    ) -> None:
        current, new = exe_pair

        def broken_spawn(exe, args):
            raise OSError(5, "Odmowa dostępu")

        monkeypatch.setattr(updater, "_spawn", broken_spawn)
        _fail_replace(monkeypatch, lambda s, d: s == current and d == new)
        with pytest.raises(UpdateError):
            updater.apply(new, current_exe=current)
        assert current.read_bytes() == b"ORIGINAL"

    def test_dev_mode_requires_current_exe(self, exe_pair, spawned, monkeypatch) -> None:
        monkeypatch.delattr(sys, "frozen", raising=False)
        _, new = exe_pair
        with pytest.raises(UpdateError, match="tylko w zbudowanej wersji"):
            updater.apply(new)
        assert new.exists() and spawned == []

    def test_frozen_uses_sys_executable(self, exe_pair, spawned, monkeypatch) -> None:
        current, new = exe_pair
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(current))
        assert updater.apply(new)
        assert current.read_bytes() == b"NEW VERSION"

    def test_spawn_environment_and_flags(self, monkeypatch, tmp_path: Path) -> None:
        calls: list[tuple[list[str], dict]] = []

        def fake_popen(args, **kwargs):
            calls.append((args, kwargs))

        monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)
        monkeypatch.delenv("PYINSTALLER_RESET_ENVIRONMENT", raising=False)
        updater._spawn(tmp_path / "w.exe", ["--x"])
        ((args, kwargs),) = calls
        assert args == [str(tmp_path / "w.exe"), "--x"]
        assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
        assert kwargs["creationflags"] == 0x00000008 | 0x00000200
        assert kwargs["close_fds"] is True
        assert "PYINSTALLER_RESET_ENVIRONMENT" not in os.environ


def _cross_device_error(src: Path) -> OSError:
    # What os.replace raises on Windows across volumes: [WinError 17] ERROR_NOT_SAME_DEVICE.
    return OSError(0, "Nie można przenieść pliku na inną stację dysków", str(src), 17)


def _cross_volume(monkeypatch: pytest.MonkeyPatch, download_dir: Path) -> list[tuple[Path, Path]]:
    """Make every os.replace out of ``download_dir`` fail like a move to another volume."""
    real = os.replace
    calls: list[tuple[Path, Path]] = []

    def replace(src, dst):
        s, d = Path(src), Path(dst)
        calls.append((s, d))
        if s.parent == download_dir and d.parent != download_dir:
            raise _cross_device_error(s)
        return real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    return calls


class TestApplyStage3:
    def test_error_helper_is_cross_device(self, tmp_path: Path) -> None:
        assert updater._is_cross_device(_cross_device_error(tmp_path))
        assert not updater._is_cross_device(PermissionError(13, "x"))

    def test_cross_volume_download_is_copied_next_to_exe(
        self, exe_pair, spawned, monkeypatch
    ) -> None:
        current, new = exe_pair
        calls = _cross_volume(monkeypatch, new.parent)
        assert updater.apply(new, current_exe=current, args=["--x"]) is True
        staged = updater.staged_path_for(current)
        assert current.read_bytes() == b"NEW VERSION"
        assert old_path_for(current).read_bytes() == b"ORIGINAL"
        assert not staged.exists() and not new.exists()
        # The final swap is a same-directory rename of the staged copy.
        assert (staged, current) in calls
        assert spawned == [(current, ["--x"])]

    def test_cross_volume_copy_failure_keeps_everything(
        self, exe_pair, spawned, monkeypatch
    ) -> None:
        current, new = exe_pair
        _cross_volume(monkeypatch, new.parent)

        def broken_copy(src, dst, **kwargs):
            Path(dst).write_bytes(b"PARTIAL")
            raise OSError(28, "Brak miejsca na dysku")

        monkeypatch.setattr(updater.shutil, "copyfile", broken_copy)
        with pytest.raises(UpdateError, match="Nie można podmienić pliku programu"):
            updater.apply(new, current_exe=current)
        assert current.read_bytes() == b"ORIGINAL"
        assert new.read_bytes() == b"NEW VERSION"
        assert not updater.staged_path_for(current).exists()
        assert not old_path_for(current).exists() and spawned == []

    def test_cross_volume_launch_failure_restores_original(self, exe_pair, monkeypatch) -> None:
        current, new = exe_pair
        _cross_volume(monkeypatch, new.parent)
        monkeypatch.setattr(updater, "_spawn", lambda exe, args: (_ for _ in ()).throw(OSError(5)))
        with pytest.raises(UpdateError, match="Nie udało się uruchomić nowej wersji"):
            updater.apply(new, current_exe=current)
        assert current.read_bytes() == b"ORIGINAL"
        assert new.read_bytes() == b"NEW VERSION"
        assert not old_path_for(current).exists()

    def test_rename_of_running_exe_fails_after_cross_volume_copy(
        self, exe_pair, spawned, monkeypatch
    ) -> None:
        current, new = exe_pair
        _cross_volume(monkeypatch, new.parent)
        real = os.replace

        def replace(src, dst):
            if Path(src) == current:
                raise PermissionError(13, "Odmowa dostępu", str(src))
            return real(src, dst)

        monkeypatch.setattr(os, "replace", replace)
        with pytest.raises(UpdateError, match="Nie można zmienić nazwy"):
            updater.apply(new, current_exe=current)
        assert current.read_bytes() == b"ORIGINAL"
        assert new.read_bytes() == b"NEW VERSION"
        assert not updater.staged_path_for(current).exists()

    def test_swap_failure_restores_original_and_download(
        self, exe_pair, spawned, monkeypatch
    ) -> None:
        current, new = exe_pair
        staged = updater.staged_path_for(current)
        _fail_replace(monkeypatch, lambda s, d: s == staged and d == current)
        with pytest.raises(UpdateError, match="Nie można podmienić pliku programu"):
            updater.apply(new, current_exe=current)
        assert current.read_bytes() == b"ORIGINAL"
        assert new.read_bytes() == b"NEW VERSION"
        assert not staged.exists() and not old_path_for(current).exists()

    def test_rollback_falls_back_to_copy_when_rename_back_fails(
        self, exe_pair, monkeypatch
    ) -> None:
        current, new = exe_pair
        old = old_path_for(current)
        monkeypatch.setattr(updater, "_spawn", lambda exe, args: (_ for _ in ()).throw(OSError(5)))
        _fail_replace(monkeypatch, lambda s, d: s == old and d == current)
        with pytest.raises(UpdateError, match="Nie udało się uruchomić nowej wersji"):
            updater.apply(new, current_exe=current)
        assert current.read_bytes() == b"ORIGINAL"

    def test_rollback_failure_is_update_error_and_keeps_old(self, exe_pair, monkeypatch) -> None:
        current, new = exe_pair
        old = old_path_for(current)
        monkeypatch.setattr(updater, "_spawn", lambda exe, args: (_ for _ in ()).throw(OSError(5)))
        _fail_replace(monkeypatch, lambda s, d: s == old)
        real_copy = updater.shutil.copyfile

        def copyfile(src, dst, **kwargs):
            if Path(src) == old:
                raise PermissionError(13, "Plik jest używany", str(dst))
            return real_copy(src, dst, **kwargs)

        monkeypatch.setattr(updater.shutil, "copyfile", copyfile)
        with pytest.raises(UpdateError, match="Oryginalny plik jest zachowany jako") as exc:
            updater.apply(new, current_exe=current)
        assert str(old) in str(exc.value)
        assert old.read_bytes() == b"ORIGINAL"  # never lost

    def test_stale_staged_file_is_replaced(self, exe_pair, spawned) -> None:
        current, new = exe_pair
        updater.staged_path_for(current).write_bytes(b"STALE")
        updater.apply(new, current_exe=current)
        assert current.read_bytes() == b"NEW VERSION"
        assert not updater.staged_path_for(current).exists()


class TestReleaseStage3:
    @pytest.mark.parametrize("size", ["big", "1.5", -1, True, [1], {"n": 1}])
    def test_bad_sizes(self, size: Any) -> None:
        asset = {"name": "a", "browser_download_url": "u", "size": size}
        with pytest.raises(UpdateError, match="Nieprawidłowy rozmiar"):
            parse_release({"tag_name": "v1.0.0", "assets": [asset]})

    @pytest.mark.parametrize("size", [None, "", 0, "12"])
    def test_tolerated_sizes(self, size: Any) -> None:
        asset = {"name": "a", "browser_download_url": "u", "size": size}
        expected = 12 if size == "12" else 0
        assert parse_release({"tag_name": "v1.0.0", "assets": [asset]}).assets[0].size == expected

    def test_assets_not_a_list(self) -> None:
        with pytest.raises(UpdateError):
            parse_release({"tag_name": "v1.0.0", "assets": {"a": 1}})

    def test_bad_size_from_check_is_update_error(self) -> None:
        data = release_json()
        data["assets"][0]["size"] = "big"
        with pytest.raises(UpdateError):
            updater.check(FakeSession(FakeResponse(200, data)), current="1.0.0", arch="x64")

    @pytest.mark.parametrize("tag", ["V2.0.0", "v2.0.0", "2.0.0"])
    def test_v_prefix_any_case(self, tag: str) -> None:
        assert parse_release({"tag_name": tag}).version == "2.0.0"
        assert is_newer(tag, "1.1.8")

    @pytest.mark.parametrize(
        ("candidate", "current"),
        [
            ("2.0.0-rc.10", "2.0.0-rc.2"),
            ("2.0.0-rc.1", "2.0.0-rc"),
            ("2.0.0-alpha.beta", "2.0.0-alpha.1"),
            ("2.0.0-beta.2", "2.0.0-beta"),
            ("2.0.0", "2.0.0-rc.99"),
        ],
    )
    def test_semver_precedence(self, candidate: str, current: str) -> None:
        assert is_newer(candidate, current) and not is_newer(current, candidate)

    def test_empty_prerelease_identifier_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_version("2.0.0-rc..1")


# --------------------------------------------------------------- cleanup


class TestCleanupOld:
    def test_removes_leftover(self, tmp_path: Path) -> None:
        exe = tmp_path / "w.exe"
        old_path_for(exe).write_bytes(b"x")
        assert updater.cleanup_old(exe) is True
        assert not old_path_for(exe).exists()

    def test_nothing_to_do(self, tmp_path: Path) -> None:
        assert updater.cleanup_old(tmp_path / "w.exe") is True

    def test_dev_mode_does_nothing(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.delattr(sys, "frozen", raising=False)
        leftover = Path(sys.executable + ".old")
        assert not leftover.exists()
        assert updater.cleanup_old() is True

    def test_frozen_uses_sys_executable(self, monkeypatch, tmp_path: Path) -> None:
        exe = tmp_path / "w.exe"
        old_path_for(exe).write_bytes(b"x")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", str(exe))
        assert updater.cleanup_old() is True
        assert not old_path_for(exe).exists()

    def test_locked_leftover_without_wait(self, tmp_path: Path) -> None:
        exe = tmp_path / "w.exe"
        old_path_for(exe).mkdir()  # unlink() of a directory always fails
        assert updater.cleanup_old(exe) is False

    def test_retries_until_deadline(self, tmp_path: Path, monkeypatch) -> None:
        exe = tmp_path / "w.exe"
        old_path_for(exe).mkdir()
        sleeps: list[float] = []
        monkeypatch.setattr(updater.time, "sleep", lambda s: sleeps.append(s))
        clock = iter([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
        monkeypatch.setattr(updater.time, "monotonic", lambda: next(clock))
        assert updater.cleanup_old(exe, wait=0.35, interval=0.1) is False
        assert sleeps == [0.1, 0.1, 0.1]

    def test_succeeds_after_previous_process_exits(self, tmp_path: Path, monkeypatch) -> None:
        exe = tmp_path / "w.exe"
        old = old_path_for(exe)
        old.write_bytes(b"x")
        real_unlink = Path.unlink
        attempts = {"n": 0}

        def flaky_unlink(self, missing_ok=False):
            if self == old and attempts["n"] < 2:
                attempts["n"] += 1
                raise PermissionError(32, "used by another process")
            return real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", flaky_unlink)
        assert updater.cleanup_old(exe, wait=5, interval=0.01) is True
        assert attempts["n"] == 2 and not old.exists()
