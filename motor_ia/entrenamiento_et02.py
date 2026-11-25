# | Fecha      | Nombre   | Que se hizo |
# |------------|----------|-------------|
# | 2026-11-03 | Arekkazu | AJF-14 RF-71 P-6: pipeline de entrenamiento del modelo individual sobre el contrato de ET-02 v5 |
"""Pipeline de ENTRENAMIENTO del modelo individual (RF-71) sobre el contrato de operación de ET-02.

No es copia de ``modelos_individuales_ET02_v5.py``: ese script no está en ningún repositorio (P-6).
Es la implementación de SIGAB, y todo lo que fija el contrato del artefacto sale de ``et02.py``,
la copia literal con la que el motor infiere: validación de lecturas, grilla, ventaneo, las 14
features en su orden, las 7 clases, la proyección a RF-64 y ``CONFIG`` (hiperparámetros, grilla,
balanceo, split, criterios de aceptación y regla de retiro). Así un modelo entrenado aquí lo usa
``et02.inferir_estado_individual`` sin cambiar una línea.

Lo propio de este archivo (``entrenar_modelo_individual``):

  1. ``calibrar_animal``: línea base del animal con sus lecturas sanas (Tabla 6.1).
  2. ``generar_linea_temporal_sintetica``: ciclos con un episodio de cada patología sobre la línea
     base y el perfil circadiano del animal. Perfiles NO clínicos (dirección y orden de magnitud de
     cada clase): sirven para que el modelo aprenda a separar desviaciones, no para describirlas.
  3. Ventanas reales etiquetadas con las retroalimentaciones de RF-72 y regla de retiro (§5.5):
     sin etiquetas → SINTETICA; con pocas → MIXTA; con las 7 clases cubiertas → REAL.
  4. ``entrenar_xgb_con_grilla``: split por ciclos (sintéticos) y cronológico (reales), SMOTE de la
     clase crítica, grilla acotada sobre validación y reajuste con el número de árboles elegido.
  5. ``evaluar_criterios_aceptacion``: métricas de prueba, línea base de reglas, sobreajuste,
     fuga, estabilidad walk-forward, sensibilidad del generador y real vs. sintético.
  6. ``exportar_onnx``: mismo grafo que los artefactos de ET-02 (entrada ``features`` [N, 14],
     salidas ``label`` y ``probabilities`` [N, 7]); se comprueba contra el modelo antes de entregar.

Un aborto se informa con ``ErrorEntrenamiento(código)``; el reentrenador lo lleva tal cual al
proceso (``codigo_error``). Lo que no aborta pero impide registrar va en ``motivo_no_registrable``.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import itertools
import math
import time
import unicodedata
import warnings
from collections.abc import Iterable
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from motor_ia import et02
from motor_ia.et02 import (
    ABREV,
    CONFIG,
    ESPECIES,
    FEATURES,
    IDX_A_RF64,
    N_CLASES,
    NOMBRES_CLASES,
    VARIABLES,
)

VERSION_ENTRENAMIENTO = "SIGAB-RF71-1.0"

#: Código RF-64 (0-7) → clase interna (la 3 agrupa los códigos 3 y 4).
RF64_A_CLASE = {rf: idx for idx, codigos in IDX_A_RF64.items() for rf in codigos}

#: Lecturas mínimas de una variable para que su media y desviación sean del animal.
MIN_LECTURAS_VARIABLE = 12
#: Piso de la desviación de la línea base: evita z explosivas con un sensor muy estable.
SIGMA_MINIMA = {"frecuencia_cardiaca": 1.0, "temperatura_corporal": 0.05, "actividad_indice": 0.02}
#: Desviación por defecto si el animal no tiene lecturas de una variable.
SIGMA_REFERENCIA = {
    "frecuencia_cardiaca": 5.0,
    "temperatura_corporal": 0.3,
    "actividad_indice": 0.15,
}
#: Precisión del sensor (MAX30102, DS18B20, MPU-6050) para el ruido ε_s del generador.
PRECISION_SENSOR = {
    "frecuencia_cardiaca": 2.0,
    "temperatura_corporal": 0.1,
    "actividad_indice": 0.05,
}

#: Perfil de cada clase en el generador (no clínico). ``dur_h``: duración del episodio; ``subida``
#: y ``bajada``: fracción de la duración en que Φ sube de 0 a 1 y vuelve; ``fc`` (lpm, escalado a
#: la FC basal de la especie) y ``temp`` (°C): desviación en la meseta; ``act``: factor de la
#: actividad en la meseta; ``var_fc``: aumento relativo de la variabilidad de la FC; ``hora``:
#: ventana de hora local en que puede empezar (None = cualquiera).
PERFILES_CLASE: dict[int, dict[str, Any]] = {
    1: {
        "dur_h": (4, 10),
        "subida": 0.30,
        "bajada": 0.25,
        "fc": 22.0,
        "temp": 1.3,
        "act": 0.55,
        "var_fc": 0.3,
        "hora": (10, 14),
    },  # ESTRES_TERMICO
    2: {
        "dur_h": (48, 120),
        "subida": 0.50,
        "bajada": 0.20,
        "fc": 14.0,
        "temp": 0.35,
        "act": 0.60,
        "var_fc": 0.0,
        "hora": None,
    },  # DESHIDRATACION
    3: {
        "dur_h": (24, 72),
        "subida": 0.30,
        "bajada": 0.20,
        "fc": 18.0,
        "temp": 0.8,
        "act": 0.50,
        "var_fc": 0.8,
        "hora": None,
    },  # RIESGO_ORIGEN_HIDRICO
    4: {
        "dur_h": (4, 10),
        "subida": 0.30,
        "bajada": 0.25,
        "fc": -14.0,
        "temp": -1.7,
        "act": 0.40,
        "var_fc": 0.0,
        "hora": (0, 4),
    },  # HIPOTERMIA_ESTRES_FRIO
    5: {
        "dur_h": (24, 72),
        "subida": 0.20,
        "bajada": 0.15,
        "fc": 34.0,
        "temp": 2.1,
        "act": 0.30,
        "var_fc": 0.5,
        "hora": None,
    },  # SEPTICEMIA
    6: {
        "dur_h": (36, 96),
        "subida": 0.30,
        "bajada": 0.20,
        "fc": 26.0,
        "temp": 1.2,
        "act": 0.55,
        "var_fc": 1.0,
        "hora": None,
    },  # SINDROME_RESPIRATORIO_AGUDO
}
FC_BASAL_REFERENCIA = 68.0  # la de BOVINO: los lpm de PERFILES_CLASE están en esa escala

#: Nombres de especie del catálogo (M09) que no coinciden con la clave de ET-02.
_ALIAS_ESPECIE = {
    "VACA": "BOVINO",
    "VACUNO": "BOVINO",
    "BOVINA": "BOVINO",
    "GANADO_BOVINO": "BOVINO",
    "CABALLO": "EQUINO",
    "YEGUA": "EQUINO",
    "EQUINA": "EQUINO",
    "BUFALA": "BUFALO",
    "BUFALINO": "BUFALO",
    "OVEJA": "OVINO",
    "OVINA": "OVINO",
    "CABRA": "CAPRINO",
    "CAPRINA": "CAPRINO",
    "BECERRO": "TERNERO",
    "TERNERA": "TERNERO",
    "POTRO": "EQUINO_JOVEN",
    "POTRA": "EQUINO_JOVEN",
}
#: Especie de referencia de cada tipo de modelo, si la del animal no se reconoce.
_ESPECIE_DE_TIPO = {"MODELO_ESPECIES_GRANDES": "BOVINO", "MODELO_ESPECIES_MEDIANAS": "OVINO"}


class ErrorEntrenamiento(RuntimeError):
    """Aborto del pipeline con su código (CALIBRACION_INCOMPLETA, DATASET_DESBALANCEADO…)."""

    def __init__(self, codigo: str, mensaje: str = "") -> None:
        super().__init__(f"{codigo}: {mensaje}" if mensaje else codigo)
        self.codigo = codigo


# ── utilidades ───────────────────────────────────────────────────────────────


def _py(valor: Any) -> Any:
    """numpy/pandas → tipos de Python (los metadatos viajan por JSON y por una cola de procesos)."""
    if isinstance(valor, dict):
        return {str(k): _py(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_py(v) for v in valor]
    if isinstance(valor, np.ndarray):
        return [_py(v) for v in valor.tolist()]
    if isinstance(valor, np.integer):
        return int(valor)
    if isinstance(valor, (float, np.floating)):
        v = float(valor)
        return None if math.isnan(v) or math.isinf(v) else v
    if isinstance(valor, np.bool_):
        return bool(valor)
    if isinstance(valor, pd.Timestamp):
        return valor.isoformat()
    return valor


def _normalizar_nombre(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return "_".join(sin_tildes.upper().replace("-", " ").split())


def resolver_especie(especie: str | None, tipo_modelo: str | None = None) -> str:
    """Nombre de especie del catálogo (``Bovino``, ``Búfalo``…) → clave de ``et02.ESPECIES``.

    Si no se reconoce, la especie de referencia del tipo de modelo del animal.
    """
    if especie:
        nombre = _normalizar_nombre(especie)
        candidatos = [nombre, _ALIAS_ESPECIE.get(nombre)]
        if nombre.endswith("S"):
            candidatos += [nombre[:-1], _ALIAS_ESPECIE.get(nombre[:-1])]
        for c in candidatos:
            if c in et02.ESPECIES_POBLACIONALES:
                raise ErrorEntrenamiento(
                    "ESPECIE_POBLACIONAL", f"{especie} es de paradigma POBLACIONAL (ET-01)."
                )
            if c in ESPECIES:
                return c
        if nombre in _ESPECIE_DE_TIPO:
            return _ESPECIE_DE_TIPO[nombre]
    if tipo_modelo in _ESPECIE_DE_TIPO:
        return _ESPECIE_DE_TIPO[tipo_modelo]
    raise ErrorEntrenamiento("ESPECIE_NO_SOPORTADA", f"Especie sin perfil en ET-02: {especie!r}.")


def lecturas_a_dataframe(lecturas: Iterable[dict] | pd.DataFrame) -> pd.DataFrame:
    """Lecturas del backend (formato largo: ``timestamp``, ``variable``, ``valor``,
    ``apto_para_ia``) o filas ya anchas → columnas ``timestamp`` + ``VARIABLES`` de ET-02."""
    df = lecturas.copy() if isinstance(lecturas, pd.DataFrame) else pd.DataFrame(list(lecturas))
    if df.empty:
        return pd.DataFrame(columns=["timestamp", *VARIABLES, "apto_para_ia"])
    if "variable" in df.columns and "valor" in df.columns:
        df = df[df["variable"].isin(VARIABLES)].copy()
        for var in VARIABLES:
            df[var] = np.where(
                df["variable"] == var, pd.to_numeric(df["valor"], errors="coerce"), np.nan
            )
        df = df.drop(columns=["variable", "valor"])
    for var in VARIABLES:
        if var not in df.columns:
            df[var] = np.nan
    # Un único formato para toda la serie: pandas fija el formato con la primera fila.
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
    if "apto_para_ia" in df.columns:
        df["apto_para_ia"] = df["apto_para_ia"].fillna(False).astype(bool)
    return df.sort_values("timestamp").reset_index(drop=True)


def _rango_dias(df: pd.DataFrame) -> float:
    con_valor = df[VARIABLES].notna().any(axis=1)
    if not con_valor.any():
        return 0.0
    ts = df.loc[con_valor, "timestamp"]
    return (ts.max() - ts.min()).total_seconds() / 86400.0


def _perfil_generico(var: str, mu: float, hora: np.ndarray) -> np.ndarray:
    """Forma circadiana por defecto (solo si el animal no tiene lecturas a esa hora)."""
    fase = 2 * np.pi * (hora - 14.0) / 24.0
    if var == "actividad_indice":
        return np.clip(mu * (1 + 0.6 * np.cos(fase)), 0.0, 1.0)
    if var == "temperatura_corporal":
        return mu + 0.25 * np.cos(fase)
    return mu * (1 + 0.05 * np.cos(fase))


# ── 1. calibración (Tabla 6.1) ───────────────────────────────────────────────


def calibrar_animal(
    df: pd.DataFrame,
    especie: str,
    id_animal: str,
    cfg: dict[str, Any] = CONFIG,
    dias_minimos: float | None = None,
) -> et02.LineaBase:
    """Línea base del animal con sus lecturas sanas (``validar_lecturas`` en modo calibración)."""
    minimos = cfg["calibracion"]["dias_minimos"] if dias_minimos is None else dias_minimos
    valido, _, resumen = et02.validar_lecturas(df, especie, cfg, modo="calibracion")
    dias = _rango_dias(valido)
    if dias < minimos * 0.9:
        raise ErrorEntrenamiento(
            "CALIBRACION_INCOMPLETA",
            f"{dias:.1f} días de lecturas aptas y sanas; la calibración pide {minimos:g}.",
        )
    basal = ESPECIES[especie]["basal"]
    zona = cfg["zona_horaria"]
    hora = valido["timestamp"].dt.tz_convert(zona).dt.hour
    mu: dict[str, float] = {}
    sigma: dict[str, float] = {}
    perfil: dict[str, list[float | None]] = {}
    n_lecturas = 0
    for var in VARIABLES:
        valores = valido[var].dropna()
        if len(valores) >= MIN_LECTURAS_VARIABLE:
            mu[var] = float(valores.mean())
            sigma[var] = max(float(valores.std(ddof=0)), SIGMA_MINIMA[var])
            n_lecturas += len(valores)
        else:
            mu[var] = float(basal[ABREV[var]])
            sigma[var] = SIGMA_REFERENCIA[var]
        por_hora = valido[var].groupby(hora).mean()
        perfil[var] = [
            None if pd.isna(por_hora.get(h)) else round(float(por_hora.get(h)), 4)
            for h in range(24)
        ]
    if n_lecturas == 0:
        raise ErrorEntrenamiento(
            "CALIBRACION_INCOMPLETA", f"Sin lecturas sanas suficientes ({resumen})."
        )
    ts = valido["timestamp"]
    return et02.LineaBase(
        id_animal=id_animal,
        especie=especie,
        mu=mu,
        sigma=sigma,
        perfil_circadiano=perfil,
        inicio=ts.min().isoformat(),
        fin=ts.max().isoformat(),
        n_lecturas=int(n_lecturas),
        fase_productiva=None,
        estado="COMPLETADA",
    )


# ── 2. generador sintético (Cap. 5) ──────────────────────────────────────────


def _perfil_continuo(lb: et02.LineaBase, var: str, hora: np.ndarray) -> np.ndarray:
    """Perfil circadiano del animal interpolado a la hora local (circular); huecos → genérico."""
    crudo = lb.perfil_circadiano.get(var) or [None] * 24
    horas = np.arange(24, dtype=float)
    base = np.array([np.nan if v is None else float(v) for v in crudo], dtype=float)
    if np.isnan(base).all():
        return _perfil_generico(var, lb.mu[var], hora)
    generico = _perfil_generico(var, lb.mu[var], horas)
    base = np.where(np.isnan(base), generico, base)
    x = np.concatenate([horas - 24, horas, horas + 24])
    y = np.concatenate([base, base, base])
    return np.interp(hora, x, y)


def _phi(t_rel: np.ndarray, subida: float, bajada: float) -> np.ndarray:
    """Intensidad del episodio en [0, 1] con ``t_rel`` en [0, 1]: sube, meseta y baja (suave)."""

    def suave(x: np.ndarray) -> np.ndarray:
        x = np.clip(x, 0.0, 1.0)
        return x * x * (3 - 2 * x)

    return np.minimum(suave(t_rel / subida), suave((1.0 - t_rel) / bajada))


def generar_linea_temporal_sintetica(
    lb: et02.LineaBase, cfg: dict[str, Any] = CONFIG, semilla: int = 42, **opciones: Any
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Paso 2 (fase PREPROCESAMIENTO): la línea temporal con la que se entrena."""
    return _generar_linea(lb, cfg, semilla, **opciones)


