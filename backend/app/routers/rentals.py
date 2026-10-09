"""Rezerwacje i wypożyczenia. Endpointy są cienkie – reguły biznesowe są w app/rentals.py."""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config, rentals
from ..deps import get_db, requires_token
from ..geo import track_length_km
from ..models import Rental, Reservation, utcnow
from ..schemas import RentalEndIn, ScooterUserIn
from ..serializers import rental_to_dict, reservation_to_dict, thin_out

router = APIRouter(prefix="/api", tags=["wypożyczenia"])


@router.get("/tariff")
def tariff():
    """Aktualny cennik (dla aplikacji klienta). Przy starcie jazdy jest kopiowany do wypożyczenia."""
    return {
        "unlock_fee": float(config.UNLOCK_FEE),
        "price_per_min": float(config.PRICE_PER_MIN),
        "pause_price_per_min": float(config.PAUSE_PRICE_PER_MIN),
        "reservation_minutes": config.RESERVATION_MINUTES,
        "full_range_km": config.FULL_RANGE_KM,
    }


# ---------------------------------------------------------------- rezerwacje

@router.get("/reservations")
def list_active_reservations(db: Session = Depends(get_db)):
    now = utcnow()
    query = (select(Reservation)
             .where(Reservation.status == "active", Reservation.expires_at > now)
             .order_by(Reservation.id.desc()))
    return [reservation_to_dict(reservation) for reservation in db.scalars(query)]


@router.post("/reservations", status_code=201, dependencies=requires_token)
def create_reservation(body: ScooterUserIn, db: Session = Depends(get_db)):
    reservation = rentals.reserve(db, body.scooter_id, body.user_id)
    return reservation_to_dict(reservation)


@router.delete("/reservations/{reservation_id}", dependencies=requires_token)
def cancel_reservation(reservation_id: int, db: Session = Depends(get_db)):
    reservation = rentals.cancel_reservation(db, reservation_id)
    return reservation_to_dict(reservation)


# ---------------------------------------------------------------- wypożyczenia

@router.get("/rentals")
def list_rentals(status: str | None = Query(None, pattern="^(open|finished)$"),
                 user_id: str | None = None,
                 limit: int = Query(50, ge=1, le=500),
                 db: Session = Depends(get_db)):
    """status=open – trwające (także w pauzie), status=finished – historia."""
    query = select(Rental).order_by(Rental.id.desc()).limit(limit)
    if status == "open":
        query = query.where(Rental.ended_at.is_(None))
    if status == "finished":
        query = query.where(Rental.ended_at.is_not(None))
    if user_id:
        query = query.where(Rental.user_id == user_id)
    return [rental_to_dict(rental) for rental in db.scalars(query)]


@router.get("/rentals/{rental_id}")
def rental_details(rental_id: int, max_points: int = Query(1000, ge=2, le=5000), db: Session = Depends(get_db)):
    """Szczegóły z trasą. Dla trwającej jazdy dystans i paragon są liczone na bieżąco."""
    rental = rentals.get_rental(db, rental_id)
    points = rentals.route_points(db, rental, rental.ended_at)
    end = rental.ended_at or utcnow()

    details = rental_to_dict(rental)
    details["track"] = [[lat, lon] for lat, lon, _ in thin_out(points, max_points)]
    details["elapsed_s"] = round((end - rental.started_at).total_seconds())
    if rental.ended_at is None:
        details["distance_km"] = round(track_length_km(points), 3)
    return details


@router.post("/rentals/start", status_code=201, dependencies=requires_token)
def start_rental(body: ScooterUserIn, db: Session = Depends(get_db)):
    return rental_to_dict(rentals.start_rental(db, body.scooter_id, body.user_id))


@router.post("/rentals/{rental_id}/pause", dependencies=requires_token)
def pause_rental(rental_id: int, db: Session = Depends(get_db)):
    return rental_to_dict(rentals.pause_rental(db, rental_id))


@router.post("/rentals/{rental_id}/resume", dependencies=requires_token)
def resume_rental(rental_id: int, db: Session = Depends(get_db)):
    return rental_to_dict(rentals.resume_rental(db, rental_id))


@router.post("/rentals/{rental_id}/end", dependencies=requires_token)
def end_rental(rental_id: int, body: RentalEndIn | None = None, db: Session = Depends(get_db)):
    force = body.force if body is not None else False
    return rental_to_dict(rentals.end_rental(db, rental_id, force=force))
