"""Ustawienia aplikacji. Każdą wartość można zmienić zmienną środowiskową o tej samej nazwie."""
import os
from decimal import Decimal


def env_bool(name: str, default: str) -> bool:
    return os.getenv(name, default) == "1"


# --- baza danych ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./scooters.db")
AUTO_MIGRATE = env_bool("AUTO_MIGRATE", "1")   # przy starcie: alembic upgrade head

# --- MQTT ---
MQTT_ENABLED = env_bool("MQTT_ENABLED", "1")
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "scooter-backend")   # stały => trwała sesja u brokera
MQTT_USERNAME = os.getenv("MQTT_USERNAME") or None
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD") or None

# --- bezpieczeństwo ---
API_TOKEN = os.getenv("API_TOKEN") or None   # pusty => autoryzacja wyłączona (tryb deweloperski)

# --- stan pojazdu i alerty ---
OFFLINE_AFTER_S = int(os.getenv("OFFLINE_AFTER_S", "30"))          # brak telemetrii dłużej => offline
LOW_BATTERY = float(os.getenv("LOW_BATTERY", "15"))                # % – poniżej: "niski akumulator"
LOW_BATTERY_CLEAR = float(os.getenv("LOW_BATTERY_CLEAR", "20"))    # % – histereza: nowy alert dopiero powyżej
FULL_RANGE_KM = float(os.getenv("FULL_RANGE_KM", "45"))            # zasięg przy 100% baterii
UNAUTHORIZED_GRACE_S = int(os.getenv("UNAUTHORIZED_GRACE_S", "15"))  # czas na dotarcie "lock" po końcu jazdy

# --- cennik (zapisywany w wypożyczeniu w chwili startu) ---
UNLOCK_FEE = Decimal(os.getenv("UNLOCK_FEE", "3.00"))                  # PLN za odblokowanie
PRICE_PER_MIN = Decimal(os.getenv("PRICE_PER_MIN", "0.60"))            # PLN za rozpoczętą minutę jazdy
PAUSE_PRICE_PER_MIN = Decimal(os.getenv("PAUSE_PRICE_PER_MIN", "0.30"))  # PLN za rozpoczętą minutę pauzy

# --- zasady wypożyczeń ---
RESERVATION_MINUTES = int(os.getenv("RESERVATION_MINUTES", "10"))      # bezpłatna rezerwacja
RENTAL_LOCKED_TIMEOUT_S = int(os.getenv("RENTAL_LOCKED_TIMEOUT_S", "60"))
#   ↑ jazda w toku, a pojazd zablokowany (i online) dłużej => zamknięcie automatyczne
#     (zaginiona komenda unlock albo restart urządzenia)

# --- dystans: filtr szumu GPS ---
GPS_MIN_STEP_M = float(os.getenv("GPS_MIN_STEP_M", "3"))          # krótsze przesunięcia = szum
GPS_MAX_SPEED_KMH = float(os.getenv("GPS_MAX_SPEED_KMH", "60"))   # szybsze "skoki" = błędny odczyt

# --- przetwarzanie w tle ---
INGEST_BATCH_SIZE = int(os.getenv("INGEST_BATCH_SIZE", "500"))         # maks. wiadomości w jednej transakcji
INGEST_FLUSH_S = float(os.getenv("INGEST_FLUSH_S", "0.2"))             # maks. czas zbierania partii
TELEMETRY_RETENTION_H = float(os.getenv("TELEMETRY_RETENTION_H", "48"))  # 0 => bez czyszczenia
STREAM_INTERVAL_S = float(os.getenv("STREAM_INTERVAL_S", "2"))         # odświeżanie panelu (SSE)
