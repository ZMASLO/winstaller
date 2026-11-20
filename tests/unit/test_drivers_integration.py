"""GPU drivers wired into the catalog, ``actions.execute``, the runner and the dry run."""

from __future__ import annotations

import socket
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from tests.support import RecordingListener, make_catalog, make_ctx, make_item, minimal_data
from winstaller.core import actions, drivers
from winstaller.core.actions import ActionError, ActionOutcome
from winstaller.core.catalog import CatalogError, ItemKind, load_catalog, parse_catalog
from winstaller.core.runner import DryRunConfig, TaskRunner, TaskStatus, make_dry_run_executor

DRIVER_IDS = ("driver-nvidia", "driver-amd", "driver-intel")


def driver_entry(**fields: Any) -> dict[str, Any]:
    entry = {"id": "d", "name": "D", "category": "cat", "kind": "driver", "vendor": "nvidia"}
    entry.update(fields)
    return {k: v for k, v in entry.items() if v is not None}


# ----------------------------------------------------------------- catalog


class TestCatalog:
    def test_driver_item_parses(self) -> None:
        cat = parse_catalog(minimal_data(items=[driver_entry()]))
        item = cat.item("d")
        assert item.kind == ItemKind.DRIVER and item.vendor == "nvidia"

    @pytest.mark.parametrize(
        ("entry", "match"),
        [
            (driver_entry(vendor=None), "wymaga pola 'vendor'"),
            (driver_entry(vendor=""), "vendor"),
            (driver_entry(vendor="matrox"), "nieznany producent"),
            (driver_entry(vendor="NVIDIA"), "nieznany producent"),
            (
                {
                    "id": "w",
                    "name": "W",
                    "category": "cat",
                    "kind": "winget",
                    "winget_id": "A.B",
                    "vendor": "amd",
                },
                "tylko dla rodzaju 'driver'",
            ),
            (driver_entry(vendor=5), "vendor"),
        ],
    )
    def test_invalid_vendor(self, entry: dict[str, Any], match: str) -> None:
        with pytest.raises(CatalogError, match=match):
            parse_catalog(minimal_data(items=[entry]))

    def test_known_vendors_can_be_injected(self) -> None:
        data = minimal_data(items=[driver_entry(vendor="matrox")])
        assert parse_catalog(data, known_vendors={"matrox"}).item("d").vendor == "matrox"

    def test_real_catalog(self) -> None:
        cat = load_catalog()
        items = [cat.item(i) for i in DRIVER_IDS]
        assert [i.vendor for i in items] == ["nvidia", "amd", "intel"]
        assert [i.id for i in cat.items("gpu-drivers")] == list(DRIVER_IDS)
        for item in items:
            assert item.kind == ItemKind.DRIVER and item.category == "gpu-drivers"
            assert item.arch == ("x64",)
            assert item.confirm and item.destructive
            assert cat.availability(item, arch="x64", base_dir=Path("C:/nowhere")).available
            unavailable = cat.availability(item, arch="arm64", base_dir=Path("C:/nowhere"))
            assert not unavailable.available and "ARM64" in (unavailable.reason or "")
        for profile in cat.profiles():
            for arch in ("x64", "arm64"):
                assert not set(profile.items_for(arch)) & set(DRIVER_IDS)  # type: ignore[arg-type]
        assert set(drivers.VENDORS) == {"nvidia", "amd", "intel"}


# ----------------------------------------------------------------- actions


