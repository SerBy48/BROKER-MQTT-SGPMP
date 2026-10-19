"""BRK-08 (D6 de AIOT-DEV-03): `POST /v1/ota/notify` publica el aviso de OTA en
`sgpmp/<serial>/ota/notify` y responde sin esperar la instalación."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import dependencies
from app.config import get_settings
from app.core.errors import MqttNotConnectedError
from app.main import app
from app.services import ota

PEDIDO = {
    "serial": "EDGE-1",
    "id_despliegue_ota": 31,
    "tipo_modelo": "AVES",
    "componente": "DETECTOR",
    "version": "3.0.0",
    "url_descarga": "https://sgpmp.example/prediccion/ota/artefactos/31?firma=abc&expira=1700",
    "sha256": "a" * 64,
    "firma": "MEUCIQ...",
    "tamano_bytes": 1048576,
    "parametros": {"tamano_fragmento_bytes": 65536, "max_reintentos_descarga": 5,
                   "nivel_bateria_minimo": 40},
}


@pytest.fixture
def cliente(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    get_settings.cache_clear()
    publicados: list[tuple[str, dict]] = []

    async def publicar(serial, payload, qos=1):
        publicados.append((serial, payload))
        return f"sgpmp/{serial}/ota/notify"

    async def sin_token():
        return None

    monkeypatch.setattr(ota.publisher, "publish_ota_notify", publicar)
    app.dependency_overrides[dependencies.require_api_token] = sin_token
    yield TestClient(app), publicados
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_publica_el_aviso_y_responde_202(cliente) -> None:
    http, publicados = cliente
    r = http.post("/v1/ota/notify", json=PEDIDO)

    assert r.status_code == 202, r.text
    assert r.json()["topic"] == "sgpmp/EDGE-1/ota/notify"
    ((serial, payload),) = publicados
    assert serial == "EDGE-1" and "serial" not in payload
    assert payload["tipo_mensaje"] == "OTA_NOTIFY"
    assert payload["id_mensaje"] == r.json()["id_mensaje"] and payload["emitido_en"]
    assert payload["url_descarga"] == PEDIDO["url_descarga"]
    assert payload["sha256"] == "a" * 64 and payload["parametros"] == PEDIDO["parametros"]


def test_sin_mqtt_responde_503_para_que_el_backend_reintente(cliente, monkeypatch) -> None:
    http, _ = cliente

    async def caido(*_a, **_kw):
        raise MqttNotConnectedError()

    monkeypatch.setattr(ota.publisher, "publish_ota_notify", caido)
    assert http.post("/v1/ota/notify", json=PEDIDO).status_code == 503


@pytest.mark.parametrize("cambio", [
    {"sha256": "A" * 64},  # hex en minúsculas, 64 caracteres
    {"sha256": "abc"},
    {"url_descarga": "no-es-url"},
    {"serial": "EDGE/#"},  # el serial termina en el topic: sin comodines
    {"id_despliegue_ota": 0},
])
def test_pedido_invalido_es_422_y_no_publica(cliente, cambio) -> None:
    http, publicados = cliente
    assert http.post("/v1/ota/notify", json={**PEDIDO, **cambio}).status_code == 422
    assert publicados == []


def test_exige_el_token_del_backend(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    get_settings.cache_clear()
    assert TestClient(app).post("/v1/ota/notify", json=PEDIDO).status_code == 401
