"""Emulator floty (emulator/emulator.py) – logika bez sieci."""
import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "emulator"))
import emulator  # noqa: E402


def make(battery=80.0):
    scooter = emulator.Scooter("SC-T-001", "Słupsk", (54.4641, 17.0285), 1500, random.Random(1), now=0.0)
    scooter.battery = battery
    return scooter


class FakeApi:
    """Udaje backend: zwraca kolejne przygotowane odpowiedzi i zapamiętuje wywołane ścieżki."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def post(self, path, body=None):
        self.calls.append(path)
        return self.responses.pop(0)

    get = post


def test_cities_with_spaces_are_stripped():
    assert list(emulator.parse_places("Słupsk, Gdańsk")) == ["Słupsk", "Gdańsk"]
    with pytest.raises(SystemExit):
        emulator.parse_places("Słupsk, Kraków")


def test_telemetry_has_seq_and_send_time():
    scooter = make()
    first, second = scooter.telemetry(), scooter.telemetry()
    assert (first["seq"], second["seq"]) == (1, 2) and abs(second["sent_at"] - time.time()) < 5


def test_empty_battery_stops_but_does_not_lock():
    scooter = make(battery=0.0)
    scooter.command("unlock", 0.0)
    position = (scooter.lat, scooter.lon)
    scooter.step(8.0)
    assert scooter.locked is False and scooter.speed == 0 and (scooter.lat, scooter.lon) == position


def test_ride_moves_and_lock_command_parks():
    scooter = make()
    scooter.command("unlock", 0.0)
    position = (scooter.lat, scooter.lon)
    scooter.step(8.0)
    assert (scooter.lat, scooter.lon) != position and scooter.speed > 0
    scooter.rental_id = 7
    scooter.command("lock", 10.0)
    assert scooter.locked and scooter.speed == 0 and scooter.rental_id is None and scooter.next_ride > 10.0


def test_background_ride_decisions():
    scooter = make()
    assert scooter.wants_ride(scooter.next_ride) and not scooter.wants_ride(scooter.next_ride - 1)
    scooter.ride_started(5, now=100.0)
    assert not scooter.wants_ride(1e9)            # ma już wypożyczenie
    scooter.command("unlock", 100.0)
    assert not scooter.wants_end(100.0) and scooter.wants_end(scooter.ride_until)
    assert not make(battery=10).wants_ride(1e9)   # za słaba bateria na jazdę tła


def test_gives_up_rental_when_unlock_never_arrives():
    scooter = make()
    scooter.ride_started(5, now=100.0)
    assert not scooter.wants_end(100.0 + emulator.UNLOCK_WAIT_S - 1)
    assert scooter.wants_end(100.0 + emulator.UNLOCK_WAIT_S)


def test_closes_own_stale_rentals_from_previous_run():
    scooter = make()
    rentals = [{"id": 1, "scooter_id": scooter.id, "user_id": f"sim-{scooter.id}"},  # nasze z poprzedniego procesu
               {"id": 2, "scooter_id": scooter.id, "user_id": "ania"},               # operatora – nie ruszamy
               {"id": 3, "scooter_id": "OTHER-1", "user_id": "sim-OTHER-1"}]         # inny emulator
    api = FakeApi([(200, rentals), (200, {})])
    assert emulator.close_stale_rentals({scooter.id: scooter}, api) == 1
    assert api.calls[-1] == "/api/rentals/1/end"


def test_background_rides_use_rental_api():
    scooter = make()
    fleet = {scooter.id: scooter}
    api = FakeApi([(201, {"id": 42}), (200, {})])
    emulator.run_background_rides(fleet, api, scooter.next_ride, emulator.ErrorLog())
    assert scooter.rental_id == 42 and api.calls == ["/api/rentals/start"]
    scooter.command("unlock", 0.0)
    emulator.run_background_rides(fleet, api, scooter.ride_until, emulator.ErrorLog())
    assert api.calls[-1] == "/api/rentals/42/end" and scooter.rental_id is None


def test_refused_ride_is_rescheduled():
    scooter = make()
    api = FakeApi([(409, {"detail": "zajęta", "code": "not_available"})])
    now = scooter.next_ride
    emulator.run_background_rides({scooter.id: scooter}, api, now, emulator.ErrorLog())
    assert scooter.rental_id is None and scooter.next_ride > now


def test_rides_on_when_ending_in_no_parking_zone():
    scooter = make()
    scooter.ride_started(42, now=0.0)
    scooter.command("unlock", 0.0)
    api = FakeApi([(409, {"detail": "strefa", "code": "no_parking_zone"})])
    planned_end = scooter.ride_until
    emulator.run_background_rides({scooter.id: scooter}, api, planned_end, emulator.ErrorLog())
    assert scooter.rental_id == 42 and scooter.ride_until > planned_end   # jedzie dalej, spróbuje później
