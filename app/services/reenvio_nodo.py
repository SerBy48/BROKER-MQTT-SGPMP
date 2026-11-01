# | Fecha      | Nombre   | Que se hizo |
# |------------|----------|-------------|
# | 2026-11-01 | Arekkazu | RF-23/RF-55: evento sin sensor resoluble se descarta con la causa |
# | 2026-11-01 | Arekkazu | RF-23/RF-55: sensor por defecto solo si ninguna variable trae `sensor` |
"""Mensajes del nodo que el broker reenvía a endpoints internos del backend (P2).

- `vision` (BRK-03, D4): vector de comportamiento → `POST /iot/telemetria/vision`.
- `evento` (BRK-04, D5): eventos Edge de RF-55 → `POST /iot/eventos-edge`.
- `ota/<evento>` (BRK-08, D6): reportes de la OTA de modelos →
  `POST /prediccion/ota/reportes`.

El broker no interpreta el contenido: lo valida el backend, que tiene la regla
de negocio. Lo único que fija es la identidad: el `serial` es siempre el del
topic, que ya autenticó la ACL de MQTT; si el nodo manda otro en el cuerpo, se
ignora (un nodo no puede reportar por otro).

El evento es la excepción: `RecibirEventoEdgeDTO` exige `device_id` y `sensor_id`
(PKs de modulo9) y el firmware solo conoce seriales, así que el broker los
resuelve antes de reenviar (BRK-12).
"""

from __future__ import annotations

import logging
from typing import Any

from app.db.engine import async_session_factory
from app.db.repositories import registry
from app.services import reenvio

logger = logging.getLogger(__name__)

RUTA_VISION = "/iot/telemetria/vision"
RUTA_EVENTOS_EDGE = "/iot/eventos-edge"
RUTA_REPORTES_OTA = "/prediccion/ota/reportes"

# D6: reportes que publica el nodo en `ota/<evento>`, y el aviso que recibe.
EVENTOS_OTA_NODO = (
    "status",
    "download_progress",
    "integrity_ok",
    "activated",
    "rollback",
    "critical_failure",
)
OTA_NOTIFY = "notify"


def _con_serial_del_topic(serial: str, data: dict[str, Any], tipo: str) -> dict[str, Any]:
    propio = data.get("serial")
    if propio is not None and propio != serial:
        logger.warning(
            "%s de %s trae serial=%r en el cuerpo: se usa el del topic.", tipo, serial, propio
        )
    return {**data, "serial": serial}


def vision(serial: str, data: dict[str, Any]) -> None:
    """Sobre de D4 tal cual (componentes, calidad, version_cascada, ventana)."""
    cuerpo = _con_serial_del_topic(serial, data, "vision")
    reenvio.reenviar(RUTA_VISION, cuerpo, f"Vector de visión de {serial}")


def _sensores_por_variable(
    variables: Any, sensores: dict[str, int]
) -> dict[str, int | None]:
    """`tipo_variable -> id_sensores` por el `sensor` que declara cada variable, en el orden
    del firmware (la primera es la que disparó la regla). Sin match queda `None`: un hueco
    explícito es mejor que un dato atribuido al sensor equivocado.

    TODO(IOT-1): confirmar que `sensor` del firmware se compara con `modulo9.sensores.nombre`.
    TODO(BRK-12): el plan pide un segundo intento por el tipo de variable, pero
    `sensores.categoria` (TEMPERATURA, HUMEDAD, ...) no tiene relación en BD con
    `variables_ambientales`; no se infiere un mapeo hasta que el catálogo lo defina.
    TODO(BRK-12): la ventana del Edge puede correlacionar lecturas de otro nodo (su
    `serial` por variable difiere del topic); hoy se buscan en el dispositivo del topic
    y quedan en None. Definir con IoT si se resuelven contra su propio serial.
    """
    if not isinstance(variables, list):
        return {}
    mapa: dict[str, int | None] = {}
    for variable in variables:
        if not isinstance(variable, dict) or not variable.get("tipo_variable"):
            continue
        mapa.setdefault(variable["tipo_variable"], sensores.get(variable.get("sensor") or ""))
    return mapa


