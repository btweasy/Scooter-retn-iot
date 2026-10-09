"""Dane na żywo dla panelu operatora.

Zamiast odpytywać kilka endpointów co 2 sekundy, panel otwiera jeden strumień Server-Sent Events
(/api/stream) i dostaje gotowy "zrzut" stanu. Zrzut jest wspólny dla wszystkich otwartych paneli,
więc 10 operatorów nie oznacza 10 razy więcej zapytań do bazy.
"""
import asyncio
import json
import logging
import threading
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import config
from ..db import SessionLocal
from ..deps import get_db
from . import fleet, rentals

router = APIRouter(prefix="/api", tags=["panel"])
log = logging.getLogger("live")


def build_snapshot(db: Session) -> dict:
    return {
        "scooters": fleet.list_scooters(None, db),
        "stats": fleet.stats(db),
        "alerts": fleet.list_alerts(limit=8, open_only=True, db=db),
        "rentals": rentals.list_rentals(status="open", user_id=None, limit=500, db=db),
        "history": rentals.list_rentals(status="finished", user_id=None, limit=8, db=db),
        "reservations": rentals.list_active_reservations(db),
    }


@router.get("/snapshot")
def snapshot(db: Session = Depends(get_db)):
    return build_snapshot(db)


class SharedSnapshot:
    """Zrzut liczony najwyżej raz na `max_age_s` sekund, niezależnie od liczby odbiorców."""

    def __init__(self, max_age_s: float):
        self.max_age_s = max_age_s
        self.data = None
        self.built_at = 0.0
        self.lock = threading.Lock()

    def get(self) -> dict:
        with self.lock:
            age = time.monotonic() - self.built_at
            if self.data is None or age > self.max_age_s:
                with SessionLocal() as db:
                    self.data = build_snapshot(db)
                self.built_at = time.monotonic()
            return self.data


shared_snapshot = SharedSnapshot(max_age_s=config.STREAM_INTERVAL_S / 2)


@router.get("/stream")
async def stream(request: Request):
    """Server-Sent Events: zrzut stanu floty co STREAM_INTERVAL_S sekund."""
    async def events():
        while not await request.is_disconnected():
            try:
                data = await run_in_threadpool(shared_snapshot.get)
                yield f"data: {json.dumps(data)}\n\n"
            except Exception:
                log.exception("Błąd budowania zrzutu stanu")
                yield "event: error\ndata: {}\n\n"
            await asyncio.sleep(config.STREAM_INTERVAL_S)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
