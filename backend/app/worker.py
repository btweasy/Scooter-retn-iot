"""Wątki w tle:

* zapis telemetrii – wiadomości z MQTT trafiają do kolejki i są zapisywane partiami (jedna transakcja na partię),
* porządki w wypożyczeniach – co 5 s (wygasłe rezerwacje, osierocone wypożyczenia),
* retencja – co 10 min usuwa starą historię telemetrii.
"""
import logging
import queue
import threading
import time
from datetime import timedelta

from sqlalchemy import delete, select

from . import config
from .db import SessionLocal
from .ingest import ingest_batch
from .models import Rental, Telemetry, utcnow
from .rental_watchdog import RentalWatchdog
from .schemas import TelemetryIn

log = logging.getLogger("worker")

_queue: queue.Queue = queue.Queue(maxsize=100_000)
_stop = threading.Event()
_threads: list[threading.Thread] = []
_WAKE_UP = object()   # wrzucany do kolejki przy zatrzymaniu, żeby wątek zapisu nie czekał
dropped = 0           # wiadomości odrzucone przy przepełnionej kolejce


# ---------------------------------------------------------------- zapis telemetrii

def submit(message: TelemetryIn) -> bool:
    """Wywoływane z wątku sieciowego MQTT – nie może blokować (zerwałoby się podtrzymanie połączenia)."""
    global dropped
    try:
        _queue.put_nowait(message)
        return True
    except queue.Full:
        dropped += 1
        if dropped % 1000 == 1:
            log.warning("Kolejka zapisu pełna – odrzucono już %d wiadomości", dropped)
        return False


def queue_size() -> int:
    return _queue.qsize()


def _collect_batch(first: TelemetryIn) -> list[TelemetryIn]:
    """Zbiera wiadomości, aż będzie ich INGEST_BATCH_SIZE albo minie INGEST_FLUSH_S."""
    batch = [first]
    deadline = time.monotonic() + config.INGEST_FLUSH_S
    while len(batch) < config.INGEST_BATCH_SIZE:
        time_left = deadline - time.monotonic()
        if time_left <= 0:
            break
        try:
            item = _queue.get(timeout=time_left)
        except queue.Empty:
            break
        if item is _WAKE_UP:
            break
        batch.append(item)
    return batch


def _save_batch(first: TelemetryIn):
    with SessionLocal() as db:
        ingest_batch(db, _collect_batch(first))


def flush():
    """Zapisuje wszystko, co czeka w kolejce (przy zatrzymaniu aplikacji i w testach)."""
    while True:
        try:
            first = _queue.get_nowait()
        except queue.Empty:
            return
        if first is not _WAKE_UP:
            _save_batch(first)


def _ingest_loop():
    while not _stop.is_set():
        try:
            first = _queue.get(timeout=0.5)
        except queue.Empty:
            continue
        if first is _WAKE_UP:
            continue
        try:
            _save_batch(first)
        except Exception:
            log.exception("Błąd wątku zapisu")


# ---------------------------------------------------------------- retencja

def purge_old_telemetry() -> int:
    """Usuwa historię starszą niż TELEMETRY_RETENTION_H. Punkty pojazdów w trakcie wypożyczenia zostają –
    są potrzebne do policzenia trasy."""
    if config.TELEMETRY_RETENTION_H <= 0:
        return 0
    cutoff = utcnow() - timedelta(hours=config.TELEMETRY_RETENTION_H)
    rented_scooters = select(Rental.scooter_id).where(Rental.ended_at.is_(None))
    with SessionLocal() as db:
        deleted = db.execute(delete(Telemetry).where(Telemetry.ts < cutoff,
                                                     Telemetry.scooter_id.not_in(rented_scooters))).rowcount
        db.commit()
    if deleted:
        log.info("Retencja: usunięto %d starych pomiarów", deleted)
    return deleted


# ---------------------------------------------------------------- uruchamianie

def _repeat_every(seconds: float, job, name: str):
    """Wykonuje job() co `seconds` sekund, aż do zatrzymania aplikacji. Błąd nie zatrzymuje pętli."""
    while not _stop.wait(seconds):
        try:
            job()
        except Exception:
            log.exception("Błąd zadania w tle: %s", name)


_watchdog = RentalWatchdog()


def run_watchdog():
    with SessionLocal() as db:
        _watchdog.tick(db)


def start():
    _stop.clear()
    _threads[:] = [
        threading.Thread(target=_ingest_loop, name="ingest", daemon=True),
        threading.Thread(target=_repeat_every, args=(5, run_watchdog, "porządki"),
                         name="rental-watchdog", daemon=True),
        threading.Thread(target=_repeat_every, args=(600, purge_old_telemetry, "retencja"),
                         name="retention", daemon=True),
    ]
    for thread in _threads:
        thread.start()


def stop():
    _stop.set()
    try:
        _queue.put_nowait(_WAKE_UP)
    except queue.Full:
        pass
    for thread in _threads:
        thread.join(timeout=5)
    flush()
