"""Wysyłanie komend (lock/unlock) do pojazdów.

Kanał główny: MQTT `scooters/<id>/command`. Gdy MQTT jest wyłączone (tryb HTTP), komendy czekają w kolejce
i są zwracane pojazdowi w odpowiedzi na jego najbliższy `POST /api/telemetry` – dzięki temu nie giną po cichu.
"""
import threading

from . import mqtt_client

_pending: dict[str, list[str]] = {}
_lock = threading.Lock()


def send(scooter_id: str, action: str) -> str:
    """Zwraca kanał dostarczenia: 'mqtt' albo 'http-queue'."""
    if mqtt_client.publish_command(scooter_id, action):
        return "mqtt"
    with _lock:
        _pending.setdefault(scooter_id, []).append(action)
    return "http-queue"


def pop_pending(scooter_id: str) -> list[str]:
    with _lock:
        return _pending.pop(scooter_id, [])


def clear():
    with _lock:
        _pending.clear()
