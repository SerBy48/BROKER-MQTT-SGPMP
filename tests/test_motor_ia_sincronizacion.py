# | Fecha      | Nombre   | Que se hizo                                                     |
# |------------|----------|-----------------------------------------------------------------|
# | 2026-11-02 | Arekkazu | RF-66/RF-69: el motor arma su catálogo desde el backend          |
"""Un animal individual nuevo se atiende solo: catálogo, copia, artefacto y calibración."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

pytest.importorskip("onnxruntime")
pytest.importorskip("pandas")

from fastapi.testclient import TestClient  # noqa: E402

from motor_ia.main import MotorIASettings, crear_app  # noqa: E402
from motor_ia.sincronizacion import (  # noqa: E402
    SincronizadorCatalogo,
    calcular_linea_base,
    cargar_plantillas,
)

FIXTURES = Path(__file__).parent / "fixtures" / "et02_bov001"
ONNX = (FIXTURES / "BOV-001.onnx").read_bytes()
HASH = hashlib.sha256(ONNX).hexdigest()
SEPTICEMIA = json.loads((FIXTURES / "referencia.json").read_text(encoding="utf-8"))["casos"][1]
VERSION = {
    "id_version_modelo": 9,
    "nombre_version": "ESPECIES_GRANDES_4_261025_ed219d41",
    "hash_artefacto_sha256": HASH,
    "fuente_datos": "SINTETICA",
}
TOKEN = "token-motor"
BACKEND = "https://api.arekkazu.top/api-sgpmp-dev"
AHORA = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)
PLANTILLA = cargar_plantillas()["MODELO_ESPECIES_GRANDES"]


def _lecturas(horas: float, fc: float = 80.0) -> list[dict]:
    """Una lectura de cada variable cada 5 min durante ``horas``, todas aptas."""
    filas = []
    for i in range(int(horas * 12) + 1):
        ts = (AHORA - timedelta(minutes=5 * i)).isoformat()
        for var, valor in (("frecuencia_cardiaca", fc + i % 3), ("temperatura_corporal", 38.7)):
            filas.append({"timestamp": ts, "variable": var, "valor": valor, "apto_para_ia": True})
    return filas


class _Cliente:
    def __init__(self, animales, lecturas=(), onnx=ONNX):
        self.animales, self._lecturas, self._onnx = animales, list(lecturas), onnx
        self.copias, self.descargas = [], 0

    def catalogo(self):
        if self.animales is None:
            raise httpx.ConnectError("backend caído")
        return self.animales

    def asignar_version(self, id_activo):
        self.copias.append(id_activo)
        return VERSION

    def artefacto(self, id_version):
        self.descargas += 1
        return self._onnx

    def lecturas(self, id_activo, horas):
        return self._lecturas


# ── Línea base propia ────────────────────────────────────────────────────


def test_linea_base_propia_con_lecturas_que_cubren_las_horas():
    lb = calcular_linea_base("ACTIVO-4", PLANTILLA, _lecturas(2), 2)
    assert lb.id_animal == "ACTIVO-4" and lb.especie == "BOVINO" and lb.estado == "COMPLETADA"
    assert lb.mu["frecuencia_cardiaca"] == pytest.approx(81, abs=0.1)
    assert lb.mu["temperatura_corporal"] == pytest.approx(38.7)
    # Sin variación propia, el piso es la mitad de la desviación de referencia.
    assert lb.sigma["temperatura_corporal"] == PLANTILLA.sigma["temperatura_corporal"] / 2
    # Sin sensor de actividad: queda la de referencia.
    assert lb.mu["actividad_indice"] == PLANTILLA.mu["actividad_indice"]


def test_sin_lecturas_suficientes_no_hay_linea_base_propia():
    assert calcular_linea_base("ACTIVO-4", PLANTILLA, _lecturas(1), 2) is None
    no_aptas = [dict(f, apto_para_ia=False) for f in _lecturas(2)]
    assert calcular_linea_base("ACTIVO-4", PLANTILLA, no_aptas, 2) is None


# ── Sincronización ───────────────────────────────────────────────────────


def test_animal_nuevo_recibe_version_y_calibra_con_la_referencia():
    cliente = _Cliente(
        [{"id_activo_biologico": 4, "tipo_modelo": "MODELO_ESPECIES_GRANDES", "version": None}]
    )
    s = SincronizadorCatalogo(cliente, 2)
    s.sincronizar(AHORA)
    modelo = s.modelos[4]
    assert cliente.copias == [4] and modelo.id_version_modelo == 9 and modelo.calibrando
    assert modelo.linea_base.id_animal == "ACTIVO-4" == modelo.artefacto.metadatos["id_animal"]
    assert modelo.linea_base.mu == PLANTILLA.mu

    # Sin cambios, conserva el mismo modelo (y su sesión ONNX) y no descarga otra vez.
    cliente.animales[0]["version"] = VERSION
    s.sincronizar(AHORA + timedelta(minutes=1))
    assert s.modelos[4] is modelo and cliente.descargas == 1


def test_con_lecturas_suficientes_pasa_a_linea_base_propia():
    cliente = _Cliente(
        [{"id_activo_biologico": 4, "tipo_modelo": "MODELO_ESPECIES_GRANDES", "version": VERSION}],
        lecturas=_lecturas(2),
    )
    s = SincronizadorCatalogo(cliente, 2)
    s.sincronizar(AHORA)
    assert not s.modelos[4].calibrando
    assert s.modelos[4].linea_base.mu["frecuencia_cardiaca"] == pytest.approx(81, abs=0.1)


def test_artefacto_alterado_no_se_usa():
    cliente = _Cliente(
        [{"id_activo_biologico": 4, "tipo_modelo": "MODELO_ESPECIES_GRANDES", "version": VERSION}],
        onnx=b"otro",
    )
    s = SincronizadorCatalogo(cliente, 2)
    s.sincronizar(AHORA)
    assert s.modelos == {}


def test_backend_caido_conserva_el_catalogo_anterior():
    cliente = _Cliente(
        [{"id_activo_biologico": 4, "tipo_modelo": "MODELO_ESPECIES_GRANDES", "version": VERSION}]
    )
    s = SincronizadorCatalogo(cliente, 2)
    s.sincronizar(AHORA)
    antes = s.modelos
    cliente.animales = None
    with pytest.raises(httpx.ConnectError):
        s.sincronizar(AHORA + timedelta(minutes=1))
    assert s.modelos is antes


# ── Flujo completo: el motor arma el catálogo por HTTP e infiere ─────────


def test_motor_atiende_un_animal_nuevo_sin_catalogo_json():
    pedidos, resultados = [], []

    def backend(request: httpx.Request) -> httpx.Response:
        ruta = request.url.path.removeprefix("/api-sgpmp-dev")
        pedidos.append((request.method, ruta))
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if ruta == "/prediccion/motor/catalogo":
            animal = {
                "id_activo_biologico": 4,
                "tipo_modelo": "MODELO_ESPECIES_GRANDES",
                "version": None,
            }
            return httpx.Response(200, json={"animales": [animal]})
        if ruta == "/prediccion/motor/activos/4/version":
            return httpx.Response(200, json=VERSION)
        if ruta == "/prediccion/motor/versiones/9/artefacto":
            return httpx.Response(200, content=ONNX)
        if ruta == "/prediccion/motor/activos/4/lecturas":
            return httpx.Response(200, json={"lecturas": []})
        if ruta == "/prediccion/inferencias":
            resultados.append(json.loads(request.content))
            return httpx.Response(201, json={})
        return httpx.Response(404)

    cliente = TestClient(
        crear_app(
            MotorIASettings(
                iot_inferencia_m04_token=TOKEN,
                backend_url=BACKEND,
                motor_ia_sincronizacion_s=3600,
                motor_ia_calibracion_horas=2,
            ),
            transport_backend=httpx.MockTransport(backend),
        )
    )
    for _ in range(100):
        salud = cliente.get("/v1/healthz").json()
        if salud["sincronizacion"]["ultima"]:
            break
        time.sleep(0.05)
    assert salud["modelos_cargados"] == 1 and salud["sincronizacion"]["calibrando"] == [4]
    assert ("POST", "/prediccion/motor/activos/4/version") in pedidos

    r = cliente.post(
        "/v1/paquetes",
        headers={"Authorization": f"Bearer {TOKEN}"},
        json={
            "paquetes": [
                {
                    "id_paquetes_inferencia": 1,
                    "id_activo_biologico": 4,
                    "lecturas": SEPTICEMIA["lecturas"],
                }
            ]
        },
    )
    assert r.status_code == 200, r.text
    (resultado,) = resultados
    assert (resultado["id_activo_biologico"], resultado["id_version_modelo"]) == (4, 9)
    assert resultado["nivel_riesgo"] == 3 and resultado["contexto_incompleto"] is True


def test_sincronizacion_sin_backend_url_no_arranca():
    with pytest.raises(RuntimeError, match="BACKEND_URL"):
        crear_app(MotorIASettings(iot_inferencia_m04_token=TOKEN, motor_ia_sincronizacion_s=60))
