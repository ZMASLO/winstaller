# Winstaller — architektura

Dokument ma dwie części: (A) mapa starej wersji 1.1.8 (punkt odniesienia do czasu usunięcia
starego kodu w etapie 3) i (B) architektura 2.0 wraz z **kontraktami publicznego API `core`**,
na których opierają się UI, testy i build.

---

## A. Wersja 1.1.8 (stary kod: `main.py`, `core/`, `gui/`)

### Pliki

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
    runner.py        TaskRunner, statusy, wyniki, listener, symulacja dry-run
    updater.py       check / download / apply / cleanup_old
  ui/                PySide6 (etap 2, agent UI)
tests/               unit / ui / perf (etap 2, agent testów)
```

Zależności między modułami `core` (bez cykli na poziomie modułu):
`version` ← `platform` ← `catalog` ← `winget` ← `actions` ← `runner`; `updater` używa
`platform` i `version`. `catalog.parse_catalog` importuje `actions.ACTIONS` leniwie (walidacja
nazw akcji).

### Przepływ danych

```
catalog.toml --load_catalog()--> Catalog --(UI: wybór id)--> TaskRunner.run(ids)
   TaskRunner: dla każdej pozycji -> availability -> executor(item, ActionContext)
      executor = actions.execute (realnie) | make_dry_run_executor(cfg) (dry-run)
      actions.execute -> winget.install/uninstall | run_local_installer | ACTIONS[name]
      wyjście procesów -> StreamParser -> ctx.log / ctx.replace_last -> listener.output_*
   zdarzenia -> RunnerListener (UI: bridge.py zamienia je na sygnały Qt)
```

### Model wątków

- `core` jest w całości **synchroniczny**. `TaskRunner.run()` blokuje i wykonuje się w wątku
  wywołującym; **wszystkie metody listenera są wołane w tym samym wątku** (wątek czytający
  potok procesu tylko wkłada zdarzenia do kolejki, oddaje je `stream_process` w wątku wołającym).
- UI uruchamia `run()` w swoim wątku roboczym (QThread) i przekazuje zdarzenia do wątku GUI
  sygnałami (połączenia queued). Nie dotykać widgetów z listenera bezpośrednio.
- `TaskRunner.cancel()` jest bezpieczne wątkowo (ustawia `threading.Event`). Zdarzenie jest
  tworzone na nowo na początku `run()`, więc przycisk Stop należy włączać dopiero po
  `run_started` (anulowanie wywołane przed startem biegu jest ignorowane).
- `updater.check/download` też są blokujące — UI woła je w tle. `download` przyjmuje
  `cancel_event`; `progress_cb` jest wołany w wątku pobierającym.

---

## Kontrakty publicznego API `core`

Sygnatury poniżej są wiążące dla UI/testów. Wszystkie komunikaty dla użytkownika (wyjątki,
`message`) są po polsku.

### `winstaller.core.version`

```python
APP_NAME: str = "Winstaller"
def get_version() -> str            # "1.1.8"; literał edytuje bump_version.py (regex return "X.Y.Z")
VERSION: str
def get_version_info() -> str       # "Winstaller 1.1.8"
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
def resource_path(relative: str | PathLike) -> Path
    # zasób pakietu względem katalogu winstaller/; frozen: _MEIPASS/winstaller/<relative>
```

### `winstaller.core.catalog`

```python
class CatalogError(Exception)

class ItemKind(StrEnum): WINGET, WINGET_UNINSTALL, LOCAL_EXE, ACTION
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
    destructive: bool  (property)          # confirm is not None
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
def load_catalog(path=None, *, known_actions: Collection[str] | None = None) -> Catalog
def parse_catalog(data: Mapping, *, known_actions=None) -> Catalog   # z gotowego dict (testy)
def resolve_relative(base: Path, rel: str) -> Path | None            # glob: ostatnie trafienie
```

Walidacja (`CatalogError` z czytelnym komunikatem): nieznane pola/sekcje, brak wymaganych pól,
duplikaty id (kategorie, pozycje, profile), nieznana kategoria pozycji, nieznany rodzaj, akcja
spoza `actions.ACTIONS`, `winget_id` wymagany dla winget, `path` dla `local_exe`, ścieżki
bezwzględne lub z `..`, nieznana architektura, profil wskazujący nieistniejące id lub pozycję
nieobsługującą danej architektury w `arch_items`.

### `winstaller.core.winget`

```python
CREATE_NO_WINDOW: int
class OutputKind(StrEnum): APPEND = "append", REPLACE_LAST = "replace_last"
@dataclass(frozen=True) class OutputEvent: kind: OutputKind; text: str

def decode_console_bytes(raw: bytes) -> str     # utf-8 -> cp1250 -> cp852 -> iso-8859-2

class StreamParser:                             # CZYSTY, bez I/O
    def feed(chunk: bytes) -> list[OutputEvent]
    def flush() -> list[OutputEvent]            # raz, na końcu strumienia

