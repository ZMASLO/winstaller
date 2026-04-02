# Changelog
Wszystkie istotne zmiany w projekcie będą dokumentowane w tym pliku.

Format oparty na [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
a projekt stosuje [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

## [1.0.2] - 2025-03-09
- dodano skrypy, które pomogą w zarządzaniu wersjami

## [1.0.3] - 2025-03-09
- test zmian

## [1.0.4] - 2025-03-09
- prepare for update script

## [1.0.5] - 2025-03-09
- fix encoding error when installing via winget
- simplyfy versioning
- fix displaying wrong version info when without git env

## [1.1.1] - 2025-03-12
- checkbox lock during installation
- fix LMStudio installation package
- add progress bar when downloading update

## [1.1.2] - 2025-03-12
- fix hardcode file name when copy to desktop
- change install method of capframex
- fix version display in app terminal

## [1.1.3] - 2026-04-02
- updater now can determine architecture and check tag for github actions

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