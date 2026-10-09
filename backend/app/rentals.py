"""Logika wypożyczeń – niezależna od HTTP (te same funkcje wywołuje API i wątek porządkowy).

Cykl życia (maszyna stanów):

    [rezerwacja: active] --start--> [wypożyczenie: active] <--pauza/wznów--> [paused]
            |                                  |                                 |
     anuluj / wygaśnij                         +------------- zakończ -----------+--> [finished]

Zasady wzorowane na usługach Bolt / Lime / Dott:
  * rezerwacja jest bezpłatna i wygasa po RESERVATION_MINUTES,
  * opłata = opłata za odblokowanie + minuty jazdy + (tańsze) minuty pauzy, liczone za każdą rozpoczętą minutę,
  * cennik zapisujemy przy starcie – późniejsza zmiana cennika nie zmienia ceny trwającej jazdy,
  * nie można zakończyć jazdy w strefie zakazu parkowania (operator może to wymusić: force=True),
  * w pauzie pojazd jest zablokowany, ale pozostaje przypisany do klienta.
"""
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import commands, config
from .geo import track_length_km, zone_for
from .models import Rental, Reservation, Scooter, Telemetry, utcnow
from .status import scooter_status


class RentalError(Exception):
    """Błąd reguły biznesowej. API zamienia go na odpowiedź HTTP {"detail": message, "code": code}."""

    def __init__(self, http_status: int, code: str, message: str):
        super().__init__(message)
        self.http_status = http_status
        self.code = code
        self.message = message


# ---------------------------------------------------------------- cennik

@dataclass
class Receipt:
    """Paragon: z czego składa się cena."""
    unlock_fee: Decimal
    ride_minutes: int
    ride_cost: Decimal
    pause_minutes: int
    pause_cost: Decimal
    total: Decimal

    def to_dict(self) -> dict:
        return {key: float(value) if isinstance(value, Decimal) else value for key, value in asdict(self).items()}


FREE = Receipt(Decimal("0.00"), 0, Decimal("0.00"), 0, Decimal("0.00"), Decimal("0.00"))


def started_minutes(seconds: float) -> int:
    """Liczba rozpoczętych minut (61 s = 2 minuty)."""
    return math.ceil(seconds / 60)


def pause_seconds(rental: Rental, until: datetime) -> float:
    """Łączny czas pauz: zakończone pauzy + trwająca (jeśli jest)."""
    total = rental.paused_seconds
    if rental.paused_at is not None:
        total += (until - rental.paused_at).total_seconds()
    return total


def calculate_receipt(rental: Rental, until: datetime) -> Receipt:
    all_seconds = (until - rental.started_at).total_seconds()
    paused = pause_seconds(rental, until)
    riding = max(0.0, all_seconds - paused)

    ride_minutes = max(1, started_minutes(riding))   # co najmniej jedna minuta jazdy
    pause_minutes = started_minutes(paused)
    ride_cost = rental.price_per_min * ride_minutes
    pause_cost = rental.pause_price_per_min * pause_minutes
    total = (rental.unlock_fee + ride_cost + pause_cost).quantize(Decimal("0.01"))
    return Receipt(rental.unlock_fee, ride_minutes, ride_cost, pause_minutes, pause_cost, total)


# ---------------------------------------------------------------- trasa

def route_points(db: Session, rental: Rental, until: datetime | None = None) -> list:
    """Punkty trasy (lat, lon, czas) od startu wypożyczenia. Czas: z urządzenia, a gdy go brak – odbioru."""
    point_time = func.coalesce(Telemetry.sent_at, Telemetry.ts)
    query = (select(Telemetry.lat, Telemetry.lon, point_time)
             .where(Telemetry.scooter_id == rental.scooter_id, Telemetry.ts >= rental.started_at)
             .order_by(Telemetry.ts, Telemetry.id))
    if until is not None:
        query = query.where(Telemetry.ts <= until)
    return db.execute(query).all()


# ---------------------------------------------------------------- odczyt

def get_scooter(db: Session, scooter_id: str) -> Scooter:
    scooter = db.get(Scooter, scooter_id)
    if scooter is None:
        raise RentalError(404, "scooter_not_found", "Nie znaleziono hulajnogi")
    return scooter


def get_rental(db: Session, rental_id: int) -> Rental:
    rental = db.get(Rental, rental_id)
    if rental is None:
        raise RentalError(404, "rental_not_found", "Nie znaleziono wypożyczenia")
    return rental


def get_open_rental(db: Session, scooter_id: str) -> Rental | None:
    return db.scalar(select(Rental).where(Rental.scooter_id == scooter_id, Rental.ended_at.is_(None)))


def get_active_reservation(db: Session, scooter_id: str, now: datetime) -> Reservation | None:
    return db.scalar(select(Reservation).where(Reservation.scooter_id == scooter_id,
                                               Reservation.status == "active",
                                               Reservation.expires_at > now))


# ---------------------------------------------------------------- rezerwacje

def expire_reservations(db: Session, now: datetime) -> int:
    """Oznacza przeterminowane rezerwacje jako wygasłe. Zwraca ich liczbę."""
    result = db.execute(update(Reservation)
                        .where(Reservation.status == "active", Reservation.expires_at <= now)
                        .values(status="expired"))
    db.commit()
    return result.rowcount


