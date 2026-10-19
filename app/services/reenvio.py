"""Reenvío al backend de los eventos de control del nodo (P2 de AIOT-DEV-03).

Los eventos de bajo volumen (buffer, visión, eventos Edge, reportes OTA) llegan
por MQTT y se reenvían a un endpoint interno del backend con el cliente de
BRK-02. Cada reenvío corre en su propia tarea: el bucle de mensajes MQTT
procesa uno a la vez, y esperar al backend ahí frenaría la telemetría de todos
los nodos.

ponytail: sin cola ni reintento. Si el backend está caído el evento queda solo
en el log del broker; hay que agregar una cola persistente si se vuelve un
problema operativo (RF-54 asigna el resguardo al Edge, no al broker).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.backend import client as backend_client

logger = logging.getLogger(__name__)

_tareas: set[asyncio.Task] = set()


async def _enviar(ruta: str, cuerpo: dict[str, Any], descripcion: str) -> None:
    respuesta = await backend_client.enviar(ruta, cuerpo)
    if respuesta.ok:
        logger.info("%s reenviado al backend (%s -> %d)", descripcion, ruta, respuesta.status)
    else:
        logger.error("%s NO se reenvió al backend (%s): %s", descripcion, ruta, respuesta.error)


def reenviar(ruta: str, cuerpo: dict[str, Any], descripcion: str) -> asyncio.Task:
    """Programa el POST a `ruta` sin esperar la respuesta del backend."""
    tarea = asyncio.create_task(_enviar(ruta, cuerpo, descripcion))
    _tareas.add(tarea)  # referencia fuerte hasta que termine (si no, el GC la puede cortar)
    tarea.add_done_callback(_tareas.discard)
    return tarea
