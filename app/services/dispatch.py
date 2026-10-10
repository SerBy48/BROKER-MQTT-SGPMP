"""Lógica de despacho de comandos: HTTPS -> publish MQTT -> espera de ACK.

El broker ya NO escribe en modulo9.configuraciones_remotas -- esa tabla es
propiedad exclusiva del backend (sgpmp-backend crea la fila PENDIENTE antes
de llamar acá y la actualiza con el resultado). Este servicio es puro
gateway de protocolo: HTTP -> MQTT -> espera acotada de ACK -> HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from app.config import get_settings
from app.core.errors import DeviceNotFoundError, MqttNotConnectedError
from app.db.engine import async_session_factory
from app.db.repositories import registry
from app.mqtt import correlacion, publisher
from app.schemas import CommandRequest, CommandResponse, UmbralCommandRequest
from app.services import credenciales_mqtt

logger = logging.getLogger(__name__)

_ESTADO_ALCANZABLE = "ACTIVO"


async def _sin_conexion(receptor: str) -> bool:
    # Atajo best-effort: ante cualquier falla se publica y se espera el ACK, como antes.
    try:
        return await credenciales_mqtt.sin_conexion(receptor)
    except Exception:
        logger.warning(
            "No se pudo consultar la conexión de %s; se publica igual.", receptor, exc_info=True
        )
        return False


def _cuerpo_comando(request: CommandRequest | UmbralCommandRequest) -> tuple[dict, str]:
    """Campos propios del comando según su `origen`, y el tipo de ACK que lo confirma.

    El comando de configuración (RF-23) se publica igual que antes, sin
    `tipo_comando`, para no romper firmware que ya lo procesa. El de umbral
    (RF-17) lleva `tipo_comando: "UMBRAL_AMBIENTAL"` para que el Edge lo
    distinga en el mismo topic `command` (no se agregan topics: la ACL de la
    credencial del Edge ya cubre `command`/`status` de su serial).
    """
    if isinstance(request, UmbralCommandRequest):
        return (
            {
                "tipo_comando": "UMBRAL_AMBIENTAL",
                "id_umbral_ambiental": request.id_umbral_ambiental,
                "version": request.version.isoformat() if request.version else None,
                "variable": request.variable,
                "unidad": request.unidad,
                "valor_min": request.valor_min,
                "valor_max": request.valor_max,
                "niveles": [nivel.model_dump() for nivel in request.niveles],
            },
            correlacion.ACK_UMBRAL,
        )
    if request.fps is not None:
        # Cámara (RF-23 v1.1): mismo comando y mismo ACK, solo cambia el parámetro.
        return {"fps": request.fps}, correlacion.ACK_CONFIGURACION
    return (
        {
            "frecuencia_captura": request.frecuencia_captura,
            "intervalo_transmision": request.intervalo_transmision,
        },
        correlacion.ACK_CONFIGURACION,
    )


async def dispatch_command(request: CommandRequest | UmbralCommandRequest) -> CommandResponse:
    logger.debug("Despachando comando: serial=%s", request.serial)
    async with async_session_factory() as session:
        device_id = await registry.resolve_device_id(session, request.serial)
        if device_id is None:
            raise DeviceNotFoundError(request.serial)

        estado_dispositivo = await registry.resolve_device_state(session, request.serial)
        registro = (await registry.mapa_dispositivos(session)).get(request.serial)

    if estado_dispositivo != _ESTADO_ALCANZABLE:
        logger.info(
            "Dispositivo %s no está %s (estado=%s); no se publica, PENDIENTE.",
            request.serial,
            _ESTADO_ALCANZABLE,
            estado_dispositivo,
        )
        return CommandResponse(
            serial=request.serial,
            estado="PENDIENTE",
            mensaje="Dispositivo offline. La configuración quedará pendiente hasta que reconecte.",
        )

    # El estado en BD tarda en pasar a offline cuando el Edge se apaga; sin este
    # chequeo se publica y se esperan los 30 s del ACK para terminar en NO_CONF.
    receptor = (registro and registro.serial_gateway) or request.serial
    if await _sin_conexion(receptor):
        logger.info(
            "%s no está conectado al broker; no se publica a %s, PENDIENTE.",
            receptor,
            request.serial,
        )
        return CommandResponse(
            serial=request.serial,
            estado="PENDIENTE",
            mensaje=(
                f"{receptor} no está conectado al broker. "
                "La configuración quedará pendiente hasta que reconecte."
            ),
        )

    settings = get_settings()
    # TC-M09-252 (anti-replay): `id_comando` correlaciona el ACK con ESTE comando y
    # `emitido_en` deja que el dispositivo descarte un comando capturado y reenviado
    # más tarde (ver GUIA_CONEXION_IOT.md).
    id_comando = uuid.uuid4().hex
    cuerpo, tipo_ack = _cuerpo_comando(request)
    payload = {
        "id_comando": id_comando,
        "emitido_en": datetime.now(UTC).isoformat(),
        **cuerpo,
    }
    future = correlacion.crear_espera(request.serial, id_comando, tipo_ack)
    try:
        topic = await publisher.publish_command(request.serial, payload)
    except MqttNotConnectedError:
        correlacion.limpiar_espera(request.serial, id_comando)
        logger.error("Broker MQTT no conectado; degradando %s a PENDIENTE.", request.serial)
        return CommandResponse(
            serial=request.serial,
            estado="PENDIENTE",
            mensaje="Broker MQTT no disponible. La configuración quedará pendiente.",
        )

    try:
        await asyncio.wait_for(future, timeout=settings.mqtt_ack_timeout_seconds)
        logger.info("ACK recibido de %s dentro del timeout.", request.serial)
        return CommandResponse(
            serial=request.serial,
            topic=topic,
            estado="APLICADA",
            mensaje="El dispositivo confirmó la recepción de la configuración.",
        )
    except TimeoutError:
        logger.error("Sin ACK de %s tras %ss.", request.serial, settings.mqtt_ack_timeout_seconds)
        return CommandResponse(
            serial=request.serial,
            topic=topic,
            estado="NO_CONF",
            mensaje="El comando fue enviado pero el dispositivo no confirmó la recepción a tiempo.",
        )
    finally:
        correlacion.limpiar_espera(request.serial, id_comando)
