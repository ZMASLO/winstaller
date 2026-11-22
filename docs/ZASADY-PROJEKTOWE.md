# Zasady wizualne i projektowe

Spis zasad, według których powstał Winstaller 2.0, napisany tak, żeby dało się go przenieść
do innego projektu z tej samej rodziny (np. programu do uruchamiania benchmarków). Sekcje
1–3 dotyczą wyglądu i zachowania interfejsu, 4–8 budowy kodu, testów i sposobu pracy.

Wartości kolorów i rozmiarów pochodzą z `winstaller/ui/theme/tokens.py` i
`winstaller/ui/theme/fonts.py` — to one są źródłem prawdy, ten plik je tylko opisuje.

---

## 1. Wygląd

Estetyka terminala: prawie czarne tło, jeden żółty akcent, ciężki grotesk na nagłówki i
mono wersalikami na wszystko drobne. Ma wyglądać jak narzędzie, nie jak aplikacja mobilna.

### 1.1 Kolory

| Rola | Ciemny (podstawowy) | Jasny |
|---|---|---|
| Tło okna | `#0D0D0D` | `#F4F4F1` |
| Powierzchnia (nawigacja, paski, karty) | `#141414` | `#FBFBF9` |
| Powierzchnia uniesiona / hover | `#1C1C1C` | `#E9E9E4` |
| Separator 1 px | `#262626` | `#D8D8D3` |
| Obrys kontrolek drugorzędnych | `#3A3A38` | `#B9B9B3` |
| Tekst główny | `#F0F0EE` | `#141414` |
| Tekst pomocniczy | `#8A8A85` | `#5E5E59` |
| Tekst nieaktywny | `#5C5C58` | `#9A9A94` |
| Akcent (wypełnienia) | `#E9C84A` | `#E9C84A` |
| Akcent jako tekst lub cienka linia | `#E9C84A` | `#7A5F00` |
| Tekst na akcencie | `#231D02` | `#231D02` |
| Błąd: linie i obrysy | `#C4453A` | `#B3362C` |
| Błąd: tekst | `#E05A4E` | `#B3362C` |

Zasady użycia:

- **Żółty jest rzadki.** Oznacza jedną rzecz naraz: główny przycisk, zaznaczenie, aktywną
  pozycję nawigacji, bieżące zadanie, postęp. Trzy żółte elementy obok siebie znaczą, że
  żaden nie jest ważny.
- **Sukces nie jest zielony.** `[OK]` jest w kolorze tekstu. Pominięte i anulowane są
  wygaszone szarością. Czerwony pojawia się wyłącznie przy prawdziwym błędzie.
- **Nigdy czysta czerń ani czysta biel.** Tekst na żółtym jest ciemnobrązowy, nie czarny.
- **Kontrast tekstu co najmniej 4,5:1** względem tła; każdy token tekstowy jest sprawdzony.
  Dlatego tekst błędu ma inny odcień niż linia błędu, a akcent w jasnym motywie jest
  przyciemniony, gdy służy za tekst.
- **Kolory tylko z tokenów.** Żadnych wartości wpisanych w widżetach.
- Motyw: systemowy, ciemny lub jasny; ciemny jest wzorcowy, jasny jest jego odpowiednikiem.

### 1.2 Typografia

| Rola | Krój | Rozmiar | Waga | Uwagi |
|---|---|---|---|---|
| Tytuł ekranu | grotesk | 34 px | pogrubiony | |
| Nagłówek strony | grotesk | 26 px | pogrubiony | |
| Nagłówek karty | grotesk | 17 px | pogrubiony | |
| Tekst | sans | 14 px | zwykły | opisy w kolorze pomocniczym |
| Tekst drobny | sans | 12 px | zwykły | |
| Etykieta | mono | 11 px | półgruby | WERSALIKI, rozstrzelone ok. 1,5–2 px |
| Etykieta mała | mono | 10 px | zwykły | WERSALIKI, rozstrzelone |
| Log, wartości | mono | 12 px | zwykły | bez wersalików |

