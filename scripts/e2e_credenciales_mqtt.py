"""E2E de SEG-BROKER-03 (TC-M09-250/251): gateway real contra Mosquitto real.

Usa el cliente MQTT y el servicio de credenciales del gateway contra el broker
levantado con el docker-compose del repo; la "Raspberry" son los
mosquitto_pub/sub del host (MQTT v5). No toca la BD: el estado de los seriales
se toma de E2E_ACTIVOS / E2E_INACTIVO (deben ser seriales válidos). Solo publica
en `status`, que el gateway no persiste.

    docker compose -p sgpmp-e2e up -d --build mosquitto   # con MQTT_HOST_PORT=1884
    MQTT_HOST=127.0.0.1 MQTT_PORT=1884 \
    E2E_ACTIVOS=IOT-EST01-HLA-001,IOT-EST02-HLA-002,IOT-ALE01-HLA-003 \
    E2E_INACTIVO=IOT-CAM01-COR-001 \
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
from app.db.engine import async_session_factory  # noqa: E402
from app.db.repositories import registry  # noqa: E402
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


ESTADOS = {s: True for s in os.environ["E2E_ACTIVOS"].split(",")}
if os.environ.get("E2E_INACTIVO"):
    ESTADOS[os.environ["E2E_INACTIVO"]] = False


async def _estado_seriales(_session, seriales=None):
    return dict(ESTADOS) if seriales is None else {k: ESTADOS[k] for k in seriales if k in ESTADOS}


class _Sesion:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


cm.registry.estado_seriales = _estado_seriales
cm.async_session_factory = _Sesion
async_session_factory = _Sesion  # noqa: F811
registry.estado_seriales = _estado_seriales


async def main() -> None:
    s = get_settings()
    async with async_session_factory() as session:
        estados = await registry.estado_seriales(session)
    activos = sorted(k for k, v in estados.items() if v)
    inactivos = sorted(k for k, v in estados.items() if not v)
    principal, adicional, ajeno = activos[0], activos[1], activos[2]
    print(f"principal={principal} adicional={adicional} ajeno={ajeno} inactivo={inactivos[:1]}\n")

    mqtt_gateway.start()
    # Primer arranque: el gateway se asigna su rol, Mosquitto lo desconecta y al
    # reconectar (MQTT_RECONNECT_DELAY) termina de sincronizar. Esperar a que
    # quede estable: sincronización completa con el rol ya asignado.
    estable, conexiones = False, set()
    for _ in range(100):
        t = mqtt_gateway._sync_task
        if t is not None:
            conexiones.add(id(t))
        if t is not None and t.done() and t.exception() is None and mqtt_gateway._client:
            try:
                (gw,) = await cm.dynsec.ejecutar(
                    [{"command": "getClient", "username": s.mqtt_username}]
                )
                (lc,) = await cm.dynsec.ejecutar([{"command": "listClients"}])
                roles = {r["rolename"] for r in gw["data"]["client"]["roles"]}
                if "gateway" in roles and s.mqtt_device_username in lc["data"]["clients"]:
                    estable = True
                    break
            except Exception:
                pass
        await asyncio.sleep(0.2)
    check(
        "gateway conecta, se asigna su rol y sincroniza",
        estable,
        f"(sincronizaciones: {len(conexiones)})",
    )

    # --- legacy: las Raspberry actuales siguen conectando ---
    lu, lp = s.mqtt_device_username, s.mqtt_device_password
    check(
        "legacy: publica status de cualquier serial (sin cortar a nadie)",
        "failed" not in pub(lu, lp, f"sgpmp/{principal}/status"),
    )
    check("legacy: SUBSCRIBE '#' denegado (antes se concedía)", sub_denegado(lu, lp, "#") is True)

    # --- credencial propia ---
    cred = await cm.emitir(principal, [adicional])
    u, p = cred.usuario, cred.password
    check(
        "emitir devuelve usuario = serial principal",
        u == principal and cred.seriales == [principal, adicional],
    )

    check("TC-251: SUBSCRIBE '#' -> SUBACK de fallo", sub_denegado(u, p, "#") is True)
    check(
        "TC-251: SUBSCRIBE 'sgpmp/+/command' -> fallo",
        sub_denegado(u, p, "sgpmp/+/command") is True,
    )
    check(
        "TC-251: SUBSCRIBE command de otro serial -> fallo",
        sub_denegado(u, p, f"sgpmp/{ajeno}/command") is True,
    )
    check(
        "SUBSCRIBE su propio command -> concedido",
        sub_denegado(u, p, f"sgpmp/{principal}/command") is False,
    )
    check(
        "SUBSCRIBE command del serial adicional -> concedido",
        sub_denegado(u, p, f"sgpmp/{adicional}/command") is False,
    )

    check(
        "TC-250: publicar en status de otro serial -> Not authorized",
        "Not authorized" in pub(u, p, f"sgpmp/{ajeno}/status"),
    )
    check(
        "TC-250: publicar en command (aunque sea el propio) -> Not authorized",
        "Not authorized" in pub(u, p, f"sgpmp/{principal}/command"),
    )
    check(
        "publicar en su propio status -> OK", "failed" not in pub(u, p, f"sgpmp/{principal}/status")
    )
    check(
        "publicar en status del serial adicional -> OK",
        "failed" not in pub(u, p, f"sgpmp/{adicional}/status"),
    )

    # entrega real: solo recibe sus comandos
    rasp = escuchar(u, p, f"sgpmp/{principal}/command")
    await asyncio.sleep(0.7)
    await mqtt_gateway.publish(f"sgpmp/{principal}/command", b'{"para":"propio"}')
    await mqtt_gateway.publish(f"sgpmp/{ajeno}/command", b'{"para":"ajeno"}')
    await asyncio.sleep(1)
    rasp.terminate()
    salida = rasp.communicate()[0]
    check("recibe el comando del gateway para su serial", "propio" in salida)
    check("no recibe el comando de otro serial", "ajeno" not in salida)

    estado = await cm.consultar(principal)
    check(
        "consultar: habilitada",
        estado is not None and estado.habilitada and estado.seriales == [principal, adicional],
    )

    # --- rotación: la clave vieja deja de servir ---
    nueva = await cm.emitir(principal, [adicional])
    check(
        "rotar: clave vieja rechazada",
        "Not authorized" in pub(u, p, f"sgpmp/{principal}/status")
        or "refused" in pub(u, p, f"sgpmp/{principal}/status").lower(),
    )
    check(
        "rotar: clave nueva funciona",
        "failed" not in pub(u, nueva.password, f"sgpmp/{principal}/status"),
    )

    # --- revocación: desconecta al instante ---
    rasp = escuchar(u, nueva.password, f"sgpmp/{principal}/command")
    await asyncio.sleep(0.7)
    await cm.revocar(principal)
    await asyncio.sleep(1)
    desconectada = rasp.poll() is not None
    if not desconectada:
        rasp.terminate()
    check(
        "revocar: la Raspberry conectada queda desconectada",
        desconectada,
        (rasp.communicate()[0] or "").strip(),
    )
    check(
        "revocar: no puede volver a conectar",
        "Not authorized" in pub(u, nueva.password, f"sgpmp/{principal}/status"),
    )
    estado = await cm.consultar(principal)
    check("consultar tras revocar: deshabilitada", estado is not None and not estado.habilitada)

    # --- reconciliación: credencial de un serial inactivo se corta al reconectar ---
    if inactivos:
        viejo = inactivos[0]
        # se crea "a mano" (como si se hubiera emitido antes de desactivarlo)
        await cm.dynsec.ejecutar(cm._asegurar_cliente(viejo, "clave-vieja-123", []))
        await cm.sincronizar()
        e = await cm.consultar(viejo)
        check(
            "reconciliación: serial inactivo en modulo9 -> credencial deshabilitada",
            e is not None and not e.habilitada,
        )

    await mqtt_gateway.stop()
    fallas = [n for n, ok in resultados if not ok]
    print(f"\n{len(resultados) - len(fallas)}/{len(resultados)} pasan")
    sys.exit(1 if fallas else 0)


asyncio.run(main())
