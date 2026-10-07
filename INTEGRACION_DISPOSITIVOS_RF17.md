# RF-17 — Umbrales ambientales hacia el Gateway Edge (qué falta del lado IoT)

Este documento es para el equipo AIoT/firmware del Gateway Edge (la Raspberry,
`edge-agent`). Resuelve la incidencia **INC-M09-104-G29** (issue #493 de
`sgpmp-backend`, casos TC-M09-62 y TC-M09-63): cuando alguien crea o edita un
umbral ambiental en la plataforma, ese umbral tiene que llegar al Edge, que es
quien evalúa las lecturas y genera las alertas en campo
(`modulo3.eventos_edge_computing.umbral_min_aplicado`/`umbral_max_aplicado`).

Backend y broker ya están construidos (ver abajo). **Lo que falta es del lado
del Edge:** entender el comando nuevo, guardar y aplicar el umbral, y devolver
el ACK.

## Flujo construido

```
Usuario crea/edita umbral (especie, variable)
  → Backend resuelve los Gateway Edge de las áreas de esa especie
  → por cada Gateway: POST /v1/commands {"origen": "umbral", "serial": <Edge>, ...}
  → Broker publica en sgpmp/<serial Edge>/command
  ← Edge publica el ACK en sgpmp/<serial Edge>/status
  ← Broker responde APLICADA / PENDIENTE / NO_CONF
  → Backend guarda estado_sincronizacion del umbral (y fecha_ultima_sincronizacion si APLICADA)
```

**No hay topics nuevos.** El umbral viaja por el mismo `command` del serial del
Edge y el ACK por el mismo `status`; la credencial MQTT propia de cada Edge ya
tiene esos permisos. El comando se dirige **al serial del Gateway Edge**, no a
los ESP32 que atiende: el umbral es por especie y variable, no por sensor.

## 1. Distinguir el comando de umbral

En `sgpmp/<serial Edge>/command` ahora llegan dos tipos de comando:

| Campo `tipo_comando` | Qué es |
|---|---|
| ausente | Configuración remota RF-23 (`frecuencia_captura`/`intervalo_transmision`), sin cambios |
| `"UMBRAL_AMBIENTAL"` | Umbral ambiental RF-17 (este documento) |

⚠️ Un `edge-agent` que hoy asuma que todo lo que llega a `command` trae
`frecuencia_captura` tiene que revisar `tipo_comando` primero. Si recibe un
umbral y no lo entiende, que no responda ACK: el servidor lo marca "no
confirmado", que es lo correcto.

## 2. Payload del comando de umbral

```json
{
  "id_comando": "9f1c2b7e4a3d4e0f8b6a5c1d2e3f4a5b",
  "emitido_en": "2026-10-05T18:00:00.123456+00:00",
  "tipo_comando": "UMBRAL_AMBIENTAL",
  "id_umbral_ambiental": 7,
  "version": "2026-10-05T17:59:58.000000+00:00",
  "variable": "temperatura",
  "unidad": "°C",
  "valor_min": 18.0,
  "valor_max": 32.0,
  "niveles": [
    {"nivel": "normal", "limite_inferior": 22.0, "limite_superior": 28.0},
    {"nivel": "precaucion", "limite_inferior": 20.0, "limite_superior": 30.0},
    {"nivel": "critico", "limite_inferior": 18.0, "limite_superior": 32.0}
  ]
}
```

| Campo | Significado |
|---|---|
| `id_comando`, `emitido_en` | Igual que en RF-23 (anti-replay, TC-M09-252): devolver `id_comando` en el ACK y no re-aplicar uno ya procesado |
| `id_umbral_ambiental` | Identifica el umbral. **Clave para guardar**: un umbral nuevo con el mismo id reemplaza al anterior |
| `version` | `fecha_actualizacion` del umbral en la plataforma (`null` si nunca se editó). Si llega una versión **más vieja** que la que el Edge ya tiene para ese `id_umbral_ambiental`, se ignora (y se responde el ACK igual: el Edge ya tiene algo más nuevo) |
| `variable` | Mismo nombre que el Edge manda en el campo `variable` de la telemetría (`modulo9.variables_ambientales.nombre`) |
| `unidad` | Unidad de los valores |
| `valor_min`, `valor_max` | Rango aceptable de la variable para la especie |
| `niveles` | Bandas de alerta `normal` / `precaucion` / `critico` con sus límites |

## 3. Qué tiene que hacer el Edge

