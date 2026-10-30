# | Fecha      | Nombre   | Que se hizo                                                     |
# |------------|----------|-----------------------------------------------------------------|
# | 2026-10-30 | Arekkazu | RF-66: catálogo de modelos e inferencia individual (M2)          |
"""Inferencia individual (ET-02) por activo y su conversión a la Salida de RF-66.

Catálogo local de modelos (``MOTOR_IA_DIR_MODELOS/catalogo.json``) mientras no existan
RFC-010 (versión activa por animal) ni la descarga desde RF-69 (B2/M3)::

    {"modelos": [{"id_activo_biologico": 885, "id_version_modelo": 12,
                  "onnx": "BOV-001.onnx", "linea_base": "BOV-001_linea_base.json",
                  "metadatos": "BOV-001_metadatos_rf69.json"}]}

Los tres archivos son los que entrega ET-02 por animal. ``id_version_modelo`` es la versión
registrada en RF-69 de ese ambiente. Si el hash del ``.onnx`` no coincide con
``hash_artefacto_modelo`` de los metadatos, el motor no arranca.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from motor_ia import et02

#: Para que el id del resultado sea el mismo si el backend reenvía el lote (B1 lo deduplica).
_ESPACIO_IDS = uuid.UUID("6f1c2a9e-3b7d-4e55-9a0c-5d8e2f4b7a31")


class CatalogoInvalidoError(RuntimeError):
    """El catálogo de modelos no se puede usar: el motor no arranca."""


@dataclass(frozen=True)
class ModeloAnimal:
    id_activo_biologico: int
    id_version_modelo: int
    linea_base: et02.LineaBase
    artefacto: et02.ArtefactoModelo


def cargar_catalogo(directorio: str) -> dict[int, ModeloAnimal]:
    """``{id_activo_biologico: ModeloAnimal}``. Directorio vacío o sin definir → sin modelos."""
    if not directorio:
        return {}
    base = Path(directorio)
    try:
        entradas = json.loads((base / "catalogo.json").read_text(encoding="utf-8"))["modelos"]
    except (OSError, ValueError, KeyError) as exc:
        raise CatalogoInvalidoError(f"No se pudo leer {base / 'catalogo.json'}: {exc}") from exc

    modelos: dict[int, ModeloAnimal] = {}
    for e in entradas:
        try:
            metadatos = json.loads((base / e["metadatos"]).read_text(encoding="utf-8"))
            linea_base = et02.LineaBase(
                **json.loads((base / e["linea_base"]).read_text(encoding="utf-8"))
            )
            artefacto = et02.ArtefactoModelo((base / e["onnx"]).read_bytes(), metadatos)
            id_activo = int(e["id_activo_biologico"])
            id_version = int(e["id_version_modelo"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CatalogoInvalidoError(f"Entrada inválida en el catálogo {e!r}: {exc}") from exc
        if not artefacto.verificar_hash():
            raise CatalogoInvalidoError(
                f"{e['onnx']}: el SHA-256 no coincide con hash_artefacto_modelo "
                f"de {e['metadatos']}."
            )
        if linea_base.id_animal != metadatos.get("id_animal"):
            raise CatalogoInvalidoError(
                f"{e['linea_base']} es de {linea_base.id_animal}, "
                f"el modelo es de {metadatos.get('id_animal')}."
            )
        if id_activo in modelos:
            raise CatalogoInvalidoError(f"El activo {id_activo} aparece dos veces en el catálogo.")
        modelos[id_activo] = ModeloAnimal(id_activo, id_version, linea_base, artefacto)
    return modelos


def lecturas_a_filas(lecturas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Formato largo del backend (una fila por lectura) → filas con las tres variables de ET-02.

    Cada fila lleva solo su variable; las otras quedan en ``None`` y ET-02 las agrupa en la
    grilla de 5 min por promedio.
    """
    filas = []
    for lectura in lecturas:
        if lectura.get("variable") not in et02.VARIABLES:
            continue
        fila = {"timestamp": lectura["timestamp"], **{v: None for v in et02.VARIABLES}}
        fila[lectura["variable"]] = lectura["valor"]
        fila["apto_para_ia"] = lectura.get("apto_para_ia")
        filas.append(fila)
    return filas


