# SGPMP — Gateway MQTT ↔ HTTPS

SGPMP es el **punto de unión** entre el servidor web (aplicación) y el mundo IoT
de la plataforma **AIoT de monitoreo de salud animal**. No mezcla la lógica MQTT
dentro del servidor web: este servicio es el único que habla MQTT (Mosquitto) y
expone un endpoint HTTPS para que el servidor web le envíe comandos. Todo se
persiste en el **PostgreSQL existente** del proyecto (esquemas `modulo3` y
`modulo9`), que el servidor web consulta directamente.

## Arquitectura

```
                    Zona IoT                                  Zona Backend
 ┌──────────────────────────────────────┐        ┌────────────────────────────────┐
 │ Dispositivos / Sensores IoT          │        │ Servidor Web (Backend API)     │
 └──────────────────┬───────────────────┘        └───────────────┬────────────────┘
                    │ MQTT publish                             │ HTTPS (leer/consultar)
                    ▼                                          ▼
          ┌──────────────────┐   subscribe   ┌──────────┐   persistir   ┌──────────────────┐
          │    Mosquitto     │◀──────────────│  SGPMP   │──────────────▶│ PostgreSQL        │
          │  (MQTT broker)   │               │ (gateway)│               │  modulo3 (ingesta)│
          └──────────────────┘   publish     └────┬─────┘               │  modulo9 (registro)│
                    ▲                             │ HTTPS (comandos)    └──────────────────┘
                    │ MQTT subscribe               ▲
                    └─────────────────────────────┘
                                servidor web ──POST /v1/commands──▶ SGPMP ──publish──▶ dispositivo
```

**Dos direcciones:**

1. **IoT → app:** el dispositivo publica telemetría/heartbeat en Mosquitto →
   SGPMP se suscribe → invoca los stored procedures de ingesta de `modulo3` →
   la app lee `modulo3`/`modulo9` directamente (BD compartida, sin webhooks).
2. **app → IoT:** el servidor web hace `POST /v1/commands` → SGPMP publica el
   comando en Mosquitto → registra la configuración remota (`modulo9`).

## Stack

| Pieza | Tecnología |
|---|---|
| Lenguaje | Python 3.11+ |
| API HTTPS | FastAPI + Uvicorn |
| Cliente MQTT | `aiomqtt` (asíncrono) |
| Broker MQTT | Mosquitto (externo) |
| BD | SQLAlchemy 2.0 async + `asyncpg` |
| Config | `pydantic-settings` (`.env`) |

## Estructura del proyecto

```
sgpmp/
├── app/
│   ├── main.py                 # FastAPI + lifecycle (arranca/para MQTT)
│   ├── config.py               # pydantic-settings (.env)
│   ├── schemas.py              # payloads MQTT y contratos de la API
│   ├── api/                    # CAPA HTTPS (solo habla con el servidor web)
│   │   ├── router.py
│   │   ├── dependencies.py     # autenticación Bearer
│   │   └── routes/             # health, commands, devices
│   ├── mqtt/                   # CAPA MQTT (habla con Mosquitto)
│   │   ├── client.py           # conexión + suscripción + loop
│   │   ├── handlers.py         # dispatch por tipo de topic
│   │   └── publisher.py        # publish de comandos
│   ├── services/               # CAPA LÓGICA (orquesta, sin I/O directo)
│   │   ├── ingest.py           # payload → SPs de ingesta
│   │   └── dispatch.py         # comando HTTPS → publish + registro
│   ├── db/                     # CAPA PERSISTENCIA
│   │   ├── engine.py           # engine async + session factory
│   │   └── repositories/       # registry (modulo9), telemetry, commands
│   └── core/                   # logging, errores, enums
├── docker/mosquitto.conf       # Mosquitto de desarrollo
├── docker-compose.yml
├── .env.example
└── pyproject.toml
```

**Regla de separación de capas:** `api` no toca MQTT ni BD directamente; `mqtt`
no toca BD; `services` orquesta y llama a `repositories` y `publisher`. Cada capa
solo depende de la inmediatamente inferior.

