"""Correlación de un comando publicado con su ACK (topic status).

Se correlaciona por `serial` (el backend garantiza, con un índice único
parcial en modulo9.configuraciones_remotas, que nunca hay más de una
configuración PENDIENTE en vuelo por dispositivo) y, desde TC-M09-252, también
por `id_comando`: cada comando lleva un id único y solo el ACK que lo devuelve
resuelve la espera. Sin eso, un ACK capturado y reenviado (replay) -- o uno
forjado por cualquiera con la credencial compartida de dispositivos, que puede
escribir en `status` de cualquier serial -- resolvía como APLICADA el comando
que estuviera en vuelo en ese momento.

ponytail: dict en memoria de un solo proceso -- si el broker corre en
múltiples réplicas sin sticky routing, la correlación se rompe. Solución
futura: backend compartido (Redis pub/sub), no necesaria para esta entrega.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class _Espera:
    id_comando: str
    future: asyncio.Future[None]


_pending_acks: dict[str, _Espera] = {}


def crear_espera(serial: str, id_comando: str) -> asyncio.Future[None]:
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    _pending_acks[serial] = _Espera(id_comando=id_comando, future=future)
    logger.debug("Espera de ACK creada para %s (id_comando=%s)", serial, id_comando)
    return future


def resolver_ack(serial: str, id_comando: str | None = None, *, exigir_id: bool = False) -> bool:
    """Resuelve la espera de `serial` si el ACK corresponde al comando en vuelo.

    Devuelve False, sin tocar la espera, cuando no hay comando en vuelo, cuando
    el ACK trae un `id_comando` distinto del emitido (replay/forjado) o cuando
    falta el id y `exigir_id` está activo.
    """
    espera = _pending_acks.get(serial)
    if espera is None or espera.future.done():
        logger.debug("ACK de %s sin espera activa", serial)
        return False

    if id_comando is None:
        if exigir_id:
            logger.warning(
                "ACK de %s sin id_comando ignorado: MQTT_ACK_REQUIERE_ID_COMANDO está activo.",
                serial,
            )
            return False
        logger.warning(
            "ACK de %s sin id_comando aceptado por compatibilidad; el firmware debe devolverlo.",
            serial,
        )
    elif id_comando != espera.id_comando:
        logger.warning(
            "ACK de %s con id_comando distinto del emitido (recibido=%s, esperado=%s): "
            "ignorado, posible replay.",
            serial,
            id_comando,
            espera.id_comando,
        )
        return False

    espera.future.set_result(None)
    logger.debug("ACK de %s resuelto", serial)
    return True


def limpiar_espera(serial: str) -> None:
    _pending_acks.pop(serial, None)
    logger.debug("Espera de ACK limpiada para %s", serial)
