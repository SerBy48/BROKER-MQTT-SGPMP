# Guía de conexión — Broker MQTT SGPMP

> **Para:** Líder de IoT / equipo de firmware
>
> Esta es la **plantilla** de la guía de conexión, sin credenciales reales.
> Los valores concretos de cada ambiente (`dev`/`test`/`prod`) se entregan
> por canal privado como `GUIA_CONEXION_IOT_DEV.md` / `_TEST.md` / `_PROD.md`
> — esos archivos SÍ tienen credenciales reales y por eso no se versionan
> (ver `.gitignore`).

Para el contrato técnico completo (topics, payloads, qué falta del lado del
firmware) ver [`INTEGRACION_DISPOSITIVOS_RF23.md`](./INTEGRACION_DISPOSITIVOS_RF23.md)
en este mismo repo — este documento no lo duplica, lo complementa con los
valores concretos de cada ambiente.

---

## 1. Datos de conexión

### MQTT (para el firmware — publicar/suscribir directo a Mosquitto)

| Dato | Valor |
|---|---|
| Host | `<host del ambiente — ver guía privada>` |
| Puerto MQTT (TCP) | `<MQTT_HOST_PORT del ambiente>` — `mqtt` en dev, `mqtts` en test/prod |
| Puerto MQTT (WebSocket) | `<MQTT_WS_HOST_PORT del ambiente>` — `ws` en dev, `wss` en test/prod |
| Usuario | El **serial principal** de la Raspberry (el primero de `EDGE_SERIALS`) |
| Contraseña | La genera la plataforma para esa Raspberry (ver abajo); se muestra una sola vez |
| TLS | `dev` no tiene TLS (texto plano). **En `test`/`prod` el TLS es obligatorio** (TC-M09-253): los puertos publicados hablan solo `mqtts`/`wss`, con los mismos números de siempre; una conexión en texto plano no conecta. Confirmar el puerto en la guía privada del ambiente. |

**Una credencial por Raspberry** (TC-M09-250/251, SEG-BROKER-03). Se genera en
la plataforma, en el detalle del dispositivo IoT → "Credencial MQTT", indicando
también los demás seriales que esa Raspberry transmite si los hay (modelo
"serial por ESP32"). La credencial solo puede publicar en
`telemetry`/`heartbeat`/`status` y suscribirse a `command` **de sus seriales**:
cualquier otro topic se rechaza (un `SUBSCRIBE #` recibe un SUBACK de fallo).
Rotarla invalida la clave anterior; revocarla desconecta a la Raspberry en el
acto. Desactivar el dispositivo en la plataforma también la revoca.

Mientras dura la migración sigue existiendo la credencial **compartida**
`sgpmp_devices` (guía privada del ambiente) con los permisos de siempre, para
que las Raspberry que todavía no tienen la suya no se corten. Se retira cuando
todas migraron; desde ahí solo conectan las credenciales propias.

### HTTP (solo si necesitan probar el API del gateway directamente — normalmente no aplica al firmware)

| Dato | Valor |
|---|---|
| Health check | `<base URL del ambiente>/v1/healthz` |
| Base URL | `<ver guía privada del ambiente>` |

---

## 2. Convención de topics

```
sgpmp/<serial>/telemetry    # IoT -> SGPMP (telemetría)
sgpmp/<serial>/heartbeat    # IoT -> SGPMP (heartbeat)
sgpmp/<serial>/status       # IoT -> SGPMP (ACK / cambio de estado)
sgpmp/<serial>/command      # SGPMP -> IoT (comando)
```

El `<serial>` debe coincidir exactamente con el registrado en el sistema
para ese dispositivo (ej. `IOT-EST01-HLA-001`, tabla
`modulo9.dispositivos_iot`).

Los contratos de payload completos (telemetría, heartbeat, comando, ACK)
están en `INTEGRACION_DISPOSITIVOS_RF23.md` y en el `README.md` de este
repo — no se repiten acá para no tener dos fuentes de verdad desincronizadas.

**Novedad (TC-M09-252):** el comando trae `id_comando` y `emitido_en`, y el
dispositivo debe devolver `id_comando` en el ACK y no re-aplicar un comando ya
procesado. Detalle y motivo en `INTEGRACION_DISPOSITIVOS_RF23.md`, pasos 2 y 3.

---

## 3. Probar la conexión sin hardware real

