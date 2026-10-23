# BRK-09 — Cola y priorización bajo saturación (RF-53 E15)

| | |
|---|---|
| **Fecha** | 2026-10-18 |
| **Tarea** | BRK-09 (Taiga #3521 / #3594), antes AIOT-11 en `AIOT-DEV-03` |
| **Estado de la definición** | Propuesta de Desarrollo para la pregunta **AN-5** de Análisis ("criterio de dato o dispositivo crítico"). El código ya la aplica; si Análisis la ajusta, se cambia en `app/services/prioridad.py` |
| **Para revisar** | Equipo AIoT (formato de `nivel_umbral`) y Análisis (AN-5) |

---

## 1. Qué es un dato crítico

Un mensaje del nodo cae en una de tres prioridades. La regla usa solo datos que el broker ya tiene: el tipo de topic, lo que el Edge calcula con sus umbrales y una marca del catálogo que edita el Administrador. Ninguna lista de variables queda fija en el código.

| Prioridad | Qué entra | Por qué |
|---|---|---|
| **P0 — CONTROL** | `status` (ACK de comandos, `DESCONEXION`, `EVENTO_BUFFER`) y `ota/*` | Son pocos, y si se pierden el backend y el nodo quedan desincronizados (un comando en `NO_CONF`, una OTA sin estado) |
| **P1 — CRÍTICO** | Heartbeat. Evento Edge de RF-55. Lectura que el Edge clasificó en el nivel `critico` del umbral vigente de la especie. Lectura de una variable marcada crítica en el catálogo de M09 | Del heartbeat depende la detección de inactividad (RF-60). Un evento RF-55 ya es una desviación. Las otras dos son las lecturas que disparan alertas o miden algo vital |
| **P2 — NORMAL** | Todo lo demás: lecturas en rango, vector de visión | Tiene sustituto: la próxima lectura, la agregación del Edge o el reintento desde el buffer |

### 1.1 Las dos marcas de una lectura crítica

1. **`nivel_umbral` en la telemetría (lo manda el Edge).** El Edge ya tiene los umbrales de la especie (RF-17), con los niveles `normal`, `precaucion` y `critico`. Al publicar una lectura agrega el nivel en que cayó:

   ```json
   { "variable": "Temperatura del agua", "valor_crudo": 34.1, "unidad": "°C",
     "timestamp_captura": "2026-10-18T10:00:00-05:00", "nivel_umbral": "critico" }
   ```

   El campo es opcional. Si falta, la lectura se trata por las otras reglas. No distingue mayúsculas.

2. **`es_critica` en el catálogo de M09 (lo marca el Administrador).** Es una columna booleana en `modulo9.variables_ambientales`. Algunos ejemplos que tienen sentido como críticos son temperatura corporal, frecuencia cardíaca y respiratoria, oxígeno disuelto y amoníaco. La decisión es del Administrador, no del código. El broker la lee con una caché de 10 minutos. Si la consulta falla (la BD puede ser justamente lo que está saturado), sigue con la última lista que leyó.

## 2. Cómo se comporta bajo saturación

El bucle MQTT solo parsea, clasifica y encola. `MQTT_COLA_WORKERS` tareas procesan en paralelo, en orden de prioridad.

| Prioridad | Entra a la cola mientras haya en espera menos de… | Si no entra |
|---|---|---|
| P0 | siempre | — |
| P1 | `2 × MQTT_COLA_CAPACIDAD` | se descarta y se cuenta |
| P2 | `MQTT_COLA_CAPACIDAD` | se descarta y se cuenta |

- **Dentro de cada prioridad sale primero el dato más viejo** (por `timestamp_captura` o `timestamp_evento`), que es el que está más cerca de vencer: el buffer de RF-53 admite datos de hasta 72 h.
- **Una lectura rechazada no se pierde.** El nodo recibe en `sgpmp/<serial>/resultado` el aviso de BRK-01 con `accion_buffer = RETENER` y `codigo = ERROR_SATURACION`, la conserva en su buffer y la reintenta con backoff (RF-54). Mientras dura la saturación no se toca la BD.
- **Un vector de visión rechazado no recibe aviso.** El sobre de D4 no tiene un canal de resultado y la siguiente ventana lo reemplaza.
- **Los logs no se inundan.** Se escribe un warning con el primer rechazo y luego uno cada 100, con cuántos mensajes hay en espera.
- **Las respuestas de dynamic-security nunca pasan por la cola**, porque otra tarea las está esperando.

### Configuración

| Variable | Default | Uso |
|---|---|---|
| `MQTT_COLA_CAPACIDAD` | `1000` | Mensajes en espera a partir de los cuales se rechaza P2. P1 se rechaza al doble |
| `MQTT_COLA_WORKERS` | `4` | Procesamiento en paralelo. Debe quedar por debajo del pool de la BD (5 + 10 de desborde en SQLAlchemy) |

## 3. Qué falta de otros equipos

| Quién | Qué | Mientras tanto |
|---|---|---|
| **AIoT** (firmware) | Mandar `nivel_umbral` en cada lectura que evalúa contra los umbrales de RF-17. Retener y reintentar con backoff cuando llega `RETENER` / `ERROR_SATURACION` (es el mismo manejo de FW-01) | Sin `nivel_umbral` solo cuentan las variables críticas del catálogo. Sin FW-01, el Edge libera con el PUBACK y una lectura rechazada sí se pierde |
| **DBA** (Samuel) | Columna `modulo9.variables_ambientales.es_critica boolean NOT NULL DEFAULT false`, con `SELECT` para el rol del broker | Sin la columna ninguna variable es crítica por catálogo. El broker lo detecta solo y no falla |
| **Backend / Frontend** (M09) | Permitir que el Administrador marque `es_critica` en el catálogo de variables | Se puede marcar a mano en la BD |
| **Análisis** | Aprobar o ajustar esta definición (AN-5) | Rige esta propuesta |

## 4. Límites conocidos

- **No hay orden por serial entre workers.** Un heartbeat y el aviso de desconexión del mismo Edge pueden procesarse cruzados. Lo peor que pasa es que un comando espere su ACK y termine en `NO_CONF`. Si eso molesta, se agrupan los workers por serial.
- **La cola vive en memoria de un solo proceso**, igual que la correlación de ACK. Si el broker se reinicia, lo que estaba en espera se pierde del lado del broker. Con FW-01, el Edge lo reintenta porque nunca recibió `LIBERAR`.

## 5. Cómo se verificó

- `tests/test_cola_priorizacion.py` cubre:
  - la definición, caso por caso;
  - el orden de salida (control, luego crítico, luego lo más viejo);
  - la admisión (P2 se rechaza primero, P1 al doble, P0 nunca);
  - el aviso `RETENER` en las lecturas rechazadas;
  - el catálogo cuando falta la columna;
  - la caché cuando la BD falla.
- El e2e contra Mosquitto 2.1.2 real (ACL, reenvío y aviso de resultado) pasa igual con el bucle MQTT pasando por la cola.
