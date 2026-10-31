# | Fecha      | Nombre   | Que se hizo                                                     |
# |------------|----------|-----------------------------------------------------------------|
# | 2026-10-30 | Arekkazu | RF-66: esqueleto del motor de inferencia M04 (M1)                |
# | 2026-10-30 | Arekkazu | RF-66: inferencia individual ET-02 y entrega al backend (M2)     |
# | 2026-10-31 | Arekkazu | RF-17 C3: la inferencia usa los rangos de M09 del paquete         |
# | 2026-10-31 | SerBy48  | RF-66: log visible bajo uvicorn y activo inválido no tumba lote  |
"""Motor de inferencia M04 (``motor-ia``): proceso aparte del gateway MQTT.

Vive en este repo pero no comparte proceso con ``app``: una inferencia en CPU
bloquearía el bucle de aiomqtt, y las dependencias de ML no deben inflar la
imagen del broker (van en el extra ``[motor-ia]``).

Arranque::

    uvicorn motor_ia.main:crear_app --factory --host 0.0.0.0 --port 8100

Variables:

- ``IOT_INFERENCIA_M04_TOKEN`` (obligatoria): el mismo secreto que tiene el backend,
  en los dos sentidos. Sin él no arranca.
- ``BACKEND_URL``: URL base del backend en cualquier dominio, con su prefijo
  (``https://api.inmero.co/api-sgpmp-test``, ``https://api.arekkazu.top``…). Obligatoria
  si hay modelos: ahí se entregan los resultados.
- ``MOTOR_IA_DIR_MODELOS``: carpeta con ``catalogo.json`` y los artefactos de ET-02
  (ver ``motor_ia.individual``). Vacía → sin modelos.

El motor responde en cualquier dominio (no valida el Host) y en la raíz o detrás de un prefijo
que quite el proxy (Strip Path). Para que Swagger arme bien sus URL detrás del prefijo:
``uvicorn ... --root-path /motor-ia-test``.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from motor_ia.backend import BackendNoDisponibleError, BackendResultadosClient
from motor_ia.individual import a_resultado_rf66, cargar_catalogo, inferir

logger = logging.getLogger(__name__)

_PREFIJO = "Bearer "


class MotorIASettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    iot_inferencia_m04_token: str = ""
    motor_ia_dir_modelos: str = ""
    backend_url: str = ""


class LotePaquetes(BaseModel):
    # Contrato IA-1 v1 (decisión D-B): el backend envía cada paquete de RF-56 con
    # ``id_activo_biologico``, ``lecturas`` (ventana del activo en formato largo) y, si la especie
    # tiene umbral en M09 · RF-17, ``rangos_especie`` (punto C3 de ET-02). Se aceptan
    # campos de más para no romper si el backend agrega contexto.
    paquetes: list[dict[str, Any]] = Field(min_length=1, max_length=500)


def crear_app(
    settings: MotorIASettings | None = None, transport_backend: httpx.BaseTransport | None = None
) -> FastAPI:
    # uvicorn solo configura sus propios loggers: sin esto los INFO del motor no salen.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    settings = settings or MotorIASettings()
    if not settings.iot_inferencia_m04_token:
        raise RuntimeError("Falta IOT_INFERENCIA_M04_TOKEN: el motor no arranca sin token.")
    esperado = settings.iot_inferencia_m04_token.encode("utf-8")

    modelos = cargar_catalogo(settings.motor_ia_dir_modelos)
    backend = None
    if modelos:
        if not settings.backend_url:
            raise RuntimeError(
                "Falta BACKEND_URL: hay modelos cargados y no hay dónde entregar resultados."
            )
        backend = BackendResultadosClient(
            settings.backend_url, settings.iot_inferencia_m04_token, transport_backend
        )
        logger.info(
            "RF-66: %d modelo(s) cargado(s); resultados hacia %s", len(modelos), backend.url
        )

    def exigir_token(authorization: str | None = Header(default=None)) -> None:
        if not authorization or not authorization.startswith(_PREFIJO):
            raise HTTPException(
                401,
                {
                    "error_code": "TOKEN_SERVICIO_INVALIDO",
                    "message": "Se requiere el token del motor de inferencia.",
                },
            )
        # En bytes: compare_digest con str lanza TypeError ante caracteres no ASCII.
        recibido = authorization.removeprefix(_PREFIJO).encode("utf-8")
        if not secrets.compare_digest(recibido, esperado):
            raise HTTPException(
                401,
                {
                    "error_code": "TOKEN_SERVICIO_INVALIDO",
                    "message": "El token del motor de inferencia no es válido.",
                },
            )

    app = FastAPI(title="SGPMP - Motor de inferencia M04")

    @app.get("/v1/healthz")
    def healthz() -> dict:
        return {
            "status": "ok",
            "modelos_cargados": len(modelos),
            "backend": backend.url if backend else None,
        }

    @app.post("/v1/paquetes", dependencies=[Depends(exigir_token)])
    def recibir_paquetes(lote: LotePaquetes) -> JSONResponse:
        if backend is None:
            # Un 2xx haría que el backend los marque ENVIADO y se perderían sin inferir.
            return JSONResponse(
                status_code=503,
                content={
                    "error_code": "MOTOR_SIN_MODELOS",
                    "message": f"El motor aún no tiene modelos cargados; {len(lote.paquetes)} "
                    "paquete(s) no procesado(s).",
                },
            )
        # Un evento Edge genera un paquete por variable: se infiere una vez por activo, con la
        # ventana del paquete más reciente del lote (llegan en orden cronológico).
        ultimo: dict[int, dict] = {}
        for p in lote.paquetes:
            id_activo = p.get("id_activo_biologico")
            if isinstance(id_activo, int) and id_activo in modelos:
                ultimo[id_activo] = p

        registrados, rechazados, sin_resultado = [], 0, 0
        try:
            for id_activo, p in ultimo.items():
                modelo = modelos[id_activo]
                salida = inferir(modelo, p.get("lecturas") or [], p.get("rangos_especie"))
                if salida is None:
                    sin_resultado += 1
                    continue
                resultado = a_resultado_rf66(
                    modelo, *salida, id_paquete=p.get("id_paquetes_inferencia")
                )
                if backend.registrar(resultado):
                    registrados.append(resultado["id_resultado_inferencia"])
                else:
                    rechazados += 1
        except BackendNoDisponibleError as exc:
            # Los ids son deterministas: al reenviar el lote, lo ya guardado vuelve como duplicado.
            return JSONResponse(
                status_code=503,
                content={"error_code": "BACKEND_NO_DISPONIBLE", "message": str(exc)},
            )
        return JSONResponse(
            content={
                "paquetes": len(lote.paquetes),
                "activos_con_modelo": len(ultimo),
                "resultados_registrados": registrados,
                "rechazados_por_backend": rechazados,
                "sin_resultado": sin_resultado,
            }
        )

    return app