## Configuración

```bash
cp .env.example .env
# editar .env con las credenciales reales
```

| Variable | Descripción |
|---|---|
| `DATABASE_URL` | Conexión al PostgreSQL existente (no se migra) |
| `MQTT_HOST` / `MQTT_PORT` | Broker Mosquitto |
| `MQTT_TOPIC_PREFIX` | Prefijo de la convención de topics |
| `MQTT_ACK_TIMEOUT_SECONDS` | Segundos que `/v1/commands` espera el ACK del dispositivo antes de responder `NO_CONF` (RF-23, default 30) |

No hay `API_TOKEN` estático: `/v1/commands` y `/v1/devices` autentican el
Bearer del servidor web contra `modulo1.credenciales_servicio`
(`nombre_servicio='broker_mqtt'`, hash sha256 del token en `hash_valor`) —
la misma base compartida, no un secreto propio de este servicio.

## Ejecución

### Local (venv)

```bash
# entorno virtual (evita instalar en el Python global)
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/Mac

# dependencias
pip install -e ".[dev]"

# broker Mosquitto de desarrollo
docker compose up -d mosquitto

# arrancar la API (y el cliente MQTT)
uvicorn app.main:app --reload
# o
python -m app.main
```

### Docker (broker + gateway, entorno reproducible)

```bash
cp .env.example .env   # ajustar valores
docker compose up -d --build
```

Levanta `mosquitto` y `gateway` (imágenes fijadas por digest, ver `docker-compose.yml`
y `Dockerfile`). Las dependencias de Python quedan fijadas en `requirements.lock`.

## Integración con la base de datos

SGPMP **no crea esquemas ni corre migraciones** (la base ya está versionada con
Alembic en el proyecto principal). Consume:

- **`modulo9`** (registro, solo lectura): `dispositivos_iot` (identidad por
  `serial`), `sensores`, `variables_ambientales`. **`configuraciones_remotas`
  ya no se lee ni se escribe desde acá** (RF-23) — esa tabla es propiedad
  exclusiva del servidor web, que crea la fila `PENDIENTE` antes de llamar a
  `/v1/commands` y la actualiza con el resultado (`APLICADA`/`NO_CONF`) que
  esta API le devuelve en la misma respuesta HTTP. SGPMP solo consulta
  `modulo3.estados_dispositivos_iot` para decidir si vale la pena esperar un
  ACK antes de publicar.
- **`modulo3`** (ingesta): invoca `fn_ingesta_telemetria(...)` para telemetría e
  inserta en `heartbeats` (dispara `fn_procesar_heartbeat`). Registra cada
  paquete en `transmisiones_mqtt`. También se lee `estados_dispositivos_iot`
  (solo lectura) para el precheck de `/v1/commands`.
- **`modulo1`** (solo lectura): `credenciales_servicio`, para autenticar al
  servidor web (ver sección de configuración).

Los valores de enums de PostgreSQL están reflejados en `app/core/enums.py` y deben
mantenerse en sincronía con la base.

## Convención de topics MQTT

```
sgpmp/<serial>/telemetry    # IoT -> SGPMP (telemetría)
sgpmp/<serial>/heartbeat    # IoT -> SGPMP (heartbeat)
sgpmp/<serial>/status       # IoT -> SGPMP (ACK / cambio de estado)
sgpmp/<serial>/command      # SGPMP -> IoT (comando)
sgpmp/<serial>/resultado    # SGPMP -> IoT (resultado de cada ingesta, BRK-01)
sgpmp/<serial>/vision       # IoT -> SGPMP (vector de visión, D4 -> backend)
sgpmp/<serial>/evento       # IoT -> SGPMP (eventos Edge RF-55 -> backend)
sgpmp/<serial>/ota/notify   # SGPMP -> IoT (aviso de OTA de modelos, D6)
sgpmp/<serial>/ota/<evento> # IoT -> SGPMP (status, download_progress, integrity_ok,
                            #   activated, rollback, critical_failure -> backend)
```

El `serial` es el `serial` único de `modulo9.dispositivos_iot` (ej.
`IOT-EST01-HLA-001`).

