"""BRK-02 (P3 de AIOT-DEV-03): cliente HTTP del broker hacia sgpmp-backend con el
token de servicio. Nunca lanza por fallas del backend y nunca loguea el token."""

from __future__ import annotations

import functools
import logging

import httpx
import pytest

from app.backend import client
from app.config import get_settings

TOKEN = "token-secreto-de-prueba"


@pytest.fixture
def backend(monkeypatch):
    """Backend simulado: `respuestas` decide qué contesta, `pedidos` registra lo recibido."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("BACKEND_URL", "http://backend:8000/")
    monkeypatch.setenv("BROKER_SERVICE_TOKEN", TOKEN)
    get_settings.cache_clear()
    estado = {"pedidos": [], "respuesta": httpx.Response(201, json={"id_alerta": 1})}

    def manejar(request: httpx.Request) -> httpx.Response:
        estado["pedidos"].append(request)
        r = estado["respuesta"]
        if isinstance(r, Exception):
            raise r
        return r

    transporte = httpx.MockTransport(manejar)
    monkeypatch.setattr(client.httpx, "AsyncClient",
                        functools.partial(httpx.AsyncClient, transport=transporte))
    client._cliente = None
    yield estado
    client._cliente = None
    get_settings.cache_clear()


async def test_envia_con_el_token_de_servicio(backend) -> None:
    r = await client.enviar("/iot/eventos-buffer", {"serial": "EDGE-1"})

    assert (r.ok, r.status, r.cuerpo) == (True, 201, {"id_alerta": 1})
    (pedido,) = backend["pedidos"]
    assert str(pedido.url) == "http://backend:8000/iot/eventos-buffer"
    assert pedido.headers["Authorization"] == f"Bearer {TOKEN}"
    assert pedido.read() == b'{"serial":"EDGE-1"}'


async def test_error_http_del_backend_no_lanza_y_no_loguea_el_token(backend, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    backend["respuesta"] = httpx.Response(401, json={"error_code": "TOKEN_SERVICIO_INVALIDO"})

    r = await client.enviar("/iot/eventos-buffer", {})

    assert (r.ok, r.status, r.error) == (False, 401, "HTTP 401")
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("falla, error", [
    (httpx.ReadTimeout("lento"), "timeout"),
    (httpx.ConnectError("caído"), "backend no disponible: ConnectError"),
])
async def test_timeout_o_backend_caido_no_lanza(backend, falla, error) -> None:
    backend["respuesta"] = falla
    r = await client.enviar("/iot/eventos-buffer", {})
    assert (r.ok, r.error) == (False, error)


async def test_sin_configuracion_no_intenta_llamar(backend, monkeypatch) -> None:
    monkeypatch.setenv("BROKER_SERVICE_TOKEN", "")
    get_settings.cache_clear()
    r = await client.enviar("/iot/eventos-buffer", {})
    assert r.ok is False and backend["pedidos"] == []
