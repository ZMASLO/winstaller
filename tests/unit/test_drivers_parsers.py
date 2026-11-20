"""GPU driver parsers on real vendor answers (2026-11-20) and on mutations of them.

Contract under test (docs/ARCHITECTURE.md): every ``parse_*`` is pure and turns any
unexpected input into :class:`DriverError` - never ``KeyError``/``ValueError``/...
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import random
import re
import zipfile
from collections.abc import Callable
from typing import Any

import pytest

from tests.drivers_support import AMD_URL, INTEL_URL, NV_URL, data, text
from winstaller.core import drivers
from winstaller.core.drivers import DriverError

# ----------------------------------------------------------------- helpers


def test_version_key_is_numeric() -> None:
    assert drivers.version_key("621.24") > drivers.version_key("621.9")
    assert drivers.version_key("26.10.1") > drivers.version_key("26.9.2")
    assert drivers.version_key(" 32.0.101.9175 ") == (32, 0, 101, 9175)
    for bad in ("", "621", "621.", "a.b", "621.24-beta", "1..2", None, 621.24):
        with pytest.raises(DriverError):
            drivers.version_key(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("1.04 GB", int(1.04 * 1024**3)),
        ("887 MB", 887 * 1024**2),
        ("1,8 GB", int(1.8 * 1024**3)),
        ("512 kb", 512 * 1024),
        ("", None),
        (None, None),
        ("about a gigabyte", None),
        ("0 MB", None),
    ],
)
def test_parse_size_text(label: str | None, expected: int | None) -> None:
    assert drivers.parse_size_text(label) == expected


# ----------------------------------------------------------------- NVIDIA


def nv_json(name: str = "nvidia_lookup.json") -> dict[str, Any]:
    return json.loads(data(name))


def nv_entry(**fields: str) -> dict[str, Any]:
    """A real lookup entry (621.24 Game Ready) with some fields replaced."""
    entry = copy.deepcopy(nv_json()["IDS"][0])
    entry["downloadInfo"].update(fields)
    return entry


def nv_answer(*entries: dict[str, Any], **top: Any) -> str:
    return json.dumps({"Success": str(len(entries)), "IDS": list(entries), **top})


def nv_url(version: str, kind: str = "dch-whql") -> str:
    return (
        f"https://us.download.nvidia.com/Windows/{version}/"
        f"{version}-desktop-win10-win11-64bit-international-{kind}.exe"
    )


class TestNvidiaLookup:
    def test_real_answer(self) -> None:
        rel = drivers.parse_nvidia_lookup(data("nvidia_lookup.json"))
        assert rel.vendor == "nvidia"
        assert rel.version == "621.24"
        assert rel.url == NV_URL
        assert rel.release_date == "2026-11-12"
        assert rel.approx_size == int(1.04 * 1024**3)
        assert rel.size is None and rel.sha1 is None and rel.sha512 is None
        # fields really are URL-encoded in the answer
        assert "GeForce%20Game%20Ready%20Driver" in text("nvidia_lookup.json")

    def test_real_mixed_answer_never_takes_studio(self) -> None:
        raw = nv_json("nvidia_lookup_mixed_grd_studio.json")
        assert any("-nsd-" in e["downloadInfo"]["DownloadURL"] for e in raw["IDS"])
        rel = drivers.parse_nvidia_lookup(json.dumps(raw))
        assert rel.url == NV_URL and "-nsd-" not in rel.url
        # Studio first with the same version: still the Game Ready one
        raw["IDS"].sort(key=lambda e: e["downloadInfo"]["IsCRD"], reverse=True)
        assert drivers.parse_nvidia_lookup(json.dumps(raw)).url == NV_URL

    def test_real_not_found_answer(self) -> None:
        with pytest.raises(DriverError, match="nie znalazł"):
            drivers.parse_nvidia_lookup(data("nvidia_lookup_notfound.json"))

    def test_highest_version_wins_numerically(self) -> None:
        answer = nv_answer(
            nv_entry(Version="621.9", DownloadURL=nv_url("621.9")),
            nv_entry(Version="621.24", DownloadURL=nv_url("621.24")),
            nv_entry(Version="599.100", DownloadURL=nv_url("599.100")),
        )
        assert drivers.parse_nvidia_lookup(answer).version == "621.24"

    @pytest.mark.parametrize(
        "fields",
        [
            {"IsBeta": "1"},
            {"IsCRD": "1"},
            {"Name": "NVIDIA%20Studio%20Driver"},
            {"DownloadURL": nv_url("700.01", "nsd-dch-whql")},
            {"DownloadURL": nv_url("700.01").replace("https://", "http://")},
            {"DownloadURL": "https://download.nvidia.com.evil.example/700.01.exe"},
            {"DownloadURL": "https://drivers.amd.com/700.01.exe"},
            {"IsBeta": ""},
            {"Version": "700.01 beta"},
            {"Version": ""},
        ],
    )
    def test_newer_but_unacceptable_entries_are_ignored(self, fields: dict[str, str]) -> None:
        bad = nv_entry(Version="700.01", DownloadURL=nv_url("700.01"))
        bad["downloadInfo"].update(fields)
        rel = drivers.parse_nvidia_lookup(nv_answer(bad, nv_entry()))
        assert rel.version == "621.24" and rel.url == NV_URL

    def test_url_encoded_url_is_decoded(self) -> None:
        encoded = NV_URL.replace(":", "%3A").replace("/", "%2F")
        assert drivers.parse_nvidia_lookup(nv_answer(nv_entry(DownloadURL=encoded))).url == NV_URL

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "{}",
            "[]",
            "null",
            '"text"',
            "<html><body>Access Denied</body></html>",
            b"\xff\xfe\x00garbage",
            '{"Success": "1", "IDS": {}}',
            '{"Success": "1", "IDS": null}',
            '{"Success": "1", "IDS": []}',
            '{"Success": "1", "IDS": [1, "x", null, [], {"downloadInfo": []}]}',
            '{"Success": "1", "IDS": [{"downloadInfo": {"Version": 621.24}}]}',
            '{"Success": "1", "IDS": [{"downloadInfo": {"IsBeta": 0, "IsCRD": 0}}]}',
            '{"Success": 0}',
        ],
    )
    def test_garbage_raises_driver_error(self, raw: str | bytes) -> None:
        with pytest.raises(DriverError):
            drivers.parse_nvidia_lookup(raw)

    def test_success_zero_as_number(self) -> None:
        with pytest.raises(DriverError, match="nie znalazł"):
            drivers.parse_nvidia_lookup(nv_answer(nv_entry(), Success=0))

    def test_deeply_nested_json_is_a_driver_error(self) -> None:
        with pytest.raises(DriverError):
            drivers.parse_nvidia_lookup('{"IDS": ' + "[" * 100_000)


class TestNvidiaProcessFind:
    def test_real_page(self) -> None:
        listing = drivers.parse_nvidia_process_find(text("nvidia_processfind.html"))
        assert listing == drivers.NvidiaListing("621.24", "2026-11-12")

    def row(self, name: str, version: str, date: str = "November 12, 2026") -> str:
        return (
            '<tr id="driverList"><td class="gridItem"></td>'
            f'<td class="gridItem driverName"><b><a href="#">{name}</a></b></td>'
            f'<td class="gridItem">{version}</td><td class="gridItem" nowrap>{date}</td></tr>'
        )

    def test_studio_rows_and_bad_versions_are_skipped(self) -> None:
        page = "".join(
            [
                self.row("NVIDIA Studio Driver", "700.01"),
                self.row("GeForce Game Ready Driver", "62x.1"),
                self.row("GeForce Game Ready Driver", "621.9"),
                self.row("GeForce Game Ready Driver", "621.24", "Nov 12 2026"),
            ]
        )
        listing = drivers.parse_nvidia_process_find(page)
        assert listing.version == "621.24"
        assert listing.release_date is None  # date cell not in a known format

    @pytest.mark.parametrize("page", ["", "<html></html>", '<tr id="driverList"></tr>', "x" * 999])
    def test_nothing_found(self, page: str) -> None:
        with pytest.raises(DriverError):
            drivers.parse_nvidia_process_find(page)

    def test_url_candidates_only_from_digits_and_dots(self) -> None:
        assert drivers.nvidia_url_candidates("621.24")[0] == NV_URL
        assert all(
            drivers.host_allowed(u, drivers.NVIDIA_DOWNLOAD_HOSTS)
            for u in drivers.nvidia_url_candidates("621.24")
        )
        for bad in ("621.24/../../evil", "621.24@evil.com", "621", "x"):
            with pytest.raises(DriverError):
                drivers.nvidia_url_candidates(bad)


# -------------------------------------------------------------------- AMD

_ARTICLE = re.compile(
    r"<article\b[^>]*driver-download-details[^>]*>.*?</article>", re.IGNORECASE | re.DOTALL
)


def amd_article(
    revision: str,
    *,
    href: str = "https://drivers.amd.com/drivers/amd-software-adrenalin-edition-26.11.2-win11-b.exe",
    size: str = "887 MB",
    date: str = "2026-11-18",
    notes: str = "/en/resources/support-articles/release-notes/RN-RAD-WIN-26-11-2.html",
) -> str:
    """Article in the structure of the real product page (see amd_product_page.html)."""
    return (
        '<article class="container-fluid driver-download-details"><div class="row">'
        f"<div><strong>Revision Number</strong>\n<p>{revision}</p></div>"
        f"<div><strong>File Size</strong>\n<p>{size}</p></div>"
        f"<div><strong>Release Date</strong>\n<p>\n {date}\n</p></div>"
        f'<div><a href="{href}" role="button">Download</a></div>'
        f'<div><p><a href="{notes}" title="x">Release No​tes</a></p></div>'
        "</div></article>"
    )


class TestAmdProductPage:
    def test_real_page(self) -> None:
        info = drivers.parse_amd_product_page(text("amd_product_page.html"))
        assert info.version == "26.11.2"
        assert info.label == "WHQL (Recommended)"
        assert info.release_date == "2026-11-18"
        assert info.package_url == (
            "https://drivers.amd.com/drivers/whql-amd-software-adrenalin-edition-26.11.2-win11-b.exe"
        )
        assert info.notes_url == (
            "https://www.amd.com/en/resources/support-articles/release-notes/RN-RAD-WIN-26-11-2.html"
        )
        assert info.approx_size and 800 * 1024**2 < info.approx_size < 1100 * 1024**2

    def test_real_previous_page_newest_regardless_of_label(self) -> None:
        info = drivers.parse_amd_product_page(text("amd_previous.html"))
        assert (info.version, info.label) == ("26.11.1", "(Optional)")

    def test_every_real_article_variant_parses(self) -> None:
        page = text("amd_previous.html")
        seen: dict[str, str] = {}
        for article in _ARTICLE.findall(page):
            revision = re.search(r"Revision Number\s*</strong>\s*<p>\s*(.*?)\s*</p>", article, re.S)
            assert revision is not None
            if not revision.group(1).startswith("Adrenalin"):
                with pytest.raises(DriverError):  # chipset/other entries are not drivers
                    drivers.parse_amd_product_page(article)
                continue
            info = drivers.parse_amd_product_page(article)
            assert revision.group(1) == f"Adrenalin {info.version} {info.label}"
            assert info.package_url and "minimalsetup" not in info.package_url
            assert info.notes_url and info.notes_url.startswith("https://www.amd.com/")
            seen[info.label] = info.version
        assert {"(Optional)", "WHQL (Optional)", "(WHQL Recommended)", "WHQL (Recommended)"} <= set(
            seen
        )
        assert "(WHQL Optional)" in seen  # a real fifth variant (26.9.1)

    def test_numeric_order_across_articles(self) -> None:
        page = amd_article("Adrenalin 26.9.2 WHQL (Recommended)") + amd_article(
            "Adrenalin 26.10.1 (Optional)"
        )
        assert drivers.parse_amd_product_page(page).version == "26.10.1"

    def test_minimalsetup_and_foreign_links_are_not_packages(self) -> None:
        page = amd_article(
            "Adrenalin 26.11.2 WHQL (Recommended)",
            href="https://drivers.amd.com/drivers/installer/26.20/whql/"
            "amd-software-adrenalin-edition-26.11.2-minimalsetup-261118_web.exe",
        )
        assert drivers.parse_amd_product_page(page).package_url is None
        page = amd_article("Adrenalin 26.11.2", href="https://drivers.amd.com.evil.example/x.exe")
        assert drivers.parse_amd_product_page(page).package_url is None
        page = amd_article("Adrenalin 26.11.2", notes="https://evil.example/RN-RAD-WIN-1.html")
        assert drivers.parse_amd_product_page(page).notes_url is None

    @pytest.mark.parametrize(
        "page",
        ["", "<html></html>", amd_article("Chipset 7.1.1"), amd_article("Adrenalin")],
    )
    def test_nothing_found(self, page: str) -> None:
        with pytest.raises(DriverError):
            drivers.parse_amd_product_page(page)


class TestAmdDriversPage:
    def test_real_page(self) -> None:
        assert drivers.parse_amd_drivers_page(text("amd_drivers.html")) == "26.11.2"

    def test_highest_version_numerically(self) -> None:
        page = (
            "amd-software-adrenalin-edition-26.9.2-minimalsetup-1_web.exe "
            "amd-software-adrenalin-edition-26.11.1-minimalsetup-2_web.exe"
        )
        assert drivers.parse_amd_drivers_page(page) == "26.11.1"

    @pytest.mark.parametrize("page", ["", "minimalsetup", "adrenalin-edition-x-minimalsetup"])
    def test_nothing_found(self, page: str) -> None:
        with pytest.raises(DriverError):
            drivers.parse_amd_drivers_page(page)

    def test_notes_url(self) -> None:
        assert drivers.amd_release_notes_url("26.11.2").endswith("/RN-RAD-WIN-26-11-2.html")
        with pytest.raises(DriverError):
            drivers.amd_release_notes_url("26.11.2/../../x")


def notes_page(*paragraphs: str, title: str = "26.11.2") -> str:
    return (
        f"<html><head><title>AMD Software: Adrenalin Edition {title} Driver Release Notes "
        "</title></head><body><p><b>Last Updated</b>: November 18<sup>th</sup>, 2026.</p>"
        + "".join(f"<p>{p}</p>" for p in paragraphs)
        + "</body></html>"
    )


def link(url: str, label: str = "AMD Software: Adrenalin Edition 26.11.2 Driver") -> str:
    return f'<a href="{url}">{label}</a>'


RDNA = "Systems with RDNA series graphics products: "


class TestAmdReleaseNotes:
    def test_real_page(self) -> None:
        notes = drivers.parse_amd_release_notes(text("amd_relnotes.html"), "26.11.2")
        assert notes == drivers.AmdReleaseNotes(AMD_URL, "26.11.2", "2026-11-18")
        assert notes.url.endswith("-win11-c.exe")

    def test_wrong_version_is_rejected(self) -> None:
        with pytest.raises(DriverError, match="26.11.2"):
            drivers.parse_amd_release_notes(text("amd_relnotes.html"), "26.11.3")

    def test_irregular_file_names_come_from_the_page(self) -> None:
        url = "https://drivers.amd.com/drivers/amd-software-adrenalin-edition-25.6.1-win10-win11-june-rdna.exe"
        page = notes_page(RDNA.replace("RDNA", "RDNA™") + link(url), title="25.6.1")
        assert drivers.parse_amd_release_notes(page, "25.6.1").url == url

    def test_only_the_rdna_paragraph_counts(self) -> None:
        other = "https://drivers.amd.com/drivers/amd-software-adrenalin-edition-26.11.2-vega-polaris.exe"
        page = notes_page(
            "Systems with Vega and Polaris graphics products: " + link(other), RDNA + link(AMD_URL)
        )
        assert drivers.parse_amd_release_notes(page, "26.11.2").url == AMD_URL

    @pytest.mark.parametrize(
        "paragraphs",
        [
            (),
            ("No links here.",),
            (RDNA + link("https://drivers.amd.com/drivers/x-26.11.2-minimalsetup-1_web.exe"),),
            (RDNA + link("https://drivers.amd.com.evil.example/x.exe"),),
            (RDNA + link("http://drivers.amd.com/x.exe"),),
            (RDNA + link("https://drivers.amd.com/notes.pdf"),),
            (RDNA + link(AMD_URL), RDNA + link(AMD_URL.replace("-c.exe", "-d.exe"))),
            ("Systems with GCN graphics: " + link(AMD_URL),),
        ],
    )
    def test_missing_or_ambiguous_link(self, paragraphs: tuple[str, ...]) -> None:
        with pytest.raises(DriverError):
            drivers.parse_amd_release_notes(notes_page(*paragraphs), "26.11.2")

    def test_same_link_twice_is_fine(self) -> None:
        page = notes_page(RDNA + link(AMD_URL), RDNA + link(AMD_URL))
        assert drivers.parse_amd_release_notes(page, "26.11.2").url == AMD_URL

    def test_title_without_version_is_not_checked(self) -> None:
        # Design note: without a version in <title> nothing ties the page to the version.
        page = notes_page(RDNA + link(AMD_URL)).replace("Edition 26.11.2", "Edition")
        assert drivers.parse_amd_release_notes(page, "99.1.1").version is None


# ------------------------------------------------------------------ Intel


def dsa_entries() -> list[dict[str, Any]]:
    return json.loads(data("intel_dsa.json").decode("utf-8-sig"))


def dsa_arc() -> dict[str, Any]:
    return copy.deepcopy(next(e for e in dsa_entries() if e["GroupId"] == "785597"))


def dsa_bytes(entries: list[dict[str, Any]], bom: bool = True) -> bytes:
    return (b"\xef\xbb\xbf" if bom else b"") + json.dumps(entries).encode("utf-8")


def dsa_zip(
    member: bytes,
    name: str = "data/software-configurations.json",
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as archive:
        archive.writestr("data/other.json", b"{}")
        archive.writestr(name, member)
    return buf.getvalue()


def newer(entry: dict[str, Any], version: str = "32.0.101.9999") -> dict[str, Any]:
    entry["Version"] = f"{version} WHQL Certified"
    entry["Files"][0]["Url"] = "https://downloadmirror.intel.com/999/gfx_win_101.9999.exe"
    return entry


class TestIntelDsa:
    expected = drivers.DriverRelease(
        vendor="intel",
        version="32.0.101.9175",
        url=INTEL_URL,
        size=904495280,
        sha1="7d7b25bab60e72db302c34fed041c6ad9b4a4a07",
        release_date="2026-11-04",
        source="Intel DSA (dsadata.intel.com)",
    )

    def test_real_json_with_bom(self) -> None:
        raw = data("intel_dsa.json")
        assert raw.startswith(b"\xef\xbb\xbf")
        assert drivers.parse_intel_dsa(raw) == self.expected
        assert drivers.parse_intel_dsa(raw.decode("utf-8")) == self.expected  # str with BOM

    def test_real_json_inside_zip(self) -> None:
        raw = dsa_zip(data("intel_dsa.json"))
        assert drivers.parse_intel_dsa(raw) == self.expected

    def test_beta_and_arc_pro_are_skipped(self) -> None:
        entries = dsa_entries()
        pro = next(e for e in entries if e["GroupId"] == "741626")
        newer(pro)
        beta = newer(dsa_arc())
        beta["IsBeta"] = True
        missing = newer(dsa_arc(), "32.0.101.9998")
        del missing["IsBeta"]
        entries += [beta, missing]
        assert drivers.parse_intel_dsa(dsa_bytes(entries)) == self.expected

    def test_highest_version_wins(self) -> None:
        entries = [*dsa_entries(), newer(dsa_arc())]
        rel = drivers.parse_intel_dsa(dsa_bytes(entries))
        assert rel.version == "32.0.101.9999" and rel.url.endswith("9999.exe")

    def test_group_id_as_number(self) -> None:
        arc = dsa_arc()
        arc["GroupId"] = 785597
        assert drivers.parse_intel_dsa(dsa_bytes([arc])).version == "32.0.101.9175"

    def test_exe_file_is_chosen_and_bad_files_skipped(self) -> None:
        arc = dsa_arc()
        good = arc["Files"][0]
        arc["Files"] = [
            {**good, "Url": good["Url"].replace(".exe", ".zip")},
            {**good, "Hash": "x" * 40},
            {**good, "Size": "904495280"},
            {**good, "Size": True},
            {**good, "Size": 0},
            {**good, "OperatingSystems": ["windows-10-22h2-32"]},
            {**good, "Url": good["Url"].replace("https://", "http://")},
            {**good, "Url": "https://downloadmirror.intel.com.evil.example/a.exe"},
            "not a dict",
            {**good, "Hash": good["Hash"].upper()},
        ]
        rel = drivers.parse_intel_dsa(dsa_bytes([arc]))
        assert rel.url == INTEL_URL and rel.sha1 == good["Hash"]  # lowercased

    @pytest.mark.parametrize(
        "raw",
        [
            b"",
            b"\xef\xbb\xbf",
            b"{}",
            b"null",
            b"[]",
            b"[1, 2]",
            b"<html>Access denied</html>",
            b"\xff\xfe\x00\x01",
            b"PK\x03\x04garbage",
            b'[{"GroupId": "785597", "IsBeta": false, "Version": "x", "Files": []}]',
            b'[{"GroupId": "785597", "IsBeta": false, "Version": "32.0.1.2", "Files": {}}]',
        ],
    )
    def test_garbage_raises_driver_error(self, raw: bytes) -> None:
        with pytest.raises(DriverError):
            drivers.parse_intel_dsa(raw)

    def test_zip_without_the_json(self) -> None:
        with pytest.raises(DriverError, match="brakuje"):
            drivers.parse_intel_dsa(dsa_zip(b"[]", name="data/other-name.json"))

    def test_huge_member_is_refused(self) -> None:
        raw = dsa_zip(b"[" + b" " * 5000 + b"]")
        with pytest.raises(DriverError, match="podejrzanie duży"):
            drivers.read_intel_dsa_zip(raw, max_member=1000)

    def test_member_lying_about_its_size_is_refused(self) -> None:
        raw = bytearray(dsa_zip(b"[" + b" " * 5000 + b"]"))
        cd = raw.rfind(b"PK\x01\x02")
        while cd >= 0:  # declare 10 bytes in every central directory entry
            raw[cd + 24 : cd + 28] = (10).to_bytes(4, "little")
            cd = raw.rfind(b"PK\x01\x02", 0, cd)
        with pytest.raises(DriverError):
            drivers.read_intel_dsa_zip(bytes(raw), max_member=1000)

    def test_zip_bomb_is_not_inflated(self) -> None:
        bomb = dsa_zip(b"[" + b" " * (8 * 1024 * 1024) + b"]")
        assert len(bomb) < 100_000
        with pytest.raises(DriverError, match="podejrzanie duży"):
            drivers.read_intel_dsa_zip(bomb, max_member=1024 * 1024)

    def test_truncated_zip(self) -> None:
        raw = dsa_zip(data("intel_dsa.json"))
        for cut in (10, len(raw) // 2, len(raw) - 10):
            with pytest.raises(DriverError):
                drivers.parse_intel_dsa(raw[:cut])

    def test_corrupted_deflate_stream(self) -> None:
        raw = bytearray(dsa_zip(b"[" + b'{"a": 1},' * 2000 + b"{}]"))
        start = raw.find(b"PK\x03\x04", raw.find(b"software-configurations") - 60)
        offset = start + 30 + len("data/software-configurations.json")
        for i in range(offset + 10, offset + 40):
            raw[i] ^= 0xFF
        with pytest.raises(DriverError):
            drivers.parse_intel_dsa(bytes(raw))

    def test_invalid_utf8_member_name(self) -> None:
        raw = bytearray(dsa_zip(b"[]"))
        cd = raw.rfind(b"PK\x01\x02")
        raw[cd + 9] |= 0x08  # general purpose flag bit 11: UTF-8 names
        raw[raw.find(b"data/software", cd)] = 0xFF
        with pytest.raises(DriverError):
            drivers.parse_intel_dsa(bytes(raw))

    def test_corrupted_lzma_member(self) -> None:
        raw = bytearray(dsa_zip(b"[" + b'{"a": 1},' * 2000 + b"{}]", compression=zipfile.ZIP_LZMA))
        offset = raw.rfind(b"data/software-configurations.json", 0, raw.rfind(b"PK\x01\x02"))
        for i in range(offset + 60, offset + 100):
            raw[i] ^= 0x5A
        with pytest.raises(DriverError):
            drivers.read_intel_dsa_zip(bytes(raw))

    def test_deeply_nested_json(self) -> None:
        with pytest.raises(DriverError):
            drivers.parse_intel_dsa(b"[" * 100_000)


class TestIntelPage:
    def test_real_page(self) -> None:
        rel = drivers.parse_intel_page(text("intel_page.html"))
        assert rel.version == "32.0.101.9175"
        assert rel.url == INTEL_URL
        assert rel.sha512 == (
            "c1c7a858a5c977df59a3b6352a925100fbf97199b235eb5c3e45a5d037850b363bbc7605206a85bc"
            "5dcaeac0c751a06875f91b9bc13b31e59115d784c9c14111"
        )
        assert rel.sha1 is None and rel.size is None
        assert rel.release_date == "2026-11-04"

    def test_sha_is_lowercased(self) -> None:
        page = text("intel_page.html").replace("c1c7a858a5c977df", "C1C7A858A5C977DF")
        assert drivers.parse_intel_page(page).sha512.startswith("c1c7a858a5c977df")  # type: ignore[union-attr]

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("SHA512:", "SHA256:"),
            ("Version 32.0.101.9175", "Wersja"),
            (
                "https://downloadmirror.intel.com/932449/gfx_win_101.9175.exe",
                "https://evil.example/a.exe",
            ),
            ("cmp-button-list__container", "cmp-other"),
        ],
    )
    def test_mutations_raise(self, old: str, new: str) -> None:
        page = text("intel_page.html")
        assert old in page
        with pytest.raises(DriverError):
            drivers.parse_intel_page(page.replace(old, new))


# ------------------------------------------------------------------- fuzz

PARSERS: list[tuple[str, Callable[[Any], object], str, bool]] = [
    ("nvidia_lookup.json", drivers.parse_nvidia_lookup, "nvidia_lookup.json", True),
    ("nvidia_processfind", drivers.parse_nvidia_process_find, "nvidia_processfind.html", False),
    ("amd_product", drivers.parse_amd_product_page, "amd_product_page.html", False),
    ("amd_drivers", drivers.parse_amd_drivers_page, "amd_drivers.html", False),
    (
        "amd_notes",
        lambda t: drivers.parse_amd_release_notes(t, "26.11.2"),
        "amd_relnotes.html",
        False,
    ),
    ("intel_dsa", drivers.parse_intel_dsa, "intel_dsa.json", True),
    ("intel_page", drivers.parse_intel_page, "intel_page.html", False),
]


def only_driver_errors(parse: Callable[[Any], object], payload: Any) -> None:
    with contextlib.suppress(DriverError):  # anything else propagates and fails the test
        parse(payload)


@pytest.mark.parametrize(
    ("label", "parse", "name", "as_bytes"), PARSERS, ids=[p[0] for p in PARSERS]
)
def test_fuzz_truncations_and_random_bytes(
    label: str, parse: Callable[[Any], object], name: str, as_bytes: bool
) -> None:
    rng = random.Random(f"drivers-{label}")
    raw = data(name)
    for _ in range(25):
        cut = raw[: rng.randrange(len(raw))]
        only_driver_errors(parse, cut if as_bytes else cut.decode("utf-8", errors="replace"))
    for _ in range(25):
        mutated = bytearray(raw)
        for _ in range(rng.randint(1, 40)):
            mutated[rng.randrange(len(mutated))] = rng.randrange(256)
        payload = bytes(mutated)
        only_driver_errors(parse, payload if as_bytes else payload.decode("utf-8", "replace"))
    noise = rng.randbytes(2000)
    only_driver_errors(parse, noise if as_bytes else noise.decode("latin-1"))


def test_fuzz_dsa_zip_truncations() -> None:
    rng = random.Random("drivers-zip")
    raw = dsa_zip(data("intel_dsa.json"))
    for _ in range(40):
        only_driver_errors(drivers.parse_intel_dsa, raw[: rng.randrange(len(raw))])