### Contrato de payload (propuesto — confirmar con el equipo IoT)

Telemetría (`.../telemetry`):

```json
{
  "variable": "Temperatura del agua",
  "valor_crudo": 23.5,
  "unidad": "°C",
  "timestamp_captura": "2026-08-17T14:00:00Z",
  "timestamp_envio": "2026-08-17T14:00:05Z",
  "sensor": "Sensor temperatura estanque-01",
  "origen": "TIEMPO_REAL",
  "categoria_variable": "AMBIENTAL",
  "tipo_dato": "CRUDO",
  "valor_agregado": false,
  "nivel_bateria_pct": 87.5,
  "calidad_senal_rssi": -90.0,
  "calidad_senal_snr": 8.5,
  "estado_conectividad": true,
  "metadatos": {}
}
```

`variable` y `unidad` son el `nombre` y la `unidad` exactos de
`modulo9.variables_ambientales` (`°C`, no `C`). `sensor` es el `nombre` del sensor
registrado en la plataforma para ese dispositivo (único por dispositivo): es
**obligatorio si el dispositivo tiene más de un sensor activo**, y conviene
enviarlo siempre (registrar un segundo sensor no obliga a tocar el firmware).

- Sin `sensor`: la lectura se guarda solo si el dispositivo tiene un único sensor
  activo. Antes se tomaba el primer sensor activo y con varios la lectura quedaba
  en el sensor equivocado.
- Sin `sensor` y con varios sensores activos, o con un `sensor` que no existe en el
  dispositivo: rechazo **definitivo** `ERROR_SENSOR`. El Edge actual no se suscribe a
  `sgpmp/<serial>/resultado` y borra la lectura de su buffer con el PUBACK, así que
  **esa lectura se pierde**: configurar el `sensor` antes de registrar un segundo
  sensor en la plataforma, y registrarlo enseguida, porque hasta entonces las lecturas
  que ya lo nombran también se rechazan. La causa queda en
  `modulo3.transmisiones_mqtt.error_descripcion` (y en el aviso de `resultado`).
- Con un único sensor, una lectura de otra variable (p. ej. humedad en un sensor de
  temperatura) se sigue aceptando: el catálogo no relaciona sensor y variable, así
  que el broker no puede detectarlo. Por eso cada variable va en su propio sensor.

Heartbeat (`.../heartbeat`):

```json
{
  "tipo_mensaje": "HEARTBEAT",
  "nivel_bateria_pct": 87.5,
  "calidad_senal_rssi": -90.0,
  "calidad_senal_snr": 8.5,
  "datos_pendientes_buffer": 0,
  "version_firmware": "1.0.0",
  "coordenadas": {"lat": 0, "lon": 0},
  "reloj_sincronizado": true,
  "sensores_operativos": [{"sensor": "dht22", "operativo": true}],
  "temperatura_dispositivo": 48.5
}
```

`sensores_operativos` y `temperatura_dispositivo` (°C) son de D11 (BRK-07),
opcionales: `sensor` es el mismo identificador que viaja en el campo `sensor`
de la telemetría. Se guardan en `modulo3.heartbeats.json_sensores_operativos` y
`temperatura_dispositivo` cuando la migración DB-03 del backend está aplicada;
antes de eso el broker los descarta y el heartbeat se guarda como siempre.

ACK de configuración (`.../status`, RF-23 — propuesto, confirmar con equipo IoT):

```json
{
  "tipo_mensaje": "ACK_CONFIGURACION",
  "resultado": "OK"
}
```

Comandos de D7 (BRK-05, `.../command`, mismo patrón de RF-17). El backend llama
`POST /v1/commands` con uno de estos `origen`; el broker publica con su
`tipo_comando`, `id_comando` y `emitido_en`, y espera el ACK de su mismo tipo
en `status` (`{"tipo_mensaje": "<ACK>", "resultado": "OK", "id_comando": ...}`).
El contenido de `regla` y `configuracion` lo arma el backend desde su tabla y
lo interpreta el firmware: el broker no lo valida por dentro.

