# Przewodnik po kodzie (do obrony)

## Mapa plików

```
emulator/emulator.py          flota hulajnóg: ruch, bateria, telemetria, komendy, "inni użytkownicy"
mosquitto/                    broker MQTT: konfiguracja, ACL (kto może pisać/czytać które tematy)
backend/app/
  main.py                     start aplikacji: endpointy, wątki w tle, obsługa błędów
  config.py                   wszystkie ustawienia (zmienne środowiskowe)
  models.py                   tabele bazy: scooters, telemetry, reservations, rentals, alerts
  schemas.py                  walidacja danych wejściowych (Pydantic)
  routers/                    endpointy HTTP – cienkie, tylko wejście i wyjście
    telemetry.py              POST /api/telemetry (zapasowa droga bez MQTT)
    rentals.py                rezerwacje i wypożyczenia
    fleet.py                  pojazdy, statystyki, alerty, metryki
    live.py                   zrzut stanu i strumień SSE dla panelu
  mqtt_client.py              odbiór telemetrii i wysyłanie komend przez MQTT
  worker.py                   wątki w tle: zapis partiami, porządki, retencja
  ingest.py                   przetwarzanie jednego pomiaru (duplikaty, stan, historia)
  alerts.py                   alerty: strefa zakazu, bateria, jazda bez wypożyczenia
  status.py                   status pojazdu (available, reserved, in_ride, paused, low_battery, offline)
  rentals.py                  logika wypożyczeń: rezerwacja, start, pauza, koniec, paragon
  rental_watchdog.py          wygaszanie rezerwacji, zamykanie osieroconych wypożyczeń
  commands.py                 komendy do pojazdu: MQTT albo kolejka dla HTTP
  geo.py                      strefy (Shapely) i długość trasy z filtrem szumu GPS (geopy)
  zones_data.py               obrysy stref zakazu parkowania (z OpenStreetMap, tools/fetch_zones.py)
  metrics.py                  metryki do badań: opóźnienie, przepustowość, duplikaty, straty
  serializers.py              zamiana obiektów bazy na JSON
backend/web/                  dwie strony w przeglądarce (bez kompilacji):
  index.html, client.*        aplikacja klienta "Hulaj" (PWA) pod adresem /
  manifest.webmanifest, sw.js   PWA: instalacja na telefonie, start bez sieci
  icons/                      ikona (SVG) + PNG z tools/make_icons.py
  panel.html, panel.*         panel operatora pod adresem /panel
backend/migrations/           migracje schematu bazy (Alembic)
backend/tests/                testy pytest
```

## Trzy przepływy, które warto umieć opowiedzieć

### 1. Pomiar z hulajnogi trafia na mapę

1. Emulator (`Scooter.telemetry`) publikuje JSON na `scooters/<id>/telemetry` z QoS 1.
2. `mqtt_client._handle_message` waliduje go (`TelemetryIn`), sprawdza, czy id zgadza się z tematem,
   i wkłada do kolejki (`worker.submit`). Nie pisze do bazy – wątek sieciowy musi być szybki.
3. Wątek `worker._ingest_loop` zbiera partię (do 500 wiadomości lub 0,2 s) i woła `ingest.ingest_batch`:
   jedna transakcja na całą partię.
4. `ingest.ingest_message`: odrzuca duplikat/spóźnioną wiadomość (`is_duplicate_or_late`), liczy straty
   (`count_lost_messages`), aktualizuje wiersz w `scooters`, sprawdza alerty, dopisuje wiersz do `telemetry`.
5. Panel ma otwarty strumień `/api/stream` – co 2 s dostaje zrzut stanu (`live.build_snapshot`),
   w którym `status.scooter_status` wylicza kolor każdego pojazdu.

### 2. Wypożyczenie od rezerwacji do paragonu

1. `POST /api/reservations` → `rentals.reserve`: pojazd musi być `available`; powstaje rezerwacja na 10 min.
2. `POST /api/rentals/start` → `rentals.start_rental`: cudza rezerwacja = błąd `reserved`; cennik zostaje
   skopiowany do wypożyczenia; `commands.send(..., "unlock")` wysyła komendę do pojazdu.
3. `POST /api/rentals/{id}/pause` / `resume` – zmiana statusu i komenda `lock` / `unlock`;
   czas pauzy sumuje się w `paused_seconds`.
4. `POST /api/rentals/{id}/end` → `rentals.end_rental`: odmowa w strefie zakazu (chyba że `force`),
   trasa z telemetrii (`route_points` + `geo.track_length_km`), paragon (`calculate_receipt`), komenda `lock`.
