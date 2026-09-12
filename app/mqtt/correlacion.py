"""Correlación de un comando publicado con su ACK (topic status).

Correlaciona por `serial` únicamente: el backend garantiza (índice único
parcial en modulo9.configuraciones_remotas) que nunca hay más de una
configuración PENDIENTE en vuelo por dispositivo, así que no hace falta un
id de correlación explícito por request.

ponytail: dict en memoria de un solo proceso -- si el broker corre en
múltiples réplicas sin sticky routing, la correlación se rompe. Solución
futura: backend compartido (Redis pub/sub), no necesaria para esta entrega.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_pending_acks: dict[str, asyncio.Future[None]] = {}


def crear_espera(serial: str) -> asyncio.Future[None]:
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    _pending_acks[serial] = future
    logger.debug("Espera de ACK creada para %s", serial)
    return future


def resolver_ack(serial: str) -> bool:
    future = _pending_acks.get(serial)
    if future is None or future.done():
        logger.debug("ACK de %s sin espera activa", serial)
        return False
    future.set_result(None)
    logger.debug("ACK de %s resuelto", serial)
    return True


def limpiar_espera(serial: str) -> None:
    _pending_acks.pop(serial, None)
    logger.debug("Espera de ACK limpiada para %s", serial)
