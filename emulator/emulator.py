#!/usr/bin/env python3
"""Emulator floty hulajnóg elektrycznych.

Każda hulajnoga co `--interval` sekund wysyła telemetrię (GPS, bateria, prędkość, blokada, czas wysłania
`sent_at`, numer `seq`) przez MQTT (domyślnie) lub HTTP i wykonuje komendy backendu {"action": "unlock"|"lock"}.

"Inni użytkownicy" (jazdy tła) wypożyczają hulajnogi przez to samo API co operator:
POST /api/rentals/start i /end. Dzięki temu statystyki, rozliczenia i alerty są spójne.

Przykłady:
  python emulator.py --transport http --api-url http://127.0.0.1:8000
  python emulator.py --mqtt-host 192.168.1.10 --scooters 1 --prefix PI --origin 54.4641,17.0285 --city-name Lab
"""
import argparse
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request

from geopy.distance import geodesic  # odległości i przesuwanie punktu na mapie

CITIES = {  # nazwa: (kod, lat, lon) – środek miasta
    "Słupsk": ("SLP", 54.4641, 17.0285),
    "Gdańsk": ("GDA", 54.3485, 18.6530),
    "Warszawa": ("WAW", 52.2497, 21.0122),
}
UNLOCK_WAIT_S = 20        # tyle czekamy na komendę unlock po rozpoczęciu własnego wypożyczenia
RIDE_SECONDS = (60, 150)  # długość jazdy tła
BREAK_SECONDS = (60, 300) # przerwa między jazdami tła – ok. 1/3 floty w ruchu, reszta do wypożyczenia
MIN_BATTERY_TO_RIDE = 20  # emulowany użytkownik nie bierze słabiej naładowanej hulajnogi
BATTERY_USE_PER_S = 0.03  # % baterii na sekundę jazdy (czasu symulacji)


class Scooter:
    """Jedna hulajnoga: położenie, bateria, blokada i plan jazdy emulowanego użytkownika."""

    def __init__(self, scooter_id, city, centre, radius_m, rng, now):
        self.id = scooter_id
        self.city = city
        self.centre = centre
        self.radius = radius_m
        self.rng = rng

        # losowy punkt w pobliżu środka miasta: losowa odległość w losowym kierunku
        start = geodesic(meters=rng.uniform(0, radius_m)).destination(centre, rng.uniform(0, 360))
        self.lat = start.latitude
        self.lon = start.longitude
        if rng.random() < 0.15:
            self.battery = rng.uniform(6, 18)    # część floty rozładowana – żeby było co pokazać
        else:
            self.battery = rng.uniform(40, 100)
        self.heading = rng.uniform(0, 360)       # kierunek jazdy w stopniach (0 = północ)
        self.speed = 0.0
        self.locked = True
        self.seq = 0

        # jazdy tła
        self.rental_id = None          # własne wypożyczenie emulatora (None = brak)
        self.rental_started = 0.0
        self.ride_until = 0.0
        self.next_ride = now + rng.uniform(5, BREAK_SECONDS[1])

    # ---- komendy z backendu
    def command(self, action, now):
        if action == "unlock":
            self.locked = False
        elif action == "lock":
            self.locked = True
            self.speed = 0.0
            self.rental_id = None
            self.next_ride = now + self.rng.uniform(*BREAK_SECONDS)

    # ---- plan jazd tła (wykonuje go pętla główna przez API)
    def wants_ride(self, now):
        return (self.locked and self.rental_id is None
                and now >= self.next_ride and self.battery >= MIN_BATTERY_TO_RIDE)

    def wants_end(self, now):
        if self.rental_id is None:
            return False
        if self.locked:
            # komenda unlock nie dotarła – rezygnujemy, zamiast trzymać pojazd na zawsze
            return now >= self.rental_started + UNLOCK_WAIT_S
        return now >= self.ride_until

    def ride_started(self, rental_id, now):
        self.rental_id = rental_id
        self.rental_started = now
        self.ride_until = now + self.rng.uniform(*RIDE_SECONDS)

    def ride_refused(self, now):
        self.next_ride = now + self.rng.uniform(*BREAK_SECONDS)

    def ride_on(self, now, seconds):
        """Nie wolno zakończyć jazdy tutaj (strefa zakazu parkowania) – jedziemy dalej."""
        self.ride_until = now + seconds

    # ---- fizyka
    def step(self, sim_seconds):
        if self.locked or self.battery <= 0:
            # rozładowana hulajnoga stoi, ale NIE blokuje się sama – o blokadzie decyduje backend
            self.speed = 0.0
            return
        self.speed = self.rng.uniform(12, 22)
        self.heading = (self.heading + self.rng.uniform(-20, 20)) % 360    # lekki losowy skręt
        if geodesic(self.centre, (self.lat, self.lon)).meters > self.radius:
            self.heading = (self.heading + 180) % 360                       # poza obszarem miasta – zawróć
        distance_m = self.speed / 3.6 * sim_seconds
        new_position = geodesic(meters=distance_m).destination((self.lat, self.lon), self.heading)
        self.lat = new_position.latitude
        self.lon = new_position.longitude
        self.battery = max(0.0, self.battery - BATTERY_USE_PER_S * sim_seconds)

    def telemetry(self):
        self.seq += 1
        return {
            "scooter_id": self.id,
            "city": self.city,
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "battery": round(self.battery, 1),
            "speed": round(self.speed, 1),
            "locked": self.locked,
            "sent_at": round(time.time(), 3),
            "seq": self.seq,
        }


