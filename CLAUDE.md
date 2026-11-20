# Winstaller

Aplikacja dla Windows (x64 i ARM64), która instaluje programy przez winget i konfiguruje
platformę testową do benchmarków. Python 3.12+, UI w PySide6, pakowana PyInstallerem do
jednego `.exe`, samoaktualizacja z GitHub Releases (`ZMASLO/winstaller`).

Pełna mapa projektu, kontrakty API `core`, opis UI i format katalogu: **docs/ARCHITECTURE.md**.

## Układ

- `winstaller/core/` — logika bez UI: `catalog.toml` + `catalog.py`, `winget.py`, `actions.py`,
  `runner.py`, `updater.py`, `platform.py`, `version.py`
- `winstaller/ui/` — interfejs PySide6 (wejście: `winstaller.ui.app.run(*, dry_run) -> int`);
  wszystko z zewnątrz (runnery, aktualizacje, winget, potwierdzenia, otwieranie plików)
  wstrzykiwane przez `ui/services.py: Services`
- `winstaller/__main__.py` — punkt wejścia
- `tests/` — `unit/`, `ui/` (marker `ui`, pytest-qt), `perf/` (marker `perf`, pytest-benchmark)
- `tools/` — `build.ps1`, `measure_exe.py`, `screenshots.py`, `make_icon.py`
- `packaging/` — `entry.py` (wejście PyInstallera), `winstaller.ico` (z `tools/make_icon.py`)
- Kod 1.1.8 usunięty w etapie 3 (historia: tag `v1.1.8`); `tests/perf/baseline.json` to jego pomiar

## Komendy (PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e . --group dev   # pip >= 25.1; albo zainstaluj grupę dev ręcznie
.venv\Scripts\python -m winstaller --smoke             # szybki test rdzenia, kod 0
.venv\Scripts\python -m winstaller --dry-run           # UI bez realnych zmian w systemie
.venv\Scripts\python -m pytest                         # wszystkie testy (~30 s)
.venv\Scripts\python -m pytest -m "not perf"           # bez benchmarków; -m ui = tylko UI
.venv\Scripts\python -m ruff check . ; .venv\Scripts\python -m ruff format .
.venv\Scripts\python tools\screenshots.py <katalog>    # zrzuty UI offscreen
.venv\Scripts\python tools\make_icon.py                # packaging\winstaller.ico
```

Dry-run: `WINSTALLER_DRY_RUN_FAIL=id1,id2` (lub `*`) symuluje błędy, `WINSTALLER_DRY_RUN_DELAY=0`
wyłącza opóźnienia, `WINSTALLER_FAKE_UPDATE=2.0.1` symuluje dostępną aktualizację.
`WINSTALLER_ARCH=arm64` udaje ARM64, `WINSTALLER_APP_DIR` zmienia katalog plików lokalnych
(BenchmarkTools, procyon\procyon-setup.exe, instalator DaVinci).
`--exit-after-show` (ukryta, diagnostyczna): pokaż okno, wypisz czas, zakończ (testy perf).

## Zasady

- `winstaller/core` nigdy nie importuje Qt/PySide/tkinter. Core jest synchroniczny; UI
  uruchamia `TaskRunner.run(ids, cancel_event=...)` we własnym wątku i przekazuje zdarzenia
  sygnałami. Listener dostaje niemutowalne migawki `Task`.
- Programy i profile dodaje się w `winstaller/core/catalog.toml`, nie w kodzie. Id pozycji są
  stabilne (profile i testy na nich polegają).
- Nigdy nie uruchamiaj realnych instalacji, odinstalowań, zmian rejestru, restartów ani
  usuwania appx w testach ani przy pracy nad kodem. Używaj `--dry-run`, `executor=` w
  `TaskRunner` albo `monkeypatch` warstwy systemowej w `actions.py`. Dozwolone polecenia
  tylko do odczytu, np. `winget search`, `winget show`, `winget error`.
- Bezpiecznik `tests/conftest.py` (autouse) blokuje procesy, rejestr, sieć, ShellExecute itp.
  we wszystkich testach — nie osłabiaj go. Testy UI dodatkowo (`tests/ui/conftest.py`)
  blokują `QDesktopServices.openUrl`, modalne dialogi i domyślne `QSettings` w rejestrze.
- Testy UI: offscreen (ustawia root conftest), okno tylko z `Services` z atrapami (fixture
  `make_window`), `QSettings` jako plik ini w `tmp_path`, bez `sleep` — `qtbot.waitUntil` /
  `waitSignal`. Ostrzeżenie Qt (`qt_log_level_fail = WARNING`), wyjątek w slocie albo w wątku
  oblewa test.
- Każdy niezerowy kod winget to błąd, poza listą `winget.SUCCESS_CODES`.
- Wspieramy tylko Windows x64 i ARM64. Pliki lokalne szukamy względem `platform.app_dir()`.
- Komunikaty dla użytkownika po polsku (UTF-8, z polskimi znakami). Identyfikatory,
  docstringi i komentarze po angielsku; komentarze tylko tam, gdzie wyjaśniają „dlaczego”.
- Typowanie wszędzie, `from __future__ import annotations`, ruff (line-length 100) dla całego repo.
- Wersja: `winstaller/core/version.py` (`return "X.Y.Z"` — format wymagany przez `bump_version.py`,
  który commituje i taguje — nie uruchamiaj go bez potrzeby).
- Assety wydań muszą się nazywać `winstaller-x64.exe` / `winstaller-arm64.exe`, tagi `vX.Y.Z`
  (zgodność aktualizacji z 1.x).
