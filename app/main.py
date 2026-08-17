"""Punto de entrada de SGPMP.

Levanta la API HTTPS (FastAPI) y, en el ciclo de vida, arranca/para la
conexión MQTT hacia Mosquitto.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.router import api_router
from app.core.logging import setup_logging
from app.mqtt.client import mqtt_gateway


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    mqtt_gateway.start()
    try:
        yield
    finally:
        await mqtt_gateway.stop()


app = FastAPI(
    title="SGPMP",
    description="Gateway MQTT <-> HTTPS para la plataforma AIoT de monitoreo de salud animal",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(api_router, prefix="/v1")


def main() -> None:
    import uvicorn

    from app.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