1. **Guardar** el umbral de forma persistente (sobrevive a un reinicio),
   indexado por `id_umbral_ambiental`.
2. **Aplicarlo** a la evaluación local de las lecturas de esa `variable`.
3. **Publicar el ACK** en `sgpmp/<serial Edge>/status`, **después** de guardar:

   ```json
   {
     "tipo_mensaje": "ACK_UMBRAL",
     "resultado": "OK",
     "id_comando": "9f1c2b7e4a3d4e0f8b6a5c1d2e3f4a5b"
   }
   ```

   - `tipo_mensaje` es `ACK_UMBRAL`, no `ACK_CONFIGURACION`: el servidor no
     acepta el ACK de un tipo como confirmación de un comando del otro.
   - `id_comando` es **obligatorio en la práctica**: un mismo Edge recibe
     varios umbrales casi a la vez (uno por variable de la especie) y sin el id
     el servidor no puede saber cuál se confirmó; con más de uno en vuelo, un
     ACK sin id se ignora.
   - **Plazo:** 30 segundos desde que se publicó el comando
     (`MQTT_ACK_TIMEOUT_SECONDS`). Si no llega, ese umbral queda "no
     confirmado" (`NO_CONF`) y el usuario ve el error de sincronización de
     RF-17.

## 4. Edge desconectado (TC-M09-63)

Si el Edge no está conectado al broker cuando se crea o edita el umbral, el
broker no publica y responde `PENDIENTE` al instante (mismo mecanismo que
RF-23); el backend guarda el umbral "Pendiente de Sincronización" y responde el
500 del flujo alterno de RF-17. El Edge **sigue usando el último umbral que
tenía guardado**: por eso el paso 3.1 pide persistencia.

**El Edge tiene que avisar que se desconecta.** Con sesión persistente,
Mosquitto sigue listando la conexión de un cliente después de que se cae, así
que el broker no puede deducirlo solo y terminaría esperando los 30 s del ACK.
El aviso es este mensaje en `sgpmp/<serial Edge>/status`:

```json
{"tipo_mensaje": "DESCONEXION"}
```

- Como **Last Will** al conectar (QoS 1, sin retain): Mosquitto lo publica si
  la conexión se corta (corte de luz, red), al vencer el keepalive.
- Publicado por el propio Edge **antes de un cierre ordenado** (`systemctl
  stop`): en un `DISCONNECT` normal MQTT no envía el Last Will.

El siguiente heartbeat del Edge lo vuelve a dar por conectado.

Al reconectar, la sesión persistente entrega los comandos que quedaron
encolados mientras estaba fuera; el Edge los aplica (respetando `version`),
aunque su ACK llegue tarde para la plataforma. El estado en la plataforma se
actualiza la próxima vez que alguien edite el umbral.

## Probar sin hardware

```bash
# 1. Escuchar como si fuera el Edge
mosquitto_sub -h <host> -p <puerto> -V mqttv5 -u <serial Edge> -P '<clave>' \
  -t "sgpmp/<serial Edge>/command"

# 2. Crear o editar un umbral en la plataforma (o POST /v1/commands, abajo)

# 3. Antes de 30 s, confirmar con el id_comando recibido
mosquitto_pub -h <host> -p <puerto> -V mqttv5 -u <serial Edge> -P '<clave>' \
  -t "sgpmp/<serial Edge>/status" -q 1 \
  -m '{"tipo_mensaje":"ACK_UMBRAL","resultado":"OK","id_comando":"<id_comando>"}'
```

Llamada directa al broker:

```bash
curl -X POST http://localhost:8000/v1/commands \
  -H "Authorization: Bearer <TOKEN_DE_SERVICIO>" \
  -H "Content-Type: application/json" \
  -d '{"origen":"umbral","serial":"<serial Edge>","id_umbral_ambiental":7,
       "version":"2026-10-05T17:59:58+00:00","variable":"temperatura","unidad":"°C",
       "valor_min":18,"valor_max":32,
       "niveles":[{"nivel":"normal","limite_inferior":22,"limite_superior":28},
                  {"nivel":"precaucion","limite_inferior":20,"limite_superior":30},
                  {"nivel":"critico","limite_inferior":18,"limite_superior":32}]}'
```

⚠️ **Contrato propuesto por backend/broker, pendiente de confirmar con AIoT.**
Si el `edge-agent` ya tiene otro formato, el ajuste es en
`app/services/dispatch.py::_cuerpo_comando()` (payload) y
`app/services/ingest.py::ingest_status()` (ACK).
