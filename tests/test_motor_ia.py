# | Fecha      | Nombre   | Que se hizo                                                     |
# |------------|----------|-----------------------------------------------------------------|
# | 2026-10-30 | Arekkazu | RF-66: esqueleto del motor de inferencia M04 (M1)                |
# | 2026-10-30 | Arekkazu | RF-66: inferencia ET-02 y entrega de resultados al backend (M2)  |
"""Motor de inferencia M04: arranque, token, inferencia ET-02 y entrega al backend.

Requiere el extra ``[motor-ia]`` (onnxruntime, numpy, pandas); sin él se omite.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("onnxruntime")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from motor_ia.backend import normalizar_url_base  # noqa: E402
from motor_ia.individual import (  # noqa: E402
    CatalogoInvalidoError,
    a_resultado_rf66,
    cargar_catalogo,
    inferir,
)
from motor_ia.main import MotorIASettings, crear_app  # noqa: E402

TOKEN = "token-motor"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
FIXTURES = Path(__file__).parent / "fixtures" / "et02_bov001"
REFERENCIA = json.loads((FIXTURES / "referencia.json").read_text(encoding="utf-8"))["casos"]
NORMAL, SEPTICEMIA, ESTRES = REFERENCIA
BACKEND = "https://api.arekkazu.top/api-sgpmp-test"


def _app(**kw) -> TestClient:
    return TestClient(
        crear_app(MotorIASettings(iot_inferencia_m04_token=TOKEN, **kw.pop("cfg", {})), **kw)
    )


# ── Arranque y token ─────────────────────────────────────────────────────


def test_no_arranca_sin_token() -> None:
    with pytest.raises(RuntimeError, match="IOT_INFERENCIA_M04_TOKEN"):
        crear_app(MotorIASettings(iot_inferencia_m04_token=""))


def test_no_arranca_con_modelos_y_sin_backend() -> None:
    with pytest.raises(RuntimeError, match="BACKEND_URL"):
        crear_app(
            MotorIASettings(iot_inferencia_m04_token=TOKEN, motor_ia_dir_modelos=str(FIXTURES))
        )


def test_sin_modelos_503_no_acusa_recibo() -> None:
    cliente = _app()
    assert cliente.get("/v1/healthz").json() == {
        "status": "ok",
        "modelos_cargados": 0,
        "backend": None,
    }
    r = cliente.post(
        "/v1/paquetes", json={"paquetes": [{"id_activo_biologico": 885}]}, headers=AUTH
    )
    assert (r.status_code, r.json()["error_code"]) == (503, "MOTOR_SIN_MODELOS")


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer otro"}, {"Authorization": TOKEN}]
)
def test_paquetes_sin_token_valido_401(headers: dict) -> None:
    r = _app().post("/v1/paquetes", json={"paquetes": [{}]}, headers=headers)
    assert r.status_code == 401
    assert r.json()["detail"]["error_code"] == "TOKEN_SERVICIO_INVALIDO"


@pytest.mark.parametrize("cuerpo", [{}, {"paquetes": []}, {"paquetes": "x"}])
def test_paquetes_cuerpo_invalido_422(cuerpo: dict) -> None:
    assert _app().post("/v1/paquetes", json=cuerpo, headers=AUTH).status_code == 422


# ── Cualquier dominio para el backend ────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "esperada"),
    [
        ("https://api.inmero.co/api-sgpmp-test", "https://api.inmero.co/api-sgpmp-test"),
        ("https://api.arekkazu.top/", "https://api.arekkazu.top"),
        ("https://spi.sgpmp.co:8443/sigab/", "https://spi.sgpmp.co:8443/sigab"),
        ("https://10-0-0-5.sslip.io", "https://10-0-0-5.sslip.io"),
        ("  https://backend.traefik.me  ", "https://backend.traefik.me"),
    ],
)
def test_backend_url_acepta_cualquier_dominio(url: str, esperada: str) -> None:
    assert normalizar_url_base(url) == esperada


@pytest.mark.parametrize(
    "url", ["http://api.arekkazu.top", "api.arekkazu.top", "https://", "https://x.co/?a=1"]
)
def test_backend_url_rechaza_sin_https_o_mal_formada(url: str) -> None:
    with pytest.raises(ValueError):
        normalizar_url_base(url)


# ── Catálogo y pipeline ET-02 ────────────────────────────────────────────


def test_catalogo_verifica_hash(tmp_path: Path) -> None:
    shutil.copytree(FIXTURES, tmp_path, dirs_exist_ok=True)
    datos = bytearray((tmp_path / "BOV-001.onnx").read_bytes())
    datos[-1] ^= 0xFF
    (tmp_path / "BOV-001.onnx").write_bytes(bytes(datos))
    with pytest.raises(CatalogoInvalidoError, match="SHA-256"):
        cargar_catalogo(str(tmp_path))


def test_catalogo_sin_directorio_no_tiene_modelos() -> None:
    assert cargar_catalogo("") == {}


@pytest.mark.parametrize("caso", REFERENCIA, ids=lambda c: c["clase_episodio"])
def test_inferencia_igual_a_la_referencia_et02(caso: dict) -> None:
    modelo = cargar_catalogo(str(FIXTURES))[885]
    contrato, decision = inferir(modelo, caso["lecturas"])
    assert {k: contrato[k] for k in caso["esperado"]} == caso["esperado"]
    assert decision["modo_sombra"] is True  # modelo SINTETICO: calcula y registra, no alerta


def test_sin_lecturas_no_hay_resultado() -> None:
    modelo = cargar_catalogo(str(FIXTURES))[885]
    assert inferir(modelo, []) is None
    assert (
        inferir(modelo, [{"timestamp": "2026-08-24T20:00:00Z", "variable": "PH", "valor": 7}])
        is None
    )


@pytest.mark.parametrize(
    ("caso", "nivel", "codigo"), [(NORMAL, 0, 0), (SEPTICEMIA, 3, 6), (ESTRES, 2, 1)]
)
def test_resultado_rf66_nivel_y_patologia(caso: dict, nivel: int, codigo: int) -> None:
    modelo = cargar_catalogo(str(FIXTURES))[885]
    r = a_resultado_rf66(modelo, *inferir(modelo, caso["lecturas"]), id_paquete=41)
    assert (r["nivel_riesgo"], r["codigo_patologia_rf64"]) == (nivel, codigo)
    assert r["modo_sombra"] is True and r["modo_ejecucion"] == "SERVIDOR"
    assert (r["id_activo_biologico"], r["id_version_modelo"], r["id_paquete_inferencia"]) == (
        885,
        12,
        41,
    )
    # El mismo activo, ventana y versión dan el mismo id: el reenvío no duplica en el backend.
    assert (
        a_resultado_rf66(modelo, *inferir(modelo, caso["lecturas"]), id_paquete=99)[
            "id_resultado_inferencia"
        ]
        == r["id_resultado_inferencia"]
    )


# ── Flujo completo: backend → motor → backend ────────────────────────────


class _BackendFalso:
    def __init__(self, status: int = 201) -> None:
        self.status, self.recibidos = status, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.recibidos.append(
            (str(request.url), request.headers["authorization"], json.loads(request.content))
        )
        return httpx.Response(self.status, json={})


def _con_modelos(backend: _BackendFalso) -> TestClient:
    return _app(
        cfg={"motor_ia_dir_modelos": str(FIXTURES), "backend_url": BACKEND + "/"},
        transport_backend=httpx.MockTransport(backend),
    )


def _lote(*paquetes) -> dict:
    return {"paquetes": list(paquetes)}


def test_infiere_una_vez_por_activo_y_entrega_al_backend() -> None:
    backend = _BackendFalso()
    cliente = _con_modelos(backend)
    assert cliente.get("/v1/healthz").json() == {
        "status": "ok",
        "modelos_cargados": 1,
        "backend": f"{BACKEND}/prediccion/inferencias",
    }

    r = cliente.post(
        "/v1/paquetes",
        headers=AUTH,
        json=_lote(
            {
                "id_paquetes_inferencia": 1,
                "id_activo_biologico": 885,
                "lecturas": SEPTICEMIA["lecturas"],
            },
            {
                "id_paquetes_inferencia": 2,
                "id_activo_biologico": 885,
                "lecturas": SEPTICEMIA["lecturas"],
            },
            {
                "id_paquetes_inferencia": 3,
                "id_activo_biologico": 777,
                "lecturas": SEPTICEMIA["lecturas"],
            },
            {"id_paquetes_inferencia": 4, "id_activo_biologico": None},
        ),
    )
    assert r.status_code == 200, r.text
    assert r.json()["activos_con_modelo"] == 1 and len(r.json()["resultados_registrados"]) == 1
    ((url, auth, cuerpo),) = backend.recibidos
    assert url == f"{BACKEND}/prediccion/inferencias" and auth == f"Bearer {TOKEN}"
    assert (cuerpo["id_paquete_inferencia"], cuerpo["nivel_riesgo"]) == (2, 3)


def test_backend_caido_responde_503_para_que_reenvie() -> None:
    r = _con_modelos(_BackendFalso(503)).post(
        "/v1/paquetes",
        headers=AUTH,
        json=_lote(
            {
                "id_paquetes_inferencia": 1,
                "id_activo_biologico": 885,
                "lecturas": NORMAL["lecturas"],
            }
        ),
    )
    assert (r.status_code, r.json()["error_code"]) == (503, "BACKEND_NO_DISPONIBLE")


def test_rechazo_del_backend_no_se_reintenta() -> None:
    r = _con_modelos(_BackendFalso(422)).post(
        "/v1/paquetes",
        headers=AUTH,
        json=_lote(
            {
                "id_paquetes_inferencia": 1,
                "id_activo_biologico": 885,
                "lecturas": NORMAL["lecturas"],
            }
        ),
    )
    assert r.status_code == 200 and r.json()["rechazados_por_backend"] == 1


def test_activo_sin_lecturas_suficientes_no_envia_nada() -> None:
    backend = _BackendFalso()
    r = _con_modelos(backend).post(
        "/v1/paquetes",
        headers=AUTH,
        json=_lote(
            {
                "id_paquetes_inferencia": 1,
                "id_activo_biologico": 885,
                "lecturas": NORMAL["lecturas"][:3],
            }
        ),
    )
    assert r.status_code == 200 and r.json()["sin_resultado"] == 1
    assert backend.recibidos == []
