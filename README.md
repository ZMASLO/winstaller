# Winstaller

Winstaller to aplikacja dla Windows, która jednym kliknięciem instaluje zestaw programów przez
[winget](https://learn.microsoft.com/windows/package-manager/winget/) i przygotowuje platformę
testową do benchmarków: launchery gier, narzędzia diagnostyczne, programy testowe, lokalne
instalatory, kopiowanie BenchmarkTools na pulpit, motyw systemu, usuwanie zbędnych aplikacji,
restart do BIOS-u, raport baterii. Profil „Benchmark starter” zaznacza typowy zestaw na raz.

## Pobieranie

Najnowsza wersja jest na stronie [Releases](https://github.com/ZMASLO/winstaller/releases/latest):

| System | Plik |
|---|---|
| Windows 10/11 x64 (Intel, AMD) | `winstaller-x64.exe` |
| Windows 11 ARM64 (Snapdragon itp.) | `winstaller-arm64.exe` |

`SHA256SUMS.txt` zawiera sumy kontrolne obu plików
(`Get-FileHash .\winstaller-x64.exe -Algorithm SHA256`).

## Wymagania

- Windows 10/11 x64 albo Windows 11 ARM64,
- konto z uprawnieniami administratora (aplikacja sama poprosi o nie przez UAC),
- winget (App Installer); jeśli go brakuje, Winstaller spróbuje go doinstalować,
- połączenie z internetem.

## Uruchomienie

Pobierz plik `.exe` i uruchom go. Nie wymaga instalacji. Zaznacz programy albo wybierz profil
i kliknij „Zainstaluj”. Postęp i wynik każdej pozycji widać na stronie „Przebieg”; nieudane
pozycje można ponowić przyciskiem „Ponów nieudane”. Pełny log sesji jest w „Dzienniku”.

### Pliki lokalne obok exe

Część pozycji korzysta z plików, których nie ma w winget. Winstaller szuka ich **w katalogu,
w którym leży `winstaller.exe`**. Gdy pliku brakuje, pozycja jest wyszarzona z wyjaśnieniem.

| Plik lub katalog | Pozycja |
|---|---|
| `BenchmarkTools\` | Kopiuj BenchmarkTools na pulpit |
| `BenchmarkToolsARM\` | Kopiuj BenchmarkToolsARM na pulpit (tylko ARM64) |
| `procyon\procyon-setup.exe` | UL Procyon |
| `DaVinci_Resolve_Studio_*_Windows.exe` | DaVinci Resolve Studio (dowolna wersja pasująca do wzorca) |

Przykładowy układ:

```
D:\Narzedzia\
  winstaller.exe
  BenchmarkTools\...
  procyon\procyon-setup.exe
```

## Aktualizacje

Winstaller sprawdza na GitHub Releases, czy jest nowsza wersja, i pobiera plik
dla swojej architektury, weryfikuje rozmiar i sumę SHA256, podmienia exe (także uruchomiony
z pendrive'a) i uruchamia się ponownie; przy błędzie przywraca poprzednią wersję. Nigdy nie instaluje starszej wersji. Wersje 1.1.x aktualizują się do 2.x tą samą drogą.

## Dla deweloperów

Wymagany Python 3.12+. Kod 2.0 jest w pakiecie `winstaller/` (rdzeń `winstaller/core/` bez Qt,
interfejs PySide6 w `winstaller/ui/`). Stary kod 1.1.8 został usunięty (jest w historii gita,
tag `v1.1.8`). Zależności są zdefiniowane wyłącznie w `pyproject.toml`.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip          # --group wymaga pip >= 25.1
.venv\Scripts\python -m pip install -e . --group dev
```

Uruchamianie bez zmian w systemie:

```powershell
.venv\Scripts\python -m winstaller --smoke      # test rdzenia i katalogu, kod 0
.venv\Scripts\python -m winstaller --dry-run    # UI z symulacją, nic nie jest instalowane
.venv\Scripts\python -m winstaller --version
```

W trybie `--dry-run`: `WINSTALLER_DRY_RUN_FAIL=id1,id2` (albo `*`) symuluje błędy,
`WINSTALLER_DRY_RUN_DELAY=0` wyłącza opóźnienia, `WINSTALLER_ARCH=arm64` udaje ARM64,
`WINSTALLER_APP_DIR` wskazuje katalog z plikami lokalnymi.

### Testy i lint

```powershell
.venv\Scripts\python -m pytest -m "not perf"     # testy jednostkowe i UI (offscreen)
.venv\Scripts\python -m pytest -m ui             # tylko UI (pytest-qt)
.venv\Scripts\python -m pytest -m perf           # wydajność, porównanie z 1.1.8
.venv\Scripts\python -m ruff check . ; .venv\Scripts\python -m ruff format --check .
```

### Build

```powershell
.\tools\build.ps1                     # wynik: build-out\dist\winstaller.exe + wynik --smoke
.\tools\build.ps1 -Clean -Measure     # czysty build + rozmiar i czas startu (tools\measure_exe.py)
.\tools\build.ps1 -OutDir C:\tmp\wb   # inny katalog roboczy
```

Build używa `winstaller.spec` (PyInstaller, jeden plik, bez konsoli, bez UPX, ikona
`packaging/winstaller.ico` generowana przez `tools\make_icon.py`). Architektura exe
jest taka jak architektura Pythona, którym uruchamiasz build: exe ARM64 buduje się natywnym
Pythonem ARM64. Aplikacja sama prosi o uprawnienia administratora w trakcie działania, więc
`--smoke`, `--version` i `--dry-run` nie wywołują UAC.

CI (`.github/workflows/build-release.yml`) dla każdego pusha na `main`/`2.0` i każdego PR
uruchamia ruff, testy i build na x64 i ARM64, sprawdza `winstaller.exe --smoke` oraz budżety
rozmiaru i czasu startu. Gotowe exe są w artefaktach biegu.

### Wydawanie wersji

```powershell
python bump_version.py patch "Opis zmiany" --dry-run       # podgląd: diff i polecenia git
python bump_version.py patch "Opis zmiany" "Druga zmiana"  # minor / major analogicznie
git push origin main
git push origin vX.Y.Z
```

`bump_version.py` podnosi wersję w `winstaller/core/version.py`, dopisuje wpis na górze
`CHANGELOG.md`, commituje tylko te dwa pliki i tworzy tag `vX.Y.Z`. Odmawia działania przy
niezacommitowanych zmianach, na gałęzi innej niż `main` (`--branch` zmienia) i gdy tag już
istnieje. Wypchnięcie tagu uruchamia workflow, który buduje oba exe i tworzy **szkic** wydania
z `winstaller-x64.exe`, `winstaller-arm64.exe`, `SHA256SUMS.txt` i automatycznymi notatkami.
Tag musi zgadzać się z wersją w `version.py`, inaczej workflow kończy się błędem. Wydanie trzeba
opublikować ręcznie na GitHubie; dopiero wtedy aktualizator je zobaczy. Nazwy plików exe muszą
zostać bez zmian (korzysta z nich aktualizator, także w wersjach 1.1.x).

### Dodawanie programu do katalogu

Programy, kategorie i profile są opisane w `winstaller/core/catalog.toml`, nie w kodzie.
Format pliku i instrukcja krok po kroku: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
(sekcje „Format `catalog.toml`” i „Jak dodać program”). Po zmianie uruchom
`python -m winstaller --smoke`, który waliduje katalog.

## Licencja

[LICENSE](LICENSE)
