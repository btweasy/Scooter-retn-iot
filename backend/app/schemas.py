"""Walidacja danych wejściowych (Pydantic). Błędne dane są odrzucane z kodem 422."""
from pydantic import BaseModel, Field


class TelemetryIn(BaseModel):
    """Jeden pomiar wysłany przez pojazd."""
    scooter_id: str = Field(min_length=1, max_length=32)
    city: str = Field(min_length=1, max_length=64)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    battery: float = Field(ge=0, le=100)
    speed: float = Field(ge=0, le=60)
    locked: bool = True
    # Opcjonalne (starsze urządzenia ich nie wysyłają), ale potrzebne do odrzucania duplikatów
    # oraz pomiaru opóźnień i strat.
    sent_at: float | None = Field(default=None, gt=0, description="czas wysłania, Unix epoch [s]")
    seq: int | None = Field(default=None, ge=0, description="numer kolejny wiadomości z urządzenia")


class TelemetryBatchIn(BaseModel):
    items: list[TelemetryIn] = Field(max_length=5000)


class ScooterUserIn(BaseModel):
    """Kto i którą hulajnogę – dla rezerwacji i startu jazdy."""
    scooter_id: str = Field(min_length=1, max_length=32)
    user_id: str = Field(default="demo-user", min_length=1, max_length=64)


class RentalEndIn(BaseModel):
    # force: operator może zakończyć jazdę także w strefie zakazu parkowania (np. przy awarii)
    force: bool = False
