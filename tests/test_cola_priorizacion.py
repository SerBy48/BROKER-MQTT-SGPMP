"""BRK-09 (RF-53 E15): cola de prioridad bajo saturación con la definición de dato
crítico de docs/BRK-09_Cola_Priorizacion_Saturacion.md."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.config import get_settings
from app.db.repositories import registry
from app.mqtt import cola as cola_mod
from app.mqtt.cola import ColaIngesta
from app.services import prioridad
from app.services.prioridad import Prioridad, clasificar


@pytest.fixture(autouse=True)
def _entorno(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("MQTT_COLA_CAPACIDAD", "2")
    monkeypatch.setenv("MQTT_COLA_WORKERS", "1")
    get_settings.cache_clear()

    async def catalogo():
        return frozenset({"Oxígeno disuelto"})

    monkeypatch.setattr(prioridad, "_variables_criticas", catalogo)
    yield
    get_settings.cache_clear()


def _lectura(**extra) -> dict:
    return {"variable": "Temperatura del agua", "valor_crudo": 24.0, "unidad": "°C",
            "timestamp_captura": "2026-10-18T10:00:00+00:00", **extra}


@pytest.mark.parametrize("tipo, data, esperado", [
    ("status", {"tipo_mensaje": "ACK_UMBRAL"}, Prioridad.CONTROL),
    ("ota", {"id_despliegue_ota": 1}, Prioridad.CONTROL),
    ("heartbeat", {}, Prioridad.CRITICO),
    ("evento", {"clasificacion_rf55": "DESVIACION_SIMPLE"}, Prioridad.CRITICO),
    ("telemetry", _lectura(nivel_umbral="CRITICO"), Prioridad.CRITICO),
    ("telemetry", _lectura(variable="Oxígeno disuelto"), Prioridad.CRITICO),
    ("telemetry", _lectura(nivel_umbral="precaucion"), Prioridad.NORMAL),
    ("telemetry", _lectura(), Prioridad.NORMAL),
    ("vision", {"area_id": 2}, Prioridad.NORMAL),
], ids=["ack", "ota", "heartbeat", "evento-edge", "nivel-critico", "variable-critica",
        "nivel-precaucion", "lectura-normal", "vision"])
async def test_definicion_de_dato_critico(tipo, data, esperado) -> None:
    assert await clasificar(tipo, data) == esperado


class _Procesador:
    """Reemplaza handlers.procesar: registra el orden y puede quedar bloqueado."""

    def __init__(self) -> None:
        self.orden: list[str] = []
        self.liberar = asyncio.Event()

    async def __call__(self, iot, data, topic) -> None:
        if not self.orden:
            self.orden.append(topic)
            await self.liberar.wait()  # el primero tapa al único worker
            return
        self.orden.append(topic)


@pytest.fixture
async def cola(monkeypatch):
    procesador = _Procesador()
    rechazos: list[tuple[str, dict]] = []

    async def rechazar(serial, data):
        rechazos.append((serial, data))

    monkeypatch.setattr(cola_mod.handlers, "procesar", procesador)
    monkeypatch.setattr(cola_mod.ingest, "rechazar_por_saturacion", rechazar)
    c = ColaIngesta()
    c.iniciar()
    yield c, procesador, rechazos
    await c.detener()


async def _entregar(c: ColaIngesta, topic: str, data: dict) -> None:
    await c.recibir(topic, json.dumps(data).encode())


async def test_sale_primero_control_luego_critico_y_lo_mas_viejo(cola) -> None:
    c, procesador, _ = cola
    await _entregar(c, "sgpmp/E/telemetry", _lectura())  # tapa al worker
    await asyncio.sleep(0)
    await _entregar(c, "sgpmp/E/vision", {"area_id": 1})
    await _entregar(c, "sgpmp/E/heartbeat", {})
    await _entregar(c, "sgpmp/E/status", {"tipo_mensaje": "ACK_UMBRAL"})
    await _entregar(c, "sgpmp/VIEJO/evento", {"timestamp_evento": "2026-10-15T00:00:00Z"})

    procesador.liberar.set()
    await c._cola.join()

    assert procesador.orden == [
        "sgpmp/E/telemetry",
        "sgpmp/E/status",          # CONTROL
        "sgpmp/VIEJO/evento",      # CRITICO, más viejo que el heartbeat (capturado ahora)
        "sgpmp/E/heartbeat",       # CRITICO
        "sgpmp/E/vision",          # NORMAL
    ]


async def test_saturado_rechaza_normal_luego_critico_y_nunca_control(cola) -> None:
    c, procesador, rechazos = cola  # capacidad 2: NORMAL hasta 2 en espera, CRITICO hasta 4
    await _entregar(c, "sgpmp/E/telemetry", _lectura())  # tapa al worker
    await asyncio.sleep(0)
    for i in range(3):
        await _entregar(c, f"sgpmp/N{i}/telemetry", _lectura())
    assert c.en_espera == 2 and rechazos == [("N2", _lectura())]  # al tercero: RETENER

    for i in range(3):
        await _entregar(c, f"sgpmp/C{i}/telemetry", _lectura(nivel_umbral="critico"))
    assert c.en_espera == 4 and [s for s, _ in rechazos] == ["N2", "C2"]

    for i in range(5):
        await _entregar(c, f"sgpmp/K{i}/status", {"tipo_mensaje": "ACK_UMBRAL"})
    assert c.en_espera == 9
    assert c.rechazados == {Prioridad.NORMAL: 1, Prioridad.CRITICO: 1}
    procesador.liberar.set()


async def test_vision_rechazada_no_avisa_al_nodo(cola) -> None:
    c, procesador, rechazos = cola
    await _entregar(c, "sgpmp/E/telemetry", _lectura())
    await asyncio.sleep(0)
    for i in range(3):
        await _entregar(c, f"sgpmp/CAM{i}/vision", {"area_id": 1})
    assert c.rechazados[Prioridad.NORMAL] == 1 and rechazos == []
    procesador.liberar.set()


async def test_sin_iniciar_procesa_en_el_acto(monkeypatch) -> None:
    vistos = []

    async def procesar(iot, data, topic):
        vistos.append(topic)

    monkeypatch.setattr(cola_mod.handlers, "procesar", procesar)
    await ColaIngesta().recibir("sgpmp/E/heartbeat", b"{}")
    assert vistos == ["sgpmp/E/heartbeat"]


async def test_catalogo_sin_columna_es_critica_no_marca_ninguna() -> None:
    class _Sesion:
        async def execute(self, *_a, **_kw):
            return type("R", (), {"first": lambda _s: None})()

    assert await registry.variables_criticas(_Sesion()) == set()


async def test_si_la_bd_falla_se_conserva_el_catalogo_anterior(monkeypatch) -> None:
    monkeypatch.undo()  # sin el doble del fixture: se prueba la caché real
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setattr(prioridad, "_criticas", {"nombres": frozenset({"NH3"}), "leido_en": -1e9})

    async def caida(_session):
        raise ConnectionRefusedError()

    monkeypatch.setattr(prioridad.registry, "variables_criticas", caida)
    assert await prioridad._variables_criticas() == frozenset({"NH3"})
