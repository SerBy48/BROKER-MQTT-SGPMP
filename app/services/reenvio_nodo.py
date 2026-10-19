"""Mensajes del nodo que el broker reenvía a endpoints internos del backend (P2).

- `vision` (BRK-03, D4): vector de comportamiento → `POST /iot/telemetria/vision`.
- `evento` (BRK-04, D5): eventos Edge de RF-55 → `POST /iot/eventos-edge`.
- `ota/<evento>` (BRK-08, D6): reportes de la OTA de modelos →
  `POST /prediccion/ota/reportes`.

El broker no interpreta el contenido: lo valida el backend, que tiene la regla
de negocio. Lo único que fija es la identidad: el `serial` es siempre el del
topic, que ya autenticó la ACL de MQTT; si el nodo manda otro en el cuerpo, se
ignora (un nodo no puede reportar por otro).
"""

from __future__ import annotations

import logging
from typing import Any

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


def evento_edge(serial: str, data: dict[str, Any]) -> None:
    """Evento clasificado por RF-55 en el Edge (AIOT-07)."""
    cuerpo = _con_serial_del_topic(serial, data, "evento")
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
