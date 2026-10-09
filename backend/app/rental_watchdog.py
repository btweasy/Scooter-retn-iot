"""Porządki w tle (co kilka sekund):

1. Rezerwacje po czasie oznaczamy jako wygasłe.
2. Zamykamy "osierocone" wypożyczenia: jazda w toku (status active), a pojazd jest online i zablokowany
   dłużej niż RENTAL_LOCKED_TIMEOUT_S. Dzieje się tak, gdy komenda unlock zaginęła (np. restart backendu
   kasuje kolejkę komend HTTP) albo urządzenie zrestartowało się w trakcie jazdy. Bez tego pojazd byłby
   "wypożyczony" na zawsze i nikt nie mógłby go wypożyczyć.

Wypożyczenia w pauzie pomijamy – tam pojazd jest zablokowany celowo.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import config
from .alerts import add_alert
from .models import Rental, Scooter, utcnow
from .rentals import RentalError, end_rental, expire_reservations

log = logging.getLogger("watchdog")


class RentalWatchdog:
    def __init__(self):
        # id wypożyczenia -> od kiedy widzimy pojazd zablokowany (w pamięci: po restarcie
        # procesu odliczanie zaczyna się od nowa, co tylko opóźnia sprzątanie)
        self.locked_since: dict[int, datetime] = {}

    def tick(self, db: Session) -> list[int]:
        now = utcnow()
        expire_reservations(db, now)
        return self.close_orphaned_rentals(db, now)

    def close_orphaned_rentals(self, db: Session, now: datetime) -> list[int]:
        rows = db.execute(select(Rental, Scooter)
                          .join(Scooter, Scooter.id == Rental.scooter_id)
                          .where(Rental.status == "active")).all()
        closed = []
        still_open = set()

        for rental, scooter in rows:
            still_open.add(rental.id)
            if not self.looks_orphaned(rental, scooter, now):
                self.locked_since.pop(rental.id, None)
                continue

            since = self.locked_since.setdefault(rental.id, max(rental.started_at, scooter.last_seen))
            locked_for = (now - since).total_seconds()
            if locked_for < config.RENTAL_LOCKED_TIMEOUT_S:
                continue

            try:
                end_rental(db, rental.id, force=True, automatic=True, end_time=since)
            except RentalError:
                continue   # w międzyczasie zamknięte inną drogą
            add_alert(db, scooter.id, "rental_closed",
                      f"wypożyczenie #{rental.id} zamknięte automatycznie (pojazd zablokowany {int(locked_for)} s)",
                      now)
            db.commit()
            closed.append(rental.id)
            self.locked_since.pop(rental.id, None)

        # zapominamy wypożyczenia zamknięte w inny sposób
        for rental_id in list(self.locked_since):
            if rental_id not in still_open:
                del self.locked_since[rental_id]

        if closed:
            log.warning("Automatycznie zamknięto osierocone wypożyczenia: %s", closed)
        return closed

    @staticmethod
    def looks_orphaned(rental: Rental, scooter: Scooter, now: datetime) -> bool:
        """Jazda w toku, a pojazd stoi zablokowany i nadaje. Pojazd offline pomijamy –
        może właśnie jechać bez zasięgu."""
        is_online = scooter.last_seen >= now - timedelta(seconds=config.OFFLINE_AFTER_S)
        reported_after_start = scooter.last_seen >= rental.started_at
        return scooter.locked and is_online and reported_after_start
