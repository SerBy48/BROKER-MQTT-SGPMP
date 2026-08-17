"""Acceso al registro/catálogo IoT (schema modulo9, solo lectura)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def resolve_device_id(session: AsyncSession, serial: str) -> int | None:
    result = await session.execute(
        text("SELECT id_dispositivo_iot FROM modulo9.dispositivos_iot WHERE serial = :serial"),
        {"serial": serial},
    )
    row = result.first()
    return row[0] if row else None


async def resolve_variable_id(session: AsyncSession, nombre: str) -> int | None:
    result = await session.execute(
        text("SELECT id_variable_ambiental FROM modulo9.variables_ambientales WHERE nombre = :nombre"),
        {"nombre": nombre},
    )
    row = result.first()
    return row[0] if row else None


async def resolve_sensor_id(
    session: AsyncSession,
    device_id: int,
    sensor_nombre: str | None = None,
) -> int | None:
    # TODO: definir la estrategia real de resolución sensor<->variable con el
    # equipo de IoT. Por ahora: por nombre si viene en el payload; si no, el
    # primer sensor activo del dispositivo.
    if sensor_nombre:
        result = await session.execute(
            text(
                "SELECT id_sensores FROM modulo9.sensores "
                "WHERE id_dispositivo_iot = :device_id AND nombre = :nombre"
            ),
            {"device_id": device_id, "nombre": sensor_nombre},
        )
    else:
        result = await session.execute(
            text(
                "SELECT id_sensores FROM modulo9.sensores "
                "WHERE id_dispositivo_iot = :device_id AND es_activo = true "
                "ORDER BY id_sensores LIMIT 1"
            ),
            {"device_id": device_id},
        )
    row = result.first()
    return row[0] if row else None


async def list_device_states(session: AsyncSession) -> list[dict]:
    result = await session.execute(
        text(
            "SELECT d.serial, e.estado_actual, e.fecha_ultimo_contacto, e.tiempo_sin_contacto "
            "FROM modulo9.dispositivos_iot d "
            "LEFT JOIN modulo3.estados_dispositivos_iot e "
            "  ON e.id_dispositivo_iot = d.id_dispositivo_iot "
            "WHERE d.es_activo = true "
            "ORDER BY d.serial"
        )
    )
    return [dict(row._mapping) for row in result]
