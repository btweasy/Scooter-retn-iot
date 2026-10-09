import os
import tempfile
import time

# Konfiguracja czytana jest przy imporcie aplikacji – zmienne muszą być ustawione wcześniej.
# TEST_DATABASE_URL pozwala uruchomić testy na PostgreSQL (np. postgresql+psycopg2://...); domyślnie SQLite.
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL") or \
    "sqlite:///" + os.path.join(tempfile.gettempdir(), "scooters_test.db")
os.environ["MQTT_ENABLED"] = "0"
os.environ["AUTO_MIGRATE"] = "0"   # schemat z create_all; migracje sprawdza osobny test
os.environ.pop("API_TOKEN", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import commands  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.metrics import metrics  # noqa: E402

FREE_SPOT = {"lat": 54.4800, "lon": 17.0300}      # Słupsk, poza strefą zakazu
NO_PARKING = {"lat": 54.4591, "lon": 17.0359}     # Słupsk, Park Kultury i Wypoczynku – strefa zakazu parkowania


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    metrics.reset()
    commands.clear()
    with TestClient(app) as c:
        yield c


def send(client, sid="T-001", lat=54.4800, lon=17.0300, battery=80.0, speed=0.0, locked=True, city="Słupsk",
         **extra):
    """Wysyła pomiar telemetrii przez HTTP (dodatkowe pola, np. sent_at, seq, przechodzą dalej)."""
    return client.post("/api/telemetry", json={"scooter_id": sid, "city": city, "lat": lat, "lon": lon,
                                               "battery": battery, "speed": speed, "locked": locked, **extra})


def start_rental(client, sid="T-001", user="ania"):
    """Pojazd zgłasza się w wolnym miejscu i zostaje wypożyczony. Zwraca id wypożyczenia."""
    send(client, sid=sid)
    response = client.post("/api/rentals/start", json={"scooter_id": sid, "user_id": user})
    assert response.status_code == 201, response.json()
    return response.json()["id"]


def wait_for(condition, timeout=3.0):
    """Czeka na warunek spełniany przez wątek w tle (np. zapis z kolejki MQTT)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.02)
    return condition()
