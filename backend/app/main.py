"""Punkt startowy backendu: tworzy aplikację FastAPI, podłącza endpointy i uruchamia wątki w tle.

Struktura:
    routers/        endpointy HTTP (cienkie – tylko wejście/wyjście)
    rentals.py      logika rezerwacji i wypożyczeń (cennik, pauza, strefy)
    ingest.py       przyjmowanie telemetrii, alerts.py – alerty, status.py – status pojazdu
    mqtt_client.py  komunikacja z pojazdami, worker.py – wątki w tle
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, mqtt_client, worker
from .db import migrate
from .rentals import RentalError
from .routers import fleet, live, rentals, telemetry

logging.basicConfig(level=logging.INFO)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if config.AUTO_MIGRATE:
        migrate()
    worker.start()
    mqtt_client.start(on_telemetry=worker.submit)
    yield
    mqtt_client.stop()
    worker.stop()


app = FastAPI(title="Platforma IoT – flota hulajnóg", version="0.3.0", lifespan=lifespan)
app.include_router(fleet.router)
app.include_router(telemetry.router)
app.include_router(rentals.router)
app.include_router(live.router)


@app.exception_handler(RentalError)
def rental_error_to_http(request: Request, error: RentalError):
    """Błędy reguł biznesowych -> odpowiedź HTTP z czytelnym komunikatem i kodem dla programów."""
    return JSONResponse({"detail": error.message, "code": error.code}, status_code=error.http_status)


# Pliki stron: przeglądarka może je trzymać, ale przed użyciem ma zapytać serwer, czy są aktualne
# (ETag -> odpowiedź 304 bez treści, gdy nic się nie zmieniło). Bez tego po aktualizacji aplikacji
# przeglądarka potrafi długo używać starego client.js.
NO_STALE = {"Cache-Control": "no-cache"}


def web_file(name: str, media_type: str | None = None) -> FileResponse:
    return FileResponse(WEB_DIR / name, media_type=media_type, headers=NO_STALE)


class WebFiles(StaticFiles):
    """Katalog /static z tym samym nagłówkiem co strony."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(NO_STALE)
        return response


@app.get("/", include_in_schema=False)
def client_app():
    """Aplikacja klienta (PWA): wypożyczanie hulajnóg."""
    return web_file("index.html")


@app.get("/panel", include_in_schema=False)
def operator_panel():
    """Panel operatora: cała flota, alerty, wszystkie wypożyczenia."""
    return web_file("panel.html")


@app.get("/manifest.webmanifest", include_in_schema=False)
def pwa_manifest():
    return web_file("manifest.webmanifest", "application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    # service worker musi leżeć w katalogu głównym, żeby obejmował całą aplikację ("/")
    return web_file("sw.js", "text/javascript")


app.mount("/static", WebFiles(directory=WEB_DIR), name="static")
