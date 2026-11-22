# | Fecha      | Nombre   | Que se hizo                                                        |
# |------------|----------|--------------------------------------------------------------------|
# | 2026-10-29 | Arekkazu | RLS M03 (#808): cada transacción declara el usuario de servicio     |
"""RLS M03 (backend #808): toda transacción del broker declara el usuario de servicio.

Con FORCE ROW LEVEL SECURITY y sin BYPASSRLS, una transacción sin
`app.current_user_id` no ve dispositivos y sus INSERT de ingesta violan RLS.
"""

from __future__ import annotations

import pytest

from app.db import engine


class _Conexion:
    def __init__(self, id_servicio) -> None:
        self.id_servicio = id_servicio
        self.sentencias: list[tuple[str, dict]] = []

    def execute(self, sentencia, parametros):
        self.sentencias.append((str(sentencia), parametros))
        id_servicio = self.id_servicio

        class _Resultado:
            def scalar(self):
                return id_servicio

        return _Resultado()


@pytest.fixture(autouse=True)
def _sin_cache(monkeypatch) -> None:
    monkeypatch.setattr(engine, "_id_usuario_servicio", None)


def test_cada_transaccion_declara_usuario_y_rol_de_servicio() -> None:
    conexion = _Conexion(id_servicio=3)

    engine._declarar_servicio(None, None, conexion)
    engine._declarar_servicio(None, None, conexion)

    busquedas = [p for s, p in conexion.sentencias if "fn_id_usuario_por_correo" in s]
    declaraciones = [p for s, p in conexion.sentencias if "set_config" in s]
    assert busquedas == [{"correo": "servicio.sistema@sgpmp.local"}]  # se resuelve una vez
    assert declaraciones == [{"uid": "3", "rol": "Administrador"}] * 2


def test_sin_usuario_de_servicio_falla_explicito() -> None:
    with pytest.raises(RuntimeError, match="servicio.sistema@sgpmp.local"):
        engine._declarar_servicio(None, None, _Conexion(id_servicio=None))


def test_las_sesiones_del_broker_usan_la_clase_con_el_listener() -> None:
    assert engine.async_session_factory().sync_session_class is engine._SesionServicio
