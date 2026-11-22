# Winstaller — architektura

Dokument ma dwie części: (A) historyczna mapa wersji 1.1.8 (jej kod został usunięty z repo w
etapie 3; jest w historii gita pod tagiem `v1.1.8`, a `tests/perf/baseline.json` zawiera jej
pomiar) i (B) architektura 2.0 wraz z **kontraktami publicznego API `core`** i opisem UI,
na których opierają się testy i build.

---

## A. Wersja 1.1.8 (historia; pliki już nie istnieją w repo)

### Pliki (stan z tagu `v1.1.8`)

| Plik | Rola |
|---|---|
| `main.py` | elewacja UAC (`ShellExecuteW runas` z `sys.argv[0]`), doinstalowanie winget (`Add-AppxPackage <msixbundle>`), zbudowanie checkboxów z `CATEGORIES`, `mainloop()` |
| `core/config.py` | `APP_CONFIG`, `CATEGORIES` (nazwa wyświetlana → funkcja, `import *`), `BENCHMARK_PROGRAMS` (lista nazw wyświetlanych), dopisanie pozycji ARM w czasie importu |
| `core/installers.py` | `winget_install/uninstall`, strumieniowanie wyjścia (`_read_stream`, `_strip_ansi_bytes`, `_decode_output`), `kill_current_winget`, `download_install`, `download_unzip_install`, ~35 funkcji `install_*` |
| `core/system_utils.py` | `is_admin`, `check_winget_installed`, motyw, restart do BIOS / zaawansowany, raport baterii, kopiowanie na pulpit, `remove_bloat` |
| `core/updater.py` | GitHub `releases/latest` → pobranie `winstaller_new.exe` → `update.bat` (ren/move/start) → `sys.exit` |
| `core/version.py` | `get_version()` zwraca literał (edytowany przez `bump_version.py`) |
| `gui/app.py`, `gui/dialogs.py` | okno CustomTkinter: sidebar (Uruchom, Stop, Benchmark starter, Odznacz, Specjalne: BIOS / zaawansowany / bateria, aktualizacje, pasek postępu, logi), dialogi modalne |
| `bump_version.py` | bump semver, wpis w CHANGELOG, commit + tag `vX.Y.Z` |
| `.github/workflows/build-release.yml` | PyInstaller `-F` na `windows-latest` (x64) i `windows-11-arm` (arm64); na tag: draft release z `winstaller-x64.exe` i `winstaller-arm64.exe` |

### Przepływ uruchomienia

1. `main.py`: jeśli nie admin → `ShellExecuteW("runas", sys.executable, sys.argv[0])` i wyjście.
2. Tworzenie `ModernApp` (ukrycie konsoli, przekierowanie `sys.stdout/stderr` do kolejki terminala).
3. Brak winget → `Add-AppxPackage` msixbundle z GitHub.
4. Checkboxy z `CATEGORIES`; „Uruchom!” startuje wątek, który po kolei wywołuje funkcje
   zaznaczonych pozycji; log to `print` przechwytywany przez `TerminalRedirector`.

### Znane błędy 1.1.8 (naprawione w 2.0)

- Kod powrotu winget ignorowany — nieudana instalacja liczona jako sukces.
- Updater porównuje wersje przez `!=` (możliwy downgrade); brak weryfikacji rozmiaru/sumy; `update.bat`.
- `core/updater.py` importuje `gui` (zależność cykliczna core ↔ gui).
- `download_install` ładuje cały plik do RAM, bez timeoutu, zapis do `cwd`.
- Pliki lokalne szukane w `os.getcwd()` zamiast w katalogu exe.
- `remove_bloat`: osobny PowerShell dla każdej z ~70 pozycji, duplikaty, podwójne `**`.
- Steam/Epic: `Popen` na sztywnej ścieżce rzuca wyjątek, gdy instalacja się nie udała.
- Motyw: zmieniany tylko `AppsUseLightTheme`; jasny rzuca wyjątek, gdy wartości brak.
- Kopiowanie katalogu na pulpit pada, gdy cel istnieje; komunikat błędu na sztywno „BenchmarkTools”.
- Pulpit zgadywany jako `~/Desktop` (zły przy przekierowaniu do OneDrive).
- DaVinci Resolve: link z wygasłym tokenem.
- Parser strumienia: przetwarza cały bufor od nowa po każdym chunku (kwadratowo), sekwencja ANSI
  przecięta na granicy chunków się psuje, `time.sleep` w pętli, sekwencje OSC (`ESC ] 9;4;...`)
  zostawiały śmieci w logu, pasek postępu z `\r` pokazywany dopiero po końcu linii.
- Raport baterii zapisywany do `cwd`, ścieżka nie jest zwracana.
- Widgety Tk modyfikowane z wątku roboczego.

---

## B. Wersja 2.0

### Układ

```
winstaller/
  __main__.py        wejście: --smoke / --dry-run / --version / --no-elevate, elewacja, start UI
  core/              ZERO importów Qt; wszystko synchroniczne
    version.py       get_version(), get_version_info(), VERSION
    platform.py      arch, admin/elewacja, frozen, app_dir, desktop_dir, resource_path
    catalog.toml     deklaratywny katalog (kategorie, pozycje, profile)
    catalog.py       dataclasses + loader tomllib + walidacja + availability
    winget.py        StreamParser (czysty), stream_process, kody winget, install/uninstall
    actions.py       akcje systemowe + execute(item, ctx); cienka warstwa systemowa do mockowania
    drivers.py       sterowniki GPU: ustalanie wersji u producenta, pobieranie, podpis, instalacja
    updates.py       Windows Update (skan WUA + instalacja w tle) i aktualizacje Microsoft Store
    update_scripts.py  skrypty PowerShell dla updates.py (stałe, uruchamiane przez -EncodedCommand)
    runner.py        TaskRunner, statusy, wyniki, listener, symulacja dry-run
    updater.py       check / download / apply / cleanup_old
  ui/                PySide6, opis niżej („Interfejs (winstaller/ui)”)
tests/               unit / ui (pytest-qt) / perf (pytest-benchmark)
tools/               build.ps1, measure_exe.py, screenshots.py, make_icon.py
packaging/           entry.py (wejście PyInstallera), winstaller.ico
```

Zależności między modułami `core` (bez cykli na poziomie modułu):
`version` ← `platform` ← `catalog` ← `winget` ← `drivers` ← `actions` ← `runner`; `updater`
używa `platform` i `version`; `drivers` używa tylko `winget` i `version`; `updates` używa
`platform`, `winget` i `update_scripts` (bez `catalog`/`actions`), a korzystają z niego
`actions` i `runner` (dry-run).
`catalog.parse_catalog` importuje leniwie `actions.ACTIONS` (walidacja nazw akcji) i
`drivers.VENDORS` (walidacja producentów).

### Przepływ danych

```
catalog.toml --load_catalog()--> Catalog --(UI: wybór id)--> TaskRunner.run(ids)
   TaskRunner: dla każdej pozycji -> availability -> executor(item, ActionContext)
      executor = actions.execute (realnie) | make_dry_run_executor(cfg) (dry-run)
      actions.execute -> winget.install/uninstall | run_local_installer | ACTIONS[name]
                         | drivers.install_latest(vendor)
      wyjście procesów -> StreamParser -> ctx.log / ctx.replace_last -> listener.output_*
   zdarzenia -> RunnerListener (UI: bridge.py zamienia je na sygnały Qt)
```

### Model wątków

- `core` jest w całości **synchroniczny**. `TaskRunner.run()` blokuje i wykonuje się w wątku
  wywołującym; **wszystkie metody listenera są wołane w tym samym wątku** (wątek czytający
  potok procesu tylko wkłada zdarzenia do kolejki, oddaje je `stream_process` w wątku wołającym).
- UI uruchamia `run()` w swoim wątku roboczym (QThread) i przekazuje zdarzenia do wątku GUI
  sygnałami (połączenia queued). Nie dotykać widgetów z listenera bezpośrednio.
- Anulowanie bez wyścigu: wołający tworzy `threading.Event` i przekazuje go do
  `run(ids, cancel_event=ev)`; ustawienie go w dowolnym momencie (także zanim wątek wszedł do
  `run()`) anuluje ten bieg. `TaskRunner.cancel()` (bezpieczne wątkowo) ustawia zdarzenie
  bieżącego biegu; wywołane, gdy nic nie biegnie, nic nie robi i nie „przecieka” do
  następnego biegu.
- `updater.check/download` też są blokujące — UI woła je w tle. `download` przyjmuje
  `cancel_event`; `progress_cb` jest wołany w wątku pobierającym.

---

## Kontrakty publicznego API `core`

Sygnatury poniżej są wiążące dla UI/testów. Wszystkie komunikaty dla użytkownika (wyjątki,
`message`) są po polsku.

### `winstaller.core.version`

```python
APP_NAME: str = "Winstaller"
def get_version() -> str            # "2.0.0"; literał edytuje bump_version.py (regex return "X.Y.Z")
VERSION: str
def get_version_info() -> str       # "Winstaller 2.0.0"
```

### `winstaller.core.platform`

```python
Arch = Literal["x64", "arm64"]
ALL_ARCHES: tuple[Arch, ...] = ("x64", "arm64")
ENV_ARCH = "WINSTALLER_ARCH"        # nadpisanie architektury (testy ręczne UI, np. profil ARM)
ENV_APP_DIR = "WINSTALLER_APP_DIR"  # nadpisanie katalogu plików lokalnych

def get_arch() -> Arch              # natywna arch.; x64 emulowany na ARM64 -> "arm64" (IsWow64Process2)
def is_admin() -> bool
def is_frozen() -> bool             # PyInstaller
def relaunch_as_admin(args: list[str] | None = None) -> bool
    # UAC runas; frozen: exe + args, dev: python -m winstaller + args; True = wystartowano, wyjdź
def app_dir() -> Path               # frozen: katalog exe; dev: katalog repo
def desktop_dir() -> Path           # SHGetKnownFolderPath(FOLDERID_Desktop), fallback %USERPROFILE%\Desktop
def windows_system_dir() -> Path    # GetSystemDirectoryW; fallback %SystemRoot%\System32
def system32_path(name: str) -> str # pełna ścieżka programu systemowego; FileNotFoundError gdy brak
def program_data_dir() -> Path      # FOLDERID_ProgramData; fallback %PROGRAMDATA%
def local_app_data_dir() -> Path    # FOLDERID_LocalAppData; fallback %LOCALAPPDATA%
POWERSHELL_EXE = r"WindowsPowerShell\v1.0\powershell.exe"
    # programy systemowe (powershell, shutdown, powercfg) zawsze po pełnej ścieżce: po samej
    # nazwie CreateProcess szuka najpierw w katalogu programu i bieżącym, więc podłożony
    # powershell.exe obok winstaller.exe działałby z prawami admina. `winget` zostaje po
    # nazwie: to App Execution Alias w profilu użytkownika (%LOCALAPPDATA%\Microsoft\WindowsApps)
def resource_path(relative: str | PathLike) -> Path
    # zasób pakietu względem katalogu winstaller/; frozen: _MEIPASS/winstaller/<relative>
```

### `winstaller.core.catalog`

```python
class CatalogError(Exception)

class ItemKind(StrEnum): WINGET, WINGET_UNINSTALL, LOCAL_EXE, ACTION, DRIVER ("driver")
class CategoryKind(StrEnum): INSTALL ("install"), TOOLS ("tools")

@dataclass(frozen=True) class Category:
    id: str; name: str; description: str; order: int; kind: CategoryKind = INSTALL

@dataclass(frozen=True) class Item:
    id: str                       # stabilny slug
    name: str                     # polska nazwa wyświetlana
    category: str                 # id kategorii
    kind: ItemKind
    description: str = ""
    winget_id: str | None = None  # WINGET / WINGET_UNINSTALL
    action: str | None = None     # ACTION: klucz w actions.ACTIONS
    path: str | None = None       # LOCAL_EXE: ścieżka względem app_dir (glob dozwolony)
    args: tuple[str, ...] = ()    # argumenty instalatora (LOCAL_EXE) lub akcji (ACTION)
    arch: tuple[Arch, ...] = ALL_ARCHES
    requires_files: tuple[str, ...] = ()   # ścieżki względne do app_dir (pliki lub katalogi)
    post_launch: str | None = None         # exe do uruchomienia po udanej instalacji (%ZMIENNE%)
    confirm: str | None = None             # tekst potwierdzenia => akcja destrukcyjna
    unavailable: str | None = None         # stały powód niedostępności (np. pakiet wycofany)
    vendor: str | None = None              # DRIVER: klucz w drivers.VENDORS (nvidia/amd/intel)
    destructive: bool  (property)          # niepusty confirm; confirm = "" -> None
    def supports(arch: Arch) -> bool

@dataclass(frozen=True) class Profile:
    id: str; name: str; description: str; items: tuple[str, ...]
    arch_items: Mapping[Arch, tuple[str, ...]]
    def items_for(arch: Arch) -> tuple[str, ...]     # items + dodatki dla arch

@dataclass(frozen=True) class Availability:
    available: bool; reason: str | None

class Catalog:
    def categories(kind: CategoryKind | None = None) -> list[Category]   # w kolejności
    def category(category_id: str) -> Category                         # CatalogError gdy brak
    def items(category_id: str | None = None) -> list[Item]            # kolejność katalogu
    def item(item_id: str) -> Item                                     # CatalogError gdy brak
    def get(item_id: str) -> Item | None
    def __contains__(item_id) -> bool
    def tools() -> list[Item]                  # pozycje kategorii typu "tools" (strona Narzędzia)
    def is_tool(item: Item) -> bool
    def in_catalog_order(item_ids: Iterable[str]) -> list[str]  # dedup + sortowanie
    def profiles() -> list[Profile]
    def profile(profile_id: str) -> Profile
    def profile_items(profile_id: str, arch: Arch | None = None) -> list[Item]
    def availability(item: Item | str, *, arch: Arch | None = None,
                     base_dir: Path | None = None) -> Availability
        # kolejność: unavailable -> architektura -> brak plików (requires_files + path)
    def resolve_path(item: Item, *, base_dir: Path | None = None) -> Path | None

DEFAULT_CATALOG_PATH: Path                 # resource_path("core/catalog.toml")
def load_catalog(path=None, *, known_actions: Collection[str] | None = None,
                 known_vendors: Collection[str] | None = None) -> Catalog
def parse_catalog(data: Mapping, *, known_actions=None, known_vendors=None) -> Catalog
    # z gotowego dict (testy); known_vendors domyślnie drivers.VENDORS
def resolve_relative(base: Path, rel: str) -> Path | None
    # glob tylko w rel ([ ] w base dosłownie); przy wielu trafieniach najwyższa wersja
    # (natural_key: 19.10 > 19.9)
def natural_key(text: str) -> tuple             # cyfry liczbowo, tekst bez wielkości liter
def is_relative_inside(rel: str) -> bool        # bez litery dysku, roota, UNC i ..
```

