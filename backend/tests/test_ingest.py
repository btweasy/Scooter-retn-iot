import json
import time
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import func, select

from app import mqtt_client, worker
from app.db import SessionLocal
from app.metrics import metrics
from app.models import Telemetry, utcnow
from app.schemas import TelemetryIn
from app.ingest import ingest_batch
from conftest import NO_PARKING, send, wait_for


def telemetry_count(sid="T-001"):
    with SessionLocal() as db:
        return db.scalar(select(func.count()).select_from(Telemetry).where(Telemetry.scooter_id == sid))


def alerts(client, kind):
    return [a for a in client.get("/api/alerts", params={"limit": 100}).json() if a["kind"] == kind]


# --- duplikaty, kolejność, straty ---

def test_duplicate_qos1_message_dropped(client):
    t0 = time.time()
    assert send(client, sent_at=t0, seq=1).json()["accepted"] is True
    r = send(client, sent_at=t0, seq=1).json()
    assert r["accepted"] is False and r["duplicate"] is True
    assert telemetry_count() == 1 and metrics.duplicates == 1


def test_late_message_does_not_overwrite_state(client):
    t0 = time.time()
    send(client, lat=54.49, sent_at=t0 + 2, seq=2)
    send(client, lat=54.48, sent_at=t0 + 1, seq=1)   # spóźniona, starsza
    assert client.get("/api/scooters").json()[0]["lat"] == 54.49


def test_seq_gap_counted_as_loss_but_restart_is_not(client):
    t0 = time.time()
    send(client, sent_at=t0, seq=1)
    send(client, sent_at=t0 + 1, seq=5)    # zginęły 2, 3, 4
    send(client, sent_at=t0 + 2, seq=1)    # restart urządzenia: nowszy czas, numeracja od nowa
    assert metrics.lost == 3 and telemetry_count() == 3


def test_seq_only_duplicate_without_sent_at(client):
    send(client, seq=7)
    assert send(client, seq=7).json()["duplicate"] is True


def test_batch_with_new_scooter_twice(client):
    """Dwie wiadomości nowego pojazdu w jednej partii nie mogą dać konfliktu klucza głównego."""
    t0 = time.time()
    items = [TelemetryIn(scooter_id="N-1", city="Słupsk", lat=54.48, lon=17.03, battery=80, speed=0,
                         sent_at=t0 + i, seq=i + 1) for i in range(2)]
    with SessionLocal() as db:
        results = ingest_batch(db, items)
    assert [r.status for r in results] == ["accepted", "accepted"] and telemetry_count("N-1") == 2


# --- alerty: histereza i jazda bez wypożyczenia ---

def test_low_battery_hysteresis(client):
    for b in (10, 16, 10):          # wahania wokół progu: jeden alert
        send(client, battery=b)
    assert len(alerts(client, "low_battery")) == 1
    send(client, battery=25)        # naładowana powyżej LOW_BATTERY_CLEAR
    send(client, battery=10)
    assert len(alerts(client, "low_battery")) == 2


def test_geofence_gps_jitter_while_parked(client):
    send(client, **NO_PARKING)            # w strefie
    send(client, lat=54.4700, lon=17.0285)            # "wyskok" GPS poza strefę, nadal zablokowana
    send(client, **NO_PARKING)
    assert len(alerts(client, "geofence")) == 1


def test_unauthorized_ride_alert_once(client):
    send(client)
    send(client, locked=False, speed=10)
    send(client, locked=False, speed=10)
    assert len(alerts(client, "unauthorized")) == 1
    assert client.get("/api/scooters").json()[0]["status"] == "in_ride"


def test_no_unauthorized_alert_right_after_rental_end(client):
    send(client)
    rid = client.post("/api/rentals/start", json={"scooter_id": "T-001"}).json()["id"]
    send(client, locked=False, speed=10)
    client.post(f"/api/rentals/{rid}/end")
    send(client, locked=False, speed=0)   # komenda lock jeszcze nie dotarła
    assert alerts(client, "unauthorized") == []


# --- ścieżka MQTT ---

def mqtt_message(payload, topic=None):
    """Udaje wiadomość paho-mqtt (ma tylko pola topic i payload)."""
    if isinstance(payload, dict):
        topic = topic or f"scooters/{payload['scooter_id']}/telemetry"
        payload = json.dumps(payload).encode()
    return SimpleNamespace(topic=topic or "scooters/T-001/telemetry", payload=payload)


def receive_over_mqtt(message, monkeypatch):
    """Przepuszcza wiadomość przez handler MQTT podłączony do kolejki zapisu – tak jak w działającej aplikacji."""
    monkeypatch.setattr(mqtt_client, "_on_telemetry", worker.submit)
    mqtt_client._handle_message(None, None, message)


def test_mqtt_message_goes_through_queue_to_db(client, monkeypatch):
    body = {"scooter_id": "M-1", "city": "Gdańsk", "lat": 54.35, "lon": 18.6, "battery": 70, "speed": 0,
            "sent_at": time.time(), "seq": 1}
    receive_over_mqtt(mqtt_message(body), monkeypatch)
    assert wait_for(lambda: telemetry_count("M-1") == 1)


def test_mqtt_rejects_invalid_and_spoofed_messages(client, monkeypatch):
    receive_over_mqtt(mqtt_message(b"{not json"), monkeypatch)
    receive_over_mqtt(mqtt_message({"scooter_id": "M-2", "city": "x", "lat": 0, "lon": 0,
                                    "battery": 500, "speed": 0}), monkeypatch)
    # scooter_id niezgodny z tematem (pojazd podszywa się pod inny)
    receive_over_mqtt(mqtt_message({"scooter_id": "M-3", "city": "x", "lat": 0, "lon": 0,
                                    "battery": 50, "speed": 0}, topic="scooters/M-9/telemetry"), monkeypatch)
    worker.flush()
    time.sleep(0.3)
    assert worker.queue_size() == 0 and client.get("/api/scooters").json() == []


# --- retencja ---

def test_retention_keeps_points_of_open_rentals(client):
    send(client, sid="A")
    send(client, sid="B")
    client.post("/api/rentals/start", json={"scooter_id": "B"})
    old = utcnow() - timedelta(days=10)
    with SessionLocal() as db:
        for sid in ("A", "B"):
            db.add(Telemetry(scooter_id=sid, ts=old, lat=54.48, lon=17.03, battery=80, speed=0))
        db.commit()
    assert worker.purge_old_telemetry() == 1
    assert telemetry_count("A") == 1 and telemetry_count("B") == 2
