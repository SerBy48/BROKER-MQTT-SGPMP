# | Fecha      | Nombre   | Que se hizo |
# |------------|----------|-------------|
# | 2026-10-20 | Arekkazu | BRK-11 RFC-016: contexto de prioridad (área, criticidad, RF-17) |
# | 2026-10-26 | SerBy48  | BRK-12 RF-55: sensores por nombre para el evento multivariable |
# | 2026-11-01 | Arekkazu | RF-23/RF-22: sin `sensor`, solo con un único sensor activo |
"""Acceso al registro/catálogo IoT (schema modulo9, solo lectura)."""

from __future__ import annotations

import logging
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def resolve_device_id(session: AsyncSession, serial: str) -> int | None:
    result = await session.execute(
        text("SELECT id_dispositivo_iot FROM modulo9.dispositivos_iot WHERE serial = :serial"),
        {"serial": serial},
    )
    row = result.first()
    device_id = row[0] if row else None
    if device_id is None:
        logger.warning("Dispositivo no encontrado en modulo9: serial=%s", serial)
    else:
        logger.debug("Resuelto device_id=%s para serial=%s", device_id, serial)
    return device_id


class EstadoDispositivo(NamedTuple):
    activo: bool
    serial_gateway: str | None  # Gateway Edge que lo atiende (RF-21), si tiene


async def mapa_dispositivos(session: AsyncSession) -> dict[str, EstadoDispositivo]:
    """`serial -> (es_activo, serial de su Gateway Edge)` de todos los dispositivos.

    Un serial que no está en el resultado no existe en modulo9.
    """
    result = await session.execute(
        text(
            "SELECT d.serial, d.es_activo, g.serial "
            "FROM modulo9.dispositivos_iot d "
            "LEFT JOIN modulo9.dispositivos_iot g "
            "  ON g.id_dispositivo_iot = d.id_dispositivo_gateway"
        )
    )
    return {serial: EstadoDispositivo(activo, gateway) for serial, activo, gateway in result}


async def resolve_variable_id(session: AsyncSession, nombre: str) -> int | None:
    result = await session.execute(
        text(
            "SELECT id_variable_ambiental FROM modulo9.variables_ambientales WHERE nombre = :nombre"
        ),
        {"nombre": nombre},
    )
    row = result.first()
    return row[0] if row else None


async def variables_criticas(session: AsyncSession) -> set[str]:
    """Nombres de las variables marcadas críticas en el catálogo de M09 (BRK-09).

    La marca vive en `modulo9.variables_ambientales.es_critica`, que agrega la
    migración del DBA. Mientras la columna no exista, ninguna variable es crítica
    por catálogo (conjunto vacío) y la prioridad sale del resto de las reglas.
    """
    existe = await session.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'modulo9' AND table_name = 'variables_ambientales' "
            "AND column_name = 'es_critica'"
        )
    )
    if existe.first() is None:
        return set()
    result = await session.execute(
        text("SELECT nombre FROM modulo9.variables_ambientales WHERE es_critica = true")
    )
    return {nombre for (nombre,) in result}


async def _existe_columna(session: AsyncSession, tabla: str, columna: str) -> bool:
    result = await session.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'modulo9' AND table_name = :t AND column_name = :c"
        ),
        {"t": tabla, "c": columna},
    )
    return result.first() is not None


# Niveles de RF-17 de un umbral (una especie): (nivel, limite_inferior, limite_superior).
NivelesUmbral = tuple[tuple[str, float, float], ...]


class ContextoPrioridad(NamedTuple):
    """Lo que la cola de RFC-016 necesita de M09 para ordenar sin consultar por mensaje."""

    area_por_serial: dict[str, int]  # dispositivo -> área
    criticidad_por_area: dict[int, str]  # ALTA / MEDIA / BAJA, todas las áreas conocidas
    umbrales: dict[tuple[int, str], list[NivelesUmbral]]  # (área, variable) -> uno por especie