Walidacja (`CatalogError` z czytelnym komunikatem): nieznane pola/sekcje, brak wymaganych pól,
duplikaty id (kategorie, pozycje, profile), nieznana kategoria pozycji, nieznany rodzaj, akcja
spoza `actions.ACTIONS`, `winget_id` wymagany dla winget, `path` dla `local_exe`, ścieżki
bezwzględne, z literą dysku (`D:setup.exe`), UNC/rootowane lub z `..` (w `path`,
`requires_files` i w `args[0]` akcji `copy_dir_to_desktop`, która wymaga tego argumentu),
`vendor` wymagany i znany (`drivers.VENDORS`) dla `driver`, zabroniony dla innych rodzajów,
nieznana architektura, wpis sekcji niebędący tabelą (np. `items = [3]`), profil wskazujący
nieistniejące id, zduplikowane id w `profiles.items` / `arch_items.<arch>` lub pozycję
nieobsługującą danej architektury w `arch_items`.

### `winstaller.core.winget`

```python
CREATE_NO_WINDOW: int
class OutputKind(StrEnum): APPEND = "append", REPLACE_LAST = "replace_last"
@dataclass(frozen=True) class OutputEvent: kind: OutputKind; text: str

def system_codepages() -> tuple[str, ...]       # ("cp<OEM>", "cp<ANSI>") z GetOEMCP/GetACP
def decoding_score(text: str) -> int
def decode_console_bytes(raw: bytes, *, codepages: Sequence[str] | None = None) -> str
    # poprawne UTF-8 -> bez zmian; inaczej kandydaci: codepages (domyślnie system_codepages())
    # + cp852 + cp1250, wygrywa najwyższy decoding_score (remis: wcześniejszy, czyli OEM)
POWERSHELL_UTF8_PREAMBLE: str                    # prefiks każdego skryptu PS: wyjście w UTF-8

class StreamParser:                             # CZYSTY, bez I/O
    def feed(chunk: bytes) -> list[OutputEvent]
    def flush() -> list[OutputEvent]            # raz, na końcu strumienia

@dataclass(frozen=True) class ProcessOutcome: returncode: int; cancelled: bool
def stream_process(args, *, on_line, on_replace_last, cancel_event=None, cwd=None,
                   on_start=None, creationflags=0) -> ProcessOutcome
    # on_start(proc): zaraz po starcie procesu (drivers: przypisanie do Job Object i wznowienie);
    # wyjątek z on_start zatrzymuje proces i jest przekazywany dalej; creationflags dodawane
    # do CREATE_NO_WINDOW (drivers: CREATE_SUSPENDED); bez nich zachowanie jak dotąd;
    # stdout+stderr scalone, CREATE_NO_WINDOW, callbacki w wątku wołającym;
    # anulowanie: terminate -> wait(2 s) -> kill; returncode -1; OSError gdy nie da się uruchomić;
    # anulowanie PO zakończeniu procesu nie zmienia wyniku (prawdziwy kod), tylko skraca
    # czekanie na EOF; po EOF/timeoucie wątek czytający jest zatrzymywany
    # (CancelSynchronousIo), a potok zamykany; wyjątek z callbacku zatrzymuje proces

SUCCESS_CODES: dict["install"|"uninstall", dict[int, str]]
ERROR_MESSAGES: dict[int, str]
def describe_returncode(returncode: int, operation: "install"|"uninstall") -> tuple[bool, str]

@dataclass(frozen=True) class WingetResult: returncode: int; ok: bool; cancelled: bool; message: str
def install(package_id, *, on_line=None, on_replace_last=None, cancel_event=None) -> WingetResult
def uninstall(package_id, *, on_line=None, on_replace_last=None, cancel_event=None) -> WingetResult
def is_available(timeout: float = 15.0) -> bool
def ensure_installed(*, on_line=None, on_replace_last=None, cancel_event=None) -> WingetResult
```

Semantyka `StreamParser`: `\n` kończy linię; `\r` wraca na początek bieżącej linii — następny
tekst ją nadpisuje (`REPLACE_LAST`), więc `\r\n` daje zwykłą linię, a pasek postępu
`\r##  \r### \r####\n` daje `APPEND` + kolejne `REPLACE_LAST`. Segment wysyłany jest po
napotkaniu `\r` lub `\n` (bez zgadywania częściowych linii). Usuwane są CSI, OSC (`ESC ] ... BEL`
albo `ESC \`; OSC bez terminatora kończy się na końcu linii lub na następnym ESC, więc nie
zjada kolejnych linii), sekwencje nF i wszystkie dwubajtowe ESC (Fp/Fe/Fs: `ESC 7`, `ESC =`,
`ESC c`, `ESC M` …); sekwencja lub znak wielobajtowy przecięty granicą `feed` jest składany
poprawnie (dekodowanie dopiero na pełnych segmentach). Wynik nie zależy od podziału na
chunki; z niedokończonego OSC przenoszone jest tylko `ESC ]` (treść i tak jest usuwana),
niedokończone CSI/nF najwyżej 4096 bajtów. `\b` jest emulowany, pozostałe
znaki sterujące usuwane, segmenty `strip()`-owane, puste pomijane (jak w 1.x). Koszt liniowy.

Polecenia winget: `install --id <id> --exact --silent --accept-package-agreements
--accept-source-agreements --disable-interactivity`; `uninstall --id <id> --exact --silent
--accept-source-agreements --disable-interactivity`.

**Kody powrotu.** 0 = sukces. Każdy inny kod = błąd, z wyjątkiem (źródło: wynik
`winget error <kod>` w winget v1.29.380 — symbol i opis każdego kodu):

| Kod | Symbol | Operacja | Komunikat |
|---|---|---|---|
| 0x8A15002B | `UPDATE_NOT_APPLICABLE` | install | już zainstalowany, brak nowszej wersji |
| 0x8A150061 | `PACKAGE_ALREADY_INSTALLED` | install | już zainstalowany |
| 0x8A15010D | `INSTALL_ALREADY_INSTALLED` | install | inna wersja już zainstalowana |
| 0x8A15010E | `INSTALL_DOWNGRADE` | install | zainstalowana nowsza wersja |
| 0x8A150109 | `INSTALL_REBOOT_REQUIRED_TO_FINISH` | install | zainstalowano, wymagany restart |
| 0x8A15010B | `INSTALL_REBOOT_INITIATED` | install | zainstalowano, system się zrestartuje |
| 0x8A150014 | `NO_APPLICATIONS_FOUND` | uninstall | program nie był zainstalowany |

Kody porównywane po masce `& 0xFFFFFFFF` (winget zwraca ujemne int32).

### `winstaller.core.actions`

```python
class ActionError(Exception)                  # str(exc) = komunikat dla użytkownika
@dataclass(frozen=True) class ActionOutcome:
    ok: bool; message: str; cancelled: bool = False; artifact: Path | None = None
@dataclass class ActionContext:
    item: Item; log: Callable[[str], None]; replace_last: Callable[[str], None]
    cancel_event: threading.Event; app_dir: Path; arch: Arch
    args: tuple[str, ...]  (property = item.args)
ActionFunc = Callable[[ActionContext], ActionOutcome | str | None]

ACTIONS: Mapping[str, ActionFunc] = {
    "remove_bloat", "theme_dark", "theme_light", "copy_dir_to_desktop" (args[0] = nazwa folderu),
    "copy_self_to_desktop", "reboot_to_bios", "reboot_advanced", "battery_report",
    "windows_update", "store_update",   # cienkie: wołają core/updates.py (sekcja niżej)
}
def execute(item: Item, ctx: ActionContext) -> ActionOutcome   # realne wykonanie pozycji
def install_driver(ctx, vendor_id) -> ActionOutcome  # DRIVER: drivers.install_latest;
    # DriverError -> ActionError(exc.user_message()) (komunikat + link do strony producenta)
def as_outcome(value: ActionOutcome | str | None) -> ActionOutcome  # normalizacja wyniku ActionFunc
def run_local_installer(ctx, relative: str, args) -> ActionOutcome
def launch_after_install(ctx, path: str) -> None   # brak pliku = ostrzeżenie w logu
BLOATWARE: tuple[str, ...]
def normalized_bloat_patterns(patterns=BLOATWARE) -> list[str]
def build_remove_bloat_script(patterns) -> str     # jeden skrypt PS; linie OK/ERR/NONE + TAB
BATTERY_REPORT_NAME = "battery-report.html"
THEME_REG_PATH, THEME_VALUES
```

Warstwa systemowa (do `monkeypatch` w testach; wszystkie wywołania systemu idą przez nie):
`run_command(args, *, timeout)`, `stream_command(ctx, args, *, on_line=None)`,
`start_detached(args)`, `set_user_dword(key_path, name, value)`,
`broadcast_setting_change(area)`, `desktop_dir()`, `is_frozen()`, `current_executable()`.
Pozycje winget idą przez `winget.install/uninstall` (mockować `winstaller.core.winget.install`
albo `winstaller.core.winget.stream_process`).

Zachowanie akcji:
- motyw: ustawia **oba** `AppsUseLightTheme` i `SystemUsesLightTheme` (0 ciemny / 1 jasny) przez
  `CreateKeyEx` (brak wartości nie jest błędem), potem `WM_SETTINGCHANGE "ImmersiveColorSet"`;
- `remove_bloat`: wzorce bez `*`, bez duplikatów i bez wzorców pokrytych przez krótszy
  (66 z 69), **jedno** wywołanie PowerShella (`-EncodedCommand`), log per pakiet; częściowe
  błędy usuwania = sukces z liczbą błędów w komunikacie, niezerowy kod PowerShella = błąd;
- `copy_dir_to_desktop`: `copytree(dirs_exist_ok=True)`, błąd z właściwą nazwą folderu;
  nazwa spoza katalogu programu (`..`, ścieżka bezwzględna) → `ActionError`;
- `copy_self_to_desktop`: tylko frozen (`sys.executable`), w dev `ActionError`;
- restarty: `shutdown /r /fw /t 1` i `shutdown /r /o /t 1`, kod ≠ 0 = błąd;
- raport baterii: `powercfg /batteryreport /output <pulpit>\battery-report.html`
  (fallback `app_dir`), ścieżka w `ActionOutcome.artifact` i `TaskResult.artifact`;
- `post_launch`: tylko po udanej instalacji i gdy plik istnieje.

- `windows_update` / `store_update`: opis w sekcji „`winstaller.core.updates`”;
  `UpdatesError` → `ActionError(exc.message)`, `UpdatesCancelled` → `ActionOutcome(cancelled=True)`.

`download_install`/`download_unzip_install` z 1.x **nie zostały przeniesione** — po przejściu
DaVinci na lokalny instalator nic ich nie używa.

### `winstaller.core.drivers` (sterowniki GPU)

Pozycje `kind = "driver"` (kategoria `gpu-drivers`, tylko `arch = ["x64"]`, poza profilem).
**Decyzja właściciela: bez wykrywania karty i bez sprawdzania zgodności sprzętu**, bez czystej
instalacji, bez automatycznego restartu.

```python
class DriverError(Exception)        # .message (PL), .vendor_url, user_message() = message + link
class DriverCancelled(DriverError)
@dataclass(frozen=True) class DriverRelease:
    vendor; version; url; size: int|None (dokładny, z metadanych); sha1|None; sha512|None
    release_date: str|None (ISO); source: str; approx_size: int|None (z etykiety "1.04 GB")
    notes: tuple[str, ...]   # ostrzeżenia do logu
@dataclass(frozen=True) class ExitCodeInfo: ok; message; reboot_required
@dataclass(frozen=True) class SignatureInfo: status; subject; status_message; thumbprint; signature_type
@dataclass(frozen=True) class ProcessResult: returncode; cancelled (użytkownik); timed_out
@dataclass(frozen=True) class DriverInstallResult: ok; message; cancelled; reboot_required; version; returncode
@dataclass(frozen=True) class DriverVendor:
    id; name; product; resolve(session) -> DriverRelease; download_hosts; signer ("O=...")
    install_args; exit_codes: Mapping[int, ExitCodeInfo]; install_timeout (s); vendor_url
    approx_size; download_headers;  describe_exit(code) -> ExitCodeInfo (nieznany = błąd)
VENDORS: Mapping[str, DriverVendor]   # "nvidia", "amd", "intel"

def resolve(vendor_id, session=None) -> DriverRelease      # źródło A, potem plan B
def download_verified(release, dest_dir, *, session=None, progress_cb=None, cancel_event=None
                      ) -> DownloadedFile                 # path, sha256 (odebranych bajtów), size
def download(...) -> Path                                  # = download_verified(...).path
class FileLock / def lock_file(path) -> FileLock           # blokada tylko-do-odczytu (niżej)
def sha256_file(path) -> str
def verify_signature(path, expected_subject_fragment, *, cancel_event=None, timeout=300) -> SignatureInfo
def install(spec, path, *, log, replace_last, cancel_event=None) -> ProcessResult
def install_latest(vendor_id, *, log, replace_last, cancel_event=None, session=None,
                   work_dir=None, app_dir=None) -> DriverInstallResult   # używa actions.execute
