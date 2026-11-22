# | Fecha      | Nombre   | Que se hizo |
# |------------|----------|-------------|
# | 2026-11-01 | Arekkazu | RF-23/RF-22: resolución de sensor por nombre o por sensor único |
"""`resolve_sensor_id`: la lectura identifica su sensor por nombre; sin nombre, solo vale
cuando el dispositivo tiene un único sensor activo (antes se tomaba el primero)."""

from __future__ import annotations

import pytest

from app.db.repositories import registry


class _Resultado:
    def __init__(self, filas):
        self._filas = filas

    def first(self):
        return self._filas[0] if self._filas else None

    def all(self):
        return self._filas


class _Sesion:
    def __init__(self, filas):
        self.filas = filas
        self.consultas: list[tuple[str, dict]] = []

    async def execute(self, sql, params):
        self.consultas.append((str(sql), params))
        return _Resultado(self.filas)


async def test_con_nombre_busca_por_dispositivo_y_nombre() -> None:
    sesion = _Sesion([(42,)])
    assert await registry.resolve_sensor_id(sesion, 7, "SEN-TEMP") == 42
    ((sql, params),) = sesion.consultas
    assert "nombre = :nombre" in sql and params == {"device_id": 7, "nombre": "SEN-TEMP"}


async def test_con_nombre_inexistente_no_resuelve() -> None:
    assert await registry.resolve_sensor_id(_Sesion([]), 7, "SEN-X") is None


@pytest.mark.parametrize(
    "filas, esperado",
    [([(5,)], 5), ([(5,), (6,)], None), ([], None)],
    ids=["un-sensor", "varios-sensores", "sin-sensores"],
)
async def test_sin_nombre_solo_resuelve_con_un_unico_sensor_activo(filas, esperado) -> None:
    sesion = _Sesion(filas)
    assert await registry.resolve_sensor_id(sesion, 7) == esperado
    ((sql, _),) = sesion.consultas
    assert "es_activo = true" in sql and "LIMIT 2" in sql
