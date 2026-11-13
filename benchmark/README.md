# Badania wydajności: opóźnienie, przepustowość, straty (QoS 0 vs QoS 1)

Katalog jest **niezależny od kodu aplikacji**. Uruchamia niezmieniony backend (`../backend/app`) jako osobny
proces i mierzy go z zewnątrz, jak urządzenia w terenie. Nic w `backend/` ani w `emulator/` nie jest zmieniane.

## Stanowisko pomiarowe

```
load_generator.py ──► netem_proxy.py ──► Mosquitto (Docker) ──► backend (uvicorn) ──► SQLite
 N „urządzeń”         opóźnienie,         persistence,     │    niezmieniony kod
 sent_at + seq        zerwania łącza      max_queued 100k  └──► broker_observer.py
                                                                (co dotarło do brokera)
```

| Plik | Rola |
|---|---|
| `run_benchmark.py` | scenariusze, uruchamianie brokera i backendu, zbieranie wyników |
| `load_generator.py` | flota wysyłająca telemetrię w stałym tempie (MQTT QoS 0/1 albo HTTP), liczy wysłane wiadomości |
| `netem_proxy.py` | pośrednik TCP: opóźnienie ± jitter, okresowe zerwania łącza |
| `broker_observer.py` | niezależny subskrybent na brokerze, który pokazuje, czy wiadomość zginęła w sieci, czy w backendzie |
| `plot.py` | wykresy PNG z wyników (można uruchomić osobno) |
| `compare.py` | porównanie dwóch serii (np. przed i po zmianie w backendzie): `porownanie_*.png`, `porownanie.csv` |
| `mosquitto.conf` | broker jak w `../mosquitto/`, ale bez haseł (nie wpływają na pomiar) |

Każdy przebieg dostaje **świeżą bazę SQLite i świeży kontener brokera**, więc przebiegi nie wpływają na siebie.

## Uruchomienie

Wymagania: Docker Desktop (dla Mosquitto), zależności backendu i matplotlib.

```powershell
cd benchmark
py -m pip install -r ..\backend\requirements.txt -r requirements.txt
py run_benchmark.py --quick        # ~8 min, krótkie przebiegi: sprawdzenie, że wszystko działa
py run_benchmark.py                # ~25 min, pełny zestaw (po 30 s wysyłania na przebieg)
py run_benchmark.py --only qos     # wybrana grupa: qos | throughput | batching | http
py plot.py results\2026-11-12_1530 # same wykresy od nowa, bez powtarzania pomiarów
py compare.py results\przed_poprawkami results\po_poprawkach   # porównanie dwóch serii
```

Wyniki w `results/<data_godzina>/`:
- `summary.csv`: jeden wiersz na przebieg (tabela do pracy),
- `runs.json`: to samo oraz momenty awarii,
- `raw/<przebieg>.csv`: każda zapisana wiadomość (`sent_at`, czas zapisu, opóźnienie),
- `*.png`: wykresy, `logs/`: logi backendu, `db/`: bazy z przebiegów (w `.gitignore`).

## Jak liczone są wskaźniki

- **Opóźnienie end-to-end** = `telemetry.ts` (czas przetworzenia przez backend) − `sent_at` (czas wysłania
  z urządzenia). Wszystko działa na jednym komputerze, więc zegary są zgodne i nie trzeba NTP.
- **Straty** = wiadomości wygenerowane przez urządzenia − unikalne pary `(scooter_id, seq)` zapisane w bazie.
  Liczone dokładnie, a nie szacowane z luk `seq` jak w `/api/metrics`.
- **Miejsce utraty** (obserwator na brokerze): *w sieci* oznacza, że urządzenie wysłało, a broker nie dostał;
  *w backendzie* oznacza, że broker dostał, a w bazie wiadomości nie ma (awaria procesu albo odrzucenie
  wiadomości przychodzącej w zmienionej kolejności jako „spóźnionej”).
- **Przepustowość** = zapisane wiadomości / czas od pierwszego do ostatniego zapisu.
- **Duplikaty** = odrzucone przez backend jako duplikat lub spóźnione (`/api/metrics`). Po awarii backendu
  licznik zaczyna się od zera, bo jest w pamięci procesu.

## Scenariusze

