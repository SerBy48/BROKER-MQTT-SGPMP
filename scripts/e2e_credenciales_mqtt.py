"""E2E de SEG-BROKER-03 (TC-M09-250/251): gateway real contra Mosquitto real.

Usa el cliente MQTT y el servicio de credenciales del gateway contra el broker
levantado con el docker-compose del repo; el "Gateway Edge" son los
mosquitto_pub/sub del host (MQTT v5). No toca la BD: la relación Edge ->
dispositivos (modulo9.dispositivos_iot.id_dispositivo_gateway) se toma de las
variables E2E_* (deben ser seriales válidos). Solo publica en `status`, que el
gateway no persiste.

    docker compose -p sgpmp-e2e up -d --build mosquitto   # con MQTT_HOST_PORT=1884
    MQTT_HOST=127.0.0.1 MQTT_PORT=1884 \
    E2E_EDGE=EDGE-REMANSO-01 E2E_ATENDIDOS=IOT-EST01-HLA-001,IOT-EST02-HLA-002 \
    E2E_DIRECTO=IOT-ALE01-HLA-003 E2E_INACTIVO=IOT-CAM01-COR-001 \
        .venv/bin/python scripts/e2e_credenciales_mqtt.py
    docker compose -p sgpmp-e2e down -v

Usar un volumen nuevo (down -v) también prueba el primer arranque.
"""

import asyncio
import os
import subprocess
import sys

sys.path.insert(0, os.getcwd())

from app.config import get_settings  # noqa: E402
from app.core.errors import DeviceDependsOnGatewayError  # noqa: E402
from app.db.repositories.registry import EstadoDispositivo  # noqa: E402
from app.mqtt.client import mqtt_gateway  # noqa: E402
from app.services import credenciales_mqtt as cm  # noqa: E402

H = ["-h", "127.0.0.1", "-p", os.environ.get("MQTT_PORT", "1884"), "-V", "mqttv5"]
resultados: list[tuple[str, bool]] = []


def check(nombre: str, ok: bool, detalle: str = "") -> None:
    resultados.append((nombre, ok))
    print(f"{'PASA' if ok else 'FALLA'}  {nombre}  {detalle}".rstrip())


