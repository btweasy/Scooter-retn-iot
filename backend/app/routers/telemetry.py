"""Telemetria przez HTTP – zapasowa droga, gdy pojazd nie ma MQTT (główna droga: mqtt_client.py).

W odpowiedzi pojazd dostaje komendy, które na niego czekają (gdy MQTT jest wyłączone) –
dzięki temu komunikacja działa w obie strony także bez brokera.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import commands
from ..deps import get_db, requires_token
from ..ingest import ingest_batch
from ..schemas import TelemetryBatchIn, TelemetryIn

router = APIRouter(prefix="/api/telemetry", tags=["telemetria"], dependencies=requires_token)


def save_or_503(db: Session, messages: list[TelemetryIn]):
    results = ingest_batch(db, messages)
    if all(result.status == "failed" for result in results):
        raise HTTPException(503, "Nie udało się zapisać telemetrii")
    return results


@router.post("", status_code=202)
def post_one(message: TelemetryIn, db: Session = Depends(get_db)):
    """Jeden pomiar z jednego pojazdu."""
    result = save_or_503(db, [message])[0]
    return {
        "accepted": result.status == "accepted",
        "duplicate": result.status == "duplicate",
        "commands": commands.pop_pending(message.scooter_id),
    }


@router.post("/batch", status_code=202)
def post_batch(body: TelemetryBatchIn, db: Session = Depends(get_db)):
    """Wiele pomiarów w jednym żądaniu (np. bramka albo emulator floty)."""
    results = save_or_503(db, body.items)

    commands_for_scooters = {}
    for scooter_id in {message.scooter_id for message in body.items}:
        waiting = commands.pop_pending(scooter_id)
        if waiting:
            commands_for_scooters[scooter_id] = waiting

    return {
        "accepted": sum(1 for result in results if result.status == "accepted"),
        "duplicates": sum(1 for result in results if result.status == "duplicate"),
        "commands": commands_for_scooters,
    }
