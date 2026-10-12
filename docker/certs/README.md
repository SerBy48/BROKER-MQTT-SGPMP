# Certificados TLS del broker (SEG-BROKER-02)

Este directorio se monta en `/mosquitto/certs` dentro del contenedor de
Mosquitto (ver `docker-compose.yml`). Está vacío a propósito — los
certificados reales (clave privada incluida) **no se versionan**, los
entrega Ops por ambiente (dev/test/prod), igual que las credenciales de
`.env`.

Para activar TLS en un ambiente, colocar acá (convención Let's
Encrypt/certbot):

```
docker/certs/fullchain.pem   # certificado + cadena
docker/certs/privkey.pem     # clave privada
```

y reiniciar el contenedor `mosquitto`. El entrypoint
(`docker/mosquitto-entrypoint.sh`) detecta ambos archivos en el arranque y
genera automáticamente los listeners cifrados:

| Puerto (contenedor) | Protocolo |
|---|---|
| 8883 | MQTT sobre TLS (mqtts) |
| 9002 | WebSocket sobre TLS (wss) |

Se publican al host según la sección "Modo solo TLS" de abajo.

Si no hay certificados, el broker sigue sirviendo solo texto plano en
1883/9001 (no falla el arranque) — no usar así en producción.

**Los listeners en texto plano (1883/9001) no se apagan automáticamente**
al agregar certs: eso es una decisión operativa aparte (evita desconectar
en producción dispositivos que todavía no migraron a TLS). Cuando todos
los dispositivos de un ambiente ya soporten TLS, dejar de publicar
`MQTT_HOST_PORT`/`MQTT_WS_HOST_PORT` al host (o cerrarlos en el firewall)
para forzarlo de verdad.

## Modo "solo TLS" hacia afuera (TC-M09-253) — test/prod

`docker-compose.yml` publica al host el puerto del contenedor que indiquen
estas dos variables (por defecto `1883`/`9001`, texto plano, como en `dev`):

```
MQTT_PUERTO_PUBLICO=8883        # MQTT -> mqtts
MQTT_WS_PUERTO_PUBLICO=9002     # WebSocket -> wss
```

Con eso el host publica **solo** los listeners TLS, **en los mismos puertos de
host de siempre** (`MQTT_HOST_PORT` / `MQTT_WS_HOST_PORT`): no hay que tocar el
firewall ni el puerto que tienen configurado los dispositivos, solo que ahora
hablan TLS. El texto plano (1883/9001) sigue existiendo dentro de la red de
Docker porque el gateway lo usa así, pero ya no es alcanzable desde afuera.

Si se pide TLS y faltan `fullchain.pem`/`privkey.pem`, el entrypoint **aborta el
arranque** (código 1) en vez de dejar un puerto publicado sin listener. Verificado
con un certificado autofirmado: conexiones `tcp` y `websockets` en texto plano
con credenciales válidas no reciben CONNACK; `mqtts` y `wss` conectan.

Para un ambiente que necesite **ambos** modos durante una migración, hay que
descomentar en `docker-compose.yml` los mapeos `MQTT_TLS_HOST_PORT` /
`MQTT_WSS_HOST_PORT` y asignarles puertos de host libres (en el servidor
compartido el 8883 lo ocupa otro proyecto).