| `origen` | `tipo_comando` | ACK | Campos |
|---|---|---|---|
| `regla_compuesta` | `REGLA_COMPUESTA` | `ACK_REGLA` | `id_regla_alerta`, `version_regla`, `es_activa`, `regla` |
| `config_motor` | `CONFIG_MOTOR_IA` | `ACK_CONFIG_MOTOR` (*) | `tipo_modelo`, `config_version`, `configuracion` |
| `tipo_modelo` | `TIPO_MODELO_ASIGNADO` | `ACK_TIPO_MODELO` (*) | `id_infraestructura`, `tipo_modelo_asignado` |

(*) Nombre propuesto por Desarrollo; AIoT lo confirma en su PR del firmware.
`ACK_CONFIG_MOTOR` trae además el `config_version` que quedó activo en el nodo:
la respuesta de `/v1/commands` lo devuelve en `config_version` para que el
backend lo compare con el suyo (RF-65 E10).

Aviso de buffer (`.../status`, BRK-06 / D10 de AIOT-DEV-03). El broker lo
reenvía a `POST /iot/eventos-buffer` del backend con el `serial` del topic y su
token de servicio (BRK-02); el backend crea la alerta técnica RF-60 y el evento
RF-63. El reenvío corre aparte del bucle MQTT: un backend lento no frena la
telemetría. Si el backend no responde, el evento queda en el log del broker.

```json
{
  "tipo_mensaje": "EVENTO_BUFFER",
  "evento": "BUFFER_OVERFLOW | BUFFER_DATA_CORRUPTION",
  "porcentaje_ocupacion": 100.0,
  "politica_aplicada": "POLITICA_RECHAZO",
  "cantidad_datos_afectados": 12,
  "timestamp_evento": "2026-10-17T10:00:00-05:00"
}
```

Visión, eventos Edge y reportes OTA (BRK-03, BRK-04, BRK-08 / D4-D6 de
AIOT-DEV-03). El broker no los interpreta: los reenvía a un endpoint interno
del backend con su token de servicio, en una tarea aparte del bucle MQTT. El
`serial` es siempre el del topic (lo autenticó la ACL); si el cuerpo trae otro,
se ignora.

| Topic | Endpoint del backend | Cuerpo |
|---|---|---|
| `vision` | `POST /iot/telemetria/vision` | el sobre de D4 tal cual + `serial` |
| `evento` | `POST /iot/eventos-edge` | el evento RF-55 + `serial`, `device_id`, `sensor_id` (ver abajo) |
| `ota/<evento>` | `POST /prediccion/ota/reportes` | `serial`, `id_despliegue_ota`, `componente`, `evento` y el resto en `detalle` |

La ACL de cada nodo cubre esos topics de su serial; los reportes OTA van uno
por uno (sin comodín), así que el nodo no puede publicar en `ota/notify` ni en
un `ota/<evento>` fuera del contrato.

Identidad del evento Edge (BRK-12, RF-55). El backend exige `device_id` y
`sensor_id` (PKs de modulo9) y el firmware solo conoce seriales, así que el
broker los resuelve antes de reenviar:

- `device_id`: el del `serial` del topic. Serial no registrado → se descarta
  con log, sin llamar al backend.
- Cada `variables_involucradas[].sensor` se busca en `modulo9.sensores.nombre`
  del dispositivo (una consulta por evento). Sin match, esa variable queda en
  `null`; nunca se cae al primer sensor activo.
- `sensor_id`: el de la variable primaria (la primera de la lista); si no
  resolvió, el de la primera que sí. Si ninguna variable trae `sensor`, el de la
  misma regla que la ingesta de telemetría (solo si el dispositivo tiene un único
  sensor activo). Si alguna trae `sensor` pero ninguno existe en el dispositivo, el
  evento se descarta, igual que la telemetría: nunca se atribuye al sensor por
  defecto. En ambos descartes queda un WARNING en el log del broker con la causa.
- El mapa va en `metadata_edge.sensores_por_variable` (el backend descarta los
  campos extra de cada variable) junto a `device_id_resuelto`. Lo que trae el
  Edge en `metadata_edge` no se toca: ante colisión gana el Edge.

