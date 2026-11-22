# | Fecha      | Nombre   | Que se hizo |
# |------------|----------|-------------|
# | 2026-11-02 | Arekkazu | AJF-14 RF-71: adaptador del pipeline de entrenamiento de ET-02 v5 con reporte de fases |
# | 2026-11-03 | Arekkazu | AJF-14 RF-71 P-6: usa el pipeline propio, con tipo de modelo y días mínimos |
"""Adaptador entre el reentrenador y el pipeline (``motor_ia/entrenamiento_et02.py``).

El pipeline entrena sobre el contrato de operación de ET-02 v5 (``et02.py``): mismas features,
clases y formato ONNX con los que el motor infiere. Si sus dependencias (extra
``[motor-ia-entrenamiento]``) no están en la imagen, ``entrenar`` lanza
``EntrenamientoNoDisponible`` y el proceso termina FALLIDO con ``ENTRENAMIENTO_NO_DISPONIBLE``:
no se inventan resultados.

Fases que ve la pantalla (las informa ``al_avanzar``) y el paso de ``entrenar_modelo_individual``
al que corresponden. Se reportan envolviendo las funciones del módulo:

  3 PREPROCESAMIENTO  calibrar_animal, generar_linea_temporal_sintetica (+ regla de retiro)
  4 ENTRENAMIENTO     entrenar_xgb_con_grilla (split, balanceo, grilla XGBoost)
  5 METRICAS          evaluar_criterios_aceptacion (controles antes de exportar)
  6 ENTREGA           exportar_onnx

``feat_real_etiquetado`` son las retroalimentaciones del backend tal cual
(``GET /prediccion/motor/activos/{id}/retroalimentaciones``); el pipeline devuelve
``{"metadatos_rf69": {...}, "onnx": bytes, "linea_base": {...}}``.
"""

from __future__ import annotations

import functools
import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MODULO_PIPELINE = "motor_ia.entrenamiento_et02"

#: Función del pipeline → fase de la pantalla.
FASE_DE_FUNCION: dict[str, int] = {
    "calibrar_animal": 3,
    "generar_linea_temporal_sintetica": 3,
    "entrenar_xgb_con_grilla": 4,
    "evaluar_criterios_aceptacion": 5,
    "exportar_onnx": 6,
}


class EntrenamientoNoDisponible(RuntimeError):
    """No está la copia del pipeline de entrenamiento (o sus dependencias del extra)."""


class FalloEntrenamiento(RuntimeError):
    """El pipeline abortó con su código (``ErrorEntrenamiento``: CALIBRACION_INCOMPLETA…)."""

    def __init__(self, codigo: str, mensaje: str = "") -> None:
        super().__init__(mensaje or codigo)
        self.codigo = codigo


@dataclass
class ResultadoEntrenamiento:
    metadatos: dict[str, Any]
    onnx: bytes | None
    motivos: list[str] = field(default_factory=list)

    @property
    def registrable(self) -> bool:
        """Hay artefacto y el pipeline no lo marcó como no registrable (o lo deja para sombra)."""
        if self.onnx is None:
            return False
        estado = str(self.metadatos.get("estado_registro_rf69") or "")
        return estado.startswith("REGISTRABLE") or bool(
            self.metadatos.get("apto_despliegue_sombra")
        )


def _codigo_de(exc: BaseException) -> str | None:
    """``ErrorEntrenamiento(código)`` de ET-02 → código; otra excepción → None."""
    if type(exc).__name__ != "ErrorEntrenamiento":
        return None
    codigo = getattr(exc, "codigo", None) or (exc.args[0] if exc.args else None)
    return str(codigo) if codigo else "ERROR_ENTRENAMIENTO"


def _normalizar_salida(salida: Any) -> ResultadoEntrenamiento:
    if not isinstance(salida, dict):
        raise FalloEntrenamiento(
            "SALIDA_PIPELINE_DESCONOCIDA",
            f"entrenar_modelo_individual devolvió {type(salida).__name__}",
        )
    metadatos = salida.get("metadatos_rf69") or salida.get("metadatos") or {}
    onnx = salida.get("onnx") or salida.get("onnx_bytes")
    ruta = salida.get("ruta_onnx")
    if onnx is None and ruta:
        onnx = Path(ruta).read_bytes()
    motivos = [str(m) for m in (metadatos.get("motivo_no_registrable") or [])]
    return ResultadoEntrenamiento(metadatos=metadatos, onnx=onnx, motivos=motivos)


#: Callback del entrenamiento en curso (uno a la vez por proceso). Las funciones envueltas lo leen
#: al llamarse, así un segundo ``entrenar`` en el mismo proceso no reporta al callback anterior.
_avance: dict[str, Callable[[int], None] | None] = {"al_avanzar": None}


def _envolver(modulo: Any) -> None:
    for nombre, fase in FASE_DE_FUNCION.items():
        original = getattr(modulo, nombre, None)
        if original is None or getattr(original, "_ajf14_envuelta", False):
            continue

        @functools.wraps(original)
        def envuelta(*args, __original=original, __fase=fase, **kwargs):
            if _avance["al_avanzar"] is not None:
                _avance["al_avanzar"](__fase)
            return __original(*args, **kwargs)

        envuelta._ajf14_envuelta = True  # type: ignore[attr-defined]
        setattr(modulo, nombre, envuelta)


def entrenar(
    *,
    id_animal: str,
    especie: str,
    lecturas: list[dict],
    retroalimentaciones: list[dict],
    id_proceso: str,
    version_referencia: int | None,
    al_avanzar: Callable[[int], None],
    tipo_modelo: str | None = None,
    dias_minimos: float | None = None,
    modulo: str = MODULO_PIPELINE,
) -> ResultadoEntrenamiento:
    """Corre ``entrenar_modelo_individual`` (sin contrastes RF/MLP en v1).

    ``dias_minimos``: días de lecturas sanas que exige la calibración (None = los 7 de ET-02).
    """
    try:
        pipeline = importlib.import_module(modulo)
    except ImportError as exc:
        raise EntrenamientoNoDisponible(
            f"No está el pipeline de entrenamiento ({modulo}): {exc}"
        ) from exc
    _envolver(pipeline)
    _avance["al_avanzar"] = al_avanzar
    try:
        salida = pipeline.entrenar_modelo_individual(
            id_animal=id_animal,
            especie=especie,
            lecturas_calibracion=lecturas,
            feat_real_etiquetado=retroalimentaciones,
            ciclo_reentrenamiento=1,
            n_verificadas_rf72=len(retroalimentaciones),
            id_proceso_rf71=id_proceso,
            version_referencia=version_referencia,
            entrenar_contrastes=False,
            tipo_modelo=tipo_modelo,
            dias_minimos=dias_minimos,
        )
    except ImportError as exc:  # el pipeline importa xgboost, sklearn… al usarlos
        raise EntrenamientoNoDisponible(
            f"Faltan dependencias del entrenamiento (extra motor-ia-entrenamiento): {exc}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — el código de ET-02 viaja tal cual al backend
        codigo = _codigo_de(exc)
        if codigo:
            raise FalloEntrenamiento(codigo, str(exc)) from exc
        raise
    finally:
        _avance["al_avanzar"] = None
    return _normalizar_salida(salida)
