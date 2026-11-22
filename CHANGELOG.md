# Changelog
Wszystkie istotne zmiany w projekcie będą dokumentowane w tym pliku.

Format oparty na [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
a projekt stosuje [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-11-19
- Nowy interfejs PySide6 (Qt): strony Aplikacje / System / Przebieg / Narzędzia / Dziennik / O programie, wyszukiwanie, profile, zbiorcze potwierdzenie operacji systemowych, motyw ciemny/jasny/systemowy.
- Katalog programów i profili w pliku TOML (`winstaller/core/catalog.toml`) ze stabilnymi identyfikatorami i walidacją przy starcie.
- Rdzeń bez UI (`winstaller/core`): kolejka zadań z anulowaniem, symulacja `--dry-run`, akcje systemowe za cienką, testowalną warstwą.
- Poprawne raportowanie błędów winget: każdy niezerowy kod powrotu to błąd (poza jawną listą „już zainstalowane” / „wymagany restart”), z polskim opisem kodu.
- Czytelny log instalacji: paski postępu nadpisują linię, sekwencje ANSI/OSC są usuwane, polskie znaki z konsoli (UTF-8, CP852, CP1250) dekodowane poprawnie.
- Nowy mechanizm aktualizacji: porównanie wersji wg SemVer (bez downgrade'u), weryfikacja rozmiaru i SHA256, podmiana pliku w Pythonie bez `update.bat`, obsługa programu uruchomionego z innego dysku (np. pendrive), wycofanie przy błędzie.
- Testy jednostkowe, testy interfejsu (pytest-qt) i wydajnościowe (porównanie z 1.1.8).
- CI: lint, testy i build dla Windows x64 oraz ARM64; plik exe z ikoną i wersją we właściwościach.
- Zmiana listy programów: Signal zamiast Messengera, usunięty Telegram, DisplayCAL z pakietu `ErkanOzgurYilmaz.DisplayCAL`.
- Nowa sekcja „Sterowniki GPU” (tylko x64): najnowszy sterownik NVIDIA GeForce Game Ready, AMD Radeon Adrenalin lub Intel Arc ustalany na stronie producenta (z zapasowym źródłem), pobierany tylko z serwera producenta po https, weryfikowany (rozmiar, suma SHA1/SHA512 gdy producent ją podaje, podpis cyfrowy wydawcy) i instalowany po cichu z limitem czasu, bez automatycznego restartu. Bez sprawdzania zgodności karty.
- Nowa kategoria „Aktualizacje” (pierwsza w kolejce, także na początku profilu „Benchmark starter”): „Windows Update” wyszukuje wszystkie aktualizacje — także opcjonalne (Preview) i sterowniki — wypisuje je w logu i uruchamia ich pobieranie oraz instalację w tle (log w `C:\ProgramData\Winstaller\logs`), bez automatycznego restartu; „Aktualizacje Microsoft Store” zleca sklepowi aktualizację wszystkich aplikacji w tle.
- Zmiana zachowania: DaVinci Resolve Studio instaluje się z lokalnego instalatora `DaVinci_Resolve_Studio_*_Windows.exe` obok programu (wybierana jest najnowsza wersja).
- Zmiana zachowania: usunięto „Instaluj lokalne oprogramowanie” (RTSS i CapFrameX instalują się przez winget).
- Zmiana zachowania: brak `requirements.txt` — zależności są w `pyproject.toml`.

## [1.1.8] - 2026-06-20
- Refactored _read_stream: changed pipe.read(1) to pipe.read(512) for proper UTF-8/CP1250 multibyte Polish character rendering
- Added ANSI escape stripping on raw bytes level before decoding (new helper _strip_ansi_bytes)
- Stop button now kills running winget process mid-installation instead of waiting for it to finish
- Replaced show_message() in stop_installation() with print() to fix Tkinter modal dialog deadlock from worker thread
- Added installation summary at the end: task count, success/fail counts, elapsed time, error details
- Removed all emojis from terminal output and dialogs for consistency

## [1.1.7] - 2026-06-20
- Real-time streaming winget output z animacją progress bara w terminalu

## [1.1.6] - 2026-06-05
- Fix updater: rename-then-move strategy to bypass Windows locked executable, full paths for bat script

## [1.1.5] - 2026-06-05
- Benchmark starter on ARM64 architecture now also selects "Kopiuj BenchmarkToolsARM na pulpit"

## [1.1.4] - 2026-04-02
- Fix thread-unsafe TerminalRedirector by using queue and after() polling instead of direct widget writes from worker thread
- Fix race condition in stop_event handling by moving clear() to main thread before thread start
- Fix bare except: in is_admin() and check_winget_installed() to no longer swallow KeyboardInterrupt/SystemExit
- Add timeout to GitHub API request (10s) and update file download (30s) to prevent GUI hangs
- Remove duplicate ctypes import and unused checkbox_function dict from main.py
- Extract _decode_output() helper to deduplicate encoding fallback logic in winget_install/uninstall
- Extract _center_on_parent() helper to deduplicate dialog centering logic across four dialog classes
- Extract _THEME_REG_PATH constant shared by windows_light_mode() and windows_dark_mode()
- Replace backslash string concatenation with os.path.join() in installer download functions
- copy BencmarkToolsARM64 when on ARM64
- change CapFrameX method to winget
- fix battlenet
- change directx9 install to winget

## [1.1.3] - 2026-04-02
- updater now can determine architecture and check tag for github actions

## [1.1.2] - 2025-03-12
- fix hardcode file name when copy to desktop
- change install method of capframex
- fix version display in app terminal

## [1.1.1] - 2025-03-12
- checkbox lock during installation
- fix LMStudio installation package
- add progress bar when downloading update

## [1.0.5] - 2025-03-09
- fix encoding error when installing via winget
- simplyfy versioning
- fix displaying wrong version info when without git env

## [1.0.4] - 2025-03-09
- prepare for update script

## [1.0.3] - 2025-03-09
- test zmian

## [1.0.2] - 2025-03-09
- dodano skrypy, które pomogą w zarządzaniu wersjami

## [1.0.0] - 2025-03-09
### Dodano
- Podstawowa funkcjonalność instalatora
- System kategorii programów
- Interfejs użytkownika z CustomTkinter
- Terminal z logami
- Automatyczne wykrywanie i instalacja winget
- Benchmark starter
- Funkcje specjalne (BIOS, tryb zaawansowany, raport baterii)
- System wersjonowania z git tags 
