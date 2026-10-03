#!/bin/sh
# Prepara /mosquitto/secrets/dynamic-security.json (SEG-BROKER-03) y los
# listeners TLS (SEG-BROKER-02) antes de arrancar Mosquitto.
#
# Autenticación y ACL viven en el plugin dynamic-security (ver
# docker/mosquitto.conf). Este script solo garantiza que exista el usuario
# admin, que es el gateway (MQTT_USERNAME/MQTT_PASSWORD). Todo lo demás —el rol
# "gateway", la credencial compartida legacy de los dispositivos, una credencial
# por Raspberry y un rol por serial— lo crea el gateway por
# $CONTROL/dynamic-security/v1 una vez que el broker está arriba
# (app/services/credenciales_mqtt.py): con el broker apagado, mosquitto_ctrl
# solo puede cambiar contraseñas de clientes que ya existen, no crear roles ni
# clientes (verificado contra Mosquitto 2.1.2).
#
# Corre como root (igual que el docker-entrypoint.sh original de la imagen)
# antes de que Mosquitto baje privilegios al usuario "mosquitto", así que puede
# dejar el archivo con el owner/permisos correctos.
set -e

SECRETS_DIR="/mosquitto/secrets"
DYNSEC_FILE="$SECRETS_DIR/dynamic-security.json"

if [ -z "$MQTT_USERNAME" ] || [ -z "$MQTT_PASSWORD" ]; then
  echo "ERROR: MQTT_USERNAME/MQTT_PASSWORD no definidas — no se puede preparar $DYNSEC_FILE" >&2
  exit 1
fi

mkdir -p "$SECRETS_DIR"

if [ ! -f "$DYNSEC_FILE" ]; then
  # Primer arranque en este volumen (o migración desde passwd/acl).
  mosquitto_ctrl dynsec init "$DYNSEC_FILE" "$MQTT_USERNAME" "$MQTT_PASSWORD" >/dev/null
  echo "dynamic-security.json creado con $MQTT_USERNAME como admin."
else
  # Rotación de la clave del gateway desde Dokploy en cada arranque, igual que
  # hacía el passwd. mosquitto_ctrl devuelve 0 aunque el cliente no exista, por
  # eso se revisa la salida.
  SALIDA=$(mosquitto_ctrl -f "$DYNSEC_FILE" dynsec setClientPassword "$MQTT_USERNAME" "$MQTT_PASSWORD" 2>&1 || true)
  if echo "$SALIDA" | grep -qi "not found"; then
    echo "ERROR: MQTT_USERNAME=$MQTT_USERNAME no existe en $DYNSEC_FILE (¿se cambió el usuario del gateway?). Restaurar el usuario anterior o, si se acepta perder todas las credenciales de dispositivos, borrar el archivo del volumen mosquitto_secrets." >&2
    exit 1
  fi
  # Ídem para la credencial compartida legacy. Acá y no en el gateway: cambiar
  # una clave con el broker arriba desconecta a todas las Raspberry que la usan,
  # y al arrancar el broker se reconectan igual. Si todavía no existe, la crea el
  # gateway al conectar (el error "not found" se ignora).
  if [ -n "$MQTT_DEVICE_USERNAME" ] && [ -n "$MQTT_DEVICE_PASSWORD" ]; then
    mosquitto_ctrl -f "$DYNSEC_FILE" dynsec setClientPassword "$MQTT_DEVICE_USERNAME" "$MQTT_DEVICE_PASSWORD" >/dev/null 2>&1 || true
  fi
fi

# Restos del esquema anterior (passwd + acl_file, SEG-BROKER-01). Ya no se leen;
# si se vuelve a la imagen anterior, su entrypoint los regenera.
rm -f "$SECRETS_DIR/passwd" "$SECRETS_DIR/acl"

