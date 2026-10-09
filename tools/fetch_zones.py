"""Pobiera kształty stref zakazu parkowania z OpenStreetMap (Nominatim) i wypisuje je w formacie app/geo.py.

Uruchamiane raz, ręcznie (wynik wklejony do backend/app/zones_data.py) – aplikacja nie potrzebuje internetu.
    py tools/fetch_zones.py > backend/app/zones_data.py

Ulice (linie) są "pogrubiane" do pasa o zadanej szerokości, a długie obrysy upraszczane
(Shapely simplify), żeby plik był czytelny. Dane: © współtwórcy OpenStreetMap, licencja ODbL.
"""
import json
import math
import time
import urllib.parse
import urllib.request

from shapely.geometry import shape
from shapely.ops import transform, unary_union

# (id, miasto, nazwa w aplikacji, zapytanie do Nominatim, szerokość pasa w m dla ulic)
PLACES = [
    ("slupsk-park-waldorffa", "Słupsk", "Park im. Jerzego Waldorffa", "Park im. Jerzego Waldorffa, Słupsk", 0),
    ("slupsk-stary-rynek", "Słupsk", "Stary Rynek", "Stary Rynek, Słupsk", 24),
    ("slupsk-wojska-polskiego", "Słupsk", "Deptak – ul. Wojska Polskiego", "Wojska Polskiego, Słupsk", 22),
    ("slupsk-park-kultury", "Słupsk", "Park Kultury i Wypoczynku (nad Słupią)", "Park Kultury i Wypoczynku, Słupsk", 0),
    ("gdansk-dlugi-targ", "Gdańsk", "Długi Targ", "Długi Targ, Gdańsk", 30),
    ("gdansk-dluga", "Gdańsk", "ul. Długa (Droga Królewska)", "Długa, Śródmieście, Gdańsk", 22),
    ("gdansk-dlugie-pobrzeze", "Gdańsk", "Długie Pobrzeże (nabrzeże Motławy)", "Długie Pobrzeże, Gdańsk", 26),
    ("warszawa-rynek", "Warszawa", "Rynek Starego Miasta", "Rynek Starego Miasta, Warszawa", 0),
    ("warszawa-plac-zamkowy", "Warszawa", "Plac Zamkowy", "Plac Zamkowy, Warszawa", 0),
    ("warszawa-ogrod-saski", "Warszawa", "Ogród Saski", "Ogród Saski, Warszawa", 0),
]
SIMPLIFY_DEG = 0.00003      # ok. 3 m – mniejsze szczegóły obrysu pomijamy
METERS_PER_DEG_LAT = 111_320


def nominatim(query: str) -> list:
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": query, "format": "jsonv2", "polygon_geojson": 1, "limit": 10})
    request = urllib.request.Request(url, headers={"User-Agent": "scooter-iot-thesis/1.0 (student project)"})
    time.sleep(1.1)   # zasady Nominatim: najwyżej 1 zapytanie na sekundę
    return json.loads(urllib.request.urlopen(request, timeout=30).read())


def to_polygon(results: list, width_m: float):
    """Pierwszy wynik z obszarem; dla ulicy – wszystkie odcinki o tej nazwie pogrubione do pasa."""
    for result in results:
        geometry = shape(result["geojson"])
        if geometry.geom_type in ("Polygon", "MultiPolygon") and width_m == 0:
            return largest(geometry)
    if width_m == 0:
        return None
    # ulica składa się w OSM z kilku odcinków – pogrubiamy każdy i łączymy w jeden pas
    lines = [shape(r["geojson"]) for r in results if r["geojson"]["type"] in ("LineString", "MultiLineString", "Polygon")]
    if not lines:
        return None
    # pogrubiamy w metrach, nie w stopniach: na szerokości Polski 1° długości to tylko ~65 km, a 1° szerokości ~111 km
    meters_per_deg_lon = METERS_PER_DEG_LAT * math.cos(math.radians(lines[0].centroid.y))
    to_meters = lambda lon, lat, z=None: (lon * meters_per_deg_lon, lat * METERS_PER_DEG_LAT)
    to_degrees = lambda x, y, z=None: (x / meters_per_deg_lon, y / METERS_PER_DEG_LAT)
    strips = [transform(to_meters, line).buffer(width_m / 2, cap_style="flat") for line in lines]
    joined = unary_union(strips).buffer(6).buffer(-6)   # skleja odcinki rozdzielone szczelinami do ~12 m
    return largest(transform(to_degrees, joined))


def largest(geometry):
    if geometry.geom_type == "MultiPolygon":
        return max(geometry.geoms, key=lambda part: part.area)
    return geometry


def main():
    print('"""Strefy zakazu parkowania – kształty z OpenStreetMap (© współtwórcy OSM, ODbL).')
    print("Wygenerowane przez tools/fetch_zones.py. Współrzędne w kolejności [lat, lon].\"\"\"")
    print()
    print("NO_PARKING_ZONES = [")
    for zone_id, city, name, query, width_m in PLACES:
        polygon = to_polygon(nominatim(query), width_m)
        if polygon is None:
            print(f"    # nie znaleziono: {query}")
            continue
        polygon = polygon.simplify(SIMPLIFY_DEG, preserve_topology=True)
        points = [[round(lat, 6), round(lon, 6)] for lon, lat in polygon.exterior.coords[:-1]]   # GeoJSON: lon, lat
        print(f'    {{"id": "{zone_id}", "city": "{city}", "name": "{name}", "polygon": [')
        for i in range(0, len(points), 4):
            print("        " + ", ".join(f"[{lat}, {lon}]" for lat, lon in points[i:i + 4]) + ",")
        print("    ]},")
    print("]")


if __name__ == "__main__":
    main()
