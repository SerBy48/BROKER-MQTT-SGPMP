# | Fecha      | Nombre   | Que se hizo                                                     |
# |------------|----------|-----------------------------------------------------------------|
# | 2026-11-02 | Arekkazu | RF-66/RF-69: el motor arma su catálogo desde el backend (B2/M3)  |
"""El motor arma su catálogo desde el backend: sin ``catalogo.json`` ni artefactos copiados a mano.

Cada ``MOTOR_IA_SINCRONIZACION_S`` pide al backend los animales individuales que debe atender
(``GET /prediccion/motor/catalogo``). A un animal sin versión el backend le registra una copia en
SOMBRA de la vigente de su tipo (RFC-010: un modelo por animal). El ``.onnx`` se descarga una vez
por versión y se verifica contra el SHA-256 registrado en RF-69 antes de usarlo.

Línea base (ET-02, Tabla 6.1): mientras el animal no junte ``MOTOR_IA_CALIBRACION_HORAS`` de
lecturas aptas, se usa la de referencia de su tipo de modelo (``plantillas/``, la del animal con
que se entrenó) y el resultado sale con ``contexto_incompleto``. Con suficientes lecturas, la
media y la desviación de cada variable pasan a ser su línea base propia, vigente 6 meses.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from motor_ia import et02
from motor_ia.backend import BackendCatalogoClient
from motor_ia.individual import ModeloAnimal

logger = logging.getLogger(__name__)

PLANTILLAS = Path(__file__).parent / "plantillas"
#: Sin esto, cada animal sin calibrar pediría su historial en cada sincronización.
REINTENTO_CALIBRACION = timedelta(minutes=10)
#: ≈ 1 h a 5 min por lectura: con menos, la media de esa variable no dice nada.
MIN_LECTURAS_VARIABLE = 12


def cargar_plantillas() -> dict[str, et02.LineaBase]:
    """``{tipo_modelo: línea base de referencia}``."""
    return {
        p.stem: et02.LineaBase(**json.loads(p.read_text(encoding="utf-8")))
        for p in PLANTILLAS.glob("*.json")
    }


def calcular_linea_base(
    clave: str, plantilla: et02.LineaBase, lecturas: list[dict], horas: float
) -> et02.LineaBase | None:
    """Línea base propia con las lecturas aptas, o None si todavía no cubren ``horas``.

    Una variable sin lecturas suficientes (el animal no tiene ese sensor) conserva la de referencia.
    """
    aptas = [lec for lec in lecturas if lec.get("apto_para_ia")]
    if not aptas:
        return None
    momentos = [datetime.fromisoformat(lec["timestamp"]) for lec in aptas]
    if max(momentos) - min(momentos) < timedelta(hours=horas) * 0.9:
        return None
    mu, sigma, n = dict(plantilla.mu), dict(plantilla.sigma), 0
    for var in plantilla.mu:
        valores = [float(lec["valor"]) for lec in aptas if lec["variable"] == var]
        if len(valores) < MIN_LECTURAS_VARIABLE:
            continue
        mu[var] = statistics.fmean(valores)
        # ponytail: con pocas horas la desviación no recoge la variación del día y las z se
        # inflan; el piso es la mitad de la de referencia. Con los 7 días de ET-02 no pesa.
        sigma[var] = max(statistics.pstdev(valores), plantilla.sigma[var] / 2)
        n += len(valores)
    if n == 0:
        return None
    return et02.LineaBase(
        id_animal=clave,
        especie=plantilla.especie,
        mu=mu,
        sigma=sigma,
        perfil_circadiano={},  # ET-02 no lo usa en las features
        inicio=min(momentos).isoformat(),
        fin=max(momentos).isoformat(),
        n_lecturas=n,
        fase_productiva=None,
        estado="COMPLETADA",
    )


class SincronizadorCatalogo:
    def __init__(
        self,
        cliente: BackendCatalogoClient,
        horas_calibracion: float,
        plantillas: dict[str, et02.LineaBase] | None = None,
    ) -> None:
        self._cliente = cliente
        self._horas = horas_calibracion
        self._plantillas = plantillas if plantillas is not None else cargar_plantillas()
        self._artefactos: dict[int, bytes] = {}
        self._propias: dict[int, et02.LineaBase] = {}
        self._de_referencia: dict[int, et02.LineaBase] = {}
        self._intentos: dict[int, datetime] = {}
        #: Se reemplaza entero en cada sincronización: quien lo lee nunca ve uno a medias.
        self.modelos: dict[int, ModeloAnimal] = {}
        self.ultima: datetime | None = None

    def sincronizar(self, ahora: datetime | None = None) -> None:
        """Si el backend no responde, lanza y se conserva el catálogo anterior."""
        ahora = ahora or datetime.now(UTC)
        nuevos: dict[int, ModeloAnimal] = {}
        for animal in self._cliente.catalogo():
            id_activo = animal["id_activo_biologico"]
            try:
                nuevos[id_activo] = self._modelo(animal, ahora)
            except Exception as exc:  # noqa: BLE001 — un animal con problemas no tumba a los demás
                logger.warning("Catálogo: activo %s sin modelo nuevo (%r)", id_activo, exc)
                if id_activo in self.modelos:
                    nuevos[id_activo] = self.modelos[id_activo]
        altas = sorted(nuevos.keys() - self.modelos.keys())
        bajas = sorted(self.modelos.keys() - nuevos.keys())
        if altas or bajas:
            logger.info("Catálogo: %d animal(es); altas %s, bajas %s", len(nuevos), altas, bajas)
        self.modelos, self.ultima = nuevos, ahora

    def _modelo(self, animal: dict, ahora: datetime) -> ModeloAnimal:
        id_activo, tipo = animal["id_activo_biologico"], animal["tipo_modelo"]
        if tipo not in self._plantillas:
            raise ValueError(f"sin línea base de referencia para {tipo}")
        version = animal["version"] or self._cliente.asignar_version(id_activo)
        onnx = self._artefacto(version)
        clave = f"ACTIVO-{id_activo}"
        linea_base = self._linea_base(id_activo, clave, self._plantillas[tipo], ahora)
        calibrando = linea_base is None
        if calibrando:
            linea_base = self._de_referencia.setdefault(
                id_activo,
                dataclasses.replace(self._plantillas[tipo], id_animal=clave, fin=ahora.isoformat()),
            )
        previo = self.modelos.get(id_activo)
        if (
            previo
            and previo.id_version_modelo == version["id_version_modelo"]
            and previo.linea_base is linea_base
        ):
            return previo  # conserva la sesión ONNX ya cargada
        metadatos = {
            "tipo_modelo": tipo,
            "version_modelo": version["nombre_version"],
            "fuente_datos": version["fuente_datos"],
            "proporcion_sintetica": 1.0 if version["fuente_datos"] == "SINTETICA" else 0.0,
            "hash_artefacto_modelo": version["hash_artefacto_sha256"],
            "id_animal": clave,
        }
        return ModeloAnimal(
            id_activo,
            version["id_version_modelo"],
            linea_base,
            et02.ArtefactoModelo(onnx, metadatos),
            calibrando,
        )

    def _artefacto(self, version: dict) -> bytes:
        id_version = version["id_version_modelo"]
        if id_version not in self._artefactos:
            onnx = self._cliente.artefacto(id_version)
            if et02.sha256_bytes(onnx) != version["hash_artefacto_sha256"]:
                raise ValueError(f"el .onnx de la versión {id_version} no coincide con su SHA-256")
            self._artefactos[id_version] = onnx
        return self._artefactos[id_version]

    def _linea_base(
        self, id_activo: int, clave: str, plantilla: et02.LineaBase, ahora: datetime
    ) -> et02.LineaBase | None:
        propia = self._propias.get(id_activo)
        if propia and et02.estado_calibracion(propia, ahora) == "COMPLETADA":
            return propia
        intento = self._intentos.get(id_activo)
        if intento and ahora - intento < REINTENTO_CALIBRACION:
            return None
        self._intentos[id_activo] = ahora
        try:
            lecturas = self._cliente.lecturas(id_activo, self._horas)
        except httpx.HTTPError as exc:
            logger.warning("Calibración: activo %s sin lecturas del backend (%r)", id_activo, exc)
            return None
        propia = calcular_linea_base(clave, plantilla, lecturas, self._horas)
        if propia:
            self._propias[id_activo] = propia
            logger.info(
                "Calibración: activo %s con línea base propia (%d lecturas, %s a %s)",
                id_activo,
                propia.n_lecturas,
                propia.inicio,
                propia.fin,
            )
        return propia
