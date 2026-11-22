"""BRK-05 (D7 de AIOT-DEV-03): REGLA_COMPUESTA, CONFIG_MOTOR_IA y TIPO_MODELO_ASIGNADO
por el patrón de RF-17: `POST /v1/commands` publica en `command` con su `tipo_comando`
y espera el ACK de su mismo tipo, correlacionado por `(serial, id_comando)`."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from app.config import get_settings
from app.mqtt import correlacion
from app.schemas import (
    ComandoRequest,
    ConfigMotorCommandRequest,
    ReglaCompuestaCommandRequest,
    TipoModeloCommandRequest,
)
from app.services import dispatch, ingest


@pytest.fixture(autouse=True)
def _entorno(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("MQTT_ACK_TIMEOUT_SECONDS", "1")
    get_settings.cache_clear()

    @asynccontextmanager
    async def sesion_falsa():
        yield object()

    async def resolver_id(_session, _serial):
        return 1

    async def estado(_session, _serial):
        return "ACTIVO"

    async def mapa(_session):
        return {}

    async def conectado(_usuario):
        return False

    monkeypatch.setattr(dispatch, "async_session_factory", sesion_falsa)
    monkeypatch.setattr(dispatch.registry, "resolve_device_id", resolver_id)
    monkeypatch.setattr(dispatch.registry, "resolve_device_state", estado)
    monkeypatch.setattr(dispatch.registry, "mapa_dispositivos", mapa)
    monkeypatch.setattr(dispatch.credenciales_mqtt, "sin_conexion", conectado)
    correlacion._pending_acks.clear()
    yield
    correlacion._pending_acks.clear()
    get_settings.cache_clear()


REGLA = {
    "origen": "regla_compuesta",
    "serial": "EDGE-1",
    "id_regla_alerta": 12,
    "version_regla": 3,
    "regla": {"nombre": "THI alto", "condiciones": [{"variable": "temperatura", "mayor_que": 30}]},
}
MOTOR = {
    "origen": "config_motor",
    "serial": "EDGE-1",
    "tipo_modelo": "AVES",
    "config_version": 4,
    "configuracion": {"umbral_riesgo_alto": 0.8, "ventana_temporal_min": 15},
}
TIPO = {
    "origen": "tipo_modelo",
    "serial": "EDGE-1",
    "id_infraestructura": 2,
    "tipo_modelo_asignado": "PORCINOS",
}


def _respondedor(monkeypatch, ack_de):
    """Publica y contesta en el acto con `ack_de(payload)` (o no contesta si es None)."""
    publicados: list[dict] = []

    async def publicar(serial, payload, qos=1):
        publicados.append(payload)
        ack = ack_de(payload)
        if ack is not None:
            await ingest.ingest_status(serial, ack)
        return f"sgpmp/{serial}/command"

    monkeypatch.setattr(dispatch.publisher, "publish_command", publicar)
    return publicados


def test_la_union_discrimina_los_tres_origenes_nuevos() -> None:
    adaptador = TypeAdapter(ComandoRequest)
    assert isinstance(adaptador.validate_python(REGLA), ReglaCompuestaCommandRequest)
    assert isinstance(adaptador.validate_python(MOTOR), ConfigMotorCommandRequest)
    assert isinstance(adaptador.validate_python(TIPO), TipoModeloCommandRequest)
    with pytest.raises(ValidationError):
        adaptador.validate_python({**MOTOR, "config_version": 0})
    with pytest.raises(ValidationError):
        adaptador.validate_python({**REGLA, "regla": {}})


@pytest.mark.parametrize(
    "datos, tipo_comando, tipo_ack, campos",
    [
        (REGLA, "REGLA_COMPUESTA", "ACK_REGLA",
         {"id_regla_alerta": 12, "version_regla": 3, "es_activa": True,
          "regla": REGLA["regla"]}),
        (MOTOR, "CONFIG_MOTOR_IA", "ACK_CONFIG_MOTOR",
         {"tipo_modelo": "AVES", "config_version": 4, "configuracion": MOTOR["configuracion"]}),
        (TIPO, "TIPO_MODELO_ASIGNADO", "ACK_TIPO_MODELO",
         {"id_infraestructura": 2, "tipo_modelo_asignado": "PORCINOS"}),
    ],
    ids=["regla", "motor", "tipo-modelo"],
)
async def test_publica_su_tipo_comando_y_lo_confirma_su_ack(
    monkeypatch, datos, tipo_comando, tipo_ack, campos
) -> None:
    publicados = _respondedor(
        monkeypatch,
        lambda p: {"tipo_mensaje": tipo_ack, "resultado": "OK", "id_comando": p["id_comando"]},
    )

    respuesta = await dispatch.dispatch_command(TypeAdapter(ComandoRequest).validate_python(datos))

    assert respuesta.estado == "APLICADA"
    (payload,) = publicados
    assert payload["tipo_comando"] == tipo_comando
    assert {k: payload[k] for k in campos} == campos
    assert payload["id_comando"] and payload["emitido_en"]


async def test_config_motor_devuelve_el_config_version_del_nodo(monkeypatch) -> None:
    _respondedor(monkeypatch, lambda p: {"tipo_mensaje": "ACK_CONFIG_MOTOR", "resultado": "OK",
                                         "id_comando": p["id_comando"], "config_version": 3})

    respuesta = await dispatch.dispatch_command(ConfigMotorCommandRequest(**MOTOR))

    # 3 != 4 del servidor: el backend lo marca DESINCRONIZADO (RF-65 E10), no el broker.
    assert (respuesta.estado, respuesta.config_version) == ("APLICADA", 3)


@pytest.mark.parametrize("valor", [None, "4", True])
async def test_config_version_que_no_es_entero_no_se_inventa(monkeypatch, valor) -> None:
    _respondedor(monkeypatch, lambda p: {"tipo_mensaje": "ACK_CONFIG_MOTOR", "resultado": "OK",
                                         "id_comando": p["id_comando"], "config_version": valor})
    respuesta = await dispatch.dispatch_command(ConfigMotorCommandRequest(**MOTOR))
    assert (respuesta.estado, respuesta.config_version) == ("APLICADA", None)


async def test_un_ack_de_otro_tipo_no_confirma(monkeypatch) -> None:
    _respondedor(monkeypatch, lambda p: {"tipo_mensaje": "ACK_UMBRAL", "resultado": "OK",
                                         "id_comando": p["id_comando"]})
    respuesta = await dispatch.dispatch_command(ReglaCompuestaCommandRequest(**REGLA))
    assert respuesta.estado == "NO_CONF"


async def test_sin_ack_queda_no_conf_y_un_edge_apagado_pendiente(monkeypatch) -> None:
    _respondedor(monkeypatch, lambda p: None)
    assert (await dispatch.dispatch_command(TipoModeloCommandRequest(**TIPO))).estado == "NO_CONF"

    async def desconectado(_usuario):
        return True

    monkeypatch.setattr(dispatch.credenciales_mqtt, "sin_conexion", desconectado)
    respuesta = await dispatch.dispatch_command(ConfigMotorCommandRequest(**MOTOR))
    assert (respuesta.estado, respuesta.config_version) == ("PENDIENTE", None)


def test_post_commands_acepta_config_motor_y_devuelve_config_version(monkeypatch) -> None:
    from app.api import dependencies
    from app.api.routes import commands
    from app.main import app

    async def sin_token():
        return None

    async def despachar(request):
        assert isinstance(request, ConfigMotorCommandRequest)
        return dispatch.CommandResponse(serial=request.serial, estado="APLICADA", mensaje="ok",
                                        config_version=4)

    monkeypatch.setattr(commands, "dispatch_command", despachar)
    app.dependency_overrides[dependencies.require_api_token] = sin_token
    try:
        r = TestClient(app).post("/v1/commands", json=MOTOR)
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200, r.text
    assert r.json()["config_version"] == 4
