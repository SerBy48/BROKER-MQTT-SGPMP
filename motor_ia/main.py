# | Fecha      | Nombre   | Que se hizo                                                     |
# |------------|----------|-----------------------------------------------------------------|
# | 2026-10-30 | Arekkazu | RF-66: esqueleto del motor de inferencia M04 (M1)                |
# | 2026-10-30 | Arekkazu | RF-66: inferencia individual ET-02 y entrega al backend (M2)     |
# | 2026-10-31 | Arekkazu | RF-17 C3: la inferencia usa los rangos de M09 del paquete         |
# | 2026-10-31 | SerBy48  | RF-66: log visible bajo uvicorn y activo inválido no tumba lote  |
# | 2026-10-31 | Arekkazu | RF-66: docstring con el prefijo /model-ia-test definido          |
# | 2026-11-02 | Arekkazu | RF-66/RF-69: catálogo desde el backend con MOTOR_IA_SINCRONIZACION_S |
# | 2026-11-02 | Arekkazu | AJF-14 RF-71: arranque del hilo reentrenador y su estado en healthz |
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
- ``MOTOR_IA_SINCRONIZACION_S``: cada cuántos segundos el motor arma su catálogo desde el
  backend (``motor_ia.sincronizacion``): un animal individual nuevo se atiende solo, sin
  ``catalogo.json``. ``0`` (por defecto) → catálogo fijo de ``MOTOR_IA_DIR_MODELOS``; así un
  ambiente cuyo backend aún no tiene ``/prediccion/motor`` sigue igual.
- ``MOTOR_IA_CALIBRACION_HORAS``: horas de lecturas aptas para la línea base propia de un animal
  (168 = 7 días, el mínimo de ET-02). Solo con la sincronización.
- ``MOTOR_IA_REENTRENAMIENTO_S`` (AJF-14): cada cuántos segundos el hilo reentrenador pide al
  backend un reentrenamiento pendiente. ``0`` (por defecto) → apagado. Necesita ``BACKEND_URL`` y
  ``MOTOR_IA_RF71_KEY`` (la misma ``X-RF71-Internal-Key`` del backend) y el extra
  ``[motor-ia-entrenamiento]`` en la imagen.
- ``MOTOR_IA_ENTRENAMIENTO_DIAS_CALIBRACION``: días de lecturas que baja para entrenar (14, el de
  ET-02). Solo para DEV, donde los animales tienen horas; si se baja, el proceso lo anota en sus motivos.
- ``MOTOR_IA_DIR_MODELOS``: carpeta con ``catalogo.json`` y los artefactos de ET-02
  (ver ``motor_ia.individual``). Vacía → sin modelos. Se ignora con la sincronización.

El motor responde en cualquier dominio (no valida el Host) y en la raíz o detrás de un prefijo
que quite el proxy (Strip Path). Para que Swagger arme bien sus URL detrás del prefijo:
``uvicorn ... --root-path /model-ia-test`` (o ``UVICORN_ROOT_PATH``).
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from motor_ia.backend import (
    BackendCatalogoClient,
    BackendNoDisponibleError,
    BackendRegistroModelosClient,
    BackendResultadosClient,
)
from motor_ia.individual import a_resultado_rf66, cargar_catalogo, inferir
from motor_ia.reentrenador import Reentrenador, reentrenar_siempre
from motor_ia.sincronizacion import SincronizadorCatalogo

logger = logging.getLogger(__name__)

_PREFIJO = "Bearer "


class MotorIASettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    iot_inferencia_m04_token: str = ""
    motor_ia_dir_modelos: str = ""
    backend_url: str = ""
    motor_ia_sincronizacion_s: int = 0
    motor_ia_calibracion_horas: float = 168.0
    motor_ia_reentrenamiento_s: int = 0
    motor_ia_rf71_key: str = ""
    motor_ia_entrenamiento_dias_calibracion: float = 14.0


