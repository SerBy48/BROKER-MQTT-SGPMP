"""BRK-03/04/08 (D4, D5 y D6 de AIOT-DEV-03): los topics `vision`, `evento` y
`ota/<evento>` se reenvían a su endpoint interno del backend, con el serial del topic."""

from __future__ import annotations

import asyncio
import json

import pytest

from app.backend.client import RespuestaBackend
from app.config import get_settings
from app.mqtt import handlers
from app.mqtt.handlers import TopicIot, parsear_topic
from app.services import reenvio


@pytest.fixture(autouse=True)
def enviados(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    get_settings.cache_clear()
    lista: list[tuple[str, dict]] = []

    async def enviar(ruta, cuerpo):
        lista.append((ruta, cuerpo))
        return RespuestaBackend(ok=True, status=201)

    monkeypatch.setattr(reenvio.backend_client, "enviar", enviar)
    yield lista
    get_settings.cache_clear()


async def _publicar(topic: str, cuerpo: dict) -> None:
    await handlers.handle_message(topic, json.dumps(cuerpo).encode())
    await asyncio.gather(*reenvio._tareas)


@pytest.mark.parametrize("topic, esperado", [
    ("sgpmp/EDGE-1/telemetry", TopicIot("EDGE-1", "telemetry", None)),
    ("sgpmp/EDGE-1/ota/activated", TopicIot("EDGE-1", "ota", "activated")),
    ("sgpmp/EDGE-1", None),
    ("otro/EDGE-1/telemetry", None),
    ("sgpmp/EDGE-1/ota/a/b", None),
    ("sgpmp//telemetry", None),
])
def test_el_serial_va_despues_del_prefijo(topic, esperado) -> None:
    assert parsear_topic(topic) == esperado


async def test_vision_se_reenvia_con_el_sobre_de_d4(enviados) -> None:
    sobre = {"area_id": 2, "ventana_inicio": "2026-10-17T10:00:00-05:00",
             "ventana_fin": "2026-10-17T10:05:00-05:00",
             "componentes": {"densidad_actividad": 0.4},
             "calidad": {"cobertura_ventana": 0.9, "n_tracks": 12},
             "version_cascada": {"detector": "v3"}}
    await _publicar("sgpmp/CAM-1/vision", sobre)
    assert enviados == [("/iot/telemetria/vision", {**sobre, "serial": "CAM-1"})]


async def test_un_nodo_no_puede_reportar_por_otro_serial(enviados, caplog) -> None:
    await _publicar("sgpmp/EDGE-1/evento", {"serial": "EDGE-2", "clasificacion_rf55": "NORMAL"})
    assert enviados == [("/iot/eventos-edge", {"serial": "EDGE-1", "clasificacion_rf55": "NORMAL"})]
    assert "se usa el del topic" in caplog.text


async def test_reporte_ota_separa_identidad_y_detalle(enviados) -> None:
    await _publicar("sgpmp/EDGE-1/ota/rollback", {"id_despliegue_ota": 31, "componente": "DETECTOR",
                                                   "motivo_fallo": "hash distinto"})
    assert enviados == [("/prediccion/ota/reportes", {
        "serial": "EDGE-1", "id_despliegue_ota": 31, "componente": "DETECTOR",
        "evento": "rollback", "detalle": {"motivo_fallo": "hash distinto"},
    })]


@pytest.mark.parametrize("topic", ["sgpmp/EDGE-1/ota/notify", "sgpmp/EDGE-1/ota/inventado"])
async def test_notify_propio_y_eventos_fuera_del_contrato_no_se_reenvian(enviados, topic) -> None:
    await _publicar(topic, {"id_despliegue_ota": 1})
    assert enviados == []


async def test_payload_que_no_es_objeto_se_descarta(enviados) -> None:
    await handlers.handle_message("sgpmp/EDGE-1/vision", b"[1, 2]")
    assert enviados == []
