# | Fecha      | Nombre   | Que se hizo                                                       |
# |------------|----------|-------------------------------------------------------------------|
# | 2026-10-29 | Arekkazu | TC-DIS-148 RF-54: metricas_buffer del heartbeat a json_metricas_buffer |
"""TC-DIS-148 (RF-54): el heartbeat trae la ocupación del buffer del Edge y el broker la
guarda en `modulo3.heartbeats.json_metricas_buffer`, que lee el panel del backend.

Un reporte inválido se descarta sin perder el heartbeat, y si la BD aún no tiene la
columna se guarda como siempre (misma regla que D11)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.config import get_settings
from app.db.repositories import telemetry
from app.schemas import HeartbeatPayload

METRICAS = {
    "capacidad_total": 10000,
    "capacidad_utilizada": 8500,
    "ultimo_timestamp_pendiente": "2026-10-29T14:00:00Z",
    "politica_buffer_activa": "POLITICA_SOBRESCRITURA",
}


class _Sesion:
    def __init__(self, columna: bool) -> None:
        self.columna = columna
        self.sentencias: list[tuple[str, dict]] = []

    async def execute(self, sentencia, parametros=None):
        sql = str(sentencia)
        self.sentencias.append((sql, parametros or {}))
        if "information_schema" in sql:
            pedidas = (parametros or {}).get("columnas", [])
            valor = len(pedidas) if self.columna and pedidas == ["json_metricas_buffer"] else 0
        else:
            valor = 41
        return type("R", (), {"scalar_one": lambda _self: valor})()


@pytest.fixture(autouse=True)
def _entorno(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    get_settings.cache_clear()
    monkeypatch.setattr(telemetry, "_d11", {"disponible": False, "revisado_en": None})
    monkeypatch.setattr(telemetry, "_metricas", {"disponible": False, "revisado_en": None})
    yield
    get_settings.cache_clear()


async def _insertar(sesion, payload):
    return await telemetry.insert_heartbeat(
        sesion, device_id=1, payload=payload, fecha_registro=datetime(2026, 10, 29, tzinfo=UTC)
    )


async def test_con_la_columna_guarda_las_metricas_con_el_contrato_del_backend() -> None:
    sesion = _Sesion(columna=True)
    assert await _insertar(sesion, HeartbeatPayload(metricas_buffer=METRICAS)) == 41

    sql, parametros = sesion.sentencias[-1]
    assert "json_metricas_buffer" in sql
    guardado = json.loads(parametros["metricas_buffer"])
    # Exactamente las 4 llaves de MetricasBufferDTO (extra='forbid' en el backend).
    assert set(guardado) == set(METRICAS)
    assert guardado["capacidad_utilizada"] == 8500
    assert guardado["politica_buffer_activa"] == "POLITICA_SOBRESCRITURA"


async def test_sin_la_columna_el_heartbeat_se_guarda_como_siempre() -> None:
    sesion = _Sesion(columna=False)
    await _insertar(sesion, HeartbeatPayload(metricas_buffer=METRICAS))

    sql, parametros = sesion.sentencias[-1]
    assert "json_metricas_buffer" not in sql and "metricas_buffer" not in parametros


async def test_sin_metricas_no_se_pregunta_por_la_columna() -> None:
    sesion = _Sesion(columna=True)
    await _insertar(sesion, HeartbeatPayload())
    assert not any("json_metricas_buffer" in str(p) for s, p in sesion.sentencias if "information_schema" in s)
    assert "json_metricas_buffer" not in sesion.sentencias[-1][0]


@pytest.mark.parametrize("cambio", [
    {"capacidad_utilizada": 10001},
    {"capacidad_total": 0},
    {"politica_buffer_activa": "DESCARTAR"},
    {"extra": 1},
])
def test_metricas_invalidas_se_descartan_sin_perder_el_heartbeat(cambio) -> None:
    payload = HeartbeatPayload(datos_pendientes_buffer=7, metricas_buffer=METRICAS | cambio)
    assert payload.metricas_buffer is None
    assert payload.datos_pendientes_buffer == 7


def test_ultimo_timestamp_pendiente_es_opcional_pero_viaja() -> None:
    payload = HeartbeatPayload(metricas_buffer=METRICAS | {"ultimo_timestamp_pendiente": None})
    assert payload.metricas_buffer.model_dump(mode="json")["ultimo_timestamp_pendiente"] is None
