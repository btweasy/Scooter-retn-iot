"""Tabele bazy danych (SQLAlchemy ORM)."""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    """Czas UTC bez strefy (spójnie dla SQLite i PostgreSQL)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


Money = Numeric(10, 2)   # kwoty w PLN – dokładnie, bez błędów zaokrągleń float


class Scooter(Base):
    """Ostatni znany stan pojazdu (jeden wiersz na hulajnogę)."""
    __tablename__ = "scooters"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    city: Mapped[str] = mapped_column(String(64), index=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    battery: Mapped[float] = mapped_column(Float)
    speed: Mapped[float] = mapped_column(Float, default=0.0)
    locked: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime)            # czas odbioru przez serwer
    last_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # czas z urządzenia
    last_seq: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Flagi alertów: alert powstaje tylko przy zmianie False -> True (bez zalewu powiadomień)
    in_zone: Mapped[bool] = mapped_column(Boolean, default=False)
    low_battery_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    unauthorized_flag: Mapped[bool] = mapped_column(Boolean, default=False)


class Telemetry(Base):
    """Historia pomiarów (szereg czasowy) – z niej liczona jest trasa wypożyczenia."""
    __tablename__ = "telemetry"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scooter_id: Mapped[str] = mapped_column(String(32), ForeignKey("scooters.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)       # czas odbioru przez serwer
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # czas wysłania z urządzenia
    seq: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    lat: Mapped[float] = mapped_column(Float)
    lon: Mapped[float] = mapped_column(Float)
    battery: Mapped[float] = mapped_column(Float)
    speed: Mapped[float] = mapped_column(Float)
    __table_args__ = (Index("ix_telemetry_scooter_ts", "scooter_id", "ts"),)


class Reservation(Base):
    """Bezpłatna rezerwacja pojazdu na kilka minut (jak w Bolt/Lime).

    Status: active -> used (rozpoczęto jazdę) | cancelled (anulowana) | expired (minął czas).
    """
    __tablename__ = "reservations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scooter_id: Mapped[str] = mapped_column(String(32), ForeignKey("scooters.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    # co najwyżej jedna aktywna rezerwacja na pojazd (także przy dwóch równoczesnych żądaniach)
    __table_args__ = (Index("uq_reservations_active_scooter", "scooter_id", unique=True,
                            sqlite_where=text("status = 'active'"), postgresql_where=text("status = 'active'")),)


class Rental(Base):
    """Wypożyczenie. Status: active <-> paused -> finished.

    Cennik jest kopiowany do wypożyczenia w chwili startu – zmiana cennika nie zmienia ceny trwającej jazdy.
    """
    __tablename__ = "rentals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scooter_id: Mapped[str] = mapped_column(String(32), ForeignKey("scooters.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="active")
    started_at: Mapped[datetime] = mapped_column(DateTime)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # pauza: paused_at = początek bieżącej pauzy; paused_seconds = suma zakończonych pauz
    paused_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    paused_seconds: Mapped[int] = mapped_column(Integer, default=0)
    # cennik z chwili startu
    unlock_fee: Mapped[Decimal] = mapped_column(Money)
    price_per_min: Mapped[Decimal] = mapped_column(Money)
    pause_price_per_min: Mapped[Decimal] = mapped_column(Money)
    # wynik – wypełniany przy zakończeniu
    distance_km: Mapped[float] = mapped_column(Float, default=0.0)
    ride_minutes: Mapped[int] = mapped_column(Integer, default=0)
    pause_minutes: Mapped[int] = mapped_column(Integer, default=0)
    cost_pln: Mapped[Decimal] = mapped_column(Money, default=Decimal("0.00"))
    closed_automatically: Mapped[bool] = mapped_column(Boolean, default=False)
    # Co najwyżej jedno niezakończone wypożyczenie na pojazd. Indeks w bazie chroni też przed wyścigiem
    # dwóch równoczesnych żądań "start", którego nie wykryłoby samo sprawdzenie w kodzie.
    __table_args__ = (Index("uq_rentals_open_scooter", "scooter_id", unique=True,
                            sqlite_where=text("ended_at IS NULL"), postgresql_where=text("ended_at IS NULL")),)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scooter_id: Mapped[str] = mapped_column(String(32), ForeignKey("scooters.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))   # geofence | low_battery | unauthorized | rental_closed
    message: Mapped[str] = mapped_column(String(255))
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