class Api:
    """Minimalny klient REST backendu (tylko biblioteka standardowa)."""

    def __init__(self, base_url, token=None, timeout=5):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def get(self, path):
        return self._send(urllib.request.Request(self.base_url + path, headers=self.headers))

    def post(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        return self._send(urllib.request.Request(self.base_url + path, data, self.headers, method="POST"))

    def _send(self, request):
        """Zwraca (kod HTTP, JSON). Błąd sieci = wyjątek, błąd HTTP = zwykła odpowiedź z kodem."""
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")


class ErrorLog:
    """Wypisuje błąd najwyżej raz na `every` sekund (z licznikiem), żeby nie zalać konsoli."""

    def __init__(self, every=30.0):
        self.every = every
        self.last_printed = -1e9
        self.count = 0

    def __call__(self, what, error):
        self.count += 1
        now = time.monotonic()
        if now - self.last_printed >= self.every:
            print(f"Błąd ({what}): {error} [łącznie błędów: {self.count}]")
            self.last_printed = now


# ---------------------------------------------------------------- flota

def parse_places(cities_arg, origin=None, city_name="Lab"):
    """Miasta z linii poleceń -> {nazwa: (kod, lat, lon)}. --origin = własny punkt (np. Raspberry Pi)."""
    if origin:
        lat, lon = map(float, origin.split(","))
        return {city_name: ("LAB", lat, lon)}
    names = [name.strip() for name in cities_arg.split(",") if name.strip()]
    unknown = [name for name in names if name not in CITIES]
    if unknown:
        raise SystemExit(f"Nieznane miasta: {', '.join(unknown)} (dostępne: {', '.join(CITIES)})")
    return {name: CITIES[name] for name in names}


def create_fleet(places, per_city, prefix, radius, rng):
    fleet = {}
    now = time.monotonic()
    for city, (code, lat, lon) in places.items():
        for number in range(1, per_city + 1):
            scooter = Scooter(f"{prefix}-{code}-{number:03d}", city, (lat, lon), radius, rng, now)
            fleet[scooter.id] = scooter
    return fleet


# ---------------------------------------------------------------- jazdy tła

def close_stale_rentals(fleet, api):
    """Zamyka wypożyczenia tła po poprzednim uruchomieniu emulatora (nowy proces o nich nie wie,
    a pojazdy byłyby "wypożyczone" na zawsze). Zwraca liczbę zamkniętych."""
    code, open_rentals = api.get("/api/rentals?status=open&limit=500")
    if code != 200:
        raise RuntimeError(f"HTTP {code}")
    stale = [rental for rental in open_rentals
             if rental["scooter_id"] in fleet and rental["user_id"] == f"sim-{rental['scooter_id']}"]
    for rental in stale:
        api.post(f"/api/rentals/{rental['id']}/end", {"force": True})
    if stale:
        print(f"Zamknięto {len(stale)} wypożyczeń tła z poprzedniego uruchomienia")
    return len(stale)


def try_start_ride(scooter, api, now):
    code, body = api.post("/api/rentals/start", {"scooter_id": scooter.id, "user_id": f"sim-{scooter.id}"})
    if code == 201:
        scooter.ride_started(body["id"], now)   # komenda unlock przyjdzie z backendu
    else:
        scooter.ride_refused(now)               # np. pojazd jeszcze nieznany backendowi, offline, zajęty


def try_end_ride(scooter, api, now):
    code, body = api.post(f"/api/rentals/{scooter.rental_id}/end")
    if code == 409 and body.get("code") == "no_parking_zone":
        scooter.ride_on(now, 15)    # jak prawdziwy użytkownik: odjeżdża ze strefy i próbuje ponownie
    elif code in (200, 404, 409):   # 404/409: zakończone już np. przez operatora
        scooter.rental_id = None    # blokadę przyniesie komenda lock z backendu


def run_background_rides(fleet, api, now, err):
    for scooter in fleet.values():
        try:
            if scooter.wants_ride(now):
                try_start_ride(scooter, api, now)
            elif scooter.wants_end(now):
                try_end_ride(scooter, api, now)
        except Exception as error:   # backend nieosiągalny – spróbujemy w następnym cyklu
            err("API wypożyczeń", error)
            return


# ---------------------------------------------------------------- transport

def connect_mqtt(args, fleet, fleet_lock):
    import paho.mqtt.client as mqtt

    def on_connect(client, userdata, flags, reason_code, properties=None):
        print("MQTT połączono:", reason_code)
        if not reason_code.is_failure:
            client.subscribe("scooters/+/command", qos=1)

    def on_message(client, userdata, msg):
        try:
            scooter_id = msg.topic.split("/")[1]
            action = json.loads(msg.payload)["action"]
        except (ValueError, KeyError, IndexError, TypeError):
            return
        if scooter_id in fleet:
            with fleet_lock:
                fleet[scooter_id].command(action, time.monotonic())
            print(f"komenda {action} -> {scooter_id}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"emulator-{args.prefix}-{os.getpid()}")
    if args.mqtt_username:
        client.username_pw_set(args.mqtt_username, args.mqtt_password)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(1, 10)
    client.connect_async(args.mqtt_host, args.mqtt_port, keepalive=30)
    client.loop_start()
    return client


def send_over_mqtt(client, messages, qos):
    for message in messages:
        client.publish(f"scooters/{message['scooter_id']}/telemetry", json.dumps(message), qos=qos)


def send_over_http(api, messages, fleet, fleet_lock, now):
    """Jedno żądanie na cykl dla całej floty. W odpowiedzi – komendy czekające na pojazdy."""
    code, response = api.post("/api/telemetry/batch", {"items": messages})
    if code != 202:
        raise RuntimeError(f"HTTP {code}: {response}")
    with fleet_lock:
        for scooter_id, actions in response.get("commands", {}).items():
            for action in actions:
                if scooter_id in fleet:
                    fleet[scooter_id].command(action, now)
                    print(f"komenda {action} -> {scooter_id}")


# ---------------------------------------------------------------- główna pętla

def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--transport", choices=["mqtt", "http"], default="mqtt")
    p.add_argument("--mqtt-host", default=os.getenv("MQTT_HOST", "localhost"))
    p.add_argument("--mqtt-port", type=int, default=int(os.getenv("MQTT_PORT", "1883")))
    p.add_argument("--mqtt-username", default=os.getenv("MQTT_USERNAME"))
    p.add_argument("--mqtt-password", default=os.getenv("MQTT_PASSWORD"))
    p.add_argument("--qos", type=int, choices=[0, 1], default=1)
    # 127.0.0.1 zamiast localhost: na Windows "localhost" najpierw próbuje IPv6 i każde żądanie traci ~2 s
    p.add_argument("--api-url", default=os.getenv("API_URL", "http://127.0.0.1:8000"))
    p.add_argument("--api-token", default=os.getenv("API_TOKEN"))
    p.add_argument("--scooters", type=int, default=30, help="liczba hulajnóg na miasto")
    p.add_argument("--cities", default=",".join(CITIES), help="lista miast oddzielona przecinkami")
    p.add_argument("--origin", help="własny punkt startu 'lat,lon' (np. węzeł Raspberry Pi)")
    p.add_argument("--city-name", default="Lab")
    p.add_argument("--prefix", default="SC")
    p.add_argument("--radius", type=float, default=1500, help="promień obszaru w metrach")
    p.add_argument("--interval", type=float, default=2.0, help="okres wysyłania [s]")
    p.add_argument("--speedup", type=float, default=4.0, help="przyspieszenie ruchu (ruch widoczny na mapie)")
    p.add_argument("--no-background-rides", action="store_true", help="bez jazd tła (tylko komendy z backendu)")
    p.add_argument("--seed", type=int, default=None)
    return p.parse_args()


def main():
    args = parse_args()
    fleet = create_fleet(parse_places(args.cities, args.origin, args.city_name),
                         args.scooters, args.prefix, args.radius, random.Random(args.seed))
    fleet_lock = threading.Lock()   # stan floty zmieniają: pętla główna i wątek MQTT (komendy)
    api = Api(args.api_url, args.api_token)
    err = ErrorLog()
    background_rides = not args.no_background_rides
    print(f"Emulator: {len(fleet)} hulajnóg, transport={args.transport}, okres={args.interval}s")

    mqtt_client = connect_mqtt(args, fleet, fleet_lock) if args.transport == "mqtt" else None
    stale_rentals_closed = False
    sent = 0
    next_tick = time.monotonic()
    last_report = time.monotonic()

    try:
        while True:
            now = time.monotonic()

            # 1. ruch i odczyt czujników
            with fleet_lock:
                for scooter in fleet.values():
                    scooter.step(args.interval * args.speedup)
                messages = [scooter.telemetry() for scooter in fleet.values()]

            # 2. wysyłka telemetrii
            try:
                if mqtt_client is not None:
                    send_over_mqtt(mqtt_client, messages, args.qos)
                else:
                    send_over_http(api, messages, fleet, fleet_lock, now)
                sent += len(messages)
            except Exception as error:   # brak połączenia nie zatrzymuje emulatora
                err("telemetria", error)

            # 3. emulowani użytkownicy
            if background_rides:
                if not stale_rentals_closed:
                    try:   # raz, przy pierwszym kontakcie z backendem
                        close_stale_rentals(fleet, api)
                        stale_rentals_closed = True
                    except Exception as error:
                        err("sprzątanie starych wypożyczeń", error)
                run_background_rides(fleet, api, now, err)

            if now - last_report >= 20:
                print(f"wysłano {sent} wiadomości")
                last_report = now

            # równe odstępy między cyklami, niezależnie od czasu wysyłki
            next_tick += args.interval
            time.sleep(max(0.0, next_tick - time.monotonic()))
    except KeyboardInterrupt:
        print("Zatrzymano.")


if __name__ == "__main__":
    main()