async def contexto_prioridad(session: AsyncSession) -> ContextoPrioridad:
    """Área de cada dispositivo, `nivel_criticidad` de cada área (RF-20) y los niveles de
    RF-17 de todas las especies presentes en cada área (RFC-012).

    Los tres niveles (`normal`, `precaucion`, `critico`) de un umbral son bandas contiguas
    y excluyentes que cubren [valor_min, valor_max] (el backend rechaza otra forma con
    SOLAPAMIENTO_NIVELES). Por eso se leen los tres y no solo `critico`.

    Tolera la BD anterior a DB-06/DB-07: sin `nivel_criticidad` todas las áreas son
    `MEDIA`, y sin `infraestructuras_especies` la especie sale de
    `infraestructuras.id_especie`.
    """
    area_por_serial = {
        serial: area
        for serial, area in await session.execute(
            text(
                "SELECT serial, id_infraestructura FROM modulo9.dispositivos_iot "
                "WHERE id_infraestructura IS NOT NULL"
            )
        )
    }
    criticidad = (
        "nivel_criticidad::text"
        if await _existe_columna(session, "infraestructuras", "nivel_criticidad")
        else "'MEDIA'"
    )
    criticidad_por_area: dict[int, str] = dict(
        (
            await session.execute(
                text(f"SELECT id_infraestructura, {criticidad} FROM modulo9.infraestructuras")
            )
        ).all()
    )
    if await _existe_columna(session, "infraestructuras_especies", "id_especie"):
        especies_area = (
            "SELECT id_infraestructura, id_especie FROM modulo9.infraestructuras_especies"
        )
    else:
        especies_area = (
            "SELECT id_infraestructura, id_especie FROM modulo9.infraestructuras "
            "WHERE id_especie IS NOT NULL"
        )
    result = await session.execute(
        text(
            "SELECT ae.id_infraestructura, va.nombre, u.id_umbral_ambiental, n.nivel::text, "
            "n.limite_inferior, n.limite_superior "
            f"FROM ({especies_area}) ae "
            "JOIN modulo9.umbrales_ambientales u ON u.id_especie = ae.id_especie AND u.es_activo "
            "JOIN modulo9.variables_ambientales va "
            "  ON va.id_variable_ambiental = u.id_variable_ambiental "
            "JOIN modulo9.niveles_alerta_ambientales n "
            "  ON n.id_umbral_ambiental = u.id_umbral_ambiental"
        )
    )
    por_umbral: dict[tuple[int, str, int], list[tuple[str, float, float]]] = {}
    for area, variable, umbral, nivel, inferior, superior in result:
        por_umbral.setdefault((area, variable, umbral), []).append(
            (nivel, float(inferior), float(superior))
        )
    umbrales: dict[tuple[int, str], list[NivelesUmbral]] = {}
    for (area, variable, _), niveles in por_umbral.items():
        umbrales.setdefault((area, variable), []).append(tuple(niveles))
    return ContextoPrioridad(area_por_serial, criticidad_por_area, umbrales)


async def resolve_sensor_id(
    session: AsyncSession,
    device_id: int,
    sensor_nombre: str | None = None,
) -> int | None:
    """Sensor de una lectura dentro de su dispositivo.

    Con `sensor_nombre`, el sensor de ese nombre (único por dispositivo desde la migración
    `c9a8cc1ef814` del backend); los inactivos también, para que el backend deje el intento
    en su bitácora. Sin nombre, solo si el dispositivo tiene **un único** sensor activo:
    con varios no hay forma de saber cuál midió la lectura, y antes se tomaba el primero y
    la lectura quedaba guardada en el sensor equivocado (p. ej. humedad en el de temperatura).
    """
    if sensor_nombre:
        result = await session.execute(
            text(
                "SELECT id_sensores FROM modulo9.sensores "
                "WHERE id_dispositivo_iot = :device_id AND nombre = :nombre"
            ),
            {"device_id": device_id, "nombre": sensor_nombre},
        )
        row = result.first()
        return row[0] if row else None
    result = await session.execute(
        text(
            "SELECT id_sensores FROM modulo9.sensores "
            "WHERE id_dispositivo_iot = :device_id AND es_activo = true "
            "ORDER BY id_sensores LIMIT 2"
        ),
        {"device_id": device_id},
    )
    filas = result.all()
    return filas[0][0] if len(filas) == 1 else None


async def sensores_por_nombre(session: AsyncSession, device_id: int) -> dict[str, int]:
    """`nombre -> id_sensores` de los sensores del dispositivo, en una sola consulta (BRK-12).

    Para el evento multivariable de RF-55: cada variable se resuelve contra este mapa, sin
    un roundtrip por variable y sin el fallback al primer sensor activo de
    `resolve_sensor_id` (en un evento de varias variables atribuiría todas al mismo sensor).
    Incluye los inactivos: si el sensor está inactivo lo decide el backend, que deja el
    intento en su bitácora. Ante nombres repetidos gana el de menor id, como en la ingesta.
    """
    result = await session.execute(
        text(
            "SELECT nombre, id_sensores FROM modulo9.sensores "
            "WHERE id_dispositivo_iot = :device_id ORDER BY id_sensores DESC"
        ),
        {"device_id": device_id},
    )
    return dict(result.all())


async def resolve_device_state(session: AsyncSession, serial: str) -> str | None:
    result = await session.execute(
        text(
            "SELECT e.estado_actual "
            "FROM modulo9.dispositivos_iot d "
            "JOIN modulo3.estados_dispositivos_iot e "
            "  ON e.id_dispositivo_iot = d.id_dispositivo_iot "
            "WHERE d.serial = :serial"
        ),
        {"serial": serial},
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
