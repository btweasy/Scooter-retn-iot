"""Generator obciążenia: flota "urządzeń" wysyłających telemetrię w stałym tempie.

W odróżnieniu od emulatora (który symuluje jazdy i wypożyczenia) tu liczy się tylko ruch: każde urządzenie
co `interval_s` wysyła jeden pomiar z `sent_at` i kolejnym `seq`. Generator pamięta, ile wiadomości
wysłał, więc straty można policzyć dokładnie: wysłane − zapisane w bazie.

Wysyłanie jest rozłożone równomiernie w czasie (każde urządzenie ma własne przesunięcie fazy) –
prawdziwe pojazdy nie nadają wszystkie w tej samej milisekundzie.
"""
import http.client
import json
import random
import threading
import time
from dataclasses import dataclass, field

import paho.mqtt.client as mqtt

CITY = "Benchmark"
CENTRE = (54.4800, 17.0300)   # Słupsk, poza strefami zakazu parkowania


@dataclass
class SendStats:
    generated: int = 0          # wiadomości, które urządzenia chciały wysłać
    rejected_by_client: int = 0  # QoS 0 bez połączenia: biblioteka od razu je wyrzuca
    http_errors: int = 0
    first_sent_at: float = 0.0
    last_sent_at: float = 0.0
    per_scooter_seq: dict = field(default_factory=dict)


def make_message(scooter_id: str, seq: int, rng: random.Random) -> dict:
    return {
        "scooter_id": scooter_id,
        "city": CITY,
        "lat": CENTRE[0] + rng.uniform(-0.002, 0.002),
        "lon": CENTRE[1] + rng.uniform(-0.002, 0.002),
        "battery": 80.0,
        "speed": 0.0,
        "locked": True,
        "sent_at": time.time(),
        "seq": seq,
    }


class LoadGenerator:
    """transport: 'mqtt' (QoS 0/1) albo 'http' (POST /api/telemetry/batch co `http_batch_s`)."""

    def __init__(self, transport: str, scooters: int, interval_s: float, qos: int = 1,
                 mqtt_host: str = "127.0.0.1", mqtt_port: int = 1883, connections: int = 20,
                 api_host: str = "127.0.0.1", api_port: int = 8000, http_batch_s: float = 0.1,
                 run_id: str = "run", seed: int = 1):
        self.transport = transport
        self.scooter_ids = [f"BM-{i:05d}" for i in range(scooters)]
        self.interval_s = interval_s
        self.qos = qos
        self.mqtt_host = mqtt_host
        self.mqtt_port = mqtt_port
        self.connections = max(1, min(connections, scooters))
        self.api_host = api_host
        self.api_port = api_port
        self.http_batch_s = http_batch_s
        self.run_id = run_id
        self.rng = random.Random(seed)
        self.stats = SendStats()
        self.clients: list[mqtt.Client] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- MQTT

    def connect(self, timeout_s: float = 15):
        if self.transport != "mqtt":
            return
        for number in range(self.connections):
            # clean_session=False: po zerwaniu połączenia niepotwierdzone wiadomości QoS 1 są wysyłane ponownie
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                 client_id=f"bench-{self.run_id}-{number}", clean_session=False)
            client.reconnect_delay_set(min_delay=1, max_delay=2)
            client.connect_async(self.mqtt_host, self.mqtt_port, keepalive=10)
            client.loop_start()
            self.clients.append(client)
        deadline = time.monotonic() + timeout_s
        while not all(client.is_connected() for client in self.clients):
            if time.monotonic() > deadline:
                raise RuntimeError("Generator nie połączył się z brokerem MQTT")
            time.sleep(0.05)

    def disconnect(self):
        for client in self.clients:
            client.disconnect()
            client.loop_stop()
        self.clients.clear()

    def pending_qos1(self) -> int:
        """Ile wiadomości QoS 1 czeka jeszcze na potwierdzenie brokera (po zakończeniu wysyłania)."""
        total = 0
        for client in self.clients:
            total += len(getattr(client, "_out_messages", {}))
        return total

    # ---------------------------------------------------------------- pętla wysyłania

    def start(self, duration_s: float):
        self._thread = threading.Thread(target=self._run, args=(duration_s,), name="load", daemon=True)
        self._thread.start()

    def join(self):
        if self._thread is not None:
            self._thread.join()

    def _run(self, duration_s: float):
        start = time.monotonic()
        end = start + duration_s
        # każde urządzenie startuje z innym przesunięciem w obrębie okresu
        next_send = {sid: start + self.rng.uniform(0, self.interval_s) for sid in self.scooter_ids}
        seq = {sid: 0 for sid in self.scooter_ids}
        client_of = {sid: index % self.connections for index, sid in enumerate(self.scooter_ids)}
        http_conn = None
        http_buffer: list[dict] = []
        last_http_flush = start
        self.stats.first_sent_at = time.time()

        while not self._stop.is_set():
            now = time.monotonic()
            if now >= end:
                break
            for sid in self.scooter_ids:
                if next_send[sid] > now:
                    continue
                next_send[sid] += self.interval_s
                seq[sid] += 1
                message = make_message(sid, seq[sid], self.rng)
                self.stats.generated += 1
                if self.transport == "mqtt":
                    self._publish(self.clients[client_of[sid]], message)
                else:
                    http_buffer.append(message)

            if self.transport == "http" and http_buffer and now - last_http_flush >= self.http_batch_s:
                http_conn = self._post_batch(http_conn, http_buffer)
                http_buffer = []
                last_http_flush = now
            time.sleep(0.002)

        if self.transport == "http" and http_buffer:
            self._post_batch(http_conn, http_buffer)
        self.stats.last_sent_at = time.time()
        self.stats.per_scooter_seq = dict(seq)

    def _publish(self, client: mqtt.Client, message: dict):
        topic = f"scooters/{message['scooter_id']}/telemetry"
        info = client.publish(topic, json.dumps(message), qos=self.qos)
        # Bez połączenia publish zwraca MQTT_ERR_NO_CONN. Dla QoS 0 wiadomość przepada od razu,
        # dla QoS 1 zostaje w kolejce klienta i zostanie wysłana po ponownym połączeniu.
        if info.rc != mqtt.MQTT_ERR_SUCCESS and self.qos == 0:
            self.stats.rejected_by_client += 1

    def _post_batch(self, conn, messages):
        body = json.dumps({"items": messages})
        for _ in range(2):   # jedno ponowienie po zerwanym połączeniu keep-alive
            try:
                if conn is None:
                    conn = http.client.HTTPConnection(self.api_host, self.api_port, timeout=30)
                conn.request("POST", "/api/telemetry/batch", body, {"Content-Type": "application/json"})
                response = conn.getresponse()
                response.read()
                if response.status != 202:
                    self.stats.http_errors += len(messages)
                return conn
            except (OSError, http.client.HTTPException):
                conn = None
        self.stats.http_errors += len(messages)
        return conn
