# | Fecha      | Nombre   | Que se hizo |
# |------------|----------|-------------|
# | 2026-11-02 | Arekkazu | AJF-14 RF-71: hilo reentrenador: toma procesos, entrena en un subproceso y reporta fases |
"""Reentrenador del motor de IA (AJF-14, M-3).

Un hilo que, cada ``MOTOR_IA_REENTRENAMIENTO_S`` segundos, pide al backend un proceso pendiente y lo
corre de punta a punta, uno a la vez:

  1 ACTIVACION     el backend ya lo pasó a EN_CURSO al tomarlo
  2 DATASET        lecturas del animal (``dias_calibracion``) y retroalimentaciones vigentes
  3-6              ``entrenamiento.entrenar`` en un SUBPROCESO con ``os.nice(10)``: una
                   grilla XGBoost no frena la inferencia. El tope de CPU (70 %, RF-71 R9) lo
                   pone el contenedor (``cpus``), no el código.
  6 ENTREGA        ``POST /prediccion/modelos`` con ``id_proceso_rf71`` = id del proceso
  7 CIERRE         ``PATCH`` COMPLETADO con la versión, o FALLIDO con el código

Latido: un ``PATCH`` al cambiar de fase y cada ``latido_s`` (30 s) con CPU y RAM del subproceso;
si el contenedor se reinicia, el backend lo da por INTERRUMPIDO a los 15 min sin latido.
"""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing as mp
import os
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from motor_ia import entrenamiento

logger = logging.getLogger(__name__)

FASE_DATASET, FASE_ENTREGA = 2, 6


