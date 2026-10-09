"""Przyjmowanie telemetrii: zapis pomiaru, aktualizacja stanu pojazdu, alerty, metryki.

Wspólne dla obu dróg: MQTT (główna) i HTTP (zapasowa).
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import alerts
from .metrics import metrics
from .models import Scooter, Telemetry, utcnow
from .schemas import TelemetryIn

log = logging.getLogger("ingest")


@dataclass
class IngestResult:
    status: str                       # "accepted" | "duplicate" | "failed"
    scooter_id: str
    latency_s: float | None = None    # opóźnienie: czas zapisu - czas wysłania z urządzenia
    lost_before: int = 0              # ile wiadomości zginęło przed tą (luka w numeracji seq)


def epoch_to_utc(epoch: float | None) -> datetime | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).replace(tzinfo=None)


def is_duplicate_or_late(scooter: Scooter, message: TelemetryIn, sent_at: datetime | None) -> bool:
    """QoS 1 w MQTT gwarantuje dostarczenie "co najmniej raz" – ta sama wiadomość może przyjść dwa razy,
    a wiadomość spóźniona nie może nadpisać nowszego stanu."""
    if sent_at is not None and scooter.last_sent_at is not None:
        return sent_at <= scooter.last_sent_at
    # urządzenie bez zegara: rozpoznajemy tylko dokładny duplikat po numerze
    return message.seq is not None and message.seq == scooter.last_seq


def count_lost_messages(scooter: Scooter, message: TelemetryIn) -> int:
    """Luka w numeracji = zgubione wiadomości. Mniejszy numer przy nowszym czasie oznacza restart
    urządzenia (numeracja od nowa), a nie stratę."""
    if message.seq is None or scooter.last_seq is None:
        return 0
    if message.seq > scooter.last_seq + 1:
        return message.seq - scooter.last_seq - 1
    return 0


def ingest_message(db: Session, message: TelemetryIn) -> IngestResult:
    """Przetwarza jeden pomiar. Nie robi commit – robi to ingest_batch (cała partia = jedna transakcja)."""
    now = utcnow()
    sent_at = epoch_to_utc(message.sent_at)

    scooter = db.get(Scooter, message.scooter_id)
    is_new = scooter is None
    if is_new:
        scooter = Scooter(id=message.scooter_id, in_zone=False, low_battery_flag=False, unauthorized_flag=False)
        db.add(scooter)
    elif is_duplicate_or_late(scooter, message, sent_at):
        return IngestResult("duplicate", message.scooter_id)

    lost = 0 if is_new else count_lost_messages(scooter, message)

    # aktualny stan pojazdu
    scooter.city = message.city
    scooter.lat = message.lat
    scooter.lon = message.lon
    scooter.battery = message.battery
    scooter.speed = message.speed
    scooter.locked = message.locked
    scooter.last_seen = now
    if sent_at is not None:
        scooter.last_sent_at = sent_at
    if message.seq is not None:
        scooter.last_seq = message.seq
    if is_new:
        db.flush()   # kolejna wiadomość tego pojazdu w tej samej partii znajdzie go przez db.get

    alerts.check_parking_zone(db, scooter, now)
    alerts.check_battery(db, scooter, now)
    alerts.check_unauthorized_ride(db, scooter, now)

    # historia
    db.add(Telemetry(scooter_id=scooter.id, ts=now, sent_at=sent_at, seq=message.seq,
                     lat=message.lat, lon=message.lon, battery=message.battery, speed=message.speed))

    latency = (now - sent_at).total_seconds() if sent_at is not None else None
    return IngestResult("accepted", message.scooter_id, latency, lost)


def ingest_batch(db: Session, messages: list[TelemetryIn]) -> list[IngestResult]:
    """Zapisuje partię wiadomości w jednej transakcji (szybciej niż commit na każdą wiadomość).

    Jeśli partia się nie zapisze, ponawiamy wiadomości pojedynczo – jedna zła wiadomość
    nie może zablokować pozostałych. Metryki liczymy dopiero po udanym zapisie.
    """
    try:
        results = [ingest_message(db, message) for message in messages]
        db.commit()
    except Exception:
        db.rollback()
        log.exception("Błąd zapisu partii (%d wiad.), ponawiam pojedynczo", len(messages))
        results = [ingest_one_safely(db, message) for message in messages]

    for result in results:
        if result.status == "accepted":
            metrics.record(result.latency_s, result.lost_before)
        elif result.status == "duplicate":
            metrics.record_duplicate()
    return results


def ingest_one_safely(db: Session, message: TelemetryIn) -> IngestResult:
    try:
        result = ingest_message(db, message)
        db.commit()
        return result
    except Exception:
        db.rollback()
        log.exception("Odrzucono wiadomość z %s", message.scooter_id)
        return IngestResult("failed", message.scooter_id)