# czyste parsery (bez I/O; śmieci -> DriverError, nigdy nic innego: dekorator @_parser zamienia
# każdy inny wyjątek — zlib.error, lzma.LZMAError, zły UTF-8, RecursionError z głęboko
# zagnieżdżonego JSON-a, MemoryError, NotImplementedError z nieobsługiwanej kompresji ZIP):
parse_nvidia_lookup(text) -> DriverRelease; parse_nvidia_process_find(html) -> NvidiaListing
parse_amd_product_page(html) -> AmdProductInfo; parse_amd_release_notes(html, expected_version)
parse_amd_drivers_page(html) -> str; read_intel_dsa_zip(bytes) -> bytes
parse_intel_dsa(json_or_zip_bytes) -> DriverRelease; parse_intel_page(html) -> DriverRelease
# pomocnicze czyste: version_key, host_allowed, sanitize_filename, parse_size_text,
# format_progress, ProgressReporter, parse_signature_output, parse_distinguished_name,
# subject_matches, SIGNER_NAMES, ps_single_quoted, build_signature_script,
# nvidia_url_candidates, amd_release_notes_url, url_has_version
DRY_RUN_SAMPLES / dry_run_release(vendor)   # dane symulacji (runner, dry-run)
```

Warstwa systemowa (do `monkeypatch`): `free_space(path)`, `remove_file(path)`,
`download_dir(app_dir)`, `lock_file(path)`, `run_process(args, *, on_line, on_replace_last,
cancel_event, timeout, cwd=None, kill_tree=True)`, `_job_for(proc)`, `_resume(proc)`,
`default_session()`; `plat.system32_path`. Sieć wyłącznie przez `session` (`get`/`head` jak
`requests.Session`; wywoływane z `allow_redirects=False`, `stream`, `timeout`, `headers`).

**Przebieg `install_latest`**: resolve → log „Najnowsza wersja: X (data), rozmiar …. Źródło: …”
→ pobranie do **nowego katalogu o losowej nazwie** `winstaller-drivers-XXXXXXXX`
(`tempfile.mkdtemp`) w `%TEMP%` (gdy `%TEMP%` to `C:\Windows\SystemTemp` / `C:\Windows\Temp`,
dowiązanie albo brak zapisu — w `<app_dir>`) z paskiem przez `replace_last` (procent, MB,
prędkość; pierwsza linia bez prędkości; max ~5 aktualizacji/s; `finish()` pokazuje stan
końcowy także przy nieznanym rozmiarze) → **blokada pliku** + ponowny SHA-256 z dysku →
podpis → instalacja (przy trzymanej blokadzie) → zwolnienie blokady → **zawsze** usunięcie
pobranego pliku i katalogu. Anulowanie: przed każdym etapem,
w trakcie pobierania (co chunk 1 MiB), weryfikacji i instalacji (zabija drzewo procesów;
w logu ostrzeżenie o stanie pośrednim). Zapytania metadanych nie są przerywane w trakcie
(timeouty 10/30 s).

**Źródła wersji** (stan 2026-11-20; realne odpowiedzi w testach jako dane):

| Producent | Źródło A | Plan B | Dozwolony host pobierania |
|---|---|---|---|
| NVIDIA | `gfwsl.geforce.com/.../AjaxDriverService.php?func=DriverManualLookup&psid=131&pfid=1066&osID=135&…&upCRD=0` (RTX 5090 = gałąź główna); JSON, pola URL-encoded; filtr `IsBeta=0`, `IsCRD=0` (nie Studio), „Game Ready” w nazwie, bez `-nsd-`, najwyższa wersja | `www.nvidia.com/Download/processFind.aspx?…` (HTML) + adres ze wzorca `…/Windows/<v>/<v>-desktop-win10-win11-64bit-international-dch-whql.exe` potwierdzony HEAD (us., potem international.) | `*.download.nvidia.com` |
| AMD | strona produktu RX 9070 XT: blok „Revision Number: Adrenalin X.Y.Z …” (najnowsza wersja bez względu na etykietę WHQL/Optional) → link „Release Notes” → link podpisany „Systems with RDNA series graphics products” (pakiet uniwersalny, np. `…-win11-c.exe`); URL zawsze ze strony, a nazwa pliku musi zawierać ustaloną wersję (`url_has_version`; także gdy `<title>` notatek nie ma wersji) | wersja z nazwy pliku `minimalsetup` na `www.amd.com/en/support/download/drivers.html` + adres notatek `RN-RAD-WIN-X-Y-Z.html`; gdy notatki zawiodą, pełny pakiet ze strony produktu (`-b`, tylko RX 7000/9000; też z wersją w nazwie) z ostrzeżeniem w logu | `drivers.amd.com` |
| Intel | `dsadata.intel.com/data/en` (ZIP; czytany tylko `software-configurations.json`, utf-8-sig, limity 64 MB) — `GroupId=="785597"`, `IsBeta is False`, plik `.exe` na Windows 64-bit, `Hash` = SHA1, `Size` | strona `…/download/785597/intel-arc-iris-xe-graphics-windows.html` (Akamai przepuszcza tylko UA `python-requests/…` / `curl/…`): wersja, URL, `SHA512` | `downloadmirror.intel.com` |

Nagłówki: domyślnie `User-Agent: Winstaller/<wersja>` (www.amd.com tarpituje
`python-requests`), `Accept-Language: en-US`; pobieranie z `drivers.amd.com` wymaga
`Referer: https://www.amd.com/` (bez niego 302 na `Download-Incomplete.html` → `DriverError`).

