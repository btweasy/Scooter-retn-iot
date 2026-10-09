from datetime import datetime, timedelta

from geopy.distance import geodesic

from shapely.geometry import Polygon

from app.geo import ZONES, distance_km, track_length_km, zone_for

T0 = datetime(2026, 1, 1, 12, 0, 0)


def test_zone_for_returns_zone_or_none():
    assert zone_for(54.4591, 17.0359)["id"] == "slupsk-park-kultury"   # w parku nad Słupią
    assert zone_for(54.4800, 17.0285) is None                           # Słupsk, poza strefami
    assert zone_for(0.0, 0.0) is None


def test_zones_are_valid_real_shapes():
    for zone in ZONES:
        polygon = Polygon(zone["polygon"])
        assert polygon.is_valid, zone["id"]
        assert len(zone["polygon"]) >= 4, zone["id"]               # kształt miejsca, nie punkt
        lat, lon = zone["polygon"][0]
        assert 49 < lat < 55 and 14 < lon < 25, zone["id"]         # [lat, lon] w Polsce – nie zamienione
    assert {zone["city"] for zone in ZONES} == {"Słupsk", "Gdańsk", "Warszawa"}


def test_distance_one_degree_latitude():
    assert abs(distance_km(0, 0, 1, 0) - 110.57) < 0.5      # ~110,6 km na stopień szerokości przy równiku


def north(m):
    p = geodesic(meters=abs(m)).destination((54.48, 17.03), 0 if m >= 0 else 180)
    return p.latitude, p.longitude


def test_track_ignores_gps_jitter_when_standing():
    pts = [(*north(m), T0 + timedelta(seconds=2 * i)) for i, m in enumerate([0, 1.5, -1, 2, 0.5, -1.5])]
    assert track_length_km(pts) == 0


def test_track_counts_slow_movement():
    pts = [(*north(2 * i), T0 + timedelta(seconds=2 * i)) for i in range(11)]   # 2 m co 2 s, razem 20 m
    assert abs(track_length_km(pts) - 0.020) < 0.002


def test_track_rejects_outlier_jump():
    pts = [(*north(0), T0), (*north(10), T0 + timedelta(seconds=2)),
           (*north(2000), T0 + timedelta(seconds=4)),          # 1 km/s – błędny odczyt
           (*north(20), T0 + timedelta(seconds=6))]
    assert abs(track_length_km(pts) - 0.020) < 0.001
