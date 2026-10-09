"""Podstawowe API: telemetria, status pojazdów, statystyki, alerty, autoryzacja."""
import time
from datetime import timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app import config
from app.db import SessionLocal
from app.models import Rental, Scooter, utcnow
from conftest import NO_PARKING, send, start_rental


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["db"] is True


def test_telemetry_creates_scooter(client):
    assert send(client).status_code == 202
    data = client.get("/api/scooters").json()
    assert len(data) == 1 and data[0]["id"] == "T-001" and data[0]["status"] == "available"
    assert data[0]["range_km"] == pytest.approx(0.8 * config.FULL_RANGE_KM)


def test_invalid_telemetry_rejected(client):
    assert send(client, battery=150).status_code == 422


def test_low_battery_status_alert_and_no_rental(client):
    send(client, battery=9)
    assert client.get("/api/scooters").json()[0]["status"] == "low_battery"
    assert [a["kind"] for a in client.get("/api/alerts").json()] == ["low_battery"]
    assert client.post("/api/rentals/start", json={"scooter_id": "T-001"}).status_code == 409


def test_geofence_alert_only_for_parked_and_only_once(client):
    send(client, **NO_PARKING, locked=False, speed=15)   # przejazd przez strefę: bez alertu strefy
    assert [a for a in client.get("/api/alerts").json() if a["kind"] == "geofence"] == []
    send(client, **NO_PARKING, locked=True)               # parkowanie w strefie: alert
    send(client, **NO_PARKING, locked=True)               # kolejny pomiar: bez duplikatu
    assert len([a for a in client.get("/api/alerts").json() if a["kind"] == "geofence"]) == 1


def test_rent_unknown_scooter(client):
    response = client.post("/api/rentals/start", json={"scooter_id": "NOPE"})
    assert response.status_code == 404 and response.json()["code"] == "scooter_not_found"


def test_rentals_limit_validated(client):
    assert client.get("/api/rentals", params={"limit": 0}).status_code == 422
    assert client.get("/api/rentals", params={"limit": 10_000}).status_code == 422
    assert client.get("/api/rentals", params={"status": "zle"}).status_code == 422


def test_only_one_open_rental_per_scooter_in_db(client):
    """Indeks uq_rentals_open_scooter chroni przed wyścigiem dwóch równoczesnych startów."""
    send(client)
    tariff = {"unlock_fee": 3, "price_per_min": 0.6, "pause_price_per_min": 0.3}
    with SessionLocal() as db:
        db.add(Rental(scooter_id="T-001", user_id="a", started_at=utcnow(), **tariff))
        db.commit()
        db.add(Rental(scooter_id="T-001", user_id="b", started_at=utcnow(), **tariff))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(Rental(scooter_id="T-001", user_id="c", started_at=utcnow(), ended_at=utcnow(), **tariff))
        db.commit()   # zakończone wypożyczenia nie są ograniczone


def test_offline_after_silence(client):
    send(client)
    with SessionLocal() as db:
        db.execute(update(Scooter).values(last_seen=utcnow() - timedelta(seconds=config.OFFLINE_AFTER_S + 5)))
        db.commit()
    assert client.get("/api/scooters").json()[0]["status"] == "offline"
    assert client.get("/api/stats").json()["by_status"]["offline"] == 1
    assert client.post("/api/rentals/start", json={"scooter_id": "T-001"}).status_code == 409


def test_rented_scooter_is_in_ride_even_before_unlock_arrives(client):
    """Wypożyczony pojazd nie może wyglądać na wolny, zanim dotrze do niego komenda unlock."""
    start_rental(client)
    assert client.get("/api/scooters").json()[0]["status"] == "in_ride"


def test_stats(client):
    send(client, sid="A")
    send(client, sid="B", locked=False, speed=10)
    send(client, sid="C", battery=5, city="Gdańsk")
    stats = client.get("/api/stats").json()
    assert stats["total"] == 3
    assert stats["by_status"] == {"available": 1, "reserved": 0, "in_ride": 1, "paused": 0,
                                  "low_battery": 1, "offline": 0}
    assert stats["by_city"] == {"Słupsk": 2, "Gdańsk": 1}
    assert stats["avg_battery"] == pytest.approx(55.0)


def test_snapshot_and_metrics(client):
    send(client, sent_at=time.time() - 0.05, seq=1)
    snapshot = client.get("/api/snapshot").json()
    assert set(snapshot) == {"scooters", "stats", "alerts", "rentals", "history", "reservations"}
    assert len(snapshot["scooters"]) == 1
    metrics = client.get("/api/metrics").json()
    assert metrics["accepted"] == 1 and metrics["latency"]["samples"] == 1 and metrics["latency"]["avg_ms"] >= 50


def test_client_app_panel_and_pwa_files(client):
    assert 'rel="manifest"' in client.get("/").text
    assert "Panel operatora" in client.get("/panel").text
    manifest = client.get("/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    assert manifest.json()["display"] == "standalone"
    for icon in manifest.json()["icons"]:
        assert client.get(icon["src"]).status_code == 200
    assert "javascript" in client.get("/sw.js").headers["content-type"]


def test_web_files_are_revalidated_after_update(client):
    # bez "no-cache" przeglądarka może po aktualizacji długo używać starego client.js
    for path in ["/", "/panel", "/sw.js", "/manifest.webmanifest", "/static/client.js", "/static/panel.js"]:
        assert client.get(path).headers["cache-control"] == "no-cache", path


def test_tariff(client):
    tariff = client.get("/api/tariff").json()
    assert tariff["unlock_fee"] == float(config.UNLOCK_FEE)
    assert tariff["price_per_min"] == float(config.PRICE_PER_MIN)
    assert tariff["reservation_minutes"] == config.RESERVATION_MINUTES


def test_alert_ack(client):
    send(client, battery=5)
    alert = client.get("/api/alerts").json()[0]
    assert alert["acknowledged_at"] is None
    assert client.post(f"/api/alerts/{alert['id']}/ack").json()["acknowledged_at"] is not None
    assert client.get("/api/alerts", params={"open_only": True}).json() == []
    assert client.post("/api/alerts/999/ack").status_code == 404


def test_api_token(client, monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "sekret")
    body = {"scooter_id": "X", "city": "c", "lat": 0, "lon": 0, "battery": 50, "speed": 0}
    assert send(client).status_code == 401
    assert client.post("/api/telemetry", headers={"Authorization": "Bearer zly"}, json=body).status_code == 401
    assert client.post("/api/telemetry", headers={"Authorization": "Bearer sekret"}, json=body).status_code == 202
    assert client.post("/api/rentals/start", json={"scooter_id": "X"}).status_code == 401
    assert client.get("/api/scooters").status_code == 200   # odczyt bez tokenu


def test_commands_delivered_in_http_response_without_mqtt(client):
    rental_id = start_rental(client)
    assert send(client).json()["commands"] == ["unlock"]
    assert send(client, locked=False, speed=10).json()["commands"] == []   # wydana tylko raz
    client.post(f"/api/rentals/{rental_id}/end")
    batch = client.post("/api/telemetry/batch", json={"items": [
        {"scooter_id": "T-001", "city": "Słupsk", "lat": 54.48, "lon": 17.03, "battery": 80, "speed": 0}]})
    assert batch.status_code == 202 and batch.json()["commands"] == {"T-001": ["lock"]}