class LotePaquetes(BaseModel):
    # Contrato IA-1 v1 (decisión D-B): el backend envía cada paquete de RF-56 con
    # ``id_activo_biologico``, ``lecturas`` (ventana del activo en formato largo) y, si la especie
    # tiene umbral en M09 · RF-17, ``rangos_especie`` (punto C3 de ET-02; ``plausible`` aún no se
    # usa, ver ``individual.PLAUSIBLE_M09_HABILITADA``). Se aceptan
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

    sincronizador = None
    modelos = {}
    if settings.motor_ia_sincronizacion_s > 0:
        if not settings.backend_url:
            raise RuntimeError("Falta BACKEND_URL: el motor arma su catálogo desde el backend.")
        sincronizador = SincronizadorCatalogo(
            BackendCatalogoClient(
                settings.backend_url, settings.iot_inferencia_m04_token, transport_backend
            ),
            settings.motor_ia_calibracion_horas,
        )
        _sincronizar_siempre(sincronizador, settings.motor_ia_sincronizacion_s)
    else:
        modelos = cargar_catalogo(settings.motor_ia_dir_modelos)

    def catalogo() -> dict:
        return sincronizador.modelos if sincronizador else modelos

    reentrenador = None
    if settings.motor_ia_reentrenamiento_s > 0:
        # AJF-14: hilo aparte; un entrenamiento corre en su propio subproceso con prioridad baja.
        if not settings.backend_url or not settings.motor_ia_rf71_key:
            raise RuntimeError("El reentrenador necesita BACKEND_URL y MOTOR_IA_RF71_KEY.")
        reentrenador = Reentrenador(
            BackendCatalogoClient(settings.backend_url, settings.iot_inferencia_m04_token, transport_backend),
            BackendRegistroModelosClient(settings.backend_url, settings.motor_ia_rf71_key, transport_backend),
            dias_calibracion=settings.motor_ia_entrenamiento_dias_calibracion,
        )
        reentrenar_siempre(reentrenador, settings.motor_ia_reentrenamiento_s)
        logger.info("RF-71: reentrenador activo, revisa pendientes cada %d s", settings.motor_ia_reentrenamiento_s)

    backend = None
    if modelos or sincronizador:
        if not settings.backend_url:
            raise RuntimeError(
                "Falta BACKEND_URL: hay modelos cargados y no hay dónde entregar resultados."
            )
        backend = BackendResultadosClient(
            settings.backend_url, settings.iot_inferencia_m04_token, transport_backend
        )
        if sincronizador:
            logger.info(
                "RF-66: catálogo desde el backend cada %d s; resultados hacia %s",
                settings.motor_ia_sincronizacion_s,
                backend.url,
            )
        else:
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
        estado = {
            "status": "ok",
            "modelos_cargados": len(catalogo()),
            "backend": backend.url if backend else None,
        }
        if reentrenador:
            estado["reentrenador"] = {"ocupado": reentrenador.ocupado}
        if sincronizador:
            estado["sincronizacion"] = {
                "ultima": sincronizador.ultima.isoformat() if sincronizador.ultima else None,
                "calibrando": sorted(i for i, m in catalogo().items() if m.calibrando),
            }
        return estado

    @app.post("/v1/paquetes", dependencies=[Depends(exigir_token)])
    def recibir_paquetes(lote: LotePaquetes) -> JSONResponse:
        modelos = catalogo()
        if backend is None or (sincronizador and sincronizador.ultima is None):
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


def _sincronizar_siempre(sincronizador: SincronizadorCatalogo, cada_s: int) -> None:
    """Hilo aparte: una inferencia no espera a la sincronización ni al revés."""

    def bucle() -> None:
        while True:
            try:
                sincronizador.sincronizar()
            except Exception as exc:  # noqa: BLE001 — backend caído: se conserva el catálogo
                logger.warning("Catálogo: no se pudo sincronizar con el backend (%r)", exc)
            time.sleep(cada_s)

    threading.Thread(target=bucle, name="catalogo-motor", daemon=True).start()
