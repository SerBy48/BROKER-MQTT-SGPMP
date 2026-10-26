"""BRK-03/04/08 (D4, D5 y D6 de AIOT-DEV-03): los topics `vision`, `evento` y
`ota/<evento>` se reenvían a su endpoint interno del backend, con el serial del topic."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from app.backend.client import RespuestaBackend
from app.config import get_settings
from app.mqtt import handlers
from app.mqtt.handlers import TopicIot, parsear_topic
from app.services import reenvio, reenvio_nodo

DISPOSITIVOS = {"EDGE-1": 12}
SENSORES = {12: {"dht22_ambiente": 342, "dht22_humedad": 343, "ds18b20_agua": 344}}


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


@pytest.fixture(autouse=True)
def consultas(monkeypatch):
    """modulo9 falso: registra cada consulta que hace la ruta de eventos."""
    lista: list[tuple[str, object]] = []

    @asynccontextmanager
    async def sesion_falsa():
        yield object()

    async def resolve_device_id(_session, serial):
        lista.append(("dispositivo", serial))
        return DISPOSITIVOS.get(serial)

    async def sensores_por_nombre(_session, device_id):
        lista.append(("sensores", device_id))
        return SENSORES.get(device_id, {})

    monkeypatch.setattr(reenvio_nodo, "async_session_factory", sesion_falsa)
    monkeypatch.setattr(reenvio_nodo.registry, "resolve_device_id", resolve_device_id)
    monkeypatch.setattr(reenvio_nodo.registry, "sensores_por_nombre", sensores_por_nombre)
    return lista


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


def _variable(tipo: str, sensor: str | None) -> dict:
    return {"tipo_variable": tipo, "valor": 1.0, "unidad": "u",
            "timestamp_captura": "2026-10-26T14:30:00Z", "sensor": sensor,
            "serial": "EDGE-1"}


async def test_un_nodo_no_puede_reportar_por_otro_serial(enviados, caplog) -> None:
    evento = {"serial": "EDGE-2", "clasificacion_rf55": "NORMAL",
              "variables_involucradas": [_variable("TEMPERATURA_AMBIENTAL", "dht22_ambiente")]}
    await _publicar("sgpmp/EDGE-1/evento", evento)
    [(ruta, cuerpo)] = enviados
    assert ruta == "/iot/eventos-edge"
    assert cuerpo["serial"] == "EDGE-1"
    assert "se usa el del topic" in caplog.text


async def test_evento_multivariable_resuelve_un_sensor_por_variable(enviados, consultas) -> None:
    metadata = {"id_evento": "edge-evt-1", "regla_aplicada": "ESTRES_TERMICO",
                "valores_derivados": {"THI": 81.3}}
    evento = {"clasificacion_rf55": "DESVIACION_COMPUESTA", "severidad": "MODERADO",
              "variables_involucradas": [_variable("HUMEDAD_RELATIVA", "dht22_humedad"),
                                         _variable("TEMPERATURA_AMBIENTAL", "dht22_ambiente"),
                                         _variable("TEMPERATURA_AGUA", "ds18b20_agua")],
              "metadata_edge": metadata}
    await _publicar("sgpmp/EDGE-1/evento", evento)
    [(_, cuerpo)] = enviados
    assert cuerpo["device_id"] == 12
    assert cuerpo["sensor_id"] == 343  # la primaria: la primera de variables_involucradas
    assert cuerpo["variables_involucradas"] == evento["variables_involucradas"]
    assert cuerpo["metadata_edge"] == {
        **metadata,
        "sensores_por_variable": {"HUMEDAD_RELATIVA": 343, "TEMPERATURA_AMBIENTAL": 342,
                                  "TEMPERATURA_AGUA": 344},
        "device_id_resuelto": 12,
    }
    # Una consulta de sensores por evento, no una por variable.
    assert consultas == [("dispositivo", "EDGE-1"), ("sensores", 12)]


async def test_serial_desconocido_se_descarta_sin_llamar_al_backend(enviados, caplog) -> None:
    evento = {"clasificacion_rf55": "NORMAL",
              "variables_involucradas": [_variable("TEMPERATURA_AMBIENTAL", "dht22_ambiente")]}
    await _publicar("sgpmp/EDGE-9/evento", evento)
    assert enviados == []
    assert "EDGE-9 descartado" in caplog.text


async def test_variable_sin_sensor_queda_en_none_y_el_evento_se_reenvia(enviados) -> None:
    evento = {"clasificacion_rf55": "DESVIACION_COMPUESTA",
              "variables_involucradas": [_variable("TEMPERATURA_AMBIENTAL", "dht22_ambiente"),
                                         _variable("HUMEDAD_RELATIVA", "inexistente"),
                                         _variable("PH", None)]}
    await _publicar("sgpmp/EDGE-1/evento", evento)
    [(_, cuerpo)] = enviados
    assert cuerpo["sensor_id"] == 342
    assert cuerpo["metadata_edge"]["sensores_por_variable"] == {
        "TEMPERATURA_AMBIENTAL": 342, "HUMEDAD_RELATIVA": None, "PH": None}


async def test_primaria_sin_sensor_usa_la_primera_resuelta(enviados) -> None:
    evento = {"clasificacion_rf55": "DESVIACION_COMPUESTA",
              "variables_involucradas": [_variable("HUMEDAD_RELATIVA", "inexistente"),
                                         _variable("TEMPERATURA_AMBIENTAL", "dht22_ambiente")]}
    await _publicar("sgpmp/EDGE-1/evento", evento)
    [(_, cuerpo)] = enviados
    assert cuerpo["sensor_id"] == 342


async def test_sin_ningun_sensor_resuelto_no_se_reenvia(enviados, caplog) -> None:
    evento = {"clasificacion_rf55": "DESVIACION_SIMPLE",
              "variables_involucradas": [_variable("HUMEDAD_RELATIVA", "inexistente")]}
    await _publicar("sgpmp/EDGE-1/evento", evento)
    assert enviados == []
    assert "ninguna variable resolvió su sensor" in caplog.text


async def test_metadata_edge_gana_ante_colision(enviados, caplog) -> None:
    metadata = {"device_id_resuelto": "del-edge", "version_firmware": "0.3.0"}
    evento = {"clasificacion_rf55": "NORMAL", "metadata_edge": metadata,
              "variables_involucradas": [_variable("TEMPERATURA_AMBIENTAL", "dht22_ambiente")]}
    await _publicar("sgpmp/EDGE-1/evento", evento)
    [(_, cuerpo)] = enviados
    assert cuerpo["metadata_edge"]["device_id_resuelto"] == "del-edge"
    assert cuerpo["metadata_edge"]["version_firmware"] == "0.3.0"
    assert cuerpo["metadata_edge"]["sensores_por_variable"] == {"TEMPERATURA_AMBIENTAL": 342}
    assert "se conserva lo del Edge" in caplog.text
    assert evento["metadata_edge"] == metadata  # el dict del Edge no se muta


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
