"""Router raíz de la API HTTP (capa de integración con el servidor web)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import commands, credenciales, devices, health

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(commands.router, tags=["commands"])
api_router.include_router(devices.router, tags=["devices"])
api_router.include_router(credenciales.router, tags=["credenciales"])
