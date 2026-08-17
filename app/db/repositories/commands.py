"""Persistencia de comandos / configuración remota (schema modulo9)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_CREATE_CONFIGURACION_SQL = """
INSERT INTO modulo9.configuraciones_remotas (
    frecuencia_captura, intervalo_transmision, id_dispositivo_iot, estado, id_usuario
) VALUES (
    :frecuencia_captura, :intervalo_transmision, :id_dispositivo_iot, 'PENDIENTE', NULL
)
RETURNING id_configuracion_remota
"""


async def create_configuracion_remota(
    session: AsyncSession,
    *,
    device_id: int,
    frecuencia_captura: int,
    intervalo_transmision: int,
) -> int:
    result = await session.execute(
        text(_CREATE_CONFIGURACION_SQL),
        {
            "frecuencia_captura": frecuencia_captura,
            "intervalo_transmision": intervalo_transmision,
            "id_dispositivo_iot": device_id,
        },
    )
    return result.scalar_one()
