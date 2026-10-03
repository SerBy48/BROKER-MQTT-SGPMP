"""SEG-BROKER-03: el endpoint de credenciales valida el serial antes de tocar Mosquitto."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import require_api_token
from app.main import app
from app.services import credenciales_mqtt


@pytest.fixture
def cliente(monkeypatch: pytest.MonkeyPatch):
    llamadas: list[tuple] = []

    async def emitir(serial, adicionales):
        llamadas.append((serial, adicionales))
        return credenciales_mqtt.CredencialEmitida(serial, "secreta", [serial, *adicionales])

    monkeypatch.setattr(credenciales_mqtt, "emitir", emitir)
    app.dependency_overrides[require_api_token] = lambda: None
    yield TestClient(app), llamadas
    app.dependency_overrides.clear()


def test_emitir_devuelve_la_clave_sin_cache(cliente) -> None:
    http, llamadas = cliente
    r = http.post("/v1/devices/RPI-1/credential", json={"seriales_adicionales": ["ESP-2"]})
    assert r.status_code == 201
    assert r.json() == {"usuario": "RPI-1", "password": "secreta", "seriales": ["RPI-1", "ESP-2"]}
    assert r.headers["cache-control"] == "no-store"
    assert llamadas == [("RPI-1", ["ESP-2"])]


@pytest.mark.parametrize("serial", ["RPI+1", "RPI%231", "a" * 51])
def test_serial_con_comodines_o_largo_invalido_se_rechaza_en_el_path(cliente, serial) -> None:
    http, llamadas = cliente
    assert http.post(f"/v1/devices/{serial}/credential", json={}).status_code == 422
    assert llamadas == []


@pytest.mark.parametrize("adicional", ["#", "sgpmp/+", "ESP 2"])
def test_serial_adicional_con_comodines_se_rechaza(cliente, adicional) -> None:
    http, llamadas = cliente
    r = http.post("/v1/devices/RPI-1/credential", json={"seriales_adicionales": [adicional]})
    assert r.status_code == 422
    assert llamadas == []


def test_sin_token_no_se_emite(monkeypatch) -> None:
    app.dependency_overrides.clear()
    r = TestClient(app).post("/v1/devices/RPI-1/credential", json={})
    assert r.status_code == 401
