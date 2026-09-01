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

if [ ! -f "$PASSWD_FILE" ]; then
  if [ -z "$MQTT_USERNAME" ] || [ -z "$MQTT_PASSWORD" ]; then
    echo "ERROR: MQTT_USERNAME/MQTT_PASSWORD no definidas — no se puede generar $PASSWD_FILE" >&2
    exit 1
  fi
  mosquitto_passwd -b -c "$PASSWD_FILE" "$MQTT_USERNAME" "$MQTT_PASSWORD"

  if [ -n "$MQTT_DEVICE_USERNAME" ] && [ -n "$MQTT_DEVICE_PASSWORD" ]; then
    mosquitto_passwd -b "$PASSWD_FILE" "$MQTT_DEVICE_USERNAME" "$MQTT_DEVICE_PASSWORD"
  else
    echo "AVISO: MQTT_DEVICE_USERNAME/MQTT_DEVICE_PASSWORD no definidas — los dispositivos IoT no van a poder autenticarse contra Mosquitto." >&2
  fi

  chown mosquitto:mosquitto "$PASSWD_FILE"
  chmod 0600 "$PASSWD_FILE"
fi

exec /docker-entrypoint.sh "$@"
