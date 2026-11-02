# | Fecha      | Nombre   | Que se hizo |
# |------------|----------|-------------|
# | 2026-11-02 | Arekkazu | AJF-14 RF-71: el reentrenador recorre las 7 fases contra un backend falso |
"""M-5: un pipeline de ET-02 de mentira (mismas funciones y contrato) y un backend en memoria.

El pipeline real (``motor_ia.entrenamiento_et02``) no está en el repo todavía (P-6); aquí se
inyecta uno corto en ``sys.modules`` para probar el recorrido, el reporte de fases y los cierres.
"""

from __future__ import annotations

import sys
import threading
import types

from motor_ia import entrenamiento
from motor_ia.reentrenador import Reentrenador, _entrenar_en_hijo

ID = "6f1c2a3b-0000-4000-8000-000000000001"
PROCESO = {
    "id_proceso_reentrenamiento": ID,
    "id_activo_biologico": 4,
    "identificador_activo": "BOV-003",
    "nombre_especie": "Bovino",
    "tipo_modelo": "MODELO_ESPECIES_GRANDES",
    "id_version_modelo_referencia": 7,
}
METADATOS = {
    "metricas_validacion": {
        "f1_score_global": 0.81,
        "recall_clase_riesgo_alto": 0.86,
        "roc_auc_score": 0.9,
    },
    "dataset_entrenamiento_hash": "ab" * 32,
    "fecha_entrenamiento": "2026-11-02T12:00:00+00:00",
    "compatibilidad_variables": ["frecuencia_cardiaca"],
    "fuente_datos": "SINTETICA",
    "version_modelo": "MODELO_ESPECIES_GRANDES-BOV-003",
    "estado_registro_rf69": "REGISTRABLE_SOMBRA",
    "apto_despliegue_sombra": True,
    "motivo_no_registrable": [],
}


class _Backend:
    def __init__(
        self,
        proceso=PROCESO,
        lecturas=(
            {
                "timestamp": "t",
                "variable": "frecuencia_cardiaca",
                "valor": 70,
                "apto_para_ia": True,
            },
        ),
    ):
        self.pendientes = [dict(proceso)] if proceso else []
        self.reportes: list[dict] = []
        self._lecturas = list(lecturas)

    def tomar_reentrenamiento(self):
        return self.pendientes.pop(0) if self.pendientes else None

    def reportar_reentrenamiento(self, id_proceso, **campos):
        self.reportes.append({k: v for k, v in campos.items() if v is not None})
        return {}

    def lecturas(self, id_activo, horas):
        self.horas = horas
        return self._lecturas

    def retroalimentaciones(self, id_activo):
        return [{"id_resultado_inferencia": "x", "diagnosticos_reales": [8]}]


class _Registro:
    def __init__(self):
        self.formularios = []

    def registrar(self, formulario, onnx, nombre):
        self.formularios.append((formulario, onnx, nombre))
        return {"id_version_modelo": 31, "nombre_version": "bov3-v2", "estado_version": "SOMBRA"}


def _pipeline(monkeypatch, *, falla_con=None, metadatos=METADATOS):
    """Módulo con las funciones que envuelve el adaptador, llamadas en el orden de ET-02."""
    mod = types.ModuleType(entrenamiento.MODULO_PIPELINE)

    class ErrorEntrenamiento(Exception):
        def __init__(self, codigo):
            super().__init__(codigo)
            self.codigo = codigo

    mod.ErrorEntrenamiento = ErrorEntrenamiento
    mod.calibrar_animal = lambda *a, **k: None
    mod.generar_linea_temporal_sintetica = lambda *a, **k: None
    mod.entrenar_xgb_con_grilla = lambda *a, **k: None
    mod.evaluar_criterios_aceptacion = lambda *a, **k: None
    mod.exportar_onnx = lambda *a, **k: b"onnx"

    def entrenar_modelo_individual(**kw):
        assert kw["entrenar_contrastes"] is False and kw["n_verificadas_rf72"] == 1
        mod.calibrar_animal()
        mod.generar_linea_temporal_sintetica()
        if falla_con:
            raise ErrorEntrenamiento(falla_con)
        mod.entrenar_xgb_con_grilla()
        mod.evaluar_criterios_aceptacion()
        return {"metadatos_rf69": metadatos, "onnx": mod.exportar_onnx()}

    mod.entrenar_modelo_individual = entrenar_modelo_individual
    monkeypatch.setitem(sys.modules, entrenamiento.MODULO_PIPELINE, mod)


