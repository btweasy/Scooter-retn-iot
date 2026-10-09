# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Engineering thesis (Uniwersytet Pomorski w Słupsku): an IoT platform for an e-scooter fleet. It covers real-time telemetry over MQTT, alerts, reservations, and rentals with pause, receipt and no-parking-zone rules modelled on Bolt/Lime (see `docs/porownanie-z-rynkiem.md`). A further goal is to measure latency, throughput and message loss for QoS 0 vs 1. User-facing text, README, docs, code comments, and API error messages are in **Polish**, so keep new strings and comments in Polish. The author must explain the code at the thesis defense, so prefer plain, well-named functions over clever one-liners. `docs/przewodnik-po-kodzie.md` is the defense guide: keep it in sync when modules change. Open work items are in `TODO.md`.

## Commands

Full stack (Mosquitto with auth + Postgres + backend + emulator); secrets come from `.env` (see `.env.example`):
```bash
docker compose up --build        # client app http://localhost:8000, operator panel /panel, API docs /docs, MQTT localhost:1883
docker compose down -v           # needed after password changes or for a pre-migration DB: wipes demo data
```

Local, without Docker or a broker (SQLite `backend/scooters.db`, HTTP transport); `launch.txt` has the PowerShell variant:
```bash
cd backend && pip install -r requirements-dev.txt
MQTT_ENABLED=0 uvicorn app.main:app --port 8000
python ../emulator/emulator.py --transport http     # second terminal
```
On Windows, use `127.0.0.1` rather than `localhost` for HTTP clients. `localhost` costs ~2 s per request (IPv6 attempt first), which shows up as fake latency in `/api/metrics`.

Tests (run from `backend/`):
```bash
pytest
pytest tests/test_rentals.py::test_pause_is_cheaper_and_locks_scooter
TEST_DATABASE_URL=postgresql+psycopg2://... pytest   # against Postgres instead of temp SQLite
```
`tests/conftest.py` sets env vars **before** importing the app, because config is read at import time. It sets `AUTO_MIGRATE=0`, so tests build the schema with `create_all`. `test_migrations.py` checks separately that `alembic upgrade head` matches the models. Helpers: `send(client, ...)` posts telemetry (extra kwargs like `sent_at`, `seq` pass through), `start_rental(client, sid, user)`, `wait_for(cond)`, `NO_PARKING`/`FREE_SPOT` coordinates. To simulate elapsed time, shift timestamps in the DB or the watchdog's `locked_since` back; don't move "now" forward, because that makes scooters look offline.

Schema changes: edit `app/models.py`, then run `alembic revision --autogenerate -m "..."` from `backend/`. New NOT NULL columns need a `server_default` in the migration for existing rows (see `0002`). The app runs `alembic upgrade head` on startup (`AUTO_MIGRATE=1`).

The frontend JS has no build step; check it with `node --check` on `backend/web/client.js`, `panel.js` and `sw.js`. There is no linter configured.

## Architecture

Layers in `backend/app/`:
- **HTTP layer.** `routers/` (`telemetry`, `rentals`, `fleet`, `live`) holds thin endpoints. `main.py` wires them up and maps `RentalError(http_status, code, message)` to `{"detail", "code"}` JSON. Clients branch on `code`: the panel and emulator handle `no_parking_zone` specially.
- **Logic layer, no HTTP knowledge.**
  - `rentals.py`: the reservation and rental state machine (reservation `active→used|cancelled|expired`; rental `active⇄paused→finished`), pricing (`calculate_receipt`, every started minute) and the zone rule.
  - `ingest.py`: one telemetry message, and batches.
  - `alerts.py`: transition-only alerts using flag columns on `Scooter`.
  - `status.py`: derived scooter status.
  - `rental_watchdog.py`: expires reservations and auto-closes orphaned rentals.
- **Vehicle communication.** `mqtt_client.py` receives telemetry and hands it to a callback, which `main` sets to `worker.submit` (no import cycle). `commands.py` sends over MQTT, or queues commands in memory for the HTTP response when MQTT is disabled.
- **Data.** `models.py`, `db.py`, `migrations/`.

