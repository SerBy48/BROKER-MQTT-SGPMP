"""Credencial MQTT del Gateway Edge (SEG-BROKER-03, TC-M09-250/251).

La usa sgpmp-backend (RBAC y auditoría viven allá); este gateway es el único
que habla con Mosquitto. Los seriales que cubre cada credencial salen de
modulo9 (el Edge y los dispositivos que apuntan a él), no de la petición. La
contraseña se devuelve una sola vez y no se guarda.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Response

from app.api.dependencies import require_api_token
from app.core.errors import (
    DeviceDependsOnGatewayError,
    DeviceInactiveError,
    DeviceNotFoundError,
    DynsecError,
    MqttNotConnectedError,
    UsuarioReservadoError,
)
from app.schemas import PATRON_SERIAL, CredencialResponse, EstadoCredencialResponse
from app.services import credenciales_mqtt

router = APIRouter(dependencies=[Depends(require_api_token)])

SerialPath = Annotated[str, Path(pattern=PATRON_SERIAL)]


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, DeviceNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, DeviceInactiveError | UsuarioReservadoError | DeviceDependsOnGatewayError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=503, detail=f"Broker MQTT no disponible: {exc}")


_ERRORES = (
    DeviceDependsOnGatewayError,
    DeviceNotFoundError,
    DeviceInactiveError,
    UsuarioReservadoError,
    DynsecError,
    MqttNotConnectedError,
)


@router.post("/devices/{serial}/credential", status_code=201, response_model=CredencialResponse)
async def emitir_credencial(serial: SerialPath, response: Response) -> CredencialResponse:
    response.headers["Cache-Control"] = "no-store"
    try:
        credencial = await credenciales_mqtt.emitir(serial)
    except _ERRORES as exc:
        raise _http(exc) from exc
    return CredencialResponse(**vars(credencial))


@router.post("/devices/{serial}/credential/sync", status_code=204)
async def sincronizar_credencial(serial: SerialPath) -> None:
    """Alinea los topics de la credencial con modulo9 sin rotar la clave. La llama
    el backend al asignar o quitar dispositivos de un Edge."""
    try:
        tiene = await credenciales_mqtt.sincronizar_credencial(serial)
    except _ERRORES as exc:
        raise _http(exc) from exc
    if not tiene:
        raise HTTPException(status_code=404, detail=f"{serial} no tiene credencial MQTT propia")


@router.get("/devices/{serial}/credential", response_model=EstadoCredencialResponse)
async def consultar_credencial(serial: SerialPath) -> EstadoCredencialResponse:
    try:
        estado = await credenciales_mqtt.consultar(serial)
    except _ERRORES as exc:
        raise _http(exc) from exc
    if estado is None:
        raise HTTPException(status_code=404, detail=f"{serial} no tiene credencial MQTT propia")
    return EstadoCredencialResponse(**vars(estado))


@router.delete("/devices/{serial}/credential", status_code=204)
async def revocar_credencial(serial: SerialPath) -> None:
    try:
        await credenciales_mqtt.revocar(serial)
    except _ERRORES as exc:
        raise _http(exc) from exc
