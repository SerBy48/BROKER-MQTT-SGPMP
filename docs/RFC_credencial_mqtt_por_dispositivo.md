# RFC (propuesta) — Credencial MQTT por dispositivo

| | |
|---|---|
| **Estado** | Propuesta. Pendiente de aprobación de Análisis/IoT; **no implementada**. |
| **Origen** | TC-M09-G127, casos **TC-M09-250** y **TC-M09-251** (ataques al protocolo MQTT, RF-23) |
| **Relacionado** | SEG-BROKER-01 (ACL de mínimo privilegio), TC-M09-252 y TC-M09-253 (resueltos aparte, ver abajo) |
| **Alcance** | Broker (`BROKER-MQTT-SGPMP`), backend (`sgpmp-backend`), firmware |

## 1. Problema

Todos los dispositivos IoT se conectan a Mosquitto con **una sola credencial
compartida** (`sgpmp_devices`). El `serial` va en el topic, pero Mosquitto no
puede saber qué dispositivo es quién. Consecuencias, reproducidas contra
Mosquitto 2.1.2 con la ACL actual (`docker/mosquitto-entrypoint.sh`):

**TC-M09-251 — espionaje con wildcard.** Un cliente con la credencial de
dispositivos se suscribe a `#` mientras el gateway publica un comando para otro
serial:

```
SUBACK concedido (sin código de fallo)
sgpmp/OTRO-DISPOSITIVO/command {"frecuencia_captura":60,"intervalo_transmision":300}
```

Recibe el comando de **otro** dispositivo. La telemetría y el `status` sí
quedan filtrados (la ACL no da lectura sobre ellos), pero `command` está
permitido para todo el que tenga la credencial (`topic read sgpmp/+/command`).

**TC-M09-250 — no se puede probar el aislamiento.** No existe una credencial
por dispositivo, así que no hay forma de comprobar que un dispositivo *no
puede* publicar en el topic de otro. Además, con la credencial compartida sí
puede escribir en `sgpmp/<cualquier serial>/status`: puede fabricar el ACK de
otro dispositivo.

