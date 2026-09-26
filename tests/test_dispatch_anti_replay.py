"""TC-M09-252: el flujo completo POST /v1/commands -> publish -> ACK, con dobles
para la BD y para Mosquitto.

Verifica lo que QA no podía ver desde afuera: el comando publicado lleva
`id_comando` y `emitido_en`, y un ACK reenviado (con el id de un comando viejo)
NO confirma el comando actual, que termina en NO_CONF.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime

import pytest

from app.config import get_settings
from app.mqtt import correlacion
from app.schemas import CommandRequest
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

    monkeypatch.setattr(dispatch, "async_session_factory", sesion_falsa)
    monkeypatch.setattr(dispatch.registry, "resolve_device_id", resolver_id)
    monkeypatch.setattr(dispatch.registry, "resolve_device_state", estado)
    correlacion._pending_acks.clear()
    yield
    correlacion._pending_acks.clear()
    get_settings.cache_clear()


def _peticion() -> CommandRequest:
    return CommandRequest(
        origen="configuracion", serial="S1", frecuencia_captura=60, intervalo_transmision=300
    )


def _ack(id_comando: str | None = None) -> dict:
    ack = {"tipo_mensaje": "ACK_CONFIGURACION", "resultado": "OK"}
    if id_comando is not None:
        ack["id_comando"] = id_comando
    return ack


async def test_el_comando_publicado_lleva_id_comando_y_emitido_en(monkeypatch) -> None:
    publicados: list[dict] = []

    async def publicar(serial, payload, qos=1):
        publicados.append(payload)
        # el dispositivo confirma devolviendo el id que recibió
        await ingest.ingest_status(serial, _ack(payload["id_comando"]))
        return f"sgpmp/{serial}/command"

    monkeypatch.setattr(dispatch.publisher, "publish_command", publicar)

    respuesta = await dispatch.dispatch_command(_peticion())

    assert respuesta.estado == "APLICADA"
    (payload,) = publicados
    assert len(payload["id_comando"]) == 32
    datetime.fromisoformat(payload["emitido_en"])  # ISO 8601 válido
    assert payload["frecuencia_captura"] == 60 and payload["intervalo_transmision"] == 300


async def test_cada_comando_lleva_un_id_distinto(monkeypatch) -> None:
    ids: list[str] = []

    async def publicar(serial, payload, qos=1):
        ids.append(payload["id_comando"])
        await ingest.ingest_status(serial, _ack(payload["id_comando"]))
        return "t"

    monkeypatch.setattr(dispatch.publisher, "publish_command", publicar)

    await dispatch.dispatch_command(_peticion())
    await dispatch.dispatch_command(_peticion())

    assert len(set(ids)) == 2


async def test_un_ack_reenviado_de_un_comando_viejo_no_confirma_el_actual(monkeypatch) -> None:
    """El caso de QA: capturar un ACK válido y reenviarlo. Con el id del comando
    anterior no debe resolver el comando en vuelo -> NO_CONF, no APLICADA."""

    async def publicar(serial, payload, qos=1):
        await ingest.ingest_status(serial, _ack("id-de-un-comando-viejo"))
        return "t"

    monkeypatch.setattr(dispatch.publisher, "publish_command", publicar)

    respuesta = await dispatch.dispatch_command(_peticion())

    assert respuesta.estado == "NO_CONF"


async def test_un_ack_sin_id_solo_confirma_si_no_se_exige_el_id(monkeypatch) -> None:
    async def publicar(serial, payload, qos=1):
        await ingest.ingest_status(serial, _ack())
        return "t"

    monkeypatch.setattr(dispatch.publisher, "publish_command", publicar)

    # compatibilidad (default): firmware que aún no devuelve el id sigue funcionando
    assert (await dispatch.dispatch_command(_peticion())).estado == "APLICADA"

    # con el id exigido, un ACK sin id (forjado o replay de firmware viejo) ya no resuelve
    monkeypatch.setenv("MQTT_ACK_REQUIERE_ID_COMANDO", "true")
    get_settings.cache_clear()
    assert (await dispatch.dispatch_command(_peticion())).estado == "NO_CONF"
