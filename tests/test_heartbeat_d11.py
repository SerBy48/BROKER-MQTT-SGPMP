"""BRK-07 (D11 de AIOT-DEV-03): el heartbeat acepta `sensores_operativos` y
`temperatura_dispositivo`, y solo los escribe si la BD ya tiene las columnas de DB-03
(regla 2 del acuerdo: lo que sale antes que su par tolera su ausencia)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.db.repositories import telemetry
from app.schemas import HeartbeatPayload


class _Sesion:
    def __init__(self, columnas_d11: int) -> None:
        self.columnas_d11 = columnas_d11
        self.sentencias: list[tuple[str, dict]] = []

    async def execute(self, sentencia, parametros=None):
        sql = str(sentencia)
        self.sentencias.append((sql, parametros or {}))
        valor = self.columnas_d11 if "information_schema" in sql else 41
        return type("R", (), {"scalar_one": lambda _self: valor})()


@pytest.fixture(autouse=True)
def _entorno(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    get_settings.cache_clear()
    monkeypatch.setattr(telemetry, "_d11", {"disponible": False, "revisado_en": None})
    yield
    get_settings.cache_clear()


def _hb(**extra) -> HeartbeatPayload:
    return HeartbeatPayload(tipo_mensaje="HEARTBEAT", **extra)


async def _insertar(sesion, payload):
    return await telemetry.insert_heartbeat(
        sesion, device_id=1, payload=payload, fecha_registro=datetime(2026, 10, 17, tzinfo=UTC)
    )


async def test_con_las_columnas_de_db03_guarda_los_dos_campos() -> None:
    sesion = _Sesion(columnas_d11=2)
    payload = _hb(sensores_operativos=[{"sensor": "dht22", "operativo": False}],
                  temperatura_dispositivo=71.5)

    assert await _insertar(sesion, payload) == 41

    sql, parametros = sesion.sentencias[-1]
    assert "json_sensores_operativos" in sql and "temperatura_dispositivo" in sql
    sensores = json.loads(parametros["sensores_operativos"])
    assert sensores == [{"sensor": "dht22", "operativo": False}]
    assert parametros["temperatura_dispositivo"] == 71.5


async def test_sin_db03_el_heartbeat_se_guarda_como_siempre() -> None:
    sesion = _Sesion(columnas_d11=0)
    await _insertar(sesion, _hb(temperatura_dispositivo=40.0))

    sql, parametros = sesion.sentencias[-1]
    assert "temperatura_dispositivo" not in sql and "temperatura_dispositivo" not in parametros


async def test_la_ausencia_de_columnas_se_revisa_cada_tanto_y_su_presencia_una_vez() -> None:
    sin = _Sesion(columnas_d11=0)
    await _insertar(sin, _hb())
    await _insertar(sin, _hb())
    assert sum("information_schema" in s for s, _ in sin.sentencias) == 1  # cacheado 10 min

    telemetry._d11["revisado_en"] -= telemetry._REVISAR_D11_CADA_S + 1
    con = _Sesion(columnas_d11=2)
    await _insertar(con, _hb())
    await _insertar(con, _hb())
    assert sum("information_schema" in s for s, _ in con.sentencias) == 1  # ya no se pregunta


async def test_firmware_viejo_sin_los_campos_nuevos_sigue_valido() -> None:
    sesion = _Sesion(columnas_d11=2)
    await _insertar(sesion, _hb())
    _, parametros = sesion.sentencias[-1]
    assert parametros["sensores_operativos"] is None
    assert parametros["temperatura_dispositivo"] is None


@pytest.mark.parametrize("extra", [
    {"temperatura_dispositivo": float("nan")},
    {"sensores_operativos": [{"sensor": "", "operativo": True}]},
    {"sensores_operativos": [{"sensor": "dht22"}]},
])
def test_valores_invalidos_se_rechazan(extra) -> None:
    with pytest.raises(ValidationError):
        _hb(**extra)
