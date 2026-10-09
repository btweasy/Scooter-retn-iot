"""Status pojazdu widoczny dla operatora.

Status nie jest zapisywany w bazie – wyliczamy go zawsze z aktualnych danych, więc nigdy się nie
"rozjedzie" z rzeczywistością. Kolejność reguł ma znaczenie (pierwsza pasująca wygrywa):

    offline      – brak telemetrii dłużej niż OFFLINE_AFTER_S
    paused       – wypożyczenie w pauzie
    in_ride      – trwa wypożyczenie albo pojazd jest odblokowany
    reserved     – aktywna rezerwacja
    low_battery  – bateria poniżej LOW_BATTERY (nie można wypożyczyć)
    available    – wolny, można wypożyczyć
"""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .models import Rental, Reservation, Scooter

STATUSES = ["available", "reserved", "in_ride", "paused", "low_battery", "offline"]


def scooter_status(scooter: Scooter, now: datetime,
                   rental: Rental | None = None, reservation: Reservation | None = None) -> str:
    seconds_silent = (now - scooter.last_seen).total_seconds()
    if seconds_silent > config.OFFLINE_AFTER_S:
        return "offline"
    if rental is not None and rental.status == "paused":
        return "paused"
    if rental is not None or not scooter.locked:
        return "in_ride"
    if reservation is not None:
        return "reserved"
    if scooter.battery < config.LOW_BATTERY:
        return "low_battery"
    return "available"


def open_rentals_by_scooter(db: Session) -> dict[str, Rental]:
    """Niezakończone wypożyczenia (aktywne i w pauzie), kluczem jest id pojazdu."""
    rentals = db.scalars(select(Rental).where(Rental.ended_at.is_(None)))
    return {rental.scooter_id: rental for rental in rentals}


def active_reservations_by_scooter(db: Session, now: datetime) -> dict[str, Reservation]:
    """Rezerwacje, które jeszcze nie wygasły, kluczem jest id pojazdu."""
    reservations = db.scalars(select(Reservation).where(Reservation.status == "active",
                                                        Reservation.expires_at > now))
    return {reservation.scooter_id: reservation for reservation in reservations}


def status_of(db: Session, scooter: Scooter, now: datetime) -> str:
    """Status jednego pojazdu (z wczytaniem jego wypożyczenia i rezerwacji)."""
    rental = db.scalar(select(Rental).where(Rental.scooter_id == scooter.id, Rental.ended_at.is_(None)))
    reservation = db.scalar(select(Reservation).where(Reservation.scooter_id == scooter.id,
                                                      Reservation.status == "active",
                                                      Reservation.expires_at > now))
    return scooter_status(scooter, now, rental, reservation)
