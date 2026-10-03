"""SEG-BROKER-03 (TC-M09-250/251): credencial MQTT por Raspberry en dynamic-security.

Con dobles para la BD y para Mosquitto: se verifica QUÉ comandos de
dynamic-security se mandan. Que Mosquitto 2.1.2 los aplica como se espera (SUBACK
de fallo con '#', publicación cruzada rechazada, desconexión al revocar) se
verificó contra el broker real; ver docs/RFC_credencial_mqtt_por_dispositivo.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import pytest

from app.config import get_settings
from app.core.errors import (
    DeviceInactiveError,
    DeviceNotFoundError,
    DynsecError,
    UsuarioReservadoError,
)
from app.mqtt import dynsec
from app.services import credenciales_mqtt as cm


class DynsecFalso:
    """Registra los lotes enviados y contesta OK, salvo lo que se configure."""

    def __init__(self) -> None:
        self.lotes: list[list[dict]] = []
        self.errores: dict[str, str] = {}  # command -> error
        self.datos: dict[str, dict] = {}  # command -> data

    async def __call__(self, comandos: list[dict]) -> list[dict]:
        self.lotes.append(comandos)
        respuestas = []
        for c in comandos:
            r = {"command": c["command"]}
            if c["command"] in self.errores:
                r["error"] = self.errores[c["command"]]
            if c["command"] in self.datos:
                r["data"] = self.datos[c["command"]]
            respuestas.append(r)
        return respuestas

    def todos(self) -> list[dict]:
        return [c for lote in self.lotes for c in lote]

    def de(self, command: str) -> list[dict]:
        return [c for c in self.todos() if c["command"] == command]


@pytest.fixture
def estados() -> dict[str, bool]:
    return {"RPI-1": True, "ESP-2": True, "VIEJO": False}


@pytest.fixture
def falso(monkeypatch: pytest.MonkeyPatch, estados: dict[str, bool]) -> DynsecFalso:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("MQTT_USERNAME", "sgpmp_gateway")
    # vacías y no delenv: Settings también lee el .env local
    monkeypatch.setenv("MQTT_DEVICE_USERNAME", "")
    monkeypatch.setenv("MQTT_DEVICE_PASSWORD", "")
    get_settings.cache_clear()

    @asynccontextmanager
    async def sesion_falsa():
        yield object()

    async def estado_seriales(_session, seriales=None):
        if seriales is None:
            return dict(estados)
        return {s: estados[s] for s in seriales if s in estados}

    falso = DynsecFalso()
    monkeypatch.setattr(cm, "async_session_factory", sesion_falsa)
    monkeypatch.setattr(cm.registry, "estado_seriales", estado_seriales)
    monkeypatch.setattr(cm.dynsec, "ejecutar", falso)
    yield falso
    get_settings.cache_clear()


async def test_emitir_crea_un_rol_por_serial_solo_con_sus_topics(falso, caplog) -> None:
    caplog.set_level(logging.DEBUG)

    credencial = await cm.emitir("RPI-1", ["ESP-2", "RPI-1"])

    assert credencial.usuario == "RPI-1"
    assert credencial.seriales == ["RPI-1", "ESP-2"]  # principal primero, sin repetir
    assert len(credencial.password) >= 32

    roles = {c["rolename"]: c for c in falso.de("createRole")}
    assert set(roles) == {"serial-RPI-1", "serial-ESP-2"}
    rol = roles["serial-ESP-2"]
    assert rol["allowwildcardsubs"] is False
    assert {(a["acltype"], a["topic"]) for a in rol["acls"]} == {
        ("publishClientSend", "sgpmp/ESP-2/telemetry"),
        ("publishClientSend", "sgpmp/ESP-2/heartbeat"),
        ("publishClientSend", "sgpmp/ESP-2/status"),
        ("subscribeLiteral", "sgpmp/ESP-2/command"),
        ("publishClientReceive", "sgpmp/ESP-2/command"),
    }
    # ningún topic con comodines: un rol solo abre los topics de SU serial
    topics = [a["topic"] for r in roles.values() for a in r["acls"]]
    assert not any("+" in t or "#" in t for t in topics)

    (cliente,) = falso.de("modifyClient")
    assert cliente["username"] == "RPI-1"
    assert cliente["password"] == credencial.password
    assert [r["rolename"] for r in cliente["roles"]] == ["serial-RPI-1", "serial-ESP-2"]
    assert falso.de("enableClient") == [{"command": "enableClient", "username": "RPI-1"}]

    # la contraseña nunca va a los logs
    assert credencial.password not in caplog.text


async def test_emitir_tolera_que_el_rol_y_el_cliente_ya_existan(falso) -> None:
    # rotación: Mosquitto contesta "already exists" a los create y modify* actualiza
    falso.errores = {"createRole": "Role already exists", "createClient": "Client already exists"}
    await cm.emitir("RPI-1")


@pytest.mark.parametrize(
    ("serial", "error"), [("NO-EXISTE", DeviceNotFoundError), ("VIEJO", DeviceInactiveError)]
)
async def test_emitir_solo_para_seriales_activos_de_modulo9(falso, serial, error) -> None:
    with pytest.raises(error):
        await cm.emitir("RPI-1", [serial])
    assert falso.lotes == []  # no toca Mosquitto


async def test_un_serial_igual_al_usuario_del_gateway_no_puede_pisar_al_admin(falso) -> None:
    with pytest.raises(UsuarioReservadoError):
        await cm.emitir("sgpmp_gateway")
    with pytest.raises(UsuarioReservadoError):
        await cm.revocar("sgpmp_gateway")
    assert falso.lotes == []


async def test_revocar_deshabilita_la_credencial_y_quita_los_topics_del_serial(falso) -> None:
    falso.errores = {"disableClient": "Client not found"}  # serial que no era principal

    await cm.revocar("ESP-2")

    assert falso.todos() == [
        {"command": "disableClient", "username": "ESP-2"},
        {"command": "deleteRole", "rolename": "serial-ESP-2"},
    ]


async def test_un_error_real_de_mosquitto_no_se_traga(falso) -> None:
    falso.errores = {"deleteRole": "Internal error"}
    with pytest.raises(DynsecError, match="Internal error"):
        await cm.revocar("ESP-2")


async def test_consultar(falso) -> None:
    falso.errores = {"getClient": "Client not found"}
    assert await cm.consultar("RPI-1") is None

    falso.errores = {}
    falso.datos = {
        "getClient": {
            "client": {
                "username": "RPI-1",
                "disabled": True,
                "roles": [{"rolename": "serial-RPI-1"}, {"rolename": "serial-ESP-2"}],
                "connections": [],
            }
        }
    }
    estado = await cm.consultar("RPI-1")
    assert estado == cm.EstadoCredencial(
        usuario="RPI-1", habilitada=False, conectada=False, seriales=["RPI-1", "ESP-2"]
    )


GATEWAY_ACLS = [{"acltype": "publishClientSend", "topic": "sgpmp/+/command", "allow": True}]
GATEWAY = {"username": "sgpmp_gateway", "roles": [{"rolename": "admin"}, {"rolename": "gateway"}]}


def _rol(nombre: str, acls: list[dict] | None = None) -> dict:
    # como lo devuelve listRoles verbose (con priority)
    return {"rolename": nombre, "acls": [{**a, "priority": 0} for a in acls or []]}


def _estado(clientes: list[dict], roles: list[dict]) -> dict[str, dict]:
    return {
        "listClients": {"totalCount": len(clientes), "clients": clientes},
        "listRoles": {"totalCount": len(roles), "roles": roles},
    }


def _legacy(monkeypatch) -> list[dict]:
    monkeypatch.setenv("MQTT_DEVICE_USERNAME", "sgpmp_devices")
    monkeypatch.setenv("MQTT_DEVICE_PASSWORD", "clave-legacy")
    get_settings.cache_clear()
    return cm._acls_dispositivo(get_settings(), "+", "subscribePattern")


async def test_primer_arranque_el_gateway_se_asigna_su_rol_y_espera_la_reconexion(falso) -> None:
    falso.datos = _estado(
        [{"username": "sgpmp_gateway", "roles": [{"rolename": "admin"}]}], [_rol("admin")]
    )

    await cm.sincronizar()

    escrito = falso.lotes[1]
    assert escrito[0] == {"command": "createRole", "rolename": "gateway", "acls": GATEWAY_ACLS}
    gateway = escrito[1]
    assert gateway["command"] == "modifyClient" and gateway["username"] == "sgpmp_gateway"
    assert "password" not in gateway  # la clave del gateway la rota el entrypoint
    assert [r["rolename"] for r in gateway["roles"]] == ["admin", "gateway"]
    # Mosquitto lo desconecta por el cambio de roles: el resto, al reconectar
    assert len(falso.lotes) == 2


async def test_en_regimen_no_reescribe_nada_que_ya_este_bien(falso, monkeypatch) -> None:
    """modifyClient/modifyRole desconectan aunque no cambie nada (Mosquitto 2.1.2):
    reescribir en cada conexión expulsaría al gateway y a las Raspberry legacy."""
    legacy_acls = _legacy(monkeypatch)
    falso.datos = _estado(
        [
            GATEWAY,
            {"username": "sgpmp_devices", "roles": [{"rolename": "dispositivos_legacy"}]},
            {"username": "RPI-1", "roles": [{"rolename": "serial-RPI-1"}]},
        ],
        [
            _rol("admin"),
            _rol("gateway", GATEWAY_ACLS),
            _rol("dispositivos_legacy", legacy_acls),
            _rol("serial-RPI-1"),
        ],
    )

    await cm.sincronizar()

    assert len(falso.lotes) == 1  # solo la lectura


async def test_legacy_se_crea_si_falta_pero_su_clave_no_se_rota_en_caliente(
    falso, monkeypatch
) -> None:
    _legacy(monkeypatch)
    falso.datos = _estado([GATEWAY], [_rol("admin"), _rol("gateway", GATEWAY_ACLS)])

    await cm.sincronizar()

    assert [c["rolename"] for c in falso.de("createRole")] == ["dispositivos_legacy"]
    (legacy,) = falso.de("createClient")
    assert legacy == {
        "command": "createClient",
        "username": "sgpmp_devices",
        "password": "clave-legacy",
        "roles": [{"rolename": "dispositivos_legacy"}],
    }
    assert falso.de("modifyClient") == []


async def test_un_rol_con_acls_distintas_se_corrige(falso) -> None:
    viejas = [{"acltype": "publishClientSend", "topic": "otro/+/command", "allow": True}]
    falso.datos = _estado([GATEWAY], [_rol("admin"), _rol("gateway", viejas)])

    await cm.sincronizar()

    assert falso.de("modifyRole") == [
        {"command": "modifyRole", "rolename": "gateway", "acls": GATEWAY_ACLS}
    ]


async def test_reconcilia_con_modulo9(falso) -> None:
    falso.datos = _estado(
        [
            GATEWAY,
            {"username": "RPI-1", "roles": [{"rolename": "serial-RPI-1"}]},
            {"username": "VIEJO", "roles": [{"rolename": "serial-VIEJO"}]},
            {"username": "NO-EXISTE", "roles": [], "disabled": True},  # ya deshabilitado
        ],
        [_rol("admin"), _rol("gateway", GATEWAY_ACLS), _rol("serial-RPI-1"), _rol("serial-VIEJO")],
    )

    await cm.sincronizar()

    # VIEJO está inactivo en modulo9: su credencial y sus topics se cortan
    assert falso.de("disableClient") == [{"command": "disableClient", "username": "VIEJO"}]
    assert falso.de("deleteRole") == [{"command": "deleteRole", "rolename": "serial-VIEJO"}]
    assert falso.de("deleteClient") == []


async def test_sin_legacy_retira_la_credencial_compartida(falso) -> None:
    falso.datos = _estado(
        [GATEWAY, {"username": "sgpmp_devices", "roles": [{"rolename": "dispositivos_legacy"}]}],
        [_rol("admin"), _rol("gateway", GATEWAY_ACLS), _rol("dispositivos_legacy")],
    )

    await cm.sincronizar()

    assert falso.de("createClient") == []
    assert falso.de("deleteClient") == [{"command": "deleteClient", "username": "sgpmp_devices"}]
    assert falso.de("deleteRole") == [{"command": "deleteRole", "rolename": "dispositivos_legacy"}]


async def test_dynsec_correlaciona_la_respuesta_con_su_lote(monkeypatch) -> None:
    publicados: list[bytes] = []

    async def publicar(topic, payload, qos=1):
        assert topic == "$CONTROL/dynamic-security/v1"
        publicados.append(payload)

    monkeypatch.setattr(dynsec.mqtt_gateway, "publish", publicar)

    tarea = asyncio.create_task(dynsec.ejecutar([{"command": "getClient", "username": "X"}]))
    await asyncio.sleep(0)
    (enviado,) = [json.loads(p)["commands"] for p in publicados]
    corr = enviado[0]["correlationData"]

    # una respuesta de otro lote no la resuelve
    dynsec.resolver_respuesta(json.dumps({"responses": [{"correlationData": "otro:0"}]}).encode())
    await asyncio.sleep(0)
    assert not tarea.done()

    respuesta = {"command": "getClient", "error": "Client not found", "correlationData": corr}
    dynsec.resolver_respuesta(json.dumps({"responses": [respuesta]}).encode())
    assert await tarea == [respuesta]


async def test_dynsec_sin_respuesta_lanza_error(monkeypatch) -> None:
    async def publicar(topic, payload, qos=1):
        pass

    monkeypatch.setattr(dynsec.mqtt_gateway, "publish", publicar)
    monkeypatch.setattr(dynsec, "_TIMEOUT_S", 0.05)
    with pytest.raises(DynsecError):
        await dynsec.ejecutar([{"command": "listClients"}])
