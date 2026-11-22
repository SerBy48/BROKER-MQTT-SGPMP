"""Cliente HTTP del broker hacia sgpmp-backend (BRK-02, P2/P3 de AIOT-DEV-03).

Lo de alto volumen (telemetría, heartbeat) el broker lo escribe directo en la
BD. Los eventos de control de bajo volumen (buffer, visión, eventos Edge,
reportes OTA) se reenvían a un endpoint interno del backend, que tiene la lógica
de negocio. El broker se autentica con el token de servicio
``BROKER_SERVICE_TOKEN`` en ``Authorization: Bearer``: el mismo valor que el
backend tiene en su entorno, nunca en el repositorio ni en los logs.

Este módulo solo habla HTTP saliente: no toca MQTT ni la BD. Nunca lanza por
fallas del backend: devuelve un ``RespuestaBackend`` con ``ok=False`` y deja el
error en el log, para que quien reenvía decida qué hacer con el mensaje.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

_cliente: httpx.AsyncClient | None = None


@dataclass(frozen=True)
class RespuestaBackend:
    ok: bool
    status: int | None = None
    cuerpo: Any = None
    error: str | None = None


def _obtener_cliente() -> httpx.AsyncClient | None:
    global _cliente
    settings = get_settings()
    if not settings.backend_url or not settings.broker_service_token:
        return None
    if _cliente is None:
        _cliente = httpx.AsyncClient(
            base_url=settings.backend_url.rstrip("/"),
            timeout=settings.backend_timeout_seconds,
            headers={"Authorization": f"Bearer {settings.broker_service_token}"},
        )
    return _cliente


async def enviar(ruta: str, cuerpo: dict[str, Any]) -> RespuestaBackend:
    """POST de ``cuerpo`` (ya serializable a JSON) a ``ruta`` del backend."""
    cliente = _obtener_cliente()
    if cliente is None:
        logger.error("BACKEND_URL/BROKER_SERVICE_TOKEN no configurados: %s no se reenvía.", ruta)
        return RespuestaBackend(ok=False, error="integración con el backend no configurada")
    try:
        respuesta = await cliente.post(ruta, json=cuerpo)
    except httpx.TimeoutException:
        logger.error("El backend no respondió %s en %ss.", ruta, cliente.timeout.read)
        return RespuestaBackend(ok=False, error="timeout")
    except httpx.HTTPError as exc:
        logger.error("No se pudo contactar al backend en %s: %s", ruta, exc)
        return RespuestaBackend(ok=False, error=f"backend no disponible: {type(exc).__name__}")

    try:
        datos = respuesta.json()
    except ValueError:
        datos = None
    if respuesta.is_error:
        # El cuerpo de error del backend es {error_code, message, field}: nunca trae el token.
        logger.error("El backend respondió %d en %s: %s", respuesta.status_code, ruta, datos)
        return RespuestaBackend(ok=False, status=respuesta.status_code, cuerpo=datos,
                                error=f"HTTP {respuesta.status_code}")
    logger.debug("Backend %s -> %d", ruta, respuesta.status_code)
    return RespuestaBackend(ok=True, status=respuesta.status_code, cuerpo=datos)


async def cerrar() -> None:
    global _cliente
    if _cliente is not None:
        await _cliente.aclose()
        _cliente = None