- Kroje: Geist i Geist Mono, a gdy ich nie ma w systemie — Segoe UI Variable / Segoe UI oraz
  Cascadia Mono / Consolas. Fonty są wyłącznie systemowe; niczego nie pobieramy.
- Wersaliki mono dostaje wszystko drobne: etykiety, tagi, liczniki, metadane, przyciski.
- Tytuły zdaniową wielkością liter, nie Każde Słowo Wielką.

### 1.3 Konwencje zapisu

- Nagłówki sekcji jak komentarze w kodzie, mono, w kolorze akcentu:
  `// PODSTAWOWE PROGRAMY`, `// KOLEJKA — 3 / 12`.
- Tagi w nawiasach kwadratowych: `[WINGET]`, `[AKCJA]`, `[STEROWNIK]`, `[OK]`, `[BŁĄD]`,
  `[POMINIĘTO]`, `[DRY-RUN]`.
- Kursor (mały żółty prostokąt) jako znak rozpoznawczy przy tytule programu i przy bieżącym
  zadaniu. Miga tylko to, co właśnie trwa.
- Strzałka `→` przy przejściach i linkach. Daty z kropkami: `2026.11.22`.
- Pozycje nawigacji numerowane: `01 APLIKACJE`, `02 SYSTEM`.

### 1.4 Układ

- Dużo powietrza, wyrównanie do lewej, mocna siatka.
- Elementy rozdziela linia 1 px, nie ramka wokół wszystkiego.
- Narożniki ostre lub minimalnie zaokrąglone (0–2 px).
- Płasko: żadnych cieni, poświat ani gradientów.
- Przycisk główny: żółte wypełnienie i ciemny tekst. Drugorzędny: przezroczysty z obrysem.
- Wyraźne stany hover, focus i wyłączony. Obwódka fokusu widoczna przy nawigacji klawiaturą.
- Okno ok. 1100×720, minimum ok. 900×600, poprawne przy skalowaniu 125–200%.

---

## 2. Zachowanie interfejsu

- **Mało kliknięć.** Narzędzie jest używane wielokrotnie na świeżych systemach; typowa
  ścieżka to profil, jeden przycisk, wynik.
- **Jedna główna akcja na ekran**, zawsze w tym samym miejscu (stały dolny pasek), z liczbą:
  „Zainstaluj (12)”.
- **Stan zawsze widoczny.** Długa operacja pokazuje, co robi teraz, ile zostało i jak ją
  przerwać. Jeśli coś dzieje się w tle, interfejs musi to pokazywać — sama informacja
  „uruchomiono” nie wystarcza.
- **Wynik każdego elementu osobno**: status, krótki komunikat, czas. Na końcu podsumowanie
  z liczbami i „Ponów nieudane”.
- **Niedostępne nie znika.** Pozycja, której nie da się użyć, jest wyszarzona i mówi dlaczego
  (zła architektura, brak pliku), zamiast zniknąć z listy.
- **Potwierdzenia tylko dla działań destrukcyjnych lub nieodwracalnych**, jedno zbiorcze
  zamiast serii okien.
- **Bez modalnych okien informacyjnych.** Wyniki i błędy pojawiają się w treści strony lub
  jako baner; dialog jest tylko wtedy, gdy trzeba podjąć decyzję.
- **Błąd mówi, co się stało i co zrobić dalej**, po polsku, z kodem w nawiasie na końcu.
  Bez śladów stosu w interfejsie.
- **Nic nie restartuje się samo.** Program mówi, że restart jest potrzebny; decyduje człowiek.
- **Klawiatura:** Tab i strzałki, Spacja przełącza, skróty dla wyszukiwania i głównej akcji.
- **Okno pojawia się szybko.** Przed pokazaniem okna nie ma sieci ani uruchamiania procesów;
  sprawdzenia startowe idą w tle i nie blokują.
- **Tryb symulacji (`--dry-run`)** przechodzi pełną ścieżkę bez zmian w systemie i jest
  wyraźnie oznaczony. Służy do testów ręcznych, zrzutów ekranu i testów automatycznych.

---

## 3. Język

- Komunikaty dla użytkownika po polsku, z polskimi znakami, zwykłymi zdaniami.
- Etykieta mówi, co się stanie: „Zainstaluj (12)”, nie „OK”. Ta sama akcja ma wszędzie tę
  samą nazwę.
