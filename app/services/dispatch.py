"""Lógica de despacho de comandos: HTTPS -> publish MQTT -> espera de ACK.

El broker ya NO escribe en modulo9.configuraciones_remotas -- esa tabla es
propiedad exclusiva del backend (sgpmp-backend crea la fila PENDIENTE antes
de llamar acá y la actualiza con el resultado). Este servicio es puro
gateway de protocolo: HTTP -> MQTT -> espera acotada de ACK -> HTTP.
"""

from __future__ import annotations

import asyncio
import logging

from app.config import get_settings
from app.core.errors import DeviceNotFoundError, MqttNotConnectedError
from app.db.engine import async_session_factory
from app.db.repositories import registry
from app.mqtt import correlacion, publisher
from app.schemas import CommandRequest, CommandResponse

logger = logging.getLogger(__name__)

_ESTADO_ALCANZABLE = "ACTIVO"


async def dispatch_command(request: CommandRequest) -> CommandResponse:
    logger.debug("Despachando comando: serial=%s", request.serial)
    async with async_session_factory() as session:
        device_id = await registry.resolve_device_id(session, request.serial)
        if device_id is None:
            raise DeviceNotFoundError(request.serial)

        estado_dispositivo = await registry.resolve_device_state(session, request.serial)

    if estado_dispositivo != _ESTADO_ALCANZABLE:
        logger.info(
            "Dispositivo %s no está %s (estado=%s); no se publica, PENDIENTE.",
            request.serial,
            _ESTADO_ALCANZABLE,
            estado_dispositivo,
        )
        return CommandResponse(
            serial=request.serial,
            estado="PENDIENTE",
            mensaje="Dispositivo offline. La configuración quedará pendiente hasta que reconecte.",
        )

    settings = get_settings()
    payload = {
        "frecuencia_captura": request.frecuencia_captura,
        "intervalo_transmision": request.intervalo_transmision,
    }
    future = correlacion.crear_espera(request.serial)
    try:
        topic = await publisher.publish_command(request.serial, payload)
    except MqttNotConnectedError:
        correlacion.limpiar_espera(request.serial)
        logger.error("Broker MQTT no conectado; degradando %s a PENDIENTE.", request.serial)
        return CommandResponse(
            serial=request.serial,
            estado="PENDIENTE",
            mensaje="Broker MQTT no disponible. La configuración quedará pendiente.",
        )

    try:
        await asyncio.wait_for(future, timeout=settings.mqtt_ack_timeout_seconds)
        logger.info("ACK recibido de %s dentro del timeout.", request.serial)
        return CommandResponse(
            serial=request.serial,
            topic=topic,
            estado="APLICADA",
            mensaje="El dispositivo confirmó la recepción de la configuración.",
        )
    except TimeoutError:
        logger.error(
            "Sin ACK de %s tras %ss.", request.serial, settings.mqtt_ack_timeout_seconds
        )
        return CommandResponse(
            serial=request.serial,
            topic=topic,
            estado="NO_CONF",
            mensaje="El comando fue enviado pero el dispositivo no confirmó la recepción a tiempo.",
        )
    finally:
        correlacion.limpiar_espera(request.serial)