def inferir(modelo: ModeloAnimal, lecturas: list[dict[str, Any]]) -> tuple[dict, dict] | None:
    """(contrato ET-02, decisión de la Tabla 2.4) de la última ventana, o None si no hay inferencia
    utilizable (sin lecturas suficientes, sin línea base vigente, error de datos)."""
    paquete = {
        "id_animal": modelo.linea_base.id_animal,
        "id_especie": modelo.linea_base.especie,
        "lecturas": lecturas_a_filas(lecturas),
    }
    if not paquete["lecturas"]:
        return None
    contrato = et02.inferir_estado_individual(paquete, modelo.linea_base, modelo.artefacto)
    decision = et02.aplicar_reglas_rf66(contrato)
    if contrato.get("estado") != "OK" or not decision["utilizable"]:
        return None
    return contrato, decision


def a_resultado_rf66(
    modelo: ModeloAnimal, contrato: dict, decision: dict, id_paquete: int | None
) -> dict[str, Any]:
    """Cuerpo de ``POST /prediccion/inferencias`` del backend.

    SUPUESTO S4 de ET-02 (pendiente de Análisis): el modelo individual da probabilidades por
    patología, no por nivel de riesgo. Mientras Análisis no defina la proyección a ``nivel_riesgo``
    0–3, el nivel sale de la clase predicha (argmax, RF-66 Restricción 6a): NINGUNA → 0 Normal,
    clases de riesgo alto (hoy SEPTICEMIA) → 3 Alto, cualquier otra patología → 2 Moderado. El
    nivel 1 (Bajo) no se emite. ``probabilidad_riesgo`` agrupa las probabilidades igual.
    La Restricción 6b (umbral_riesgo_alto de RF-65) no se aplica: en sombra no alerta.
    """
    p = contrato["probabilidades"]
    clases_alto = et02.CONFIG["riesgo_alto"]["clases"]
    alto = sum(p[c] for c in clases_alto)
    normal = p["NINGUNA"]
    probabilidad_riesgo = {
        "0": normal,
        "2": round(max(0.0, 1.0 - normal - alto), 4),
        "3": round(alto, 4),
    }
    clase = contrato["clase_predicha"]
    nivel = 0 if clase == "NINGUNA" else 3 if clase in clases_alto else 2
    llave = f"{modelo.id_activo_biologico}|{contrato['ventana_fin']}|{modelo.id_version_modelo}"
    return {
        "id_resultado_inferencia": str(uuid.uuid5(_ESPACIO_IDS, llave)),
        "id_paquete_inferencia": id_paquete,
        "id_activo_biologico": modelo.id_activo_biologico,
        "tipo_modelo": contrato["tipo_modelo"],
        "id_version_modelo": modelo.id_version_modelo,
        "nivel_riesgo": nivel,
        "probabilidad_riesgo": probabilidad_riesgo,
        # Catálogo RF-64 (0–7); la clase fusionada 3+4 va sin código y la separa RF-66 (S3).
        "codigo_patologia_rf64": contrato["enfermedad_estimada"],
        "probabilidad_enfermedad": contrato["probabilidad_enfermedad"],
        "confianza_global": contrato["confianza"],
        "contexto_incompleto": decision["contexto_incompleto"],
        "modo_ejecucion": "SERVIDOR",
        "latencia_ms": contrato["latencia_ms"],
        "fecha_inicio_ventana": contrato["ventana_inicio"],
        "fecha_fin_ventana": contrato["ventana_fin"],
        "fecha_inferencia": contrato["timestamp_inferencia"],
        "modo_sombra": decision["modo_sombra"],
    }
