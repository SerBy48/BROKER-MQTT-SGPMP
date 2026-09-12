"""Dependencias compartidas de la API (autenticación, etc.)."""

from __future__ import annotations

import hashlib
import logging

from fastapi import Header, HTTPException

from app.db.engine import async_session_factory
from app.db.repositories import credenciales as credenciales_repo

logger = logging.getLogger(__name__)

NOMBRE_SERVICIO_BACKEND = "broker_mqtt"


async def require_api_token(authorization: str | None = Header(default=None)) -> None:
    if authorization is None or not authorization.startswith("Bearer "):
        logger.warning("Request sin token Bearer válido")
        raise HTTPException(status_code=401, detail="Token no válido")

    token = authorization.removeprefix("Bearer ")
    hash_valor = hashlib.sha256(token.encode("utf-8")).hexdigest()

    async with async_session_factory() as session:
        valido = await credenciales_repo.validar_token_servicio(
            session, NOMBRE_SERVICIO_BACKEND, hash_valor
        )
    if not valido:
        logger.warning("Token de servicio rechazado para %s", NOMBRE_SERVICIO_BACKEND)
        raise HTTPException(status_code=401, detail="Token no válido")
    logger.debug("Token de servicio validado para %s", NOMBRE_SERVICIO_BACKEND)
