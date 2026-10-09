"""Połączenie z brokerem MQTT: odbiór telemetrii i wysyłanie komend do pojazdów.

Tematy:
    scooters/<id>/telemetry   pojazd -> backend   (QoS 1)
    scooters/<id>/command     backend -> pojazd   {"action": "unlock" | "lock"}
"""
import json
import logging
from typing import Callable

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from . import config
from .schemas import TelemetryIn

log = logging.getLogger("mqtt")
TELEMETRY_TOPIC = "scooters/+/telemetry"

_client: mqtt.Client | None = None
_on_telemetry: Callable[[TelemetryIn], object] | None = None
connected = False


def _handle_connect(client, userdata, flags, reason_code, properties=None):
    global connected
    if reason_code.is_failure:
        log.error("Broker MQTT odrzucił połączenie: %s (sprawdź MQTT_USERNAME/MQTT_PASSWORD)", reason_code)
        return
    connected = True
    log.info("Połączono z brokerem MQTT (%s, zachowana sesja: %s)", reason_code, flags.session_present)
    client.subscribe(TELEMETRY_TOPIC, qos=1)   # ponownie po każdym połączeniu


def _handle_disconnect(client, userdata, flags, reason_code, properties=None):
    global connected
    connected = False
    log.warning("Rozłączono z brokerem MQTT (%s)", reason_code)


def _handle_message(client, userdata, msg):
    """Wątek sieciowy paho – tylko walidacja i przekazanie dalej. Zapis do bazy robi inny wątek,
    żeby wolna baza nie blokowała odbioru (i podtrzymywania połączenia)."""
    try:
        message = TelemetryIn.model_validate_json(msg.payload)
    except ValidationError as exc:
        log.warning("Odrzucono wiadomość z %s: %s", msg.topic, exc.errors()[:1])
        return

    topic_scooter_id = msg.topic.split("/")[1]
    if message.scooter_id != topic_scooter_id:
        # pojazd może publikować tylko we własnym temacie (ACL brokera), więc id w treści musi się zgadzać
        log.warning("Odrzucono wiadomość z %s: scooter_id %s niezgodny z tematem", msg.topic, message.scooter_id)
        return

    if _on_telemetry is not None:
        _on_telemetry(message)


def start(on_telemetry: Callable[[TelemetryIn], object]):
    """Łączy się z brokerem; każdą poprawną wiadomość telemetrii przekazuje do on_telemetry."""
    global _client, _on_telemetry
    if not config.MQTT_ENABLED:
        log.info("MQTT wyłączone (MQTT_ENABLED=0) – komendy będą dostarczane w odpowiedziach HTTP")
        return
    _on_telemetry = on_telemetry
    # clean_session=False: broker pamięta subskrypcję i przechowuje wiadomości QoS 1 na czas przerwy w połączeniu
    _client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=config.MQTT_CLIENT_ID, clean_session=False)
    if config.MQTT_USERNAME:
        _client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)
    _client.on_connect = _handle_connect
    _client.on_disconnect = _handle_disconnect
    _client.on_message = _handle_message
    _client.reconnect_delay_set(min_delay=1, max_delay=10)
    _client.connect_async(config.MQTT_HOST, config.MQTT_PORT, keepalive=30)
    _client.loop_start()


def stop():
    global _client, connected
    if _client is not None:
        _client.disconnect()
        _client.loop_stop()
    _client = None
    connected = False


def publish_command(scooter_id: str, action: str) -> bool:
    """Wysyła komendę do pojazdu. Zwraca False, gdy MQTT jest wyłączone.

    Przy chwilowym braku połączenia paho przechowa wiadomość QoS 1 i wyśle ją po ponownym połączeniu.
    """
    if _client is None:
        return False
    _client.publish(f"scooters/{scooter_id}/command", json.dumps({"action": action}), qos=1)
    return True
