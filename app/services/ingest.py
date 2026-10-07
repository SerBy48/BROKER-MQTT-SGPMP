"""Lógica de ingesta: recibe payloads MQTT y los persiste vía los SPs de la BD."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from app.config import get_settings
from app.core.errors import (
    DeviceNotFoundError,
    SensorNotFoundError,
    VariableNotFoundError,
)
from app.db.engine import async_session_factory
from app.db.repositories import registry
from app.db.repositories import telemetry as telemetry_repo
from app.mqtt import correlacion, presencia
from app.schemas import HeartbeatPayload, TelemetryPayload

logger = logging.getLogger(__name__)


async def ingest_telemetry(serial: str, data: dict, topic: str | None = None) -> dict:
    logger.debug("Ingestando telemetría: serial=%s topic=%s", serial, topic)
    payload = TelemetryPayload(**data)
    async with async_session_factory() as session:
        device_id = await registry.resolve_device_id(session, serial)
        if device_id is None:
            raise DeviceNotFoundError(serial)

        variable_id = await registry.resolve_variable_id(session, payload.variable)
        if variable_id is None:
            raise VariableNotFoundError(payload.variable)

        sensor_id = await registry.resolve_sensor_id(session, device_id, payload.sensor)
        if sensor_id is None:
            raise SensorNotFoundError(serial, payload.variable)

        if payload.timestamp_envio is None:
            payload = payload.model_copy(update={"timestamp_envio": payload.timestamp_captura})

        result = await telemetry_repo.ingesta_telemetria(
            session,
            device_id=device_id,
            sensor_id=sensor_id,
            variable_id=variable_id,
            payload=payload,
        )

        await telemetry_repo.log_transmission(
            session,
            device_id=device_id,
            topic=topic,
            qos=1,
            payload_bytes=len(json.dumps(data).encode("utf-8")),
            estado="EXITO",
            rssi=payload.calidad_senal_rssi,
            snr=payload.calidad_senal_snr,
        )

        await session.commit()
        logger.info("Telemetría ingestada (%s): %s", serial, result)
        return result


async def ingest_heartbeat(serial: str, data: dict, topic: str | None = None) -> int:
    logger.debug("Ingestando heartbeat: serial=%s topic=%s", serial, topic)
    # TC-M09-63: el Edge que había avisado su desconexión volvió (antes de tocar
    # la BD: aunque el heartbeat no se pueda guardar, el Edge está conectado).
    presencia.marcar_conectado(serial)
    payload = HeartbeatPayload(**data)
    async with async_session_factory() as session:
        device_id = await registry.resolve_device_id(session, serial)
        if device_id is None:
            raise DeviceNotFoundError(serial)

        fecha_registro = payload.fecha_registro or datetime.now(UTC)
        heartbeat_id = await telemetry_repo.insert_heartbeat(
            session,
            device_id=device_id,
            payload=payload,
            fecha_registro=fecha_registro,
        )

        await telemetry_repo.log_transmission(
            session,
            device_id=device_id,
            topic=topic,
            qos=1,
            payload_bytes=len(json.dumps(data).encode("utf-8")),
            estado="EXITO",
            rssi=payload.calidad_senal_rssi,
            snr=payload.calidad_senal_snr,
        )

        await session.commit()
        logger.info("Heartbeat ingestado (%s): id=%s", serial, heartbeat_id)
        return heartbeat_id


_TIPOS_ACK = {correlacion.ACK_CONFIGURACION, correlacion.ACK_UMBRAL}


async def ingest_status(serial: str, data: dict) -> None:
    """Procesa un mensaje del topic `<prefix>/<serial>/status`.

    Contrato propuesto para el ACK de configuración (RF-23, confirmar con
    equipo IoT cuando los topics estén cerrados):
    ``{"tipo_mensaje": "ACK_CONFIGURACION", "resultado": "OK", "id_comando": "<id del comando>"}``.
    El `id_comando` es el que llegó en el comando; solo un ACK que lo devuelva
    resuelve la espera (TC-M09-252, anti-replay). El ACK de un umbral (RF-17)
    es igual con ``"tipo_mensaje": "ACK_UMBRAL"``; cada tipo solo resuelve
    comandos de su mismo tipo.

    Si hay una espera de comando pendiente para este `serial`
    (dispatch_command la crea al publicar), se resuelve acá -- eso es lo que
    permite que /v1/commands responda APLICADA antes de agotar el timeout.
    No transiciona nada en modulo9.configuraciones_remotas: esa tabla es
    propiedad del backend, que actualiza su propia fila con el resultado que
    /v1/commands le devuelve en la misma respuesta HTTP.
    """
    logger.info("Status recibido de %s: %s", serial, data)
    tipo_mensaje = data.get("tipo_mensaje")
    if tipo_mensaje == presencia.TIPO_DESCONEXION:
        # Last Will o cierre ordenado del Edge (TC-M09-63): los comandos que van
        # por él quedan PENDIENTE al instante en vez de esperar el ACK.
        presencia.marcar_desconectado(serial)
        return
    if tipo_mensaje in _TIPOS_ACK and data.get("resultado") == "OK":
        resuelto = correlacion.resolver_ack(
            serial,
            data.get("id_comando"),
            tipo_ack=tipo_mensaje,
            exigir_id=get_settings().mqtt_ack_requiere_id_comando,
        )
        if not resuelto:
            logger.info(
                "ACK de %s no resolvió ninguna espera (expiró, no había ninguna o no "
                "correspondía al comando en vuelo).",
                serial,
            )
