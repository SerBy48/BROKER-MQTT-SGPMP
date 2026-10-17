"""RF-17 (INC-M09-104-G29): propagación de un umbral ambiental a un Gateway Edge.

`POST /v1/commands` con `origen: "umbral"` publica en el mismo topic `command`
del Gateway, con `tipo_comando: "UMBRAL_AMBIENTAL"`, y espera un ACK
`ACK_UMBRAL` que devuelva el `id_comando`. Varios umbrales en vuelo hacia el
mismo Gateway se confirman cada uno con su propio ACK.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from app.config import get_settings
from app.mqtt import correlacion
from app.schemas import ComandoRequest, CommandRequest, CommandResponse, UmbralCommandRequest
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


def _datos_umbral(id_umbral: int = 7) -> dict:
    return {
        "origen": "umbral",
        "serial": "EDGE-1",
        "id_umbral_ambiental": id_umbral,
        "version": "2026-10-05T12:00:00+00:00",
        "variable": "temperatura",
        "unidad": "°C",
        "valor_min": 18,
        "valor_max": 32,
        "niveles": [
            {"nivel": "normal", "limite_inferior": 22, "limite_superior": 28},
            {"nivel": "precaucion", "limite_inferior": 20, "limite_superior": 30},
            {"nivel": "critico", "limite_inferior": 18, "limite_superior": 32},
        ],
    }


def _umbral(id_umbral: int = 7) -> UmbralCommandRequest:
    return UmbralCommandRequest(**_datos_umbral(id_umbral))


def _ack(id_comando: str | None, tipo: str = "ACK_UMBRAL") -> dict:
    ack = {"tipo_mensaje": tipo, "resultado": "OK"}
    if id_comando is not None:
        ack["id_comando"] = id_comando
    return ack


def test_la_union_discrimina_por_origen() -> None:
    adaptador = TypeAdapter(ComandoRequest)
    umbral = adaptador.validate_python(_datos_umbral())
    configuracion = adaptador.validate_python(
        {
            "origen": "configuracion",
            "serial": "S1",
            "frecuencia_captura": 1,
            "intervalo_transmision": 1,
        }
    )

    assert isinstance(umbral, UmbralCommandRequest)
    assert isinstance(configuracion, CommandRequest)
    with pytest.raises(ValidationError):
        adaptador.validate_python({"origen": "umbral", "serial": "EDGE-1"})


async def test_el_umbral_se_publica_con_sus_valores_y_niveles(monkeypatch) -> None:
    publicados: list[tuple[str, dict]] = []

    async def publicar(serial, payload, qos=1):
        publicados.append((serial, payload))
        await ingest.ingest_status(serial, _ack(payload["id_comando"]))
        return f"sgpmp/{serial}/command"

    monkeypatch.setattr(dispatch.publisher, "publish_command", publicar)

    respuesta = await dispatch.dispatch_command(_umbral())

    assert respuesta.estado == "APLICADA"
    assert respuesta.topic == "sgpmp/EDGE-1/command"
    ((serial, payload),) = publicados
    assert serial == "EDGE-1"
    assert payload["tipo_comando"] == "UMBRAL_AMBIENTAL"
    assert payload["id_umbral_ambiental"] == 7
    assert payload["version"] == "2026-10-05T12:00:00+00:00"
    assert payload["variable"] == "temperatura" and payload["unidad"] == "°C"
    assert payload["valor_min"] == 18 and payload["valor_max"] == 32
    assert payload["niveles"][0] == {
        "nivel": "normal",
        "limite_inferior": 22,
        "limite_superior": 28,
    }
    assert [n["nivel"] for n in payload["niveles"]] == ["normal", "precaucion", "critico"]
    assert "frecuencia_captura" not in payload


async def test_el_comando_de_configuracion_se_publica_igual_que_antes(monkeypatch) -> None:
    publicados: list[dict] = []

    async def publicar(serial, payload, qos=1):
        publicados.append(payload)
        await ingest.ingest_status(serial, _ack(payload["id_comando"], "ACK_CONFIGURACION"))
        return "t"

    monkeypatch.setattr(dispatch.publisher, "publish_command", publicar)

    peticion = CommandRequest(
        origen="configuracion", serial="S1", frecuencia_captura=60, intervalo_transmision=300
    )
    assert (await dispatch.dispatch_command(peticion)).estado == "APLICADA"
    assert set(publicados[0]) == {
        "id_comando",
        "emitido_en",
        "frecuencia_captura",
        "intervalo_transmision",
    }


async def test_un_ack_de_configuracion_no_confirma_un_umbral(monkeypatch) -> None:
    async def publicar(serial, payload, qos=1):
        await ingest.ingest_status(serial, _ack(payload["id_comando"], "ACK_CONFIGURACION"))
        return "t"

    monkeypatch.setattr(dispatch.publisher, "publish_command", publicar)

    assert (await dispatch.dispatch_command(_umbral())).estado == "NO_CONF"


async def test_sin_ack_del_edge_el_umbral_queda_no_conf(monkeypatch) -> None:
    async def publicar(serial, payload, qos=1):
        return "t"

    monkeypatch.setattr(dispatch.publisher, "publish_command", publicar)

    assert (await dispatch.dispatch_command(_umbral())).estado == "NO_CONF"


async def test_varios_umbrales_en_vuelo_al_mismo_edge_se_confirman_cada_uno(monkeypatch) -> None:
    """El caso que antes fallaba: la segunda espera pisaba a la primera."""
    publicados: list[str] = []
    todos_publicados = asyncio.Event()

    async def publicar(serial, payload, qos=1):
        publicados.append(payload["id_comando"])
        if len(publicados) == 3:
            todos_publicados.set()
        return "t"

    monkeypatch.setattr(dispatch.publisher, "publish_command", publicar)

    async def edge_confirma_en_orden_inverso():
        await todos_publicados.wait()
        for id_comando in reversed(publicados):
            await ingest.ingest_status("EDGE-1", _ack(id_comando))

    respuestas, _ = await asyncio.gather(
        asyncio.gather(*(dispatch.dispatch_command(_umbral(i)) for i in (1, 2, 3))),
        edge_confirma_en_orden_inverso(),
    )

    assert [r.estado for r in respuestas] == ["APLICADA"] * 3
    assert correlacion._pending_acks == {}


def test_post_commands_acepta_el_origen_umbral(monkeypatch) -> None:
    from app.api.dependencies import require_api_token
    from app.main import app

    recibidos: list = []

    async def despachar(request):
        recibidos.append(request)
        return CommandResponse(serial=request.serial, estado="APLICADA", mensaje="ok")

    monkeypatch.setattr("app.api.routes.commands.dispatch_command", despachar)
    app.dependency_overrides[require_api_token] = lambda: None
    try:
        respuesta = TestClient(app).post("/v1/commands", json=_datos_umbral())
    finally:
        app.dependency_overrides.clear()

    assert respuesta.status_code == 200, respuesta.text
    assert respuesta.json()["estado"] == "APLICADA"
    assert isinstance(recibidos[0], UmbralCommandRequest)


async def test_el_comando_de_camara_publica_solo_fps(monkeypatch) -> None:
    publicados: list[dict] = []

    async def publicar(serial, payload, qos=1):
        publicados.append(payload)
        await ingest.ingest_status(serial, _ack(payload["id_comando"], "ACK_CONFIGURACION"))
        return "t"

    monkeypatch.setattr(dispatch.publisher, "publish_command", publicar)

    peticion = CommandRequest(origen="configuracion", serial="CAM-1", fps=15)
    assert (await dispatch.dispatch_command(peticion)).estado == "APLICADA"
    assert set(publicados[0]) == {"id_comando", "emitido_en", "fps"}
    assert publicados[0]["fps"] == 15


@pytest.mark.parametrize(
    "campos",
    [
        {},
        {"fps": 15, "frecuencia_captura": 60, "intervalo_transmision": 300},
        {"frecuencia_captura": 60},
        {"fps": 0},
        {"fps": 61},
    ],
)
def test_configuracion_exige_un_solo_juego_de_parametros(campos) -> None:
    with pytest.raises(ValidationError):
        CommandRequest(origen="configuracion", serial="S1", **campos)
