"""Stan floty dla operatora: pojazdy, strefy, alerty, statystyki, metryki, zdrowie systemu."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from .. import config, mqtt_client, worker
from ..deps import get_db, requires_token
from ..geo import ZONES
from ..metrics import metrics
from ..models import Alert, Rental, Scooter, utcnow
from ..serializers import alert_to_dict, scooter_to_dict
from ..status import STATUSES, active_reservations_by_scooter, open_rentals_by_scooter, scooter_status

router = APIRouter(tags=["flota"])


def fleet_with_status(db: Session, now: datetime, city: str | None = None) -> list[tuple[Scooter, str]]:
    """Wszystkie pojazdy razem z ich statusem (wypożyczenia i rezerwacje wczytane jednym zapytaniem)."""
    rentals = open_rentals_by_scooter(db)
    reservations = active_reservations_by_scooter(db, now)
    query = select(Scooter).order_by(Scooter.id)
    if city:
        query = query.where(Scooter.city == city)

    fleet = []
    for scooter in db.scalars(query):
        status = scooter_status(scooter, now, rentals.get(scooter.id), reservations.get(scooter.id))
        fleet.append((scooter, status))
    return fleet


@router.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse({"status": "error", "db": False}, status_code=503)
    return {
        "status": "ok",
        "db": True,
        "mqtt": mqtt_client.connected if config.MQTT_ENABLED else None,
        "ingest_queue": worker.queue_size(),
    }


@router.get("/api/scooters")
def list_scooters(city: str | None = None, db: Session = Depends(get_db)):
    return [scooter_to_dict(scooter, status) for scooter, status in fleet_with_status(db, utcnow(), city)]


@router.get("/api/zones")
def list_zones():
    return ZONES


@router.get("/api/alerts")
def list_alerts(limit: int = Query(20, ge=1, le=500), open_only: bool = False, db: Session = Depends(get_db)):
    query = select(Alert).order_by(Alert.id.desc()).limit(limit)
    if open_only:
        query = query.where(Alert.acknowledged_at.is_(None))
    return [alert_to_dict(alert) for alert in db.scalars(query)]


@router.post("/api/alerts/{alert_id}/ack", dependencies=requires_token)
def acknowledge_alert(alert_id: int, db: Session = Depends(get_db)):
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(404, "Nie znaleziono alertu")
    if alert.acknowledged_at is None:
        alert.acknowledged_at = utcnow()
        db.commit()
    return alert_to_dict(alert)


@router.get("/api/stats")
def stats(db: Session = Depends(get_db)):
    fleet = fleet_with_status(db, utcnow())

    by_status = {status: 0 for status in STATUSES}
    by_city = {}
    battery_sum = 0.0
    for scooter, status in fleet:
        by_status[status] += 1
        by_city[scooter.city] = by_city.get(scooter.city, 0) + 1
        battery_sum += scooter.battery

    open_rentals = db.scalar(select(func.count()).select_from(Rental).where(Rental.ended_at.is_(None)))
    return {
        "total": len(fleet),
        "by_status": by_status,
        "by_city": by_city,
        "avg_battery": round(battery_sum / len(fleet), 1) if fleet else 0,
        "active_rentals": open_rentals,
        "messages_per_s": metrics.rate(),
    }


@router.get("/api/metrics")
def research_metrics():
    """Metryki do badań: opóźnienie (sent_at -> zapis), przepustowość, duplikaty, straty (luki w seq)."""
    return {
        **metrics.snapshot(),
        "ingest_queue": worker.queue_size(),
        "ingest_dropped": worker.dropped,
        "mqtt_connected": mqtt_client.connected if config.MQTT_ENABLED else None,
    }
