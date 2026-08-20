"""Lógica de ingesta: recibe payloads MQTT y los persiste vía los SPs de la BD."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from app.core.errors import (
    DeviceNotFoundError,
    SensorNotFoundError,
    VariableNotFoundError,
)
from app.db.engine import async_session_factory
from app.db.repositories import registry, telemetry as telemetry_repo
from app.mqtt import correlacion
from app.schemas import HeartbeatPayload, TelemetryPayload

logger = logging.getLogger(__name__)


async def ingest_telemetry(serial: str, data: dict, topic: str | None = None) -> dict:
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
    payload = HeartbeatPayload(**data)
    async with async_session_factory() as session:
        device_id = await registry.resolve_device_id(session, serial)
        if device_id is None:
            raise DeviceNotFoundError(serial)

        fecha_registro = payload.fecha_registro or datetime.now(timezone.utc)
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


async def ingest_status(serial: str, data: dict) -> None:
    """Procesa un mensaje del topic `<prefix>/<serial>/status`.

    Contrato propuesto para el ACK de configuración (RF-23, confirmar con
    equipo IoT cuando los topics estén cerrados):
    ``{"tipo_mensaje": "ACK_CONFIGURACION", "resultado": "OK"}``.

    Si hay una espera de comando pendiente para este `serial`
    (dispatch_command la crea al publicar), se resuelve acá -- eso es lo que
    permite que /v1/commands responda APLICADA antes de agotar el timeout.
    No transiciona nada en modulo9.configuraciones_remotas: esa tabla es
    propiedad del backend, que actualiza su propia fila con el resultado que
    /v1/commands le devuelve en la misma respuesta HTTP.
    """
    logger.info("Status recibido de %s: %s", serial, data)
    if data.get("tipo_mensaje") == "ACK_CONFIGURACION" and data.get("resultado") == "OK":
        resuelto = correlacion.resolver_ack(serial)
        if not resuelto:
            logger.info(
                "ACK de %s sin request en espera (ya expiró o no había ninguna).",
                serial,
            )
