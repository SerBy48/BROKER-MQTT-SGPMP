"""Endpoint para enviar comandos del servidor web hacia los dispositivos IoT."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import require_api_token
from app.core.errors import DeviceNotFoundError
from app.schemas import CommandRequest, CommandResponse
from app.services.dispatch import dispatch_command

router = APIRouter()


@router.post(
    "/commands",
    response_model=CommandResponse,
    dependencies=[Depends(require_api_token)],
)
async def create_command(request: CommandRequest) -> CommandResponse:
    try:
        return await dispatch_command(request)
    except DeviceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
