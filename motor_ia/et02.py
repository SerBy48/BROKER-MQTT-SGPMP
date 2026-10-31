# | Fecha      | Nombre   | Que se hizo                                                     |
# |------------|----------|-----------------------------------------------------------------|
# | 2026-10-30 | Arekkazu | RF-66: copia literal del pipeline de operación de ET-02 v5 (M2)  |
"""Pipeline de OPERACIÓN del modelo individual ET-02 v5.0.0 (``MODELO_ESPECIES_MEDIANAS/GRANDES``).

Copia literal, generada, de ``modelos_individuales_ET02_v5.py`` (tag ``ET02-v5.0.0``): validación,
ventaneo, línea base, features, artefacto ONNX e inferencia. Se dejó fuera todo lo de
entrenamiento (xgboost, sklearn, imblearn, matplotlib), que no corre en el motor.

No editar a mano: el contrato del artefacto (orden de FEATURES, ventaneo, bandas por especie) tiene
que ser el mismo con el que se entrenó. Para una versión nueva de ET-02 se vuelve a copiar y se
regenera ``tests/fixtures/et02_bov001_referencia.json``.
"""
# ruff: noqa
from __future__ import annotations

import copy
import hashlib
import os
import time
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import onnxruntime as ort
import pandas as pd


CONFIG: Dict[str, Any] = {
    "version_pipeline": "ET02-v5.0.0",
    # --- RF-65 v2.0 / RF-69 v2.0: taxonomía de modelos ---
    "paradigma": "INDIVIDUAL",
    # --- RF-71 R9: límites de recursos del entrenamiento (CPU máx. 70 %) ---
    "recursos": {"n_jobs": max(1, int((os.cpu_count() or 2) * 0.70))},
    # --- RF-69 / RF-66: definición de «riesgo alto» para el modelo individual. SUPUESTO S4 (pendiente de Análisis) ---
    "riesgo_alto": {"clases": ["SEPTICEMIA"]},
    "semilla": 42,
    "zona_horaria": "America/Bogota",  # la fase circadiana se calcula en hora local de la granja
    # --- Tabla 3.2: ventaneo (configuración operativa) ---
    "ventaneo": {
        "periodo_muestreo_min": 5,          # Tabla 2.1: 5–10 min
        "tam_ventana_min": 10,              # propuesta 10 (rango 5–15)
        "solapamiento_entrenamiento": 0.0,  # DEBE ser 0 (ventanas disjuntas)
        "solapamiento_operacion": 0.5,      # permitido 0–50 %
        "cobertura_minima": 0.60,           # 0.50–0.80
        "tolerancia_sincronizacion_s": 5,   # ≤ 5 s → simultáneas
        "jitter_maximo_s": 35,              # > 35 s → lectura descartada
    },
    # --- Tabla 3.1: manejo de faltantes (configuración operativa) ---
    "faltantes": {
        "temperatura_corporal": {"max_hueco": 2, "si_excede": "EXCLUIR"},
        "frecuencia_cardiaca": {"max_hueco": 2, "si_excede": "EXCLUIR"},
        "actividad_indice": {"max_hueco": 3, "si_excede": "MEDIA_MOVIL", "n_media_movil": 6},
    },
    # --- Tabla 4.2: hiperparámetros XGBoost ---
    "xgboost": {
        "objective": "multi:softprob",
        "num_class": 7,
        "eval_metric": "mlogloss",
        "n_estimators": 200,
        "max_depth": 4,
        "learning_rate": 0.05,
        "subsample": 0.85,
        "colsample_bytree": 0.85,
        "reg_lambda": 1.0,
        "reg_alpha": 0.0,
        "gamma": 0.1,
        "min_child_weight": 3,
        "tree_method": "hist",
        "early_stopping_rounds": 20,
    },
    # Búsqueda en grilla ACOTADA (no aleatoria) sobre validación, sin barajar el orden temporal (§4.3)
    "grid_busqueda": {
        # v0.2: grilla desplazada hacia árboles más simples por la alerta de sobreajuste de v0.1
        # (ET-02 Tabla 7.2: «reducir max_depth; subir reg_lambda y gamma»). Valores dentro de los rangos de la Tabla 4.2.
        "max_depth": [3, 4],
        "learning_rate": [0.05, 0.1],
        "reg_lambda": [1.0, 3.0],
        "gamma": [0.3, 0.5],
    },
    # --- Tabla 4.3: balanceo ---
    "balanceo": {
        "clases_criticas": [5],        # índice interno 5 = Septicemia (mayor severidad)
        "umbral_smote": 0.15,          # SMOTE solo si la clase crítica < 15 %
        "objetivo_smote": 0.20,        # ... hasta llegar al 20 %
        "umbral_submuestreo": 0.70,    # submuestreo opcional si «Ninguna» > 70 %
        "min_ventanas_submuestreo": 10_000,
        "min_muestras_clase": 10,      # < 10 → DATASET_DESBALANCEADO (RF-71 E2)
    },
    # --- Tabla 6.1: calibración por animal ---
    # v3 (SC-I-03 B·8.1): 14 días (M04 V2.0 Edge-2: «7–14 días»); con 2 días el bootstrap era circular.
    "calibracion": {"dias_calibracion": 14, "dias_minimos": 7, "meses_validez": 6,
                    "dias_base_por_bloque_min": 2},
    # --- Tabla 6.2: esquema de validación ---
    "validacion": {
        "split": (0.70, 0.15, 0.15),
        # Walk-forward (Plan §5.5). v3 (SC-I-03 A·5.4): la unidad de prueba es un CICLO (las 7 clases), porque una
        # ventana de 7 días no puede contener las 7 clases: un episodio de deshidratación dura hasta ~7 días (Tabla 5.1).
        "wf_unidad_prueba": "ciclo",        # v4 (SC-I-04 R3-03): período mínimo que contiene las 7 clases
        "wf_ciclos_entrenamiento": 3,       # ≈ 60 días de historia (un ciclo ≈ 20 días)
        "wf_min_iteraciones": 4,
        # v5 (SC-I-05 R4-02): el paso es de UN ciclo, como dice el texto editorial de C6 punto 7. En la v4 el
        # muestreo uniforme contra un tope de 8 daba un paso real de 2 ciclos. Con 18 ciclos → 15 iteraciones.
        "wf_paso_ciclos": 1,
        "wf_max_iteraciones": 16,           # tope de seguridad, no el paso; queda registrado en los metadatos
    },
    # --- Tabla 6.3 + Tabla 7.2: criterios propios y señales de riesgo ---
    "aceptacion": {
        "auc_real_vs_sintetico_max": 0.80,
        "caida_f1_sensibilidad_max": 0.15,
        "artefacto_mb_max": 40.0,
        "fraccion_ram_max": 0.70,
        "brecha_sobreajuste_max": 0.10,
        "f1_fuga_sospechosa": 0.99,
        "max_fraccion_temp_ausente": 0.40,
        "sigma_f1_max": 0.05,               # Plan §2.5 / §5.5: σ(F1_macro) walk-forward ≤ 0.05 (obligatorio)
        "sigma_f1_recomendado": 0.03,
        # v5 (SC-I-05 R4-01): el recorte en el piso 0 del índice de actividad deja de ser informativo y pasa a
        # ser control declarado. Umbral del mismo orden que el 0.05 de temperatura, relajado porque en actividad
        # algo de piso es legítimo (animal echado). Se evalúa por episodio.
        "fraccion_piso_actividad_max": 0.20,
        # v5 (SC-I-05 menor 5): piso de recall de la clase severa SOBRE SINTÉTICOS. El criterio del Plan §2.5
        # (≥ 0.85) se mide con datos reales; este es un diagnóstico declarado, NO bloqueante, hasta que
        # Análisis decida si debe existir un piso sintético (escalado en C7).
        "recall_severo_sintetico_referencia": 0.60,
    },
    # --- §5.5: regla de retiro de sintéticos ---
    "retiro": {
        "rho_inicial": 1.0,
        "n_reales_min": 500,
        "fraccion_verificadas_min": 0.20,
        "peso_sintetico_residual": 0.25,
    },
    # --- Tabla 2.4: reglas de resolución en RF-66 (configuración operativa de RF-66) ---
    "rf66": {"confianza_min": 0.50, "margen_min": 0.15, "completitud_min": 0.60},
    # --- §6.4: gateway. v3 (SC-I-03 A·5.8): doble plataforma ---
    "gateway": {
        "ram_objetivo_bytes": 4 * 1024**3,     # Raspberry Pi 4 — Cotización ítem P1 #1
        "ram_prototipo_bytes": 1 * 1024**3,    # Raspberry Pi 3 Model B — plataforma real de desarrollo (Plan §4.1)
        "residente_estimado_bytes": 400 * 1024**2,  # SO + Python + onnxruntime + pandas en el Pi 3 (estimación SC-I-03)
        "max_modelos_en_memoria": 200,
    },
    # --- Cap. 5: control del generador (no clínico) ---
    "generador": {
        # v3 (SC-I-03 B·N2): 18 ciclos → 18 episodios por clase, repartidos 12 / 3 / 3 ciclos por bloque
        "n_ciclos": 18,
        "ciclos_validacion": 3,
        "ciclos_prueba": 3,
        "hueco_sano_h": (12, 36),       # tiempo sano entre episodios
        "previo_sano_h": (2, 6),        # tramo sano antes de t0 dentro de cada segmento
        "factor_ruido_sensor": 0.10,    # ε_s ~ N(0, (factor·precisión)²)
        "phi_min_etiqueta": 0.0,        # ET-02 §5.4 paso 5: clase c desde t0 (Φ > 0)
        # v5 (SC-I-05 R4-01): cota inferior del factor multiplicativo de la actividad, por si dos episodios
        # se solapan y sus caídas se acumulan. Con los huecos sanos actuales no llega a actuar.
        "escala_multiplicativa_min": 0.05,
        "ciclos_sensibilidad": 6,       # línea temporal alternativa para la prueba de sensibilidad
    },
}


