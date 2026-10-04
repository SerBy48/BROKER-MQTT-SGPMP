"""Credenciales MQTT del Gateway Edge en dynamic-security (SEG-BROKER-03, TC-M09-250/251).

Modelo (ver docs/RFC_credencial_mqtt_por_dispositivo.md):

- El Gateway Edge (la computadora de borde del sitio, el "Gateway IoT" de M03)
  es un dispositivo de tipo GATEWAY_EDGE en modulo9.dispositivos_iot, y cada
  dispositivo que atiende apunta a él con ``id_dispositivo_gateway`` (N:1).
- Se conecta al broker quien no depende de un Edge: el Edge, o un dispositivo
  que se conecta directo. Su credencial: usuario = su serial.
- Un rol ``serial-<S>`` por serial, con permiso solo sobre sus topics. La
  credencial del Edge lleva su rol y el de cada dispositivo activo que atiende,
  leídos de la BD. Un SUBSCRIBE '#' o sobre otro serial recibe SUBACK de fallo;
  publicar en el topic de otro serial se rechaza (verificado contra 2.1.2).
- Rol ``gateway`` para publicar comandos; el resto lo da el rol ``admin`` que
  crea ``mosquitto_ctrl dynsec init`` para MQTT_USERNAME (ver el entrypoint).
- Rol ``dispositivos_legacy``: la ACL de la credencial compartida MQTT_DEVICE_*,
  solo mientras los Edge migran.

La BD manda: cada vez que el gateway conecta (``sincronizar``) se deshabilitan
las credenciales de seriales inactivos o que pasaron a depender de un Edge, se
borran los roles de seriales inactivos y los roles de cada Edge se alinean con
los dispositivos que atiende. El backend avisa los cambios de Edge con
``sincronizar_credencial`` para no esperar a la próxima reconexión.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.core.errors import (
    DeviceDependsOnGatewayError,
    DeviceInactiveError,
    DeviceNotFoundError,
    DynsecError,
    UsuarioReservadoError,
)
from app.db.engine import async_session_factory
from app.db.repositories import registry
from app.db.repositories.registry import EstadoDispositivo
from app.mqtt import dynsec
from app.schemas import PATRON_SERIAL

logger = logging.getLogger(__name__)

ROL_ADMIN = "admin"  # lo crea `mosquitto_ctrl dynsec init`
ROL_GATEWAY = "gateway"
ROL_LEGACY = "dispositivos_legacy"
_PREFIJO_ROL_SERIAL = "serial-"

_YA_EXISTE = frozenset({"Role already exists", "Client already exists"})
_NO_EXISTE = frozenset({"Client not found", "Role not found"})


@dataclass(frozen=True)
class CredencialEmitida:
    usuario: str
    password: str
    seriales: list[str]


@dataclass(frozen=True)
class EstadoCredencial:
    usuario: str
    habilitada: bool
    conectada: bool
    seriales: list[str]


def rol_serial(serial: str) -> str:
    return f"{_PREFIJO_ROL_SERIAL}{serial}"


def _acl(tipo: str, topic: str) -> dict:
    return {"acltype": tipo, "topic": topic, "allow": True}


def _acls_dispositivo(settings: Settings, serial: str, suscripcion: str) -> list[dict]:
    p = settings.mqtt_topic_prefix
    comando = f"{p}/{serial}/{settings.mqtt_topic_command}"
    propios = (
        settings.mqtt_topic_telemetry,
        settings.mqtt_topic_heartbeat,
        settings.mqtt_topic_status,
    )
    return [
        *(_acl("publishClientSend", f"{p}/{serial}/{t}") for t in propios),
        _acl(suscripcion, comando),
        _acl("publishClientReceive", comando),
    ]


def _rol_de_serial(settings: Settings, serial: str) -> dict:
    return {
        "rolename": rol_serial(serial),
        "acls": _acls_dispositivo(settings, serial, "subscribeLiteral"),
        "allowwildcardsubs": False,
    }


def _verificar(respuestas: list[dict], ignorables: frozenset[str] = frozenset()) -> None:
    errores = [
        f"{r.get('command')}: {r['error']}"
        for r in respuestas
        if r.get("error") and r["error"] not in ignorables
    ]
    if errores:
        raise DynsecError("; ".join(errores))


def _rechazar_reservado(settings: Settings, serial: str) -> None:
    # Un serial válido como "sgpmp_gateway" pisaría la clave y los roles del admin.
    if serial in {settings.mqtt_username, settings.mqtt_device_username}:
        raise UsuarioReservadoError(serial)


def _roles_de(cliente: dict) -> set[str]:
    return {r["rolename"] for r in cliente.get("roles", [])}


def _firma(acls: list[dict]) -> set[tuple]:
    return {(a["acltype"], a["topic"], a["allow"]) for a in acls}


async def _mapa() -> dict[str, EstadoDispositivo]:
    async with async_session_factory() as session:
        return await registry.mapa_dispositivos(session)


def _seriales_de_la_credencial(serial: str, mapa: dict[str, EstadoDispositivo]) -> list[str]:
    """El serial propio y, si es un Edge, los dispositivos activos que atiende.

    Los atendidos salen de la BD, no de una petición validada: un serial legado
    con '+', '#' o '/' abriría topics ajenos dentro de la ACL, así que se omite.
    """
    atendidos = []
    for s, e in sorted(mapa.items()):
        if not (e.activo and e.serial_gateway == serial):
            continue
        if re.fullmatch(PATRON_SERIAL, s):
            atendidos.append(s)
        else:
            logger.warning("Serial %r con formato inválido: no entra a la ACL de %s", s, serial)
    return [serial, *atendidos]


def _se_conecta_directo(serial: str, mapa: dict[str, EstadoDispositivo]) -> bool:
    estado = mapa.get(serial)
    return estado is not None and estado.activo and estado.serial_gateway is None


def _alinear_roles(
    settings: Settings,
    usuario: str,
    cliente: dict,
    roles_existentes: set[str],
    seriales: list[str],
) -> list[dict]:
    """Comandos para que el cliente tenga exactamente el rol de cada serial.

    Solo crea los roles que faltan y solo modifica el cliente si sus roles
    difieren: cambiarle los roles lo desconecta (se reconecta solo, con la misma
    clave) y modifyRole desconecta a quien tenga el rol.
    """
    comandos = [
        {"command": "createRole", **_rol_de_serial(settings, s)}
        for s in seriales
        if rol_serial(s) not in roles_existentes
    ]
    deseados = [rol_serial(s) for s in seriales]
    actuales = {r for r in _roles_de(cliente) if r.startswith(_PREFIJO_ROL_SERIAL)}
    if actuales != set(deseados):
        comandos.append(
            {
                "command": "modifyClient",
                "username": usuario,
                "roles": [{"rolename": r} for r in deseados],
            }
        )
    return comandos


async def sincronizar() -> None:
    """Deja dynamic-security al día y lo reconcilia con modulo9.

    Corre en cada conexión del gateway. Lee el estado y escribe solo lo que
    difiere: en Mosquitto 2.1.2 modifyClient y setClientPassword desconectan al
    cliente aunque no cambie nada, y modifyRole desconecta a quien tenga ese rol
    (medido). Reescribir todo en cada conexión expulsaría al gateway y a los
    Edge una y otra vez.
    """
    settings = get_settings()
    p = settings.mqtt_topic_prefix
    legacy = bool(settings.mqtt_device_username and settings.mqtt_device_password)
    roles_deseados = {
        ROL_GATEWAY: [_acl("publishClientSend", f"{p}/+/{settings.mqtt_topic_command}")]
    }
    if legacy:
        roles_deseados[ROL_LEGACY] = _acls_dispositivo(settings, "+", "subscribePattern")

    lista_clientes, lista_roles = await dynsec.ejecutar(
        [{"command": "listClients", "verbose": True}, {"command": "listRoles", "verbose": True}]
    )
    _verificar([lista_clientes, lista_roles])
    clientes = {c["username"]: c for c in lista_clientes["data"]["clients"]}
    roles = {r["rolename"]: r for r in lista_roles["data"]["roles"]}

    comandos = []
    for nombre, acls in roles_deseados.items():
        if nombre not in roles:
            comandos.append({"command": "createRole", "rolename": nombre, "acls": acls})
        elif _firma(roles[nombre].get("acls", [])) != _firma(acls):
            comandos.append({"command": "modifyRole", "rolename": nombre, "acls": acls})

    if ROL_GATEWAY not in _roles_de(clientes.get(settings.mqtt_username, {})):
        # Cambiarle los roles desconecta al gateway ("administrative action"). Pasa
        # una sola vez, en el primer arranque: la reconexión vuelve a sincronizar y
        # ya no entra acá. modifyClient sin password conserva la clave.
        comandos.append(
            {
                "command": "modifyClient",
                "username": settings.mqtt_username,
                "roles": [{"rolename": ROL_ADMIN}, {"rolename": ROL_GATEWAY}],
            }
        )
        _verificar(await dynsec.ejecutar(comandos), _YA_EXISTE)
        logger.info("Rol gateway asignado: Mosquitto desconecta al gateway una vez para aplicarlo")
        return

    if legacy and settings.mqtt_device_username not in clientes:
        # Solo el alta: si ya existe, su clave la rota el entrypoint con el broker
        # apagado (rotarla acá desconectaría a todos los Edge legacy).
        comandos.append(
            {
                "command": "createClient",
                "username": settings.mqtt_device_username,
                "password": settings.mqtt_device_password,
                "roles": [{"rolename": ROL_LEGACY}],
            }
        )

    # La BD manda.
    mapa = await _mapa()
    activos = {serial for serial, estado in mapa.items() if estado.activo}
    roles_existentes = set(roles)
    for usuario, cliente in clientes.items():
        if usuario == settings.mqtt_username:
            continue
        if ROL_LEGACY in _roles_de(cliente):
            if not legacy:
                comandos.append({"command": "deleteClient", "username": usuario})
            continue
        if not _se_conecta_directo(usuario, mapa):
            # Inactivo, inexistente o pasó a depender de un Edge: sin credencial propia.
            if not cliente.get("disabled"):
                comandos.append({"command": "disableClient", "username": usuario})
            continue
        if cliente.get("disabled"):
            continue  # revocada a propósito: no se reactiva sola
        alinear = _alinear_roles(
            settings, usuario, cliente, roles_existentes, _seriales_de_la_credencial(usuario, mapa)
        )
        roles_existentes |= {c["rolename"] for c in alinear if c["command"] == "createRole"}
        comandos += alinear
    for nombre in roles:
        inactivo = nombre.startswith(_PREFIJO_ROL_SERIAL) and (
            nombre.removeprefix(_PREFIJO_ROL_SERIAL) not in activos
        )
        if inactivo or (nombre == ROL_LEGACY and not legacy):
            comandos.append({"command": "deleteRole", "rolename": nombre})

    if comandos:
        _verificar(await dynsec.ejecutar(comandos), _YA_EXISTE | _NO_EXISTE)
    logger.info(
        "dynamic-security sincronizado: %d cambios (credencial legacy %s)",
        len(comandos),
        "activa" if legacy else "retirada",
    )


def _validar_conexion_propia(serial: str, mapa: dict[str, EstadoDispositivo]) -> None:
    estado = mapa.get(serial)
    if estado is None:
        raise DeviceNotFoundError(serial)
    if not estado.activo:
        raise DeviceInactiveError(serial)
    if estado.serial_gateway is not None:
        raise DeviceDependsOnGatewayError(serial, estado.serial_gateway)


async def emitir(serial: str) -> CredencialEmitida:
    """Crea o rota la credencial de `serial` (un Edge o un dispositivo que se
    conecta directo) con permiso sobre sus topics y los de los dispositivos que
    atiende según modulo9.

    Devuelve la contraseña una sola vez: no se guarda en ningún lado salvo su
    hash dentro de dynamic-security.json. Rotar invalida la clave anterior.
    """
    settings = get_settings()
    _rechazar_reservado(settings, serial)
    mapa = await _mapa()
    _validar_conexion_propia(serial, mapa)
    seriales = _seriales_de_la_credencial(serial, mapa)
    for s in seriales:
        _rechazar_reservado(settings, s)

    password = secrets.token_urlsafe(24)
    comandos = []
    for s in seriales:
        rol = _rol_de_serial(settings, s)
        # createRole falla con "Role already exists" si ya está; modifyRole deja las
        # ACL al día (y desconecta a quien tenga el rol, que al rotar es lo que se quiere).
        comandos += [{"command": "createRole", **rol}, {"command": "modifyRole", **rol}]
    cliente = {
        "username": serial,
        "password": password,
        "roles": [{"rolename": rol_serial(s)} for s in seriales],
    }
    comandos += [
        {"command": "createClient", **cliente},
        {"command": "modifyClient", **cliente},  # si ya existía: rota y reemplaza roles
        {"command": "enableClient", "username": serial},  # por si estaba revocada
    ]
    _verificar(await dynsec.ejecutar(comandos), _YA_EXISTE)
    logger.info("Credencial MQTT emitida para %s (seriales=%s)", serial, seriales)
    return CredencialEmitida(usuario=serial, password=password, seriales=seriales)


async def sincronizar_credencial(serial: str) -> bool:
    """Alinea los topics de la credencial de `serial` con modulo9 sin rotar la clave.

    False si `serial` no tiene credencial propia (no hay nada que sincronizar).
    """
    settings = get_settings()
    _rechazar_reservado(settings, serial)
    cliente, lista_roles = await dynsec.ejecutar(
        [{"command": "getClient", "username": serial}, {"command": "listRoles"}]
    )
    if cliente.get("error") == "Client not found":
        return False
    _verificar([cliente, lista_roles])
    mapa = await _mapa()
    datos = cliente["data"]["client"]
    if datos.get("disabled") or not _se_conecta_directo(serial, mapa):
        return True  # revocada o sin conexión propia: lo resuelve la reconciliación
    roles_existentes = set(lista_roles["data"]["roles"])
    seriales = _seriales_de_la_credencial(serial, mapa)
    comandos = _alinear_roles(settings, serial, datos, roles_existentes, seriales)
    if comandos:
        _verificar(await dynsec.ejecutar(comandos), _YA_EXISTE)
        logger.info("Credencial MQTT de %s sincronizada con modulo9", serial)
    return True


async def consultar(serial: str) -> EstadoCredencial | None:
    _rechazar_reservado(get_settings(), serial)
    (respuesta,) = await dynsec.ejecutar([{"command": "getClient", "username": serial}])
    if respuesta.get("error") == "Client not found":
        return None
    _verificar([respuesta])
    cliente = respuesta["data"]["client"]
    return EstadoCredencial(
        usuario=serial,
        habilitada=not cliente.get("disabled", False),
        conectada=bool(cliente.get("connections")),
        seriales=[
            r["rolename"].removeprefix(_PREFIJO_ROL_SERIAL)
            for r in cliente.get("roles", [])
            if r["rolename"].startswith(_PREFIJO_ROL_SERIAL)
        ],
    )


async def revocar(serial: str) -> None:
    """Deshabilita la credencial de `serial` (y la desconecta en el acto) y le
    quita sus topics a cualquier Edge que lo atienda. Idempotente."""
    _rechazar_reservado(get_settings(), serial)
    respuestas = await dynsec.ejecutar(
        [
            {"command": "disableClient", "username": serial},
            {"command": "deleteRole", "rolename": rol_serial(serial)},
        ]
    )
    _verificar(respuestas, _NO_EXISTE)
    logger.info("Credencial MQTT revocada para %s", serial)