**Weryfikacja pobrania**: przekierowania obsługiwane ręcznie (max 5), **każdy** skok musi
przejść `host_allowed`: https, port 443, bez userinfo i `%` w części hosta, w całym URL bez
`\`, białych/sterujących i nie-ASCII znaków, host = poprawna nazwa DNS (żadnych IP), a host
widziany przez `urlsplit` i przez urllib3 (`parse_url`, czyli to, z czym naprawdę łączy się
`requests`) musi być ten sam i dozwolony; URL wydania jest sprawdzany przed pierwszym
zapytaniem; HTTP 200; `Content-Type` nie `text/*`/HTML/JSON/XML; pierwsze bajty `MZ`;
`Content-Length` zgodny z rozmiarem z metadanych (wartość < 10 MiB jest ignorowana —
`drivers.amd.com` na HEAD zwraca `Content-Length: 20`); twardy limit
`MAX_DOWNLOAD_BYTES` = 4 GiB (z nagłówka i liczony w trakcie, także bez `Content-Length`);
liczba bajtów = oczekiwana; plik ≥ 10 MiB; SHA1 (Intel DSA) / SHA512 (strona Intel), gdy
znane; SHA-256 liczony zawsze (do ponownego sprawdzenia); wolne miejsce ≥ 3 × rozmiar (przed
pobraniem z metadanych/szacunku i ponownie po `Content-Length`); pozostałości poprzedniego
biegu o tej samej nazwie (plik i `.part`) usuwane przed pobraniem, `.part` przy
błędzie/anulowaniu; nazwa pliku z URL po sanityzacji (nazwy urządzeń `CON`, `NUL`, `COM0-9`,
`LPT0-9`, `AUX`, `PRN` dostają prefiks `_`). NVIDIA i AMD nie publikują sum kontrolnych —
tam chroni host + podpis.

**Podpis**: `Get-AuthenticodeSignature -LiteralPath` w PowerShellu uruchamianym po pełnej
ścieżce System32 (`-EncodedCommand`, `POWERSHELL_UTF8_PREAMBLE`, wynik jako jedna linia JSON).
Ścieżka jest literałem PowerShella w apostrofach z podwojonym **każdym** z pięciu znaków, które
PowerShell uznaje za apostrof (`' ‘ ’ ‚ ‛`, jak w jego `EscapeSingleQuotedStringContent`);
znaki sterujące → `DriverError`. Wymagane: kod wyjścia PowerShella 0, `Status == "Valid"`
**oraz** podmiot z dokładnie jednym `O=` równym (z rozróżnianiem wielkości liter) jednej z
nazw prawnych z zamkniętej listy `SIGNER_NAMES` (`NVIDIA Corporation`; `Advanced Micro
Devices` albo `Advanced Micro Devices, Inc.`; `Intel Corporation`) i dokładnie jednym `C=US`;
DN parsowany z obsługą cudzysłowów, więc `CN="O=NVIDIA Corporation"` ani `O="NVIDIA
Corporation, Evil Ltd"` nie przechodzą. Inaczej `DriverError` i plik nie jest uruchamiany.
Wybór PowerShella zamiast WinVerifyTrust+CryptQueryObject przez ctypes: mniej kodu
wskaźnikowego, ten sam silnik (WinVerifyTrust, łącznie z podpisami katalogowymi), łatwy do
testowania parser wyjścia.

**Instalacja**: `run_process` uruchamia instalator z `CREATE_SUSPENDED`, w `on_start`
tworzy Job Object (**bez** limitu `KILL_ON_JOB_CLOSE`), przypisuje do niego proces
(uchwyt z `OpenProcess(pid)` — prywatny `Popen._handle` nie jest używany; `Popen` trzyma
proces, więc pid nie może zostać ponownie użyty) i dopiero wtedy wznawia wątki (Toolhelp32,
bo `Popen` zamyka uchwyt wątku), więc żaden proces potomny nie powstaje poza jobem. Job służy
do dwóch rzeczy:

1. **Anulowanie, `install_timeout`, wyjątek** → cały job zabijany (`TerminateJobObject`),
   wynik CANCELLED / błąd (także gdy następuje w trakcie czekania z p. 2).
2. **Normalne wyjście procesu głównego** → jeśli w jobie żyją jeszcze inne procesy
   (`ActiveProcesses` z `JobObjectBasicAccountingInformation`, potem lista PID-ów z
   `JobObjectBasicProcessIdList` sprawdzona w migawce Toolhelp32 — lista zawiera też procesy
   już zakończone, których uchwyty są otwarte; `conhost.exe`, host konsoli dodawany przez
   Windows, jest pomijany), log raz „czekam na zakończenie pozostałych procesów instalatora:
   <nazwy>” i czekanie do `POST_EXIT_GRACE_SECONDS` = 180 s (w ramach `install_timeout`,
   z reakcją na anulowanie). Po limicie **bez zabijania**: ostrzeżenie w logu, że procesy
   instalatora nadal działają w tle, i `ProcessResult.left_running=True`. Kod wyjścia i
   wynik (OK/restart/błąd) zawsze z procesu głównego.

Wybór: limitu `KILL_ON_JOB_CLOSE` nie ma wcale (zamiast zdejmowania go przed zamknięciem),
więc zamknięcie uchwytu joba — także przez system, gdy Winstaller ulegnie awarii albo
zostanie zabity — nigdy nie przerywa trwającej instalacji sterownika; zabijanie jest zawsze
jawnym `TerminateJobObject`. `FileLock` jest trzymany do końca czekania; gdy po limicie pliku
lub katalogu nie da się usunąć, to nie jest błąd instalacji — log mówi, gdzie plik został.
Gdy job się nie uda, proces i tak jest wznawiany, a log ostrzega (wtedy nie ma ani zabijania
drzewa, ani czekania na potomków). `cwd` = katalog pobrania.

**Model zagrożeń modułu sterowników i zabezpieczenia** (Winstaller działa z prawami
administratora, więc wszystko, co uruchomi, działa z tymi prawami):

| Zagrożenie | Zabezpieczenie |
|---|---|
| podmieniona/zatruta odpowiedź metadanych, adres spoza producenta | lista hostów na każdym skoku, zgodność `urlsplit` i urllib3, brak `\`/`%`/spacji/nie-ASCII, nazwy DNS zamiast IP; AMD: wersja w nazwie pliku |
| podmieniony plik na serwerze / w drodze | https, `MZ`, rozmiar, SHA1/SHA512 (Intel), podpis Authenticode z dokładną nazwą wydawcy i `C=US` |
| proces tego samego użytkownika (bez podniesienia) podmienia instalator między weryfikacją a uruchomieniem (TOCTOU) | nowy katalog z `mkdtemp` (ACL: SYSTEM, Administratorzy, właściciel — po podniesieniu właścicielem jest grupa Administratorzy), odrzucane dowiązania; `FileLock`: plik otwarty z `FILE_SHARE_READ` (bez zapisu/usunięcia/zmiany nazwy) i katalog bez `FILE_SHARE_DELETE` (bez zmiany nazwy/usunięcia) od zaraz po pobraniu do końca instalacji; SHA-256 liczony przy pobieraniu i ponownie z dysku po założeniu blokady |
| podłożony `powershell.exe`/`shutdown.exe` obok programu lub w katalogu bieżącym | programy systemowe po pełnej ścieżce `system32_path` |
| wstrzyknięcie poleceń przez ścieżkę w skrypcie PowerShella | literał z podwojonymi wszystkimi pięcioma apostrofami, odrzucane znaki sterujące |
| zawieszony/wrogi instalator, procesy potomne po przerwaniu | timeout, Job Object od pierwszej instrukcji (`CREATE_SUSPENDED`), `TerminateJobObject` przy anulowaniu/timeoucie |
| przerwanie instalacji w połowie przez samego Winstallera (zewnętrzne exe kończy się przed właściwym instalatorem, awaria Winstallera) | brak `KILL_ON_JOB_CLOSE`; po wyjściu procesu głównego czekanie do 180 s na resztę joba, potem pozostawienie jej w spokoju |
| uszkodzone/złośliwe dane (ZIP-bomba, głęboki JSON, zły UTF-8) | limity rozmiaru (strony 8 MB, ZIP/JSON 64 MB, pobranie 4 GiB), `@_parser` → zawsze `DriverError` → plan B |
| nazwy urządzeń, wyjście poza katalog | sanityzacja nazwy (`[A-Za-z0-9._-]`, `.exe`, prefiks dla `CON`…) |

Sprawdzone 2026-11-20 na kopii nieszkodliwego, podpisanego `python.exe` (nie na instalatorze):
przy trzymanej blokadzie zapis, dopisanie, usunięcie i zmiana nazwy pliku, zmiana nazwy i
usunięcie katalogu oraz zmiana nazwy katalogu nadrzędnego są odrzucane, odczyt działa,
CreateProcess zablokowanego exe (przez `run_process`: suspended + job + resume) działa, a
uruchomiony proces czyta własny plik exe (jak samorozpakowujący się instalator). Możliwe jest
utworzenie twardego dowiązania — nie daje ono zapisu (tryb współdzielenia dotyczy pliku).

| Producent | Argumenty | Timeout | Kody wyjścia |
|---|---|---|---|
| NVIDIA | `-s -noreboot -noeula` (7z SFX przekazuje do setup.exe; instaluje domyślne komponenty, w tym NVIDIA App) | 30 min | 0 sukces (restart zalecany), 1 sukces + wymagany restart, inne = błąd |
| AMD | `-INSTALL` (bez `-boot`, bez `-factoryresetInstall`) | 40 min | 0 sukces (restart zalecany), inne = błąd |
| Intel | `-s --terminateProcesses` (WinRAR SFX → Installer.exe; bez `-f`, `-b`; `-s` pierwsze) | 30 min | 0 sukces; 14 / 18 sukces + wymagany restart / pełne wyłączenie; 2 / 17 system już się restartuje / wyłącza (sukces z komunikatem); błędy: 1 ogólny, 5 platforma, 6 zamknięty, 7 parametry, 8 brak pasującego urządzenia, 9/10 podpis, 11 miejsce, 13 restart przed instalacją, 15 procesy, 16 inna instalacja; pozostałe = błąd |

Dry-run (`runner.make_dry_run_executor`): ten sam układ logu (ustalanie wersji → pasek
pobierania → podpis → uruchomienie → „kod 0”) na danych `DRY_RUN_SAMPLES`, bez sieci i procesów.

**ZNANE NIEWIADOME — wymagają testu na prawdziwym sprzęcie** (nie było wolno uruchamiać
instalatorów ani pobierać całych pakietów):

- czy zewnętrzne exe faktycznie przekazują flagi: NVIDIA (7z SFX → `setup.exe -s -noreboot
  -noeula`), AMD (`-INSTALL` w pakiecie `-c`), Intel (WinRAR SFX → `Installer.exe -s
  --terminateProcesses`) i czy instalacja jest naprawdę cicha (bez okien/EULA);
- kody wyjścia przy braku karty danego producenta (NVIDIA, AMD — nieudokumentowane; Intel
  wg readme: 8) i przy próbie instalacji starszej wersji niż zainstalowana (Intel w trybie
  cichym bez `--overwrite` „przerywa” — nie wiadomo jakim kodem);
- czy kod 0 AMD zawsze oznacza sukces (i czy AMD zwraca coś dla „wymagany restart”);
- czy NVIDIA zwraca 1 dla „wymagany restart” i czy pakiet desktop instaluje się na laptopach
  (lookup dla notebooków zwraca osobny plik `-notebook-`; używamy wyłącznie `-desktop-`);
- zachowanie Intel przy nieudanym wewnętrznym sprawdzeniu integralności (modalne okno →
  chroni timeout + wcześniejsza weryfikacja SHA i podpisu) i czy pakiet faktycznie nie
  instaluje się z `C:\Windows\SystemTemp`;
- `Content-Length` przy zwykłym GET z `drivers.amd.com` (HEAD zwraca 20 B; Range podaje
  pełny rozmiar w `Content-Range`) — kod toleruje nieprawdopodobne wartości;
- czy pakiety po rozpakowaniu mieszczą się w 3 × rozmiar (rozpakowanie idzie do `%TEMP%`
  / katalogów producenta, nie zawsze na ten sam dysk co pobranie);
- Intel: w danych DSA pakiet Arc ma `"AllowSilentInstallation": false` i
  `"InstallOptions": "--nouninstall"`. Flag **nie** zmieniamy (readme pakietu dokumentuje
  `-s`), ale trzeba sprawdzić, czy instalacja cicha naprawdę działa i co oznacza
  `--nouninstall` (prawdopodobnie: bez odinstalowania poprzedniej wersji);
- czy samorozpakowanie instalatorów działa, gdy plik exe jest otwarty przez Winstaller z
  `FILE_SHARE_READ` (sprawdzone tylko dla procesu czytającego własny exe zwykłym odczytem;
  SFX otwierający siebie z wyłącznym dostępem albo do zapisu by się nie uruchomił — wtedy
  instalacja kończy się błędem, nie uruchomieniem niezweryfikowanego pliku);
- czy zewnętrzne exe (SFX NVIDIA/AMD/Intel) czekają na właściwy instalator
  (`setup.exe`/`Installer.exe`) i oddają jego kod wyjścia; jeśli nie, kod 0 SFX znaczy tylko
  „rozpakowano i uruchomiono”, a Winstaller czeka na resztę joba najwyżej 180 s — dłuższa
  instalacja zostanie zgłoszona jako zakończona z ostrzeżeniem, że procesy działają w tle;
- czy instalatory zostawiają po sobie długo żyjące procesy (np. NVIDIA App / AMD Software
  uruchomione na końcu, aktualizatory) — wtedy każde zakończenie kosztuje do 180 s czekania i
  kończy się ostrzeżeniem (procesy nie są zabijane; usługi startowane przez Menedżera usług
  nie należą do joba);
- ACL katalogu z `mkdtemp` sprawdzona tylko bez podniesienia (wpisy SYSTEM, Administratorzy,
  „właściciel”); to, że po podniesieniu właścicielem jest grupa Administratorzy (więc proces
  bez podniesienia nie ma dostępu), wynika z domyślnego właściciela tokenu administratora i
  nie było sprawdzone.

### `winstaller.core.updates` (Windows Update i Microsoft Store)

Kategoria `updates` „Aktualizacje” jest **pierwsza w katalogu** (runner wykonuje w kolejności
katalogu, więc aktualizacje startują przed wszystkim), pokazywana jako pierwsza sekcja strony
**System**. Pozycje (`kind = "action"`, x64 i arm64, obie na początku profilu „Benchmark
starter”): `windows-update` (akcja `windows_update`, z `confirm`) i `store-update` (akcja
`store_update`, bez `confirm`). **Decyzje właściciela (2026.11.22)**: tryb „w tle” (zadanie
kończy się zaraz po zleceniu), zakres „wszystko”: zwykłe, opcjonalne/Preview i sterowniki z
WU, także klasy Display; nigdy automatyczny restart.

```python
class UpdatesError(Exception)        # .message (PL), .kind (ErrorKind), .hresult ("0x8024402C"|None)
class UpdatesCancelled(UpdatesError) # kind = "cancelled"
ErrorKind = Literal["network", "service_disabled", "policy", "busy", "reboot", "server",
                    "not_elevated", "timeout", "cancelled", "format", "start", "store",
                    "blocked", "other"]
HRESULT_KINDS: Mapping[int, ErrorKind]; def error_kind(hresult) -> ErrorKind
def describe_wu_error(hresult, detail="") -> UpdatesError      # polski komunikat + „(kod 0x…)”
@dataclass(frozen=True) class WindowsUpdateInfo:
    id (GUID, małe litery); revision; title; kb: tuple[str, ...]; type: "software"|"driver"
    optional (DeploymentAction=OptionalInstallation); browse_only; downloaded; categories
    interactive (CanRequestUserInput); driver_class; driver_model; driver_vendor
    is_driver; key; label() -> "KB… — tytuł [opcjonalna] [sterownik]"   # bez rozmiarów
@dataclass(frozen=True) class WindowsUpdateScan:
    updates; reboot_pending; is_admin: bool|None (None = nieznane); seconds; criteria
    result_code; warnings: tuple[str, ...] (do logu); skipped: int; optional_count; driver_count
@dataclass(frozen=True) class BackgroundInstall:
    log_path; status_path|None; pid|None; already_running; install_all; finished
@dataclass(frozen=True) class StoreAppInfo:
    product_id; package_family_name; install_type; state; error_code; failed; name; label()
@dataclass(frozen=True) class StoreUpdateResult:
    method: "AppInstallManager"|"store-cli"; items (zlecone); failed (stan Error/kod błędu)
    lines (store-cli); found_count; auto_update_setting; queue
    confirmed (property: True tylko dla WinRT); count (property)

def scan_windows_updates(*, cancel_event=None, timeout=SCAN_TIMEOUT) -> WindowsUpdateScan  # 180 s
def select_updates(updates, *, skip_drivers=SKIP_DRIVERS, skip_display_drivers=SKIP_DISPLAY_DRIVERS,
                   skip_interactive=SKIP_INTERACTIVE) -> list[WindowsUpdateInfo]  # wszystkie False
def background_install_running() -> bool                 # sam mutex (bez potwierdzenia)
def running_install() -> BackgroundInstall               # potwierdzenie plikiem statusu albo
                                                         # UpdatesError(kind="blocked"); tylko odczyt
def start_windows_update_install(*, update_ids=(), install_all=False, skip_drivers=False,
                                 skip_display_drivers=False, skip_interactive=False,
                                 now=None) -> BackgroundInstall
    # pusta lista bez install_all -> UpdatesError; lista > MAX_IDS_CHARS -> install_all
def verify_background_start(started, *, cancel_event=None, timeout=START_CONFIRM_TIMEOUT=10,
                            poll=0.1) -> BackgroundInstall
def update_store_apps(*, cancel_event=None, timeout=STORE_TIMEOUT, log=None) -> StoreUpdateResult
def list_store_queue(*, cancel_event=None, timeout=STORE_TIMEOUT) -> StoreUpdateResult  # tylko odczyt
def log_dir() -> Path; def existing_log_dirs() -> list[Path]   # tworzy / tylko czyta
# postęp instalacji w tle (tylko odczyt; opis niżej, „Postęp instalacji w tle”):
@dataclass(frozen=True) class InstallItem:
    title; status: ItemStatus; id ("" gdy znane tylko z logu); revision; hresult; reboot_required
    done / ok / failed (properties)
@dataclass(frozen=True) class InstallProgress:   # done = finished|stale|unknown (unknown: końcowy)
    state: "running"|"finished"|"stale"|"unknown"; phase: "searching"|"downloading"|
    "installing"|"finished"|"unknown"; total; current; current_title; download_total;
    download_current; items: tuple[InstallItem, ...]; reboot_required; exit_code; error;
    started; finished; updated; phase_started; step_started (aware, czas lokalny);
    log_path; status_path; pid; alive: bool; from_log: bool
    done / ok_count / failed_count / skipped_count / count / succeeded / steps_done /
    steps_total / key (nazwa pliku statusu) / reference_time (properties)
@dataclass(frozen=True) class InstallLogSummary:
    exit_code; items; reboot_required; pid; found; started; finished; ok_count; failed_count
parse_install_status(text, *, path=None) -> InstallProgress    # czysty, @_parser
parse_install_log(text) -> InstallLogSummary                    # czysty, @_parser
def read_install_progress(status_path) -> InstallProgress       # tylko UpdatesError
def find_background_install() -> InstallProgress | None         # najnowszy z zaufanych katalogów
def describe_progress(progress, *, mention_restart=True) -> tuple[str, str]  # (tytuł, podtytuł) PL
def system_boot_time() -> datetime; def read_shared(path, limit) -> bytes    # warstwa systemowa
PROGRESS_HINT, ITEM_STATUSES, ITEM_OK, ITEM_FAILED, ITEM_DONE, READ_ATTEMPTS, MAX_STATUS_BYTES,
LOST_MESSAGE, LOG_AHEAD, INSTALLER_START_BEFORE/AFTER
# needs_restart (property), InstallLogSummary.positions/phase/current_index/current_title/last
def process_create_time(pid) -> datetime | None; def is_installer_process(pid, started) -> bool
# czyste (bez I/O; śmieci -> UpdatesError, nigdy nic innego — dekorator @_parser):
parse_wu_scan(text, *, stderr="", returncode=0) -> WindowsUpdateScan
parse_store_update(text, *, stderr="", returncode=0) -> StoreUpdateResult
parse_store_cli_output(text, returncode=0) -> StoreUpdateResult
# pomocnicze czyste: clean_powershell_stderr (CLIXML), clean_store_lines, compact_script,
# powershell_args(script, *, hidden=False), hresult_value, format_hresult, validate_update_ids,
# log_file_names(now), latest_log(folder), security_is_trusted(owner, aces), describe_exit
# stałe: CHILD_ENV_ALLOWED, LOG_DIR_SDDL, TRUSTED_OWNERS, TRUSTED_WRITERS, EXIT_MESSAGES,
# BLOCKED_MESSAGE, MAX_IDS_CHARS
DRY_RUN_WINDOWS_UPDATES, DRY_RUN_STORE_APPS, DRY_RUN_LOG_PATH   # dane symulacji (runner)
```

Warstwa systemowa (do `monkeypatch`): `run_powershell(script, *, env=None, timeout,
cancel_event=None) -> (returncode, stdout, stderr)` (ustawione wcześniej anulowanie → nic nie
startuje; anulowanie w trakcie → zabicie procesu i `UpdatesCancelled`; timeout → zabicie i
`UpdatesError(kind="timeout")`), `start_background(args, env) -> pid`, `mutex_exists(name)`,
`run_store_cli(args, *, timeout, cancel_event) -> (returncode, output)`, `log_dir()`,
`existing_log_dirs()`, `create_secure_dir(path, sddl)`, `read_dir_security(path)`,
`dir_is_trusted(path)`, `process_image(pid)`, `process_running(pid)`,
`is_live_powershell(pid)`, `background_exit_code(pid)`, `read_status(path)`,
`read_shared(path, limit)` (odczyt z udostępnianiem odczytu/zapisu/**usuwania** — patrz
„Pliki logu i statusu”), `system_boot_time()`. Skrypty:
`update_scripts` (`WU_SCAN_SCRIPT`, `WU_INSTALL_SCRIPT`, `STORE_SCRIPT`, `ALL_SCRIPTS`,
`WU_CRITERIA`, `WU_FALLBACK_CRITERIA`).

**Dlaczego WUA (`Microsoft.Update.Session`), a nie USO/winget/MDM** (zbadane 2026-11-22 na
Windows 11 Insider 26300): `UsoClient` w tej kompilacji nie ma poleceń instalacji i nie obejmuje
aktualizacji opcjonalnych; `winget` nie widzi aplikacji Store; most MDM (WMI
`MDM_EnterpriseModernAppManagement`) wymaga konta SYSTEM i tylko skanuje. Windows Update idzie
więc przez WUA z PowerShella 5.1, a Store przez WinRT `AppInstallManager`.

**Kryterium wyszukiwania.** Aktualizacje Preview/opcjonalne (w Ustawieniach „Pobierz i
zainstaluj”) są widoczne tylko z wyrazem `DeploymentAction`; bez niego WUA zakłada
`'Installation'`. Główne (udokumentowana składnia: OR tylko na najwyższym poziomie, AND wiąże
mocniej): `IsInstalled=0 and IsHidden=0 and DeploymentAction='Installation' or IsInstalled=0
and IsHidden=0 and DeploymentAction='OptionalInstallation'`. Zapasowe, tylko gdy WUA odrzuci
główne kodem `0x80240032` (WU_E_INVALID_CRITERIA) lub `0x80070057`: nieudokumentowane
`… and DeploymentAction=*` (na maszynie testowej oba dają ten sam komplet). Kolejność wybrana
tak, bo wartość `*` nie ma oparcia w dokumentacji i może przestać działać bez ostrzeżenia, a
jawne OR jest opisane. Wynik bez duplikatów po `UpdateID`+`RevisionNumber` (w skrypcie i w
parserze). Wyszukiwanie działa bez admina. `MaxDownloadSize` nie jest odczytywany ani
pokazywany (dla LCU to suma wariantów, ~97 GB). Skan ma limit 180 s i reaguje na Stop
(zabicie PowerShella).

**Przebieg `windows_update`**: mutex `Global\Winstaller.WindowsUpdate` istnieje → szukamy
w zaufanych katalogach logów (`existing_log_dirs`, tylko odczyt) naszego pliku statusu ze
`state = running` i PID-em żyjącego `powershell.exe`; jest → „Instalacja aktualizacji już trwa
w tle” + ścieżka logu, OK, bez skanu; nie ma → zadanie **FAILED** („Inny proces blokuje
aktualizację Windows…”), bo mutex mógł założyć dowolny proces. Inaczej: skan → ostrzeżenia
skanu w logu (ResultCode 3, pominięte wadliwe pozycje) → lista (`  KB… — tytuł [opcjonalna]
[sterownik]`) → pusta: „System jest aktualny…” (OK; same pominięte pozycje → FAILED) →
`reboot_pending`: ostrzeżenie → `scan.is_admin is False` (proces bez podniesienia, np.
`--no-elevate`): **FAILED** „wymaga uprawnień administratora”, nic nie startuje → start procesu
w tle z allow-listą ID ze skanu → **weryfikacja startu** (`verify_background_start`, niżej) →
OK z komunikatem „Znaleziono N aktualizacji (w tym M opcjonalnych); instalacja uruchomiona w
tle. Postęp: zakładka Przebieg (blok „W tle”) i log <ścieżka>. Po zakończeniu może być
wymagany restart.” i `artifact` = log (dry-run: `DRY_RUN_LOG_PATH`). Z `artifact` UI ustala
plik statusu do śledzenia (`.json` obok logu). Panel Ustawienia → Windows Update **nie
pokazuje** instalacji zleconej przez WUA z innego procesu (sprawdzone 2026-11-22 na
sprzęcie) — stąd blok „W tle” w Winstallerze.

**Weryfikacja startu** (`verify_background_start`): do 10 s (Stop przerywa — wtedy
`CANCELLED` z informacją, że instalacja mogła już ruszyć) czytany jest plik statusu. Sukces
tylko gdy `state = running` z naszym PID-em i proces żyje albo `state = finished` z kodem 0.
Proces zakończony od razu (status `finished` z kodem ≠ 0 albo brak statusu — kod wyjścia z
`Popen`) → FAILED z komunikatem dla kodu (`EXIT_MESSAGES`: 3 inna instancja, 4 brak uprawnień,
5 wymagany restart przed instalacją, 6 WU zajęty, 7 złe parametry, inne) i ścieżką logu; brak
potwierdzenia w czasie → FAILED. Skrypt sprawdza uprawnienia **przed** pierwszym zapisem
statusu, więc kod 4 nigdy nie wygląda jak `running`; kody 5/6 zapadają później (po
wyszukiwaniu) — są w logu i statusie, nie w wyniku zadania. Limit 10 s (zamiast ~5) z powodu
zimnego startu PowerShella na wolnych dyskach; potwierdzenie zwykle przychodzi po 0,2–0,5 s.

**Równoległe starty w jednym procesie**: sekcja krytyczna (`threading.Lock`) wokół
przygotowania i startu; drugi wywołujący po wejściu porównuje licznik startów z wartością
sprzed czekania — jeśli ktoś uruchomił proces w międzyczasie (jego mutex mógł jeszcze nie
powstać), dostaje `already_running` z tym samym logiem. Między procesami chroni mutex skryptu.

**Proces w tle** (`start_windows_update_install`): `powershell.exe` z pełnej ścieżki System32,
`-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand
<WU_INSTALL_SCRIPT>`; `creationflags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP |
CREATE_BREAKAWAY_FROM_JOB` (przy `OSError` z `winerror == 5` — job bez
`JOB_OBJECT_LIMIT_BREAKAWAY_OK` — ponowienie bez BREAKAWAY), `stdin/stdout/stderr=DEVNULL`,
`close_fds=True`, `cwd` = System32, środowisko z białej listy (niżej). Parametry
**wyłącznie przez zmienne środowiskowe**:
`WINSTALLER_WU_LOG`, `WINSTALLER_WU_STATUS` (ścieżki bezwzględne, inaczej kod 7),
`WINSTALLER_WU_IDS` (GUID-y po przecinku, walidowane w Pythonie), `WINSTALLER_WU_ALL`
(„1” = wszystko, co znajdzie; pusta lista bez tej flagi → kod 7; Python przechodzi na „all”
z ostrzeżeniem w logu, gdy lista przekracza `MAX_IDS_CHARS` = 30 000 znaków),
`WINSTALLER_WU_SKIP_DRIVERS`, `WINSTALLER_WU_SKIP_DISPLAY_DRIVERS`,
`WINSTALLER_WU_SKIP_INTERACTIVE` („1” = pomiń; domyślnie wszystkie „0”). Skrypt tworzy mutex i
zaraz ogranicza jego DACL do Administratorów i SYSTEM (chroniony,
`MutexSecurity`), sprawdza uprawnienia (kod 4 przed pierwszym statusem), sam wyszukuje
ponownie (to samo kryterium), instaluje tylko pozycje z allow-listy, `AcceptEula()`, czeka do
10 min, gdy `IsBusy`, kończy kodem 5 przy `RebootRequiredBeforeInstallation`, pobiera
**pojedynczo** (tylko niepobrane; dla każdej nowy `IUpdateDownloader` i synchroniczne
`Download()` na kolekcji jednoelementowej — to samo wywołanie, które 2026-11-22 zadziałało na
sprzęcie dla całej listy; wynik pozycji wg `IsDownloaded`, błąd jednej nie przerywa reszty),
instaluje **pojedynczo** (`IUpdateInstaller`,
`ForceQuiet`, `AllowSourcePrompts = $false`; aktualizacja z wyłącznym trybem instalacji musi
być sama, a jeden błąd nie przerywa reszty), wyjątki per aktualizacja, nigdy restart. Kody
wyjścia: 0 OK/nic do zrobienia, 1 nieoczekiwany błąd, 2 część się nie udała, 3 inna instancja
(mutex), 4 brak admina, 5 wymagany restart przed instalacją, 6 WU zajęty, 7 złe parametry.
Wynik pobrania i instalacji: tylko `ResultCode` 2/3 to sukces (pobranie: dodatkowo
`IsDownloaded`, odczytywane wewnątrz `try`); każdy inny kod (także 0/1 po synchronicznym
wywołaniu) to błąd i `$anyFailed` → kod wyjścia 2. Pozycja z błędem pobrania, którą WUA mimo to
uznaje za pobraną (`IsDownloaded`), jest w fazie instalacji instalowana (wynik instalacji
nadpisuje status). W `finally` pozycje niedokończone dostają status końcowy: `pending`/
`downloaded` → `skipped`, `downloading`/`installing` → `aborted` (także przy kodach 5/6 i po
wyjątku). Długość: skrypt po `compact_script` (bez wcięć, pustych linii i komentarzy) daje ok.
27,5 tys. znaków `-EncodedCommand` (cały wiersz poleceń 27 677 = 84,5 % limitu; test pilnuje
< 85 %). Żeby zmieścić znacznik `[i]`, domknięcie statusów i warunki `-in 2, 3`, skrócono
komunikaty logu dla kodów 3–6 i pomijania pozycji oraz w regule `MutexAccessRule` zamieniono
typy wyliczeniowe na napisy `'FullControl', 'Allow'` (konwersja PowerShella; sprawdzone
2026-11-22 na prywatnym muteksie w scratchpadzie: reguły SYSTEM i Administratorzy
FullControl Allow). **Dalsze dopisywanie wymaga innego rozwiązania** — propozycja: skrypt
przez standardowe wejście (`powershell.exe -Command -`, potok zamykany po zapisie; nadal bez
pliku na dysku) zamiast `-EncodedCommand`, co zdejmuje limit wiersza poleceń; wymaga testu
uruchamiania odłączonego procesu z potokiem — poniżej limitu 32 767 (`powershell_args` sprawdza całość i rzuca
`UpdatesError`; `--smoke` buduje wszystkie trzy i wypisuje maksimum). `-EncodedCommand` zamiast
pliku `.ps1`: brak pliku na dysku = nic do podmiany między zapisem a uruchomieniem.

`CanRequestUserInput`: WUA oznacza tak większość sterowników (na maszynie testowej 3 z 3), a
Windows Update instaluje je po cichu — pomijanie takich pozycji wyłączyłoby sterowniki wbrew
decyzji właściciela, więc domyślnie **nie** są pomijane (`ForceQuiet`, wynik per aktualizacja);
przełącznik `skip_interactive` zostaje.

**Katalog logów** (`log_dir`). Proces **podniesiony**: `%ProgramData%\Winstaller\logs`;
każdy poziom (`Winstaller`, `logs`) tworzony osobno przez `CreateDirectoryW` z
`SECURITY_ATTRIBUTES` z SDDL `D:P(A;OICI;FA;;;BA)(A;OICI;FA;;;SY)(A;OICI;0x1200a9;;;BU)`
(chroniony DACL nadany w chwili tworzenia: pełne prawa Administratorzy i SYSTEM, odczyt
Użytkownicy, bez dziedziczenia; bez właściciela w SDDL — domyślnym właścicielem obiektów
tworzonych przez podniesiony token administratora jest grupa Administratorzy). Gdy poziom
**już istnieje**, jest używany tylko jeśli: nie jest punktem reparse (sprawdzane przed i po
próbie utworzenia; `CreateDirectoryW` na junction kończy się „już istnieje” i niczego nie
tworzy pod celem), właścicielem jest Administratorzy/SYSTEM/TrustedInstaller i żaden wpis
„allow” DACL nie daje praw zapisu (`FILE_WRITE_DATA`, `FILE_APPEND_DATA`, `WRITE_EA`,
`DELETE_CHILD`, `WRITE_ATTRIBUTES`, `DELETE`, `WRITE_DAC`, `WRITE_OWNER`, `GENERIC_WRITE/ALL`,
`MAXIMUM_ALLOWED`) nikomu poza Administratorzy/SYSTEM; nieznane typy wpisów = niezaufany
(`GetNamedSecurityInfoW` + `GetAce`, nie parsowanie tekstu `icacls`). Niezaufanego drzewa
**nie naprawiamy** (żadnego `icacls /T`, żadnego tworzenia przez link): powstaje świeży
`%ProgramData%\Winstaller-<8 znaków hex>\logs` z tym samym DACL, ostrzeżenie w logu Pythona i
w pierwszych liniach logu instalacji; gdy i to zawiedzie → `UpdatesError`. Proces
**niepodniesiony** (tryb dev `--no-elevate`; instalacji i tak się wtedy nie uruchamia):
`%LOCALAPPDATA%\Winstaller\logs` bez zmian ACL, z odrzucaniem linków. `running_install` i
`existing_log_dirs` tylko czytają (nie tworzą katalogów, nie zmieniają ACL).

**Pliki logu i statusu**: nazwy `windows-update-RRRR.MM.DD-GGMMSS.log` i `.json` (przy
kolizji sufiks `-2`…; `latest_log` sortuje naturalnie: znacznik czasu, potem sufiks, potem
czas modyfikacji). Log: UTF-8 z BOM (Notatnik), linia po linii z czasem, po polsku, tekst
oczyszczany ze znaków sterujących, separatorów linii i znaków bidi/zero-width (`Get-CleanText`
w skrypcie, `_one_line` w Pythonie); pierwszą linię zapisuje Winstaller (`open(..., "x")` —
wyłączne utworzenie; przy nieudanym starcie procesu plik jest usuwany), resztę skrypt. Linie
logu, na których opiera się `parse_install_log`: `Start instalacji aktualizacji w tle (PID n).`,
`Znaleziono n aktualizacji.`, `Pobieranie k z n [i]: <tytuł>` + `-> pobrano` / `-> nie
pobrano (0x…)`, `Instalacja [i]: <tytuł>` + `-> [i] OK|OK z błędami|błąd|przerwano|kod N
(0x…)[, wymaga restartu]` albo `-> [i] błąd (0x…): <komunikat>` (wyjątek), `Nie pobrano,
pomijam instalację [i]: <tytuł>`, dokładnie `Aby dokończyć instalację, uruchom ponownie
komputer (Winstaller nie robi tego automatycznie).`, `Koniec (kod n).` — `[i]` to pozycja
1..N w `updates[]` statusu (logi sprzed znacznika: bez `[i]`). Każdy wzorzec jest
dopasowywany do **całej** treści linii po znaczniku czasu, więc tytuł aktualizacji nie
podrobi wyniku, restartu ani końca.

Status JSON (UTF-8 bez BOM, znacznik `"winstaller": "wu-install"`, zapisywany przy **każdym
kroku**):

| Pole | Znaczenie |
|---|---|
| `state` | `running` → `finished` (w `finally`, zawsze) |
| `phase` | `searching` → `downloading` (gdy jest co pobrać) → `installing` → `finished` |
| `pid`, `started`, `finished`, `exitCode`, `error`, `rebootRequired` | jak wcześniej; `rebootRequired` globalnie |
| `updated` | czas ostatniego zapisu (ustawiany w `Save-Status`) |
| `phaseStarted`, `stepStarted` | początek fazy / bieżącej aktualizacji (ISO 8601 z przesunięciem) |
| `total`, `current` | liczba aktualizacji do zrobienia (po wyszukaniu) i indeks 1..N bieżącej (kolejność instalacji) |
| `downloadTotal`, `downloadCurrent` | ile trzeba pobrać i która z nich jest pobierana („Pobieranie 2 z 5”) |
| `currentTitle` | tytuł bieżącej aktualizacji (`null` po końcu) |
| `log` | ścieżka logu (Python i tak bierze `.log` obok pliku statusu) |
| `updates[]` | **od razu po wyszukaniu**: `id`, `revision`, `title`, `status` (`pending`, `downloading`, `downloaded`, `installing`, `succeeded`, `succeeded_with_errors`, `failed`, `aborted`, `not_downloaded`, `error`, `unknown`, `skipped`), `hresult`, `rebootRequired` |

Zgodność wstecz: pliki sprzed tej zmiany (bez `phase`/`total`/`status`; wyniki w `result`)
czytają się — `phase = "unknown"`, wynik pozycji z `result`, `total` = długość listy.

Zapis atomowy (`Save-Status`): stary `.tmp` usuwany, nowy tworzony `CreateNew`
(`FileShare.None`), potem `File.Replace(tmp, status, [NullString]::Value)` albo `Move`, gdy
statusu jeszcze nie ma; do 20 prób co 50 ms. **Błąd z 2026-11-22 (potwierdzony na
sprzęcie):** wcześniej trzeci argument był `$null`, a PowerShell przekazuje `$null` do
parametru typu `string` jako `""` → `File.Replace` rzucał „Ścieżka ma niedozwolony format”.
Pierwszy zapis szedł gałęzią `Move` i działał, każdy kolejny padał, więc status zostawał na
`running` z pustą listą (a obok `.json.tmp` z poprawną treścią). Instalacja się udała (6/6),
tylko wynik nie trafił do statusu. Druga pułapka: `File.Replace` potrzebuje prawa usuwania
pliku docelowego, a Python `open()` nie udostępnia `FILE_SHARE_DELETE` — czytelnik w
Pythonie trzymający plik w chwili zapisu powodował błąd współdzielenia; dlatego Python czyta
przez `read_shared` (`CreateFileW` z `FILE_SHARE_READ|WRITE|DELETE`) z ponowieniem
(w trakcie `ReplaceFile` plik bywa przez mikrosekundy „nie znaleziony” albo zablokowany).

Sprawdzone 2026-11-22 w PowerShell 5.1 na nieszkodliwym skrypcie (funkcje `Save-Status` /
`Set-Phase` / `Set-Step` wycięte z `WU_INSTALL_SCRIPT`, plik w scratchpadzie, nic z Windows
Update): stara wersja — 20 z 21 zapisów z tym samym błędem co na sprzęcie; nowa — 61, 101,
201 i 401 zapisów pod rząd bez błędu, także gdy równolegle czyta proces Pythona w pętli:
`read_shared` 0 błędów zapisu (czytelnik widział 0 niepoprawnych JSON-ów, ~150 chwilowych
„nie znaleziono”/błędów współdzielenia — stąd ponowienie), `read_install_progress` 532/532
odczytów bez wyjątku; z czytelnikiem przez zwykły `open()` 71 zapisów z 101 padło mimo
ponowień (dowód, że udostępnianie usuwania jest potrzebne). Pozostałe skrypty nie przekazują
`$null` do parametrów `string` metod .NET (jedyne `$null` w wywołaniu: `MethodInfo.Invoke($null,
…)` — parametr typu `object`).

**Postęp instalacji w tle** (`read_install_progress`, `find_background_install`; tylko
odczyt). `read_install_progress(status)`: do 5 prób (co 50 ms) odczytu i parsowania; stan z
pliku `running` jest weryfikowany procesem (`is_installer_process`): żywy `powershell.exe` o
tym PID, `started` po ostatnim rozruchu (`system_boot_time()` − 5 s) **i** czas utworzenia
procesu (`process_create_time`, `GetProcessTimes`) w przedziale [`started` − 10 min,
`started` + 60 s] — PID użyty ponownie przez inny PowerShell (choćby skan samego
Winstallera) nie jest instalatorem; gdy czasu nie da się odczytać, decyduje sama nazwa
obrazu. Ta sama reguła w `running_install()`. Wtedy `alive = True`; inaczej **`stale`**:
proces skończył się bez zapisania wyniku. Wynik jest odtwarzany z logu obok
(`parse_install_log`; log innego PID-u jest ignorowany): ostatnie `Koniec (kod N).` →
`exit_code`, `phase = finished`, wyniki pozycji, restart, `from_log = True`. Wyniki z logu
trafiają do pozycji statusu po znaczniku `[i]` (gdy tytuł się zgadza), w starych logach po
**kolejności wystąpień** (k-te „Instalacja: T” → k-ta pozycja o tytule T — sterowniki często
mają te same tytuły); niedopasowane są dopisywane. Brak `Koniec` → „Instalacja przerwana”
(pozycje w trakcie = `[PRZERWANO]`, liczone jako nieudane; nieruszone = `[POMINIĘTO]`).
`running` bez PID-u → wynik z logu, a bez niego **`unknown`** — stan **końcowy** („Stan
instalacji nieznany — Nie można odczytać stanu instalacji — sprawdź log.”), podlega filtrowi
„po ostatnim rozruchu” i nie jest odpytywany. Żywy instalator, którego `updated` jest starsze
niż 2 min (`LOG_AHEAD`), a log ma linie nowsze o ponad 2 min od `updated` (zapisy statusu
padają, log idzie dalej): widok uzupełniany z logu (wyniki, bieżąca pozycja i etap),
`from_log = True`, podtytuł „Stan wg logu (plik statusu nie jest aktualizowany).”.
`find_background_install()`: pliki `windows-update-<znacznik>.json` ze wszystkich
`existing_log_dirs()` (tylko zaufane, nic nie tworzy), sortowane jak `latest_log`;
pomijane są tylko pliki JSON wyraźnie nie nasze (inny znacznik `winstaller`); gdy
najnowszy nasz plik jest nieczytelny, wynikiem jest `unknown` dla niego (z jego logiem), a
nie starszy zakończony przebieg. Sukces (`succeeded`) tylko przy `exit_code == 0`, bez
pozycji nieudanych i bez pominiętych/niedokończonych; `finished` bez kodu albo z pominiętymi →
„Zakończono niepełne (k z n zainstalowano)”. Restart (`needs_restart`) = flaga globalna lub
którakolwiek pozycja (lub log). Odczyt pliku kilku KB + parsowanie: 0,2 ms (stan `stale` z
logiem: 0,9 ms), ale ponowienia mogą potrwać do ~0,2 s, więc UI czyta w wątku roboczym.
Na tej maszynie (2026-11-22, Winstaller bez podniesienia) `find_background_install()`
zwraca dzisiejszy przebieg z `C:\ProgramData\Winstaller\logs` (katalog zaufany): `stale`,
`from_log`, 6 OK, kod 0, restart → „Zakończono — wymagany restart”.

**Heartbeat — świadomie nie ma.** Pole `updated` zmienia się przy każdym zapisie, ale nic nie
zapisuje statusu w trakcie jednej długiej operacji (`Install()` aktualizacji zbiorczej ~5 min,
`Download()` ~minuty). Osobny runspace/wątek z zegarem w podniesionym procesie oznaczałby
drugiego pisarza tego samego pliku (synchronizacja `.tmp`/`Replace` między wątkami) obok
synchronicznego wywołania COM, którego nie da się sprawdzić bez prawdziwej instalacji;
akcje zdarzeń `Register-ObjectEvent` (np. `System.Timers.Timer`) wykonuje wątek potoku
między poleceniami, więc w trakcie wywołania COM i tak by nie ruszyły (zachowanie PowerShella
znane z dokumentacji, tu niesprawdzane). Ryzyko dla działającej (sprawdzonej na sprzęcie)
instalacji jest większe niż zysk: „żywotność” określa Python sprawdzeniem procesu
(`is_live_powershell`), a UI pokazuje czas trwania bieżącego kroku liczony od `stepStarted`.

**Czego nie pokazujemy i dlaczego.** Procentu wewnątrz jednej aktualizacji (pobierania ani
instalacji): wymagałby asynchronicznych `BeginDownload`/`BeginInstall` z
`IDownloadProgressChangedCallback`/`IInstallationProgressChangedCallback` (obiekty COM
implementowane w PowerShellu albo odpytywanie `IDownloadJob.GetProgress()`), czyli innej
ścieżki niż sprawdzona 2026-11-22 na sprzęcie. **Możliwe rozszerzenie**, które wymaga testu
na prawdziwym sprzęcie (pobranie i instalacja realnych aktualizacji). Nie pokazujemy też
rozmiarów (`MaxDownloadSize` jest bezużyteczny — patrz kryterium) ani niczego z panelu
Ustawień.

**Środowisko procesów potomnych** (`_child_env`, każdy PowerShell i `store.exe`): biała
lista `CHILD_ENV_ALLOWED` (`SystemRoot`, `windir`, `SystemDrive`, `ComSpec`, `PATHEXT`,
`TEMP`/`TMP`, `USERPROFILE`, `LOCALAPPDATA`, `APPDATA`, `ProgramData`, `ProgramFiles*`,
`CommonProgramFiles*`, `USERNAME`, `USERDOMAIN`, `COMPUTERNAME`, `NUMBER_OF_PROCESSORS`,
`PROCESSOR_ARCHITECTURE`, `OS`, `PUBLIC`, `ALLUSERSPROFILE`, `HOMEDRIVE`/`HOMEPATH`) i
`WINSTALLER_*`, porównanie nazw bez wielkości liter; `PATH` jawnie = `System32;System32\Wbem;
System32\WindowsPowerShell\v1.0`, `PSModulePath` jawnie = `System32\WindowsPowerShell\v1.0\
Modules`. Nic z `COR_*`, `COMPlus_*`, `DOTNET_*`, `PYTHON*`, `PSModulePath` użytkownika.
PowerShell 5.1 sam dopisuje na początek `%ProgramFiles%\WindowsPowerShell\Modules`
(zapisywalne tylko dla administratorów) — tego nie da się wyłączyć; skrypty i tak używają
tylko modułów wbudowanych. Sprawdzone realnym skanem WUA, odczytem kolejki Store (WinRT) i
skryptem zastępczym w tle.

**Microsoft Store** (`update_store_apps`): metoda główna — `STORE_SCRIPT` z
`WINSTALLER_STORE_MODE=update`: `AppInstallManager.IsStoreBlockedByPolicyAsync` (best effort),
potem `SearchForAllUpdatesAsync("", "Winstaller", AppUpdateOptions{
AutomaticallyDownloadAndInstallUpdateIfFound = true, AllowForcedAppRestart = false})` przez most
`AsTask` z limitem; zwraca listę `AppInstallItem`, kolejkę realizuje usługa InstallService
(proces może się zakończyć). Pozycje w stanie `Error` lub z `errorCode` **nie** liczą się jako
zlecone (`StoreUpdateResult.failed`, w logu jako „Błąd: …”); wszystkie błędne → FAILED,
część → OK z ostrzeżeniem i liczbami. Tryb domyślny skryptu (brak/inna wartość zmiennej) to
bezpieczne `list` — tylko odczyt `AppInstallItems`. Blokada zasadami → błąd bez metody
zapasowej. **Timeout** metody głównej (Pythona albo `TimeoutException` w skrypcie, zgłaszany
polem `error.timeout`) → błąd „nie udało się potwierdzić” **bez** `store.exe` (zlecenie mogło
już pójść — drugie by je zdublowało). Metoda zapasowa tylko, gdy główna zawiodła przed
zleceniem (błąd ładowania typów/COM, odrzucone zlecenie, brak wyniku, brak PowerShella):
`store.exe updates --apply` (`%LOCALAPPDATA%\Microsoft\WindowsApps\store.exe`; uruchamiany
tylko, gdy jest punktem reparse `IO_REPARSE_TAG_APPEXECLINK`; `stdin=DEVNULL`, `NO_COLOR=1`,
środowisko z białej listy, limit 300 s). **Ocena wyniku `store.exe`** (jego kod wyjścia bywa 0
po błędzie, więc jest ignorowany): jakikolwiek znacznik błędu (`[y/n]`, `0x` + 8 cyfr hex,
`timed out`/`timeout`, `error(s)`, `fail…`, `blocked`, `sign in`, `exception`, `denied`,
`unable to`, `could not`, `cannot`, `non-interactive`, „Zainstalowano 0 z”, „błąd”, „nie
udało”, „nie można”, „nie powiodło”, „zablokowan…”, „zaloguj”, „przekroczono”/„limit czasu”)
albo brak wyjścia → błąd; w przeciwnym razie wynik **„zlecono, ale niepotwierdzone”**
(`confirmed = False`): zadanie OK z komunikatem, że aktualizację zlecono przez wiersz poleceń
Sklepu i wyniku nie da się potwierdzić (Microsoft Store → Pobrane), oczyszczone wyjście w
logu. Żadnej heurystyki „sukcesu”. Obie metody zawiodą → `UpdatesError` z instrukcją
ręczną; żadne okno nie jest otwierane.

**Zabezpieczenia** (Winstaller działa jako admin; proces w tle też):

| Zagrożenie | Zabezpieczenie |
|---|---|
| podłożony `powershell.exe` | pełna ścieżka `system32_path` |
| podmiana pliku skryptu przed uruchomieniem z prawami admina (`_MEIPASS` onefile jest zapisywalny dla użytkownika) | skrypty to stałe w kodzie (PYZ w exe), przekazywane `-EncodedCommand`; pliku skryptu na dysku nie ma |
| wstrzyknięcie przez parametry | brak interpolacji do treści skryptu; parametry w zmiennych środowiskowych, ID walidowane jako GUID (Python), ścieżki muszą być bezwzględne (skrypt) |
| proces bez podniesienia podkłada dowiązanie/junction lub zakłada katalog tam, gdzie pisze proces admina (domyślnie Użytkownicy mogą tworzyć pliki i foldery w `%ProgramData%`) | katalogi tworzone z chronionym DACL w chwili utworzenia; istniejące używane tylko po sprawdzeniu właściciela i DACL przez API; punkty reparse odrzucane; niezaufane drzewo nie jest naprawiane (świeży losowy katalog); log tworzony wyłącznie (`"x"`); `.tmp` statusu usuwany i tworzony `CreateNew` |
| wstrzyknięcie kodu do podniesionego PowerShella przez środowisko (`COR_PROFILER`, `COMPlus_*`, `DOTNET_*`, `PSModulePath`, `PATH`) | biała lista zmiennych, `PATH`/`PSModulePath` jawnie systemowe |
| druga instancja instalacji / fałszywa „trwająca instalacja” | mutex `Global\Winstaller.WindowsUpdate` z DACL tylko Administratorzy/SYSTEM; Python uznaje „już trwa” tylko z potwierdzeniem plikiem statusu (`running` + żywy `powershell.exe`) w zaufanym katalogu, inaczej FAILED „inny proces blokuje”; sekcja krytyczna przeciw równoległym startom w jednym procesie |
| fałszywy sukces (proces w tle od razu kończy się błędem, `store.exe` zgłasza błąd z kodem 0) | weryfikacja startu przez plik statusu i kod wyjścia; `store.exe` bez znaczników błędu = tylko „niepotwierdzone” |
| podrobione linie logu w tytułach aktualizacji | usuwanie znaków sterujących, separatorów linii, bidi/zero-width w skrypcie i w Pythonie; podział wyjścia JSON tylko po `\n` |
| fałszywy `store.exe` w profilu użytkownika | tylko alias App Execution (tag reparse); ochrona częściowa — to samo ryzyko co `winget` po nazwie |
| zawieszony skan/Store | limity 180 s (skan, WinRT) / 300 s (`store.exe`), zabicie procesu, reakcja na Stop |
| nieoczekiwany restart | nigdzie nie ma restartu; `ForceQuiet` nie restartuje, `RebootRequired` trafia do logu/statusu |

Sprawdzone 2026-11-22: realny skan WUA przez `scan_windows_updates()` (5 pozycji, 7–10 s, bez
admina), akcja z zamockowanym `start_background`, `list_store_queue()` (tylko odczyt), składnia
wszystkich skryptów (`Parser.ParseInput`, 0 błędów), `start_background` na nieszkodliwym
skrypcie zastępczym trzymającym mutex: proces ukryty (`MainWindowHandle = 0`), przeżywa
zakończenie rodzica (także rodzica w jobie z `KILL_ON_JOB_CLOSE` + `BREAKAWAY_OK`), zmienne
środowiskowe z polskimi znakami docierają, `mutex_exists`/`background_install_running` widzą
mutex, a `start_windows_update_install` przy trzymanym mutexie niczego nie uruchamia. W jobie z
`KILL_ON_JOB_CLOSE` **bez** `BREAKAWAY_OK` proces ginie razem z rodzicem (ograniczenie systemu).

**ZNANE NIEWIADOME — wymagają testu na prawdziwym sprzęcie** (nie wolno było uruchamiać
pobierania/instalacji ani zlecenia Store):

- ~~czy `IUpdateInstaller.Install()` instaluje Preview LCU i .NET Preview w procesie spoza
  Ustawień~~ — **sprawdzone na sprzęcie 2026-11-22** (stary skrypt, pobieranie jednym
  `Download()`): 6/6 OK, w tym Preview LCU i .NET Preview, kod 0, wymagany restart;
  pobieranie ~3 min, instalacja LCU ~5 min; Ustawienia tej instalacji nie pokazują;
- **nowy skrypt nie był uruchamiany na sprzęcie**: pobieranie pojedyncze (`Download()` na
  kolekcji jednoelementowej, nowy downloader dla każdej) — czy WUA nie zachowuje się inaczej
  niż dla całej listy (np. zależności między pakietami jednej aktualizacji, LCU + SSU), czas
  całości; zapis statusu przy każdym kroku (`[NullString]::Value`, ponowienia) sprawdzony
  tylko na nieszkodliwym skrypcie z tymi samymi funkcjami;
- czy sterowniki z flagą `CanRequestUserInput` instalują się z `ForceQuiet` i czy sterownik
  klasy Display z WU nie nadpisze sterownika producenta z tej samej kolejki (decyzja
  właściciela: nie pomijamy);
- przeżycie procesu po zamknięciu **zbudowanego** exe onefile (sprawdzone z procesu Pythona i
  syntetycznych jobów; jeśli Winstaller zostanie uruchomiony w jobie z `KILL_ON_JOB_CLOSE` bez
  `BREAKAWAY_OK`, instalacja zginie z nim);
- `AppInstallManager.SearchForAllUpdatesAsync` z procesu **podniesionego** (uruchomiono wyłącznie
  odczyt kolejki, bez podniesienia) i to, czy pobieranie rusza bez otwierania Store;
- `store.exe updates --apply`: czy flaga działa bez wartości (pomoc: `--apply <value>`,
  przykład bez wartości), jak wygląda wyjście w trakcie/po instalacji (heurystyka słów może dać
  fałszywy błąd albo fałszywy sukces) i czy bez konsoli nie pyta mimo `--apply`;
- Windows 10 (inny WUA, brak `store.exe`, inne zachowanie Preview) i ARM64;
- kolizje z resztą kolejki: instalacja w tle obciąża dysk/sieć w czasie instalacji programów
  winget (msiexec może czekać na `_MSIExecute`, instalatory mogą zgłosić 1618 „inna instalacja w
  toku”) oraz sterownik GPU z kolejki równolegle ze sterownikiem z WU;
- katalog logów z procesu **podniesionego** (sprawdzone tylko w scratchpadzie bez podniesienia:
  DACL `P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)` i brak zapisu dla
  zwykłego użytkownika — ale właścicielem był użytkownik; że po podniesieniu właścicielem
  jest grupa Administratorzy, wynika z domyślnego właściciela tokenu i nie było sprawdzone;
  `%ProgramData%` nie był dotykany);
- `MutexSecurity.SetAccessControl` w skrypcie tła (tylko kontrola składni) i to, czy
  Winstaller bez podniesienia widzi wtedy mutex (`OpenMutexW` → `ERROR_ACCESS_DENIED` = istnieje).

### `winstaller.core.runner`

```python
class TaskStatus(StrEnum): PENDING, RUNNING, OK, FAILED, CANCELLED, SKIPPED
    finished: bool (property)
@dataclass(frozen=True) class Task: item: Item; index: int; status: TaskStatus = PENDING
    item_id (property)   # niemutowalna migawka: run_started -> PENDING, task_started -> RUNNING
@dataclass(frozen=True) class TaskResult:
    item_id: str; status: TaskStatus; message: str; duration: float; artifact: Path | None = None
@dataclass(frozen=True) class RunSummary:
    results: tuple[TaskResult, ...]; duration: float
    cancelled: bool                # True tylko gdy któreś zadanie ma status CANCELLED
    count(status) / ok_count / failed_count / cancelled_count / skipped_count
    failed_ids: list[str]          # FAILED + CANCELLED — dla „Ponów nieudane”

class RunnerListener(Protocol):
    def run_started(self, tasks: Sequence[Task]) -> None
    def task_started(self, task: Task) -> None
    def output_line(self, text: str) -> None
    def output_replace_last(self, text: str) -> None
    def task_finished(self, result: TaskResult) -> None
    def run_finished(self, summary: RunSummary) -> None
class NullListener                 # wszystkie metody puste; wygodna klasa bazowa

Executor = Callable[[Item, ActionContext], ActionOutcome | str | None]  # str/None = sukces
class TaskRunner:
    def __init__(self, catalog, *, dry_run=False, listener=None, dry_run_config=None,
                 executor=None, arch=None, app_dir=None, preserve_order=False)
    def run(self, item_ids: Iterable[str], *, cancel_event: threading.Event | None = None
            ) -> RunSummary
    def cancel(self) -> None       # thread-safe; tylko bieg w toku
    is_running: bool (property)
    listener_errors: list[str]     # wyjątki listenera z ostatniego biegu ("metoda: Typ: msg")

ENV_DRY_RUN_FAIL = "WINSTALLER_DRY_RUN_FAIL"     # "id1,id2" lub "*"
ENV_DRY_RUN_DELAY = "WINSTALLER_DRY_RUN_DELAY"   # sekundy na krok, 0 = natychmiast
@dataclass(frozen=True) class DryRunConfig:
    delay: float = 0.15; progress_steps: int = 8; fail_ids: frozenset[str] = frozenset()
    @classmethod from_env(environ=None) -> DryRunConfig
def make_dry_run_executor(config: DryRunConfig) -> Executor
```

Dry-run aktualizacji: `windows_update` — „wyszukiwanie” → lista `updates.DRY_RUN_WINDOWS_UPDATES`
→ „uruchomiono w tle (PID 4242) — symulacja” + przykładowa ścieżka logu; `store_update` — lista
`updates.DRY_RUN_STORE_APPS` (3 aplikacje). Bez procesów, plików i sieci; `fail_ids` działa.

Reguły `run()`:
- kolejność wykonania = kolejność katalogu (jak w 1.x), chyba że `preserve_order=True`;
  duplikaty usuwane; nieznane id → `CatalogError` **przed** startem;
- sekwencja zdarzeń: `run_started(tasks)` → dla każdego zadania albo
  `task_started` → `output_*`… → `task_finished`, albo (pozycja niedostępna / po anulowaniu)
  od razu `task_finished` ze statusem `SKIPPED`/`CANCELLED` **bez** `task_started` →
  na końcu `run_finished(summary)`;
- przed każdym zadaniem runner loguje `--- <nazwa> ---`, po nim `outcome.message`;
- wyjątek w zadaniu → `FAILED` z komunikatem, bieg trwa dalej; wynik executora `str`/`None`
  to sukces (jak `ActionFunc`), inny typ → `FAILED`;
- wyjątek (`Exception`) rzucony przez metodę listenera **nie przerywa biegu** i nie zmienia
  statusu zadania: jest logowany (`logging`, logger `winstaller.core.runner`) i zapisany
  w `listener_errors`; pozostałe zdarzenia, łącznie z `run_finished`, są dostarczane.
  `BaseException` (np. `KeyboardInterrupt`) przechodzi dalej, `is_running` jest zerowane;
- `cancel()` / `cancel_event` przerywa bieżący proces (status `CANCELLED`) i oznacza resztę
  jako `CANCELLED`; `RunSummary.cancelled` jest prawdą tylko, gdy coś zostało anulowane
  (cancel po ukończeniu ostatniego zadania nie czyni biegu „anulowanym”);
- `dry_run=True`: nic nie dotyka systemu; symulacja: kilka linii, pasek postępu przez
  `replace_last`, opóźnienia `delay` (przerywalne przez `cancel`), błąd dla `fail_ids`;
  availability nadal obowiązuje (pozycje bez plików lokalnych → `SKIPPED`);
- `run()` nie jest reentrant (`RuntimeError`, gdy już trwa).

### `winstaller.core.updater`

```python
GITHUB_REPO = "ZMASLO/winstaller"; API_LATEST_URL
class UpdateError(Exception); class UpdateCancelled(UpdateError)
def parse_version(text) -> VersionKey # "[vV]X.Y.Z[-pre][+build]"; porządek SemVer:
    # pre-release < release, identyfikatory pre porównywane po kolei (rc.2 < rc.10,
    # liczbowe < tekstowe), build ignorowany; ValueError
    # klucz: (major, minor, patch, 0|1, ((0, n, "") | (1, 0, "txt"), ...))
def is_newer(candidate: str, current: str) -> bool
@dataclass(frozen=True) class ReleaseAsset: name; url; size: int; sha256: str | None
@dataclass(frozen=True) class ReleaseInfo: tag; version; name; notes; html_url; assets
    def asset_named(name) -> ReleaseAsset | None
@dataclass(frozen=True) class UpdateCheck:
    current: str; latest: str; update_available: bool; asset: ReleaseAsset | None; release: ReleaseInfo
def asset_name_for(arch) -> str        # "winstaller-x64.exe" / "winstaller-arm64.exe"
def parse_release(data: dict) -> ReleaseInfo
def check(session=None, *, current=None, arch=None, url=API_LATEST_URL) -> UpdateCheck
def download(asset, dest, progress_cb=None, cancel_event=None, *, session=None) -> Path
    # progress_cb(received_bytes, total_or_None)
def apply(new_exe, *, current_exe=None, args=None, launch=True) -> bool   # True = zakończ aplikację
def cleanup_old(exe=None, *, wait=0.0, interval=0.25) -> bool
def old_path_for(exe: Path) -> Path    # "<exe>.old"
def staged_path_for(exe: Path) -> Path # "<exe>.new" (kopia na tym samym woluminie co exe)
```

`session` to dowolny obiekt z metodą `get` jak `requests.Session` (testy: atrapa).
Błędy sieci/HTTP/formatu (także nieliczbowy/ujemny `size` assetu) → `UpdateError` z polskim
komunikatem (nigdy `None`).

### Mechanizm aktualizacji i zgodność z 1.x

1. `check()` — `GET releases/latest` (timeouty 5/10 s), tag `vX.Y.Z`, porównanie semver;
   `update_available` tylko gdy wydanie jest **nowsze** (nigdy downgrade).
2. Asset wybierany po nazwie `winstaller-<arch>.exe` (te same nazwy co w 1.x i w workflow).
3. `download()` — strumień do `<dest>.part` (timeouty 10/30 s), weryfikacja rozmiaru z API
   i SHA256 z pola `digest` (`sha256:<hex>`, gdy GitHub je zwraca), potem rename do `dest`;
   plik częściowy usuwany przy każdym błędzie/anulowaniu.
4. `apply()` — (a) `new → exe.new` obok exe: rename, a gdy pobrany plik leży na innym
   woluminie (`%TEMP%` na C:, exe na pendrive; `os.replace` daje WinError 17) — kopia;
   (b) `exe → exe.old` (Windows pozwala zmienić nazwę działającego obrazu); (c) `exe.new → exe`
   (zawsze atomowy rename w obrębie katalogu); (d) start nowego procesu z
   `PYINSTALLER_RESET_ENVIRONMENT=1` (PyInstaller ≥ 6.9: nowa instancja onefile nie przejmuje
   katalogu `_MEI` starej), zwraca `True` → UI kończy aplikację. Pobrany plik jest usuwany po
   udanej podmianie z kopii. Niepowodzenie dowolnego kroku → **najpierw** przywrócenie
   oryginału (`exe.old → exe`, nadpisuje nowy plik; gdy rename się nie da — kopia treści),
   potem (best effort) oddanie pobranego pliku na miejsce i `UpdateError`. Jeśli przywrócenie
   się nie uda, też `UpdateError` z informacją, gdzie leży oryginał (`exe.old` nigdy nie jest
   usuwany przez `apply`). Po każdej nieudanej ścieżce oryginalny exe jest na miejscu.
   W dev (`not is_frozen()`) bez `current_exe` → `UpdateError`.
5. `cleanup_old()` przy starcie usuwa `exe.old`; stary proces może jeszcze się zamykać, więc UI
   powinno wołać `cleanup_old(wait=10)` w tle.

Zgodność: wersja 1.1.x pobiera `winstaller-<arch>.exe` z najnowszego wydania i podmienia go
swoim `update.bat`, więc 2.0 opublikowane z tymi samymi nazwami assetów i tagiem `v2.0.0`
zostanie przez 1.x zainstalowane. `update.bat` z 1.x kasuje `winstaller.exe.old`, a 2.0 i tak
sprząta `.old` przy starcie.

### `winstaller/__main__.py`

`--smoke` (import całego core, wczytanie katalogu, wypisanie wersji/arch, kod 0; błąd → 1),
`--version`, `--dry-run`, `--no-elevate`, ukryte `--exit-after-show` (diagnostyka: pokaż okno,
po pierwszej klatce wypisz `window-shown <s od startu procesu>` i zakończ; bez sprawdzania
aktualizacji/winget — używa go `tests/perf/test_ui_perf.py`). Bez `--dry-run`/`--no-elevate`
i bez uprawnień admina → `relaunch_as_admin()`. UI: `from winstaller.ui.app import run`;
kontrakt **`run(*, dry_run: bool, exit_after_show: bool = False) -> int`** (kod wyjścia).
Brak modułu UI → komunikat, kod 2.

---

## Interfejs (`winstaller/ui`)

### Struktura

```
ui/
  app.py           create_application() (Fusion, fonty, ikona), run(), load_theme_mode()
  services.py      Services: wszystko spoza UI, wstrzykiwalne (testy, zrzuty, dry-run)
  bridge.py        granica wątków: LogStore, RunController, ToolController,
                   UpdateController, WingetController, BackgroundUpdateMonitor, Job (wątki tła)
  main_window.py   MainWindow: nawigacja, banery, spinanie stron z kontrolerami
  selection.py     SelectionModel: jedyne źródło prawdy o zaznaczeniu (blokowane w biegu)
  icons.py         ikona rysowana w kodzie (ta sama w tools/make_icon.py -> .ico)
  pages/           selection (Aplikacje/System), run (Przebieg), tools, log (Dziennik), about
  widgets/         item_tile, action_bar, banner, log_view, nav, confirm_dialog, common,
                   background_panel (blok „W tle” strony Przebieg)
  theme/           tokens (palety), qss, fonts, manager (tryb system/ciemny/jasny)
```

Strony: „Aplikacje” i „System” (kategorie `install`, kafelki, wyszukiwanie z debounce 60 ms,
podpowiedź o trafieniach na drugiej stronie, profil, „zaznacz wszystkie” w kategorii; tagi
rodzaju na kafelku: `[WINGET]`, `[WINGET · USUŃ]`, `[LOKALNY]`, `[AKCJA]`, `[STEROWNIK]`),
„Przebieg” (lista zadań, log na żywo, Stop / Ponów nieudane), „Narzędzia” (kategoria `tools`,
akcje natychmiastowe z wynikiem i „Otwórz” dla `artifact`), „Dziennik” (cały log sesji,
kopiuj/zapisz/wyczyść), „O programie” (motyw, ręczne sprawdzenie aktualizacji).
Pozycje z `confirm` w kolejce wymagają jednego zbiorczego potwierdzenia; zamknięcie okna w
trakcie biegu pyta, a po zgodzie zatrzymuje kolejkę i zamyka okno po `run_finished`.

**Instalacja aktualizacji Windows w tle — co widzi użytkownik.**

- Strona Przebieg, nad kolejką, niezależnie od niej: `BackgroundUpdatePanel` (`wuPanel`):
  nagłówek `// W TLE — WINDOWS UPDATE` + `START gg:mm · KROK mm:ss` (czas bieżącego kroku od
  `stepStarted`, po końcu `KONIEC gg:mm`, `WG LOGU`), „Lista (N)”/„Zwiń listę”, „Otwórz log”
  (`Services.open_path`); duży stan z `describe_progress` („Wyszukiwanie aktualizacji”,
  „Pobieranie 2 z 5”, „Instalacja 5 z 6”, „Zakończono — wymagany restart”, „Zakończono z
  błędami (2 z 6 nieudane)”, „Instalacja przerwana”, „Nie zainstalowano aktualizacji”), tytuł
  bieżącej aktualizacji, pasek krokowy (`steps_done/steps_total`: pobrania + instalacje; w
  trakcie jednego długiego kroku pasek stoi, a sygnałem „trwa” jest migający kursor i licznik
  — żadnych udawanych procentów), lista (`[OCZEKUJE]`, `[POBIERANIE]`, `[POBRANO]`,
  `[INSTALACJA]`, `[OK]`, `[BŁĄD]` + kod, po końcu `[POMINIĘTO]`/`[PRZERWANO]`), po końcu
  liczby `[OK] n · [BŁĄD] n` i ramka `[WYMAGANY RESTART]`. Bez przycisku restartu. Przy
  pustej kolejce blok jest pełny (H1, lista do 8 wierszy); gdy widok kolejki jest pokazany —
  kompaktowy (H2, lista zwinięta poza nieudanymi); gdy kolejka **biegnie**, żółty należy do
  niej: kursor bloku znika, pasek bloku jest szary (`set_secondary`). Kolory: żółty tylko to,
  co trwa; `[OK]` w kolorze tekstu; czerwony tylko błąd; restart — obrys bez koloru.
  Zegar: dla dnia innego niż dziś z datą (`START 2026.11.21 15:55`). „Otwórz log” wyłączony,
  gdy pliku logu nie ma (`Services.path_exists`). Gdy żywy instalator stoi w jednym kroku
  dłużej niż `STALL_SECONDS` (90 min), podtytuł dostaje „Brak postępu od g:mm:ss — sprawdź
  log.” (bez zmiany stanu, znacznika ani czegokolwiek w systemie). Stan `unknown` (np.
  plik statusu zniknął w trakcie): „Stan instalacji nieznany”, ostatnie znane statusy pozycji
  bez werdyktu, bez liczb.
- Nawigacja: drugi znacznik przy „Przebieg” (`NavButton.extra_badge`): `WU …` (szukanie),
  `WU 2/5` (pobieranie: k/n pobrań; instalacja: i/N) — żółty, gdy kolejka nie biegnie,
  inaczej szary; po końcu `WU OK` (kolor tekstu, tylko czysty sukces) / `WU BŁ.` (czerwony:
  błędy, niepełne, przerwane) / `WU ?` (czerwony, `unknown`).
- Po zakończeniu baner `wuBanner` (niemodalny, u góry): „Aktualizacje Windows zainstalowane —
  uruchom ponownie komputer” / „Brak aktualizacji Windows do zainstalowania”
  (`tone="neutral"`: obrys szary, tag w kolorze tekstu) / „… zakończone z błędami” /
  „Instalacja aktualizacji Windows niepełna” / „… przerwana” / „Nie zainstalowano
  aktualizacji Windows” / „Stan instalacji aktualizacji Windows nieznany” (`tone="error"`),
  szczegół z liczbami (nieudane, pominięte, „wynik odczytany z logu”), akcje „Pokaż →”
  (`wuBannerShow`, Przebieg), „Otwórz log” (`wuBannerLog`, tylko gdy log istnieje), „Ukryj”
  (`wuBannerHide`: chowa baner, znacznik nawigacji i — dla stanu końcowego — blok, od razu i
  przy kolejnych startach: `QSettings` `updates/hiddenBackgroundInstall` = nazwa pliku
  statusu; trwającej instalacji nie da się ukryć).
- Start programu (`start_background_tasks`, po `show()`, w wątku): `find_background_install`;
  trwająca → blok + śledzenie; zakończona/`stale` → pokazana tylko, gdy `reference_time`
  (koniec, inaczej ostatni zapis, inaczej start) ≥ `Services.boot_time()` i nie ukryta. Po
  restarcie komputera stary wynik więc znika.
- Zamknięcie okna, gdy instalacja w tle trwa: jedno pytanie „Zamknąć Winstaller?” („…
  instalują się dalej w tle — zamknięcie Winstallera ich nie przerwie… Log: …”, „Zamknij” /
  „Anuluj”); gdy jednocześnie biegnie kolejka — istniejące pytanie o kolejkę z tym zdaniem
  dopisanym (jedno okno, nie dwa).
- Dziennik: zmiana stanu (tytułu) dopisuje linię `[winstaller] Windows Update w tle: …`.

**`BackgroundUpdateMonitor`** (`bridge.py`, `MainWindow.bg_updates`): `track(status_path)` /
`track_result(TaskResult)` (pozycja `windows-update` OK **albo CANCELLED** — przerwane
czekanie na potwierdzenie startu — z `artifact` `.log` → `.json` obok; pewniejsze niż
„najnowszy plik”, bo to dokładnie ten przebieg, także „już trwa”) i `detect(is_hidden)`
(start; stany końcowe, w tym `unknown`, tylko po ostatnim rozruchu i nieukryte). Odpytuje
co `Services.background_poll_ms` (1000) **tylko** gdy coś śledzi, każdy odczyt w
wątku-demonie (`Job`; nowy odczyt nie startuje, gdy poprzedni trwa), `changed(InstallProgress)`
tylko przy faktycznej zmianie migawki (równość dataclass), zatrzymuje się po stanie
końcowym albo po `MAX_FAILURES` (60) nieudanych odczytach z rzędu (pierwszy błąd do
dziennika) — wtedy, jeśli coś już było widać, ostatnia migawka przechodzi w końcowe
`unknown` z `LOST_MESSAGE` (żadnego zamrożonego „WU 1/4” ani pytania przy zamykaniu). Wynik, który przyjdzie po zamknięciu okna, jest ignorowany
(`threading.Event`, nie atrybut QObject). Właściwości: `progress`, `polling`, `running`,
`status_path`, `enabled`.

### Model wątków

- Wątek GUI posiada wszystkie widgety. Kolejka instalacji biegnie w `QThread`
  (`_RunThread` → `TaskRunner.run(ids, cancel_event=...)`), narzędzia, sprawdzanie i
  pobieranie aktualizacji, winget i `cleanup_old` w wątkach-demonach (`Job`), żeby wiszące
  wywołanie sieciowe nie blokowało zamknięcia.
- Zdarzenia z wątków idą do GUI wyłącznie sygnałami obiektów żyjących w wątku GUI
  (`QueuedConnection`). Listener przekazuje niemutowalne `Task`/`TaskResult`/`RunSummary`.
- Wyjście procesów nie jest sygnalizowane per linia: trafia do `LogStore` (bufor pod blokadą),
  opróżnianego w wątku GUI co najwyżej co `FLUSH_MS` = 40 ms jedną partią (`batch(replace,
  lines)`), z limitem `MAX_LOG_LINES` = 5000 linii (`LogView` ma ten sam limit bloków).
- `RunController.start()` tworzy `cancel_event` przed startem wątku, więc Stop działa od
  pierwszej chwili (stan `STARTING` → `STOPPING`; późniejsze `run_started` go nie cofa).

### `Services`

`Services.create(dry_run=...)` łączy prawdziwe funkcje core (albo symulacje w dry-run).
Pola: `catalog`, `runner_factory`, `tool_runner_factory` (`preserve_order=True`), `dry_run`,
`arch`, `app_dir`, `version`, `check_update`, `download_update`, `apply_update` (`None` =
pobieranie symulowane bez podmiany), `update_dest` (obok exe, gdy da się pisać; inaczej
`%TEMP%` — `apply` radzi sobie z innym woluminem), `cleanup_old`, `winget_available`,
`ensure_winget`, `open_path`, `open_url`, `confirm` (`None` = modalny `ConfirmDialog`),
`auto_check_updates`, `check_winget_on_start`, `find_background_install`,
`read_install_progress` (`None` = blok „W tle” wyłączony; tak jest w testach UI, które ich
nie podają), `boot_time` (`None` = bez limitu „niedawno”), `clock` (aware czas lokalny),
`background_poll_ms`, `path_exists` (czy log istnieje — „Otwórz log”). Realnie: funkcje `core.updates`. Dry-run:
`SimulatedBackgroundInstall` (pierwszy odczyt ścieżki uruchamia oś czasu: wyszukiwanie,
3 pobrania, 4 instalacje — pierwsza 3 kroki — koniec z restartem; krok = 8 ×
`WINSTALLER_DRY_RUN_DELAY`, przy 0 od razu koniec; bez plików i procesów),
`find_background_install = lambda: None`, `boot_time` = teraz − 1 h; czysta funkcja
`simulated_progress(steps, start=…)` daje te same stany zrzutom ekranu. Testy budują `Services` bezpośrednio z
atrapami (`tests/ui/conftest.py`), `tools/screenshots.py` przez `Services.create(dry_run=True)`
i `dataclasses.replace`. Ustawienia (`ui/theme`) w `QSettings` przekazanym do `MainWindow`
(aplikacja: domyślne `QSettings`; testy: plik ini w `tmp_path`).

---

## Format `catalog.toml`

```toml
[[categories]]              # kolejność w pliku = kolejność wyświetlania
id = "basic"
name = "Podstawowe programy"
description = "..."
kind = "install"            # domyślnie; "tools" = akcje natychmiastowe (strona Narzędzia)

[[items]]                   # kolejność w pliku = kolejność w kategorii i kolejność wykonania
id = "steam"                # stabilny slug: [a-z0-9-], nie zmieniać bez potrzeby
name = "Steam"              # polska nazwa wyświetlana
category = "launchers"
kind = "winget"             # winget | winget_uninstall | local_exe | action | driver
winget_id = "Valve.Steam"
post_launch = '%ProgramFiles(x86)%\Steam\Steam.exe'
description = "Krótki opis po polsku."
# opcjonalnie:
# arch = ["arm64"]                    # domyślnie wszystkie
# requires_files = ["BenchmarkTools"] # względem app_dir; pliki lub katalogi
# path = 'procyon\procyon-setup.exe'  # local_exe; glob dozwolony (ostatnie trafienie)
# args = ["/silent"]                  # local_exe: argumenty instalatora; action: argumenty akcji
# action = "copy_dir_to_desktop"      # action: klucz w actions.ACTIONS
# vendor = "nvidia"                   # driver (wymagane): nvidia | amd | intel
# confirm = "Czy na pewno ...?"       # UI pyta przed wykonaniem
# unavailable = "Powód"               # pozycja wyszarzona na stałe

[[profiles]]
id = "benchmark-starter"
name = "Benchmark starter"
items = ["hwmonitor", "7zip"]
[profiles.arch_items]
arm64 = ["copy-benchmark-tools-arm"]
```

Ścieżki Windows zapisuj w TOML w apostrofach (literal string), żeby `\` nie był escapem.

### Jak dodać program

1. Znajdź identyfikator: `winget search <nazwa>`, potwierdź `winget show --id <id> --exact`.
2. Dopisz `[[items]]` w odpowiedniej sekcji `catalog.toml` (`kind = "winget"`, unikalne `id`).
3. Opcjonalnie dodaj `id` do profilu.
4. `python -m winstaller --smoke` (walidacja katalogu) i testy.
Nowa akcja systemowa: funkcja `def x(ctx: ActionContext) -> ActionOutcome | str | None`
w `actions.py`, wpis w `ACTIONS`, pozycja z `kind = "action"`, `action = "x"`.

---

## Decyzje i ich powody

- **Katalog w TOML + `tomllib`** — dane oddzielone od kodu, stabilne id zamiast nazw
  wyświetlanych jako kluczy, walidacja przy ładowaniu; `tomllib` jest w stdlib.
- **Core bez Qt, synchroniczny** — testowalny bez GUI, jeden model wątków, UI decyduje o wątkach.
- **Zdarzenia przez `RunnerListener`, nie `print`** — w 1.x log był kanałem danych
  przez podmienione `sys.stdout`.
- **stdout i stderr scalone** w `stream_process` — jeden parser, poprawna semantyka
  „nadpisz ostatnią linię”; 1.x miał dwa niezależne wątki piszące do jednego terminala.
- **Wątek czytający + kolejka**, callbacki w wątku wołającym — bez `time.sleep` w pętli czytania
  (`read1` blokuje do czasu danych), anulowanie sprawdzane co 50 ms. Jeżeli proces potomny
  (instalator) trzyma odziedziczony potok po zakończeniu winget, czekamy na EOF maks. 3 s.
- **`--id … --exact`** zamiast samego zapytania (1.x) — jednoznaczne dopasowanie pakietu.
- **Niezerowy kod winget = błąd**, z jawną listą kodów „już zainstalowane” (tabela wyżej).
  Kody restartu (`0x8A150109`, `0x8A15010B`) też jako sukces — instalacja się udała, potrzebny
  tylko restart (to wykracza poza samo „już zainstalowane”, decyzja świadoma).
  Uninstall pakietu, którego nie ma (`0x8A150014`), to sukces z komunikatem — profil
  „Benchmark starter” odinstalowuje OneDrive, a brak OneDrive nie jest porażką.
- **DaVinci Resolve Studio** — `winget search "DaVinci Resolve"` (19.11.2026) zwraca tylko
  `elpideus.DaVinciResolveRPC` (wtyczka Discord RPC, nie program), `winget search Blackmagic`
  nie zwraca Resolve. Brak wiarygodnego pakietu → pozycja `local_exe` z
  `path = 'DaVinci_Resolve_Studio_*_Windows.exe'` w `app_dir` i argumentami `/i /q /noreboot`
  (jak w 1.x dla instalatora z ZIP). Glob, bo nazwa pliku zawiera numer wersji; availability
  wyjaśnia brak pliku.
- **Zmiany listy względem 1.1.8 (decyzja właściciela, 2026.11.20)** — Messenger zastąpiony
  przez Signal (`OpenWhisperSystems.Signal`), Telegram usunięty, DisplayCAL z pakietu
  `ErkanOzgurYilmaz.DisplayCAL`, „Instaluj lokalne oprogramowanie” (RTSS i CapFrameX z plików
  lokalnych) usunięte — oba programy instalują się przez winget.
- **Sterowniki GPU (decyzja właściciela, 2026.11.20)** — kategoria `gpu-drivers` zaraz po
  „Narzędzia testowe” w katalogu, pokazywana na stronie **System** jako pierwsza sekcja
  (`SYSTEM_CATEGORY_IDS` w `ui/main_window.py`; przeniesiona z Aplikacji 2026.11.22; od
  2026.11.22 druga, po „Aktualizacje”). Kolejność wykonania = kolejność katalogu, więc sterownik instaluje się po
  narzędziach testowych, przed aplikacjami profesjonalnymi. Każda pozycja ma `confirm`
  (wyłączenie obrazu, restart) — trafia do zbiorczego potwierdzenia. Bez wykrywania karty.
- **Aktualizacje (decyzja właściciela, 2026.11.22)** — kategoria `updates` jako pierwsza w
  katalogu (wykonanie przed wszystkim) i pierwsza sekcja strony System; tryb „w tle”; zakres
  „wszystko, łącznie z opcjonalnymi, Preview i sterownikami z WU” (świadomie bez pomijania
  sterowników, także Display; przełączniki `SKIP_*` w `updates.py` domyślnie wyłączone); obie
  pozycje na początku profilu „Benchmark starter”; `confirm` tylko dla Windows Update.
  Skrypty jako stałe Pythona zamiast plików `.ps1` w pakiecie — patrz zabezpieczenia w sekcji
  `updates` (żaden plik skryptu w zapisywalnym `_MEIPASS`).
- **Kopiowanie BenchmarkTools (x64)** zostaje dostępne na obu architekturach (jak w 1.x);
  „BenchmarkToolsARM” tylko na arm64 i tylko w profilu przez `arch_items.arm64`.
- **`ensure_installed`** najpierw `Add-AppxPackage -RegisterByFamilyName` (App Installer jest
  preinstalowany w Windows 11, często tylko niezarejestrowany dla konta), potem msixbundle
  z GitHub jak w 1.x. Uwaga: na czystym systemie msixbundle może wymagać zależności (VCLibs,
  UI.Xaml) — tego 1.x też nie obsługiwał.
- **Anulowanie** zabija proces winget (terminate → 2 s → kill, jak w 1.x); instalator
  uruchomiony przez winget może działać dalej (drzewo procesów nie jest zabijane).
- **Pliki lokalne** względem `app_dir()` (katalog exe), nie `cwd`.
- **Wersja** `2.0.0` w `winstaller/core/version.py` w formacie zgodnym z `bump_version.py`
  (ten edytuje właśnie ten plik); `pyproject.toml` czyta ją dynamicznie
  (`winstaller.core.version.VERSION`), `winstaller.spec` wpisuje ją do właściwości exe.
- **Dekodowanie konsoli** — tam, gdzie sami uruchamiamy PowerShell, wymuszamy UTF-8
  (`POWERSHELL_UTF8_PREAMBLE`), więc zgadywanie dotyczy tylko obcych narzędzi (shutdown,
  powercfg, winget). CP852 i CP1250 dają się wzajemnie zdekodować bez błędu, dlatego zamiast
  kolejności prób jest punktacja (polskie litery +3, inne litery +1, symbole/ramki −3).
- **PyInstaller**: `catalog.toml` musi trafić do
  `_MEIPASS/winstaller/core/catalog.toml` (np. `--add-data "winstaller/core/catalog.toml;winstaller/core"`).