VARIABLES = ["frecuencia_cardiaca", "temperatura_corporal", "actividad_indice"]


ABREV = {"frecuencia_cardiaca": "fc", "temperatura_corporal": "temp", "actividad_indice": "act"}


CATALOGO_CLASES: List[Dict[str, Any]] = [
    {"idx": 0, "codigo": "0", "nombre": "NINGUNA"},
    {"idx": 1, "codigo": "1", "nombre": "ESTRES_TERMICO"},
    {"idx": 2, "codigo": "2", "nombre": "DESHIDRATACION"},
    {"idx": 3, "codigo": "3+4", "nombre": "RIESGO_ORIGEN_HIDRICO"},
    {"idx": 4, "codigo": "5", "nombre": "HIPOTERMIA_ESTRES_FRIO"},
    {"idx": 5, "codigo": "6", "nombre": "SEPTICEMIA"},
    {"idx": 6, "codigo": "7", "nombre": "SINDROME_RESPIRATORIO_AGUDO"},
]


NOMBRES_CLASES = [c["nombre"] for c in CATALOGO_CLASES]


CATALOGO_RF64 = {0: "Ninguna", 1: "Estrés térmico", 2: "Deshidratación", 3: "Intoxicación hídrica",
                 4: "Gastroenteritis infecciosa", 5: "Hipotermia o estrés por frío",
                 6: "Septicemia o infección sistémica", 7: "Síndrome respiratorio agudo"}


IDX_A_RF64 = {0: [0], 1: [1], 2: [2], 3: [3, 4], 4: [5], 5: [6], 6: [7]}


IDX_CLASE = {c["nombre"]: c["idx"] for c in CATALOGO_CLASES}


N_CLASES = len(CATALOGO_CLASES)


CLASES_DEPENDEN_TEMPERATURA = ["SEPTICEMIA", "SINDROME_RESPIRATORIO_AGUDO"]


