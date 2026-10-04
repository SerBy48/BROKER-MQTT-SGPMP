"""Comando a un dispositivo cuyo Gateway Edge está apagado: PENDIENTE al instante.

Antes el broker publicaba y esperaba los 30 s del ACK (NO_CONF), porque el
estado en BD sigue ACTIVO un rato después de apagar el Edge. Lo que vale la
pena fijar: solo se corta cuando es SEGURO que nadie recibe el comando; ante
la duda (sin credencial propia, credencial legacy en uso, dynsec caído) se
publica como siempre.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.config import get_settings
from app.db.repositories.registry import EstadoDispositivo
from app.schemas import CommandRequest
from app.services import credenciales_mqtt, dispatch


@pytest.fixture(autouse=True)
def _entorno(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("MQTT_DEVICE_USERNAME", "")
    monkeypatch.setenv("MQTT_DEVICE_PASSWORD", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _cliente(conexiones: int) -> dict:
    return {"data": {"client": {"connections": [{"address": "1.2.3.4"}] * conexiones}}}


def _dynsec(monkeypatch, *respuestas: dict) -> list:
    pedidos: list = []

    async def ejecutar(comandos):
        pedidos.append([c["username"] for c in comandos])
        return list(respuestas)

    monkeypatch.setattr(credenciales_mqtt.dynsec, "ejecutar", ejecutar)
    return pedidos


async def test_sin_conexiones_y_sin_legacy_es_seguro(monkeypatch) -> None:
    pedidos = _dynsec(monkeypatch, _cliente(0))

    assert await credenciales_mqtt.sin_conexion("EDGE-1") is True
    assert pedidos == [["EDGE-1"]]


@pytest.mark.parametrize(
    "respuesta",
    [_cliente(1), {"error": "Client not found"}],
    ids=["conectado", "sin-credencial-propia"],
)
async def test_conectado_o_sin_credencial_propia_no_se_corta(monkeypatch, respuesta) -> None:
    _dynsec(monkeypatch, respuesta)

    assert await credenciales_mqtt.sin_conexion("EDGE-1") is False


@pytest.mark.parametrize(("conexiones_legacy", "esperado"), [(1, False), (0, True)])
async def test_con_alguien_en_la_credencial_legacy_no_se_sabe(
    monkeypatch, conexiones_legacy, esperado
) -> None:
    monkeypatch.setenv("MQTT_DEVICE_USERNAME", "sgpmp_devices")
    monkeypatch.setenv("MQTT_DEVICE_PASSWORD", "x")
    get_settings.cache_clear()
    pedidos = _dynsec(monkeypatch, _cliente(0), _cliente(conexiones_legacy))

    assert await credenciales_mqtt.sin_conexion("EDGE-1") is esperado
    assert pedidos == [["EDGE-1", "sgpmp_devices"]]


def _despacho(monkeypatch, *, sin_conexion) -> list:
    @asynccontextmanager
    async def sesion_falsa():
        yield object()

    async def resolver_id(_session, _serial):
        return 1

    async def estado(_session, _serial):
        return "ACTIVO"

    async def mapa(_session):
        return {"ESP-1": EstadoDispositivo(activo=True, serial_gateway="EDGE-1")}

    async def consultar_conexion(usuario):
        assert usuario == "EDGE-1", "se consulta al Edge que atiende al dispositivo"
        if isinstance(sin_conexion, Exception):
            raise sin_conexion
        return sin_conexion

    publicados: list = []

    async def publicar(serial, payload, qos=1):
        publicados.append(serial)
        return f"sgpmp/{serial}/command"

    monkeypatch.setattr(dispatch, "async_session_factory", sesion_falsa)
    monkeypatch.setattr(dispatch.registry, "resolve_device_id", resolver_id)
    monkeypatch.setattr(dispatch.registry, "resolve_device_state", estado)
    monkeypatch.setattr(dispatch.registry, "mapa_dispositivos", mapa)
    monkeypatch.setattr(dispatch.credenciales_mqtt, "sin_conexion", consultar_conexion)
    monkeypatch.setattr(dispatch.publisher, "publish_command", publicar)
    monkeypatch.setenv("MQTT_ACK_TIMEOUT_SECONDS", "0")
    get_settings.cache_clear()
    return publicados


def _peticion() -> CommandRequest:
    return CommandRequest(
        origen="configuracion", serial="ESP-1", frecuencia_captura=15, intervalo_transmision=15
    )


async def test_edge_apagado_queda_pendiente_sin_publicar(monkeypatch) -> None:
    publicados = _despacho(monkeypatch, sin_conexion=True)

    respuesta = await dispatch.dispatch_command(_peticion())

    assert respuesta.estado == "PENDIENTE"
    assert "EDGE-1 no está conectado" in respuesta.mensaje
    assert publicados == []


@pytest.mark.parametrize(
    "sin_conexion", [False, TimeoutError("dynsec lento")], ids=["conectado", "dynsec-falla"]
)
async def test_conectado_o_sin_datos_se_publica_como_siempre(monkeypatch, sin_conexion) -> None:
    publicados = _despacho(monkeypatch, sin_conexion=sin_conexion)

    respuesta = await dispatch.dispatch_command(_peticion())

    assert publicados == ["ESP-1"]
    assert respuesta.estado == "NO_CONF"  # nadie manda el ACK en el test
