"""Obserwator na brokerze: niezależny subskrybent, który zapisuje, które wiadomości dotarły do Mosquitto.

Dzięki niemu straty można rozdzielić na dwie części:
  * zginęły w sieci     – urządzenie wysłało, broker nie dostał (zerwane łącze, QoS 0 bez połączenia),
  * zginęły w backendzie – broker dostał, w bazie ich nie ma (awaria procesu, odrzucenie jako "spóźniona").

Obserwator łączy się z brokerem bezpośrednio (z pominięciem psującego łącze pośrednika) i ma trwałą sesję,
więc po restarcie brokera dostaje zaległe wiadomości QoS 1. Liczy też wiadomości, które przyszły
w zmienionej kolejności (mniejszy `seq` po większym) – backend odrzuca je jako spóźnione.
"""
import json
import threading
import time

import paho.mqtt.client as mqtt


class BrokerObserver:
    def __init__(self, host: str, port: int, client_id: str):
        self.seen: set[tuple[str, int]] = set()
        self.duplicates = 0     # ta sama wiadomość drugi raz (QoS 1 = "co najmniej raz")
        self.out_of_order = 0   # starszy seq po nowszym w obrębie jednego pojazdu
        self._max_seq: dict[str, int] = {}
        self._lock = threading.Lock()
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, clean_session=False)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=2)
        self._host = host
        self._port = port

    def start(self, timeout_s: float = 15):
        self._client.connect_async(self._host, self._port, keepalive=10)
        self._client.loop_start()
        deadline = time.monotonic() + timeout_s
        while not self._client.is_connected():
            if time.monotonic() > deadline:
                raise RuntimeError("Obserwator nie połączył się z brokerem")
            time.sleep(0.05)

    def stop(self):
        self._client.disconnect()
        self._client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if not reason_code.is_failure:
            client.subscribe("scooters/+/telemetry", qos=1)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload)
            key = (payload["scooter_id"], int(payload["seq"]))
        except (ValueError, KeyError, TypeError):
            return
        with self._lock:
            if key in self.seen:
                self.duplicates += 1
                return
            self.seen.add(key)
            scooter_id, seq = key
            if seq < self._max_seq.get(scooter_id, 0):
                self.out_of_order += 1
            else:
                self._max_seq[scooter_id] = seq