| Grupa | Przebiegi | Pytanie badawcze |
|---|---|---|
| `qos` | 5 warunków × QoS 0/1, 200 pojazdów co 1 s | Ile wiadomości ginie i jakim kosztem opóźnienia QoS 1 je ratuje? |
| `throughput` | 200…1500 wiad./s, QoS 1 | Gdzie jest granica wydajności i co się wtedy dzieje z opóźnieniem? |
| `batching` | `INGEST_FLUSH_S` 20…500 ms przy 200 wiad./s | Jak czas zbierania partii wpływa na opóźnienie? |
| `http` | HTTP `POST /api/telemetry/batch`, 200 wiad./s | Porównanie z zapasową drogą HTTP |

Warunki sieci w grupie `qos`:
- **Sieć idealna**: pośrednik bez zakłóceń (punkt odniesienia).
- **Opóźnienie 80 ± 40 ms**: typowe łącze komórkowe.
- **Zerwania łącza**: co 10 s połączenie urządzeń zrywa się na 3 s (np. tunel, brak zasięgu).
  Utraty pojedynczych pakietów nie symulujemy, bo TCP sam je powtarza i w MQTT objawia się to tylko jako
  opóźnienie. Wiadomości giną dopiero przy zerwaniu połączenia.
- **Restart brokera**: `docker restart` w 40% pomiaru.
- **Awaria backendu**: twarde zabicie procesu w 40% pomiaru, ponowne uruchomienie po 5 s
  (ten sam `MQTT_CLIENT_ID`, czyli trwała sesja u brokera).

## Wykresy

1. `1_straty_qos.png`: straty w każdym warunku, QoS 0 vs QoS 1.
2. `2_opoznienia_qos.png`: mediana i p95 opóźnienia (skala logarytmiczna).
3. `3_rozklad_opoznien.png`: dystrybuanta opóźnień, sieć idealna vs z opóźnieniem.
4. `4–6_przebieg_*.png`: wiadomości zapisane w każdej sekundzie podczas zerwań, restartu brokera i awarii
   backendu. Szare pasy oznaczają awarię, kropkowana linia tempo wysyłania. Przy QoS 1 widać „nadrabianie” po awarii.
7. `7_przepustowosc.png`: zapisane vs wysyłane wiadomości/s oraz opóźnienie przy rosnącym obciążeniu.
8. `8_zapis_partiami.png`: opóźnienie i przepustowość a `INGEST_FLUSH_S`.
9. `9_mqtt_vs_http.png`: dystrybuanta opóźnień MQTT QoS 0/1 vs HTTP.

## Ograniczenia (warto opisać w pracy)

- Generator, broker i backend działają na jednym komputerze i konkurują o procesor. Przy dużym obciążeniu
  część opóźnienia może wynikać z samego generatora.
- Baza SQLite. Na PostgreSQL granica przepustowości będzie inna (backend obsługuje `DATABASE_URL`,
  więc to naturalne rozszerzenie badań).
- Urządzenia dzielą 20 połączeń MQTT (jak bramki), a nie mają po jednym połączeniu na pojazd.
- Opóźnienie obejmuje oczekiwanie w kolejce zapisu backendu (do `INGEST_FLUSH_S`, domyślnie 200 ms).
  To świadomy kompromis, mierzony w grupie `batching`.

## Przed i po poprawkach (2026-11-12)

Badanie wykryło dwie przyczyny strat przy QoS 1. Obie leżały w backendzie, a nie w sieci:

1. **Zmieniona kolejność po zerwaniu łącza.** Zaległe wiadomości QoS 1 przychodziły przemieszane z nowymi,
   a backend odrzucał każdą starszą od ostatniej jako „spóźnioną”: obserwator na brokerze widział ich tyle,
   ile zginęło. Poprawka: `ingest.classify` zapisuje spóźnione wiadomości do historii (bez zmiany stanu pojazdu),
   a trasa jest sortowana wg czasu urządzenia.
2. **Potwierdzenie przed zapisem.** Backend wysyłał PUBACK, gdy wiadomość trafiła do kolejki w pamięci,
   więc przy awarii procesu ginęły wiadomości z ostatnich ~0,4 s. Poprawka: `manual_ack`, czyli PUBACK dopiero po
   zapisie do bazy, oraz `max_inflight_messages 1000` w brokerze.

Wyniki: `results/przed_poprawkami/` i `results/po_poprawkach/` (z wykresami `porownanie_qos1.png`
i `porownanie_przepustowosc.png`).
