"""Lógica de despacho de comandos: HTTPS -> publish MQTT -> registro en BD."""

from __future__ import annotations

import logging

from app.core.errors import DeviceNotFoundError
from app.db.engine import async_session_factory
from app.db.repositories import commands as commands_repo
from app.db.repositories import registry
from app.mqtt import publisher
from app.schemas import CommandRequest, CommandResponse

logger = logging.getLogger(__name__)


async def dispatch_command(request: CommandRequest) -> CommandResponse:
    async with async_session_factory() as session:
        device_id = await registry.resolve_device_id(session, request.serial)
        if device_id is None:
            raise DeviceNotFoundError(request.serial)

        topic = await publisher.publish_command(
            request.serial,
            {
                "frecuencia_captura": request.frecuencia_captura,
                "intervalo_transmision": request.intervalo_transmision,
            },
        )

        id_config = await commands_repo.create_configuracion_remota(
            session,
            device_id=device_id,
            frecuencia_captura=request.frecuencia_captura,
            intervalo_transmision=request.intervalo_transmision,
        )
        await session.commit()

        logger.info(
            "Comando despachado a %s (config_remota=%s)", request.serial, id_config
        )
        return CommandResponse(
            serial=request.serial,
            topic=topic,
            id_configuracion_remota=id_config,
        )
