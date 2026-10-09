"""Geostrefy i odległości na gotowych bibliotekach: Shapely (wielokąty) i geopy (odległości)."""
from datetime import datetime

from geopy.distance import geodesic
from shapely.geometry import Point, Polygon

from . import config
from .zones_data import NO_PARKING_ZONES

# Strefy zakazu parkowania mają kształt prawdziwych miejsc (place, deptaki, parki, nabrzeża) –
# obrysy pobrane z OpenStreetMap skryptem tools/fetch_zones.py i zapisane w zones_data.py.
ZONES = NO_PARKING_ZONES

# Wielokąty Shapely budowane raz przy starcie. Uwaga: kolejność współrzędnych to (lat, lon)
# zarówno w wielokątach, jak i w punktach – musi pozostać spójna.
_SHAPES = [(zone, Polygon(zone["polygon"])) for zone in ZONES]


def zone_for(lat: float, lon: float):
    """Zwraca strefę, w której leży punkt, albo None."""
    point = Point(lat, lon)
    for zone, shape in _SHAPES:
        if shape.contains(point):
            return zone
    return None


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Odległość między dwoma punktami na Ziemi w kilometrach."""
    return geodesic((lat1, lon1), (lat2, lon2)).km


def track_length_km(points: list[tuple[float, float, datetime]]) -> float:
    """Długość trasy z odfiltrowaniem szumu GPS.

    points: (lat, lon, czas) posortowane po czasie. Przesunięcia krótsze niż GPS_MIN_STEP_M nie są liczone
    (punkt odniesienia zostaje, więc powolny ruch i tak się sumuje), a skoki z prędkością powyżej
    GPS_MAX_SPEED_KMH są odrzucane jako błędne odczyty.
    """
    total_m, anchor = 0.0, None
    for lat, lon, ts in points:
        if anchor is None:
            anchor = (lat, lon, ts)
            continue
        d_m = geodesic(anchor[:2], (lat, lon)).meters
        if d_m < config.GPS_MIN_STEP_M:
            continue
        dt = (ts - anchor[2]).total_seconds()
        if dt <= 0 or d_m / dt * 3.6 > config.GPS_MAX_SPEED_KMH:
            continue
        total_m += d_m
        anchor = (lat, lon, ts)
    return total_m / 1000
