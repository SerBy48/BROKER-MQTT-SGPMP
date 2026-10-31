# | Fecha      | Nombre   | Que se hizo                                                     |
# |------------|----------|-----------------------------------------------------------------|
# | 2026-10-30 | Arekkazu | RF-66: esqueleto del motor de inferencia M04 (M1)                |
"""Motor de inferencia M04 (``motor-ia``): proceso aparte del gateway MQTT.

Vive en este repo pero no comparte proceso con ``app``: una inferencia en CPU
bloquearía el bucle de aiomqtt, y las dependencias de ML no deben inflar la
imagen del broker. Se despliega como servicio propio detrás de Traefik
(``api.inmero.co/motor-ia[-test|-dev]`` con Strip Path; el HTTPS lo pone Dokploy).

Arranque::

    uvicorn motor_ia.main:crear_app --factory --host 0.0.0.0 --port 8100

Variables:

- ``IOT_INFERENCIA_M04_TOKEN`` (obligatoria): el mismo secreto que tiene el
  backend. Con él el backend llama a ``/v1/paquetes`` y el motor entrega sus
  resultados en ``POST /prediccion/inferencias`` del backend. Sin él no arranca.
- ``MOTOR_IA_ROOT_PATH``: prefijo que quita Traefik (ej. ``/motor-ia-test``),
  solo para que Swagger arme bien sus URL.

Estado (M1): todavía no carga modelos. ``/v1/paquetes`` responde 503
``MOTOR_SIN_MODELOS`` para no acusar recibo de paquetes que no procesa.
"""
from __future__ import annotations

import secrets
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_PREFIJO = "Bearer "


class MotorIASettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    iot_inferencia_m04_token: str = ""
    motor_ia_root_path: str = ""


class LotePaquetes(BaseModel):
    # TODO(IA-1): contrato provisional, el mismo cuerpo que envía hoy el backend
    # (motor_inferencia_http_adapter.py). Se cierra con la decisión D-B del plan.
    paquetes: list[dict[str, Any]] = Field(min_length=1, max_length=500)


def crear_app(settings: MotorIASettings | None = None) -> FastAPI:
    settings = settings or MotorIASettings()
    if not settings.iot_inferencia_m04_token:
        raise RuntimeError("Falta IOT_INFERENCIA_M04_TOKEN: el motor no arranca sin token.")
    esperado = settings.iot_inferencia_m04_token.encode("utf-8")

    def exigir_token(authorization: str | None = Header(default=None)) -> None:
        if not authorization or not authorization.startswith(_PREFIJO):
            raise HTTPException(401, {"error_code": "TOKEN_SERVICIO_INVALIDO",
                                      "message": "Se requiere el token del motor de inferencia."})
        # En bytes: compare_digest con str lanza TypeError ante caracteres no ASCII.
        recibido = authorization.removeprefix(_PREFIJO).encode("utf-8")
        if not secrets.compare_digest(recibido, esperado):
            raise HTTPException(401, {"error_code": "TOKEN_SERVICIO_INVALIDO",
                                      "message": "El token del motor de inferencia no es válido."})

    app = FastAPI(title="SGPMP - Motor de inferencia M04", root_path=settings.motor_ia_root_path)

    @app.get("/v1/healthz")
    def healthz() -> dict:
        return {"status": "ok", "modelos_cargados": 0}

    @app.post("/v1/paquetes", dependencies=[Depends(exigir_token)])
    def recibir_paquetes(lote: LotePaquetes) -> JSONResponse:
        # Un 2xx haría que el backend los marque ENVIADO y se perderían sin inferir.
        return JSONResponse(
            status_code=503,
            content={"error_code": "MOTOR_SIN_MODELOS",
                     "message": f"El motor aún no tiene modelos cargados; {len(lote.paquetes)} "
                                "paquete(s) no procesado(s)."},
        )

    return app