Con cualquier cliente MQTT (`mosquitto_pub`/`mosquitto_sub`, MQTT Explorer,
etc.), con la credencial generada para ese serial. Usar MQTT v5 (`-V mqttv5`):
en v3.1.1 un publish rechazado igual responde "OK" y engaña.

```bash
# Suscribirse a comandos de un dispositivo de prueba
mosquitto_sub -h <host> -p <puerto> -V mqttv5 \
  -u IOT-TEST-001 -P '<contraseña generada>' \
  -t "sgpmp/IOT-TEST-001/command"

# Publicar un heartbeat de prueba
mosquitto_pub -h <host> -p <puerto> -V mqttv5 \
  -u IOT-TEST-001 -P '<contraseña generada>' \
  -t "sgpmp/IOT-TEST-001/heartbeat" \
  -m '{"tipo_mensaje":"HEARTBEAT","nivel_bateria_pct":87.5}' -q 1
```

`IOT-TEST-001` debe existir como `serial` real en `modulo9.dispositivos_iot`
para que el sistema lo reconozca — si necesitas un serial de prueba que no
exista todavía, pídelo (ver sección 5).

---

## 4. Limitaciones conocidas (léelo antes de reportar como bug)

- **La credencial compartida `sgpmp_devices` sigue abierta hasta que todas
  las Raspberry migren.** Con ella todavía se puede leer el `command` de otro
  serial (TC-M09-250/251); con una credencial propia no. Diseño y mediciones en
  [`docs/RFC_credencial_mqtt_por_dispositivo.md`](./docs/RFC_credencial_mqtt_por_dispositivo.md).
- **`dev` no tiene TLS; `test`/`prod` solo aceptan TLS.** En `dev` el
  tráfico MQTT va sin cifrar (no tiene certificados montados). En `test`/
  `prod` (TC-M09-253) el host publica únicamente los listeners TLS, en los
  puertos de siempre: el cliente debe conectar con `mqtts`/`wss` y validar el
  certificado; en texto plano no hay respuesta.
- **Sin reenvío automático de comandos.** Si un dispositivo estaba offline
  cuando se le envió un comando, alguien tiene que reintentarlo manualmente
  desde la UI una vez que el dispositivo reconecta — no está construido el
  mecanismo automático todavía.
- **El contrato de payload del ACK está propuesto, no confirmado con
  ustedes.** Si el firmware ya tiene otro formato definido, es un cambio de
  una función en el broker, no de arquitectura — avisar.
- **Los valores de cada ambiente son independientes.** `dev`/`test`/`prod`
  tienen host, puertos y credenciales distintas — cada uno se entrega por
  aparte cuando ese ambiente esté listo.

---

## 5. Cómo pedir algo (soporte, cambios, ambientes nuevos)

Depende de qué estás pidiendo:

| Qué necesitas | A quién / cómo |
|---|---|
| El broker no responde, error de conexión, credencial no funciona | Reportar directo al equipo de desarrollo/backend — es un problema operativo, no requiere trámite formal. |
| Un `serial` de prueba nuevo, o dudas sobre un `serial` existente | Al equipo de backend — se gestiona en `modulo9.dispositivos_iot`, no lo maneja este servicio. |
| Cambiar el formato de un payload (ACK, telemetría, etc.) porque el firmware ya tiene otro definido | Avisar al equipo de backend/broker — confirmado en la sección "Qué NO tienen que hacer" de `INTEGRACION_DISPOSITIVOS_RF23.md`: es un cambio acotado (una función), no necesita RFC. |
| Un ambiente `test`/`prod` para el broker | Al equipo de backend — se coordina el deploy igual que se hizo con `dev`. |
| Cambiar algo que sí toca el **requerimiento** (ej. agregar revocación por dispositivo, agregar reenvío automático, cambiar la convención de topics de forma estructural) | Esto sí requiere una **solicitud de cambio formal (RFC)** — es el proceso que ya usa el equipo para cualquier cambio que afecte requerimientos o arquitectura. Repórtalo al Analista/líder de análisis con la descripción del gap; el RFC queda documentado en `docs-sgpmp/1-analisis/gestion-cambios/`. |

En general: **si es un problema operativo o un ajuste técnico chico, repórtalo directo** — no hace falta ceremonia. **Si cambia lo que el sistema debe hacer** (no solo cómo), pasa por RFC para que quede trazado.
