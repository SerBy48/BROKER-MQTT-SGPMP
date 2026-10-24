# | Fecha      | Nombre   | Que se hizo |
# |------------|----------|-------------|
# | 2026-10-20 | Arekkazu | BRK-11 RFC-016: clave compuesta y severidad calculada en el broker |
"""Prioridad de un mensaje del nodo bajo saturación (RF-53 E15, RFC-016 v1.2).

Cada mensaje recibe una **clave de orden** determinista; sale primero la menor:

0. **Control** (`status`: ACK, desconexión, EVENTO_BUFFER; `ota/*`) antes que todo.
   Si se pierde, el backend y el nodo quedan desincronizados (P-6, propuesta de
   Desarrollo: la ficha dice "crítico siempre antes", pero un ACK perdido es peor).
1. **Severidad** (A.3, binaria por B.4): una lectura es crítica si su variable es
   `es_critica` en M09 o si, para alguna especie del área (la más restrictiva,
   RFC-012), su valor cae en la banda `critico` de RF-17 **o fuera de todas sus
   bandas**. Las tres bandas de un umbral son contiguas y excluyentes y cubren
   [valor_min, valor_max]; fuera de ellas el backend pinta ROJO
   (`SemaforoCalculator.calcular_por_niveles`), y el broker sigue la misma regla.
   Lo decide el broker; el `nivel_umbral` del Edge solo cuenta cuando el broker no
   tiene umbral para esa área y variable (pista re-validable).
2. **`nivel_criticidad` del área** (A.1, RF-20): ALTA > MEDIA > BAJA. El área es la
   del dispositivo emisor; solo visión puede indicar otra con `area_id`, y solo si
   es un área conocida (un Edge no puede adelantarse declarando un área ALTA).
3. **Tipo de dato**: control/negocio (eventos Edge de RF-55) > heartbeat (B.6) >
   telemetría escalar o de visión.
4. **Origen** (A.2): `TIEMPO_REAL` antes que `BUFFER_LOCAL`.
5. **Antigüedad**: primero el `timestamp_captura` más viejo; luego el serial, para
   que un mismo conjunto de mensajes salga siempre en el mismo orden (oráculo §4).

La **clase de admisión** (`Prioridad`) decide qué se rechaza con `RETENER` bajo
saturación: CONTROL nunca, CRITICO (dato crítico, heartbeat, evento Edge) a partir
de 2 × capacidad, NORMAL a partir de la capacidad.

Todo lo que viene de M09 se lee en bloque cada 10 min en una tarea de fondo
(`mantener_catalogo`, la arranca la cola), así clasificar un mensaje nunca espera a la
BD. Cada lectura se refresca por separado y, si falla (la BD puede ser la causa de la
saturación, o faltar un GRANT), se sigue con lo último leído. Hasta la primera lectura
no hay umbrales ni áreas: se ordena con la pista del Edge y la criticidad MEDIA.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Set
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
_cache: dict = {"criticas": frozenset(), "contexto": _VACIO}


async def _leer(nombre: str, consulta) -> None:
    try:
        async with async_session_factory() as session:
            _cache[nombre] = await asyncio.wait_for(consulta(session), _TIMEOUT_CONSULTA_S)
    except Exception:
        logger.warning("No se pudo leer %s de M09; se usa lo último leído.", nombre, exc_info=True)


async def refrescar_catalogo() -> None:
    """Relee de M09 lo que usa la prioridad; cada parte por separado."""
    await _leer("criticas", registry.variables_criticas)
    await _leer("contexto", registry.contexto_prioridad)


async def mantener_catalogo() -> None:
    """Tarea de fondo: refresca la caché cada `_REFRESCO_S` hasta que la cancelen."""
    while True:
        await refrescar_catalogo()
        await asyncio.sleep(_REFRESCO_S)


def _catalogo() -> tuple[Set[str], registry.ContextoPrioridad]:
    return _cache["criticas"], _cache["contexto"]


def _area(tipo: str, serial: str | None, data: dict,
          contexto: registry.ContextoPrioridad) -> int | None:
    area = data.get("area_id")
    if (tipo == get_settings().mqtt_topic_vision and isinstance(area, int)
            and not isinstance(area, bool) and area in contexto.criticidad_por_area):
        return area
    return contexto.area_por_serial.get(serial) if serial else None


def _es_critico(valor: float, niveles: registry.NivelesUmbral) -> bool:
    """En la banda `critico` o fuera de todas (el ROJO del backend). En el límite entre
    dos bandas cuenta la más grave."""
    dentro = {nivel for nivel, inferior, superior in niveles if inferior <= valor <= superior}
    return not dentro or NIVEL_UMBRAL_CRITICO in dentro


def _es_lectura_critica(data: dict, area: int | None, criticas: Set[str],
                        contexto: registry.ContextoPrioridad) -> bool:
    variable = data.get("variable")
    if variable in criticas:
        return True
    umbrales = contexto.umbrales.get((area, variable)) if area is not None else None
    if umbrales:
        valor = data.get("valor_crudo")
        if not isinstance(valor, (int, float)) or isinstance(valor, bool):
            return False
        return any(_es_critico(valor, niveles) for niveles in umbrales)
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

    criticas, contexto = _catalogo()
    area = _area(tipo, serial, data, contexto)
    critica = tipo == s.mqtt_topic_telemetry and _es_lectura_critica(
        data, area, criticas, contexto
    )
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