Key cross-file points:
- **Telemetry path:** MQTT → `worker` queue → batch → `ingest.ingest_batch` (one transaction; falls back to per-message on error; metrics recorded after commit). `ingest_message` does not commit. Duplicates and late messages are dropped by `sent_at`, and `seq` gaps count as loss.
- **Scooter status is never stored.** `status.scooter_status(scooter, now, rental, reservation)` is the single source of truth: offline > paused > in_ride (open rental *or* unlocked) > reserved > low_battery > available. `/api/stats` counts statuses in Python from the same function.
- **Rental concurrency:** state changes go through `rentals.change_status`, a conditional `UPDATE ... WHERE status=expected AND ended_at IS NULL`. Partial unique indexes allow one open rental and one active reservation per scooter (IntegrityError → 409 `conflict`). The tariff is copied into the rental at start. Auto-close (watchdog, `automatic=True`) is free when distance is 0, and ignores paused rentals.
- **Emulator** (`emulator/emulator.py`): background "users" rent through the real API (`sim-<scooter_id>`). The scooter waits for backend unlock/lock commands and never locks itself. Rides end only via `/end`; on `no_parking_zone` it rides on. On startup it closes its own leftover `sim-` rentals. If unlock doesn't arrive within 20 s it gives up.
- **Backend MQTT session is persistent** (`clean_session=False`, fixed `MQTT_CLIENT_ID`), so run one backend per client id. Broker ACL is in `mosquitto/acl`, and the password file is generated at container start from `.env`.
- **Auth:** when `API_TOKEN` is set, state-changing endpoints (`deps.requires_token`) require `Authorization: Bearer`. The panel asks for the token on 401.
- **Two web frontends** in `backend/web/` (no build step, Leaflet vendored). Both subscribe to SSE `/api/stream`, which serves `live.build_snapshot` (scooters, stats, alerts, open rentals, history, reservations) cached once per interval for all clients.
  - **Client PWA** at `/`: `index.html` + `client.css` + `client.js`, plus `manifest.webmanifest` and `sw.js`, both served from the root by `main.py` so the service worker scope is `/`. Startup runs splash → onboarding (first run only) → name-only "login" (`user_id` derived from the name, stored in localStorage) → map with a bottom sheet. `currentView()` picks the sheet view from state: browse / scooter / reserved / ride / summary. The client shows only available scooters plus the user's own, filtering the snapshot by `user_id`. Buttons use `data-ui` dispatched through the `UI` map. Tariff comes from `GET /api/tariff`. The service worker is network-first for app files and never touches `/api/`; bump `CACHE` in `sw.js` when the shell file list changes.
  - **Operator panel** at `/panel`: `panel.html` + `panel.css` + `panel.js`. Actions use `data-action`/`data-id` through the `ACTIONS` map. "Działasz jako klient" sets the `user_id` for demoing reservations.
  - Icons are drawn in `web/icons/icon.svg`. The PNGs are generated by `py tools/make_icons.py` (numpy only, no Pillow), which keeps the drawing coordinates in sync with the SVG by hand.
- **Geofences** (no-parking zones) are real places: squares, a pedestrian street, parks and a waterfront in each city. Their outlines are generated from OpenStreetMap by `py tools/fetch_zones.py > backend/app/zones_data.py`. The script uses Nominatim; it buffers streets into strips in metres and simplifies outlines. The output is committed, so the app needs no network, and is exposed as `geo.ZONES`. Polygons are `[lat, lon]` (Shapely `Point(lat, lon)`, so x=lat: keep this order consistent; GeoJSON from OSM is lon,lat and the script swaps it). Tests use `conftest.NO_PARKING`, a point inside the Słupsk park zone, so don't hardcode zone coordinates in tests. Distance comes from `geo.track_length_km` (drops jitter below `GPS_MIN_STEP_M` and jumps above `GPS_MAX_SPEED_KMH`).
- Config is all env vars in `app/config.py`. Timestamps are naive UTC (`models.utcnow`), serialised with `Z`. `Telemetry.ts` is server receive time and `sent_at` is device time.

Planned (per README, not implemented): native packaging / push notifications for the PWA, speed-limit zones (`limit` command), parking photo, PostGIS, InfluxDB + Grafana, Raspberry Pi GPS node, JWT user auth, MQTT TLS, performance studies, Docker Swarm.