FEATURES = [
    "fc_media", "temp_media", "act_media",
    "fc_z", "temp_z", "act_z",
    "delta_temp", "delta_fc",
    "t_act",
    "fc_sigma", "temp_sigma", "act_sigma",
    "fase_circadiana_sen", "fase_circadiana_cos",
]


assert len(FEATURES) == 14


GRUPO_MANEJO_A_TIPO_MODELO = {
    "ESPECIES_GRANDES": "MODELO_ESPECIES_GRANDES",
    "ESPECIES_MEDIANAS": "MODELO_ESPECIES_MEDIANAS",
}


TIPOS_MODELO_INDIVIDUAL = ["MODELO_ESPECIES_MEDIANAS", "MODELO_ESPECIES_GRANDES"]


ESPECIES_POBLACIONALES = {"PORCINO", "AVE", "GALLINA", "POLLO", "ACUICOLA", "PEZ", "TILAPIA", "TRUCHA"}


_F_M09 = "M09 V3.0 — catálogo de variables (temperatura corporal 37–40 °C, FC 40–120 bpm)"


_F_M04 = "M04 V2.0 — ficha ESPECIES_GRANDES: «rangos sanos de especie» bovino adulto, temperatura corporal 38.0–39.5 °C"


_F_EQ = "SC-I-03 §5.7 — equino en reposo 28–40 bpm; el mínimo de 40 de M09 excluiría equinos sanos (escalado en C3)"


_F_ACT = "I3P-1 — índice de actividad normalizado a [0, 1] por construcción"


_F_PL = "v0.1 (propuesta inicial), recuperado como rango plausible por SC-I-04 R3-01 — confirmar con I3P-1 (C3)"


_F_PL_BOV = "Por analogía con BOVINO, valores de v0.1 recuperados por SC-I-04 R3-01 — confirmar con I3P-1 (C3)"


_F_PL_EQ = "Por analogía con EQUINO, valores de v0.1 recuperados por SC-I-04 R3-01 — confirmar con I3P-1 (C3)"


_F_PL_OVC = ("v0.1 (45–170 bpm) con el piso bajado a 40 para contener el rango sano de M09 (40–120): una banda sana debe "
             "caber dentro de la plausible — inconsistencia entre fuentes, confirmar con I3P-1 (C3)")


_F_BASAL = "SIMULADOR — no hay documento; confirmar con veterinario (C5). Se reemplaza por la calibración real"


def _especie(grupo: str, fc_sano: Tuple[float, float], temp_sano: Tuple[float, float],
             fc_plausible: Tuple[float, float], temp_plausible: Tuple[float, float], basal: Dict[str, float],
             fuente_fc_sano: str = _F_M09, fuente_temp_sano: str = _F_M09, fuente_plausible: str = _F_PL) -> Dict[str, Any]:
    return {
        "grupo_manejo": grupo,
        "rango_sano": {"frecuencia_cardiaca": fc_sano, "temperatura_corporal": temp_sano, "actividad_indice": (0.0, 1.0)},
        "rango_plausible": {"frecuencia_cardiaca": fc_plausible, "temperatura_corporal": temp_plausible,
                            "actividad_indice": (0.0, 1.0)},
        "rango_sano_fuente": {"frecuencia_cardiaca": fuente_fc_sano, "temperatura_corporal": fuente_temp_sano,
                              "actividad_indice": _F_ACT},
        "rango_plausible_fuente": {"frecuencia_cardiaca": fuente_plausible, "temperatura_corporal": fuente_plausible,
                                   "actividad_indice": _F_ACT},
        "basal": basal,
        "basal_fuente": _F_BASAL,
    }


ESPECIES: Dict[str, Dict[str, Any]] = {
    # ---------------- MODELO_ESPECIES_GRANDES (Plan §2.8) ----------------
    #            grupo                 FC sano        temp sano      FC plausible    temp plausible
    "BOVINO": _especie("ESPECIES_GRANDES", (40.0, 120.0), (38.0, 39.5), (30.0, 160.0), (35.0, 43.0),
                       {"fc": 68.0, "temp": 38.6, "act": 0.35}, fuente_temp_sano=_F_M04),
    "EQUINO": _especie("ESPECIES_GRANDES", (28.0, 120.0), (37.0, 40.0), (20.0, 140.0), (34.5, 42.5),
                       {"fc": 38.0, "temp": 37.8, "act": 0.40}, fuente_fc_sano=_F_EQ),
    "BUFALO": _especie("ESPECIES_GRANDES", (40.0, 120.0), (37.0, 40.0), (30.0, 160.0), (35.0, 43.0),
                       {"fc": 60.0, "temp": 38.3, "act": 0.30}, fuente_plausible=_F_PL_BOV),
    # ---------------- MODELO_ESPECIES_MEDIANAS (Plan §2.8) ----------------
    "OVINO": _especie("ESPECIES_MEDIANAS", (40.0, 120.0), (37.0, 40.0), (40.0, 170.0), (35.5, 43.0),
                      {"fc": 80.0, "temp": 39.0, "act": 0.38}),
    "CAPRINO": _especie("ESPECIES_MEDIANAS", (40.0, 120.0), (37.0, 40.0), (40.0, 170.0), (35.5, 43.0),
                        {"fc": 85.0, "temp": 39.0, "act": 0.40}),
    "TERNERO": _especie("ESPECIES_MEDIANAS", (40.0, 120.0), (37.0, 40.0), (30.0, 160.0), (35.0, 43.0),
                        {"fc": 95.0, "temp": 38.9, "act": 0.40}, fuente_plausible=_F_PL_BOV),
    "EQUINO_JOVEN": _especie("ESPECIES_MEDIANAS", (28.0, 120.0), (37.0, 40.0), (20.0, 140.0), (34.5, 42.5),
                             {"fc": 50.0, "temp": 37.9, "act": 0.45}, fuente_fc_sano=_F_EQ, fuente_plausible=_F_PL_EQ),
    # PORCINO: NO va aquí. Es MODELO_PORCINOS, paradigma POBLACIONAL (ET-01) — SC-01 §4.2 / RFC-009.
}


