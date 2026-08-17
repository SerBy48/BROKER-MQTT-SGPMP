"""Dependencias compartidas de la API (autenticación, etc.)."""

from __future__ import annotations

from fastapi import Header, HTTPException

from app.config import get_settings


async def require_api_token(authorization: str | None = Header(default=None)) -> None:
    settings = get_settings()
    expected = f"Bearer {settings.api_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Token no válido")