def sub_denegado(user: str, pw: str, topic: str) -> bool | None:
    try:
        r = subprocess.run(
            ["mosquitto_sub", *H, "-u", user, "-P", pw, "-t", topic, "-C", "1"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return "denied" in (r.stdout + r.stderr)
    except subprocess.TimeoutExpired:
        return False  # suscripción concedida, esperando mensajes


def pub(user: str, pw: str, topic: str) -> str:
    r = subprocess.run(
        ["mosquitto_pub", *H, "-u", user, "-P", pw, "-t", topic, "-q", "1", "-m", '{"e2e":1}'],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return (r.stdout + r.stderr).strip()


def escuchar(user: str, pw: str, topic: str) -> subprocess.Popen:
    return subprocess.Popen(
        ["mosquitto_sub", *H, "-u", user, "-P", pw, "-t", topic, "-v"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


EDGE = os.environ["E2E_EDGE"]
ATENDIDOS = os.environ["E2E_ATENDIDOS"].split(",")
DIRECTO = os.environ["E2E_DIRECTO"]
INACTIVO = os.environ.get("E2E_INACTIVO")

# modulo9 simulado: serial -> (activo, serial de su Gateway Edge)
BD = {
    EDGE: EstadoDispositivo(True, None),
    DIRECTO: EstadoDispositivo(True, None),
    **{s: EstadoDispositivo(True, EDGE) for s in ATENDIDOS},
}
if INACTIVO:
    BD[INACTIVO] = EstadoDispositivo(False, None)


async def _mapa(_session):
    return dict(BD)


class _Sesion:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


cm.registry.mapa_dispositivos = _mapa
cm.async_session_factory = _Sesion


async def esperar_gateway_estable(s) -> bool:
    # Primer arranque: el gateway se asigna su rol, Mosquitto lo desconecta y al
    # reconectar (MQTT_RECONNECT_DELAY) termina de sincronizar.
    for _ in range(100):
        t = mqtt_gateway._sync_task
        if t is not None and t.done() and t.exception() is None and mqtt_gateway._client:
            try:
                (gw,) = await cm.dynsec.ejecutar(
                    [{"command": "getClient", "username": s.mqtt_username}]
                )
                (lc,) = await cm.dynsec.ejecutar([{"command": "listClients"}])
                roles = {r["rolename"] for r in gw["data"]["client"]["roles"]}
                if "gateway" in roles and s.mqtt_device_username in lc["data"]["clients"]:
                    return True
            except Exception:
                pass
        await asyncio.sleep(0.2)
    return False


async def main() -> None:
    s = get_settings()
    a1, a2 = ATENDIDOS[0], ATENDIDOS[-1]
    print(f"Edge={EDGE} atiende={ATENDIDOS} directo={DIRECTO} inactivo={INACTIVO}\n")

    mqtt_gateway.start()
    check("gateway conecta, se asigna su rol y sincroniza", await esperar_gateway_estable(s))

    lu, lp = s.mqtt_device_username, s.mqtt_device_password
    check(
        "legacy: publica status (sin cortar a nadie)",
        "failed" not in pub(lu, lp, f"sgpmp/{a1}/status"),
    )
    check("legacy: SUBSCRIBE '#' denegado", sub_denegado(lu, lp, "#") is True)

    # --- credencial del Edge ---
    cred = await cm.emitir(EDGE)
    u, p = cred.usuario, cred.password
    check("emitir: usuario = serial del Edge", u == EDGE)
    check(
        "emitir: cubre el Edge y los dispositivos que atiende (de la BD)",
        cred.seriales == [EDGE, *sorted(ATENDIDOS)],
    )

    check("TC-251: SUBSCRIBE '#' -> SUBACK de fallo", sub_denegado(u, p, "#") is True)
    check(
        "TC-251: SUBSCRIBE 'sgpmp/+/command' -> fallo",
        sub_denegado(u, p, "sgpmp/+/command") is True,
    )
    check(
        "TC-251: command de un dispositivo que no atiende -> fallo",
        sub_denegado(u, p, f"sgpmp/{DIRECTO}/command") is True,
    )
    check(
        "SUBSCRIBE command de un dispositivo que atiende -> concedido",
        sub_denegado(u, p, f"sgpmp/{a1}/command") is False,
    )
    check(
        "TC-250: publicar en status de un dispositivo ajeno -> Not authorized",
        "Not authorized" in pub(u, p, f"sgpmp/{DIRECTO}/status"),
    )
    check(
        "TC-250: publicar en command -> Not authorized",
        "Not authorized" in pub(u, p, f"sgpmp/{a1}/command"),
    )
    check(
        "publicar en status de un dispositivo que atiende -> OK",
        "failed" not in pub(u, p, f"sgpmp/{a2}/status"),
    )

    # entrega real: el Edge recibe el comando de su dispositivo, no el de uno ajeno
    edge = escuchar(u, p, f"sgpmp/{a1}/command")
    await asyncio.sleep(0.7)
    await mqtt_gateway.publish(f"sgpmp/{a1}/command", b'{"para":"atendido"}')
    await mqtt_gateway.publish(f"sgpmp/{DIRECTO}/command", b'{"para":"ajeno"}')
    await asyncio.sleep(1)
    edge.terminate()
    salida = edge.communicate()[0]
    check("RF-23: el comando de su dispositivo le llega al Edge", "atendido" in salida)
    check("no recibe el comando de un dispositivo ajeno", "ajeno" not in salida)

    try:
        await cm.emitir(a1)
        check("un dispositivo del Edge no tiene credencial propia", False)
    except DeviceDependsOnGatewayError:
        check("un dispositivo del Edge no tiene credencial propia", True)

    # --- asignar un dispositivo al Edge y sincronizar SIN rotar la clave ---
    check(
        "antes de asignarlo, el Edge no puede usar el topic de DIRECTO",
        sub_denegado(u, p, f"sgpmp/{DIRECTO}/command") is True,
    )
    BD[DIRECTO] = EstadoDispositivo(True, EDGE)  # PATCH /{id}/gateway en el backend
    await cm.sincronizar_credencial(EDGE)
    check(
        "asignado + sync: el Edge ya puede usar el topic de DIRECTO, con la MISMA clave",
        sub_denegado(u, p, f"sgpmp/{DIRECTO}/command") is False,
    )
    BD[DIRECTO] = EstadoDispositivo(True, None)
    await cm.sincronizar_credencial(EDGE)
    check(
        "quitado + sync: el Edge vuelve a no poder",
        sub_denegado(u, p, f"sgpmp/{DIRECTO}/command") is True,
    )

    # --- rotación ---
    nueva = await cm.emitir(EDGE)
    check("rotar: clave vieja rechazada", "Not authorized" in pub(u, p, f"sgpmp/{a1}/status"))
    check(
        "rotar: clave nueva funciona", "failed" not in pub(u, nueva.password, f"sgpmp/{a1}/status")
    )

    # --- desactivar un dispositivo del Edge: pierde sus topics ---
    await cm.revocar(a2)  # lo que hace el backend al desactivarlo
    check(
        "dispositivo desactivado: el Edge pierde su topic",
        sub_denegado(u, nueva.password, f"sgpmp/{a2}/command") is True,
    )
    check(
        "los demás dispositivos del Edge siguen",
        sub_denegado(u, nueva.password, f"sgpmp/{a1}/command") is False,
    )

    # --- desactivar el Edge (cascada): desconexión inmediata ---
    edge = escuchar(u, nueva.password, f"sgpmp/{a1}/command")
    await asyncio.sleep(0.7)
    await cm.revocar(EDGE)
    await asyncio.sleep(1)
    desconectado = edge.poll() is not None
    if not desconectado:
        edge.terminate()
    check(
        "Edge desactivado: queda desconectado", desconectado, (edge.communicate()[0] or "").strip()
    )
    check(
        "Edge desactivado: no puede volver a conectar",
        "Not authorized" in pub(u, nueva.password, f"sgpmp/{a1}/status"),
    )

    # --- reconciliación: credencial de un serial inactivo se corta al reconectar ---
    if INACTIVO:
        await cm.dynsec.ejecutar(
            [{"command": "createClient", "username": INACTIVO, "password": "clave-vieja-123"}]
        )
        await cm.sincronizar()
        estado = await cm.consultar(INACTIVO)
        check(
            "reconciliación: serial inactivo en modulo9 -> deshabilitada",
            estado is not None and not estado.habilitada,
        )

    await mqtt_gateway.stop()
    fallas = [n for n, ok in resultados if not ok]
    print(f"\n{len(resultados) - len(fallas)}/{len(resultados)} pasan")
    sys.exit(1 if fallas else 0)


asyncio.run(main())
