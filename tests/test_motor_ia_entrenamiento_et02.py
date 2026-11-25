# | Fecha      | Nombre   | Que se hizo |
# |------------|----------|-------------|
# | 2026-11-03 | Arekkazu | AJF-14 RF-71 P-6: pipeline de entrenamiento: etiquetas, retiro y artefacto que usa el motor |
"""P-6: el pipeline de entrenamiento produce un artefacto que el motor carga e infiere con ET-02.

La prueba de punta a punta corre con una configuración reducida (6 ciclos, una combinación de la
grilla, 40 árboles) para tardar segundos; el contrato (features, clases, ONNX) es el mismo.
"""

from __future__ import annotations

import copy
import json

import pytest

pytest.importorskip("xgboost")
pytest.importorskip("sklearn")
pytest.importorskip("imblearn")
pytest.importorskip("onnxmltools")
pytest.importorskip("onnxruntime")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from motor_ia import entrenamiento, et02, individual  # noqa: E402
from motor_ia import entrenamiento_et02 as e  # noqa: E402
from motor_ia.entrenamiento_et02 import ErrorEntrenamiento  # noqa: E402

LINEA_BASE = et02.LineaBase(
    id_animal="BOV-001",
    especie="BOVINO",
    mu={"frecuencia_cardiaca": 71.0, "temperatura_corporal": 38.57, "actividad_indice": 0.24},
    sigma={"frecuencia_cardiaca": 4.3, "temperatura_corporal": 0.28, "actividad_indice": 0.18},
    perfil_circadiano={},
    inicio="2026-10-01T00:00:00+00:00",
    fin="2026-10-15T00:00:00+00:00",
    n_lecturas=0,
    fase_productiva=None,
    estado="COMPLETADA",
)


def _lecturas_sanas(dias: float, inicio: str = "2026-10-20") -> list[dict]:
    """Lecturas del backend (formato largo) de un bovino sano, cada 5 min."""
    rng = np.random.default_rng(1)
    ts = pd.date_range(inicio, periods=int(dias * 24 * 12), freq="5min", tz="UTC")
    hora = np.asarray(((ts.hour + ts.minute / 60) - 5) % 24, dtype=float)
    filas = []
    for var in et02.VARIABLES:
        perfil = e._perfil_continuo(LINEA_BASE, var, hora)
        valores = perfil + rng.normal(0, LINEA_BASE.sigma[var] * 0.5, len(ts))
        if var == "actividad_indice":
            valores = np.clip(valores, 0, 1)
        filas += [
            {"timestamp": t.isoformat(), "variable": var, "valor": float(v), "apto_para_ia": True}
            for t, v in zip(ts, valores, strict=True)
        ]
    return filas


def _cfg_rapida() -> dict:
    cfg = copy.deepcopy(et02.CONFIG)
    cfg["generador"]["n_ciclos"] = 6
    cfg["generador"]["ciclos_sensibilidad"] = 2
    cfg["grid_busqueda"] = {
        "max_depth": [3],
        "learning_rate": [0.2],
        "reg_lambda": [1.0],
        "gamma": [0.3],
    }
    cfg["xgboost"]["n_estimators"] = 40
    return cfg


@pytest.mark.parametrize(
    ("especie", "tipo", "clave"),
    [
        ("Bovino", None, "BOVINO"),
        ("Búfalo", None, "BUFALO"),
        ("Bovinos", None, "BOVINO"),
        ("Caballo", None, "EQUINO"),
        ("Especie rara", "MODELO_ESPECIES_MEDIANAS", "OVINO"),
        ("MODELO_ESPECIES_GRANDES", None, "BOVINO"),
    ],
)
def test_resuelve_la_especie_del_catalogo(especie, tipo, clave):
    assert e.resolver_especie(especie, tipo) == clave


def test_especie_poblacional_o_desconocida_aborta_con_codigo():
    with pytest.raises(ErrorEntrenamiento) as exc:
        e.resolver_especie("Porcino")
    assert exc.value.codigo == "ESPECIE_POBLACIONAL"
    with pytest.raises(ErrorEntrenamiento) as exc:
        e.resolver_especie("Especie rara")
    assert exc.value.codigo == "ESPECIE_NO_SOPORTADA"