def _en_hilo(cola, kwargs):
    """Lanza el cuerpo del subproceso en un hilo (sin spawn) para la prueba."""
    hilo = threading.Thread(target=_entrenar_en_hijo, args=(cola, kwargs), daemon=True)
    hilo.start()
    return hilo


def _reentrenador(backend, registro=None, dias=14.0):
    return Reentrenador(
        backend, registro or _Registro(), dias_calibracion=dias, latido_s=0.01, lanzar=_en_hilo
    )


def test_recorre_las_siete_fases_y_registra_la_version(monkeypatch):
    _pipeline(monkeypatch)
    backend, registro = _Backend(), _Registro()
    assert _reentrenador(backend, registro).ciclo() is True
    fases = [r["fase"] for r in backend.reportes if "fase" in r]
    assert fases == sorted(fases) and {2, 3, 4, 5, 6} <= set(fases)
    cierre = backend.reportes[-1]
    assert cierre["estado"] == "COMPLETADO" and cierre["id_version_modelo"] == 31
    formulario, onnx, nombre = registro.formularios[0]
    assert formulario["id_proceso_rf71"] == ID and formulario["id_activo_biologico"] == "4"
    assert (
        formulario["fuente_datos"] == "SINTETICA"
        and formulario["version_referencia"] == "7"
        and onnx == b"onnx"
    )
    assert backend.horas == 14 * 24


def test_error_de_et02_cierra_fallido_con_su_codigo(monkeypatch):
    _pipeline(monkeypatch, falla_con="CALIBRACION_INCOMPLETA")
    backend = _Backend()
    _reentrenador(backend).ciclo()
    assert (
        backend.reportes[-1]["estado"] == "FALLIDO"
        and backend.reportes[-1]["codigo_error"] == "CALIBRACION_INCOMPLETA"
    )


def test_version_no_registrable_no_se_entrega(monkeypatch):
    _pipeline(
        monkeypatch,
        metadatos={
            **METADATOS,
            "estado_registro_rf69": "NO_REGISTRABLE",
            "apto_despliegue_sombra": False,
            "motivo_no_registrable": ["F1_BAJO"],
        },
    )
    backend, registro = _Backend(), _Registro()
    _reentrenador(backend, registro).ciclo()
    assert backend.reportes[-1] == {
        "estado": "FALLIDO",
        "codigo_error": "NO_REGISTRABLE",
        "motivos": ["F1_BAJO"],
    }
    assert registro.formularios == []


def test_sin_el_pipeline_copiado_falla_sin_inventar(monkeypatch):
    monkeypatch.setitem(
        sys.modules, entrenamiento.MODULO_PIPELINE, None
    )  # ImportError al importarlo
    backend = _Backend()
    _reentrenador(backend).ciclo()
    assert backend.reportes[-1]["codigo_error"] == "ENTRENAMIENTO_NO_DISPONIBLE"


def test_sin_lecturas_y_con_menos_dias_lo_anota(monkeypatch):
    _pipeline(monkeypatch)
    backend = _Backend(lecturas=())
    _reentrenador(backend, dias=2).ciclo()
    final = backend.reportes[-1]
    assert final["codigo_error"] == "CALIBRACION_INCOMPLETA" and "2 días" in final["motivos"][0]


def test_sin_pendientes_no_hace_nada():
    assert _reentrenador(_Backend(proceso=None)).ciclo() is False


def test_subproceso_real_sin_pipeline_reporta_no_disponible():
    """El camino por defecto (spawn): el hijo no encuentra el pipeline y lo dice por la cola."""
    backend = _Backend()
    Reentrenador(backend, _Registro(), latido_s=0.5).ciclo()
    assert backend.reportes[-1]["codigo_error"] == "ENTRENAMIENTO_NO_DISPONIBLE"
