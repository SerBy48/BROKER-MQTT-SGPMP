"""SEG-BROKER-03: el endpoint de credenciales valida el serial antes de tocar Mosquitto."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import require_api_token
from app.core.errors import DeviceDependsOnGatewayError
from app.main import app
from app.services import credenciales_mqtt


@pytest.fixture
def cliente(monkeypatch: pytest.MonkeyPatch):
    llamadas: list[tuple] = []

    async def emitir(serial):
        llamadas.append(("emitir", serial))
        if serial == "ESP-1":
            raise DeviceDependsOnGatewayError(serial, "EDGE-1")
        return credenciales_mqtt.CredencialEmitida(serial, "secreta", [serial, "ESP-1"])

    async def sincronizar_credencial(serial):
        llamadas.append(("sincronizar", serial))
        return serial != "SIN-CREDENCIAL"

    monkeypatch.setattr(credenciales_mqtt, "emitir", emitir)
    monkeypatch.setattr(credenciales_mqtt, "sincronizar_credencial", sincronizar_credencial)
    app.dependency_overrides[require_api_token] = lambda: None
    yield TestClient(app), llamadas
    app.dependency_overrides.clear()


def test_emitir_devuelve_la_clave_sin_cache(cliente) -> None:
    http, llamadas = cliente
    r = http.post("/v1/devices/EDGE-1/credential")
    assert r.status_code == 201
    assert r.json() == {"usuario": "EDGE-1", "password": "secreta", "seriales": ["EDGE-1", "ESP-1"]}
    assert r.headers["cache-control"] == "no-store"
    assert llamadas == [("emitir", "EDGE-1")]


def test_emitir_a_un_dispositivo_de_un_edge_es_409(cliente) -> None:
    http, _ = cliente
    r = http.post("/v1/devices/ESP-1/credential")
    assert r.status_code == 409 and "EDGE-1" in r.json()["detail"]


def test_sincronizar(cliente) -> None:
    http, llamadas = cliente
    assert http.post("/v1/devices/EDGE-1/credential/sync").status_code == 204
    assert http.post("/v1/devices/SIN-CREDENCIAL/credential/sync").status_code == 404
    assert llamadas == [("sincronizar", "EDGE-1"), ("sincronizar", "SIN-CREDENCIAL")]


@pytest.mark.parametrize("serial", ["RPI+1", "RPI%231", "a" * 51])
def test_serial_con_comodines_o_largo_invalido_se_rechaza_en_el_path(cliente, serial) -> None:
    http, llamadas = cliente
    assert http.post(f"/v1/devices/{serial}/credential").status_code == 422
    assert http.post(f"/v1/devices/{serial}/credential/sync").status_code == 422
    assert llamadas == []


def test_sin_token_no_se_emite(monkeypatch) -> None:
    app.dependency_overrides.clear()
    r = TestClient(app).post("/v1/devices/EDGE-1/credential")
    assert r.status_code == 401
