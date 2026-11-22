"""BRK-06 (D10 de AIOT-DEV-03): un `status` con `tipo_mensaje = EVENTO_BUFFER` se reenvía
a `POST /iot/eventos-buffer` del backend, sin frenar el bucle de mensajes MQTT."""

from __future__ import annotations

import asyncio

import pytest

from app.backend.client import RespuestaBackend
from app.config import get_settings
from app.mqtt import correlacion
from app.services import ingest, reenvio

AVISO = {
    "tipo_mensaje": "EVENTO_BUFFER",
    "evento": "BUFFER_OVERFLOW",
    "porcentaje_ocupacion": 100.0,
    "politica_aplicada": "POLITICA_RECHAZO",
    "cantidad_datos_afectados": 12,
    "timestamp_evento": "2026-10-17T10:00:00-05:00",
}


@pytest.fixture(autouse=True)
def _entorno(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_se_reenvia_al_backend_con_el_serial_del_topic(monkeypatch) -> None:
    enviados: list[tuple[str, dict]] = []

    async def enviar(ruta, cuerpo):
        enviados.append((ruta, cuerpo))
        return RespuestaBackend(ok=True, status=201)

    monkeypatch.setattr(reenvio.backend_client, "enviar", enviar)

    await ingest.ingest_status("EDGE-1", dict(AVISO))
    await asyncio.gather(*reenvio._tareas)

    assert enviados == [("/iot/eventos-buffer", {
        "serial": "EDGE-1",
        "evento": "BUFFER_OVERFLOW",
        "porcentaje_ocupacion": 100.0,
        "politica_aplicada": "POLITICA_RECHAZO",
        "cantidad_datos_afectados": 12,
        "timestamp_evento": "2026-10-17T10:00:00-05:00",
    })]


async def test_un_backend_lento_no_frena_el_procesamiento_de_status(monkeypatch) -> None:
    liberar = asyncio.Event()

    async def lento(ruta, cuerpo):
        await liberar.wait()
        return RespuestaBackend(ok=True, status=201)

    monkeypatch.setattr(reenvio.backend_client, "enviar", lento)

    await asyncio.wait_for(ingest.ingest_status("EDGE-1", dict(AVISO)), timeout=0.5)
    assert len(reenvio._tareas) == 1  # sigue en vuelo, ingest_status ya volvió
    liberar.set()
    await asyncio.gather(*reenvio._tareas)
    assert reenvio._tareas == set()


async def test_si_el_backend_falla_queda_en_el_log(monkeypatch, caplog) -> None:
    async def caido(ruta, cuerpo):
        return RespuestaBackend(ok=False, error="backend no disponible: ConnectError")

    monkeypatch.setattr(reenvio.backend_client, "enviar", caido)
    await ingest.ingest_status("EDGE-1", dict(AVISO))
    await asyncio.gather(*reenvio._tareas)
    assert "EVENTO_BUFFER de EDGE-1 NO se reenvió" in caplog.text


async def test_un_evento_buffer_no_resuelve_esperas_de_ack(monkeypatch) -> None:
    async def enviar(ruta, cuerpo):
        return RespuestaBackend(ok=True, status=201)

    monkeypatch.setattr(reenvio.backend_client, "enviar", enviar)
    futuro = correlacion.crear_espera("EDGE-1", "c1", correlacion.ACK_UMBRAL)
    try:
        await ingest.ingest_status("EDGE-1", {**AVISO, "resultado": "OK", "id_comando": "c1"})
        await asyncio.gather(*reenvio._tareas)
        assert not futuro.done()
    finally:
        correlacion.limpiar_espera("EDGE-1", "c1")