# El broker reescribe el archivo en caliente: necesita escribir en el directorio.
chown mosquitto:mosquitto "$SECRETS_DIR" "$DYNSEC_FILE"
chmod 0700 "$SECRETS_DIR"
chmod 0600 "$DYNSEC_FILE"

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
# listener cifrado en 8883/9002 SIN apagar el listener en texto plano (el
# gateway lo usa por la red interna de Docker, así que tampoco se puede apagar).
# Lo que se controla es qué puertos se PUBLICAN al host: docker-compose.yml
# publica el puerto del contenedor que indiquen MQTT_PUERTO_PUBLICO (1883 texto
# plano | 8883 TLS) y MQTT_WS_PUERTO_PUBLICO (9001 | 9002). Con 8883/9002 el
# texto plano queda solo dentro de la red de Docker (TC-M09-253).
CONF_D="/mosquitto/config/conf.d"
mkdir -p "$CONF_D"
rm -f "$CONF_D"/tls.conf

TLS_CERT="/mosquitto/certs/fullchain.pem"
TLS_KEY="/mosquitto/certs/privkey.pem"

# TC-M09-253: si se publica un puerto TLS al host pero no hay certificados, el
# puerto quedaría muerto sin que nadie se entere (el listener nunca se crea).
# Es preferible no arrancar a arrancar "sano" y sin canal para los dispositivos.
PUBLICA_TLS=0
if [ "${MQTT_PUERTO_PUBLICO:-1883}" = "8883" ]; then PUBLICA_TLS=1; fi
if [ "${MQTT_WS_PUERTO_PUBLICO:-9001}" = "9002" ]; then PUBLICA_TLS=1; fi
HAY_CERTS=0
if [ -f "$TLS_CERT" ] && [ -f "$TLS_KEY" ]; then HAY_CERTS=1; fi

if [ "$PUBLICA_TLS" = "1" ] && [ "$HAY_CERTS" = "0" ]; then
  echo "ERROR: MQTT_PUERTO_PUBLICO/MQTT_WS_PUERTO_PUBLICO piden publicar TLS (8883/9002) pero no hay certificados en /mosquitto/certs ($TLS_CERT / $TLS_KEY). Se aborta el arranque: sin certificados esos puertos quedarían sin listener (TC-M09-253)." >&2
  exit 1
fi

if [ "${MQTT_PUERTO_PUBLICO:-1883}" = "8883" ] && [ "${MQTT_WS_PUERTO_PUBLICO:-9001}" != "9002" ]; then
  echo "AVISO: MQTT (8883) se publica solo por TLS pero WebSocket (MQTT_WS_PUERTO_PUBLICO=${MQTT_WS_PUERTO_PUBLICO:-9001}) sigue publicando texto plano. Definir MQTT_WS_PUERTO_PUBLICO=9002." >&2
fi
if [ "${MQTT_WS_PUERTO_PUBLICO:-9001}" = "9002" ] && [ "${MQTT_PUERTO_PUBLICO:-1883}" != "8883" ]; then
  echo "AVISO: WebSocket (9002) se publica solo por TLS pero MQTT (MQTT_PUERTO_PUBLICO=${MQTT_PUERTO_PUBLICO:-1883}) sigue publicando texto plano. Definir MQTT_PUERTO_PUBLICO=8883." >&2
fi

if [ "$HAY_CERTS" = "1" ]; then
  # Sin password_file/acl_file: el plugin dynamic-security (global en
  # mosquitto.conf) autentica y aplica la ACL también en estos listeners.
  {
    echo "# Generado por mosquitto-entrypoint.sh — no editar a mano, se sobreescribe en cada arranque."
    echo ""
    echo "listener 8883"
    echo "protocol mqtt"
    echo "certfile $TLS_CERT"
    echo "keyfile $TLS_KEY"
    echo "allow_anonymous false"
    echo ""
    echo "listener 9002"
    echo "protocol websockets"
    echo "certfile $TLS_CERT"
    echo "keyfile $TLS_KEY"
    echo "allow_anonymous false"
  } > "$CONF_D/tls.conf"
  echo "TLS habilitado: escuchando mqtts en 8883 y wss en 9002."
else
  echo "AVISO: no hay certificados en /mosquitto/certs ($TLS_CERT / $TLS_KEY) — el broker sirve MQTT sin cifrar en 1883/9001. No usar así en producción (SEG-BROKER-02)." >&2
fi

exec /docker-entrypoint.sh "$@"
