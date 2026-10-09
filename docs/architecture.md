# Architektura systemu

## Komponenty

| Komponent | Technologia | Rola |
|---|---|---|
| Emulator / węzeł IoT | Python, paho-mqtt | generuje telemetrię GPS, baterii i prędkości; reaguje na komendy lock/unlock; jazdy tła jako wypożyczenia w API |
| Broker | Eclipse Mosquitto 2 | kanał publikuj/subskrybuj, QoS 1, uwierzytelnianie + ACL, trwałe sesje |
| Backend | FastAPI, SQLAlchemy, Alembic | przyjmowanie telemetrii, logika wypożyczeń, geostrefy, alerty, metryki, REST API, SSE |
| Baza danych | PostgreSQL (SQLite lokalnie i w testach) | stan pojazdów, historia telemetrii, wypożyczenia, alerty |
| Aplikacja klienta (`/`) | PWA: HTML/JS, Leaflet, manifest, service worker | wyszukiwanie, rezerwacja, jazda, paragon, historia – widok jednej osoby |
| Panel operatora (`/panel`) | HTML/JS, Leaflet (lokalnie) | mapa floty, statystyki, alerty, sterowanie wypożyczeniem |
| Środowisko | Docker Compose | uruchomienie całości jednym poleceniem |

## Przepływ danych

1. Pojazd publikuje `scooters/<id>/telemetry` co kilka sekund (QoS 1) z czasem wysłania `sent_at` i numerem `seq`.
2. Wątek sieciowy MQTT tylko waliduje wiadomość (Pydantic, zgodność `scooter_id` z tematem) i wkłada ją do kolejki.
   Wątek zapisu pobiera partie (do 500 wiadomości / 0,2 s) i zapisuje każdą w jednej transakcji (`ingest.ingest_batch`).
   Przy błędzie partia jest ponawiana pojedynczo.
3. `ingest.ingest_message` odrzuca duplikaty i wiadomości spóźnione (`sent_at` ≤ ostatniego), liczy luki w `seq`
   jako straty, aktualizuje tabelę `scooters`, dopisuje wiersz do `telemetry` i sprawdza alerty (`alerts.py`).
4. Panel otrzymuje zrzut stanu przez SSE (`/api/stream`) co 2 s; zrzut jest wspólny dla wszystkich otwartych paneli.
5. Wypożyczenia (`rentals.py`): rezerwacja → start (komenda `unlock` na `scooters/<id>/command`) ⇄ pauza
   (`lock`/`unlock`) → koniec (odmowa w strefie zakazu, dystans z telemetrii z filtrem szumu GPS, paragon,
   komenda `lock`). Bez MQTT komendy czekają w kolejce i są zwracane w odpowiedzi na telemetrię HTTP pojazdu.
6. Co 5 s porządki (`rental_watchdog.py`): wygasłe rezerwacje, osierocone wypożyczenia.
   Co 10 min retencja usuwa telemetrię starszą niż `TELEMETRY_RETENTION_H` (poza pojazdami w trakcie jazdy).

## Warstwy backendu

| Warstwa | Pliki | Odpowiada za |
|---|---|---|
| HTTP | `routers/*.py`, `main.py` | wejście/wyjście, walidacja, kody odpowiedzi |
| Logika | `rentals.py`, `ingest.py`, `alerts.py`, `status.py`, `rental_watchdog.py` | reguły biznesowe – bez wiedzy o HTTP |
| Komunikacja z pojazdami | `mqtt_client.py`, `commands.py` | MQTT, kolejka komend dla HTTP |
| Dane | `models.py`, `db.py`, `migrations/` | tabele, sesje, migracje |

Błąd reguły biznesowej to `RentalError(http_status, code, message)`; `main.py` zamienia go na
odpowiedź `{"detail": ..., "code": ...}`.

## Model danych

- `scooters(id, city, lat, lon, battery, speed, locked, last_seen, last_sent_at, last_seq, in_zone, low_battery_flag, unauthorized_flag)`
- `telemetry(id, scooter_id → scooters, ts, sent_at, seq, lat, lon, battery, speed)` – indeks `(scooter_id, ts)`
- `reservations(id, scooter_id → scooters, user_id, status, created_at, expires_at)` – status
  `active | used | cancelled | expired`; częściowy unikalny indeks: jedna aktywna rezerwacja na pojazd
- `rentals(id, scooter_id → scooters, user_id, status, started_at, ended_at, paused_at, paused_seconds,
  unlock_fee, price_per_min, pause_price_per_min, distance_km, ride_minutes, pause_minutes, cost_pln,
  closed_automatically)` – status `active | paused | finished`, kwoty `NUMERIC(10,2)`; cennik kopiowany
  przy starcie; częściowy unikalny indeks: jedno niezakończone wypożyczenie na pojazd