for _e in ("OVINO", "CAPRINO"):
    ESPECIES[_e]["rango_plausible_fuente"]["frecuencia_cardiaca"] = _F_PL_OVC


LIMITES_SENSOR = {
    "frecuencia_cardiaca": (0.0, 250.0),     # MAX30102
    "temperatura_corporal": (-10.0, 85.0),   # DS18B20
    "actividad_indice": (0.0, 1.0),          # MPU-6050, normalizado por construcción
}


def grupo_de_especie(especie: str) -> str:
    """Especie → tipo_modelo vía grupo_manejo (RF-15/RF-20). Rechaza especies de paradigma POBLACIONAL."""
    if especie in ESPECIES_POBLACIONALES:
        raise ValueError(f"{especie} es de paradigma POBLACIONAL (ET-01), no tiene modelo individual. "
                         "Ver SC-01 §4.2 / RFC-009.")
    if especie not in ESPECIES:
        raise ValueError(f"Especie no soportada: {especie}")
    tipo = GRUPO_MANEJO_A_TIPO_MODELO.get(ESPECIES[especie]["grupo_manejo"])
    if tipo not in TIPOS_MODELO_INDIVIDUAL:
        raise ValueError(f"grupo_manejo sin tipo_modelo INDIVIDUAL asignable: {ESPECIES[especie]['grupo_manejo']}")
    return tipo


def sha256_bytes(datos: bytes) -> str:
    return hashlib.sha256(datos).hexdigest()


def validar_lecturas(
    df: pd.DataFrame,
    especie: str,
    cfg: Dict[str, Any] = CONFIG,
    modo: str = "entrenamiento",
) -> Tuple[pd.DataFrame, List[Dict[str, Any]], Dict[str, int]]:
    """Sincronización + filtrado RF-17. Devuelve (df_valido, eventos_regla_dura, resumen).

    Tres bandas (v4, SC-I-04 R3-01):
      * Fuera del límite FÍSICO del sensor → lectura inválida en todos los modos.
      * Fuera del rango PLAUSIBLE de la especie → lectura inválida y evento de regla dura RF-17, en todos los modos.
      * Fuera del rango SANO → solo en «calibracion» se excluye (la línea base se construye con lo sano, §6.1);
        en «entrenamiento» y «operacion» la lectura es válida: una patología, por definición, sale de lo sano.
    (La decisión D1 de la v3 queda absorbida: ya no hace falta un caso especial para operación.)
    """
    grupo_de_especie(especie)  # valida especie y rechaza paradigma POBLACIONAL
    faltan = [c for c in ["timestamp", *VARIABLES] if c not in df.columns]
    if faltan:
        raise ValueError(f"Faltan columnas obligatorias: {faltan}")

    v = cfg["ventaneo"]
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    out = out.sort_values("timestamp").reset_index(drop=True)
    if "desfase_sincronizacion_s" not in out.columns:
        out["desfase_sincronizacion_s"] = 0.0
    out["desfase_sincronizacion_s"] = out["desfase_sincronizacion_s"].fillna(0.0)

    resumen = {"lecturas_recibidas": len(out)}
    descartar = out["desfase_sincronizacion_s"] > v["jitter_maximo_s"]
    resumen["descartadas_sincronizacion"] = int(descartar.sum())
    out = out.loc[~descartar].copy()
    out["sincronizacion_degradada"] = out["desfase_sincronizacion_s"] > v["tolerancia_sincronizacion_s"]

    # RF-71 R1 / RF-62: solo lecturas con apto_para_ia = true. Si la columna no viene (datos simulados), se asume true.
    if "apto_para_ia" in out.columns:
        no_apta = out["apto_para_ia"].fillna(False).astype(bool) == False  # noqa: E712
        resumen["descartadas_no_aptas_ia"] = int(no_apta.sum())
        out = out.loc[~no_apta].copy()
    else:
        resumen["descartadas_no_aptas_ia"] = 0

    eventos: List[Dict[str, Any]] = []
    for var in VARIABLES:
        pmin, pmax = ESPECIES[especie]["rango_plausible"][var]
        hmin, hmax = ESPECIES[especie]["rango_sano"][var]
        smin, smax = LIMITES_SENSOR[var]
        valores = pd.to_numeric(out[var], errors="coerce")
        invalida = valores.notna() & ((valores < smin) | (valores > smax))
        resumen[f"invalidas_limite_sensor_{ABREV[var]}"] = int(invalida.sum())
        valores = valores.where(~invalida)
        fuera = valores.notna() & ((valores < pmin) | (valores > pmax))
        out[f"excluida_por_umbral_especie_{ABREV[var]}"] = fuera
        resumen[f"excluidas_umbral_{ABREV[var]}"] = int(fuera.sum())
        for ts, val in zip(out.loc[fuera, "timestamp"], valores[fuera]):
            eventos.append({
                "tipo": "REGLA_DURA_RF17",
                "variable": var,
                "valor": float(val),
                "rango_plausible_especie": [pmin, pmax],
                "timestamp": ts.isoformat(),
            })
        valores = valores.where(~fuera)
        if modo == "calibracion":
            no_sana = valores.notna() & ((valores < hmin) | (valores > hmax))
            resumen[f"excluidas_rango_sano_{ABREV[var]}"] = int(no_sana.sum())
            valores = valores.where(~no_sana)
        out[var] = valores
    return out, eventos, resumen


def _longitud_rachas_nan(es_nan: pd.Series) -> pd.Series:
    """Para cada posición NaN, longitud de la racha de NaN consecutivos a la que pertenece."""
    grupos = (es_nan != es_nan.shift()).cumsum()
    largo = es_nan.groupby(grupos).transform("sum")
    return largo.where(es_nan, 0).astype(int)


