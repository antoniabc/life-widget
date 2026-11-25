# Life widget

Stoper zadań na pulpit i telefon. Okienko zawsze na wierzchu, z widokiem w przeglądarce.

**Apka w przeglądarce:** https://life-timer-786a.web.app/

## Funkcje

- Zadania z planowanym czasem, stoper, kategorie i historia z wykresem.
- Dwa wyglądy: Lawenda i Ognisko RPG (menu ☰).
- Widok w przeglądarce i na telefonie przez Wi-Fi (menu ☰ → Połącz telefon).
- Opcjonalny import dzisiejszych zadań z Google Calendar.

## Uruchomienie (Windows + Python 3)

```
pythonw widget.pyw
```

Podgląd bez prawdziwych danych: dodaj `--demo`.

## Import z Google Calendar (opcjonalnie)

Potrzebujesz własnego `credentials.json` (Google Cloud → Calendar API → OAuth client typu *Desktop app*), zapisanego obok `widget.pyw`. Token zapisze się w `token.json`. Nie udostępniaj tych plików nikomu.

## Licencja

MIT
