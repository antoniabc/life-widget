# Life widget

Spokojna karta ze stoperem zadań (okienko zawsze na wierzchu). Uruchamiasz skrótem **Life widget** z pulpitu.

## Wygląd i dostęp z telefonu

- **☰ → Lawenda** — jasna karta z wypukłym pierścieniem i dużym przyciskiem startu. To domyślny wygląd.
- **☰ → Ognisko RPG** — ciemna karta z ilustracją w stylu pixel art. Pasek „PD dziś” pokazuje czas względem dziennego celu.
- **☰ → Otwórz w przeglądarce** — otwiera widok w przeglądarce na tym samym komputerze.
- **☰ → Połącz telefon przez Wi-Fi…** — na Androidzie otwórz pokazany adres w przeglądarce i wpisz kod z okienka. Pełny link z kodem jest też w schowku Windows. Telefon i komputer muszą być w tej samej zaufanej sieci Wi-Fi, a widget musi pozostać uruchomiony. Jeżeli Zapora Windows zapyta o dostęp, wybierz sieć prywatną. Połączenie korzysta z lokalnego HTTP. Możesz je zamknąć z menu widgetu za pomocą **Wyłącz dostęp z przeglądarki**.

Widok w przeglądarce obsługuje stoper, dodawanie i edycję zadań, historię i wykres. Motyw wybrany na jednym ekranie pojawi się także na drugim. Import z Google Calendar, zarządzanie listą kategorii i eksport CSV są dostępne w oknie Windows. Android korzysta z wersji przeglądarkowej.

<details>
<summary>Ilustracja Ognisko RPG i prompt ImageGen</summary>

Ilustrację `web/campfire.png` wygenerowano wbudowanym ImageGen.

