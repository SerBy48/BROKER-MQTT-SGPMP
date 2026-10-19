"""Persistencia de ingesta IoT (schema modulo3).

Usa las stored functions/procedures ya definidas en la base, delegando en la BD
toda la lógica de calidad, validación y triggers (no se reimplementa aquí).
"""

from __future__ import annotations

import json
import logging
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import HeartbeatPayload, TelemetryPayload

logger = logging.getLogger(__name__)

_INGESTA_TELEMETRIA_SQL = """
SELECT id_telemetria, estado_calidad, mensaje
FROM modulo3.fn_ingesta_telemetria(
    :p_id_dispositivo,
    :p_id_sensor,
    :p_id_variable,
    :p_valor_crudo,
    :p_unidad,
    :p_timestamp_captura,
    :p_timestamp_envio,
    CAST(:p_origen AS modulo3.enum_telemetria_origen),
    CAST(:p_categoria_variable AS modulo3.enum_telemetria_categoria_variable),
    CAST(:p_tipo_dato AS modulo3.enum_telemetria_tipo_dato),
    :p_valor_agregado,
    :p_ventana_agregacion,
    :p_latitud,
    :p_longitud,
    :p_nivel_bateria,
    :p_calidad_rssi,
    :p_calidad_snr,
    :p_estado_conectividad,
    CAST(:p_metadatos AS jsonb)
)
"""

_COLUMNAS_HEARTBEAT = """
    id_dispositivo_iot, tipo_mensaje, nivel_bateria_pct, calidad_senal_rssi,
    calidad_senal_snr, estado_local_buffer, datos_pendientes_buffer,
    version_firmware, coordenadas, fecha_registro, reloj_sincronizado"""

_VALORES_HEARTBEAT = """
    :id_dispositivo,
    CAST(:tipo_mensaje AS modulo3.enum_tipo_mensaje_iot),
    :nivel_bateria,
    :rssi,
    :snr,
    -- "char": asyncpg lo codifica como bytes; pasando por text acepta un str.
    CAST(CAST(:estado_buffer AS text) AS "char"),
    :datos_pendientes,
    :version_firmware,
    CAST(:coordenadas AS jsonb),
    :fecha_registro,
    :reloj_sincronizado"""

# BRK-07 (D11 de AIOT-DEV-03): columnas que agrega la migración DB-03. Nombres
# según anotaciones/convencion_nomenclatura_bd.md del backend (JSONB con prefijo
# `json_`); si DB-03 las nombra distinto, se cambian solo acá.
COLUMNA_SENSORES_OPERATIVOS = "json_sensores_operativos"
COLUMNA_TEMPERATURA_DISPOSITIVO = "temperatura_dispositivo"
_COLUMNAS_D11 = (COLUMNA_SENSORES_OPERATIVOS, COLUMNA_TEMPERATURA_DISPOSITIVO)
_REVISAR_D11_CADA_S = 600


def _insert_heartbeat_sql(con_d11: bool) -> str:
    columnas, valores = _COLUMNAS_HEARTBEAT, _VALORES_HEARTBEAT
    if con_d11:
        columnas += f", {COLUMNA_SENSORES_OPERATIVOS}, {COLUMNA_TEMPERATURA_DISPOSITIVO}"
        valores += ",\n    CAST(:sensores_operativos AS jsonb),\n    :temperatura_dispositivo"
    return (
        f"INSERT INTO modulo3.heartbeats ({columnas}\n) VALUES ({valores}\n)\n"
        "RETURNING id_heartbeat"
    )


_INSERT_HEARTBEAT_SQL = _insert_heartbeat_sql(con_d11=False)
_INSERT_HEARTBEAT_D11_SQL = _insert_heartbeat_sql(con_d11=True)

# Regla 2 del acuerdo: lo que sale antes que su par tolera su ausencia. Mientras
# DB-03 no esté aplicada, el heartbeat se guarda como siempre (sin los dos campos).
# Una vez que aparecen las columnas no se vuelve a preguntar.
_d11: dict = {"disponible": False, "revisado_en": None}


async def _heartbeat_tiene_d11(session: AsyncSession) -> bool:
    ahora = time.monotonic()
    revisado = _d11["revisado_en"]
    if _d11["disponible"] or (revisado is not None and ahora - revisado < _REVISAR_D11_CADA_S):
        return _d11["disponible"]
    resultado = await session.execute(
        text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema = 'modulo3' AND table_name = 'heartbeats' "
            "AND column_name = ANY(:columnas)"
        ),
        {"columnas": list(_COLUMNAS_D11)},
    )
    _d11["disponible"] = resultado.scalar_one() == len(_COLUMNAS_D11)
    _d11["revisado_en"] = ahora
    if not _d11["disponible"]:
        logger.info("modulo3.heartbeats aún sin las columnas de D11 (DB-03): se omiten.")
    return _d11["disponible"]