Prioridad bajo saturación (BRK-09, RF-53 E15). Los mensajes pasan por una cola
de prioridad: control (`status`, `ota/*`) antes que críticos (heartbeat, eventos
Edge, lecturas con `"nivel_umbral": "critico"` o de una variable marcada
`es_critica` en M09) antes que el resto. Si el gateway se satura, las lecturas
normales reciben `RETENER` / `ERROR_SATURACION` y el Edge las reintenta. La
definición completa de "dato crítico" y la configuración están en
[`docs/BRK-09_Cola_Priorizacion_Saturacion.md`](./docs/BRK-09_Cola_Priorizacion_Saturacion.md).

Resultado de la ingesta (`.../resultado`, BRK-01 — formato propuesto por
Desarrollo, AIoT lo confirma en FW-01). Por cada lectura de `telemetry`, el
broker publica lo que devolvió `fn_ingesta_telemetria` (D2 de AIOT-DEV-03) y
lo deja también en `transmisiones_mqtt.estado`:

```json
{
  "tipo_mensaje": "RESULTADO_INGESTA",
  "id_mensaje": "<eco del id_mensaje de la lectura, si lo trae>",
  "sensor": "dht22",
  "variable": "Temperatura del agua",
  "timestamp_captura": "2026-08-17T14:00:00Z",
  "resultado": "EXITO | FALLIDO | TIMEOUT",
  "accion_buffer": "LIBERAR | RETENER",
  "codigo": "LECTURA_VALIDA | FUERA_DE_RANGO | ERROR_CALIBRACION | ERROR_* | ERROR_PERSISTENCIA | ERROR_SATURACION",
  "mensaje": "ERROR_TIEMPO: timestamp_captura futuro.",
  "id_telemetria": 123
}
```

| Resultado de la función | `transmisiones_mqtt.estado` | `accion_buffer` |
|---|---|---|
| Persistida (`id_telemetria` no nulo) | `EXITO` | `LIBERAR` |
| `ERROR_DUPLICADO` (ya estaba persistida) | `FALLIDO` + `error_descripcion` | `LIBERAR` |
| `ERROR_ESTRUCTURA` / `ERROR_AUTENTICACION` / `ERROR_TIEMPO` / `ERROR_UNIDAD` / `ERROR_SENSOR` | `FALLIDO` + `error_descripcion` | `LIBERAR` (rechazo definitivo) |
| Excepción, timeout o BD caída | `FALLIDO` / `TIMEOUT` si se puede escribir | `RETENER` (reintentar con backoff, RF-54) |

El nodo correlaciona el aviso con su lectura por `id_mensaje` (campo opcional de
la telemetría que el broker devuelve tal cual) o, si no lo manda, por
`(sensor, variable, timestamp_captura)`. Va en un topic propio y no en
`command` porque el firmware actual trata todo `command` sin `tipo_comando`
como una configuración RF-23. Hasta que el Edge se suscriba (FW-01) el aviso no
tiene suscriptores y se descarta sin efecto; mientras tanto el Edge sigue
liberando con el PUBACK.

Si llega mientras `/v1/commands` está esperando el ACK de ese `serial`
(dentro de `MQTT_ACK_TIMEOUT_SECONDS`), la request en curso responde
`APLICADA`. Si llega tarde (ya expiró el timeout o no había ninguna request
en curso), se loguea y se descarta — el reenvío automático de una
configuración `PENDIENTE` que reconecta más tarde no está implementado en
esta entrega (requeriría que SGPMP le avise al servidor web vía un webhook
inverso, fuera de alcance de RF-23 en su primera entrega).

## API HTTPS

| Método | Ruta | Descripción | Auth |
|---|---|---|---|
| `GET` | `/v1/healthz` | Salud del servicio | — |
| `POST` | `/v1/commands` | Enviar comando a un dispositivo (RF-23) o un umbral a un Gateway Edge (RF-17) | Bearer |
| `GET` | `/v1/devices` | Estado de dispositivos IoT | Bearer |
| `POST` | `/v1/ota/notify` | Aviso de OTA de modelo a un nodo (BRK-08); `202` al publicar, `503` sin MQTT | Bearer |

