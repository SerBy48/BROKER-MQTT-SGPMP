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

| Puerto (contenedor) | Protocolo | Puerto host (override) |
|---|---|---|
| 8883 | MQTT sobre TLS (mqtts) | `MQTT_TLS_HOST_PORT` (default `8883`) |
| 9002 | WebSocket sobre TLS (wss) | `MQTT_WSS_HOST_PORT` (default `9002`) |

Si no hay certificados, el broker sigue sirviendo solo texto plano en
1883/9001 (no falla el arranque) — no usar así en producción.

**Los listeners en texto plano (1883/9001) no se apagan automáticamente**
al agregar certs: eso es una decisión operativa aparte (evita desconectar
en producción dispositivos que todavía no migraron a TLS). Cuando todos
los dispositivos de un ambiente ya soporten TLS, dejar de publicar
`MQTT_HOST_PORT`/`MQTT_WS_HOST_PORT` al host (o cerrarlos en el firewall)
para forzarlo de verdad.
