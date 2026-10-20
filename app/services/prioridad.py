"""Prioridad de un mensaje del nodo bajo saturación (BRK-09, RF-53 E15).

Definición de "dato crítico" (propuesta de Desarrollo para AN-5, ver
docs/BRK-09_Cola_Priorizacion_Saturacion.md). Un mensaje es:

- CONTROL (P0) si es de control: `status` (ACK de comandos, desconexión,
  EVENTO_BUFFER) y `ota/*`. Son pocos y, si se pierden, el backend y el nodo
  quedan desincronizados. Nunca se descartan.
- CRITICO (P1) si es un heartbeat (de él depende la detección de inactividad de
  RF-60), un evento Edge de RF-55 (ya es una desviación), o una lectura que:
    · el Edge clasificó en el nivel "critico" del umbral vigente de la especie
      (`nivel_umbral`, niveles de RF-17), o
    · es de una variable marcada crítica en el catálogo de M09 (`es_critica`).
- NORMAL (P2) todo lo demás, incluido el vector de visión.

Las tres fuentes son datos que el broker ya tiene: el tipo de topic, lo que el
Edge calcula con sus umbrales y una marca del catálogo que edita el Administrador.
Nada de la regla queda fijo en el código salvo la estructura.
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import IntEnum

from app.config import get_settings
from app.db.engine import async_session_factory
from app.db.repositories import registry

logger = logging.getLogger(__name__)

NIVEL_UMBRAL_CRITICO = "critico"
_REFRESCO_S = 600
_TIMEOUT_CONSULTA_S = 2.0


class Prioridad(IntEnum):
    CONTROL = 0
    CRITICO = 1
    NORMAL = 2


_criticas: dict = {"nombres": frozenset(), "leido_en": None}


async def _variables_criticas() -> frozenset[str]:
    """Catálogo de M09 con caché de 10 min. Si la BD no responde (puede ser la causa
    de la saturación), se sigue con lo último que se leyó."""
    leido = _criticas["leido_en"]
    if leido is not None and time.monotonic() - leido < _REFRESCO_S:
        return _criticas["nombres"]
    _criticas["leido_en"] = time.monotonic()
    try:
        async with async_session_factory() as session:
            nombres = await asyncio.wait_for(
                registry.variables_criticas(session), timeout=_TIMEOUT_CONSULTA_S
            )
        _criticas["nombres"] = frozenset(nombres)
    except Exception:
        logger.warning("No se pudo leer el catálogo de variables críticas; se usa el anterior.")
    return _criticas["nombres"]


async def clasificar(tipo: str, data: dict) -> Prioridad:
    """Prioridad de un mensaje según el tipo de su topic y su contenido."""
    s = get_settings()
    if tipo in (s.mqtt_topic_status, s.mqtt_topic_ota):
        return Prioridad.CONTROL
    if tipo in (s.mqtt_topic_heartbeat, s.mqtt_topic_evento):
        return Prioridad.CRITICO
    if tipo == s.mqtt_topic_telemetry:
        nivel = data.get("nivel_umbral")
        if isinstance(nivel, str) and nivel.strip().lower() == NIVEL_UMBRAL_CRITICO:
            return Prioridad.CRITICO
        if data.get("variable") in await _variables_criticas():
            return Prioridad.CRITICO
    return Prioridad.NORMAL