Es un riesgo ya conocido y aceptado por escrito en el entrypoint
("Riesgo residual conocido y aceptado por ahora ... evaluar credencial por
dispositivo"); este RFC es esa evaluación.

## 2. Qué se resolvió ya y qué no

| Caso | Estado | Cómo |
|---|---|---|
| TC-M09-252 replay | **Resuelto en servidor** | `id_comando` + `emitido_en` en el comando; el ACK debe devolver el `id_comando` y un ACK con otro id se ignora (`MQTT_ACK_REQUIERE_ID_COMANDO` para exigirlo). Requiere que el firmware devuelva el id. |
| TC-M09-253 sin TLS | **Resuelto en servidor** | `MQTT_PUERTO_PUBLICO=8883` / `MQTT_WS_PUERTO_PUBLICO=9002` publican solo TLS; sin certificados el broker no arranca. Requiere certificados en test/prod. `dev` sigue sin TLS por decisión del equipo. |
| TC-M09-250 / 251 | **Abierto** | Necesita credencial por dispositivo. Este RFC. |

Nota: con el ACK anti-replay, un dispositivo malicioso con la credencial
compartida ya no confirma un comando ajeno *sin conocer su `id_comando`*, pero
como puede **leer** el `command` de otro serial (251), puede leer el id y
forjar el ACK. Cerrar 251 cierra también esa vía.

## 3. Opciones evaluadas

Todas exigen lo mismo: **un usuario MQTT por dispositivo, con `usuario = serial`**.
Cambia solo cómo se expresa la ACL.

| | A. `dynamic-security` | B. `acl_file` con `pattern %u` |
|---|---|---|
| Aísla `command`/`status`/`telemetry` por serial | Sí | Sí |
| `SUBSCRIBE #` de un dispositivo | **SUBACK de fallo** ("All subscription requests were denied") | SUBACK concedido, pero **solo entrega los mensajes propios** |
| Publicar en topic ajeno | `PUBACK RC:135` en MQTT v5; en v3.1.1 el `PUBACK` sale con `RC:0` pero el mensaje se descarta | igual que A (medido) |
| Alta/rotación/revocación sin reiniciar el broker | Sí (API `$CONTROL/dynamic-security/v1`) | No: hay que regenerar `passwd` y `acl` y recargar (SIGHUP) |
| Complejidad | Plugin + `dynsec.json` persistente + credencial de administración | Mínima: mismo mecanismo que hoy |

Ambas se probaron contra Mosquitto 2.1.2 (la imagen del broker):

- **A:** con el rol `dispositivo` (`publishClientSend sgpmp/%u/{telemetry,heartbeat,status}`,
  `subscribePattern`/`publishClientReceive sgpmp/%u/command`): `#`, `sgpmp/#`,
  `sgpmp/+/command` y `sgpmp/<otro>/command` → denegados; `sgpmp/<propio>/command`
  → concedido; publicar en `status` o `command` de otro serial → `RC:135`; el
  dispositivo víctima solo recibió el mensaje legítimo del gateway.
- **B:** `SER-A` se suscribió a `#` (SUBACK concedido) y solo recibió
  `sgpmp/SER-A/command`, nunca el comando de `SER-B`; publicar en `status` de
  otro serial dio `RC:135` (v5) igual que en A.

Un detalle para quien escriba la prueba: con MQTT v3.1.1 un `PUBACK` con `RC:0`
**no** significa que el mensaje se haya entregado (Mosquitto descarta el
publish denegado sin avisar). Para verificar el aislamiento hay que usar MQTT v5
(código de razón) o comprobar la entrega real en el otro extremo.

**Recomendación: A.** Es la única que cumple el criterio literal de TC-M09-251
(el `SUBSCRIBE` debe recibir un código de fallo) y la única que permite dar de
alta y **revocar un dispositivo sin tocar a los demás ni reiniciar el broker**,
que es lo que hoy no se puede hacer. B es un paso intermedio válido si no se
quiere aún operar el plugin: cierra la fuga pero no el SUBACK.

`pattern` sobre `%c` (client id) **no** sirve como sustituto: el client id lo
elige el cliente y no está autenticado, así que quien tenga la credencial
compartida podría declarar el client id de otro dispositivo.

## 4. Propuesta (opción A)

### 4.1 Broker
- Cargar `mosquitto_dynamic_security.so` en los listeners (los TLS incluidos).
- `dynsec.json` en un volumen persistente (como `mosquitto_secrets`), con
  permisos restringidos; la credencial de administración es de un cliente del
  broker (no la del gateway) y no se expone fuera de la red interna.
- Roles: `dispositivo` (los ACL de arriba, con `%u`) y `gateway`
  (`publishClientSend sgpmp/+/command`; lectura de `telemetry`, `heartbeat`, `status`).
- El entrypoint deja de generar `acl` para dispositivos; conserva el usuario del
  gateway.

### 4.2 Gateway (`BROKER-MQTT-SGPMP`)
- Endpoints internos (protegidos con el mismo Bearer que `/v1/commands`):
  `POST /v1/devices/{serial}/credential` (crea o **rota**; devuelve la contraseña
  una sola vez) y `DELETE` (revoca). Internamente publican en
  `$CONTROL/dynamic-security/v1`.

### 4.3 Backend (`sgpmp-backend`)
- Endpoint de administración que llama al gateway y **audita** quién generó,
  rotó o revocó la credencial de qué dispositivo. Requiere un permiso RBAC
  nuevo (`modulo1.permisos`), con Administrador como titular inicial.
- **No se almacena la contraseña**: solo se muestra una vez, en la respuesta.
- Sin cambios de esquema previstos. Si se quiere mostrar "credencial emitida el…",
  sería una columna `fecha_credencial_emision` en `modulo9.dispositivos_iot`, y
  esa migración iría por Alembic con autorización del DBA.

### 4.4 Firmware
- Conectar con `usuario = serial` y su contraseña propia. El client id es libre.
- Solo cambia la configuración de conexión; los topics y payloads son los mismos.

### 4.5 Migración sin cortar dispositivos
1. **Coexistencia:** se crean usuarios por dispositivo mientras `sgpmp_devices`
   sigue con su ACL actual.
2. Cada dispositivo migra cuando se le entrega su credencial.
3. Cuando todos migraron: se elimina `sgpmp_devices`. Desde ese momento 250 y 251
   quedan cerrados de verdad.

## 5. Preguntas abiertas para Análisis / IoT

1. ¿Se aprueba la credencial por dispositivo? Toca lo que el sistema debe hacer
   (revocación por dispositivo), así que va por **RFC formal**
   (`docs-sgpmp/1-analisis/gestion-cambios/`), según `GUIA_CONEXION_IOT.md` §5.
2. RF-23 hoy solo dice, como requisito de seguridad, "Solo usuarios autorizados
   pueden modificar la configuración de los dispositivos". No menciona
   autenticación ni aislamiento entre dispositivos, TLS ni anti-replay. Conviene
   añadir un requisito no funcional que respalde estos casos de prueba.
3. ¿Puede el firmware actual usar un usuario distinto por dispositivo y devolver
   `id_comando` en el ACK? Determina cuándo se puede pasar
   `MQTT_ACK_REQUIERE_ID_COMANDO` a `true`.
4. Operación: ¿respaldo de `dynsec.json`, y qué pasa con más de una réplica del
   broker? Con una sola réplica no hay problema; con varias, el estado de
   `dynsec` no se comparte.

## 6. Fuera de alcance
- **LoRaWAN (TC-M09-G128).** El sistema no tiene un servidor de red LoRaWAN: el
  gateway y los dispositivos hablan MQTT directo con Mosquitto. La repetición
  de un *join-request* y la unicidad de las claves de sesión (AppSKey/NwkSKey)
  pertenecen a ese servidor de red y al firmware del sensor, no a este broker.
  Ver `anotaciones/modulo_9/tc_m09_g127_g128_seguridad_iot.md` en `sgpmp-backend`.
