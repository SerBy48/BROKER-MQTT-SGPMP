"""Publicación de comandos hacia los dispositivos (SGPMP -> Mosquitto -> IoT)."""

from __future__ import annotations

import json
import logging

from app.config import get_settings
from app.mqtt.client import mqtt_gateway

logger = logging.getLogger(__name__)


def command_topic(serial: str) -> str:
    settings = get_settings()
    return f"{settings.mqtt_topic_prefix}/{serial}/{settings.mqtt_topic_command}"


async def publish_command(serial: str, payload: dict, qos: int = 1) -> str:
    topic = command_topic(serial)
    data = json.dumps(payload).encode("utf-8")
    await mqtt_gateway.publish(topic, data, qos=qos)
    logger.info("Comando publicado en %s", topic)
    return topic