@dataclass
class _Recursos:
    """CPU (% de un núcleo) y RAM (MB) de un proceso, leídos de /proc (Linux, el contenedor)."""

    pid: int
    _cpu_prev: tuple[float, float] | None = None

    def leer(self) -> tuple[float | None, int | None]:
        try:
            with open(f"/proc/{self.pid}/stat", encoding="ascii") as f:
                campos = f.read().rsplit(")", 1)[1].split()
            ticks = (int(campos[11]) + int(campos[12])) / os.sysconf("SC_CLK_TCK")
            ahora = time.monotonic()
            cpu = None
            if self._cpu_prev is not None:
                t0, c0 = self._cpu_prev
                cpu = round(min(100.0, max(0.0, (ticks - c0) / max(ahora - t0, 1e-6) * 100)), 2)
            self._cpu_prev = (ahora, ticks)
            with open(f"/proc/{self.pid}/status", encoding="ascii") as f:
                rss = next((int(l.split()[1]) for l in f if l.startswith("VmRSS:")), None)
            return cpu, (rss // 1024 if rss is not None else None)
        except (OSError, ValueError, IndexError):
            return None, None


def _entrenar_en_hijo(cola: Any, kwargs: dict) -> None:
    """Cuerpo del subproceso: reporta fases por la cola y deja el resultado al final."""
    try:
        os.nice(10)
    except OSError:
        pass
    try:
        r = entrenamiento.entrenar(**kwargs, al_avanzar=lambda fase: cola.put(("fase", fase)))
        cola.put(("ok", {"metadatos": r.metadatos, "onnx": r.onnx, "motivos": r.motivos}))
    except entrenamiento.EntrenamientoNoDisponible as exc:
        cola.put(("error", ("ENTRENAMIENTO_NO_DISPONIBLE", str(exc))))
    except entrenamiento.FalloEntrenamiento as exc:
        cola.put(("error", (exc.codigo, str(exc))))
    except Exception as exc:  # noqa: BLE001
        cola.put(("error", ("ERROR_ENTRENAMIENTO", f"{type(exc).__name__}: {exc}")))


class Reentrenador:
    def __init__(
        self,
        cliente: Any,
        registro: Any,
        *,
        dias_calibracion: float = 14.0,
        latido_s: float = 30.0,
        lanzar: Callable[[Any, dict], Any] | None = None,
    ) -> None:
        """``cliente``: ``BackendCatalogoClient``; ``registro``: ``BackendRegistroModelosClient``.
        ``lanzar(cola, kwargs)`` arranca el entrenamiento (por defecto, un subproceso)."""
        self._cliente = cliente
        self._registro = registro
        self._dias = dias_calibracion
        self._latido_s = latido_s
        self._lanzar = lanzar or self._subproceso
        self.ocupado = False

    @staticmethod
    def _subproceso(cola: Any, kwargs: dict) -> mp.Process:
        p = mp.get_context("spawn").Process(
            target=_entrenar_en_hijo, args=(cola, kwargs), name="reentrenamiento", daemon=True
        )
        p.start()
        return p

    def ciclo(self) -> bool:
        """Toma y corre un proceso. False si no había ninguno pendiente."""
        proceso = self._cliente.tomar_reentrenamiento()
        if proceso is None:
            return False
        self.ocupado = True
        id_proceso = proceso["id_proceso_reentrenamiento"]
        try:
            self._correr(proceso)
        except Exception as exc:  # noqa: BLE001 — el proceso no queda EN_CURSO por un fallo del motor
            logger.exception("Reentrenamiento %s: fallo del motor", id_proceso)
            self._cerrar_fallido(id_proceso, "ERROR_MOTOR", [f"{type(exc).__name__}: {exc}"[:300]])
        finally:
            self.ocupado = False
        return True

    def _correr(self, proceso: dict) -> None:
        id_proceso = proceso["id_proceso_reentrenamiento"]
        id_activo = proceso["id_activo_biologico"]
        self._cliente.reportar_reentrenamiento(id_proceso, fase=FASE_DATASET, progreso=10)
        lecturas = self._cliente.lecturas(id_activo, self._dias * 24)
        retros = self._cliente.retroalimentaciones(id_activo)
        motivos: list[str] = []
        if self._dias < 14:
            motivos.append(f"Calibración con {self._dias:g} días de lecturas (ET-02 pide 14).")
        if not lecturas:
            self._cerrar_fallido(
                id_proceso,
                "CALIBRACION_INCOMPLETA",
                motivos + ["El animal no tiene lecturas en el periodo."],
            )
            return

        cola: Any = (
            mp.get_context("spawn").Queue() if self._lanzar == self._subproceso else queue.Queue()
        )
        hijo = self._lanzar(
            cola,
            {
                "id_animal": proceso.get("identificador_activo") or str(id_activo),
                # Especie del animal (perfil de ET-02); sin ella, el tipo de modelo.
                "especie": proceso.get("nombre_especie") or proceso["tipo_modelo"],
                "lecturas": lecturas,
                "retroalimentaciones": retros,
                "id_proceso": id_proceso,
                "version_referencia": proceso.get("id_version_modelo_referencia"),
            },
        )
        recursos = _Recursos(hijo.pid) if getattr(hijo, "pid", None) else None
        fase, ultimo_latido = FASE_DATASET, time.monotonic()
        resultado: tuple[str, Any] | None = None
        while resultado is None:
            try:
                tipo, dato = cola.get(timeout=1.0)
            except queue.Empty:
                tipo, dato = None, None
                if hijo is not None and hasattr(hijo, "is_alive") and not hijo.is_alive():
                    resultado = (
                        "error",
                        ("ERROR_ENTRENAMIENTO", "El subproceso terminó sin resultado."),
                    )
            if tipo == "fase" and dato > fase:
                fase = dato
                cpu, ram = recursos.leer() if recursos else (None, None)
                self._cliente.reportar_reentrenamiento(
                    id_proceso, fase=fase, progreso=min(95, fase * 14), cpu=cpu, ram_mb=ram
                )
                ultimo_latido = time.monotonic()
            elif tipo in ("ok", "error"):
                resultado = (tipo, dato)
            if resultado is None and time.monotonic() - ultimo_latido >= self._latido_s:
                cpu, ram = recursos.leer() if recursos else (None, None)
                self._cliente.reportar_reentrenamiento(id_proceso, cpu=cpu, ram_mb=ram)
                ultimo_latido = time.monotonic()
        if hijo is not None and hasattr(hijo, "join"):
            hijo.join(timeout=5)

        tipo, dato = resultado
        if tipo == "error":
            codigo, mensaje = dato
            self._cerrar_fallido(id_proceso, codigo, motivos + ([mensaje[:300]] if mensaje else []))
            return
        r = entrenamiento.ResultadoEntrenamiento(
            metadatos=dato["metadatos"], onnx=dato["onnx"], motivos=dato["motivos"]
        )
        if not r.registrable:
            self._cerrar_fallido(id_proceso, "NO_REGISTRABLE", motivos + r.motivos)
            return
        self._cliente.reportar_reentrenamiento(id_proceso, fase=FASE_ENTREGA, progreso=95)
        version = self._registrar(proceso, r)
        self._cliente.reportar_reentrenamiento(
            id_proceso,
            estado="COMPLETADO",
            id_version_modelo=version.get("id_version_modelo"),
            metricas={
                **(r.metadatos.get("metricas_validacion") or {}),
                "estado_resultado": version.get("estado_version"),
            },
        )
        logger.info(
            "Reentrenamiento %s: versión %s registrada (%s)",
            id_proceso,
            version.get("nombre_version"),
            version.get("estado_version"),
        )

    def _registrar(self, proceso: dict, r: entrenamiento.ResultadoEntrenamiento) -> dict:
        m = r.metadatos
        formulario = {
            "tipo_modelo": proceso["tipo_modelo"],
            "hash_artefacto_sha256": hashlib.sha256(r.onnx).hexdigest(),
            "dataset_entrenamiento_hash": str(m.get("dataset_entrenamiento_hash") or ""),
            "metricas_validacion": json.dumps(m.get("metricas_validacion") or {}),
            "fecha_entrenamiento": str(
                m.get("fecha_entrenamiento") or datetime.now(UTC).isoformat()
            ),
            "compatibilidad_variables": json.dumps(m.get("compatibilidad_variables") or []),
            "id_proceso_rf71": proceso["id_proceso_reentrenamiento"],
            "id_activo_biologico": str(proceso["id_activo_biologico"]),
            "fuente_datos": str(m.get("fuente_datos") or "SINTETICA"),
        }
        if proceso.get("id_version_modelo_referencia") is not None:
            formulario["version_referencia"] = str(proceso["id_version_modelo_referencia"])
        nombre = f"{m.get('version_modelo') or proceso['id_proceso_reentrenamiento']}.onnx"
        return self._registro.registrar(formulario, r.onnx, nombre)

    def _cerrar_fallido(self, id_proceso: str, codigo: str, motivos: list[str]) -> None:
        try:
            self._cliente.reportar_reentrenamiento(
                id_proceso, estado="FALLIDO", codigo_error=codigo[:60], motivos=motivos or None
            )
        except Exception as exc:  # noqa: BLE001 — el vigilante del backend lo cerrará por latido
            logger.warning("Reentrenamiento %s: no se pudo reportar el fallo (%r)", id_proceso, exc)


def reentrenar_siempre(reentrenador: Reentrenador, cada_s: int) -> threading.Thread:
    """Hilo aparte del de catálogo: un entrenamiento no frena la sincronización ni la inferencia."""

    def bucle() -> None:
        while True:
            try:
                while reentrenador.ciclo():
                    pass  # mientras haya pendientes, uno tras otro
            except Exception as exc:  # noqa: BLE001 — backend caído: se reintenta en el siguiente ciclo
                logger.warning("Reentrenador: no se pudo consultar el backend (%r)", exc)
            time.sleep(cada_s)

    hilo = threading.Thread(target=bucle, name="reentrenador", daemon=True)
    hilo.start()
    return hilo
