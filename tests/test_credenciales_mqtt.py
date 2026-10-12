"""SEG-BROKER-03 (TC-M09-250/251): credencial MQTT del Gateway Edge en dynamic-security.

Con dobles para la BD y para Mosquitto: se verifica QUÉ comandos de
dynamic-security se mandan. Que Mosquitto 2.1.2 los aplica como se espera (SUBACK
de fallo con '#', publicación cruzada rechazada, desconexión al revocar) se
verifica contra el broker real con scripts/e2e_credenciales_mqtt.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import pytest

from app.config import get_settings
from app.core.errors import (
    DeviceDependsOnGatewayError,
    DeviceInactiveError,
    DeviceNotFoundError,
    DynsecError,
    UsuarioReservadoError,
)
from app.db.repositories.registry import EstadoDispositivo as E
from app.mqtt import dynsec, presencia
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
def bd() -> dict[str, E]:
    """modulo9: EDGE-1 atiende a ESP-1 y ESP-2 (ESP-3 apagado); SUELTO se conecta directo."""
    return {
        "EDGE-1": E(True, None),
        "ESP-1": E(True, "EDGE-1"),
        "ESP-2": E(True, "EDGE-1"),
        "ESP-3": E(False, "EDGE-1"),
        "SUELTO": E(True, None),
        "VIEJO": E(False, None),
    }


@pytest.fixture
def falso(monkeypatch: pytest.MonkeyPatch, bd: dict[str, E]) -> DynsecFalso:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("MQTT_USERNAME", "sgpmp_gateway")
    # vacías y no delenv: Settings también lee el .env local
    monkeypatch.setenv("MQTT_DEVICE_USERNAME", "")
    monkeypatch.setenv("MQTT_DEVICE_PASSWORD", "")
    get_settings.cache_clear()

    @asynccontextmanager
    async def sesion_falsa():
        yield object()

    async def mapa_dispositivos(_session):
        return dict(bd)

    falso = DynsecFalso()
    monkeypatch.setattr(cm, "async_session_factory", sesion_falsa)
    monkeypatch.setattr(cm.registry, "mapa_dispositivos", mapa_dispositivos)
    monkeypatch.setattr(cm.dynsec, "ejecutar", falso)
    yield falso
    get_settings.cache_clear()


def _roles(cliente: dict) -> list[str]:
    return [r["rolename"] for r in cliente["roles"]]


# ── Emitir ──────────────────────────────────────────────────────────────────


async def test_emitir_al_edge_le_da_sus_topics_y_los_de_sus_dispositivos(falso, caplog) -> None:
    caplog.set_level(logging.DEBUG)

    credencial = await cm.emitir("EDGE-1")

    assert credencial.usuario == "EDGE-1"
    assert credencial.seriales == ["EDGE-1", "ESP-1", "ESP-2"]  # ESP-3 está apagado
    assert len(credencial.password) >= 32

    roles = {c["rolename"]: c for c in falso.de("createRole")}
    assert set(roles) == {"serial-EDGE-1", "serial-ESP-1", "serial-ESP-2"}
    rol = roles["serial-ESP-2"]
    assert rol["allowwildcardsubs"] is False
    assert {(a["acltype"], a["topic"]) for a in rol["acls"]} == {
        ("publishClientSend", "sgpmp/ESP-2/telemetry"),
        ("publishClientSend", "sgpmp/ESP-2/heartbeat"),
        ("publishClientSend", "sgpmp/ESP-2/status"),
        ("subscribeLiteral", "sgpmp/ESP-2/command"),
        ("publishClientReceive", "sgpmp/ESP-2/command"),
    }
    topics = [a["topic"] for r in roles.values() for a in r["acls"]]
    assert not any("+" in t or "#" in t for t in topics)  # un rol abre solo SU serial

    (cliente,) = falso.de("modifyClient")
    assert cliente["username"] == "EDGE-1" and cliente["password"] == credencial.password
    assert _roles(cliente) == ["serial-EDGE-1", "serial-ESP-1", "serial-ESP-2"]
    assert falso.de("enableClient") == [{"command": "enableClient", "username": "EDGE-1"}]
    assert credencial.password not in caplog.text  # nunca a los logs


async def test_emitir_a_un_dispositivo_directo_solo_cubre_su_serial(falso) -> None:
    assert (await cm.emitir("SUELTO")).seriales == ["SUELTO"]


async def test_emitir_tolera_que_el_rol_y_el_cliente_ya_existan(falso) -> None:
    falso.errores = {"createRole": "Role already exists", "createClient": "Client already exists"}
    await cm.emitir("EDGE-1")


@pytest.mark.parametrize(
    ("serial", "error"),
    [
        ("NO-EXISTE", DeviceNotFoundError),
        ("VIEJO", DeviceInactiveError),
        ("ESP-1", DeviceDependsOnGatewayError),  # se comunica por su Edge
        ("sgpmp_gateway", UsuarioReservadoError),  # pisaría al admin
    ],
)
async def test_emitir_solo_para_quien_se_conecta_al_broker(falso, serial, error) -> None:
    with pytest.raises(error):
        await cm.emitir(serial)
    assert falso.lotes == []  # no toca Mosquitto


async def test_un_serial_legado_con_comodines_no_entra_a_la_acl(falso, bd) -> None:
    bd["ROTO/+"] = E(True, "EDGE-1")
    assert (await cm.emitir("EDGE-1")).seriales == ["EDGE-1", "ESP-1", "ESP-2"]


# ── Sincronizar (cambio de Edge desde el backend, sin rotar) ────────────────


def _edge(*seriales: str, **extra) -> dict:
    roles = [{"rolename": f"serial-{s}"} for s in seriales]
    return {"username": "EDGE-1", "roles": roles, **extra}


async def test_sincronizar_agrega_el_dispositivo_nuevo_sin_tocar_la_clave(falso) -> None:
    falso.datos = {
        "getClient": {"client": _edge("EDGE-1", "ESP-1")},
        "listRoles": {"roles": ["serial-EDGE-1", "serial-ESP-1"]},
    }

    assert await cm.sincronizar_credencial("EDGE-1") is True

    assert [c["rolename"] for c in falso.de("createRole")] == ["serial-ESP-2"]
    (cliente,) = falso.de("modifyClient")
    assert "password" not in cliente
    assert _roles(cliente) == ["serial-EDGE-1", "serial-ESP-1", "serial-ESP-2"]


async def test_sincronizar_no_escribe_si_ya_esta_alineado(falso) -> None:
    falso.datos = {
        "getClient": {"client": _edge("EDGE-1", "ESP-1", "ESP-2")},
        "listRoles": {"roles": ["serial-EDGE-1", "serial-ESP-1", "serial-ESP-2"]},
    }
    await cm.sincronizar_credencial("EDGE-1")
    assert len(falso.lotes) == 1  # solo la lectura


async def test_sincronizar_un_edge_sin_credencial_no_hace_nada(falso) -> None:
    falso.errores = {"getClient": "Client not found"}
    assert await cm.sincronizar_credencial("EDGE-1") is False
    assert len(falso.lotes) == 1


async def test_sincronizar_no_reactiva_una_credencial_revocada(falso) -> None:
    falso.datos = {
        "getClient": {"client": _edge("EDGE-1", disabled=True)},
        "listRoles": {"roles": []},
    }
    assert await cm.sincronizar_credencial("EDGE-1") is True
    assert len(falso.lotes) == 1


# ── Revocar y consultar ─────────────────────────────────────────────────────


async def test_revocar_deshabilita_la_credencial_y_quita_los_topics_del_serial(falso) -> None:
    falso.errores = {"disableClient": "Client not found"}  # un dispositivo de un Edge

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
    assert await cm.consultar("EDGE-1") is None

    falso.errores = {}
    falso.datos = {"getClient": {"client": _edge("EDGE-1", "ESP-1", disabled=True, connections=[])}}
    estado = await cm.consultar("EDGE-1")
    assert estado == cm.EstadoCredencial(
        usuario="EDGE-1", habilitada=False, conectada=False, seriales=["EDGE-1", "ESP-1"]
    )


async def test_consultar_no_da_por_conectado_al_edge_que_aviso_desconexion(falso) -> None:
    """INC-M09-70-G29: dynsec sigue listando la sesión persistente del Edge caído;
    `conectada` debe coincidir con lo que decide `sin_conexion()` al publicar."""
    falso.datos = {"getClient": {"client": _edge("EDGE-1", connections=[{"address": "10.0.0.2"}])}}
    assert (await cm.consultar("EDGE-1")).conectada is True

    presencia.marcar_desconectado("EDGE-1")
    try:
        assert (await cm.consultar("EDGE-1")).conectada is False
    finally:
        presencia.marcar_conectado("EDGE-1")


# ── Sincronización al conectar (reconciliación con modulo9) ─────────────────

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


def _base(*roles_serial: str) -> list[dict]:
    return [_rol("admin"), _rol("gateway", GATEWAY_ACLS), *(_rol(r) for r in roles_serial)]


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
    assert _roles(gateway) == ["admin", "gateway"]
    assert len(falso.lotes) == 2  # Mosquitto lo desconecta: el resto, al reconectar


async def test_en_regimen_no_reescribe_nada_que_ya_este_bien(falso, monkeypatch) -> None:
    """modifyClient/modifyRole desconectan aunque no cambie nada (Mosquitto 2.1.2):
    reescribir en cada conexión expulsaría al gateway y a los Edge."""
    legacy_acls = _legacy(monkeypatch)
    legacy = {"username": "sgpmp_devices", "roles": [{"rolename": "dispositivos_legacy"}]}
    falso.datos = _estado(
        [GATEWAY, legacy, _edge("EDGE-1", "ESP-1", "ESP-2")],
        [
            *_base("serial-EDGE-1", "serial-ESP-1", "serial-ESP-2"),
            _rol("dispositivos_legacy", legacy_acls),
        ],
    )

    await cm.sincronizar()

    assert len(falso.lotes) == 1  # solo la lectura


async def test_reconcilia_cada_edge_con_los_dispositivos_que_atiende(falso) -> None:
    # A EDGE-1 le falta ESP-2 (se le asignó con el broker caído)
    falso.datos = _estado(
        [GATEWAY, _edge("EDGE-1", "ESP-1")], _base("serial-EDGE-1", "serial-ESP-1")
    )

    await cm.sincronizar()

    assert [c["rolename"] for c in falso.de("createRole")] == ["serial-ESP-2"]
    (cliente,) = falso.de("modifyClient")
    assert cliente["username"] == "EDGE-1" and "password" not in cliente
    assert _roles(cliente) == ["serial-EDGE-1", "serial-ESP-1", "serial-ESP-2"]


async def test_reconcilia_inactivos_y_dispositivos_que_pasaron_a_un_edge(falso) -> None:
    falso.datos = _estado(
        [
            GATEWAY,
            # ESP-1 tenía credencial propia y después se le asignó un Edge
            {"username": "ESP-1", "roles": [{"rolename": "serial-ESP-1"}]},
            {"username": "VIEJO", "roles": [{"rolename": "serial-VIEJO"}]},
            {"username": "NO-EXISTE", "roles": [], "disabled": True},  # ya deshabilitado
        ],
        _base("serial-ESP-1", "serial-VIEJO", "serial-ESP-3"),
    )

    await cm.sincronizar()

    assert {c["username"] for c in falso.de("disableClient")} == {"ESP-1", "VIEJO"}
    assert {c["rolename"] for c in falso.de("deleteRole")} == {"serial-VIEJO", "serial-ESP-3"}
    assert falso.de("deleteClient") == []


async def test_sin_legacy_retira_la_credencial_compartida(falso) -> None:
    legacy = {"username": "sgpmp_devices", "roles": [{"rolename": "dispositivos_legacy"}]}
    falso.datos = _estado([GATEWAY, legacy], [*_base(), _rol("dispositivos_legacy")])

    await cm.sincronizar()

    assert falso.de("createClient") == []
    assert falso.de("deleteClient") == [{"command": "deleteClient", "username": "sgpmp_devices"}]
    assert falso.de("deleteRole") == [{"command": "deleteRole", "rolename": "dispositivos_legacy"}]


async def test_legacy_se_crea_si_falta_pero_su_clave_no_se_rota_en_caliente(
    falso, monkeypatch
) -> None:
    _legacy(monkeypatch)
    falso.datos = _estado([GATEWAY], _base())

    await cm.sincronizar()

    assert [c["rolename"] for c in falso.de("createRole")] == ["dispositivos_legacy"]
    assert falso.de("createClient") == [
        {
            "command": "createClient",
            "username": "sgpmp_devices",
            "password": "clave-legacy",
            "roles": [{"rolename": "dispositivos_legacy"}],
        }
    ]
    assert falso.de("modifyClient") == []


async def test_un_rol_con_acls_distintas_se_corrige(falso) -> None:
    viejas = [{"acltype": "publishClientSend", "topic": "otro/+/command", "allow": True}]
    falso.datos = _estado([GATEWAY], [_rol("admin"), _rol("gateway", viejas)])

    await cm.sincronizar()

    assert falso.de("modifyRole") == [
        {"command": "modifyRole", "rolename": "gateway", "acls": GATEWAY_ACLS}
    ]


# ── Cliente de $CONTROL ──────────────────────────────────────────────────────


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
