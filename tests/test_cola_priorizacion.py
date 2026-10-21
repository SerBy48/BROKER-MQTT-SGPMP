# | Fecha      | Nombre   | Que se hizo |
# |------------|----------|-------------|
# | 2026-10-20 | Arekkazu | BRK-11 RFC-016: severidad del broker, criticidad del área, origen y oráculo §4 |
"""BRK-09/BRK-11 (RF-53 E15, RFC-016 v1.2): cola de prioridad bajo saturación."""

from __future__ import annotations

import asyncio
import itertools
import json

import pytest

from app.config import get_settings
from app.db.repositories import registry
from app.mqtt import cola as cola_mod
from app.mqtt.cola import ColaIngesta
from app.services import prioridad
from app.services.prioridad import Prioridad, clasificar


# Área 1 ALTA con dos especies (bandas críticas de cada una), área 2 BAJA, área 3 sin
# nivel_criticidad (MEDIA). Los dispositivos A1, B2 y M3 viven en esas áreas.
CONTEXTO = registry.ContextoPrioridad(
    area_por_serial={"A1": 1, "B2": 2, "M3": 3},
    criticidad_por_area={1: "ALTA", 2: "BAJA"},
    bandas_criticas={(1, "Temperatura del agua"): [(0.0, 20.0), (18.0, 25.0)]},
)


