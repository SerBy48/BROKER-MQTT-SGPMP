# | Fecha      | Nombre   | Que se hizo                                                     |
# |------------|----------|-----------------------------------------------------------------|
# | 2026-10-30 | Arekkazu | RF-66: esqueleto del motor de inferencia M04 (M1)                |
# | 2026-10-30 | Arekkazu | RF-66: inferencia ET-02 y entrega de resultados al backend (M2)  |
# | 2026-10-31 | Arekkazu | RF-17 C3: rangos de M09 en la inferencia, con respaldo a ET-02   |
# | 2026-10-31 | SerBy48  | RF-66: 403/404, timestamps mixtos, lecturas inválidas y reloj    |
"""Motor de inferencia M04: arranque, token, inferencia ET-02 y entrega al backend.

Requiere el extra ``[motor-ia]`` (onnxruntime, numpy, pandas); sin él se omite.
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("onnxruntime")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from motor_ia import et02  # noqa: E402
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
_ESTADO_CALIBRACION_REAL = et02.estado_calibracion


def _fijar_fecha(monkeypatch: pytest.MonkeyPatch, fecha: datetime) -> None:
    monkeypatch.setattr(
        et02,
        "estado_calibracion",
        lambda lb, _ahora, *args: _ESTADO_CALIBRACION_REAL(lb, fecha, *args),
    )


@pytest.fixture(autouse=True)
def _linea_base_vigente(monkeypatch: pytest.MonkeyPatch) -> None:
    """La línea base de prueba vence a los 6 meses (feb. 2027): las pruebas no dependen del día."""
    _fijar_fecha(monkeypatch, datetime(2026, 10, 31, tzinfo=UTC))


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


# ── C3: rangos de M09 · RF-17 ────────────────────────────────────────────

RANGOS_M09_AMPLIOS = {
    "temperatura_corporal": {"sano": [36.0, 42.0], "plausible": [35.0, 43.0]},
    "frecuencia_cardiaca": {"sano": [30.0, 150.0], "plausible": [30.0, 160.0]},
}


def test_rangos_m09_cambian_el_rango_sano_y_no_el_modelo() -> None:
    modelo = cargar_catalogo(str(FIXTURES))[885]
    propio, _ = inferir(modelo, SEPTICEMIA["lecturas"])
    m09, _ = inferir(modelo, SEPTICEMIA["lecturas"], RANGOS_M09_AMPLIOS)
    # Con la banda de ET-02 (38.0–39.5 °C) la fiebre sale de lo sano; con la de M09 (36–42), no.
    assert propio["variables_fuera_rango_sano"] == ["temperatura_corporal"]
    assert m09["variables_fuera_rango_sano"] == []
    assert m09["fuente_rangos_especie"] == {"frecuencia_cardiaca": "M09", "temperatura_corporal": "M09"}
    assert propio["fuente_rangos_especie"] == {"frecuencia_cardiaca": "ET-02", "temperatura_corporal": "ET-02"}
    # En operación el rango sano no filtra: el modelo ve las mismas lecturas.
    assert (m09["id_especie"], m09["clase_predicha"], m09["probabilidades"]) == (
        "BOVINO", propio["clase_predicha"], propio["probabilidades"],
    )


def test_plausible_de_m09_descarta_lecturas_como_regla_dura() -> None:
    modelo = cargar_catalogo(str(FIXTURES))[885]
    # La fiebre del caso (~40–40.6 °C) cabe en la plausible de ET-02 (35–43) pero no en esta.
    rangos = {"temperatura_corporal": {"sano": [38.0, 39.0], "plausible": [37.0, 40.0]}}
    propio, _ = inferir(modelo, SEPTICEMIA["lecturas"])
    m09, _ = inferir(modelo, SEPTICEMIA["lecturas"], rangos)
    assert propio["eventos_regla_dura_rf17"] == []
    assert m09["eventos_regla_dura_rf17"]
    assert {e["rango_plausible_especie"][1] for e in m09["eventos_regla_dura_rf17"]} == {40.0}


@pytest.mark.parametrize("rangos", [
    {"temperatura_corporal": {"sano": [37.0, 41.0], "plausible": [38.0, 40.0]}},  # plausible ⊉ sano
    {"temperatura_corporal": {"sano": [39.5, 38.0], "plausible": [35.0, 43.0]}},  # min > max
    {"temperatura_corporal": {"sano": "38-39"}},                                    # mal formado
    {"actividad_indice": {"sano": [0.2, 0.3], "plausible": [0.0, 0.5]}},           # no configurable
    None,
], ids=["invariante", "invertido", "mal_formado", "actividad", "sin_rangos"])
def test_rango_invalido_usa_el_de_et02(rangos) -> None:
    modelo = cargar_catalogo(str(FIXTURES))[885]
    contrato, _ = inferir(modelo, SEPTICEMIA["lecturas"], rangos)
    assert contrato["fuente_rangos_especie"]["temperatura_corporal"] == "ET-02"
    assert {k: contrato[k] for k in SEPTICEMIA["esperado"]} == SEPTICEMIA["esperado"]


def test_timestamps_en_formatos_mixtos_dan_el_mismo_resultado() -> None:
    modelo = cargar_catalogo(str(FIXTURES))[885]
    lecturas = copy.deepcopy(SEPTICEMIA["lecturas"])
    for lectura in lecturas[::2]:
        lectura["timestamp"] = lectura["timestamp"].replace("+00:00", ".000000+00:00")
    contrato, _ = inferir(modelo, lecturas)
    assert {k: contrato[k] for k in SEPTICEMIA["esperado"]} == SEPTICEMIA["esperado"]


def test_timestamps_con_y_sin_fraccion_producen_resultado() -> None:
    modelo = cargar_catalogo(str(FIXTURES))[885]
    lecturas = copy.deepcopy(SEPTICEMIA["lecturas"])
    for lectura in lecturas[1::2]:
        lectura["timestamp"] = lectura["timestamp"].replace("+00:00", ".123456+00:00")
    assert inferir(modelo, lecturas) is not None


@pytest.mark.parametrize(
    "lecturas",
    [
        "abc",
        [1],
        5,
        [{"variable": "temperatura_corporal", "valor": 39}],
        [{"timestamp": "x", "variable": "temperatura_corporal", "valor": 39}],
    ],
    ids=["texto", "no_dict", "no_iterable", "sin_timestamp", "timestamp_invalido"],
)
def test_lecturas_invalidas_no_dan_resultado_y_quedan_en_el_log(
    lecturas, caplog: pytest.LogCaptureFixture
) -> None:
    modelo = cargar_catalogo(str(FIXTURES))[885]
    with caplog.at_level(logging.WARNING):
        assert inferir(modelo, lecturas) is None
    assert "activo 885 sin resultado: lecturas inválidas" in caplog.text


def test_lecturas_no_aptas_dejan_el_motivo_en_el_log(caplog: pytest.LogCaptureFixture) -> None:
    modelo = cargar_catalogo(str(FIXTURES))[885]
    lecturas = [{**lectura, "apto_para_ia": False} for lectura in SEPTICEMIA["lecturas"]]
    with caplog.at_level(logging.WARNING):
        assert inferir(modelo, lecturas) is None
    assert "estado=NO_EJECUTA" in caplog.text and "descartadas_no_aptas_ia" in caplog.text


def test_linea_base_vencida_deja_el_motivo_en_el_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _fijar_fecha(monkeypatch, datetime(2027, 3, 1, tzinfo=UTC))
    modelo = cargar_catalogo(str(FIXTURES))[885]
    with caplog.at_level(logging.WARNING):
        assert inferir(modelo, SEPTICEMIA["lecturas"]) is None
    assert "sin línea base vigente" in caplog.text


def test_clase_no_evaluable_sin_temperatura_queda_en_el_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    modelo = cargar_catalogo(str(FIXTURES))[885]
    lecturas = [x for x in SEPTICEMIA["lecturas"] if x["variable"] != "temperatura_corporal"]
    contrato, decision = inferir(modelo, lecturas)
    assert contrato["clase_predicha"] == "SEPTICEMIA"
    with caplog.at_level(logging.WARNING):
        r = a_resultado_rf66(modelo, contrato, decision, id_paquete=1)
    assert r["contexto_incompleto"] is True
    assert "SEPTICEMIA predicha sin temperatura" in caplog.text


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
    def __init__(self, status: int = 201, cuerpo: dict | None = None) -> None:
        self.status, self.cuerpo, self.recibidos = status, {} if status < 400 else cuerpo, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.recibidos.append(
            (str(request.url), request.headers["authorization"], json.loads(request.content))
        )
        return httpx.Response(self.status, json=self.cuerpo)


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


@pytest.mark.parametrize(
    "backend",
    [
        _BackendFalso(403, {"detail": "Forbidden"}),
        _BackendFalso(404, {"detail": "Not Found"}),
        _BackendFalso(404),
    ],
    ids=["403_proxy", "404_ruta", "404_sin_json"],
)
def test_ruta_o_proxy_responde_503_para_que_reenvie(backend: _BackendFalso) -> None:
    r = _con_modelos(backend).post(
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


def test_activo_invalido_no_tumba_el_lote() -> None:
    r = _con_modelos(_BackendFalso()).post(
        "/v1/paquetes",
        headers=AUTH,
        json=_lote(
            {"id_paquetes_inferencia": 1, "id_activo_biologico": [885]},
            {
                "id_paquetes_inferencia": 2,
                "id_activo_biologico": 885,
                "lecturas": NORMAL["lecturas"],
            },
        ),
    )
    assert r.status_code == 200 and len(r.json()["resultados_registrados"]) == 1


@pytest.mark.parametrize(
    "backend",
    [_BackendFalso(422), _BackendFalso(404, {"error_code": "VERSION_MODELO_NO_ENCONTRADA"})],
    ids=["422", "404_de_negocio"],
)
def test_rechazo_del_backend_no_se_reintenta(backend: _BackendFalso) -> None:
    r = _con_modelos(backend).post(
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
