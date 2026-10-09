"""Zamiana obiektów bazy danych na JSON zwracany przez API."""
from datetime import datetime

from . import config
from .models import Alert, Rental, Reservation, Scooter, utcnow
from .rentals import FREE, Receipt, calculate_receipt


def iso(moment: datetime | None) -> str | None:
    """Czas UTC w formacie ISO z sufiksem Z (przeglądarka zamieni go na czas lokalny)."""
    if moment is None:
        return None
    return moment.isoformat() + "Z"


def scooter_to_dict(scooter: Scooter, status: str) -> dict:
    return {
        "id": scooter.id,
        "city": scooter.city,
        "lat": scooter.lat,
        "lon": scooter.lon,
        "battery": round(scooter.battery, 1),
        "range_km": round(scooter.battery / 100 * config.FULL_RANGE_KM, 1),   # szacowany zasięg
        "speed": round(scooter.speed, 1),
        "status": status,
        "last_seen": iso(scooter.last_seen),
    }


def rental_receipt(rental: Rental) -> Receipt:
    """Paragon: dla zakończonej jazdy – zapisany, dla trwającej – "gdyby zakończyć teraz"."""
    if rental.ended_at is None:
        return calculate_receipt(rental, utcnow())
    if rental.cost_pln == 0:
        return FREE
    return Receipt(unlock_fee=rental.unlock_fee,
                   ride_minutes=rental.ride_minutes, ride_cost=rental.price_per_min * rental.ride_minutes,
                   pause_minutes=rental.pause_minutes, pause_cost=rental.pause_price_per_min * rental.pause_minutes,
                   total=rental.cost_pln)


def rental_to_dict(rental: Rental) -> dict:
    receipt = rental_receipt(rental)
    return {
        "id": rental.id,
        "scooter_id": rental.scooter_id,
        "user_id": rental.user_id,
        "status": rental.status,
        "started_at": iso(rental.started_at),
        "ended_at": iso(rental.ended_at),
        "paused_at": iso(rental.paused_at),
        "distance_km": round(rental.distance_km or 0, 3),
        "cost_pln": float(receipt.total),
        "receipt": receipt.to_dict(),
        "closed_automatically": rental.closed_automatically,
    }


def reservation_to_dict(reservation: Reservation) -> dict:
    return {
        "id": reservation.id,
        "scooter_id": reservation.scooter_id,
        "user_id": reservation.user_id,
        "status": reservation.status,
        "created_at": iso(reservation.created_at),
        "expires_at": iso(reservation.expires_at),
    }


def alert_to_dict(alert: Alert) -> dict:
    return {
        "id": alert.id,
        "scooter_id": alert.scooter_id,
        "kind": alert.kind,
        "message": alert.message,
        "ts": iso(alert.ts),
        "acknowledged_at": iso(alert.acknowledged_at),
    }


def thin_out(points: list, max_points: int) -> list:
    """Przerzedza długą trasę do około max_points punktów (co n-ty punkt). Ostatni punkt zostaje zawsze,
    żeby linia kończyła się w aktualnym położeniu pojazdu."""
    if len(points) <= max_points:
        return list(points)
    step = len(points) // max_points + 1
    thinned = list(points[::step])
    if thinned[-1] != points[-1]:
        thinned.append(points[-1])
    return thinned