> Use case: illustration-story. Create an original wide 3:2 16-bit pixel art backdrop for a cozy RPG focus timer, art only and edge to edge. Reference mood: violet night, layered mountain peaks and spruce silhouettes; a tiny seated cloaked traveler to the left of a glowing amber campfire in a clearing near center, framed by trees. Dark indigo lower edge (#211b32), lilac twilight sky and sparse stars, deliberate blocky visible pixel clusters with a consistent pixel grid, calm low contrast except for the flames, no letters, UI, icons, frame or watermarks. Put traveler and fire in the central third so a wide crop retains both.

</details>

## Jak działa

- **+ Nowe**: nazwa i planowany czas (`45`, `1h30`, `1:30`; liczba bez jednostki = minuty), potem **Dodaj** albo **Dodaj i start**.
- **Zegar**: pierścień lub pasek pokazuje czas względem planu. **▶ / ⏸** uruchamia lub pauzuje stoper (start innego zadania pauzuje poprzednie), a **✓** zalicza zadanie. Kliknij nazwę zadania, aby ją zmienić. W menu **···** możesz przedłużyć plan lub usunąć zadanie.
- **Kategorie** (np. Architektura, Aplikacje): wybierasz pigułką przy „+ Nowe” (domyślnie ostatnio użyta), a zmieniasz w każdej chwili: kropka/nazwa kategorii w pierścieniu zegara, kropka przy zadaniu na liście, kliknięcie wpisu w Historii (tam też zmiana nazwy zaliczonego zadania). Zmiana działa wstecz, więc wykres od razu się przelicza. W tym samym menu: **Nowa kategoria…** i **Edytuj kategorie** (nazwa, kolor z palety lub własny, usunięcie; zadania z usuniętej kategorii przechodzą do innej). Prawy klik na pigułce przy „+ Nowe” też otwiera edycję. Pierścień zegara ma kolor kategorii.
- **Pasek „Dziś”** pod przyciskami: łączny czas pracy dzisiaj ze wszystkich zadań, rośnie na żywo i dzieli się kolorami kategorii. Stoper zadania i pasek dnia liczą się osobno, ale z tych samych bloków czasu.
- **Po przekroczeniu planu** pierścień robi się pomarańczowy, widget piknie raz, a stoper idzie dalej. Możesz przedłużyć plan albo po prostu skończyć zadanie.
- **Zadania**: lista wszystkich otwartych zadań, szybki start/pauza, wybór aktywnego.
- **Historia**: zaliczone zadania z błędem planowania i podsumowanie „zwykle trwa ×1,2 planu”. **eksport CSV** otwiera tabelę w Excelu.
- **Wykres**: słupki, gdzie wysokość = czas pracy, złożone z kolorowych odcinków kategorii; pod spodem legenda z sumą okresu dla każdej kategorii. Przerywana linia to cel dzienny (domyślnie 4 h; zmienisz kluczem `goal_h` w `config.json`). Przełącznik **Tydzień** (7 dni) / **4 tygodnie** (4 rzędy po 7 słupków z sumą tygodnia), strzałki ‹ › przesuwają okres. Dzień bez pracy to czerwona kreska. Dane z `Life/timelog.jsonl`, więc obejmują też zadania niezaliczone.
- **Minimalizacja**: przycisk **—** w prawym górnym rogu zwija kartę do małej pigułki z czasem zadania i przyciskiem ▶/⏸ (stoper liczy dalej). Kliknięcie pigułki rozwija ją z powrotem. Stan jest zapamiętywany, a pigułkę też można przeciągać.
- Przeciągnij kartę za tło, żeby ją przesunąć (pozycja jest zapamiętywana). Ctrl + kółko myszy skaluje okno.
- Zamknięcie komputera z włączonym stoperem nie psuje czasu: blok zostaje zamknięty na ostatniej chwili, kiedy widget działał.

## Dane (do analizy)

- `data/tasks.json`: otwarte zadania i stan stopera (pole `cat` = id kategorii).
- `data/categories.json`: lista kategorii `{id, name, color}`. Wpisy bez kategorii trafiają do pierwszej (Architektura).
- `data/history.jsonl`: jedna linia JSON na zadanie przy zaliczeniu (lub usunięciu, jeśli już coś odliczono). Pola: `planned_original_s` (plan pierwotny), `planned_final_s` (plan po przedłużeniach), `actual_s`, `error_s` = faktycznie − plan pierwotny, `error_pct`, `extensions` (kiedy i o ile przedłużono), `first_start`, `finished`, `note`, `source`, `cat`. Zmiana kategorii lub nazwy w Historii przepisuje tę linię.
- `Life/timelog.jsonl` i `Life/log.md` nadal są zasilane, więc `/weekreview` działa jak dotąd.

Błąd planowania liczę względem planu **pierwotnego**, a przedłużenia zapisuję osobno. Dzięki temu widać, ile naprawdę zabrało zadanie i jak bardzo je niedoszacowałeś, mimo że dokończyłeś je w przedłużonym czasie.

## Uruchamianie

Skrót na pulpicie albo:

```
pythonw "C:\Users\user\Documents\Life\tools\widget\widget.pyw"
```

Widget działa w jednym egzemplarzu: drugie uruchomienie samo się zamyka. Podgląd bez dotykania prawdziwych danych: dodaj `--demo` (pliki `data-demo/`).

Ikona: `life-widget.ico` (odtworzysz ją poleceniem `python widget.pyw --make-icon`).

**Start razem z Windowsem**: skrót „Life widget” w folderze `shell:startup` (już istnieje i wskazuje na ten sam plik). Usunięcie z autostartu: skasuj go stamtąd.

## Opcjonalnie: import z Google Calendar

Na ekranie „+ Nowe” link **Dodaj dzisiejsze z kalendarza** wczytuje dzisiejsze wydarzenia zaczynające się od ① ② ③ ○ (czas z tytułu, np. „3h”, „10 min”). Zaliczenie takiego zadania dopisuje ✓ w tytule wydarzenia. Wymaga jednorazowej konfiguracji poniżej; bez niej reszta widgetu działa normalnie.

## Jednorazowa konfiguracja Google (ok. 10 min)

1. Wejdź na <https://console.cloud.google.com/> i utwórz nowy projekt, np. „Life widget”.
2. **APIs & Services → Library** → wyszukaj **Google Calendar API** → **Enable**.
3. **Google Auth Platform** (dawniej „OAuth consent screen”) → **Get started**: nazwa aplikacji „Life widget”, Twój e-mail, typ **External**, zapisz.
4. **Audience** → **Test users** → **Add users** → swój e-mail. Aplikacja zostaje w trybie *Testing* (publikacja wymaga strony domowej i polityki prywatności). Skutek: mniej więcej co 7 dni Google prosi o ponowne logowanie; widget sam otworzy przeglądarkę. Przy logowaniu zobaczysz ostrzeżenie „Google hasn't verified this app”, kliknij **Advanced → Go to Life widget**. To Twoja własna aplikacja.
5. **Clients → Create client** → typ **Desktop app** → **Create** → **Download JSON**.
6. Zapisz pobrany plik jako `tools/widget/credentials.json` (w tym folderze).
7. Uruchom widget (niżej). Otworzy się przeglądarka: zaloguj się na swoje konto Google i zezwól na dostęp do kalendarza. Token zapisze się w `token.json`.

`credentials.json` i `token.json` dają dostęp do Twojego kalendarza; nie wysyłaj ich nikomu.

## Uruchamianie

```
pythonw "C:\Users\user\Documents\Life\tools\widget\widget.pyw"
```

Podgląd bez kalendarza: dodaj `--demo` (zapisuje do plików `*-demo` w tym folderze).

**Start razem z Windowsem** (skrót w folderze Autostart):

```
powershell -NoProfile -Command "$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Startup')+'\Life widget.lnk');$s.TargetPath=(Get-Command pythonw).Source;$s.Arguments='\"C:\Users\user\Documents\Life\tools\widget\widget.pyw\"';$s.Save()"
```

Usunięcie z autostartu: skasuj „Life widget” z folderu `shell:startup`.
