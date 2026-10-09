"""Alerty dla operatora, sprawdzane przy każdym pomiarze telemetrii.

Zasada: alert powstaje tylko przy ZMIANIE stanu (flaga w tabeli scooters przechodzi z False na True).
Kolejne pomiary w tym samym stanie nie tworzą duplikatów.
"""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .geo import zone_for
from .models import Alert, Rental, Scooter


def add_alert(db: Session, scooter_id: str, kind: str, message: str, now: datetime):
    db.add(Alert(scooter_id=scooter_id, kind=kind, ts=now, message=f"{scooter_id}: {message}"))


def check_parking_zone(db: Session, scooter: Scooter, now: datetime):
    """Pojazd zaparkowany (zablokowany) w strefie zakazu parkowania."""
    if not scooter.locked:
        # Flagę zerujemy dopiero przy odblokowaniu. Dzięki temu drgania GPS na granicy strefy
        # u stojącego pojazdu nie wywołują kolejnych alertów.
        scooter.in_zone = False
        return

    zone = zone_for(scooter.lat, scooter.lon)
    if zone is not None and not scooter.in_zone:
        add_alert(db, scooter.id, "geofence", f"zaparkowana w strefie zakazu ({zone['name']})", now)
        scooter.in_zone = True


def check_battery(db: Session, scooter: Scooter, now: datetime):
    """Niski poziom baterii – z histerezą."""
    if scooter.battery < config.LOW_BATTERY and not scooter.low_battery_flag:
        add_alert(db, scooter.id, "low_battery", f"niski poziom akumulatora ({scooter.battery:.0f}%)", now)
        scooter.low_battery_flag = True
    elif scooter.battery >= config.LOW_BATTERY_CLEAR:
        # Histereza: kolejny alert możliwy dopiero po naładowaniu powyżej LOW_BATTERY_CLEAR,
        # a nie przy każdym wahaniu wokół progu.
        scooter.low_battery_flag = False


def check_unauthorized_ride(db: Session, scooter: Scooter, now: datetime):
    """Pojazd odblokowany bez wypożyczenia (np. kradzież, błąd sterownika)."""
    if scooter.locked:
        scooter.unauthorized_flag = False
        return
    if scooter.unauthorized_flag:
        return   # alert już wysłany

    has_open_rental = db.scalar(select(Rental.id).where(Rental.scooter_id == scooter.id,
                                                        Rental.ended_at.is_(None)))
    if has_open_rental:
        return

    # Jazda mogła się właśnie skończyć, a komenda "lock" jeszcze nie dotarła do pojazdu.
    grace_start = now - timedelta(seconds=config.UNAUTHORIZED_GRACE_S)
    just_finished = db.scalar(select(Rental.id).where(Rental.scooter_id == scooter.id,
                                                      Rental.ended_at >= grace_start))
    if just_finished:
        return

    add_alert(db, scooter.id, "unauthorized", "odblokowana bez aktywnego wypożyczenia", now)
    scooter.unauthorized_flag = True
