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
  "reloj_sincronizado": true
}
```

ACK de configuración (`.../status`, RF-23 — propuesto, confirmar con equipo IoT):

```json
{
  "tipo_mensaje": "ACK_CONFIGURACION",
  "resultado": "OK"
}
```

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
| `POST` | `/v1/commands` | Enviar comando a un dispositivo (RF-23) | Bearer |
| `GET` | `/v1/devices` | Estado de dispositivos IoT | Bearer |

`POST /v1/commands` es síncrono: si el dispositivo está `ACTIVO`
(`modulo3.estados_dispositivos_iot`), publica el comando y espera hasta
`MQTT_ACK_TIMEOUT_SECONDS` un ACK correlacionado por `serial` antes de
responder. `origen` identifica el caso de uso que originó el comando —hoy
solo `"configuracion"` existe de verdad, el campo queda reservado para que
telemetría/predicción lo usen el día que también necesiten enviar comandos
por este mismo punto de entrada.

```bash
curl -X POST http://localhost:8000/v1/commands \
  -H "Authorization: Bearer <TOKEN_DE_SERVICIO>" \
  -H "Content-Type: application/json" \
  -d '{"origen": "configuracion", "serial": "IOT-EST01-HLA-001", "frecuencia_captura": 60, "intervalo_transmision": 300}'
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
