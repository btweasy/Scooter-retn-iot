"""Prosta autoryzacja tokenem dla żądań zmieniających stan (do czasu wdrożenia JWT per użytkownik).

Gdy API_TOKEN nie jest ustawiony, autoryzacja jest wyłączona (tryb deweloperski).
Token: nagłówek `Authorization: Bearer <token>` albo `X-API-Token: <token>`.
"""
import secrets

from fastapi import Header, HTTPException

from . import config


def require_token(authorization: str | None = Header(default=None), x_api_token: str | None = Header(default=None)):
    if config.API_TOKEN is None:
        return
    token = x_api_token
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token or not secrets.compare_digest(token.encode(), config.API_TOKEN.encode()):
        raise HTTPException(401, "Brak lub nieprawidłowy token API", headers={"WWW-Authenticate": "Bearer"})
