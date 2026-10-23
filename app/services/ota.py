"""OTA de modelos por componente: aviso al nodo (BRK-08, D6 de AIOT-DEV-03).

El backend orquesta la distribución (DEV-15) y le pide al broker publicar
`ota/notify` por cada nodo. El broker publica y responde: no espera la
instalación, que el nodo reporta después por `ota/<evento>` (ver reenvio_nodo).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from app.mqtt import publisher
from app.schemas import OtaNotifyRequest, OtaNotifyResponse

logger = logging.getLogger(__name__)

TIPO_OTA_NOTIFY = "OTA_NOTIFY"


async def notificar(request: OtaNotifyRequest) -> OtaNotifyResponse:
    """Publica el aviso. Lanza MqttNotConnectedError si el broker no está conectado."""
    id_mensaje = uuid.uuid4().hex
    payload = {
        "tipo_mensaje": TIPO_OTA_NOTIFY,
        # Como en los comandos (TC-M09-252): el nodo descarta un aviso ya procesado o viejo.
        "id_mensaje": id_mensaje,
        "emitido_en": datetime.now(UTC).isoformat(),
        **request.model_dump(mode="json", exclude={"serial"}),
    }
    topic = await publisher.publish_ota_notify(request.serial, payload)
    logger.info(
        "OTA %s (%s %s) avisada a %s",
        request.id_despliegue_ota,
        request.componente,
        request.version,
        request.serial,
    )
    return OtaNotifyResponse(serial=request.serial, topic=topic, id_mensaje=id_mensaje)
