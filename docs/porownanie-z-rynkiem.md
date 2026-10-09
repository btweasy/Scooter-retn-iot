# Wypożyczenie – porównanie z usługami komercyjnymi

Punkt odniesienia: funkcje typowe dla aplikacji Bolt, Lime i Dott/Tier (opis ogólny – szczegóły,
nazwy funkcji i ceny różnią się między operatorami i miastami).

| Funkcja | Typowo na rynku | Prototyp – przed | Prototyp – teraz |
|---|---|---|---|
| Mapa dostępnych pojazdów z baterią | tak | tak | tak + szacowany **zasięg w km** |
| Rezerwacja pojazdu na kilka minut | tak (bezpłatna lub tania) | brak | **tak** – 10 min, bezpłatnie, automatyczne wygaśnięcie |
| Cennik: odblokowanie + minuta | tak | tak | tak, za **każdą rozpoczętą minutę** |
| Cena ustalona przy starcie jazdy | tak | nie (cennik z konfiguracji w chwili końca) | **tak** – cennik zapisany w wypożyczeniu |
| Pauza w trakcie jazdy (pojazd zablokowany, tańsza stawka) | tak | brak | **tak** – 0,30 zł/min, komenda `lock`/`unlock` |
| Zakaz kończenia jazdy w strefie zakazu parkowania | tak | brak (tylko alert dla operatora) | **tak** – błąd `no_parking_zone`, operator może wymusić |
| Paragon z rozbiciem kosztów | tak | tylko suma | **tak** – odblokowanie, minuty jazdy, minuty pauzy |
| Historia przejazdów z trasą | tak | brak | **tak** – lista „Historia”, trasa na mapie |
| Blokada wypożyczenia przy niskiej baterii | tak | tak (< 15%) | tak |
| Zamknięcie jazdy, gdy pojazd nie zareagował | po stronie operatora | brak | **tak** – automatycznie po 60 s, bezpłatnie bez dystansu |
| Strefy ograniczenia prędkości | tak | brak | brak – **plan** (komenda `limit` do pojazdu) |
| Zdjęcie zaparkowanego pojazdu | często | brak | brak – plan (wymaga aplikacji klienta) |
| Płatności, konta, karnety | tak | brak | brak – poza zakresem prototypu |
| Aplikacja mobilna klienta | tak (natywna) | brak | **tak – PWA** „Hulaj”: ekran startowy, wprowadzenie, mapa z kartą na dole, rezerwacja z odliczaniem, ekran jazdy, paragon, historia; instalowalna, startuje bez sieci |
| Pokazywanie klientowi tylko wolnych pojazdów i odległości pieszo | tak | brak | **tak** – najbliższe hulajnogi z zasięgiem i czasem dojścia |

## Co zostało dodane i dlaczego

1. **Rezerwacja** – klient idący do hulajnogi nie chce, żeby ktoś mu ją zabrał. Przy okazji pojawił się
   nowy status pojazdu `reserved` (fioletowy).
2. **Pauza** – krótki postój (sklep, kawa) bez kończenia jazdy: pojazd jest zablokowany (komenda `lock`
   przez MQTT), pozostaje przypisany do klienta, minuty są tańsze. Wypożyczenie ma statusy
   `active ⇄ paused → finished`.
3. **Zakaz kończenia jazdy w strefie zakazu parkowania** – wcześniej system tylko informował operatora
   alertem. Teraz zasada jest egzekwowana (jak w aplikacjach komercyjnych); alert pozostał dla przypadków,
   gdy pojazd trafi do strefy inaczej.
4. **Paragon i cena z chwili startu** – klient widzi, za co płaci; zmiana cennika nie dotyka trwających jazd.
5. **Historia** – zakończone wypożyczenia z trasą i paragonem.
6. **Zasięg** – klient ocenia, czy dojedzie, zanim wypożyczy.

## Czego świadomie nie dodano

Układ aplikacji klienta wzorowany jest na wspólnym schemacie aplikacji Bolt/Lime/Dott (mapa na cały
ekran, karta wysuwana od dołu, duży licznik jazdy, paragon na końcu), z własną nazwą i kolorystyką.

Płatności, konta użytkowników i aplikacja natywna wymagają osobnych systemów (bramka płatnicza, JWT, PWA)
i nie są związane z tematem pracy (telemetria IoT w czasie rzeczywistym). Strefy ograniczenia prędkości
są dobrym kolejnym krokiem, bo wykorzystują istniejący kanał komend MQTT.
