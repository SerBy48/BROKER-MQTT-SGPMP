# RF-23 — Qué falta del lado de los dispositivos IoT

Este documento es para el equipo de hardware/firmware. Todo lo demás (backend,
broker, frontend) ya está implementado y probado end-to-end con un dispositivo
simulado. Lo único que falta para que funcione con hardware real son los tres
puntos de abajo.

## Resumen del flujo ya construido

```
Usuario (UI) → Backend → Broker (este repo) → MQTT (Mosquitto) → Dispositivo
                                             ← MQTT (Mosquitto) ← Dispositivo (ACK)
```

El broker publica el comando y espera hasta 30s (configurable) la confirmación
del dispositivo antes de responder. Si no llega, el sistema lo marca "no
confirmado" — eso es exactamente lo que hace hoy porque **ningún dispositivo
real publica el ACK todavía**.

---

## 1. Publicar heartbeat (para que el sistema sepa que el dispositivo está online)

**Ya implementado del lado del servidor — esto no es nuevo para RF-23**, es
requisito general de conectividad. Sin heartbeat, el sistema asume que el
dispositivo está offline y ni siquiera intenta enviarle el comando (queda en
estado "pendiente" indefinidamente).

- **Topic:** `sgpmp/<serial>/heartbeat`
- **Frecuencia:** la que tenga configurada el dispositivo (parámetro que este
  mismo RF-23 permite ajustar)
- El `<serial>` debe coincidir exactamente con el que está registrado en el
  sistema para ese dispositivo (ej. `IOT-EST01-HLA-001`)

## 2. Suscribirse al topic de comandos y aplicar la configuración recibida

- **Topic a suscribirse:** `sgpmp/<serial>/command`
- **Payload que va a recibir (JSON):**
  ```json
  {
    "frecuencia_captura": 10,
    "intervalo_transmision": 15
  }
  ```
  Ambos valores en minutos. El dispositivo debe aplicar:
  - `frecuencia_captura`: cada cuántos minutos captura datos de los sensores.
  - `intervalo_transmision`: cada cuántos minutos transmite lo capturado al servidor.

## 3. Publicar la confirmación (ACK) después de aplicar la configuración

**Este es el paso que falta y que bloquea todo el flujo hoy.**

- **Topic a publicar:** `sgpmp/<serial>/status`
- **Payload exacto (JSON):**
  ```json
  {
    "tipo_mensaje": "ACK_CONFIGURACION",
    "resultado": "OK"
  }
  ```
- **Plazo:** debe publicarse dentro de los **30 segundos** siguientes a recibir
  el comando en el topic `command`. Si no llega a tiempo, el sistema marca la
  configuración como "no confirmada" y así se lo muestra al usuario.

⚠️ **Este contrato de payload está propuesto por nuestro lado, no confirmado
con ustedes.** Si el firmware ya tiene definido otro formato de ACK (otros
nombres de campo, otro valor para "éxito", etc.), avisen y ajustamos
`app/services/ingest.py::ingest_status()` en este repo para que matchee lo que
realmente van a enviar — es un cambio de una función, no de arquitectura.

---

## Conexión y autenticación al broker

Mosquitto ya no acepta conexiones anónimas (`allow_anonymous false`). El
dispositivo necesita autenticarse con usuario/contraseña MQTT estándar al
conectar (no es lo mismo que ningún token HTTP — es la credencial del
protocolo MQTT en sí, `CONNECT` con `username`/`password`).

| Dato | Valor |
|---|---|
| Host | *(entregado aparte según ambiente — dev/test/prod)* |
| Puerto MQTT (TCP) | *(entregado aparte, ver tabla de variables más abajo)* |
| Puerto MQTT (WebSocket) | *(entregado aparte)* |
| Usuario | *(entregado aparte — no es `sgpmp_gateway`, ese es del gateway, no de los dispositivos)* |
| Contraseña | *(entregado aparte)* |
| TLS | No en este ambiente (dev) — sin cifrado en tránsito. Antes de producción hay que activar `MQTT_TLS`/certificados. |

Es una única credencial **compartida por todos los dispositivos** (no hay
usuario/contraseña por dispositivo individual) — el `serial` en el topic es
lo que identifica a cada uno, no la credencial de conexión. Si se necesita
revocar acceso a un dispositivo específico sin afectar al resto, avisen: hoy
no está soportado (se rotaría la credencial compartida para todos).

## Qué NO tienen que hacer

- No necesitan tocar nada de la base de datos — eso ya está resuelto entre el
  broker y el backend.
- No necesitan escribir nada en `modulo9.configuraciones_remotas` — esa tabla
  la maneja el backend exclusivamente.
- No necesitan implementar el reenvío automático cuando un dispositivo estuvo
  offline y vuelve a conectar — **eso no está construido todavía** en este
  lado tampoco (queda para una entrega futura, requiere definir un mecanismo
  nuevo). Por ahora, si un comando queda "pendiente" porque el dispositivo
  estaba offline, alguien tiene que reintentar manualmente desde la UI una vez
  que el dispositivo reconecte.

## Cómo probar sin esperar al hardware real

Mientras el firmware no esté listo, se puede simular el ACK manualmente
(esto es lo que usamos para las pruebas):

```bash
docker exec <container_mosquitto> mosquitto_pub -h localhost \
  -u sgpmp_devices -P '<MQTT_DEVICE_PASSWORD real>' \
  -t "sgpmp/<serial>/status" \
  -m '{"tipo_mensaje":"ACK_CONFIGURACION","resultado":"OK"}' -q 1
```

(`<container_mosquitto>` es el nombre real del contenedor en ese ambiente —
ya no es fijo, Dokploy lo genera automáticamente por proyecto/ambiente;
consultarlo en el panel o con `docker ps`.)

## Configuración relevante del broker (por si cambia el ambiente)

| Variable | Qué controla |
|---|---|
| `MQTT_TOPIC_PREFIX` | El prefijo `sgpmp` de todos los topics (default `sgpmp`) |
| `MQTT_TOPIC_COMMAND` | Nombre del sufijo de comando (default `command`) |
| `MQTT_TOPIC_STATUS` | Nombre del sufijo de status/ACK (default `status`) |
| `MQTT_ACK_TIMEOUT_SECONDS` | Segundos que espera el ACK antes de dar timeout (default `30`) |

Si el prefijo o los nombres de topic van a ser distintos en producción, avisar
para ajustar la configuración de ambos lados (deben coincidir).