`POST /v1/commands` es síncrono: si el dispositivo está `ACTIVO`
(`modulo3.estados_dispositivos_iot`), publica el comando y espera hasta
`MQTT_ACK_TIMEOUT_SECONDS` el ACK que devuelva su `id_comando` antes de
responder. `origen` identifica el caso de uso que originó el comando:
`"configuracion"` (RF-23) o `"umbral"` (RF-17, contrato completo en
[`INTEGRACION_DISPOSITIVOS_RF17.md`](./INTEGRACION_DISPOSITIVOS_RF17.md)).
Varios comandos en vuelo al mismo serial se confirman cada uno por su
`id_comando`.

```bash
curl -X POST http://localhost:8000/v1/commands \
  -H "Authorization: Bearer <TOKEN_DE_SERVICIO>" \
  -H "Content-Type: application/json" \
  -d '{"origen": "configuracion", "serial": "IOT-EST01-HLA-001", "frecuencia_captura": 60, "intervalo_transmision": 300}'
```

`POST /v1/ota/notify` (BRK-08, D6 paso 3) no espera la instalación: publica en
`sgpmp/<serial>/ota/notify` (QoS 1; con la sesión persistente del Edge,
Mosquitto lo entrega al reconectar) y responde `202` con el `id_mensaje` del
aviso. El nodo reporta el avance por `ota/<evento>`, que el broker reenvía a
`POST /prediccion/ota/reportes`. `parametros` (fragmento, reintentos, batería
mínima, ventana) viaja tal cual desde el backend. Contrato propuesto por
Desarrollo hasta que DEV-15 fije el suyo:

```json
{
  "serial": "EDGE-REMANSO-01",
  "id_despliegue_ota": 31,
  "tipo_modelo": "AVES",
  "componente": "DETECTOR",
  "version": "3.0.0",
  "url_descarga": "https://.../prediccion/ota/artefactos/31?firma=...&expira=...",
  "sha256": "<64 hex en minúsculas>",
  "firma": "<firma del artefacto>",
  "tamano_bytes": 1048576,
  "parametros": {"tamano_fragmento_bytes": 65536, "max_reintentos_descarga": 5}
}
```

Respuestas posibles (siempre `200` si el request es válido — el resultado
del intento de configuración va en el campo `estado` del body, no en el
código HTTP; es el servidor web quien lo traduce a 200/202/504 hacia su
propio cliente):

```json
{"serial": "IOT-EST01-HLA-001", "topic": null, "estado": "PENDIENTE", "mensaje": "Dispositivo offline. La configuración quedará pendiente hasta que reconecte."}
{"serial": "IOT-EST01-HLA-001", "topic": "sgpmp/IOT-EST01-HLA-001/command", "estado": "APLICADA", "mensaje": "El dispositivo confirmó la recepción de la configuración."}
{"serial": "IOT-EST01-HLA-001", "topic": "sgpmp/IOT-EST01-HLA-001/command", "estado": "NO_CONF", "mensaje": "El comando fue enviado pero el dispositivo no confirmó la recepción a tiempo."}
```

## Entorno de trabajo del equipo

El equipo IoT trabaja con **Git Flow**, **Taiga** para tareas ligadas a
requerimientos funcionales (RF) y **VS Code** como IDE, sobre un entorno
reproducible en Docker. Ningún aporte llega a `main`/`develop` sin pasar por
Pull Request y por la validación de criterios de aceptación de su RF.

Ver `AGENTS.md` para las reglas de colaboración, la estrategia de ramas y las
convenciones que debe respetar cualquier contribución (humana o asistida por IA).

Formatos de validación del equipo:

- [Anexo A — Validación de Criterios de Aceptación](docs/Anexo_A_Validacion_Criterios.md)
- [Anexo B — Solicitud de Cambio](docs/Anexo_B_Solicitud_Cambio.md)
