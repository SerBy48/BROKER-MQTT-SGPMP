"""Manejo de mensajes MQTT entrantes: parseo y dispatch por tipo de topic."""

from __future__ import annotations

import json
import logging

from app.config import get_settings
from app.core.errors import SgpmpError
from app.services import ingest

logger = logging.getLogger(__name__)


async def handle_message(topic: str, payload: bytes) -> None:
    settings = get_settings()
    parts = topic.split("/")
    if len(parts) < 3:
        logger.warning("Topic MQTT no reconocido: %s", topic)
        return

    msg_type = parts[-1]
    serial = parts[-2]

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        logger.error("Payload JSON inválido en topic %s", topic)
        return

    logger.debug("Mensaje parseado: serial=%s tipo=%s payload=%s", serial, msg_type, data)

    try:
        if msg_type == settings.mqtt_topic_telemetry:
            await ingest.ingest_telemetry(serial, data, topic=topic)
        elif msg_type == settings.mqtt_topic_heartbeat:
            await ingest.ingest_heartbeat(serial, data, topic=topic)
        elif msg_type == settings.mqtt_topic_status:
            await ingest.ingest_status(serial, data)
        else:
            logger.warning("Tipo de mensaje desconocido en topic %s", topic)
    except SgpmpError as exc:
        logger.error("Error de dominio procesando mensaje en %s: %s", topic, exc)
    except Exception:
        logger.exception("Error inesperado procesando mensaje en %s", topic)
