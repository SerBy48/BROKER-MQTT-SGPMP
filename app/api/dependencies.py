"""Dependencias compartidas de la API (autenticación, etc.)."""

from __future__ import annotations

import hashlib

from fastapi import Header, HTTPException

from app.db.engine import async_session_factory
from app.db.repositories import credenciales as credenciales_repo

NOMBRE_SERVICIO_BACKEND = "broker_mqtt"


async def require_api_token(authorization: str | None = Header(default=None)) -> None:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token no válido")

    token = authorization.removeprefix("Bearer ")
    hash_valor = hashlib.sha256(token.encode("utf-8")).hexdigest()

    async with async_session_factory() as session:
        valido = await credenciales_repo.validar_token_servicio(
            session, NOMBRE_SERVICIO_BACKEND, hash_valor
        )
    if not valido:
        raise HTTPException(status_code=401, detail="Token no válido")