def regularizar_y_rellenar(
    df_valido: pd.DataFrame,
    cfg: Dict[str, Any] = CONFIG,
    origen: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """Lleva las lecturas a la grilla regular de muestreo y aplica la Tabla 3.1.

    Columnas resultantes por variable (abreviatura a = fc/temp/act):
      rec_a   → la lectura llegó realmente (base de `completitud_ventana`)
      imp_a   → el valor fue imputado
      a_nan   → quedó sin valor tras la Tabla 3.1 (hueco excedido en temp/fc)
    """
    v = cfg["ventaneo"]
    periodo = pd.Timedelta(minutes=v["periodo_muestreo_min"])
    tam = pd.Timedelta(minutes=v["tam_ventana_min"])
    if df_valido.empty:
        return pd.DataFrame()

    ts = df_valido["timestamp"]
    if origen is None:
        origen = ts.min().floor(tam)  # anclado a múltiplos del tamaño de ventana
    slot = ((ts - origen) / periodo).round().astype(int)
    tmp = df_valido.assign(_slot=slot.values)

    agregaciones = {var: "mean" for var in VARIABLES}
    agregaciones["sincronizacion_degradada"] = "max"
    for col in ["etiqueta", "id_episodio"]:
        if col in tmp.columns:
            agregaciones[col] = "max"
    for col in ["tipo_etiqueta", "bloque", "ciclo", "dia_base"]:
        if col in tmp.columns:
            agregaciones[col] = "first"
    g = tmp.groupby("_slot").agg(agregaciones)

    n_slots = int(g.index.max()) + 1
    g = g.reindex(range(n_slots))
    g.index = origen + g.index * periodo
    g.index.name = "timestamp"
    g["sincronizacion_degradada"] = g["sincronizacion_degradada"].fillna(False).astype(bool)
    for col in ["tipo_etiqueta", "bloque", "ciclo", "dia_base"]:  # metadatos de la línea temporal sintética
        if col in g.columns:
            g[col] = g[col].ffill()

    for var in VARIABLES:
        a = ABREV[var]
        regla = cfg["faltantes"][var]
        crudo = g[var]
        es_nan = crudo.isna()
        g[f"rec_{a}"] = ~es_nan
        rachas = _longitud_rachas_nan(es_nan)
        interpolado = crudo.interpolate(method="linear", limit_area="inside")
        corto = es_nan & (rachas <= regla["max_hueco"]) & interpolado.notna()
        rellenado = crudo.where(~corto, interpolado)
        if regla["si_excede"] == "MEDIA_MOVIL":
            media_movil = crudo.rolling(regla["n_media_movil"], min_periods=1).mean().ffill()
            largo = rellenado.isna()
            rellenado = rellenado.where(~largo, media_movil)
        g[var] = rellenado
        g[f"imp_{a}"] = es_nan & rellenado.notna()
        g[f"{a}_nan"] = rellenado.isna()
    return g


def construir_ventanas(
    grid: pd.DataFrame,
    cfg: Dict[str, Any] = CONFIG,
    modo: str = "entrenamiento",
) -> pd.DataFrame:
    """Agrega la grilla en ventanas (Tabla 3.2). Vectorizado con numpy.

    modo="entrenamiento": solapamiento 0 y se EXCLUYEN ventanas con contexto incompleto.
    modo="operacion":     solapamiento configurable y se conservan con banderas.
    """
    if grid is None or grid.empty:
        return pd.DataFrame()
    v = cfg["ventaneo"]
    L = int(v["tam_ventana_min"] // v["periodo_muestreo_min"])
    solap = v["solapamiento_entrenamiento"] if modo == "entrenamiento" else v["solapamiento_operacion"]
    if modo == "entrenamiento" and solap != 0:
        raise ValueError("En entrenamiento el solapamiento DEBE ser 0 (fuga entre entrenamiento y prueba).")
    paso = max(1, int(round(L * (1 - solap))))
    n = len(grid)
    if n < L:
        return pd.DataFrame()
    inicios = np.arange(0, n - L + 1, paso)
    idx = inicios[:, None] + np.arange(L)[None, :]

    filas: Dict[str, Any] = {
        "ventana_inicio": grid.index[inicios],
        "ventana_fin": grid.index[inicios] + pd.Timedelta(minutes=v["tam_ventana_min"]),
    }
    recibida_alguna = np.zeros((len(inicios), L), dtype=bool)
    ausentes_mat = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        for var in VARIABLES:
            a = ABREV[var]
            valores = grid[var].to_numpy(dtype=float)[idx]
            rec = grid[f"rec_{a}"].to_numpy(dtype=bool)[idx]
            sin_valor = grid[f"{a}_nan"].to_numpy(dtype=bool)[idx]
            recibida_alguna |= rec
            filas[f"{a}_media"] = np.nanmean(valores, axis=1)
            filas[f"{a}_sigma"] = np.nanstd(valores, axis=1)
            filas[f"completitud_{a}"] = rec.mean(axis=1)
            ausente = (~rec).all(axis=1) | sin_valor.any(axis=1)
            filas[f"ausente_{a}"] = ausente
            ausentes_mat.append(ausente)
    # v3 (SC-I-03 A·5.6): fracción de las lecturas ESPERADAS (3 variables por instante), no OR entre variables
    filas["completitud_ventana"] = np.mean([filas[f"completitud_{ABREV[var]}"] for var in VARIABLES], axis=0)
    filas["recibio_alguna_lectura"] = recibida_alguna.any(axis=1)
    filas["sincronizacion_degradada"] = grid["sincronizacion_degradada"].to_numpy(dtype=bool)[idx].any(axis=1)

    if "etiqueta" in grid.columns:
        lab = grid["etiqueta"].fillna(-1).to_numpy(dtype=int)[idx]
        conteos = np.stack([(lab == c).sum(axis=1) for c in range(N_CLASES)], axis=1).astype(float)
        conteos[:, 0] -= 0.5  # empate → gana la patología
        etiqueta = conteos.argmax(axis=1)
        etiqueta[(lab < 0).all(axis=1)] = -1
        filas["etiqueta"] = etiqueta
    for col in ["tipo_etiqueta", "bloque", "ciclo", "dia_base"]:
        if col in grid.columns:
            filas[col] = grid[col].to_numpy(dtype=object)[idx[:, 0]]
    if "id_episodio" in grid.columns:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            filas["id_episodio"] = np.nanmax(grid["id_episodio"].to_numpy(dtype=float)[idx], axis=1)

    vent = pd.DataFrame(filas)
    aus = np.stack(ausentes_mat, axis=1)  # orden VARIABLES
    vent["variables_ausentes"] = [
        [VARIABLES[j] for j in range(len(VARIABLES)) if aus[i, j]] for i in range(len(vent))
    ]
    hueco_critico = vent["ausente_temp"] | vent["ausente_fc"]
    vent["contexto_incompleto"] = hueco_critico | (vent["completitud_ventana"] < cfg["rf66"]["completitud_min"])
    vent["cobertura_suficiente"] = vent["completitud_ventana"] >= v["cobertura_minima"]

    if modo == "entrenamiento":
        vent = vent[vent["cobertura_suficiente"] & ~hueco_critico].reset_index(drop=True)
    return vent


@dataclass
class LineaBase:
    id_animal: str
    especie: str
    mu: Dict[str, float]
    sigma: Dict[str, float]
    perfil_circadiano: Dict[str, List[Optional[float]]]
    inicio: str
    fin: str
    n_lecturas: int
    fase_productiva: Optional[str]
    estado: str

    def a_dict(self) -> Dict[str, Any]:
        return asdict(self)


def estado_calibracion(
    lb: Optional[LineaBase],
    ahora: Optional[datetime] = None,
    fase_productiva_actual: Optional[str] = None,
    cfg: Dict[str, Any] = CONFIG,
) -> str:
    """EN_CURSO / COMPLETADA / EXPIRADA según Tabla 6.1."""
    if lb is None:
        return "EN_CURSO"
    if lb.estado == "EN_CURSO":
        return "EN_CURSO"
    ahora = ahora or datetime.now(timezone.utc)
    fin = pd.Timestamp(lb.fin).to_pydatetime()
    vigencia = timedelta(days=30.44 * cfg["calibracion"]["meses_validez"])
    if ahora - fin > vigencia:
        return "EXPIRADA"
    if fase_productiva_actual is not None and lb.fase_productiva is not None \
            and fase_productiva_actual != lb.fase_productiva:
        return "EXPIRADA"
    return "COMPLETADA"


EPS_ACTIVIDAD = 0.05  # evita divisiones explosivas en `t_act` cuando la actividad previa ≈ 0 (noche)


def calcular_features(
    ventanas: pd.DataFrame,
    grid: pd.DataFrame,
    lb: LineaBase,
    cfg: Dict[str, Any] = CONFIG,
) -> pd.DataFrame:
    """Añade las 14 features de la Tabla 4.1 a las ventanas."""
    if ventanas.empty:
        return ventanas.assign(**{f: pd.Series(dtype=float) for f in FEATURES})
    v = cfg["ventaneo"]
    periodo = pd.Timedelta(minutes=v["periodo_muestreo_min"])
    tam = pd.Timedelta(minutes=v["tam_ventana_min"])
    L = int(v["tam_ventana_min"] // v["periodo_muestreo_min"])
    out = ventanas.copy()

    # 4–6: desviación estandarizada respecto a la línea base personal
    for var in VARIABLES:
        a = ABREV[var]
        out[f"{a}_z"] = (out[f"{a}_media"] - lb.mu[var]) / lb.sigma[var]

    # 7–8: delta respecto a la ventana inmediatamente anterior (calculada sobre la grilla, así no depende de
    # que la ventana anterior haya sobrevivido al filtrado)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        for var, nombre in [("temperatura_corporal", "delta_temp"), ("frecuencia_cardiaca", "delta_fc")]:
            media_movil = grid[var].rolling(L, min_periods=1).mean()
            ref = out["ventana_inicio"] - periodo  # último slot de la ventana anterior
            previa = media_movil.reindex(ref).to_numpy()
            # si la ventana anterior no tiene datos válidos completos → NaN (XGBoost lo maneja)
            ok_prev = grid[var].notna().rolling(L, min_periods=1).sum().reindex(ref).to_numpy() >= 1
            previa = np.where(ok_prev, previa, np.nan)
            out[nombre] = out[f"{ABREV[var]}_media"].to_numpy() - previa

        # 9: tendencia de actividad (RF-57)
        act_hora = grid["actividad_indice"].rolling("60min", min_periods=1).mean()
        t_ult = out["ventana_fin"] - periodo
        ultima = act_hora.reindex(t_ult).to_numpy()
        previa = act_hora.reindex(t_ult - pd.Timedelta(minutes=60)).to_numpy()
        out["t_act"] = (ultima - previa) / np.maximum(previa, EPS_ACTIVIDAD)

    # Auxiliar (NO es feature del modelo): media móvil de actividad de las 2 h previas, para la regla de RF-57
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        act_2h = grid["actividad_indice"].rolling("120min", min_periods=1).mean()
        out["act_media_movil_2h"] = act_2h.reindex(out["ventana_inicio"] - periodo).to_numpy()

    # 13: fase circadiana (hora local del punto medio de la ventana)
    medio = (out["ventana_inicio"] + tam / 2).dt.tz_convert(cfg["zona_horaria"])
    hora = medio.dt.hour + medio.dt.minute / 60.0
    out["fase_circadiana_sen"] = np.sin(2 * np.pi * hora / 24.0)
    out["fase_circadiana_cos"] = np.cos(2 * np.pi * hora / 24.0)
    out["hora_local"] = hora
    return out


def pipeline_ventanas_features(
    lecturas: pd.DataFrame,
    lb: LineaBase,
    modo: str = "entrenamiento",
    cfg: Dict[str, Any] = CONFIG,
) -> Tuple[pd.DataFrame, List[Dict[str, Any]], Dict[str, int]]:
    """Validación → grilla → ventanas → features. Mismo código para entrenamiento y operación."""
    df_valido, eventos, resumen = validar_lecturas(lecturas, lb.especie, cfg, modo=modo)
    grid = regularizar_y_rellenar(df_valido, cfg)
    ventanas = construir_ventanas(grid, cfg, modo=modo)
    feats = calcular_features(ventanas, grid, lb, cfg)
    resumen["ventanas"] = int(len(feats))
    return feats, eventos, resumen


class ArtefactoModelo:
    """Modelo desplegable en el gateway: bytes ONNX + sesión onnxruntime + metadatos RF-69."""

    def __init__(self, onnx_bytes: bytes, metadatos: Dict[str, Any]):
        self.onnx_bytes = onnx_bytes
        self.metadatos = metadatos
        self.hash = sha256_bytes(onnx_bytes)
        self._sesion: Optional[ort.InferenceSession] = None

    @property
    def sesion(self) -> ort.InferenceSession:
        if self._sesion is None:  # carga perezosa (§6.4)
            self._sesion = ort.InferenceSession(self.onnx_bytes, providers=["CPUExecutionProvider"])
        return self._sesion

    def descargar(self) -> None:
        self._sesion = None

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        nombre_in = self.sesion.get_inputs()[0].name
        salidas = self.sesion.run(None, {nombre_in: np.asarray(X, dtype=np.float32)})
        proba = salidas[1]
        if isinstance(proba, list):  # ZipMap → lista de dicts
            proba = np.array([[d[c] for c in range(N_CLASES)] for d in proba])
        return np.asarray(proba, dtype=float)

    @property
    def tam_mb(self) -> float:
        return len(self.onnx_bytes) / 1024**2

    def verificar_hash(self) -> bool:
        """Se recalcula justo antes del envío a RF-69 (§7.2)."""
        return sha256_bytes(self.onnx_bytes) == self.metadatos.get("hash_artefacto_modelo")


def _contrato_vacio(paquete: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "estado": "OK",
        "id_animal": paquete.get("id_animal"),
        "id_especie": paquete.get("id_especie"),
        "id_area_productiva": paquete.get("id_area_productiva"),
        "id_lote": paquete.get("id_lote"),
        "area_id": paquete.get("area_id", paquete.get("id_area_productiva")),  # RF-66 v2.0 (entrada area_id)
        "tipo_modelo": None,                     # RF-65 / RF-20: tipo_modelo_asignado del área
        "paradigma": CONFIG["paradigma"],
        "ventana_inicio": None,
        "ventana_fin": None,
        "probabilidades": None,
        "clase_predicha": None,
        "codigo_clase_predicha": None,
        # --- proyección al catálogo RF-64 (8 patologías) para que RF-66 calcule enfermedad_estimada ---
        "probabilidad_enfermedad": None,         # {id_patologia_RF64: P}; la clase fusionada usa la llave "3+4" — S3
        "enfermedad_estimada": None,             # id RF-64; null si gana el grupo 3+4 (lo separa RF-66)
        "enfermedad_estimada_grupo": None,       # [3, 4] cuando gana la clase fusionada
        "confianza": None,
        "margen": None,
        "completitud_ventana": None,
        "variables_ausentes": [],
        "estado_calibracion_animal": None,
        "fuente_datos_modelo": None,
        "proporcion_sintetica": None,
        "desviaciones_linea_base": None,
        "version_modelo": None,
        "hash_artefacto_modelo": None,
        "timestamp_inferencia": None,
        "latencia_ms": None,
        # --- campos de diagnóstico adicionales (no forman parte de la Tabla 2.3) ---
        "contexto_incompleto": None,
        "sincronizacion_degradada": None,
        "eventos_regla_dura_rf17": [],
        "variables_fuera_rango_sano": [],       # v4 (SC-I-04 R3-01): referencia de desviación para RF-66
        "detalle": {},
    }


def proyectar_a_rf64(p: np.ndarray) -> Dict[str, Any]:
    """Proyecta las 7 probabilidades internas al catálogo RF-64 (ids 0–7). SUPUESTO S3: la clase fusionada se
    reporta con la llave "3+4" (suma 1.0 con el resto); si gana, `enfermedad_estimada` = null y RF-66 la separa
    con el contexto hídrico (pH/TDS/OD), como indica ET-02 Tabla 2.2."""
    prob = {"+".join(str(j) for j in ids): round(float(p[i]), 4) for i, ids in IDX_A_RF64.items()}
    ids = IDX_A_RF64[int(np.argmax(p))]
    return {
        "probabilidad_enfermedad": prob,
        "enfermedad_estimada": ids[0] if len(ids) == 1 else None,
        "enfermedad_estimada_grupo": ids if len(ids) > 1 else None,
    }


def inferir_estado_individual(
    paquete: Dict[str, Any],
    linea_base: Optional[LineaBase],
    artefacto: Optional[ArtefactoModelo],
    ahora: Optional[datetime] = None,
    fase_productiva_actual: Optional[str] = None,
    todas_las_ventanas: bool = False,
    cfg: Dict[str, Any] = CONFIG,
) -> Any:
    """Inferencia en operación para un animal. Devuelve el contrato de la última ventana (o una lista)."""
    salida = _contrato_vacio(paquete if isinstance(paquete, dict) else {})

    # 1. Validación de estructura
    if not isinstance(paquete, dict):
        salida.update(estado="ERROR", detalle={"mensaje": "El paquete debe ser un diccionario."})
        return salida
    faltan = [c for c in ["id_animal", "id_especie", "lecturas"] if c not in paquete]
    if faltan:
        salida.update(estado="ERROR", detalle={"mensaje": "Faltan campos obligatorios.", "campos_faltantes": faltan})
        return salida
    if paquete["id_especie"] not in ESPECIES:
        salida.update(estado="ERROR", detalle={"mensaje": f"Especie no soportada: {paquete['id_especie']}"})
        return salida
    if not paquete["lecturas"]:
        salida.update(estado="ERROR", detalle={"mensaje": "El paquete no contiene lecturas."})
        return salida

    # 2. Línea base y artefacto
    estado_cal = estado_calibracion(linea_base, ahora, fase_productiva_actual, cfg)
    salida["estado_calibracion_animal"] = estado_cal
    if linea_base is None or artefacto is None:
        salida.update(estado="NO_EJECUTA", detalle={"mensaje": "Sin línea base o sin artefacto para este animal."})
        return salida
    if linea_base.id_animal != paquete["id_animal"] or artefacto.metadatos.get("id_animal") != paquete["id_animal"]:
        salida.update(estado="ERROR", detalle={"mensaje": "El artefacto o la línea base pertenecen a otro animal."})
        return salida
    t_inicio = time.perf_counter()
    salida.update(tipo_modelo=artefacto.metadatos["tipo_modelo"])
    salida.update(version_modelo=artefacto.metadatos["version_modelo"], hash_artefacto_modelo=artefacto.hash,
                  fuente_datos_modelo=artefacto.metadatos["fuente_datos"],
                  proporcion_sintetica=artefacto.metadatos["proporcion_sintetica"])

    # 3. Validación + ventaneo + features (MISMO código que en entrenamiento)
    try:
        df = pd.DataFrame(paquete["lecturas"])
        feat, eventos, resumen = pipeline_ventanas_features(df, linea_base, modo="operacion", cfg=cfg)
    except Exception as exc:  # noqa: BLE001
        salida.update(estado="ERROR", detalle={"mensaje": f"Lecturas inválidas: {exc}"})
        return salida
    salida["eventos_regla_dura_rf17"] = eventos
    if feat.empty:
        salida.update(estado="NO_EJECUTA", detalle={"mensaje": "No hay lecturas suficientes para formar una ventana.", **resumen})
        return salida

    filas = feat if todas_las_ventanas else feat.iloc[[-1]]
    proba = artefacto.predict_proba(filas[FEATURES].to_numpy(dtype=float))
    resultados = []
    for (_, f), p in zip(filas.iterrows(), proba):
        s = copy.deepcopy(salida)
        s.update(
            ventana_inicio=f["ventana_inicio"].isoformat(),
            ventana_fin=f["ventana_fin"].isoformat(),
            completitud_ventana=round(float(f["completitud_ventana"]), 4),
            variables_ausentes=list(f["variables_ausentes"]),
            contexto_incompleto=bool(f["contexto_incompleto"]),
            sincronizacion_degradada=bool(f["sincronizacion_degradada"]),
            desviaciones_linea_base={var: (None if pd.isna(f[f"{ABREV[var]}_z"]) else round(float(f[f"{ABREV[var]}_z"]), 3))
                                     for var in VARIABLES},
            variables_fuera_rango_sano=[var for var in VARIABLES if pd.notna(f[f"{ABREV[var]}_media"]) and not (
                ESPECIES[paquete["id_especie"]]["rango_sano"][var][0] <= f[f"{ABREV[var]}_media"]
                <= ESPECIES[paquete["id_especie"]]["rango_sano"][var][1])],
            detalle={"resumen_validacion": resumen},
        )
        if not f["cobertura_suficiente"]:
            s.update(estado="NO_EJECUTA", detalle={"mensaje": "Cobertura de la ventana por debajo del mínimo.", "resumen_validacion": resumen})
        else:
            orden = np.argsort(p)[::-1]
            s.update(
                probabilidades={NOMBRES_CLASES[c]: round(float(p[c]), 4) for c in range(N_CLASES)},
                clase_predicha=NOMBRES_CLASES[orden[0]],
                codigo_clase_predicha=CATALOGO_CLASES[orden[0]]["codigo"],
                confianza=round(float(p[orden[0]]), 4),                 # P(1)
                margen=round(float(p[orden[0]] - p[orden[1]]), 4),      # P(1) − P(2)
                **proyectar_a_rf64(p),
            )
        s.update(timestamp_inferencia=datetime.now(timezone.utc).isoformat(),
                 latencia_ms=int(round((time.perf_counter() - t_inicio) * 1000)))
        resultados.append(s)
    return resultados if todas_las_ventanas else resultados[0]


def aplicar_reglas_rf66(contrato: Dict[str, Any], cfg: Dict[str, Any] = CONFIG) -> Dict[str, Any]:
    """Implementación de REFERENCIA de la Tabla 2.4 (pertenece a RF-66, no al modelo)."""
    r = cfg["rf66"]
    d = {"utilizable": True, "vinculante": True, "modo_sombra": False, "contexto_incompleto": False,
         "evaluar_dos_hipotesis": False, "hipotesis_no_evaluables": [], "motivos": []}
    if contrato.get("estado") != "OK" or contrato.get("probabilidades") is None:
        d.update(utilizable=False, vinculante=False)
        d["motivos"].append("sin inferencia")
        return d
    if contrato["estado_calibracion_animal"] != "COMPLETADA":
        d.update(utilizable=False, vinculante=False)
        d["motivos"].append("sin línea base vigente: la desviación no es interpretable")
    if contrato["fuente_datos_modelo"] != "REAL":
        d["modo_sombra"] = True
        d["motivos"].append("modelo entrenado con datos sintéticos: calcula y registra, no alerta")
    if contrato["confianza"] < r["confianza_min"]:
        d["vinculante"] = False
        d["motivos"].append("confianza < 0.50: decide RF-66 con umbrales RF-17 y contexto")
    if contrato["margen"] < r["margen_min"]:
        d["evaluar_dos_hipotesis"] = True
        orden = sorted(contrato["probabilidades"].items(), key=lambda kv: kv[1], reverse=True)
        d["hipotesis_a_contrastar"] = [orden[0][0], orden[1][0]]
        d["motivos"].append("margen < 0.15: contrastar las dos primeras hipótesis con el contexto")
    if contrato["completitud_ventana"] < r["completitud_min"] or contrato.get("contexto_incompleto"):
        d["contexto_incompleto"] = True
        d["motivos"].append("contexto incompleto: no emitir alerta alta sustentada solo en este modelo")
    if "temperatura_corporal" in contrato["variables_ausentes"]:
        d["hipotesis_no_evaluables"] = list(CLASES_DEPENDEN_TEMPERATURA)
        d["motivos"].append("temperatura ausente: no se evalúan septicemia ni síndrome respiratorio")
    return d
