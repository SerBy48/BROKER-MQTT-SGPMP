"""Credenciales MQTT por Raspberry en dynamic-security (SEG-BROKER-03, TC-M09-250/251).

Modelo (ver docs/RFC_credencial_mqtt_por_dispositivo.md):

- Una credencial por Raspberry, que es una sola conexión MQTT aunque transmita
  por varios seriales (EDGE_SERIALS del edge_agent). Usuario = serial principal.
- Un rol `serial-<S>` por cada serial, con permiso solo sobre sus topics. Un
  SUBSCRIBE '#' o sobre otro serial recibe SUBACK de fallo; publicar en el topic
  de otro serial se rechaza (verificado contra Mosquitto 2.1.2).
- Rol `gateway` para publicar comandos; el resto lo da el rol `admin` que crea
  `mosquitto_ctrl dynsec init` para MQTT_USERNAME (ver el entrypoint).
- Rol `dispositivos_legacy`: la ACL de la credencial compartida MQTT_DEVICE_*,
  solo mientras las Raspberry migran.

La BD manda: solo se emiten credenciales para seriales activos de
modulo9.dispositivos_iot y, cada vez que el gateway conecta, se deshabilitan las
de seriales inactivos (por si la revocación que hace el backend al desactivar un
dispositivo falló).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.core.errors import (
    DeviceInactiveError,
    DeviceNotFoundError,
    DynsecError,
    UsuarioReservadoError,
)
from app.db.engine import async_session_factory
from app.db.repositories import registry
from app.mqtt import dynsec

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


def _asegurar_rol(nombre: str, acls: list[dict], **extra) -> list[dict]:
    # createRole falla con "Role already exists" si ya está; modifyRole deja las
    # ACL al día en ambos casos (y desconecta a quien tenga el rol, ver sincronizar).
    rol = {"rolename": nombre, "acls": acls, **extra}
    return [{"command": "createRole", **rol}, {"command": "modifyRole", **rol}]


def _asegurar_cliente(usuario: str, password: str, roles: list[str]) -> list[dict]:
    # Igual que _asegurar_rol: createClient si es nuevo; modifyClient rota la
    # clave y reemplaza los roles si ya existía (y lo desconecta, que al rotar es lo
    # que se quiere); enableClient por si estaba revocado.
    cliente = {
        "username": usuario,
        "password": password,
        "roles": [{"rolename": r} for r in roles],
    }
    return [
        {"command": "createClient", **cliente},
        {"command": "modifyClient", **cliente},
        {"command": "enableClient", "username": usuario},
    ]


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


async def sincronizar() -> None:
    """Deja dynamic-security al día y lo reconcilia con modulo9.

    Corre en cada conexión del gateway. Lee el estado y escribe solo lo que
    difiere: en Mosquitto 2.1.2 modifyClient y setClientPassword desconectan al
    cliente aunque no cambie nada, y modifyRole desconecta a quien tenga ese rol
    (medido). Reescribir todo en cada conexión expulsaría al gateway y a las
    Raspberry legacy una y otra vez.
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
        # apagado (rotarla acá desconectaría a todas las Raspberry legacy).
        comandos.append(
            {
                "command": "createClient",
                "username": settings.mqtt_device_username,
                "password": settings.mqtt_device_password,
                "roles": [{"rolename": ROL_LEGACY}],
            }
        )

    # La BD manda: credencial de un serial inactivo o inexistente, deshabilitada;
    # rol de un serial inactivo, borrado (le quita esos topics a quien los tenga).
    async with async_session_factory() as session:
        estados = await registry.estado_seriales(session)
    activos = {serial for serial, activo in estados.items() if activo}
    for usuario, cliente in clientes.items():
        if usuario == settings.mqtt_username:
            continue
        if ROL_LEGACY in _roles_de(cliente):
            if not legacy:
                comandos.append({"command": "deleteClient", "username": usuario})
        elif usuario not in activos and not cliente.get("disabled"):
            comandos.append({"command": "disableClient", "username": usuario})
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


async def emitir(serial: str, adicionales: list[str] | None = None) -> CredencialEmitida:
    """Crea o rota la credencial de la Raspberry `serial` para ella y sus `adicionales`.

    Devuelve la contraseña una sola vez: no se guarda en ningún lado salvo su
    hash dentro de dynamic-security.json. Rotar invalida la clave anterior.
    """
    settings = get_settings()
    seriales = list(dict.fromkeys([serial, *(adicionales or [])]))  # principal primero
    for s in seriales:
        _rechazar_reservado(settings, s)

    async with async_session_factory() as session:
        estados = await registry.estado_seriales(session, seriales)
    for s in seriales:
        if s not in estados:
            raise DeviceNotFoundError(s)
        if not estados[s]:
            raise DeviceInactiveError(s)

    # ponytail: no se quita el rol serial-<S> de otra Raspberry que lo tuviera
    # (mover un nodo de Raspberry); se revoca aparte. Agregar si pasa seguido.
    password = secrets.token_urlsafe(24)
    comandos = [
        c
        for s in seriales
        for c in _asegurar_rol(
            rol_serial(s),
            _acls_dispositivo(settings, s, "subscribeLiteral"),
            allowwildcardsubs=False,
        )
    ]
    comandos += _asegurar_cliente(serial, password, [rol_serial(s) for s in seriales])
    _verificar(await dynsec.ejecutar(comandos), _YA_EXISTE)
    logger.info("Credencial MQTT emitida para %s (seriales=%s)", serial, seriales)
    return CredencialEmitida(usuario=serial, password=password, seriales=seriales)


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
    quita sus topics a cualquier Raspberry que transmita por él. Idempotente."""
    _rechazar_reservado(get_settings(), serial)
    respuestas = await dynsec.ejecutar(
        [
            {"command": "disableClient", "username": serial},
            {"command": "deleteRole", "rolename": rol_serial(serial)},
        ]
    )
    _verificar(respuestas, _NO_EXISTE)
    logger.info("Credencial MQTT revocada para %s", serial)