def _generar_linea(
    lb: et02.LineaBase,
    cfg: dict[str, Any] = CONFIG,
    semilla: int = 42,
    *,
    n_ciclos: int | None = None,
    factor_magnitud: float = 1.0,
    factor_tiempo: float = 1.0,
    inicio: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Lecturas sintéticas de ``n_ciclos`` ciclos; cada ciclo tiene un episodio de cada patología
    en orden aleatorio, separados por tramos sanos (``hueco_sano_h``). Devuelve las lecturas con
    ``etiqueta``, ``id_episodio`` y ``ciclo`` y el control de calidad del generador."""
    g = cfg["generador"]
    rng = np.random.default_rng(semilla)
    n_ciclos = n_ciclos or g["n_ciclos"]
    periodo_min = cfg["ventaneo"]["periodo_muestreo_min"]
    zona = cfg["zona_horaria"]
    inicio = inicio if inicio is not None else pd.Timestamp("2025-01-01T00:00:00Z")
    offset_local_h = inicio.tz_convert(zona).utcoffset().total_seconds() / 3600.0

    # Agenda de episodios (horas desde el inicio).
    episodios: list[tuple[int, float, float, float, int]] = []  # (clase, t0, dur, magnitud, ciclo)
    t = 0.0
    for ciclo in range(n_ciclos):
        for clase in rng.permutation(list(PERFILES_CLASE)):
            p = PERFILES_CLASE[int(clase)]
            t += rng.uniform(*g["hueco_sano_h"])
            if p["hora"] is not None:  # empieza en su franja de hora local
                h_local = (t + offset_local_h) % 24
                h0 = rng.uniform(*p["hora"])
                t += (h0 - h_local) % 24
            dur = rng.uniform(*p["dur_h"]) * factor_tiempo
            magnitud = rng.uniform(0.7, 1.3) * factor_magnitud
            episodios.append((int(clase), t, dur, magnitud, ciclo))
            t += dur
    t += rng.uniform(*g["hueco_sano_h"])

    n = int(t * 60 // periodo_min)
    horas = np.arange(n) * periodo_min / 60.0
    hora_local = (horas + offset_local_h) % 24
    ciclo_de = np.zeros(n, dtype=int)
    etiqueta = np.zeros(n, dtype=int)
    id_episodio = np.full(n, np.nan)
    phi = np.zeros(n)
    efecto = {"fc": np.zeros(n), "temp": np.zeros(n), "act": np.ones(n), "var_fc": np.zeros(n)}
    escala_fc = ESPECIES[lb.especie]["basal"]["fc"] / FC_BASAL_REFERENCIA
    for k, (clase, t0, dur, mag, ciclo) in enumerate(episodios):
        dentro = (horas >= t0) & (horas < t0 + dur)
        ciclo_de[horas >= t0] = ciclo
        p = PERFILES_CLASE[clase]
        f = _phi((horas[dentro] - t0) / dur, p["subida"], p["bajada"]) * mag
        phi[dentro] = f
        etiqueta[dentro] = clase
        id_episodio[dentro] = k
        efecto["fc"][dentro] += f * p["fc"] * escala_fc
        efecto["temp"][dentro] += f * p["temp"]
        efecto["act"][dentro] *= np.maximum(1 - f * (1 - p["act"]), g["escala_multiplicativa_min"])
        efecto["var_fc"][dentro] += f * p["var_fc"]

    def ar1(sd: float, coef: float = 0.9) -> np.ndarray:
        """Ruido AR(1) estacionario con desviación ``sd`` (deriva lenta, como la fisiología)."""
        from scipy.signal import lfilter

        ruido = rng.normal(0.0, sd * math.sqrt(1 - coef**2), n)
        inicial = np.array([coef * rng.normal(0.0, sd)])
        return lfilter([1.0], [1.0, -coef], ruido, zi=inicial)[0]

    valores: dict[str, np.ndarray] = {}
    for var in VARIABLES:
        perfil = _perfil_continuo(lb, var, hora_local)
        sd_res = math.sqrt(
            max(lb.sigma[var] ** 2 - float(np.var(perfil)), (0.35 * lb.sigma[var]) ** 2)
        )
        # 60 % de la varianza residual es deriva lenta (AR(1)); 40 %, variación entre lecturas.
        ruido = ar1(sd_res * math.sqrt(0.6)) + rng.normal(0.0, sd_res * math.sqrt(0.4), n)
        sensor = rng.normal(0.0, g["factor_ruido_sensor"] * PRECISION_SENSOR[var], n)
        if var == "frecuencia_cardiaca":
            valores[var] = perfil + efecto["fc"] + ruido * (1 + efecto["var_fc"]) + sensor
        elif var == "temperatura_corporal":
            valores[var] = perfil + efecto["temp"] + ruido + sensor
        else:
            valores[var] = (perfil + ruido) * efecto["act"] + sensor

    # Faltantes: lecturas sueltas y cortes del collar (todas las variables a la vez).
    for var in VARIABLES:
        valores[var][rng.random(n) < 0.015] = np.nan
    for _ in range(int(t // 72)):
        a = int(rng.integers(0, n))
        largo = int(rng.integers(6, 19))  # 30–90 min
        for var in VARIABLES:
            valores[var][a : a + largo] = np.nan

    # Recorte a la banda plausible de la especie (control de calidad del generador).
    control: dict[str, Any] = {
        "fraccion_recortada_por_clase": {},
        "valores_fuera_rango_plausible": {},
    }
    recortes: dict[str, np.ndarray] = {}
    for var in VARIABLES:
        pmin, pmax = ESPECIES[lb.especie]["rango_plausible"][var]
        v = valores[var]
        fuera = ~np.isnan(v) & ((v < pmin) | (v > pmax))
        recortes[var] = fuera
        valores[var] = np.where(np.isnan(v), np.nan, np.clip(v, pmin, pmax))
        control["valores_fuera_rango_plausible"][var] = 0  # tras el recorte
    for clase in PERFILES_CLASE:
        en_clase = etiqueta == clase
        control["fraccion_recortada_por_clase"][NOMBRES_CLASES[clase]] = {
            ABREV[var]: round(float(recortes[var][en_clase].mean()) if en_clase.any() else 0.0, 4)
            for var in VARIABLES
        }
    piso = valores["actividad_indice"] <= 0.0
    control["fraccion_piso_actividad_max"] = round(
        max(
            (float(piso[etiqueta == c].mean()) for c in PERFILES_CLASE if (etiqueta == c).any()),
            default=0.0,
        ),
        4,
    )
    control["episodios"] = len(episodios)
    control["ciclos"] = n_ciclos
    control["dias"] = round(t / 24.0, 1)

    df = pd.DataFrame(
        {
            "timestamp": inicio + pd.to_timedelta(horas, unit="h"),
            **valores,
            "etiqueta": etiqueta,
            "id_episodio": id_episodio,
            "ciclo": ciclo_de,
            "apto_para_ia": True,
        }
    )
    return df, control


# ── ventanas, etiquetas reales y regla de retiro ─────────────────────────────


def ventanas_con_features(
    df: pd.DataFrame, lb: et02.LineaBase, cfg: dict[str, Any] = CONFIG
) -> pd.DataFrame:
    """El mismo código de ventaneo y features con el que infiere el motor (modo entrenamiento)."""
    feats, _, _ = et02.pipeline_ventanas_features(df, lb, modo="entrenamiento", cfg=cfg)
    return feats


def etiqueta_de_retroalimentacion(r: dict[str, Any]) -> int | None:
    """Clase interna que confirma una retroalimentación de RF-72, o None si no se puede saber.

    SIN_EVENTO → NINGUNA. CORRECTO → la clase que predijo la inferencia (sin código RF-64 y con
    nivel 0, NINGUNA; sin código y con nivel > 0 ganó el grupo 3+4). PARCIAL o INCORRECTO → el
    primer diagnóstico real (RF-72: la etiqueta primaria es ``diagnosticos_reales[0]``).
    """
    estado = r.get("estado_retroalimentacion")
    if estado == "SIN_EVENTO":
        return 0
    if estado == "CORRECTO":
        codigo = r.get("codigo_rf64_inferido")
        if codigo is None:
            nivel = r.get("nivel_riesgo_inferido")
            if nivel is None:
                return None
            return 0 if int(nivel) == 0 else RF64_A_CLASE[3]
        return RF64_A_CLASE.get(int(codigo))
    if estado in ("PARCIAL", "INCORRECTO"):
        codigos = r.get("codigos_rf64_reales") or []
        return RF64_A_CLASE.get(int(codigos[0])) if codigos else None
    return None


def etiquetar_ventanas_reales(
    ventanas: pd.DataFrame, retroalimentaciones: Iterable[dict[str, Any]]
) -> pd.Series:
    """Etiqueta (o -1) de cada ventana real según las retroalimentaciones cuya ventana de
    inferencia se solapa con ella. Si dos se contradicen, gana la más reciente."""
    etiqueta = pd.Series(-1, index=ventanas.index, dtype=int)
    if ventanas.empty:
        return etiqueta
    orden = sorted(
        (
            r
            for r in retroalimentaciones
            if r.get("fecha_inicio_ventana") and r.get("fecha_fin_ventana")
        ),
        key=lambda r: str(r.get("fecha_retroalimentacion") or ""),
    )
    for r in orden:
        clase = etiqueta_de_retroalimentacion(r)
        if clase is None:
            continue
        ini = pd.Timestamp(r["fecha_inicio_ventana"]).tz_convert("UTC")
        fin = pd.Timestamp(r["fecha_fin_ventana"]).tz_convert("UTC")
        solapa = (ventanas["ventana_inicio"] < fin) & (ventanas["ventana_fin"] > ini)
        etiqueta[solapa] = clase
    return etiqueta


def aplicar_regla_retiro(
    y_real_train: np.ndarray, n_reales_total: int, cfg: dict[str, Any] = CONFIG
) -> dict[str, Any]:
    """§5.5: cuánto pesa lo sintético según las ventanas reales etiquetadas.

    Sin etiquetas → SINTETICA (ρ = 1). Con menos de ``n_reales_min`` o una fracción etiquetada menor
    que ``fraccion_verificadas_min`` → MIXTA con peso sintético completo. Por encima de eso, la
    clase con ``min_muestras_clase`` reales baja su sintético a ``peso_sintetico_residual``; si las
    7 clases están cubiertas, se retira todo lo sintético y el modelo es REAL.
    """
    r = cfg["retiro"]
    minimo = cfg["balanceo"]["min_muestras_clase"]
    n_reales = int(len(y_real_train))
    fraccion = n_reales / n_reales_total if n_reales_total else 0.0
    pesos = dict.fromkeys(range(N_CLASES), float(r["rho_inicial"]))
    resultado = {
        "n_reales_etiquetadas": n_reales,
        "fraccion_etiquetada": round(fraccion, 4),
        "clases_con_sinteticos_residuales": [],
        "retiro_total": False,
    }
    if n_reales == 0:
        return {**resultado, "fuente_datos_modelo": "SINTETICA", "pesos_sinteticos": pesos}
    if n_reales < r["n_reales_min"] or fraccion < r["fraccion_verificadas_min"]:
        return {**resultado, "fuente_datos_modelo": "MIXTA", "pesos_sinteticos": pesos}
    conteo = np.bincount(y_real_train, minlength=N_CLASES)
    cubiertas = [c for c in range(N_CLASES) if conteo[c] >= minimo]
    if len(cubiertas) == N_CLASES:
        return {
            **resultado,
            "fuente_datos_modelo": "REAL",
            "retiro_total": True,
            "pesos_sinteticos": dict.fromkeys(range(N_CLASES), 0.0),
        }
    for c in cubiertas:
        pesos[c] = float(r["peso_sintetico_residual"])
    return {
        **resultado,
        "fuente_datos_modelo": "MIXTA",
        "pesos_sinteticos": pesos,
        "clases_con_sinteticos_residuales": [
            NOMBRES_CLASES[c] for c in range(N_CLASES) if c not in cubiertas
        ],
    }


# ── 4. entrenamiento ─────────────────────────────────────────────────────────


def _parametros_xgb(cfg: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    base = {
        k: v for k, v in cfg["xgboost"].items() if k not in ("num_class", "early_stopping_rounds")
    }
    return {**base, **extra, "n_jobs": cfg["recursos"]["n_jobs"], "random_state": cfg["semilla"]}


def _balancear(
    X: np.ndarray, y: np.ndarray, w: np.ndarray, cfg: dict[str, Any], semilla: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, Any]]:
    """Tabla 4.3: SMOTE de la clase crítica si queda bajo ``umbral_smote`` y submuestreo de
    NINGUNA si pasa de ``umbral_submuestreo`` (con muchas ventanas)."""
    b = cfg["balanceo"]
    info: dict[str, Any] = {"smote": False, "submuestreo": False}
    rng = np.random.default_rng(semilla)
    n = len(y)
    if (y == 0).mean() > b["umbral_submuestreo"] and n >= b["min_ventanas_submuestreo"]:
        ningunas = np.flatnonzero(y == 0)
        otras = n - len(ningunas)
        objetivo = int(otras * b["umbral_submuestreo"] / (1 - b["umbral_submuestreo"]))
        quedan = np.sort(
            np.concatenate([np.flatnonzero(y != 0), rng.choice(ningunas, objetivo, replace=False)])
        )
        X, y, w = X[quedan], y[quedan], w[quedan]
        info["submuestreo"] = True
    for c in b["clases_criticas"]:
        n_c, n = int((y == c).sum()), len(y)
        if n_c / n >= b["umbral_smote"] or n_c < 2:
            continue
        from imblearn.over_sampling import SMOTE

        meta = int(math.ceil((b["objetivo_smote"] * n - n_c) / (1 - b["objetivo_smote"]))) + n_c
        # SMOTE no admite NaN: interpola sobre las medianas y las originales conservan sus NaN.
        medianas = np.nan_to_num(np.nanmedian(X, axis=0))
        X_lleno = np.where(np.isnan(X), medianas, X)
        smote = SMOTE(
            sampling_strategy={c: meta}, k_neighbors=min(5, n_c - 1), random_state=semilla
        )
        X_res, y_res = smote.fit_resample(X_lleno, y)
        nuevas = X_res[n:]  # imblearn agrega las sintéticas al final
        peso_c = float(w[y == c].mean())
        X = np.vstack([X, nuevas])
        y = np.concatenate([y, y_res[n:]])
        w = np.concatenate([w, np.full(len(nuevas), peso_c)])
        info.update(smote=True, clase=NOMBRES_CLASES[c], muestras_nuevas=int(len(nuevas)))
    return X, y, w, info


def entrenar_xgb_con_grilla(
    X_tr: np.ndarray,
    y_tr: np.ndarray,
    w_tr: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    w_val: np.ndarray,
    cfg: dict[str, Any] = CONFIG,
) -> tuple[Any, dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Grilla acotada sobre validación (F1 macro) con parada temprana; el elegido se reajusta con
    el número de árboles hallado, para que el ONNX tenga exactamente los árboles que se evaluaron.
    Devuelve (modelo, parámetros, resultados de la grilla, balanceo aplicado)."""
    from sklearn.metrics import f1_score
    from xgboost import XGBClassifier

    conteo = np.bincount(y_tr, minlength=N_CLASES)
    escasas = [
        NOMBRES_CLASES[c]
        for c in range(N_CLASES)
        if conteo[c] < cfg["balanceo"]["min_muestras_clase"]
    ]
    if escasas:
        raise ErrorEntrenamiento(
            "DATASET_DESBALANCEADO",
            f"Clases con menos de {cfg['balanceo']['min_muestras_clase']} ventanas: {escasas}.",
        )
    X_tr, y_tr, w_tr, balanceo = _balancear(X_tr, y_tr, w_tr, cfg, cfg["semilla"])
    grilla = cfg["grid_busqueda"]
    claves = list(grilla)
    resultados: list[dict[str, Any]] = []
    mejor: tuple[float, dict[str, Any], int] | None = None
    for combinacion in itertools.product(*(grilla[k] for k in claves)):
        extra = dict(zip(claves, combinacion, strict=True))
        modelo = XGBClassifier(
            **_parametros_xgb(cfg, extra),
            early_stopping_rounds=cfg["xgboost"]["early_stopping_rounds"],
        )
        modelo.fit(
            X_tr,
            y_tr,
            sample_weight=w_tr,
            eval_set=[(X_val, y_val)],
            sample_weight_eval_set=[w_val],
            verbose=False,
        )
        arboles = int(modelo.best_iteration) + 1
        f1 = float(
            f1_score(
                y_val,
                modelo.predict(X_val),
                average="macro",
                labels=list(range(N_CLASES)),
                zero_division=0,
            )
        )
        resultados.append({**extra, "f1_validacion": round(f1, 4), "n_estimators": arboles})
        if mejor is None or f1 > mejor[0] + 1e-9:
            mejor = (f1, extra, arboles)
    assert mejor is not None
    _, extra, arboles = mejor
    parametros = _parametros_xgb(cfg, {**extra, "n_estimators": arboles})
    final = XGBClassifier(**parametros)
    final.fit(X_tr, y_tr, sample_weight=w_tr, verbose=False)
    return final, parametros, resultados, balanceo


# ── 5. métricas y criterios de aceptación ────────────────────────────────────


def calcular_metricas(
    y: np.ndarray, proba: np.ndarray, cfg: dict[str, Any] = CONFIG
) -> dict[str, Any]:
    """Las métricas que exige RF-69 (``metricas_validacion``) sobre el juego de prueba."""
    from sklearn.metrics import (
        accuracy_score,
        confusion_matrix,
        f1_score,
        precision_score,
        recall_score,
        roc_auc_score,
    )

    clases = list(range(N_CLASES))
    pred = proba.argmax(axis=1)
    recall = recall_score(y, pred, labels=clases, average=None, zero_division=0)
    alto = [NOMBRES_CLASES.index(c) for c in cfg["riesgo_alto"]["clases"]]
    es_alto = np.isin(y, alto)
    recall_alto = float(np.isin(pred[es_alto], alto).mean()) if es_alto.any() else 0.0
    aucs = []
    for c in clases:  # AUC uno-contra-resto de las clases presentes en la prueba
        positivo = y == c
        if positivo.any() and (~positivo).any():
            aucs.append(roc_auc_score(positivo, proba[:, c]))
    return {
        "f1_score_global": float(
            f1_score(y, pred, labels=clases, average="macro", zero_division=0)
        ),
        "recall_clase_riesgo_alto": recall_alto,
        "precision_global": float(
            precision_score(y, pred, labels=clases, average="macro", zero_division=0)
        ),
        "accuracy": float(accuracy_score(y, pred)),
        "recall_por_clase": {NOMBRES_CLASES[c]: float(recall[c]) for c in clases},
        "matriz_confusion": confusion_matrix(y, pred, labels=clases).tolist(),
        "roc_auc_score": float(np.mean(aucs)) if aucs else 0.0,
    }


def clasificar_por_reglas(ventanas: pd.DataFrame, lb: et02.LineaBase) -> np.ndarray:
    """Línea base de comparación: reglas de umbral sobre la desviación de la media de la ventana.
    El modelo tiene que superarla para que valga la pena desplegarlo."""
    d_temp = (ventanas["temp_media"] - lb.mu["temperatura_corporal"]).to_numpy()
    d_fc = (ventanas["fc_media"] - lb.mu["frecuencia_cardiaca"]).to_numpy()
    hora = ventanas["hora_local"].to_numpy()
    d_temp = np.nan_to_num(d_temp)
    d_fc = np.nan_to_num(d_fc)
    pred = np.zeros(len(ventanas), dtype=int)
    reglas = [  # en orden de prioridad (la primera que se cumple)
        (d_temp <= -1.0, 4),
        ((d_temp >= 1.6) & (d_fc >= 20), 5),
        ((d_temp >= 0.9) & (hora >= 10) & (hora <= 17), 1),
        (d_temp >= 0.9, 6),
        ((d_fc >= 10) & (d_temp >= 0.5), 3),
        (d_fc >= 10, 2),
    ]
    asignada = np.zeros(len(ventanas), dtype=bool)
    for condicion, clase in reglas:
        nuevo = condicion & ~asignada
        pred[nuevo] = clase
        asignada |= condicion
    return pred


def _f1(y: np.ndarray, pred: np.ndarray) -> float:
    from sklearn.metrics import f1_score

    return float(f1_score(y, pred, labels=list(range(N_CLASES)), average="macro", zero_division=0))


def _walk_forward(
    feats: pd.DataFrame, parametros: dict[str, Any], cfg: dict[str, Any]
) -> dict[str, Any]:
    """Plan §5.5: entrena con ``wf_ciclos_entrenamiento`` ciclos y prueba con el siguiente,
    avanzando ``wf_paso_ciclos``. σ(F1 macro) ≤ ``sigma_f1_max``."""
    from xgboost import XGBClassifier

    v = cfg["validacion"]
    ciclos = sorted(int(c) for c in feats["ciclo"].unique())
    k = v["wf_ciclos_entrenamiento"]
    inicios = list(range(0, len(ciclos) - k, v["wf_paso_ciclos"]))[: v["wf_max_iteraciones"]]
    base = {
        "unidad_prueba": v["wf_unidad_prueba"],
        "ciclos_entrenamiento": k,
        "min_iteraciones": v["wf_min_iteraciones"],
        "paso_ciclos": v["wf_paso_ciclos"],
        "max_iteraciones": v["wf_max_iteraciones"],
        "umbral_sigma": cfg["aceptacion"]["sigma_f1_max"],
    }
    if len(inicios) < v["wf_min_iteraciones"]:
        return {
            **base,
            "estado": "NO_EVALUADA",
            "iteraciones": len(inicios),
            "motivo": f"{len(ciclos)} ciclos: no alcanzan {v['wf_min_iteraciones']} iteraciones.",
        }
    f1s = []
    for i in inicios:
        entrena = feats["ciclo"].isin(ciclos[i : i + k])
        prueba = feats["ciclo"] == ciclos[i + k]
        y_e = feats.loc[entrena, "etiqueta"].to_numpy(dtype=int)
        if len(np.unique(y_e)) < N_CLASES:
            continue
        m = XGBClassifier(**parametros)
        m.fit(feats.loc[entrena, FEATURES].to_numpy(dtype=float), y_e, verbose=False)
        pred = m.predict(feats.loc[prueba, FEATURES].to_numpy(dtype=float))
        f1s.append(_f1(feats.loc[prueba, "etiqueta"].to_numpy(dtype=int), pred))
    if len(f1s) < v["wf_min_iteraciones"]:
        return {
            **base,
            "estado": "NO_EVALUADA",
            "iteraciones": len(f1s),
            "motivo": "Ciclos de entrenamiento sin las 7 clases.",
        }
    desv = float(np.std(f1s))
    return {
        **base,
        "estado": "OK" if desv <= cfg["aceptacion"]["sigma_f1_max"] else "INESTABLE",
        "iteraciones": len(f1s),
        "f1_media": round(float(np.mean(f1s)), 4),
        "f1_desv": round(desv, 4),
    }


def _real_vs_sintetico(
    reales: pd.DataFrame, sinteticas_sanas: pd.DataFrame, cfg: dict[str, Any], semilla: int
) -> dict[str, Any]:
    """¿Se distinguen las ventanas reales de las sintéticas sanas? AUC alto = generador irreal."""
    n = min(len(reales), len(sinteticas_sanas), 2000)
    umbral = cfg["aceptacion"]["auc_real_vs_sintetico_max"]
    if n < 100:
        return {"estado": "NO_EVALUADO", "n_por_grupo": int(n), "umbral": umbral}
    from sklearn.metrics import roc_auc_score
    from xgboost import XGBClassifier

    rng = np.random.default_rng(semilla)
    a = reales[FEATURES].to_numpy(dtype=float)[rng.choice(len(reales), n, replace=False)]
    b = sinteticas_sanas[FEATURES].to_numpy(dtype=float)[
        rng.choice(len(sinteticas_sanas), n, replace=False)
    ]
    X = np.vstack([a, b])
    y = np.concatenate([np.ones(n, dtype=int), np.zeros(n, dtype=int)])
    orden = rng.permutation(len(y))
    corte = int(len(y) * 0.7)
    entrena, prueba = orden[:corte], orden[corte:]
    m = XGBClassifier(
        n_estimators=50, max_depth=3, learning_rate=0.1, n_jobs=1, random_state=semilla
    )
    m.fit(X[entrena], y[entrena])
    auc = float(roc_auc_score(y[prueba], m.predict_proba(X[prueba])[:, 1]))
    auc = max(auc, 1 - auc)
    return {
        "estado": "OK" if auc <= umbral else "DISTINGUIBLE",
        "auc": round(auc, 4),
        "n_por_grupo": int(n),
        "umbral": umbral,
    }


def evaluar_criterios_aceptacion(
    *,
    modelo: Any,
    parametros: dict[str, Any],
    lb: et02.LineaBase,
    fuente_datos: str,
    X_tr: np.ndarray,
    y_tr: np.ndarray,
    prueba: pd.DataFrame,
    feats_sinteticas: pd.DataFrame,
    reales_sin_etiqueta: pd.DataFrame,
    cfg: dict[str, Any] = CONFIG,
    semilla: int = 42,
) -> dict[str, Any]:
    """Tabla 6.3 + Tabla 7.2. ``bloqueantes``: impiden registrar; ``alertas``: se registran."""
    a = cfg["aceptacion"]
    X_te = prueba[FEATURES].to_numpy(dtype=float)
    y_te = prueba["etiqueta"].to_numpy(dtype=int)
    proba = modelo.predict_proba(X_te)
    metricas = calcular_metricas(y_te, proba, cfg)
    f1_te = metricas["f1_score_global"]
    f1_tr = _f1(y_tr, modelo.predict(X_tr))
    brecha = round(f1_tr - f1_te, 4)
    f1_reglas = round(_f1(y_te, clasificar_por_reglas(prueba, lb)), 4)
    bloqueantes: list[str] = []
    alertas: list[str] = []

    if f1_te >= a["f1_fuga_sospechosa"]:
        bloqueantes.append(
            f"FUGA_SOSPECHOSA: F1 de prueba {f1_te:.4f} ≥ {a['f1_fuga_sospechosa']}."
        )
    if f1_te <= f1_reglas:
        bloqueantes.append(
            f"NO_SUPERA_LINEA_BASE: F1 {f1_te:.4f} no supera el de las reglas de umbral "
            f"({f1_reglas})."
        )
    if brecha > a["brecha_sobreajuste_max"]:
        alertas.append(
            f"SOBREAJUSTE: brecha entrenamiento-prueba {brecha} > {a['brecha_sobreajuste_max']}."
        )

    sinteticas = feats_sinteticas[feats_sinteticas["ciclo"].notna()]
    estabilidad = (
        _walk_forward(sinteticas, {**parametros, "n_jobs": parametros.get("n_jobs", 1)}, cfg)
        if fuente_datos != "REAL"
        else {"estado": "NO_EVALUADA", "motivo": "Modelo REAL: sin ciclos sintéticos."}
    )
    if estabilidad["estado"] == "INESTABLE":
        texto = (
            f"ESTABILIDAD_INSUFICIENTE: σ(F1) walk-forward {estabilidad['f1_desv']} > "
            f"{a['sigma_f1_max']}."
        )
        (bloqueantes if fuente_datos == "REAL" else alertas).append(texto)

    if fuente_datos == "REAL":
        # Plan §2.5: con datos reales, el recall de riesgo alto es criterio de despliegue.
        if metricas["recall_clase_riesgo_alto"] < 0.85:
            bloqueantes.append(
                "RECALL_RIESGO_ALTO_INSUFICIENTE: "
                f"{metricas['recall_clase_riesgo_alto']:.4f} < 0.85."
            )
    elif metricas["recall_clase_riesgo_alto"] < a["recall_severo_sintetico_referencia"]:
        alertas.append(
            f"RECALL_SEVERO_SINTETICO_BAJO: {metricas['recall_clase_riesgo_alto']:.4f} < "
            f"{a['recall_severo_sintetico_referencia']} (diagnóstico, no bloquea)."
        )

    real_vs_sint = _real_vs_sintetico(
        reales_sin_etiqueta, sinteticas[sinteticas["etiqueta"] == 0], cfg, semilla
    )
    if real_vs_sint["estado"] == "DISTINGUIBLE":
        alertas.append(
            f"GENERADOR_DISTINGUIBLE: AUC real vs. sintético {real_vs_sint['auc']} > "
            f"{a['auc_real_vs_sintetico_max']}."
        )
    if len(reales_sin_etiqueta):
        temp_ausente = float(reales_sin_etiqueta["ausente_temp"].mean())
        if temp_ausente > a["max_fraccion_temp_ausente"]:
            alertas.append(
                f"TEMPERATURA_AUSENTE: {temp_ausente:.2%} de las ventanas reales sin temperatura."
            )

    return {
        "metricas": metricas,
        "f1_entrenamiento": round(f1_tr, 4),
        "brecha_entrenamiento_prueba": brecha,
        "linea_base_superada": {"usada": "reglas_umbral", "f1": f1_reglas},
        "estabilidad_walk_forward": estabilidad,
        "real_vs_sintetico": real_vs_sint,
        "bloqueantes": bloqueantes,
        "alertas": alertas,
    }


def _sensibilidad(
    modelo: Any, lb: et02.LineaBase, f1_base: float, cfg: dict[str, Any], semilla: int
) -> dict[str, Any]:
    """Prueba de sensibilidad: el modelo frente a episodios 25 % más débiles y 50 % más largos."""
    alt, _ = _generar_linea(  # no la envuelta: es parte de las métricas, no del preprocesamiento
        lb,
        cfg,
        semilla + 7,
        n_ciclos=cfg["generador"]["ciclos_sensibilidad"],
        factor_magnitud=0.75,
        factor_tiempo=1.5,
    )
    feats = ventanas_con_features(alt, lb, cfg)
    f1_alt = _f1(
        feats["etiqueta"].to_numpy(dtype=int), modelo.predict(feats[FEATURES].to_numpy(dtype=float))
    )
    caida = round(f1_base - f1_alt, 4)
    return {
        "f1_juego_entrenamiento": round(f1_base, 4),
        "f1_juego_alternativo": round(f1_alt, 4),
        "caida": caida,
        "factor_magnitud": 0.75,
        "factor_tiempo": 1.5,
        "aprobado": caida <= cfg["aceptacion"]["caida_f1_sensibilidad_max"],
    }


# ── 6. exportación ───────────────────────────────────────────────────────────


def exportar_onnx(modelo: Any, X_muestra: np.ndarray) -> bytes:
    """ONNX con el mismo contrato que los artefactos de ET-02 y verificado contra el modelo."""
    from onnxmltools import convert_xgboost
    from onnxmltools.convert.common.data_types import FloatTensorType

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        onnx_modelo = convert_xgboost(
            modelo, initial_types=[("features", FloatTensorType([None, len(FEATURES)]))]
        )
    datos = onnx_modelo.SerializeToString()
    artefacto = et02.ArtefactoModelo(datos, {})
    esperado = modelo.predict_proba(X_muestra)
    obtenido = artefacto.predict_proba(X_muestra)
    if obtenido.shape != esperado.shape or not np.allclose(obtenido, esperado, atol=1e-4):
        raise ErrorEntrenamiento(
            "ONNX_NO_EQUIVALENTE", "El artefacto ONNX no reproduce las probabilidades del modelo."
        )
    return datos


# ── orquestación ─────────────────────────────────────────────────────────────


def _split_sintetico(feats: pd.DataFrame, cfg: dict[str, Any]) -> pd.Series:
    """Bloque por ciclo, en orden: entrenamiento / validación / prueba (12 / 3 / 3 con 18)."""
    g = cfg["generador"]
    referencia = CONFIG["generador"]["n_ciclos"]  # la proporción 12/3/3 de ET-02
    ciclos = sorted(int(c) for c in feats["ciclo"].unique())
    n = len(ciclos)
    n_prueba = min(g["ciclos_prueba"], max(1, round(n * g["ciclos_prueba"] / referencia)))
    n_val = min(g["ciclos_validacion"], max(1, round(n * g["ciclos_validacion"] / referencia)))
    if n - n_prueba - n_val < 1:
        raise ErrorEntrenamiento(
            "DATASET_INSUFICIENTE", f"{n} ciclos sintéticos no alcanzan para dividir."
        )
    bloque = {c: "entrenamiento" for c in ciclos[: n - n_prueba - n_val]}
    bloque.update({c: "validacion" for c in ciclos[n - n_prueba - n_val : n - n_prueba]})
    bloque.update({c: "prueba" for c in ciclos[n - n_prueba :]})
    return feats["ciclo"].map(lambda c: bloque[int(c)])


def _split_cronologico(n: int, cfg: dict[str, Any]) -> np.ndarray:
    p_tr, p_val, _ = cfg["validacion"]["split"]
    corte_tr, corte_val = int(n * p_tr), int(n * (p_tr + p_val))
    return np.array(
        ["entrenamiento"] * corte_tr
        + ["validacion"] * (corte_val - corte_tr)
        + ["prueba"] * (n - corte_val),
        dtype=object,
    )


def _versiones_entorno() -> dict[str, str]:
    import platform

    versiones = {"python": platform.python_version()}
    for paquete in (
        "xgboost",
        "scikit-learn",
        "imbalanced-learn",
        "numpy",
        "pandas",
        "onnx",
        "onnxmltools",
        "onnxruntime",
    ):
        try:
            versiones[paquete] = importlib_metadata.version(paquete)
        except importlib_metadata.PackageNotFoundError:
            versiones[paquete] = "no instalado"
    return versiones


def entrenar_modelo_individual(
    *,
    id_animal: str,
    especie: str,
    lecturas_calibracion: Iterable[dict] | pd.DataFrame,
    feat_real_etiquetado: Iterable[dict] | None = None,
    ciclo_reentrenamiento: int = 0,
    n_verificadas_rf72: int = 0,
    id_proceso_rf71: str | None = None,
    version_referencia: int | None = None,
    entrenar_contrastes: bool = False,
    tipo_modelo: str | None = None,
    dias_minimos: float | None = None,
    cfg: dict[str, Any] = CONFIG,
) -> dict[str, Any]:
    """Entrena el modelo individual de un animal.

    ``lecturas_calibracion``: lecturas del backend (las del periodo de calibración).
    ``feat_real_etiquetado``: retroalimentaciones de RF-72 del animal (ver
    ``etiqueta_de_retroalimentacion``).
    Devuelve ``{"metadatos_rf69": {...}, "onnx": bytes, "linea_base": {...}}``.
    """
    t_inicio = time.perf_counter()
    for paquete in ("xgboost", "sklearn", "imblearn", "onnxmltools", "scipy"):
        importlib.import_module(paquete)  # ImportError antes de calibrar si falta el extra
    cfg = copy.deepcopy(cfg)
    retros = list(feat_real_etiquetado or [])
    clave_especie = resolver_especie(especie, tipo_modelo)
    tipo = tipo_modelo or et02.grupo_de_especie(clave_especie)
    semilla = int(cfg["semilla"])
    semilla_generador = semilla + 1000 + int(ciclo_reentrenamiento)

    # 1. Calibración
    df_real = lecturas_a_dataframe(lecturas_calibracion)
    if df_real.empty:
        raise ErrorEntrenamiento("CALIBRACION_INCOMPLETA", "El animal no tiene lecturas.")
    lb = calibrar_animal(df_real, clave_especie, id_animal, cfg, dias_minimos)

    # 2. Línea temporal sintética y ventanas
    sint, control_generador = generar_linea_temporal_sintetica(lb, cfg, semilla_generador)
    feats_sint = ventanas_con_features(sint, lb, cfg)
    feats_sint = feats_sint[feats_sint["etiqueta"] >= 0].reset_index(drop=True)
    feats_sint["bloque"] = _split_sintetico(feats_sint, cfg)
    feats_sint["origen"] = "SINTETICA"

    # 3. Ventanas reales etiquetadas (RF-72) y regla de retiro
    feats_real = ventanas_con_features(df_real, lb, cfg)
    feats_real = feats_real.drop(columns=["etiqueta"], errors="ignore")
    feats_real["etiqueta"] = etiquetar_ventanas_reales(feats_real, retros).to_numpy()
    etiquetadas = (
        feats_real[feats_real["etiqueta"] >= 0].sort_values("ventana_inicio").reset_index(drop=True)
    )
    etiquetadas["bloque"] = _split_cronologico(len(etiquetadas), cfg)
    etiquetadas["origen"] = "REAL"
    etiquetadas["ciclo"] = np.nan
    sin_etiqueta = feats_real[feats_real["etiqueta"] < 0]
    retiro = aplicar_regla_retiro(
        etiquetadas.loc[etiquetadas["bloque"] == "entrenamiento", "etiqueta"].to_numpy(dtype=int),
        len(feats_real),
        cfg,
    )
    fuente = retiro["fuente_datos_modelo"]
    pesos_sint = retiro["pesos_sinteticos"]
    partes = [etiquetadas] if retiro["retiro_total"] else [feats_sint, etiquetadas]
    datos = pd.concat(partes, ignore_index=True)
    datos["peso"] = np.where(
        datos["origen"] == "SINTETICA",
        datos["etiqueta"].map(lambda c: pesos_sint[int(c)]),
        1.0,
    )
    datos = datos[datos["peso"] > 0]

    def juego(nombre: str) -> pd.DataFrame:
        return datos[datos["bloque"] == nombre]

    entrena, valida, prueba = juego("entrenamiento"), juego("validacion"), juego("prueba")
    if valida.empty or prueba.empty:
        raise ErrorEntrenamiento("DATASET_INSUFICIENTE", "Sin ventanas de validación o de prueba.")
    X_tr = entrena[FEATURES].to_numpy(dtype=float)
    y_tr = entrena["etiqueta"].to_numpy(dtype=int)
    w_tr = entrena["peso"].to_numpy(dtype=float)
    X_val = valida[FEATURES].to_numpy(dtype=float)
    y_val = valida["etiqueta"].to_numpy(dtype=int)
    w_val = valida["peso"].to_numpy(dtype=float)
    hash_dataset = hashlib.sha256(
        X_tr.tobytes() + y_tr.tobytes() + w_tr.tobytes() + X_val.tobytes() + y_val.tobytes()
    ).hexdigest()
    proporcion_sintetica = float(
        w_tr[(entrena["origen"] == "SINTETICA").to_numpy()].sum() / w_tr.sum()
    )

    # 4. Entrenamiento
    modelo, parametros, grilla, balanceo = entrenar_xgb_con_grilla(
        X_tr, y_tr, w_tr, X_val, y_val, w_val, cfg
    )

    # 5. Métricas y criterios
    criterios = evaluar_criterios_aceptacion(
        modelo=modelo,
        parametros=parametros,
        lb=lb,
        fuente_datos=fuente,
        X_tr=X_tr,
        y_tr=y_tr,
        prueba=prueba,
        feats_sinteticas=feats_sint,
        reales_sin_etiqueta=sin_etiqueta,
        cfg=cfg,
        semilla=semilla,
    )
    alertas = list(criterios["alertas"])
    bloqueantes = list(criterios["bloqueantes"])
    sensibilidad = None
    if fuente != "REAL":
        sensibilidad = _sensibilidad(
            modelo, lb, criterios["metricas"]["f1_score_global"], cfg, semilla
        )
        if not sensibilidad["aprobado"]:
            alertas.append(
                f"SENSIBILIDAD: caída de F1 {sensibilidad['caida']} > "
                f"{cfg['aceptacion']['caida_f1_sensibilidad_max']} con episodios más débiles."
            )
    if entrenar_contrastes:
        alertas.append("CONTRASTES_NO_EVALUADOS: los contrastes RF/MLP no corren en el motor.")

    # 6. Exportación
    onnx_bytes = exportar_onnx(modelo, prueba[FEATURES].to_numpy(dtype=float)[:256])
    tam_mb = len(onnx_bytes) / 1024**2
    if tam_mb > cfg["aceptacion"]["artefacto_mb_max"]:
        bloqueantes.append(
            f"ARTEFACTO_EXCEDE_TAMANO: {tam_mb:.1f} MB > {cfg['aceptacion']['artefacto_mb_max']}."
        )

    especie_def = ESPECIES[clave_especie]
    metadatos = {
        "tipo_modelo": tipo,
        "paradigma": cfg["paradigma"],
        "componente": None,
        "metricas_validacion": criterios["metricas"],
        "dataset_entrenamiento_hash": hash_dataset,
        "fecha_entrenamiento": datetime.now(UTC).isoformat(),
        "version_referencia": version_referencia,
        "compatibilidad_variables": list(VARIABLES),
        "id_proceso_rf71": id_proceso_rf71,
        "formato": "ONNX",
        "id_animal": id_animal,
        "id_especie": clave_especie,
        "especie_recibida": especie,
        "grupo_manejo": especie_def["grupo_manejo"],
        "version_modelo": (
            f"{tipo}-{id_animal}-{cfg['version_pipeline']}-c{int(ciclo_reentrenamiento)}"
        ),
        "version_pipeline": cfg["version_pipeline"],
        "version_entrenamiento": VERSION_ENTRENAMIENTO,
        "modo_generacion": "REENTRENAMIENTO",
        "ciclo_reentrenamiento": int(ciclo_reentrenamiento),
        "n_verificadas_rf72": int(n_verificadas_rf72),
        "estado_registro_rf69": "NO_REGISTRABLE" if bloqueantes else "REGISTRABLE",
        "motivo_no_registrable": bloqueantes,
        "apto_despliegue_sombra": not bloqueantes,
        "semilla": semilla,
        "semilla_generador": semilla_generador,
        "parametros_entrenamiento": {
            "algoritmo": "XGBOOST",
            **{k: v for k, v in parametros.items() if k not in ("n_jobs", "random_state")},
        },
        "busqueda_grilla": grilla,
        "balanceo": balanceo,
        "linea_base_superada": criterios["linea_base_superada"],
        "brecha_entrenamiento_prueba": criterios["brecha_entrenamiento_prueba"],
        "estabilidad_walk_forward": criterios["estabilidad_walk_forward"],
        "control_calidad_generador": {
            "rangos": control_generador,
            "real_vs_sintetico": criterios["real_vs_sintetico"],
            "sensibilidad": sensibilidad,
        },
        "dataset": {
            "ventanas_sinteticas": int((datos["origen"] == "SINTETICA").sum()),
            "ventanas_reales": int(len(feats_real)),
            "ventanas_reales_etiquetadas": int(len(etiquetadas)),
            "ventanas_por_juego": {
                b: int((datos["bloque"] == b).sum())
                for b in ("entrenamiento", "validacion", "prueba")
            },
            "dias_lecturas": round(_rango_dias(df_real), 2),
        },
        "fuente_datos": fuente,
        "proporcion_sintetica": round(proporcion_sintetica, 4),
        "features": list(FEATURES),
        "clases": list(et02.CATALOGO_CLASES),
        "mapeo_clases_rf64": {NOMBRES_CLASES[i]: ids for i, ids in IDX_A_RF64.items()},
        "rangos_especie": {
            k: especie_def[k]
            for k in (
                "rango_sano",
                "rango_plausible",
                "rango_sano_fuente",
                "rango_plausible_fuente",
            )
        },
        "linea_base": lb.a_dict(),
        "hash_codigo_pipeline": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "versiones_entorno": _versiones_entorno(),
        "alertas": alertas,
        "hash_artefacto_modelo": et02.sha256_bytes(onnx_bytes),
        "regla_retiro": {k: v for k, v in retiro.items() if k != "pesos_sinteticos"}
        | {
            "rho_objetivo": cfg["retiro"]["rho_inicial"],
            "proporcion_sintetica": round(proporcion_sintetica, 4),
        },
        "duracion_s": round(time.perf_counter() - t_inicio, 1),
    }
    return {"metadatos_rf69": _py(metadatos), "onnx": onnx_bytes, "linea_base": _py(lb.a_dict())}
