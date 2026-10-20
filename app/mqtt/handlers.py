"""Manejo de mensajes MQTT entrantes: parseo y dispatch por tipo de topic."""

from __future__ import annotations

import json
import logging
from typing import NamedTuple

from app.config import get_settings
from app.core.errors import SgpmpError
from app.mqtt import dynsec
from app.services import ingest, reenvio_nodo

logger = logging.getLogger(__name__)


class TopicIot(NamedTuple):
    serial: str
    tipo: str
    subtipo: str | None  # el `<evento>` de `ota/<evento>`; None en los demás


def parsear_topic(topic: str) -> TopicIot | None:
    """`<prefix>/<serial>/<tipo>[/<subtipo>]` (D5 de AIOT-DEV-03).

    El serial va siempre después del prefijo: tomarlo del penúltimo segmento
    (como antes) daba `ota` para `sgpmp/<serial>/ota/<evento>`.
    """
    prefijo = get_settings().mqtt_topic_prefix.split("/")
    partes = topic.split("/")
    if partes[: len(prefijo)] != prefijo:
        return None
    resto = partes[len(prefijo):]
    if len(resto) not in (2, 3) or not all(resto):
        return None
    return TopicIot(resto[0], resto[1], resto[2] if len(resto) == 3 else None)


def parsear_mensaje(topic: str, payload: bytes) -> tuple[TopicIot, dict] | None:
    """Topic y cuerpo JSON de un mensaje del nodo; None (y log) si no son válidos."""
    iot = parsear_topic(topic)
    if iot is None:
        logger.warning("Topic MQTT no reconocido: %s", topic)
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        logger.error("Payload JSON inválido en topic %s", topic)
        return None
    if not isinstance(data, dict):
        logger.error("Payload en %s no es un objeto JSON", topic)
        return None
    logger.debug("Mensaje parseado: %s payload=%s", iot, data)
    return iot, data


async def procesar(iot: TopicIot, data: dict, topic: str) -> None:
    """Despacha un mensaje ya parseado a su servicio."""
    settings = get_settings()
    serial, msg_type = iot.serial, iot.tipo
    try:
        if iot.subtipo is not None:
            if msg_type == settings.mqtt_topic_ota:
                reenvio_nodo.reporte_ota(serial, iot.subtipo, data)
            else:
                logger.warning("Tipo de mensaje desconocido en topic %s", topic)
        elif msg_type == settings.mqtt_topic_telemetry:
            await ingest.ingest_telemetry(serial, data, topic=topic)
        elif msg_type == settings.mqtt_topic_heartbeat:
            await ingest.ingest_heartbeat(serial, data, topic=topic)
        elif msg_type == settings.mqtt_topic_status:
            await ingest.ingest_status(serial, data)
        elif msg_type == settings.mqtt_topic_vision:
            reenvio_nodo.vision(serial, data)
        elif msg_type == settings.mqtt_topic_evento:
            reenvio_nodo.evento_edge(serial, data)
        else:
            logger.warning("Tipo de mensaje desconocido en topic %s", topic)
    except SgpmpError as exc:
        logger.error("Error de dominio procesando mensaje en %s: %s", topic, exc)
    except Exception:
        logger.exception("Error inesperado procesando mensaje en %s", topic)


async def handle_message(topic: str, payload: bytes) -> None:
    """Procesa un mensaje en el acto, sin pasar por la cola de BRK-09."""
    if topic == dynsec.TOPIC_RESPUESTA:
        dynsec.resolver_respuesta(payload)
        return
    mensaje = parsear_mensaje(topic, payload)
    if mensaje is not None:
        await procesar(*mensaje, topic)
