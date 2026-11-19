# Testy wydajnościowe (`-m perf`)

```powershell
.venv\Scripts\python -m pytest -m perf            # tylko perf (kilka sekund)
.venv\Scripts\python -m pytest -m "not perf"      # bez perf
$env:WINSTALLER_PERF_SLACK = "2"; .venv\Scripts\python -m pytest -m perf   # luźniejsze progi (wolny CI)
```

Na końcu biegu drukowana jest tabela „Winstaller perf: 2.0 vs 1.1.8 baseline” oraz tabela
pytest-benchmark (`--benchmark-disable` też działa — testy mierzą wtedy same).

## Pliki

| Plik | Rola |
|---|---|
| `corpus.py` | wspólne wejścia: konkatenacja próbek z `tests/data/*.bin` (realne `winget show/search`, syntetyczna instalacja z `\r`, spinnerem, CSI, OSC 9;4, `\b`, polskie znaki w UTF-8/CP1250/CP852), strumień samego paska postępu w skalach 1x/4x/16x, obciążenie kalibracyjne |
| `measure_baseline.py` | ręczny pomiar wersji 1.1.8 → `baseline.json` (instrukcja w docstringu) |
| `baseline.json` | wynik pomiaru 1.1.8: maszyna, data (RRRR.MM.DD), wersja Pythona, liczba powtórzeń, mediana, min/max/odchylenie i surowe przebiegi |
| `test_perf.py` | testy 2.0 porównujące z baseline'em i/lub z budżetami absolutnymi |

Testy **nie importują starego kodu** (zostanie usunięty w etapie 3) — porównują się tylko z
`baseline.json`. Żeby odświeżyć baseline (np. po zmianie próbek w `tests/data`, co wykrywa
`test_corpus_is_the_one_baseline_was_measured_with` przez SHA256 korpusu), uruchom
`measure_baseline.py` na kodzie wypakowanym z gałęzi `main`/tagu `v1.1.8` w osobnym venv z
`customtkinter==5.2.2`, `darkdetect`, `requests`. Skrypt na chwilę pokazuje okno 1.1.8
(pomiar `time_to_window`), poza tym uruchamia wyłącznie procesy Pythona.

## Co jest mierzone i jak porównywane

| Test | 2.0 | Punkt odniesienia | Próg |
|---|---|---|---|
| przepustowość `StreamParser` (512 B na `feed`, jak `read(512)` w 1.x) | MB/s, linie/s | `_read_stream` 1.1.8 bez `sleep` (czysty CPU), przeskalowany kalibracją | ≥ 0.8× baseline'u i ≥ 2 MB/s |
| to samo wobec realnego czytnika 1.1.8 | MB/s | `_read_stream` z `time.sleep(0.01)`/chunk | ≥ 10× |
| złożoność | koszt na bajt dla 1x/4x/16x | 1.1.8: czas 16x/1x ≈ 228 (kwadratowo) | koszt/bajt 4x ≤ 2×, 16x ≤ 3× kosztu 1x |
| `load_catalog()` | mediana | `importlib.reload(core.config)` (tylko informacyjnie: literał słownika vs plik TOML + walidacja) | ≤ 50 ms × kalibracja |
| import `winstaller.core.*` | `-X importtime`, suma modułów `winstaller*` | `import core.config` (ciepły) | ≤ baseline × kalibracja |
| `python -m winstaller --smoke` | mediana z 5 świeżych procesów, także po odjęciu `python -c pass` | `python -c "import core.config"` (proces) | ≤ 1.5 s oraz nadwyżka ponad gołe uruchomienie ≤ nadwyżka 1.1.8 × kalibracja |
| brak `PySide6`/`requests`/`tkinter` po imporcie core i `--smoke` | `sys.modules` w podprocesie | — | lista pusta |
| narzut `TaskRunner` | 1000 pustych zadań, listener zliczający | — | ≤ 200 µs na zadanie |
| `stream_process` end-to-end | proces potomny wypisuje 50 000 linii, wszystkie muszą dojść w kolejności | — | ≥ 20 000 linii/s |

Wszystkie progi mnoży `WINSTALLER_PERF_SLACK` (domyślnie 1, wartości < 1 ignorowane).
Na tej maszynie (AMD Ryzen 5 8400F, Python 3.12.10, 2026.11.19) zapas wynosi 2.5–100×,
więc zwykły szum nie powinien wywracać testów.

## Co jest porównywalne między maszynami, a co nie

- **Kalibracja** (`machine_factor`): ten sam deterministyczny wsad (pętla po bajtach, regex,
  operacje na `bytes`) jest mierzony w `baseline.json` i w bieżącej sesji; ich iloraz skaluje
  liczby 1.1.8 do bieżącej maszyny (ograniczony do 0.2–10). Dobrze przenosi koszt
  **czystego CPU w CPythonie** (parser), słabo — wszystko, co zależy od dysku, antywirusa,
  liczby rdzeni czy implementacji regexów na danej architekturze (ARM64 vs x64 mogą się
  różnić proporcjami między bytecode a `re`).
- **Porównywalne z grubsza**: przepustowość parsera, kształt złożoności (to iloraz z jednej
  maszyny, więc przenosi się dobrze), import i start procesu po przeskalowaniu — dlatego progi
  mają duży zapas.
- **Nieporównywalne bezpośrednio**: liczby bezwzględne w ms/MB/s między różnymi maszynami;
  `load_catalog()` vs `reload(core.config)` (inna praca — tylko informacyjnie, próg jest
  absolutny); „zimny” import w baseline oznacza tylko usunięte `__pycache__` starego kodu
  (z kompilacją), pamięć podręczna plików systemu **nie** jest czyszczona; na runnerach CI
  pierwszy start Pythona bywa wielokrotnie wolniejszy (skan antywirusa) — stąd mediany.
- Venv na Windows uruchamia interpreter przez launcher (dwa procesy), więc czasy startu
  procesu zawierają ten narzut po obu stronach.
- Testy mierzą wątek wywołujący; przy równoległym uruchamianiu testów (np. `-n` xdist) wyniki
  tracą sens — perf uruchamiaj sekwencyjnie.

## Etap późniejszy: start UI i exe (jeszcze nie napisane)

Gdy powstanie `winstaller.ui` (PySide6), dojdą pomiary:

- **czas do pokazania okna**: świeży proces `python -m winstaller --dry-run --no-elevate`
  (oraz zbudowany `winstaller.exe`) do pierwszego `QWindow.exposeEvent`/`showEvent` głównego
  okna z listą pozycji katalogu — porównanie z `window.time_to_window_s` z `baseline.json`
  (1.1.8: `ModernApp` + 36 checkboxów + `update_idletasks()`, mediana ~0.68 s na tej maszynie;
  rozbicie na import / konstrukcję / checkboxy / idletasks w `window.breakdown`);
- czas importu `winstaller.ui` (PySide6 ładowany dopiero tam) i to, że `--smoke` nadal nie
  ładuje Qt;
- start exe onefile PyInstallera (rozpakowanie `_MEI`) — 1.1.8 nie ma tu baseline'u w tym
  pliku; trzeba by go zmierzyć na zbudowanym `winstaller-x64.exe` 1.1.8 z GitHub Releases;
- płynność logu: liczba zdarzeń `output_*` na sekundę obsłużona przez most Qt bez blokowania
  wątku GUI.
