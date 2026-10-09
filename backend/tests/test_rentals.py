"""Wypożyczenia: rezerwacja, start, pauza, koniec, paragon, strefy, historia, sprzątanie osieroconych."""
import time
from datetime import timedelta

from sqlalchemy import update

from app import config
from app.db import SessionLocal
from app.models import Rental, Reservation, Scooter, utcnow
from app.rental_watchdog import RentalWatchdog
from conftest import NO_PARKING, send, start_rental


def shift_rental_back(rental_id, **delta):
    """Przesuwa start (i ewentualną pauzę) w przeszłość – symulacja upływu czasu bez czekania."""
    with SessionLocal() as db:
        rental = db.get(Rental, rental_id)
        rental.started_at -= timedelta(**delta)
        if rental.paused_at is not None:
            rental.paused_at -= timedelta(**delta)
        db.commit()


def get(client, rental_id):
    return client.get(f"/api/rentals/{rental_id}").json()


# ---------------------------------------------------------------- start i koniec

def test_rental_lifecycle_with_route_and_receipt(client):
    rental_id = start_rental(client)
    assert client.post("/api/rentals/start", json={"scooter_id": "T-001"}).status_code == 409
    t0 = time.time()
    send(client, lat=54.4800, locked=False, speed=15, sent_at=t0)
    send(client, lat=54.4850, locked=False, speed=15, sent_at=t0 + 60)   # ~556 m w minutę

    ended = client.post(f"/api/rentals/{rental_id}/end")
    assert ended.status_code == 200
    body = ended.json()
    assert body["status"] == "finished" and 0.5 < body["distance_km"] < 0.6
    assert body["receipt"] == {"unlock_fee": 3.0, "ride_minutes": 1, "ride_cost": 0.6,
                               "pause_minutes": 0, "pause_cost": 0.0, "total": 3.6}
    assert client.post(f"/api/rentals/{rental_id}/end").json()["code"] == "already_finished"
    assert client.get("/api/rentals", params={"status": "open"}).json() == []
    assert [r["id"] for r in client.get("/api/rentals", params={"status": "finished"}).json()] == [rental_id]


def test_every_started_minute_is_charged(client):
    rental_id = start_rental(client)
    shift_rental_back(rental_id, minutes=6, seconds=30)   # 7 rozpoczętych minut: 3,00 + 7 × 0,60
    assert client.post(f"/api/rentals/{rental_id}/end").json()["cost_pln"] == 7.2


def test_tariff_is_fixed_at_start(client, monkeypatch):
    rental_id = start_rental(client)
    monkeypatch.setattr(config, "PRICE_PER_MIN", config.PRICE_PER_MIN * 10)   # podwyżka w trakcie jazdy
    assert client.post(f"/api/rentals/{rental_id}/end").json()["cost_pln"] == 3.6


def test_live_details_with_track(client):
    rental_id = start_rental(client)
    t0 = time.time()
    for i in range(5):   # 5 punktów co 10 s, po ~100 m
        send(client, lat=54.4800 + 0.0009 * i, lon=17.0300, locked=False, speed=15, sent_at=t0 + 10 * i)
    live = get(client, rental_id)
    assert live["status"] == "active" and len(live["track"]) == 5 and live["track"][0] == [54.48, 17.03]
    assert 0.35 < live["distance_km"] < 0.45 and live["receipt"]["total"] == 3.6
    thin = client.get(f"/api/rentals/{rental_id}", params={"max_points": 2}).json()["track"]
    assert thin[0] == live["track"][0] and thin[-1] == live["track"][-1] and len(thin) <= 3

    client.post(f"/api/rentals/{rental_id}/end")
    send(client, lat=54.50, sent_at=t0 + 100)   # po zakończeniu – już nie należy do trasy
    finished = get(client, rental_id)
    assert finished["status"] == "finished" and len(finished["track"]) == 5
    assert client.get("/api/rentals/999").json()["code"] == "rental_not_found"


# ---------------------------------------------------------------- strefa zakazu parkowania

def test_cannot_end_in_no_parking_zone_unless_forced(client):
    rental_id = start_rental(client)
    send(client, **NO_PARKING, locked=False, speed=10)
    refused = client.post(f"/api/rentals/{rental_id}/end")
    assert refused.status_code == 409 and refused.json()["code"] == "no_parking_zone"
    assert "Park Kultury" in refused.json()["detail"]

    forced = client.post(f"/api/rentals/{rental_id}/end", json={"force": True})   # operator
    assert forced.status_code == 200 and forced.json()["status"] == "finished"


# ---------------------------------------------------------------- pauza

def test_pause_is_cheaper_and_locks_scooter(client):
    rental_id = start_rental(client)
    send(client)   # odbiera komendę unlock

    paused = client.post(f"/api/rentals/{rental_id}/pause").json()
    assert paused["status"] == "paused"
    assert send(client).json()["commands"] == ["lock"]
    assert client.get("/api/scooters").json()[0]["status"] == "paused"
    assert client.post(f"/api/rentals/{rental_id}/pause").json()["code"] == "not_active"

    shift_rental_back(rental_id, seconds=239)          # prawie 4 minuty pauzy => 4 rozpoczęte minuty
    resumed = client.post(f"/api/rentals/{rental_id}/resume").json()
    assert resumed["status"] == "active"
    assert send(client).json()["commands"] == ["unlock"]

    receipt = client.post(f"/api/rentals/{rental_id}/end").json()["receipt"]
    # 3,00 za odblokowanie + 1 min jazdy × 0,60 + 4 min pauzy × 0,30
    assert receipt["pause_minutes"] == 4 and receipt["ride_minutes"] == 1
    assert receipt["total"] == 4.8