_INSERT_TRANSMISION_SQL = """
INSERT INTO modulo3.transmisiones_mqtt (
    id_dispositivo_iot, topic, qos, payloads_bytes, estado, gatway_id,
    rssi_dbm, snr_db, intentos, error_descripcion
) VALUES (
    :id_dispositivo, :topic, :qos, :payloads_bytes,
    CAST(:estado AS modulo3.enum_transmiciones_mqqt_estado),
    :gatway_id, :rssi, :snr, :intentos, :error_descripcion
)
"""


async def ingesta_telemetria(
    session: AsyncSession,
    *,
    device_id: int,
    sensor_id: int,
    variable_id: int,
    payload: TelemetryPayload,
) -> dict:
    result = await session.execute(
        text(_INGESTA_TELEMETRIA_SQL),
        {
            "p_id_dispositivo": device_id,
            "p_id_sensor": sensor_id,
            "p_id_variable": variable_id,
            "p_valor_crudo": payload.valor_crudo,
            "p_unidad": payload.unidad,
            "p_timestamp_captura": payload.timestamp_captura,
            "p_timestamp_envio": payload.timestamp_envio,
            "p_origen": payload.origen.value,
            "p_categoria_variable": payload.categoria_variable.value,
            "p_tipo_dato": payload.tipo_dato.value,
            "p_valor_agregado": payload.valor_agregado,
            "p_ventana_agregacion": payload.ventana_agregacion_min,
            "p_latitud": payload.latitud,
            "p_longitud": payload.longitud,
            "p_nivel_bateria": payload.nivel_bateria_pct,
            "p_calidad_rssi": payload.calidad_senal_rssi,
            "p_calidad_snr": payload.calidad_senal_snr,
            "p_estado_conectividad": payload.estado_conectividad,
            "p_metadatos": json.dumps(payload.metadatos),
        },
    )
    row = result.first()
    return {
        "id_telemetria": row.id_telemetria,
        "estado_calidad": row.estado_calidad,
        "mensaje": row.mensaje,
    }


async def insert_heartbeat(
    session: AsyncSession,
    *,
    device_id: int,
    payload: HeartbeatPayload,
    fecha_registro,
) -> int:
    con_d11 = await _heartbeat_tiene_d11(session)
    parametros = {
        "id_dispositivo": device_id,
        "tipo_mensaje": payload.tipo_mensaje.value,
        "nivel_bateria": payload.nivel_bateria_pct,
        "rssi": payload.calidad_senal_rssi,
        "snr": payload.calidad_senal_snr,
        "estado_buffer": payload.estado_local_buffer,
        "datos_pendientes": payload.datos_pendientes_buffer,
        "version_firmware": payload.version_firmware,
        "coordenadas": json.dumps(payload.coordenadas) if payload.coordenadas else None,
        "fecha_registro": fecha_registro,
        "reloj_sincronizado": payload.reloj_sincronizado,
    }
    if con_d11:
        sensores = payload.sensores_operativos
        parametros["sensores_operativos"] = (
            json.dumps([s.model_dump() for s in sensores]) if sensores is not None else None
        )
        parametros["temperatura_dispositivo"] = payload.temperatura_dispositivo
    elif payload.sensores_operativos is not None or payload.temperatura_dispositivo is not None:
        logger.debug("Heartbeat con campos de D11 y BD sin DB-03: se descartan esos campos.")
    sql = _INSERT_HEARTBEAT_D11_SQL if con_d11 else _INSERT_HEARTBEAT_SQL
    result = await session.execute(text(sql), parametros)
    return result.scalar_one()


async def log_transmission(
    session: AsyncSession,
    *,
    device_id: int,
    topic: str | None,
    qos: int,
    payload_bytes: int,
    estado: str = "EXITO",
    gatway_id: str | None = None,
    rssi: float | None = None,
    snr: float | None = None,
    error_descripcion: str | None = None,
) -> None:
    await session.execute(
        text(_INSERT_TRANSMISION_SQL),
        {
            "id_dispositivo": device_id,
            "topic": topic,
            "qos": qos,
            "payloads_bytes": payload_bytes,
            "estado": estado,
            "gatway_id": gatway_id,
            "rssi": int(rssi) if rssi is not None else None,
            "snr": snr,
            "intentos": 1,
            "error_descripcion": error_descripcion,
        },
    )
