"""Validación de credenciales de servicio (schema modulo1, solo lectura)."""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def validar_token_servicio(
    session: AsyncSession, nombre_servicio: str, hash_valor: str
) -> bool:
    result = await session.execute(
        text(
            "SELECT 1 FROM modulo1.credenciales_servicio "
            "WHERE nombre_servicio = :nombre AND hash_valor = :hash AND es_activo = true"
        ),
        {"nombre": nombre_servicio, "hash": hash_valor},
    )
    valido = result.first() is not None
    logger.debug("Validación de credencial para %s: %s", nombre_servicio, valido)
    return valido