5. `change_status` zmienia wiersz warunkowo (`WHERE status = ...`) – dwa równoczesne „zakończ”
   nie policzą jazdy dwa razy. Unikalny indeks w bazie pilnuje, by pojazd miał najwyżej jedno otwarte
   wypożyczenie i jedną aktywną rezerwację.

### 3. Coś poszło nie tak

| Sytuacja | Reakcja | Gdzie |
|---|---|---|
| Ta sama wiadomość przyszła dwa razy (QoS 1) | odrzucona, liczona jako duplikat | `ingest.is_duplicate_or_late` |
| Zgubione wiadomości | luka w `seq` liczona jako strata | `ingest.count_lost_messages` |
| Backend chwilowo rozłączony z brokerem | broker przechowuje wiadomości (trwała sesja) | `mqtt_client.start` |
| Pojazd przestał nadawać | po 30 s status `offline` | `status.scooter_status` |
| Komenda `unlock` zaginęła | po 60 s wypożyczenie zamknięte automatycznie + alert | `rental_watchdog.py` |
| Pojazd odblokowany bez wypożyczenia | alert `unauthorized` | `alerts.check_unauthorized_ride` |
| Szum GPS u stojącego pojazdu | nie zwiększa dystansu, nie dubluje alertów | `geo.track_length_km`, `alerts.check_parking_zone` |

### 4. Aplikacja klienta (PWA)

1. Ekran startowy → wprowadzenie (3 slajdy, tylko za pierwszym razem) → podanie imienia
   (z niego powstaje `user_id`, zapamiętany w `localStorage`) → mapa.
2. Mapa pokazuje tylko wolne hulajnogi i moją (jak Bolt/Lime); karta na dole ma 5 widoków
   wybieranych przez `currentView()`: lista najbliższych → wybrana hulajnoga → rezerwacja z odliczaniem
   → jazda (czas, dystans, koszt, pauza) → paragon. Historia przejazdów w osobnym panelu.
3. Dane: ten sam strumień SSE co panel operatora, filtrowany po `user_id`; akcje to te same endpointy
   (`/api/reservations`, `/api/rentals/...`). Cennik z `GET /api/tariff`.
4. PWA: `manifest.webmanifest` (nazwa, ikony, tryb pełnoekranowy – "Dodaj do ekranu głównego")
   i `sw.js` (service worker: pliki aplikacji w pamięci podręcznej, więc aplikacja uruchamia się
   bez sieci i pokazuje komunikat zamiast pustej strony; dane `/api` zawsze z sieci).

## Typowe pytania

- **Dlaczego MQTT, a nie HTTP?** Lekki protokół dla urządzeń, stałe połączenie, QoS, komunikacja w obie
  strony (komendy). HTTP zostało jako zapasowa droga.
- **Dlaczego status nie jest zapisany w bazie?** Wyliczany zawsze z aktualnych danych – nie może się
  „rozjechać” z rzeczywistością (np. offline wynika z czasu ostatniego pomiaru).
- **Jak mierzone jest opóźnienie?** Urządzenie wysyła `sent_at`; backend liczy `czas zapisu − sent_at`
  (`metrics.py`, `GET /api/metrics`). Wymaga zsynchronizowanych zegarów (NTP).
- **Dlaczego zapis partiami?** Jeden commit na wiadomość ogranicza przepustowość do liczby transakcji
  na sekundę, jaką wytrzyma baza; partia = jedna transakcja na setki wiadomości.
- **Dlaczego PWA, a nie aplikacja natywna?** Jeden kod dla Androida i iPhone'a, instalacja prosto
  z przeglądarki, bez sklepu z aplikacjami; do prototypu wystarcza (mapa, lokalizacja, praca offline).
  Ograniczenia: na iOS słabsze powiadomienia push, brak dostępu do NFC/Bluetooth.
- **Jak backend wie, że pojazd jest w strefie?** `Polygon.contains(Point)` z biblioteki Shapely (`geo.zone_for`).
- **Skąd kształty stref?** Z OpenStreetMap: skrypt `tools/fetch_zones.py` pyta serwis Nominatim o miejsca
  (np. „Park Kultury i Wypoczynku, Słupsk”), dostaje obrys, upraszcza go i zapisuje w `app/zones_data.py`.
  Deptaki są w OSM liniami, więc skrypt robi z nich pas o szerokości ~22 m (liczonej w metrach, nie
  w stopniach – na szerokości Polski stopień długości jest prawie 2× krótszy niż stopień szerokości).
  Strefy w prototypie: Słupsk – deptak Wojska Polskiego, Stary Rynek, Park Waldorffa, Park Kultury
  nad Słupią; Gdańsk – ul. Długa, Długi Targ, Długie Pobrzeże; Warszawa – Rynek Starego Miasta,
  Plac Zamkowy, Ogród Saski.