@pytest.mark.parametrize(
    ("retro", "clase"),
    [
        ({"estado_retroalimentacion": "SIN_EVENTO"}, 0),
        ({"estado_retroalimentacion": "CORRECTO", "codigo_rf64_inferido": 6}, 5),
        (
            {
                "estado_retroalimentacion": "CORRECTO",
                "codigo_rf64_inferido": None,
                "nivel_riesgo_inferido": 0,
            },
            0,
        ),
        # Sin código y con riesgo: ganó el grupo 3+4 (la clase fusionada).
        (
            {
                "estado_retroalimentacion": "CORRECTO",
                "codigo_rf64_inferido": None,
                "nivel_riesgo_inferido": 2,
            },
            3,
        ),
        ({"estado_retroalimentacion": "INCORRECTO", "codigos_rf64_reales": [4, 1]}, 3),
        ({"estado_retroalimentacion": "PARCIAL", "codigos_rf64_reales": [7]}, 6),
        # Diagnóstico que no es patología base: no hay etiqueta posible.
        ({"estado_retroalimentacion": "INCORRECTO", "codigos_rf64_reales": []}, None),
    ],
)
def test_etiqueta_de_cada_retroalimentacion(retro, clase):
    assert e.etiqueta_de_retroalimentacion(retro) == clase


def test_ventanas_reales_toman_la_etiqueta_de_la_inferencia_que_solapan():
    ventanas = pd.DataFrame(
        {
            "ventana_inicio": pd.date_range("2026-10-20T00:00Z", periods=4, freq="10min"),
        }
    )
    ventanas["ventana_fin"] = ventanas["ventana_inicio"] + pd.Timedelta(minutes=10)
    retro = {
        "estado_retroalimentacion": "CORRECTO",
        "codigo_rf64_inferido": 6,
        "fecha_inicio_ventana": "2026-10-20T00:15:00+00:00",
        "fecha_fin_ventana": "2026-10-20T00:25:00+00:00",
    }
    assert e.etiquetar_ventanas_reales(ventanas, [retro]).tolist() == [-1, 5, 5, -1]


def test_regla_de_retiro():
    cfg = et02.CONFIG
    assert (
        e.aplicar_regla_retiro(np.array([], dtype=int), 1000, cfg)["fuente_datos_modelo"]
        == "SINTETICA"
    )
    pocas = e.aplicar_regla_retiro(np.zeros(20, dtype=int), 1000, cfg)
    assert pocas["fuente_datos_modelo"] == "MIXTA" and pocas["pesos_sinteticos"][0] == 1.0
    # 600 etiquetadas de 1000 con NINGUNA cubierta: su sintético baja al peso residual.
    parcial = e.aplicar_regla_retiro(np.zeros(600, dtype=int), 1000, cfg)
    assert parcial["fuente_datos_modelo"] == "MIXTA"
    assert parcial["pesos_sinteticos"][0] == cfg["retiro"]["peso_sintetico_residual"]
    assert parcial["pesos_sinteticos"][5] == 1.0
    todas = e.aplicar_regla_retiro(np.repeat(np.arange(7), 100), 1000, cfg)
    assert todas["fuente_datos_modelo"] == "REAL" and todas["retiro_total"] is True


def test_calibracion_con_pocos_dias_aborta():
    df = e.lecturas_a_dataframe(_lecturas_sanas(2))
    with pytest.raises(ErrorEntrenamiento) as exc:
        e.calibrar_animal(df, "BOVINO", "BOV-001")
    assert exc.value.codigo == "CALIBRACION_INCOMPLETA"
    lb = e.calibrar_animal(df, "BOVINO", "BOV-001", dias_minimos=1)
    assert abs(lb.mu["temperatura_corporal"] - 38.57) < 0.3 and lb.estado == "COMPLETADA"
    assert len(lb.perfil_circadiano["actividad_indice"]) == 24


