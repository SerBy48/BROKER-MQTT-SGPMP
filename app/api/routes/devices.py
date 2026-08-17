"""Consulta de estado de dispositivos IoT (espejo de la BD compartida)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import require_api_token
from app.db.engine import async_session_factory
from app.db.repositories import registry

router = APIRouter()


@router.get("/devices", dependencies=[Depends(require_api_token)])
async def list_devices() -> list[dict]:
    async with async_session_factory() as session:
        return await registry.list_device_states(session)