- Opis pozycji mówi wprost o skutkach: rozmiar pobierania, restart, brak sprawdzania
  zgodności.
- Nie obiecujemy tego, czego program nie potwierdził. Jeśli wyniku nie da się sprawdzić,
  komunikat brzmi „zlecono, wyniku nie da się potwierdzić”, nie „gotowe”.
- W kodzie identyfikatory, docstringi i komentarze po angielsku; komentarze tylko tam, gdzie
  wyjaśniają „dlaczego”.

---

## 4. Budowa programu

- **Rdzeń bez interfejsu.** Logika (`core/`) nie importuje biblioteki UI i daje się uruchomić
  oraz przetestować bez okna. Interfejs tylko wywołuje rdzeń i pokazuje stan.
- **Rdzeń jest synchroniczny; wątki należą do interfejsu.** Praca blokująca idzie w wątku
  roboczym, a widżety są dotykane wyłącznie z wątku interfejsu (sygnały). Zdarzenia niosą
  niezmienne migawki danych.
- **Dane zamiast kodu.** Lista pozycji, kategorie i profile są w pliku TOML, z walidacją przy
  wczytaniu i czytelnymi błędami. Dodanie pozycji nie wymaga zmiany kodu. Identyfikatory są
  stabilne.
- **Zależności wstrzykiwane.** Wszystko z zewnątrz (uruchamianie zadań, sieć, potwierdzenia,
  otwieranie plików) trafia do okna przez jeden obiekt `Services`, więc testy podstawiają
  atrapy.
- **Cienka warstwa systemowa.** Procesy, rejestr, sieć i pliki są wołane przez małe funkcje
  modułowe, które da się podmienić w testach.
- **Parsery są czyste i nie ufają wejściu.** Funkcja parsująca nie robi wejścia/wyjścia i dla
  każdego złego wejścia rzuca jeden, własny typ błędu — nigdy przypadkowy wyjątek.
- **Błędy mają typ i komunikat dla użytkownika.** Jedno nieudane zadanie nie przerywa reszty.
- **Anulowanie i limit czasu od początku.** Każda długa operacja reaguje na „Stop” i ma limit
  czasu; przerwanie zabija całe drzewo procesów.
- **Tylko to, co potrzebne.** Bez martwego kodu „na zapas”, bez opcji, których nikt nie
  używa, bez warstw abstrakcji dla jednego przypadku.

---

## 5. Bezpieczeństwo (program działa jako administrator)

- **Programy systemowe pełną ścieżką** z System32, nigdy po samej nazwie.
- **Dane zewnętrzne nie trafiają do treści skryptów ani poleceń.** Parametry idą przez
  zmienne środowiskowe lub argumenty; nic nie jest sklejane w tekst skryptu.
- **Proces potomny dostaje zamkniętą listę zmiennych środowiskowych.**
- **Plik pobrany z sieci jest uruchamiany dopiero po weryfikacji:** tylko serwer producenta
  przez https (każde przekierowanie sprawdzane), rozmiar, suma kontrolna gdy istnieje, podpis
  cyfrowy z dokładną nazwą wydawcy.
- **Plik nie może się zmienić między sprawdzeniem a uruchomieniem:** świeży losowy katalog,
  blokada pliku, ponowne policzenie sumy.
- **Katalogi używane przez proces podniesiony są tworzone z właściwymi uprawnieniami od
  razu.** Cudzego katalogu program nie „naprawia” — zakłada własny.
- **Fałszywy błąd jest lepszy niż fałszywy sukces.** Gdy wynik jest niejednoznaczny, zadanie
  kończy się błędem albo „niepotwierdzone”.
- **Bezpieczna porażka.** Gdy weryfikacja zawodzi, nic się nie uruchamia, a komunikat wskazuje,
  co zrobić ręcznie.

---

## 6. Testy

- **Trzy rodzaje:** jednostkowe rdzenia, interfejsu (offscreen) i wydajnościowe.
- **Testy nigdy nie dotykają prawdziwego systemu.** Wspólny bezpiecznik oblewa każdy test,
  który próbuje uruchomić program systemowy, pisać do rejestru albo łączyć się z siecią.
  Bezpiecznika się nie osłabia.
