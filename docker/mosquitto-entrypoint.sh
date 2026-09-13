#!/bin/sh
# Genera /mosquitto/config/passwd en cada arranque a partir de
# MQTT_USERNAME/MQTT_PASSWORD (gateway) y, si están definidas,
# MQTT_DEVICE_USERNAME/MQTT_DEVICE_PASSWORD (credencial compartida para los
# dispositivos IoT — se conectan directo a Mosquitto, no pasan por el
# gateway HTTP, así que necesitan su propio usuario MQTT).
#
# docker/passwd está en .gitignore a propósito (son credenciales, no se
# versionan) — eso significa que en un clone nuevo (como el que hace
# Dokploy) el archivo no existe. Sin este script, Docker crea un
# directorio vacío en su lugar al montar el bind mount inexistente, y
# Mosquitto falla con "passwd is not a file". Este entrypoint corre como
# root (igual que el docker-entrypoint.sh original de la imagen) antes de
# que Mosquitto baje privilegios al usuario "mosquitto", así que puede
# crear el archivo con el owner/permisos correctos.
set -e

PASSWD_FILE="/mosquitto/secrets/passwd"

# "upsert", no "crear una vez": corre en CADA arranque, no solo cuando el
# archivo no existe. Con el volumen persistente (mosquitto_secrets), un
# passwd generado en un deploy viejo (ej. antes de que existiera
# MQTT_DEVICE_USERNAME) nunca ganaba los usuarios/contraseñas agregados
# después — "if [ ! -f ... ]" lo saltaba siempre. mosquitto_passwd -b sin
# -c ya hace upsert (agrega si no está, actualiza si sí), así que esto
# también aplica rotaciones de contraseña en cada redeploy, no solo altas.
if [ -z "$MQTT_USERNAME" ] || [ -z "$MQTT_PASSWORD" ]; then
  echo "ERROR: MQTT_USERNAME/MQTT_PASSWORD no definidas — no se puede generar $PASSWD_FILE" >&2
  exit 1
fi

if [ ! -f "$PASSWD_FILE" ]; then
  mosquitto_passwd -b -c "$PASSWD_FILE" "$MQTT_USERNAME" "$MQTT_PASSWORD"
else
  mosquitto_passwd -b "$PASSWD_FILE" "$MQTT_USERNAME" "$MQTT_PASSWORD"
fi

if [ -n "$MQTT_DEVICE_USERNAME" ] && [ -n "$MQTT_DEVICE_PASSWORD" ]; then
  mosquitto_passwd -b "$PASSWD_FILE" "$MQTT_DEVICE_USERNAME" "$MQTT_DEVICE_PASSWORD"
else
  echo "AVISO: MQTT_DEVICE_USERNAME/MQTT_DEVICE_PASSWORD no definidas — los dispositivos IoT no van a poder autenticarse contra Mosquitto." >&2
fi

chown mosquitto:mosquitto "$PASSWD_FILE"
chmod 0600 "$PASSWD_FILE"

# ACL (SEG-BROKER-01): sin esto, cualquier cliente autenticado —incluida la
# credencial compartida de los dispositivos, embebida en firmware— puede
# SUBSCRIBE # y leer/escribir en cualquier topic. Se regenera en cada
# arranque (mismo motivo que el passwd: los nombres de usuario vienen de
# env vars de Dokploy, no son fijos, y no se versiona el archivo resultante).
#
# Prefijo/sufijos de topic con los mismos defaults que app/config.py —
# deben coincidir con lo que usa el gateway (Settings.mqtt_topic_*).
ACL_FILE="/mosquitto/secrets/acl"
TOPIC_PREFIX="${MQTT_TOPIC_PREFIX:-sgpmp}"
TOPIC_TELEMETRY="${MQTT_TOPIC_TELEMETRY:-telemetry}"
TOPIC_HEARTBEAT="${MQTT_TOPIC_HEARTBEAT:-heartbeat}"
TOPIC_STATUS="${MQTT_TOPIC_STATUS:-status}"
TOPIC_COMMAND="${MQTT_TOPIC_COMMAND:-command}"

{
  echo "# Generado por mosquitto-entrypoint.sh — no editar a mano, se sobreescribe en cada arranque."
  echo ""
  echo "user $MQTT_USERNAME"
  echo "topic read $TOPIC_PREFIX/+/$TOPIC_TELEMETRY"
  echo "topic read $TOPIC_PREFIX/+/$TOPIC_HEARTBEAT"
  echo "topic read $TOPIC_PREFIX/+/$TOPIC_STATUS"
  echo "topic write $TOPIC_PREFIX/+/$TOPIC_COMMAND"

  if [ -n "$MQTT_DEVICE_USERNAME" ]; then
    echo ""
    # Privilegio mínimo dado que es una credencial COMPARTIDA por todos los
    # dispositivos (ver INTEGRACION_DISPOSITIVOS_RF23.md): solo pueden
    # escribir en su propio canal (telemetry/heartbeat/status) y leer
    # comandos, nunca suscribirse a telemetry/heartbeat/status de otros.
    #
    # Riesgo residual conocido y aceptado por ahora: al ser una única
    # credencial para todos los seriales, cualquier dispositivo con ella
    # puede leer el topic "command" de OTRO serial (no hay forma de
    # aislarlo sin credenciales por dispositivo). Cierra la brecha grave
    # (leer/inyectar telemetría de toda la red) pero no esta — evaluar
    # credencial por dispositivo para resolverla (ver SEG-BROKER-01).
    echo "user $MQTT_DEVICE_USERNAME"
    echo "topic write $TOPIC_PREFIX/+/$TOPIC_TELEMETRY"
    echo "topic write $TOPIC_PREFIX/+/$TOPIC_HEARTBEAT"
    echo "topic write $TOPIC_PREFIX/+/$TOPIC_STATUS"
    echo "topic read $TOPIC_PREFIX/+/$TOPIC_COMMAND"
  fi
} > "$ACL_FILE"

chown mosquitto:mosquitto "$ACL_FILE"
chmod 0600 "$ACL_FILE"

exec /docker-entrypoint.sh "$@"