def test_end_while_paused(client):
    rental_id = start_rental(client)
    client.post(f"/api/rentals/{rental_id}/pause")
    body = client.post(f"/api/rentals/{rental_id}/end").json()
    assert body["status"] == "finished" and body["paused_at"] is None
    assert client.post(f"/api/rentals/{rental_id}/resume").json()["code"] == "not_paused"


# ---------------------------------------------------------------- rezerwacja

def test_reservation_blocks_others_but_not_owner(client):
    send(client)
    reservation = client.post("/api/reservations", json={"scooter_id": "T-001", "user_id": "ania"})
    assert reservation.status_code == 201
    assert client.get("/api/scooters").json()[0]["status"] == "reserved"
    assert client.post("/api/reservations", json={"scooter_id": "T-001", "user_id": "bartek"}).status_code == 409

    stranger = client.post("/api/rentals/start", json={"scooter_id": "T-001", "user_id": "bartek"})
    assert stranger.status_code == 409 and stranger.json()["code"] == "reserved"
    assert client.post("/api/rentals/start", json={"scooter_id": "T-001", "user_id": "ania"}).status_code == 201
    with SessionLocal() as db:
        assert db.get(Reservation, reservation.json()["id"]).status == "used"


def test_reservation_expires(client):
    send(client)
    reservation_id = client.post("/api/reservations", json={"scooter_id": "T-001", "user_id": "ania"}).json()["id"]
    with SessionLocal() as db:
        db.execute(update(Reservation).values(expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    assert client.get("/api/scooters").json()[0]["status"] == "available"
    assert client.post("/api/rentals/start", json={"scooter_id": "T-001", "user_id": "bartek"}).status_code == 201
    with SessionLocal() as db:
        assert db.get(Reservation, reservation_id).status == "expired"


def test_cancel_reservation(client):
    send(client)
    reservation_id = client.post("/api/reservations", json={"scooter_id": "T-001"}).json()["id"]
    assert client.delete(f"/api/reservations/{reservation_id}").json()["status"] == "cancelled"
    assert client.delete(f"/api/reservations/{reservation_id}").status_code == 409
    assert client.get("/api/reservations").json() == []


# ---------------------------------------------------------------- osierocone wypożyczenia

def tick(watchdog):
    with SessionLocal() as db:
        return watchdog.tick(db)


def pretend_locked_longer(watchdog, seconds):
    """Udaje, że pojazdy stoją zablokowane o `seconds` dłużej (telemetria nadal przychodzi na bieżąco)."""
    for rental_id in watchdog.locked_since:
        watchdog.locked_since[rental_id] -= timedelta(seconds=seconds)


def test_orphan_rental_closed_free_after_timeout(client):
    """Unlock nie dotarł (np. restart emulatora/backendu): pojazd online i zablokowany => zamknięcie bez opłaty."""
    rental_id = start_rental(client)
    send(client)   # nadal zablokowana
    watchdog = RentalWatchdog()
    assert tick(watchdog) == []
    pretend_locked_longer(watchdog, config.RENTAL_LOCKED_TIMEOUT_S - 5)
    assert tick(watchdog) == []
    pretend_locked_longer(watchdog, 6)
    assert tick(watchdog) == [rental_id]

    body = get(client, rental_id)
    assert body["status"] == "finished" and body["cost_pln"] == 0 and body["closed_automatically"] is True
    alert = client.get("/api/alerts").json()[0]
    assert alert["kind"] == "rental_closed" and f"#{rental_id}" in alert["message"]
    assert client.post("/api/rentals/start", json={"scooter_id": "T-001"}).status_code == 201


def test_paused_riding_and_offline_rentals_are_left_alone(client):
    paused = start_rental(client, "P")
    client.post(f"/api/rentals/{paused}/pause")
    riding = start_rental(client, "A")
    send(client, sid="A", locked=False, speed=10)
    silent = start_rental(client, "B")
    with SessionLocal() as db:   # B przestała nadawać – może jechać bez zasięgu
        db.execute(update(Scooter).where(Scooter.id == "B")
                   .values(last_seen=utcnow() - timedelta(seconds=config.OFFLINE_AFTER_S + 5)))
        db.commit()

    watchdog = RentalWatchdog()
    tick(watchdog)
    pretend_locked_longer(watchdog, config.RENTAL_LOCKED_TIMEOUT_S + 5)
    assert tick(watchdog) == []
    for rental_id in (paused, riding, silent):
        assert get(client, rental_id)["status"] != "finished"


def test_unlock_resets_countdown(client):
    rental_id = start_rental(client)
    send(client)
    watchdog = RentalWatchdog()
    tick(watchdog)
    pretend_locked_longer(watchdog, config.RENTAL_LOCKED_TIMEOUT_S - 10)
    send(client, locked=False, speed=10)
    tick(watchdog)                       # odblokowana: licznik skasowany
    send(client, locked=True)
    tick(watchdog)                       # znów zablokowana – liczy od nowa
    assert rental_id in watchdog.locked_since and get(client, rental_id)["status"] == "active"