- **Prawdziwe próbki danych.** Odpowiedzi usług i wyjście programów są nagrane i trzymane
  jako pliki testowe bajt w bajt; testy sprawdzają je oraz ich zniekształcone wersje.
- **Testy szukają błędów, nie potwierdzają implementacji.** Przypadki brzegowe, ścieżki
  błędów, podział wejścia w dowolnym miejscu, losowe dane z ustalonym ziarnem.
- **Znaleziony błąd najpierw dostaje test**, który go pokazuje, potem poprawkę. Testu nie
  dopasowuje się do błędnego zachowania.
- **Testy interfejsu są deterministyczne:** bez `sleep`, z czekaniem na sygnał lub warunek.
  Ostrzeżenie biblioteki UI albo wyjątek w wątku oblewa test.
- **Wydajność ma punkt odniesienia.** Pomiar poprzedniej wersji jest zapisany, a testy
  pilnują budżetów (czas do okna, przepustowość, rozmiar pliku wykonywalnego) z zapasem na
  wolniejsze maszyny.
- **Test „na żywo”** (sprawdzający, czy zewnętrzne źródła nadal odpowiadają) jest domyślnie
  wyłączony i niczego nie pobiera ani nie zmienia.
- **Zrzuty ekranu ze skryptu.** Każdy stan interfejsu da się wyrenderować jednym poleceniem
  i obejrzeć przed oddaniem zmiany.

---

## 7. Budowanie i wydania

- Jeden plik `.exe`, budowany tym samym przepisem dla x64 i ARM64, bez kompresji UPX.
- Do pliku trafia tylko to, czego program używa; rozmiar i czas startu mają budżet w CI.
- Zbudowany plik ma tryb `--smoke`: szybki test bez okna i bez uprawnień administratora.
- Podniesienie uprawnień robi sam program przy starcie, nie manifest — dzięki temu testy
  i symulacja nie wywołują okna UAC.
- CI: lint, testy i build na obu architekturach; wydanie robocze z sumami kontrolnymi tylko
  na tagu.
- Jedno źródło zależności (`pyproject.toml`) i jedno źródło numeru wersji.

---

## 8. Sposób pracy

- **Najpierw plan, potem kod.** Plan mówi, co i dlaczego, wymienia decyzje do podjęcia
  i sposób weryfikacji. Decyzje o zakresie podejmuje właściciel, zanim ruszy implementacja.
- **Najpierw sprawdzić, potem zakładać.** Mechanizmy nieudokumentowane (API producentów,
  zachowanie systemu) są badane realnymi zapytaniami tylko do odczytu, a wynik badania
  rozróżnia: zweryfikowane, z dokumentacji, niepewne.
- **Podział ról:** jeden agent bada, drugi implementuje, trzeci — niezależny — pisze testy
  i szuka błędów, implementujący je naprawia, a na końcu jest osobna weryfikacja: przegląd
  zmian, testy, zrzuty, build.
- **Raport jest deklaracją, nie dowodem.** Każdy etap jest sprawdzany na nowo przed
  zatwierdzeniem.
- **Uczciwe raportowanie.** Każdy raport ma listę tego, czego nie zrobiono i nie
  zweryfikowano. To, czego nie da się sprawdzić bez zmieniania systemu, jest wprost
  oddawane do testu na sprzęcie.
- **Nic nie zmienia systemu bez zgody:** żadnych realnych instalacji, zmian rejestru,
  restartów ani aktualizacji podczas pracy nad kodem. Polecenie, które „tylko listuje”,
  też może mieć skutki uboczne — sprawdzić, zanim się je uruchomi.
- **Małe, opisane commity po zweryfikowanym etapie.** Wypchnięcie na serwer tylko na
  wyraźne polecenie.
- **Dokumentacja idzie razem z kodem:** mapa projektu i kontrakty w `docs/ARCHITECTURE.md`,
  zasady dla pracy nad kodem w `CLAUDE.md`, opis dla użytkownika w `README.md`.