@dataclass(frozen=True) class ProcessOutcome: returncode: int; cancelled: bool
def stream_process(args, *, on_line, on_replace_last, cancel_event=None, cwd=None) -> ProcessOutcome
    # stdout+stderr scalone, CREATE_NO_WINDOW, callbacki w wątku wołającym;
    # anulowanie: terminate -> wait(2 s) -> kill; returncode -1; OSError gdy nie da się uruchomić

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
napotkaniu `\r` lub `\n` (bez zgadywania częściowych linii). Usuwane są CSI, OSC (`ESC ] ... BEL`),
sekwencje nF i dwubajtowe ESC; sekwencja lub znak wielobajtowy przecięty granicą `feed` jest
składany poprawnie (dekodowanie dopiero na pełnych segmentach). `\b` jest emulowany, pozostałe
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
}
def execute(item: Item, ctx: ActionContext) -> ActionOutcome   # realne wykonanie pozycji
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
- `copy_self_to_desktop`: tylko frozen (`sys.executable`), w dev `ActionError`;
- restarty: `shutdown /r /fw /t 1` i `shutdown /r /o /t 1`, kod ≠ 0 = błąd;
- raport baterii: `powercfg /batteryreport /output <pulpit>\battery-report.html`
  (fallback `app_dir`), ścieżka w `ActionOutcome.artifact` i `TaskResult.artifact`;
- `post_launch`: tylko po udanej instalacji i gdy plik istnieje.

`download_install`/`download_unzip_install` z 1.x **nie zostały przeniesione** — po przejściu
DaVinci na lokalny instalator nic ich nie używa.

### `winstaller.core.runner`

```python
class TaskStatus(StrEnum): PENDING, RUNNING, OK, FAILED, CANCELLED, SKIPPED
    finished: bool (property)
@dataclass class Task: item: Item; index: int; status: TaskStatus = PENDING; item_id (property)
@dataclass(frozen=True) class TaskResult:
    item_id: str; status: TaskStatus; message: str; duration: float; artifact: Path | None = None
@dataclass(frozen=True) class RunSummary:
    results: tuple[TaskResult, ...]; duration: float; cancelled: bool
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

Executor = Callable[[Item, ActionContext], ActionOutcome]
class TaskRunner:
    def __init__(self, catalog, *, dry_run=False, listener=None, dry_run_config=None,
                 executor=None, arch=None, app_dir=None, preserve_order=False)
    def run(self, item_ids: Iterable[str]) -> RunSummary
    def cancel(self) -> None       # thread-safe
    is_running: bool (property)

ENV_DRY_RUN_FAIL = "WINSTALLER_DRY_RUN_FAIL"     # "id1,id2" lub "*"
ENV_DRY_RUN_DELAY = "WINSTALLER_DRY_RUN_DELAY"   # sekundy na krok, 0 = natychmiast
@dataclass(frozen=True) class DryRunConfig:
    delay: float = 0.15; progress_steps: int = 8; fail_ids: frozenset[str] = frozenset()
    @classmethod from_env(environ=None) -> DryRunConfig
def make_dry_run_executor(config: DryRunConfig) -> Executor
```

Reguły `run()`:
- kolejność wykonania = kolejność katalogu (jak w 1.x), chyba że `preserve_order=True`;
  duplikaty usuwane; nieznane id → `CatalogError` **przed** startem;
- sekwencja zdarzeń: `run_started(tasks)` → dla każdego zadania albo
  `task_started` → `output_*`… → `task_finished`, albo (pozycja niedostępna / po anulowaniu)
  od razu `task_finished` ze statusem `SKIPPED`/`CANCELLED` **bez** `task_started` →
  na końcu `run_finished(summary)`;
- przed każdym zadaniem runner loguje `--- <nazwa> ---`, po nim `outcome.message`;
- wyjątek w zadaniu → `FAILED` z komunikatem, bieg trwa dalej;
- `cancel()` przerywa bieżący proces (status `CANCELLED`) i oznacza resztę jako `CANCELLED`;
- `dry_run=True`: nic nie dotyka systemu; symulacja: kilka linii, pasek postępu przez
  `replace_last`, opóźnienia `delay` (przerywalne przez `cancel`), błąd dla `fail_ids`;
  availability nadal obowiązuje (pozycje bez plików lokalnych → `SKIPPED`);
- `run()` nie jest reentrant (`RuntimeError`, gdy już trwa).

### `winstaller.core.updater`

```python
GITHUB_REPO = "ZMASLO/winstaller"; API_LATEST_URL
class UpdateError(Exception); class UpdateCancelled(UpdateError)
def parse_version(text) -> tuple      # "vX.Y.Z[-pre]"; pre-release < release; ValueError
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
```

`session` to dowolny obiekt z metodą `get` jak `requests.Session` (testy: atrapa).
Błędy sieci/HTTP/formatu → `UpdateError` z polskim komunikatem (nigdy `None`).

### Mechanizm aktualizacji i zgodność z 1.x

