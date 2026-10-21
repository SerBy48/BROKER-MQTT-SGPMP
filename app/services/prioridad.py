# | Fecha      | Nombre   | Que se hizo |
# |------------|----------|-------------|
# | 2026-10-20 | Arekkazu | BRK-11 RFC-016: clave compuesta (severidad, criticidad del área, tipo, origen, antigüedad) y severidad calculada en el broker |
"""Prioridad de un mensaje del nodo bajo saturación (RF-53 E15, RFC-016 v1.2).

Cada mensaje recibe una **clave de orden** determinista; sale primero la menor:

0. **Control** (`status`: ACK, desconexión, EVENTO_BUFFER; `ota/*`) antes que todo.
   Si se pierde, el backend y el nodo quedan desincronizados (P-6, propuesta de
   Desarrollo: la ficha dice "crítico siempre antes", pero un ACK perdido es peor).
1. **Severidad** (A.3, binaria por B.4): una lectura es crítica si su valor cae en el
   nivel `critico` de RF-17 de alguna especie del área (el umbral más restrictivo,
   RFC-012) o si su variable es `es_critica` en M09. Lo decide el broker; el
   `nivel_umbral` del Edge solo cuenta cuando el broker no tiene umbral para esa
   área y variable (pista re-validable).
2. **`nivel_criticidad` del área** (A.1, RF-20): ALTA > MEDIA > BAJA. Visión trae
   `area_id`; el resto se resuelve por el área del dispositivo emisor.
3. **Tipo de dato**: control/negocio (eventos Edge de RF-55) > heartbeat (B.6) >
   telemetría escalar o de visión.
4. **Origen** (A.2): `TIEMPO_REAL` antes que `BUFFER_LOCAL`.
5. **Antigüedad**: primero el `timestamp_captura` más viejo; luego el serial, para
   que un mismo conjunto de mensajes salga siempre en el mismo orden (oráculo §4).

La **clase de admisión** (`Prioridad`) decide qué se rechaza con `RETENER` bajo
saturación: CONTROL nunca, CRITICO (dato crítico, heartbeat, evento Edge) a partir
de 2 × capacidad, NORMAL a partir de la capacidad.

Todo lo que viene de M09 se lee en bloque y se guarda 10 min; si la BD no responde
(puede ser la causa de la saturación) se sigue con lo último leído.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from enum import IntEnum

from app.config import get_settings
from app.db.engine import async_session_factory
from app.db.repositories import registry

logger = logging.getLogger(__name__)

NIVEL_UMBRAL_CRITICO = "critico"
_REFRESCO_S = 600
_TIMEOUT_CONSULTA_S = 2.0
_RANGO_CRITICIDAD = {"ALTA": 0, "MEDIA": 1, "BAJA": 2}
_RANGO_CRITICIDAD_DEFECTO = _RANGO_CRITICIDAD["MEDIA"]  # RF-20: default MEDIA


class Prioridad(IntEnum):
    CONTROL = 0
    CRITICO = 1
    NORMAL = 2


_VACIO = registry.ContextoPrioridad({}, {}, {})
_cache: dict = {"criticas": frozenset(), "contexto": _VACIO, "leido_en": None}


async def _catalogo() -> tuple[frozenset[str], registry.ContextoPrioridad]:
    leido = _cache["leido_en"]
    if leido is None or time.monotonic() - leido >= _REFRESCO_S:
        _cache["leido_en"] = time.monotonic()
        try:
            async with async_session_factory() as session:
                criticas = await asyncio.wait_for(registry.variables_criticas(session), _TIMEOUT_CONSULTA_S)
                contexto = await asyncio.wait_for(registry.contexto_prioridad(session), _TIMEOUT_CONSULTA_S)
            _cache["criticas"], _cache["contexto"] = frozenset(criticas), contexto
        except Exception:
            logger.warning("No se pudo leer el contexto de prioridad de M09; se usa el anterior.")
    return _cache["criticas"], _cache["contexto"]


def _area(serial: str | None, data: dict, contexto: registry.ContextoPrioridad) -> int | None:
    area = data.get("area_id")
    if isinstance(area, int) and not isinstance(area, bool):
        return area
    return contexto.area_por_serial.get(serial) if serial else None


def _es_lectura_critica(data: dict, area: int | None, criticas: frozenset[str],
                        contexto: registry.ContextoPrioridad) -> bool:
    variable = data.get("variable")
    if variable in criticas:
        return True
    bandas = contexto.bandas_criticas.get((area, variable)) if area is not None else None
    if bandas:
        valor = data.get("valor_crudo")
        if not isinstance(valor, (int, float)) or isinstance(valor, bool):
            return False
        return any(inferior <= valor <= superior for inferior, superior in bandas)
    # Sin umbral conocido para esa área y variable: la pista del Edge.
    nivel = data.get("nivel_umbral")
    return isinstance(nivel, str) and nivel.strip().lower() == NIVEL_UMBRAL_CRITICO


def _antiguedad(data: dict) -> float:
    valor = data.get("timestamp_captura") or data.get("timestamp_evento")
    if isinstance(valor, str):
        try:
            return datetime.fromisoformat(valor.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


async def clasificar(tipo: str, data: dict, serial: str | None = None) -> tuple[Prioridad, tuple]:
    """Clase de admisión y clave de orden de un mensaje (menor = sale antes)."""
    s = get_settings()
    if tipo in (s.mqtt_topic_status, s.mqtt_topic_ota):
        return Prioridad.CONTROL, (0, 0, 0, 0, 0, _antiguedad(data), serial or "")

    criticas, contexto = await _catalogo()
    area = _area(serial, data, contexto)
    critica = tipo == s.mqtt_topic_telemetry and _es_lectura_critica(data, area, criticas, contexto)
    rango_tipo = {s.mqtt_topic_evento: 0, s.mqtt_topic_heartbeat: 1}.get(tipo, 2)
    clave = (
        1,
        0 if critica else 1,
        _RANGO_CRITICIDAD.get(contexto.criticidad_por_area.get(area), _RANGO_CRITICIDAD_DEFECTO),
        rango_tipo,
        1 if data.get("origen") == "BUFFER_LOCAL" else 0,
        _antiguedad(data),
        serial or "",
    )
    elevada = critica or rango_tipo < 2
    return (Prioridad.CRITICO if elevada else Prioridad.NORMAL), clave
