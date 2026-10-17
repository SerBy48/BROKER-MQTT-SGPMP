"""Correlación de un comando publicado con su ACK (topic status).

Cada comando lleva un `id_comando` único (TC-M09-252) y solo el ACK que lo
devuelve resuelve la espera. Sin eso, un ACK capturado y reenviado (replay) --
o uno forjado por cualquiera con la credencial compartida de dispositivos, que
puede escribir en `status` de cualquier serial -- resolvía como APLICADA el
comando que estuviera en vuelo en ese momento.

RF-17 (INC-M09-104-G29): las esperas se indexan por `(serial, id_comando)`, no
solo por `serial`. Antes había una sola espera por serial, garantizada por el
índice único de configuraciones PENDIENTE del backend; con los umbrales, un
mismo Gateway Edge recibe varios comandos seguidos (uno por variable de la
especie, o uno cruzado con una configuración RF-23) y la segunda espera pisaba
a la primera, que terminaba en NO_CONF aunque el Edge sí hubiera confirmado.

Cada espera recuerda además qué `tipo_ack` espera ("ACK_CONFIGURACION" o
"ACK_UMBRAL"): un ACK de un tipo nunca resuelve un comando del otro.

ponytail: dict en memoria de un solo proceso -- si el broker corre en
múltiples réplicas sin sticky routing, la correlación se rompe. Solución
futura: backend compartido (Redis pub/sub), no necesaria para esta entrega.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

ACK_CONFIGURACION = "ACK_CONFIGURACION"
ACK_UMBRAL = "ACK_UMBRAL"


@dataclass
class _Espera:
    tipo_ack: str
    future: asyncio.Future[None]


# serial -> id_comando -> espera
_pending_acks: dict[str, dict[str, _Espera]] = {}


def crear_espera(
    serial: str, id_comando: str, tipo_ack: str = ACK_CONFIGURACION
) -> asyncio.Future[None]:
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    _pending_acks.setdefault(serial, {})[id_comando] = _Espera(tipo_ack=tipo_ack, future=future)
    logger.debug(
        "Espera de ACK creada para %s (id_comando=%s, tipo=%s)", serial, id_comando, tipo_ack
    )
    return future


def resolver_ack(
    serial: str,
    id_comando: str | None = None,
    *,
    tipo_ack: str = ACK_CONFIGURACION,
    exigir_id: bool = False,
) -> bool:
    """Resuelve la espera del comando de `serial` al que corresponde este ACK.

    Devuelve False, sin tocar ninguna espera, cuando no hay comando en vuelo de
    ese tipo, cuando el `id_comando` no corresponde a ninguno (replay/forjado),
    o cuando falta el id y `exigir_id` está activo. Un ACK sin id (firmware que
    aún no lo devuelve) solo resuelve si hay exactamente un comando de ese tipo
    en vuelo para el serial: con más de uno sería adivinar cuál confirmó.
    """
    esperas = {
        id_: espera
        for id_, espera in _pending_acks.get(serial, {}).items()
        if espera.tipo_ack == tipo_ack and not espera.future.done()
    }
    if not esperas:
        logger.debug("ACK %s de %s sin espera activa", tipo_ack, serial)
        return False

    if id_comando is None:
        if exigir_id:
            logger.warning(
                "ACK de %s sin id_comando ignorado: MQTT_ACK_REQUIERE_ID_COMANDO está activo.",
                serial,
            )
            return False
        if len(esperas) > 1:
            logger.warning(
                "ACK %s de %s sin id_comando ignorado: hay %d comandos en vuelo y no se "
                "puede saber cuál confirmó.",
                tipo_ack,
                serial,
                len(esperas),
            )
            return False
        logger.warning(
            "ACK de %s sin id_comando aceptado por compatibilidad; el firmware debe devolverlo.",
            serial,
        )
        (espera,) = esperas.values()
    else:
        espera = esperas.get(id_comando)
        if espera is None:
            logger.warning(
                "ACK %s de %s con id_comando=%s que no corresponde a ningún comando en "
                "vuelo: ignorado, posible replay.",
                tipo_ack,
                serial,
                id_comando,
            )
            return False

    espera.future.set_result(None)
    logger.debug("ACK de %s resuelto", serial)
    return True


def limpiar_espera(serial: str, id_comando: str) -> None:
    esperas = _pending_acks.get(serial)
    if esperas is None:
        return
    esperas.pop(id_comando, None)
    if not esperas:
        del _pending_acks[serial]
    logger.debug("Espera de ACK limpiada para %s (id_comando=%s)", serial, id_comando)