- `alerts(id, scooter_id → scooters, kind, message, ts, acknowledged_at)`

Status pojazdu wyliczany jest dynamicznie (`status.py`), pierwsza pasująca reguła: `offline` (brak telemetrii
> 30 s), `paused`, `in_ride` (wypożyczenie albo odblokowany), `reserved`, `low_battery` (< 15%), `available`.
Zmiany schematu: `alembic revision --autogenerate`.

## Alerty

| Rodzaj | Kiedy | Ponownie możliwy |
|---|---|---|
| `geofence` | pojazd zablokowany w strefie zakazu | po odblokowaniu (drgania GPS na granicy nie dublują alertu) |
| `low_battery` | bateria < `LOW_BATTERY` | po naładowaniu do `LOW_BATTERY_CLEAR` (histereza) |
| `unauthorized` | odblokowany bez otwartego wypożyczenia (poza oknem `UNAUTHORIZED_GRACE_S` po zakończeniu) | po zablokowaniu |
| `rental_closed` | wypożyczenie zamknięte automatycznie: pojazd online i zablokowany dłużej niż `RENTAL_LOCKED_TIMEOUT_S` (zaginiona komenda unlock, restart urządzenia) | – |

Osierocone wypożyczenia (`app/rental_watchdog.py`, wątek w `worker`): bez sprzątania pojazd pozostałby
„wypożyczony” na zawsze. Dotyczy tylko jazd w statusie `active` (w pauzie pojazd jest zablokowany celowo).
Zamknięcie automatyczne jest bezpłatne, jeśli pojazd nie przejechał żadnego dystansu. Emulator
dodatkowo przy starcie zamyka własne wypożyczenia tła (`sim-<id>`) z poprzedniego uruchomienia i rezygnuje
z wypożyczenia, jeśli unlock nie dotrze w 20 s.

## Decyzje projektowe

- **MQTT zamiast HTTP dla telemetrii** – lekki protokół dla urządzeń, QoS, dwukierunkowa komunikacja (komendy).
- **Trwała sesja backendu** (`clean_session=False`, stały `MQTT_CLIENT_ID`, persistence brokera) – wiadomości QoS 1
  z czasu przerwy w połączeniu nie giną. Dlatego backend może działać w jednym egzemplarzu na `client_id`.
- **Kolejka + zapis partiami** – wątek sieciowy MQTT nie czeka na bazę; jedna transakcja na partię zamiast na wiadomość.
- **Emulator jako podstawa, sprzęt jako dodatek** – badania na setkach pojazdów bez kosztów sprzętu;
  Raspberry Pi pokazuje wdrożenie na prawdziwym urządzeniu.
- **Alerty tylko przy zmianie stanu, z histerezą** – brak zalewu powiadomień przy każdym pomiarze.
- **Geostrefy: Shapely, odległości: geopy** – gotowe biblioteki zamiast własnych algorytmów; docelowo PostGIS.
- **Strefy w kształcie prawdziwych miejsc** – obrysy placów, deptaków, parków i nabrzeży z OpenStreetMap
  (`tools/fetch_zones.py` → `app/zones_data.py`), pobrane raz i zapisane w kodzie: aplikacja nie zależy
  od zewnętrznego serwisu. Ulice (linie w OSM) są pogrubiane do pasa o szerokości w metrach.
- **Status pojazdu liczony, a nie zapisywany** – nie może się rozjechać z rzeczywistością; statystyki liczone
  w Pythonie z tej samej funkcji (tabela `scooters` ma jeden wiersz na pojazd, więc to tanie).
- **Warunkowe zmiany statusu wypożyczenia** (`UPDATE ... WHERE status = ...`) i unikalne indeksy częściowe
  – poprawność przy równoczesnych żądaniach bez blokad w kodzie.
- **Zasady wypożyczeń wzorowane na rynku** – zob. [porownanie-z-rynkiem.md](porownanie-z-rynkiem.md).

## Ograniczenia prototypu

Token API jest wspólny dla operatora (brak kont użytkowników i JWT – wypożyczenie może zakończyć każdy posiadacz
tokenu), brak TLS dla MQTT, brak płatności, strefy zakazu zapisane w kodzie (obrysy z OSM, bez edycji w panelu), kolejka komend HTTP i metryki
w pamięci jednego procesu, wiadomości w kolejce zapisu giną przy awarii procesu (potwierdzenie QoS 1 następuje
przy przyjęciu do kolejki), telemetria bez agregacji (planowane InfluxDB).

## Plan badań

Pomiar opóźnienia end-to-end (`sent_at` → zapis, `/api/metrics`), przepustowości (wiadomości/s) i utraty wiadomości
(luki w `seq`) dla QoS 0 i 1 przy rosnącej liczbie pojazdów (100, 1000, 10 000), z użyciem emulatora.
