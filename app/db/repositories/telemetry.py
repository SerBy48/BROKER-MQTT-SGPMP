"""Persistencia de ingesta IoT (schema modulo3).

Usa las stored functions/procedures ya definidas en la base, delegando en la BD
toda la lógica de calidad, validación y triggers (no se reimplementa aquí).
"""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas import HeartbeatPayload, TelemetryPayload

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

_INSERT_HEARTBEAT_SQL = """
INSERT INTO modulo3.heartbeats (
    id_dispositivo_iot, tipo_mensaje, nivel_bateria_pct, calidad_senal_rssi,
    calidad_senal_snr, estado_local_buffer, datos_pendientes_buffer,
    version_firmware, coordenadas, fecha_registro, reloj_sincronizado
) VALUES (
    :id_dispositivo,
    CAST(:tipo_mensaje AS modulo3.enum_tipo_mensaje_iot),
    :nivel_bateria,
    :rssi,
    :snr,
    :estado_buffer,
    :datos_pendientes,
    :version_firmware,
    CAST(:coordenadas AS jsonb),
    :fecha_registro,
    :reloj_sincronizado
)
RETURNING id_heartbeat
"""

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
    result = await session.execute(
        text(_INSERT_HEARTBEAT_SQL),
        {
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
        },
    )
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