def reserve(db: Session, scooter_id: str, user_id: str) -> Reservation:
    now = utcnow()
    expire_reservations(db, now)
    scooter = get_scooter(db, scooter_id)

    status = scooter_status(scooter, now, get_open_rental(db, scooter_id), get_active_reservation(db, scooter_id, now))
    if status != "available":
        raise RentalError(409, "not_available", f"Hulajnoga niedostępna (status: {status})")

    reservation = Reservation(scooter_id=scooter_id, user_id=user_id, status="active", created_at=now,
                              expires_at=now + timedelta(minutes=config.RESERVATION_MINUTES))
    db.add(reservation)
    commit_or_conflict(db, "Hulajnoga została właśnie zarezerwowana przez kogoś innego")
    return reservation


def cancel_reservation(db: Session, reservation_id: int) -> Reservation:
    reservation = db.get(Reservation, reservation_id)
    if reservation is None:
        raise RentalError(404, "reservation_not_found", "Nie znaleziono rezerwacji")
    if reservation.status != "active":
        raise RentalError(409, "reservation_not_active", f"Rezerwacja nie jest aktywna ({reservation.status})")
    reservation.status = "cancelled"
    db.commit()
    return reservation


# ---------------------------------------------------------------- wypożyczenia

def start_rental(db: Session, scooter_id: str, user_id: str) -> Rental:
    now = utcnow()
    expire_reservations(db, now)
    scooter = get_scooter(db, scooter_id)

    reservation = get_active_reservation(db, scooter_id, now)
    if reservation is not None and reservation.user_id != user_id:
        raise RentalError(409, "reserved", "Hulajnoga jest zarezerwowana przez innego użytkownika")

    # własna rezerwacja nie blokuje startu – dlatego nie przekazujemy jej do scooter_status
    status = scooter_status(scooter, now, get_open_rental(db, scooter_id))
    if status != "available":
        raise RentalError(409, "not_available", f"Hulajnoga niedostępna (status: {status})")

    rental = Rental(scooter_id=scooter_id, user_id=user_id, status="active", started_at=now,
                    unlock_fee=config.UNLOCK_FEE, price_per_min=config.PRICE_PER_MIN,
                    pause_price_per_min=config.PAUSE_PRICE_PER_MIN)
    db.add(rental)
    if reservation is not None:
        reservation.status = "used"
    commit_or_conflict(db, "Hulajnoga niedostępna (status: in_ride)")

    commands.send(scooter_id, "unlock")
    return rental


def pause_rental(db: Session, rental_id: int) -> Rental:
    rental = get_rental(db, rental_id)
    change_status(db, rental, expected="active", status="paused", paused_at=utcnow())
    commands.send(rental.scooter_id, "lock")   # w pauzie pojazd stoi zablokowany
    return rental


def resume_rental(db: Session, rental_id: int) -> Rental:
    rental = get_rental(db, rental_id)
    if rental.status != "paused":
        raise RentalError(409, "not_paused", "Wypożyczenie nie jest w pauzie")
    now = utcnow()
    change_status(db, rental, expected="paused", status="active", paused_at=None,
                  paused_seconds=round(pause_seconds(rental, now)))
    commands.send(rental.scooter_id, "unlock")
    return rental


def end_rental(db: Session, rental_id: int, force: bool = False,
               automatic: bool = False, end_time: datetime | None = None) -> Rental:
    """Kończy jazdę i rozlicza ją.

    force      – pomiń zakaz kończenia jazdy w strefie zakazu parkowania (operator),
    automatic  – zamknięcie przez system (pojazd nie odblokował się); bez przejechanego dystansu bezpłatne,
    end_time   – moment zakończenia (domyślnie teraz).
    """
    rental = get_rental(db, rental_id)
    if rental.ended_at is not None:
        raise RentalError(409, "already_finished", "Wypożyczenie jest już zakończone")

    scooter = get_scooter(db, rental.scooter_id)
    zone = zone_for(scooter.lat, scooter.lon)
    if zone is not None and not force:
        raise RentalError(409, "no_parking_zone",
                          f"Nie można zakończyć jazdy w strefie zakazu parkowania ({zone['name']})")

    end_time = end_time or utcnow()
    distance = track_length_km(route_points(db, rental, end_time))
    if automatic and distance == 0:
        receipt = FREE
    else:
        receipt = calculate_receipt(rental, end_time)

    change_status(db, rental, expected=rental.status, status="finished", ended_at=end_time,
                  paused_at=None, paused_seconds=round(pause_seconds(rental, end_time)),
                  distance_km=distance, ride_minutes=receipt.ride_minutes, pause_minutes=receipt.pause_minutes,
                  cost_pln=receipt.total, closed_automatically=automatic)
    commands.send(rental.scooter_id, "lock")
    return rental


# ---------------------------------------------------------------- pomocnicze

def change_status(db: Session, rental: Rental, expected: str, **new_values):
    """Zmienia wypożyczenie tylko wtedy, gdy wciąż ma status `expected`.

    Warunek jest w samym UPDATE, więc z dwóch równoczesnych żądań (np. dwa kliknięcia "zakończ")
    wygra dokładnie jedno – drugie dostanie błąd 409, zamiast policzyć jazdę dwa razy.
    """
    changed = db.execute(update(Rental)
                         .where(Rental.id == rental.id, Rental.status == expected, Rental.ended_at.is_(None))
                         .values(**new_values)).rowcount
    db.commit()
    if not changed:
        db.refresh(rental)
        if rental.ended_at is not None:
            raise RentalError(409, "already_finished", "Wypożyczenie jest już zakończone")
        raise RentalError(409, f"not_{expected}", f"Nieprawidłowy stan wypożyczenia ({rental.status})")
    db.refresh(rental)


def commit_or_conflict(db: Session, message: str):
    """Commit; naruszenie unikalnego indeksu (wyścig dwóch żądań) zamieniamy na błąd 409."""
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise RentalError(409, "conflict", message)
