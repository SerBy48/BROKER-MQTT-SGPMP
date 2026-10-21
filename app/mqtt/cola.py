# | Fecha      | Nombre   | Que se hizo |
# |------------|----------|-------------|
# | 2026-10-20 | Arekkazu | BRK-11 RFC-016: la cola ordena por la clave compuesta de prioridad |
"""Cola de prioridad de los mensajes del nodo bajo saturación (BRK-09/BRK-11, RF-53 E15, RFC-016).

El bucle de mensajes MQTT solo parsea, clasifica (services/prioridad) y encola;
`workers` tareas procesan en paralelo en orden de prioridad. Así un pico de
telemetría no retrasa el ACK de un comando ni el heartbeat que mantiene vivo a un
nodo, y cuando el procesamiento no da abasto se pierde primero lo menos importante:

- CONTROL: siempre entra.
- CRITICO: entra mientras haya menos de 2 × capacidad mensajes en espera.
- NORMAL: entra mientras haya menos de capacidad.

Una lectura rechazada recibe `RETENER` en su topic `resultado`: el Edge la conserva
en su buffer y la reintenta (RF-54), así que rechazar no es perderla. Dentro de cada
clase el orden lo da la clave compuesta de services/prioridad (severidad,
criticidad del área, tipo de dato, origen, antigüedad).

ponytail: el rechazo es por clase al llegar, no desaloja lo que ya está en cola. Un
dato NORMAL encolado sigue ahí aunque llegue otro de mejor clave con la cola llena;
si hace falta, pasar a un heap de doble extremo y desalojar el peor con RETENER.

ponytail: sin orden por serial entre workers. Un heartbeat y un aviso de
desconexión del mismo Edge pueden procesarse cruzados; lo peor es un comando que
espera su ACK y termina en NO_CONF. Si molesta, agrupar los workers por serial.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections import Counter

from app.config import get_settings
from app.mqtt import dynsec, handlers
from app.services import ingest
from app.services.prioridad import Prioridad, clasificar

logger = logging.getLogger(__name__)

_LOG_CADA = 100  # bajo saturación, un warning por cada 100 rechazos y no uno por mensaje


class ColaIngesta:
    def __init__(self) -> None:
        self._cola: asyncio.PriorityQueue | None = None
        self._workers: list[asyncio.Task] = []
        self._secuencia = itertools.count()  # desempate: nunca se comparan los dicts
        self.rechazados: Counter[Prioridad] = Counter()

    def _limite(self, prioridad: Prioridad) -> float:
        capacidad = get_settings().mqtt_cola_capacidad
        return {
            Prioridad.CONTROL: float("inf"),
            Prioridad.CRITICO: 2 * capacidad,
            Prioridad.NORMAL: capacidad,
        }[prioridad]

    def iniciar(self) -> None:
        if self._cola is not None:
            return
        self._cola = asyncio.PriorityQueue()
        self._workers = [
            asyncio.create_task(self._trabajar()) for _ in range(get_settings().mqtt_cola_workers)
        ]

    async def detener(self) -> None:
        for tarea in self._workers:
            tarea.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers, self._cola = [], None

    @property
    def en_espera(self) -> int:
        return self._cola.qsize() if self._cola is not None else 0

    async def recibir(self, topic: str, payload: bytes) -> None:
        """Entrada desde el bucle MQTT."""
        if topic == dynsec.TOPIC_RESPUESTA:
            dynsec.resolver_respuesta(payload)  # lo espera otra tarea: nunca a la cola
            return
        mensaje = handlers.parsear_mensaje(topic, payload)
        if mensaje is None:
            return
        iot, data = mensaje
        if self._cola is None:  # sin workers (p. ej. pruebas): en el acto, como antes
            await handlers.procesar(iot, data, topic)
            return
        prioridad, clave = await clasificar(iot.tipo, data, iot.serial)
        if self._cola.qsize() >= self._limite(prioridad):
            await self._rechazar(prioridad, iot, data, topic)
            return
        self._cola.put_nowait((clave, next(self._secuencia), topic, iot, data))

    async def _rechazar(self, prioridad: Prioridad, iot, data: dict, topic: str) -> None:
        self.rechazados[prioridad] += 1
        total = self.rechazados[prioridad]
        if total == 1 or total % _LOG_CADA == 0:
            logger.warning(
                "Gateway saturado (%d en espera): %d mensajes %s rechazados (último: %s)",
                self.en_espera, total, prioridad.name, topic,
            )
        if iot.tipo == get_settings().mqtt_topic_telemetry:
            await ingest.rechazar_por_saturacion(iot.serial, data)

    async def _trabajar(self) -> None:
        assert self._cola is not None
        cola = self._cola
        while True:
            _, _, topic, iot, data = await cola.get()
            try:
                await handlers.procesar(iot, data, topic)
            finally:
                cola.task_done()


cola_ingesta = ColaIngesta()
