"""Cliente de la API de dynamic-security de Mosquitto (SEG-BROKER-03).

Los comandos van en un solo mensaje a `$CONTROL/dynamic-security/v1` y Mosquitto
contesta en `.../response` con una lista de respuestas en el mismo orden, cada
una con el `correlationData` del comando y, si falló, un `error` en texto
("Client not found", "Role already exists", ...). Este módulo solo habla MQTT:
qué comandos mandar lo decide app/services/credenciales_mqtt.py.

ponytail: esperas en memoria de un solo proceso, igual que correlacion.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from app.core.errors import DynsecError
from app.mqtt.client import mqtt_gateway

logger = logging.getLogger(__name__)

TOPIC = "$CONTROL/dynamic-security/v1"
TOPIC_RESPUESTA = f"{TOPIC}/response"
_TIMEOUT_S = 5

_pendientes: dict[str, asyncio.Future[list[dict]]] = {}


async def ejecutar(comandos: list[dict]) -> list[dict]:
    """Ejecuta `comandos` y devuelve las respuestas en el mismo orden.

    No interpreta los `error` de cada respuesta (algunos son esperables, como
    "Role already exists"); eso lo decide quien llama. Lanza DynsecError si
    Mosquitto no contesta a tiempo.
    """
    lote = uuid.uuid4().hex
    mensaje = {
        "commands": [{**c, "correlationData": f"{lote}:{i}"} for i, c in enumerate(comandos)]
    }
    future: asyncio.Future[list[dict]] = asyncio.get_running_loop().create_future()
    _pendientes[lote] = future
    try:
        await mqtt_gateway.publish(TOPIC, json.dumps(mensaje).encode("utf-8"), qos=1)
        return await asyncio.wait_for(future, timeout=_TIMEOUT_S)
    except TimeoutError as exc:
        raise DynsecError(f"Mosquitto no respondió a dynamic-security en {_TIMEOUT_S}s") from exc
    finally:
        _pendientes.pop(lote, None)


def resolver_respuesta(payload: bytes) -> None:
    """Entrega una respuesta de `TOPIC_RESPUESTA` a quien la está esperando."""
    try:
        respuestas = json.loads(payload)["responses"]
        lote = str(respuestas[0]["correlationData"]).split(":", 1)[0]
    except (ValueError, KeyError, IndexError, TypeError):
        logger.warning("Respuesta de dynamic-security no reconocida: %r", payload[:200])
        return
    future = _pendientes.get(lote)
    if future is not None and not future.done():
        future.set_result(respuestas)