def test_generador_cubre_las_siete_clases_dentro_de_la_banda_plausible():
    df, control = e.generar_linea_temporal_sintetica(LINEA_BASE, et02.CONFIG, 3, n_ciclos=2)
    assert set(df["etiqueta"].unique()) == set(range(7))
    assert control["episodios"] == 12
    for var in et02.VARIABLES:
        pmin, pmax = et02.ESPECIES["BOVINO"]["rango_plausible"][var]
        assert df[var].dropna().between(pmin, pmax).all()


def test_entrena_y_el_motor_usa_el_artefacto(tmp_path):
    fases: list[int] = []
    r = entrenamiento.entrenar(
        id_animal="BOV-009",
        especie="Bovino",
        lecturas=_lecturas_sanas(3),
        retroalimentaciones=[],
        id_proceso="6f1c2a3b-0000-4000-8000-000000000001",
        version_referencia=7,
        al_avanzar=fases.append,
        tipo_modelo="MODELO_ESPECIES_GRANDES",
        dias_minimos=2,
    )
    # Fases en orden, sin retroceder (3 PREPROCESAMIENTO … 6 ENTREGA).
    assert fases[0] == 3 and fases[-1] == 6 and fases == sorted(fases)
    m = r.metadatos
    assert r.registrable and m["fuente_datos"] == "SINTETICA" and m["proporcion_sintetica"] == 1.0
    assert m["features"] == et02.FEATURES and m["id_proceso_rf71"].startswith("6f1c2a3b")
    # Lo que exige el registro de RF-69, en [0, 1].
    for campo in (
        "f1_score_global",
        "recall_clase_riesgo_alto",
        "precision_global",
        "accuracy",
        "roc_auc_score",
    ):
        assert 0.0 <= m["metricas_validacion"][campo] <= 1.0
    assert len(m["metricas_validacion"]["matriz_confusion"]) == 7
    assert len(m["dataset_entrenamiento_hash"]) == 64
    json.dumps(m)  # viaja por JSON al backend

    # El motor lo carga como cualquier artefacto de ET-02 (verifica el SHA-256) y lo usa.
    (tmp_path / "m.onnx").write_bytes(r.onnx)
    (tmp_path / "meta.json").write_text(json.dumps(m))
    (tmp_path / "lb.json").write_text(json.dumps(m["linea_base"]))
    (tmp_path / "catalogo.json").write_text(
        json.dumps(
            {
                "modelos": [
                    {
                        "id_activo_biologico": 9,
                        "id_version_modelo": 1,
                        "onnx": "m.onnx",
                        "linea_base": "lb.json",
                        "metadatos": "meta.json",
                    }
                ]
            }
        )
    )
    modelo = individual.cargar_catalogo(str(tmp_path))[9]
    ts = pd.date_range("2026-11-01T18:00Z", periods=24, freq="5min")

    def lecturas(fc, temp, act):
        return [
            {"timestamp": t.isoformat(), "variable": v, "valor": x, "apto_para_ia": True}
            for t in ts
            for v, x in (
                ("frecuencia_cardiaca", fc),
                ("temperatura_corporal", temp),
                ("actividad_indice", act),
            )
        ]

    sano, _ = individual.inferir(modelo, lecturas(71, 38.6, 0.3))
    septico, decision = individual.inferir(modelo, lecturas(110, 40.8, 0.05))
    assert sano["clase_predicha"] == "NINGUNA"
    assert septico["clase_predicha"] == "SEPTICEMIA"
    assert decision["modo_sombra"] is True  # SINTETICA: calcula y registra, no alerta


@pytest.fixture(autouse=True)
def _config_rapida(monkeypatch):
    """El adaptador llama al pipeline con CONFIG: aquí se reduce para que tarde segundos."""
    original = e.entrenar_modelo_individual

    def rapida(**kwargs):
        return original(**kwargs, cfg=_cfg_rapida())

    monkeypatch.setattr(e, "entrenar_modelo_individual", rapida)
