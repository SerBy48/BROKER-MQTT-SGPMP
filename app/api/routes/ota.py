"""Endpoint para que el servidor web pida el aviso de OTA a un nodo (BRK-08)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import require_api_token
from app.core.errors import MqttNotConnectedError
from app.schemas import OtaNotifyRequest, OtaNotifyResponse
from app.services import ota

router = APIRouter()


@router.post(
    "/ota/notify",
    response_model=OtaNotifyResponse,
    status_code=202,
    dependencies=[Depends(require_api_token)],
)
async def ota_notify(request: OtaNotifyRequest) -> OtaNotifyResponse:
    try:
        return await ota.notificar(request)
    except MqttNotConnectedError as exc:
        # El backend deja el despliegue PENDIENTE y reintenta: no se publicó nada.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
