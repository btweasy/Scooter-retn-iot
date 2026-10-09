"""Metryki przyjmowania telemetrii w pamięci procesu (do badań opóźnień, przepustowości i strat).

Opóźnienie = czas odbioru na serwerze − `sent_at` z urządzenia, więc wymaga zsynchronizowanych zegarów (NTP).
"""
import math
import threading
import time
from collections import deque


class Metrics:
    def __init__(self, latency_samples: int = 10000):
        self._lock = threading.Lock()
        self._latency = deque(maxlen=latency_samples)
        self._per_second = {}   # sekunda (int, monotonic) -> liczba przyjętych wiadomości
        self.reset()

    def reset(self):
        with self._lock:
            self.accepted = 0      # zapisane pomiary
            self.duplicates = 0    # odrzucone: duplikat lub starszy niż ostatni (QoS 1 = "co najmniej raz")
            self.lost = 0          # szacowane straty: luki w numeracji `seq`
            self._latency.clear()
            self._per_second.clear()

    def record(self, latency_s: float | None = None, lost_before: int = 0):
        """Przyjęta wiadomość: jej opóźnienie i liczba zgubionych wiadomości przed nią."""
        sec = int(time.monotonic())
        with self._lock:
            self.accepted += 1
            self.lost += lost_before
            self._per_second[sec] = self._per_second.get(sec, 0) + 1
            if len(self._per_second) > 120:
                for old in [s for s in self._per_second if s < sec - 60]:
                    del self._per_second[old]
            if latency_s is not None:
                self._latency.append(latency_s)

    def record_duplicate(self):
        with self._lock:
            self.duplicates += 1

    def rate(self, window: int = 10) -> float:
        """Przyjęte wiadomości na sekundę w ostatnich `window` pełnych sekundach."""
        now = int(time.monotonic())
        with self._lock:
            n = sum(c for s, c in self._per_second.items() if now - window <= s < now)
        return round(n / window, 2)

    def snapshot(self) -> dict:
        with self._lock:
            lat = sorted(self._latency)
            base = {"accepted": self.accepted, "duplicates": self.duplicates, "lost": self.lost}
        if lat:
            p95 = lat[min(len(lat) - 1, math.ceil(0.95 * len(lat)) - 1)]
            latency = {"samples": len(lat), "avg_ms": round(sum(lat) / len(lat) * 1000, 1),
                       "p95_ms": round(p95 * 1000, 1), "max_ms": round(lat[-1] * 1000, 1)}
        else:
            latency = {"samples": 0, "avg_ms": None, "p95_ms": None, "max_ms": None}
        return {**base, "messages_per_s": self.rate(), "latency": latency}


metrics = Metrics()
