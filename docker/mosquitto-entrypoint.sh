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

# TLS (SEG-BROKER-02): cifrado en tránsito para el tramo expuesto a
# internet (dispositivos IoT conectando directo al broker). mosquitto.conf
# incluye /mosquitto/config/conf.d/*.conf (include_dir) — acá se genera un
# listener TLS por cada protocolo SOLO si hay certificados montados en
# /mosquitto/certs (fullchain.pem + privkey.pem, convención Let's
# Encrypt/certbot). Sin certs (ej. dev), el directorio queda vacío y el
# broker sigue sirviendo solo texto plano en 1883/9001 — no falla el
# arranque por esto.
#
# Ops decide cuándo "forzar" TLS de verdad: montar los certs acá activa el
# listener cifrado en 8883/9002 SIN apagar el listener en texto plano —
# apagarlo (o dejar de publicar 1884/9001 al host) es un paso aparte, para
# no desconectar en producción dispositivos que todavía no migraron.
CONF_D="/mosquitto/config/conf.d"
mkdir -p "$CONF_D"
rm -f "$CONF_D"/tls.conf

TLS_CERT="/mosquitto/certs/fullchain.pem"
TLS_KEY="/mosquitto/certs/privkey.pem"
# Si ya existe (SEG-BROKER-01, ACL de privilegio mínimo), se referencia acá
# también para que los listeners TLS queden igual de restringidos que los
# de texto plano — sin acoplar esta rama a esa: si el archivo no existe
# todavía, simplemente se omite la línea.
ACL_FILE="/mosquitto/secrets/acl"

if [ -f "$TLS_CERT" ] && [ -f "$TLS_KEY" ]; then
  {
    echo "# Generado por mosquitto-entrypoint.sh — no editar a mano, se sobreescribe en cada arranque."
    echo ""
    echo "listener 8883"
    echo "protocol mqtt"
    echo "certfile $TLS_CERT"
    echo "keyfile $TLS_KEY"
    echo "allow_anonymous false"
    echo "password_file $PASSWD_FILE"
    if [ -f "$ACL_FILE" ]; then echo "acl_file $ACL_FILE"; fi
    echo ""
    echo "listener 9002"
    echo "protocol websockets"
    echo "certfile $TLS_CERT"
    echo "keyfile $TLS_KEY"
    echo "allow_anonymous false"
    echo "password_file $PASSWD_FILE"
    if [ -f "$ACL_FILE" ]; then echo "acl_file $ACL_FILE"; fi
  } > "$CONF_D/tls.conf"
  echo "TLS habilitado: escuchando mqtts en 8883 y wss en 9002."
else
  echo "AVISO: no hay certificados en /mosquitto/certs ($TLS_CERT / $TLS_KEY) — el broker sirve MQTT sin cifrar en 1883/9001. No usar así en producción (SEG-BROKER-02)." >&2
fi

exec /docker-entrypoint.sh "$@"
