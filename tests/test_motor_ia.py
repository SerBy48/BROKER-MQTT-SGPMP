# | Fecha      | Nombre   | Que se hizo                                                     |
# |------------|----------|-----------------------------------------------------------------|
# | 2026-10-30 | Arekkazu | RF-66: esqueleto del motor de inferencia M04 (M1)                |
"""Esqueleto del motor de inferencia M04: arranque, token y respuesta sin modelos."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from motor_ia.main import MotorIASettings, crear_app

TOKEN = "token-motor"
LOTE = {"paquetes": [{"id_paquetes_inferencia": 1, "tipo_variable": "TEMPERATURA"}]}


@pytest.fixture
def cliente() -> TestClient:
    return TestClient(crear_app(MotorIASettings(iot_inferencia_m04_token=TOKEN)))


def test_no_arranca_sin_token() -> None:
    with pytest.raises(RuntimeError, match="IOT_INFERENCIA_M04_TOKEN"):
        crear_app(MotorIASettings(iot_inferencia_m04_token=""))


def test_healthz_sin_token(cliente: TestClient) -> None:
    r = cliente.get("/v1/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "modelos_cargados": 0}


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer otro"}, {"Authorization": TOKEN}]
)
def test_paquetes_sin_token_valido_401(cliente: TestClient, headers: dict) -> None:
    r = cliente.post("/v1/paquetes", json=LOTE, headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"]["error_code"] == "TOKEN_SERVICIO_INVALIDO"


def test_paquetes_sin_modelos_503_no_acusa_recibo(cliente: TestClient) -> None:
    r = cliente.post("/v1/paquetes", json=LOTE, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 503
    assert r.json()["error_code"] == "MOTOR_SIN_MODELOS"


@pytest.mark.parametrize("cuerpo", [{}, {"paquetes": []}, {"paquetes": "x"}])
def test_paquetes_cuerpo_invalido_422(cliente: TestClient, cuerpo: dict) -> None:
    r = cliente.post("/v1/paquetes", json=cuerpo, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 422


def test_root_path_para_traefik() -> None:
    settings = MotorIASettings(iot_inferencia_m04_token=TOKEN, motor_ia_root_path="/model-ia-test")
    app = crear_app(settings)
    assert app.root_path == "/model-ia-test"