1. `check()` — `GET releases/latest` (timeouty 5/10 s), tag `vX.Y.Z`, porównanie semver;
   `update_available` tylko gdy wydanie jest **nowsze** (nigdy downgrade).
2. Asset wybierany po nazwie `winstaller-<arch>.exe` (te same nazwy co w 1.x i w workflow).
3. `download()` — strumień do `<dest>.part` (timeouty 10/30 s), weryfikacja rozmiaru z API
   i SHA256 z pola `digest` (`sha256:<hex>`, gdy GitHub je zwraca), potem rename do `dest`;
   plik częściowy usuwany przy każdym błędzie/anulowaniu.
4. `apply()` — `exe → exe.old` (Windows pozwala zmienić nazwę działającego obrazu),
   `new → exe`, start nowego procesu z `PYINSTALLER_RESET_ENVIRONMENT=1` (PyInstaller ≥ 6.9:
   nowa instancja onefile nie przejmuje katalogu `_MEI` starej), zwraca `True` → UI kończy
   aplikację. Niepowodzenie dowolnego kroku → przywrócenie oryginału i `UpdateError`.
   W dev (`not is_frozen()`) bez `current_exe` → `UpdateError`.
5. `cleanup_old()` przy starcie usuwa `exe.old`; stary proces może jeszcze się zamykać, więc UI
   powinno wołać `cleanup_old(wait=10)` w tle.

Zgodność: wersja 1.1.x pobiera `winstaller-<arch>.exe` z najnowszego wydania i podmienia go
swoim `update.bat`, więc 2.0 opublikowane z tymi samymi nazwami assetów i tagiem `v2.0.0`
zostanie przez 1.x zainstalowane. `update.bat` z 1.x kasuje `winstaller.exe.old`, a 2.0 i tak
sprząta `.old` przy starcie.

### `winstaller/__main__.py`

`--smoke` (import całego core, wczytanie katalogu, wypisanie wersji/arch, kod 0; błąd → 1),
`--version`, `--dry-run`, `--no-elevate`. Bez `--dry-run`/`--no-elevate` i bez uprawnień
admina → `relaunch_as_admin()`. UI: `from winstaller.ui.app import run`;
kontrakt **`run(*, dry_run: bool) -> int`** (kod wyjścia). Brak modułu UI → komunikat, kod 2.

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
kind = "winget"             # winget | winget_uninstall | local_exe | action
winget_id = "Valve.Steam"
post_launch = '%ProgramFiles(x86)%\Steam\Steam.exe'
description = "Krótki opis po polsku."
# opcjonalnie:
# arch = ["arm64"]                    # domyślnie wszystkie
# requires_files = ["BenchmarkTools"] # względem app_dir; pliki lub katalogi
# path = 'procyon\procyon-setup.exe'  # local_exe; glob dozwolony (ostatnie trafienie)
# args = ["/silent"]                  # local_exe: argumenty instalatora; action: argumenty akcji
# action = "copy_dir_to_desktop"      # action: klucz w actions.ACTIONS
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
- **Messenger** (`9WZDNCRF0083`) — `winget show` zwraca `0x8A150014` (pakiet nie istnieje).
  Pozycja zostaje w katalogu z `unavailable = ...` (wyszarzona z wyjaśnieniem), bez zgadywania
  zamiennika.
- **„Instaluj lokalne oprogramowanie” rozdzielone** na `local-rtss` (`RTSSSetup734.exe /S`)
  i `local-capframex` (`CapFrameXBootstrapper.exe /S`) — każda pozycja ma własną availability
  i własny status. Procyon był już osobną pozycją.
- **Kopiowanie BenchmarkTools (x64)** zostaje dostępne na obu architekturach (jak w 1.x);
  „BenchmarkToolsARM” tylko na arm64 i tylko w profilu przez `arch_items.arm64`.
- **`ensure_installed`** najpierw `Add-AppxPackage -RegisterByFamilyName` (App Installer jest
  preinstalowany w Windows 11, często tylko niezarejestrowany dla konta), potem msixbundle
  z GitHub jak w 1.x. Uwaga: na czystym systemie msixbundle może wymagać zależności (VCLibs,
  UI.Xaml) — tego 1.x też nie obsługiwał.
- **Anulowanie** zabija proces winget (terminate → 2 s → kill, jak w 1.x); instalator
  uruchomiony przez winget może działać dalej (drzewo procesów nie jest zabijane).
- **Pliki lokalne** względem `app_dir()` (katalog exe), nie `cwd`.
- **Wersja** zostaje `1.1.8` w `core/version.py` w formacie zgodnym z `bump_version.py`;
  `pyproject.toml` czyta ją dynamicznie (`winstaller.core.version.VERSION`). Bump do 2.0.0
  w etapie 3; `bump_version.py` trzeba wtedy przestawić na `winstaller/core/version.py`.
- **PyInstaller** (dla agenta buildu): `catalog.toml` musi trafić do
  `_MEIPASS/winstaller/core/catalog.toml` (np. `--add-data "winstaller/core/catalog.toml;winstaller/core"`).