class TestExecute:
    def test_success(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        seen: dict[str, Any] = {}

        def fake(vendor_id: str, **kw: Any) -> drivers.DriverInstallResult:
            seen.update(kw, vendor=vendor_id)
            kw["log"]("line")
            return drivers.DriverInstallResult(True, "Zainstalowano.", version="1.2")

        monkeypatch.setattr(drivers, "install_latest", fake)
        item = make_item("driver-amd", kind=ItemKind.DRIVER, vendor="amd")
        ctx, rec = make_ctx(tmp_path, item)
        outcome = actions.execute(item, ctx)
        assert outcome == ActionOutcome(True, "Zainstalowano.")
        assert seen["vendor"] == "amd" and seen["app_dir"] == tmp_path
        assert seen["cancel_event"] is ctx.cancel_event
        assert "session" not in seen and "work_dir" not in seen
        assert rec.lines == ["line"]

    def test_driver_error_becomes_action_error_with_link(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def fake(vendor_id: str, **kw: Any) -> drivers.DriverInstallResult:
            raise drivers.DriverError("Zły podpis.", vendor_url=drivers.INTEL_PAGE)

        monkeypatch.setattr(drivers, "install_latest", fake)
        item = make_item("driver-intel", kind=ItemKind.DRIVER, vendor="intel")
        with pytest.raises(ActionError) as info:
            actions.execute(item, make_ctx(tmp_path, item)[0])
        assert (
            str(info.value) == f"Zły podpis. Sterownik można pobrać ręcznie: {drivers.INTEL_PAGE}"
        )

    def test_cancel(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(
            drivers,
            "install_latest",
            lambda _v, **_k: drivers.DriverInstallResult(False, "Przerwano.", cancelled=True),
        )
        item = make_item("driver-nvidia", kind=ItemKind.DRIVER)
        outcome = actions.execute(item, make_ctx(tmp_path, item)[0])
        assert outcome.cancelled and not outcome.ok

    def test_missing_vendor_is_an_action_error(self, tmp_path: Path) -> None:
        item = make_item("x", kind=ItemKind.DRIVER, vendor=None)
        with pytest.raises(ActionError, match="Nieznany producent"):
            actions.execute(item, make_ctx(tmp_path, item)[0])


# ------------------------------------------------------------------ runner


def runner_with(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, result: Any, arch: str = "x64"
) -> tuple[TaskRunner, RecordingListener, list[str]]:
    calls: list[str] = []

    def fake(vendor_id: str, **kw: Any) -> drivers.DriverInstallResult:
        calls.append(vendor_id)
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return result(kw)
        return result

    monkeypatch.setattr(drivers, "install_latest", fake)
    listener = RecordingListener()
    runner = TaskRunner(load_catalog(), listener=listener, arch=arch, app_dir=tmp_path)  # type: ignore[arg-type]
    return runner, listener, calls


@pytest.mark.parametrize(
    ("result", "status"),
    [
        (drivers.DriverInstallResult(True, "OK"), TaskStatus.OK),
        (drivers.DriverInstallResult(False, "Przerwano.", cancelled=True), TaskStatus.CANCELLED),
        (drivers.DriverError("Błąd.", vendor_url="https://x"), TaskStatus.FAILED),
        (RuntimeError("unexpected"), TaskStatus.FAILED),
    ],
)
def test_runner_statuses(monkeypatch, tmp_path, result, status) -> None:
    runner, listener, calls = runner_with(monkeypatch, tmp_path, result)
    summary = runner.run(["driver-nvidia"])
    assert summary.results[0].status == status
    assert calls == ["nvidia"]
    if status == TaskStatus.FAILED and isinstance(result, drivers.DriverError):
        assert "https://x" in summary.results[0].message


def test_runner_skips_drivers_on_arm64(monkeypatch, tmp_path) -> None:
    runner, _l, calls = runner_with(
        monkeypatch, tmp_path, drivers.DriverInstallResult(True, "OK"), arch="arm64"
    )
    summary = runner.run(list(DRIVER_IDS))
    assert [r.status for r in summary.results] == [TaskStatus.SKIPPED] * 3
    assert calls == []


def test_runner_cancel_during_driver(monkeypatch, tmp_path) -> None:
    cancel = threading.Event()

    def install(kw: dict[str, Any]) -> drivers.DriverInstallResult:
        cancel.set()  # the user presses Stop while the driver installs
        assert kw["cancel_event"].is_set()
        return drivers.DriverInstallResult(False, "Przerwano.", cancelled=True)

    runner, _l, calls = runner_with(monkeypatch, tmp_path, install)
    summary = runner.run(["driver-nvidia", "driver-amd"], cancel_event=cancel)
    assert [r.status for r in summary.results] == [TaskStatus.CANCELLED] * 2
    assert calls == ["nvidia"] and summary.cancelled


# ----------------------------------------------------------------- dry run


@pytest.fixture
def no_side_effects(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    touched: list[str] = []

    def forbid(name: str) -> Any:
        def _f(*a: Any, **k: Any) -> Any:
            touched.append(name)
            raise AssertionError(f"dry run used {name}")

        return _f

    for name in (
        "install_latest",
        "download",
        "verify_signature",
        "run_process",
        "resolve",
        "default_session",
        "download_dir",
    ):
        monkeypatch.setattr(drivers, name, forbid(name))
    monkeypatch.setattr(actions, "execute", forbid("actions.execute"))
    monkeypatch.setattr(subprocess, "Popen", forbid("Popen"))
    monkeypatch.setattr(socket, "getaddrinfo", forbid("getaddrinfo"))
    return touched


@pytest.mark.parametrize("vendor", ["nvidia", "amd", "intel"])
def test_dry_run_driver(no_side_effects: list[str], tmp_path: Path, vendor: str) -> None:
    listener = RecordingListener()
    runner = TaskRunner(
        load_catalog(),
        dry_run=True,
        dry_run_config=DryRunConfig(delay=0.0),
        listener=listener,
        arch="x64",
        app_dir=tmp_path,
    )
    summary = runner.run([f"driver-{vendor}"])
    result = summary.results[0]
    assert result.status == TaskStatus.OK
    assert "Uruchom ponownie komputer" in result.message
    assert drivers.DRY_RUN_SAMPLES[vendor].version in result.message
    lines = [p for n, p in listener.events if n == "output_line"]
    replaced = [p for n, p in listener.events if n == "output_replace_last"]
    assert any(x.startswith("Najnowsza wersja:") for x in lines)
    assert any("Pobieranie: 100%" in x for x in replaced)
    spec = drivers.VENDORS[vendor]
    assert any(" ".join(spec.install_args) in x for x in lines)
    assert any(spec.signer in x for x in lines)
    assert no_side_effects == []


def test_dry_run_fail_ids_and_cancel(no_side_effects: list[str], tmp_path: Path) -> None:
    runner = TaskRunner(
        load_catalog(),
        dry_run=True,
        dry_run_config=DryRunConfig(delay=0.0, fail_ids=frozenset({"driver-amd"})),
        arch="x64",
        app_dir=tmp_path,
    )
    summary = runner.run(list(DRIVER_IDS))
    assert [r.status for r in summary.results] == [
        TaskStatus.OK,
        TaskStatus.FAILED,
        TaskStatus.OK,
    ]
    cancel = threading.Event()
    cancel.set()
    summary = runner.run(list(DRIVER_IDS), cancel_event=cancel)
    assert {r.status for r in summary.results} == {TaskStatus.CANCELLED}
    assert no_side_effects == []


def test_dry_run_cancel_mid_driver(tmp_path: Path) -> None:
    cancel = threading.Event()
    # delay=0 never looks at the cancel event (same as for winget items); a tiny delay does
    executor = make_dry_run_executor(DryRunConfig(delay=0.001))
    item = make_catalog([make_item("driver-intel", kind=ItemKind.DRIVER, vendor="intel")]).item(
        "driver-intel"
    )
    ctx, rec = make_ctx(tmp_path, item, cancel_event=cancel)
    ctx_log = ctx.log

    def log(text: str) -> None:
        ctx_log(text)
        if text.startswith("Pobieranie https"):
            cancel.set()

    object.__setattr__(ctx, "log", log)
    outcome = executor(item, ctx)
    assert outcome.cancelled
    assert not any("Instalator zakończył" in x for x in rec.lines)


def test_dry_run_unknown_vendor_does_not_crash(tmp_path: Path) -> None:
    executor = make_dry_run_executor(DryRunConfig(delay=0.0))
    item = make_item("driver-x", kind=ItemKind.DRIVER, vendor="matrox")
    ctx, _rec = make_ctx(tmp_path, item)
    assert executor(item, ctx).ok


def test_dry_run_release_samples() -> None:
    for vendor, sample in drivers.DRY_RUN_SAMPLES.items():
        rel = drivers.dry_run_release(vendor)
        assert rel.version == sample.version and rel.size == sample.size
        assert drivers.host_allowed(rel.url, drivers.VENDORS[vendor].download_hosts)
    assert drivers.dry_run_release(None).vendor == "nvidia"
