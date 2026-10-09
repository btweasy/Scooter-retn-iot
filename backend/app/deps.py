"""Zależności wspólne dla endpointów (FastAPI Depends)."""
from fastapi import Depends

from .auth import require_token
from .db import SessionLocal


def get_db():
    """Sesja bazy na czas jednego żądania HTTP."""
    with SessionLocal() as db:
        yield db


# dopisywane do endpointów zmieniających stan: wymagają tokenu, jeśli ustawiono API_TOKEN
requires_token = [Depends(require_token)]
