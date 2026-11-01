# | Fecha      | Nombre   | Que se hizo |
# |------------|----------|-------------|
# | 2026-11-01 | Arekkazu | RF-23/RF-22: ERROR_SENSOR cuando la lectura no identifica su sensor |
"""BRK-01 (D2 de AIOT-DEV-03): `transmisiones_mqtt` refleja el resultado real de
fn_ingesta_telemetria y el nodo recibe un aviso para liberar o retener su buffer."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from app.config import get_settings
from app.core.errors import MqttNotConnectedError
from app.services import ingest

LECTURA = {
    "id_mensaje": "buf-77",
    "variable": "TEMPERATURA_AMBIENTAL",
    "sensor": "dht22",
    "valor_crudo": 24.5,
    "unidad": "°C",
    "timestamp_captura": "2026-10-17T10:00:00-05:00",
}


@pytest.mark.parametrize(
    "fila, estado, accion, codigo",
    [
        ({"id_telemetria": 5, "estado_calidad": "LECTURA_VALIDA", "mensaje": "OK"},
         "EXITO", "LIBERAR", "LECTURA_VALIDA"),
        ({"id_telemetria": 6, "estado_calidad": "FUERA_DE_RANGO", "mensaje": "OK"},
         "EXITO", "LIBERAR", "FUERA_DE_RANGO"),
        ({"id_telemetria": None, "estado_calidad": None,
          "mensaje": "ERROR_TIEMPO: timestamp_captura futuro."},
         "FALLIDO", "LIBERAR", "ERROR_TIEMPO"),
        ({"id_telemetria": None, "estado_calidad": None,
          "mensaje": "ERROR_DUPLICADO: Registro ya existe."},
         "FALLIDO", "LIBERAR", "ERROR_DUPLICADO"),
        ({"id_telemetria": None, "estado_calidad": None,
          "mensaje": "ERROR_NUEVO: algo que el contrato no conoce"},
         "FALLIDO", "RETENER", "ERROR_NUEVO"),
    ],
    ids=["valida", "fuera-de-rango", "tiempo", "duplicado", "codigo-desconocido"],
)
def test_contrato_de_retorno_de_la_funcion(fila, estado, accion, codigo) -> None:
    r = ingest.clasificar(fila)
    assert (r.estado, r.accion_buffer, r.codigo) == (estado, accion, codigo)


class _Bd:
    """Dobles de registry/telemetry_repo; registra transmisiones y avisos publicados."""

    def __init__(self, monkeypatch, fila=None, falla=None, device_id=1) -> None:
        self.transmisiones: list[dict] = []
        self.avisos: list[tuple[str, dict]] = []
        self.commits = 0

        bd = self

        class Sesion:
            async def commit(self) -> None:
                bd.commits += 1

        @asynccontextmanager
        async def sesion():
            yield Sesion()

        async def resolve_device_id(_s, _serial):
            return device_id

        async def resolve_id(*_a):
            return 3

        async def ingesta(_s, **_kw):
            if falla:
                raise falla
            return fila

        async def log_transmission(_s, **kw):
            bd.transmisiones.append(kw)

        async def publicar(serial, aviso, qos=1):
            bd.avisos.append((serial, aviso))
            return f"sgpmp/{serial}/resultado"

        monkeypatch.setattr(ingest, "async_session_factory", sesion)
        monkeypatch.setattr(ingest.registry, "resolve_device_id", resolve_device_id)
        monkeypatch.setattr(ingest.registry, "resolve_variable_id", resolve_id)
        monkeypatch.setattr(ingest.registry, "resolve_sensor_id", resolve_id)
        monkeypatch.setattr(ingest.telemetry_repo, "ingesta_telemetria", ingesta)
        monkeypatch.setattr(ingest.telemetry_repo, "log_transmission", log_transmission)
        monkeypatch.setattr(ingest.publisher, "publish_resultado_ingesta", publicar)


@pytest.fixture(autouse=True)
def _entorno(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_lectura_persistida_queda_exito_y_el_nodo_libera(monkeypatch) -> None:
    bd = _Bd(monkeypatch, fila={"id_telemetria": 9, "estado_calidad": "LECTURA_VALIDA",
                                "mensaje": "OK"})

    await ingest.ingest_telemetry("EDGE-1", dict(LECTURA), topic="sgpmp/EDGE-1/telemetry")

    (t,) = bd.transmisiones
    assert (t["estado"], t["error_descripcion"]) == ("EXITO", None)
    (serial, aviso) = bd.avisos[0]
    assert serial == "EDGE-1"
    assert aviso == {
        "tipo_mensaje": "RESULTADO_INGESTA",
        "id_mensaje": "buf-77",
        "sensor": "dht22",
        "variable": "TEMPERATURA_AMBIENTAL",
        "timestamp_captura": "2026-10-17T10:00:00-05:00",
        "resultado": "EXITO",
        "accion_buffer": "LIBERAR",
        "codigo": "LECTURA_VALIDA",
        "mensaje": "OK",
        "id_telemetria": 9,
    }


async def test_timestamp_futuro_queda_fallido_con_la_descripcion(monkeypatch) -> None:
    bd = _Bd(monkeypatch, fila={"id_telemetria": None, "estado_calidad": None,
                                "mensaje": "ERROR_TIEMPO: timestamp_captura futuro."})

    r = await ingest.ingest_telemetry("EDGE-1", dict(LECTURA))

    (t,) = bd.transmisiones
    assert t["estado"] == "FALLIDO"
    assert t["error_descripcion"] == "ERROR_TIEMPO: timestamp_captura futuro."
    assert bd.commits == 1
    assert (r.accion_buffer, bd.avisos[0][1]["accion_buffer"]) == ("LIBERAR", "LIBERAR")


async def test_con_la_bd_caida_el_nodo_retiene(monkeypatch) -> None:
    bd = _Bd(monkeypatch, falla=ConnectionRefusedError("BD caída"))

    r = await ingest.ingest_telemetry("EDGE-1", dict(LECTURA))

    assert (r.estado, r.accion_buffer, r.codigo) == ("FALLIDO", "RETENER", "ERROR_PERSISTENCIA")
    assert bd.avisos[0][1]["accion_buffer"] == "RETENER"
    # Intento best-effort de dejar la transmisión fallida (aquí la sesión sí responde).
    assert bd.transmisiones[-1]["estado"] == "FALLIDO"


async def test_timeout_de_bd_queda_timeout(monkeypatch) -> None:
    bd = _Bd(monkeypatch, falla=TimeoutError())
    r = await ingest.ingest_telemetry("EDGE-1", dict(LECTURA))
    assert (r.estado, r.accion_buffer) == ("TIMEOUT", "RETENER")
    assert bd.transmisiones[-1]["estado"] == "TIMEOUT"


async def test_payload_invalido_es_rechazo_definitivo(monkeypatch) -> None:
    bd = _Bd(monkeypatch)
    r = await ingest.ingest_telemetry("EDGE-1", {"variable": "X"})  # faltan campos
    assert (r.estado, r.accion_buffer, r.codigo) == ("FALLIDO", "LIBERAR", "ERROR_ESTRUCTURA")
    assert bd.transmisiones[0]["estado"] == "FALLIDO"


async def test_dispositivo_no_registrado_avisa_sin_fila_de_transmision(monkeypatch) -> None:
    bd = _Bd(monkeypatch, device_id=None)
    r = await ingest.ingest_telemetry("NO-EXISTE", dict(LECTURA))
    assert r.codigo == "ERROR_AUTENTICACION" and r.accion_buffer == "LIBERAR"
    assert bd.transmisiones == []  # id_dispositivo_iot es NOT NULL
    assert bd.avisos[0][0] == "NO-EXISTE"


async def test_sin_mqtt_el_aviso_no_rompe_la_ingesta(monkeypatch) -> None:
    bd = _Bd(monkeypatch, fila={"id_telemetria": 1, "estado_calidad": "LECTURA_VALIDA",
                                "mensaje": "OK"})

    async def sin_conexion(*_a, **_kw):
        raise MqttNotConnectedError()

    monkeypatch.setattr(ingest.publisher, "publish_resultado_ingesta", sin_conexion)
    r = await ingest.ingest_telemetry("EDGE-1", dict(LECTURA))
    assert r.estado == "EXITO" and bd.transmisiones[0]["estado"] == "EXITO"


@pytest.mark.parametrize(
    "sensor, causa",
    [("SEN-NO-EXISTE", "no tiene un sensor llamado 'SEN-NO-EXISTE'"), (None, "Falta `sensor`")],
    ids=["nombre-inexistente", "sin-nombre-y-varios-sensores"],
)
async def test_sensor_no_resuelto_es_error_sensor(monkeypatch, sensor, causa) -> None:
    bd = _Bd(monkeypatch)

    async def sin_sensor(_s, _device_id, _nombre=None):
        return None

    monkeypatch.setattr(ingest.registry, "resolve_sensor_id", sin_sensor)
    lectura = {k: v for k, v in LECTURA.items() if k != "sensor"}
    if sensor:
        lectura["sensor"] = sensor

    r = await ingest.ingest_telemetry("EDGE-1", lectura)

    assert (r.estado, r.accion_buffer, r.codigo) == ("FALLIDO", "LIBERAR", "ERROR_SENSOR")
    assert causa in r.mensaje
    assert bd.transmisiones[0]["error_descripcion"] == r.mensaje
    assert bd.avisos[0][1]["codigo"] == "ERROR_SENSOR"