def _sensores_declarados(variables: Any) -> list[str]:
    """Nombres de `sensor` que trae el evento, sin repetir y en orden."""
    if not isinstance(variables, list):
        return []
    nombres = (v.get("sensor") for v in variables if isinstance(v, dict))
    return list(dict.fromkeys(n for n in nombres if n))


async def evento_edge(serial: str, data: dict[str, Any]) -> None:
    """Evento clasificado por RF-55 en el Edge (AIOT-07), con `device_id` y `sensor_id`."""
    async with async_session_factory() as session:
        device_id = await registry.resolve_device_id(session, serial)
        if device_id is None:
            # El backend respondería 401 y lo anotaría en su bitácora de rechazos.
            logger.warning("Evento Edge de %s descartado: serial no registrado en modulo9.", serial)
            return
        sensores = await registry.sensores_por_nombre(session, device_id)
        por_variable = _sensores_por_variable(data.get("variables_involucradas"), sensores)
        # Raíz: la primaria; si no resolvió, la primera que sí.
        sensor_id = next((s for s in por_variable.values() if s is not None), None)
        declarados = _sensores_declarados(data.get("variables_involucradas"))
        if sensor_id is None and declarados:
            # Misma regla que la telemetría: un nombre que no existe se rechaza, nunca se
            # atribuye al sensor por defecto (ni siquiera con un único sensor activo).
            logger.warning(
                "Evento Edge de %s descartado: el dispositivo no tiene sensores llamados %s.",
                serial,
                declarados,
            )
            return
        if sensor_id is None:
            # Ninguna variable trae `sensor` (firmware anterior): la raíz sale de la misma
            # regla que la ingesta de telemetría, que solo resuelve si el dispositivo tiene un
            # único sensor activo. El mapa queda en None.
            sensor_id = await registry.resolve_sensor_id(session, device_id)
        if sensor_id is None:
            # `sensor_id` es obligatorio en el backend: reenviarlo sería un 422 seguro.
            logger.warning(
                "Evento Edge de %s descartado: sus variables no traen `sensor` y el "
                "dispositivo no tiene un único sensor activo.",
                serial,
            )
            return
    if sin_sensor := [v for v, s in por_variable.items() if s is None]:
        # INFO y no WARNING: con el firmware actual pasa en cada evento.
        logger.info(
            "Evento Edge de %s: variables sin sensor resuelto %s; sensor_id=%s.",
            serial,
            sin_sensor,
            sensor_id,
        )

    cuerpo = _con_serial_del_topic(serial, data, "evento")
    cuerpo["device_id"] = device_id
    cuerpo["sensor_id"] = sensor_id
    metadata = data.get("metadata_edge")
    if metadata is None or isinstance(metadata, dict):
        # VariableInvolucradaDTO descarta los campos extra: el mapa solo sobrevive aquí.
        del_broker = {"sensores_por_variable": por_variable, "device_id_resuelto": device_id}
        metadata = metadata or {}
        if choque := del_broker.keys() & metadata.keys():
            logger.warning(
                "Evento Edge de %s: metadata_edge ya trae %s; se conserva lo del Edge.",
                serial,
                sorted(choque),
            )
        cuerpo["metadata_edge"] = {**del_broker, **metadata}
    reenvio.reenviar(RUTA_EVENTOS_EDGE, cuerpo, f"Evento Edge de {serial}")


def reporte_ota(serial: str, evento: str, data: dict[str, Any]) -> None:
    """Reporte del nodo durante la OTA: `serial`, `id_despliegue_ota`, `componente`,
    `evento` y el resto como `detalle` (D6, paso 5)."""
    if evento == OTA_NOTIFY:
        return  # lo publica el propio broker; le llega por su suscripción a ota/+
    if evento not in EVENTOS_OTA_NODO:
        logger.warning("Reporte OTA desconocido de %s: ota/%s (ignorado)", serial, evento)
        return
    identidad = ("serial", "id_despliegue_ota", "componente")
    detalle = {k: v for k, v in data.items() if k not in identidad}
    cuerpo = {
        "serial": serial,
        "id_despliegue_ota": data.get("id_despliegue_ota"),
        "componente": data.get("componente"),
        "evento": evento,
        "detalle": detalle,
    }
    reenvio.reenviar(RUTA_REPORTES_OTA, cuerpo, f"Reporte OTA {evento} de {serial}")
