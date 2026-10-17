"""Lógica de ingesta: recibe payloads MQTT y los persiste vía los SPs de la BD."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError

from app.config import get_settings
from app.core.errors import DeviceNotFoundError, MqttNotConnectedError
from app.db.engine import async_session_factory
from app.db.repositories import registry
from app.db.repositories import telemetry as telemetry_repo
from app.mqtt import correlacion, presencia, publisher
from app.schemas import HeartbeatPayload, TelemetryPayload

logger = logging.getLogger(__name__)


# Contrato de retorno de fn_ingesta_telemetria (D2 de AIOT-DEV-03): el código de
# rechazo es el prefijo de `mensaje` antes de ":". Todos son definitivos (reintentar
# da lo mismo) y el duplicado ya está persistido: en ambos casos el nodo libera.
_RECHAZOS_DEFINITIVOS = frozenset({
    "ERROR_DUPLICADO",
    "ERROR_ESTRUCTURA",
    "ERROR_AUTENTICACION",
    "ERROR_TIEMPO",
    "ERROR_UNIDAD",
    "ERROR_SENSOR",
})
LIBERAR = "LIBERAR"
RETENER = "RETENER"
TIPO_RESULTADO_INGESTA = "RESULTADO_INGESTA"


@dataclass(frozen=True)
class ResultadoIngesta:
    estado: str  # modulo3.enum_transmiciones_mqqt_estado: EXITO / FALLIDO / TIMEOUT
    accion_buffer: str  # LIBERAR / RETENER
    codigo: str  # estado_calidad si se persistió; ERROR_* o ERROR_PERSISTENCIA si no
    mensaje: str
    id_telemetria: int | None = None


def _rechazo(codigo: str, mensaje: str) -> ResultadoIngesta:
    return ResultadoIngesta("FALLIDO", LIBERAR, codigo, f"{codigo}: {mensaje}")


def clasificar(fila: dict) -> ResultadoIngesta:
    """Traduce el retorno de fn_ingesta_telemetria a estado de transmisión y acción del nodo."""
    if fila["id_telemetria"] is not None:
        return ResultadoIngesta(
            "EXITO", LIBERAR, fila["estado_calidad"], fila["mensaje"] or "", fila["id_telemetria"]
        )
    mensaje = fila["mensaje"] or ""
    codigo = mensaje.split(":", 1)[0].strip()
    if codigo in _RECHAZOS_DEFINITIVOS:
        return ResultadoIngesta("FALLIDO", LIBERAR, codigo, mensaje)
    # Un código fuera del contrato: ante la duda el Edge conserva el dato (P5).
    logger.warning("fn_ingesta_telemetria devolvió un rechazo fuera del contrato: %r", mensaje)
    return ResultadoIngesta("FALLIDO", RETENER, codigo or "ERROR_DESCONOCIDO", mensaje)


async def _persistir(
    serial: str, data: dict, topic: str | None, payload_bytes: int
) -> ResultadoIngesta:
    async with async_session_factory() as session:
        device_id = await registry.resolve_device_id(session, serial)
        if device_id is None:
            # Sin dispositivo no hay fila en transmisiones_mqtt (id_dispositivo_iot NOT NULL).
            return _rechazo("ERROR_AUTENTICACION", f"Dispositivo no registrado: {serial}.")

        payload: TelemetryPayload | None = None
        try:
            payload = TelemetryPayload(**data)
        except ValidationError as exc:
            resultado = _rechazo(
                "ERROR_ESTRUCTURA", f"Payload inválido ({exc.error_count()} errores)."
            )
        else:
            variable_id = await registry.resolve_variable_id(session, payload.variable)
            sensor_id = await registry.resolve_sensor_id(session, device_id, payload.sensor)
            if variable_id is None:
                resultado = _rechazo(
                    "ERROR_ESTRUCTURA", f"Variable no registrada: {payload.variable}."
                )
            elif sensor_id is None:
                resultado = _rechazo(
                    "ERROR_AUTENTICACION", f"Sensor no resuelto: {payload.sensor}."
                )
            else:
                if payload.timestamp_envio is None:
                    payload = payload.model_copy(
                        update={"timestamp_envio": payload.timestamp_captura}
                    )
                resultado = clasificar(
                    await telemetry_repo.ingesta_telemetria(
                        session,
                        device_id=device_id,
                        sensor_id=sensor_id,
                        variable_id=variable_id,
                        payload=payload,
                    )
                )

        await telemetry_repo.log_transmission(
            session,
            device_id=device_id,
            topic=topic,
            qos=1,
            payload_bytes=payload_bytes,
            estado=resultado.estado,
            rssi=payload.calidad_senal_rssi if payload else None,
            snr=payload.calidad_senal_snr if payload else None,
            error_descripcion=None if resultado.estado == "EXITO" else resultado.mensaje,
        )
        # Lectura y transmisión van juntas: si el commit falla, la lectura no quedó
        # y el nodo debe retenerla (rama de excepción de ingest_telemetry).
        await session.commit()
        return resultado


async def _registrar_falla_persistencia(serial: str, topic: str | None, payload_bytes: int,
                                        estado: str, mensaje: str) -> None:
    """Best-effort: con la BD caída esta escritura también falla, y está bien."""
    try:
        async with async_session_factory() as session:
            device_id = await registry.resolve_device_id(session, serial)
            if device_id is None:
                return
            await telemetry_repo.log_transmission(
                session, device_id=device_id, topic=topic, qos=1, payload_bytes=payload_bytes,
                estado=estado, error_descripcion=mensaje,
            )
            await session.commit()
    except Exception:
        logger.warning("No se pudo registrar la transmisión fallida de %s.", serial, exc_info=True)


async def _avisar_nodo(serial: str, data: dict, resultado: ResultadoIngesta) -> None:
    """Aviso del resultado al nodo (BRK-01). El nodo correlaciona por `id_mensaje` si lo
    mandó, o por (sensor, variable, timestamp_captura), la misma clave del duplicado."""
    aviso = {
        "tipo_mensaje": TIPO_RESULTADO_INGESTA,
        "id_mensaje": data.get("id_mensaje"),
        "sensor": data.get("sensor"),
        "variable": data.get("variable"),
        "timestamp_captura": data.get("timestamp_captura"),
        "resultado": resultado.estado,
        "accion_buffer": resultado.accion_buffer,
        "codigo": resultado.codigo,
        "mensaje": resultado.mensaje,
        "id_telemetria": resultado.id_telemetria,
    }
    try:
        await publisher.publish_resultado_ingesta(serial, aviso)
    except MqttNotConnectedError:
        # Sin aviso el nodo no libera nada: retener es el lado seguro.
        logger.warning("MQTT no conectado: no se avisó a %s el resultado de su ingesta.", serial)


async def ingest_telemetry(serial: str, data: dict, topic: str | None = None) -> ResultadoIngesta:
    """Persiste una lectura por fn_ingesta_telemetria y avisa el resultado real al nodo.

    Antes se registraba EXITO aunque la función rechazara la lectura, y el Edge
    liberaba su buffer con el PUBACK de Mosquitto. Ahora `transmisiones_mqtt`
    refleja lo que pasó y el nodo solo libera lo persistido o lo rechazado de
    forma definitiva; ante una falla de persistencia retiene y reintenta (RF-54).
    """
    logger.debug("Ingestando telemetría: serial=%s topic=%s", serial, topic)
    payload_bytes = len(json.dumps(data).encode("utf-8"))
    try:
        resultado = await _persistir(serial, data, topic, payload_bytes)
    except Exception as exc:
        estado = "TIMEOUT" if isinstance(exc, TimeoutError) else "FALLIDO"
        logger.exception("Falla de persistencia ingestando telemetría de %s", serial)
        resultado = ResultadoIngesta(
            estado, RETENER, "ERROR_PERSISTENCIA",
            f"ERROR_PERSISTENCIA: no se pudo persistir la lectura ({type(exc).__name__}).",
        )
        await _registrar_falla_persistencia(serial, topic, payload_bytes, estado, resultado.mensaje)

    log = logger.info if resultado.estado == "EXITO" else logger.warning
    log("Telemetría de %s: %s %s (%s)", serial, resultado.estado, resultado.codigo,
        resultado.accion_buffer)
    await _avisar_nodo(serial, data, resultado)
    return resultado


async def ingest_heartbeat(serial: str, data: dict, topic: str | None = None) -> int:
    logger.debug("Ingestando heartbeat: serial=%s topic=%s", serial, topic)
    # TC-M09-63: el Edge que había avisado su desconexión volvió (antes de tocar
    # la BD: aunque el heartbeat no se pueda guardar, el Edge está conectado).
    presencia.marcar_conectado(serial)
    payload = HeartbeatPayload(**data)
    async with async_session_factory() as session:
        device_id = await registry.resolve_device_id(session, serial)
        if device_id is None:
            raise DeviceNotFoundError(serial)

        fecha_registro = payload.fecha_registro or datetime.now(UTC)
        heartbeat_id = await telemetry_repo.insert_heartbeat(
            session,
            device_id=device_id,
            payload=payload,
            fecha_registro=fecha_registro,
        )

        await telemetry_repo.log_transmission(
            session,
            device_id=device_id,
            topic=topic,
            qos=1,
            payload_bytes=len(json.dumps(data).encode("utf-8")),
            estado="EXITO",
            rssi=payload.calidad_senal_rssi,
            snr=payload.calidad_senal_snr,
        )

        await session.commit()
        logger.info("Heartbeat ingestado (%s): id=%s", serial, heartbeat_id)
        return heartbeat_id


_TIPOS_ACK = {correlacion.ACK_CONFIGURACION, correlacion.ACK_UMBRAL}


async def ingest_status(serial: str, data: dict) -> None:
    """Procesa un mensaje del topic `<prefix>/<serial>/status`.

    Contrato propuesto para el ACK de configuración (RF-23, confirmar con
    equipo IoT cuando los topics estén cerrados):
    ``{"tipo_mensaje": "ACK_CONFIGURACION", "resultado": "OK", "id_comando": "<id del comando>"}``.
    El `id_comando` es el que llegó en el comando; solo un ACK que lo devuelva
    resuelve la espera (TC-M09-252, anti-replay). El ACK de un umbral (RF-17)
    es igual con ``"tipo_mensaje": "ACK_UMBRAL"``; cada tipo solo resuelve
    comandos de su mismo tipo.

    Si hay una espera de comando pendiente para este `serial`
    (dispatch_command la crea al publicar), se resuelve acá -- eso es lo que
    permite que /v1/commands responda APLICADA antes de agotar el timeout.
    No transiciona nada en modulo9.configuraciones_remotas: esa tabla es
    propiedad del backend, que actualiza su propia fila con el resultado que
    /v1/commands le devuelve en la misma respuesta HTTP.
    """
    logger.info("Status recibido de %s: %s", serial, data)
    tipo_mensaje = data.get("tipo_mensaje")
    if tipo_mensaje == presencia.TIPO_DESCONEXION:
        # Last Will o cierre ordenado del Edge (TC-M09-63): los comandos que van
        # por él quedan PENDIENTE al instante en vez de esperar el ACK.
        presencia.marcar_desconectado(serial)
        return
    if tipo_mensaje in _TIPOS_ACK and data.get("resultado") == "OK":
        resuelto = correlacion.resolver_ack(
            serial,
            data.get("id_comando"),
            tipo_ack=tipo_mensaje,
            exigir_id=get_settings().mqtt_ack_requiere_id_comando,
        )
        if not resuelto:
            logger.info(
                "ACK de %s no resolvió ninguna espera (expiró, no había ninguna o no "
                "correspondía al comando en vuelo).",
                serial,
            )