@pytest.fixture(autouse=True)
def _entorno(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("MQTT_COLA_CAPACIDAD", "2")
    monkeypatch.setenv("MQTT_COLA_WORKERS", "1")
    get_settings.cache_clear()

    async def catalogo():
        return frozenset({"Oxígeno disuelto"}), CONTEXTO

    monkeypatch.setattr(prioridad, "_catalogo", catalogo)
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
    assert (await clasificar(tipo, data))[0] == esperado


@pytest.mark.parametrize("serial, data, critica", [
    ("A1", _lectura(valor_crudo=19.0), True),                       # banda de una sola especie
    ("A1", _lectura(valor_crudo=22.0), True),                       # banda de la otra especie
    ("A1", _lectura(valor_crudo=27.0, nivel_umbral="critico"), False),  # el broker sabe más que el Edge
    ("B2", _lectura(valor_crudo=1.0, nivel_umbral="critico"), True),   # sin umbral: vale la pista
    ("B2", _lectura(valor_crudo=1.0), False),
    ("A1", _lectura(valor_crudo="x"), False),
], ids=["especie-1", "especie-2-mas-restrictiva", "edge-no-manda", "pista-del-edge",
        "sin-umbral-ni-pista", "valor-no-numerico"])
async def test_severidad_la_decide_el_broker(serial, data, critica) -> None:
    prioridad_, clave = await clasificar("telemetry", data, serial)
    assert (prioridad_ == Prioridad.CRITICO) is critica and (clave[1] == 0) is critica


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
    await _entregar(c, "sgpmp/E/vision", {"area_id": 3})  # misma criticidad (MEDIA) que E
    await _entregar(c, "sgpmp/E/heartbeat", {})
    await _entregar(c, "sgpmp/E/status", {"tipo_mensaje": "ACK_UMBRAL"})
    await _entregar(c, "sgpmp/VIEJO/evento", {"timestamp_evento": "2026-10-15T00:00:00Z"})

    procesador.liberar.set()
    await c._cola.join()

    assert procesador.orden == [
        "sgpmp/E/telemetry",
        "sgpmp/E/status",          # control
        "sgpmp/VIEJO/evento",      # evento Edge antes que heartbeat (tipo de dato)
        "sgpmp/E/heartbeat",
        "sgpmp/E/vision",
    ]


async def _orden(c, procesador, mensajes):
    await _entregar(c, "sgpmp/TAPON/telemetry", _lectura())  # tapa al worker
    await asyncio.sleep(0)
    for topic, data in mensajes:
        await _entregar(c, topic, data)
    procesador.liberar.set()
    await c._cola.join()
    return procesador.orden[1:]


ESPERADO_RFC016 = [
    "sgpmp/X/status",                 # 0. control
    "sgpmp/A1/telemetry#critica",     # 1. severidad: dato crítico, área ALTA
    "sgpmp/B2/telemetry#critica",     #    dato crítico, área BAJA
    "sgpmp/A1/evento",                # 2. área ALTA, 3. evento > heartbeat > lectura
    "sgpmp/A1/heartbeat",
    "sgpmp/A1/telemetry#real",        # 4. TIEMPO_REAL antes que BUFFER_LOCAL
    "sgpmp/A1/telemetry#buffer-viejo",
    "sgpmp/A1/telemetry#buffer",      # 5. dentro del origen, el más viejo primero
    "sgpmp/M3/telemetry",             # área sin nivel → MEDIA
    "sgpmp/B2/telemetry",             # área BAJA
]


def _mensajes_rfc016():
    return [
        ("sgpmp/B2/telemetry", _lectura(marca="sgpmp/B2/telemetry")),
        ("sgpmp/A1/telemetry", _lectura(valor_crudo=27.0, marca="sgpmp/A1/telemetry#buffer", origen="BUFFER_LOCAL",
                                        timestamp_captura="2026-10-18T09:00:00+00:00")),
        ("sgpmp/A1/telemetry", _lectura(valor_crudo=27.0, marca="sgpmp/A1/telemetry#buffer-viejo", origen="BUFFER_LOCAL",
                                        timestamp_captura="2026-10-15T00:00:00+00:00")),
        ("sgpmp/M3/telemetry", _lectura(marca="sgpmp/M3/telemetry")),
        ("sgpmp/A1/heartbeat", {"marca": "sgpmp/A1/heartbeat", "timestamp_captura": "2026-10-18T10:00:00+00:00"}),
        ("sgpmp/B2/telemetry", _lectura(marca="sgpmp/B2/telemetry#critica", variable="Oxígeno disuelto")),
        ("sgpmp/A1/telemetry", _lectura(valor_crudo=27.0, marca="sgpmp/A1/telemetry#real")),
        ("sgpmp/X/status", {"marca": "sgpmp/X/status", "tipo_mensaje": "ACK_UMBRAL"}),
        ("sgpmp/A1/evento", {"marca": "sgpmp/A1/evento", "timestamp_evento": "2026-10-18T10:00:00+00:00"}),
        ("sgpmp/A1/telemetry", _lectura(marca="sgpmp/A1/telemetry#critica", valor_crudo=10.0)),
    ]


@pytest.mark.parametrize("permutacion", list(itertools.islice(
    itertools.permutations(range(10)), 0, 3628800, 362880)))
async def test_oraculo_rfc016_mismo_conjunto_mismo_orden(monkeypatch, permutacion) -> None:
    monkeypatch.setenv("MQTT_COLA_CAPACIDAD", "100")
    get_settings.cache_clear()
    orden: list[str] = []
    liberar = asyncio.Event()

    async def procesar(iot, data, topic):
        if not orden:
            orden.append("tapon")
            await liberar.wait()
            return
        orden.append(data["marca"])

    monkeypatch.setattr(cola_mod.handlers, "procesar", procesar)
    c = ColaIngesta()
    c.iniciar()
    try:
        await _entregar(c, "sgpmp/TAPON/telemetry", _lectura())
        await asyncio.sleep(0)
        mensajes = _mensajes_rfc016()
        for i in permutacion:
            await _entregar(c, *mensajes[i])
        liberar.set()
        await c._cola.join()
    finally:
        await c.detener()
    assert orden[1:] == ESPERADO_RFC016


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


async def test_si_la_bd_falla_se_conserva_el_contexto_anterior(monkeypatch) -> None:
    monkeypatch.undo()  # sin el doble del fixture: se prueba la caché real
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setattr(prioridad, "_cache", {"criticas": frozenset({"NH3"}), "contexto": CONTEXTO,
                                              "leido_en": -1e9})

    async def caida(_session):
        raise ConnectionRefusedError()

    monkeypatch.setattr(prioridad.registry, "variables_criticas", caida)
    assert await prioridad._catalogo() == (frozenset({"NH3"}), CONTEXTO)
