# AGENTS.md — Reglas de trabajo para el repositorio SGPMP

Guía para cualquier agente (humano o IA) que contribuya a este repositorio.
Describe el **ambiente de trabajo del equipo IoT**, la estrategia de ramas y las
convenciones técnicas que deben respetarse para que un aporte sea aceptado.

## 1. Qué es este proyecto

SGPMP es el **gateway MQTT ↔ HTTPS** de la plataforma **AIoT de monitoreo de
salud animal**. Es el único componente que habla MQTT (Mosquitto); el servidor
web se comunica con él solo por HTTPS y consulta el PostgreSQL compartido
(esquemas `modulo3` para ingesta y `modulo9` para registro). No debe replicar la
lógica de negocio ni de datos: esa vive en la base de datos.

Stack: Python 3.11+, FastAPI, `aiomqtt`, SQLAlchemy 2.0 async, `asyncpg`,
`pydantic-settings`.

## 2. Ambiente de trabajo del equipo IoT

- **Control de versiones:** Git remoto con Pull Requests.
- **Estrategia de ramas:** Git Flow.
- **Gestión de tareas:** Taiga (cada tarea nace de un requerimiento funcional —RF—).
- **IDE:** Visual Studio Code.
- **Entorno reproducible:** imagen Docker con dependencias fijadas.
- **Validación:** criterios de aceptación del RF antes de la revisión (la CI
  automatizada es responsabilidad del grupo de implementación/pruebas, no del IoT).

Ninguna tarea existe sin un RF que la justifique; todo aporte queda ligado a una
tarjeta de Taiga y a un commit etiquetado.

## 3. Estrategia de ramas (Git Flow)

| Rama | Nace de → integra en | Uso |
|---|---|---|
| `main` | release/hotfix → main | Línea estable y publicable, solo versiones etiquetadas. **Nunca** commit directo. |
| `develop` | feature → develop | Integración de aportes aprobados. **Nunca** commit directo. |
| `feature/*` | develop → develop | Un aporte = una tarea Taiga = un RF. Vida corta. |
| `release/*` | develop → main (y develop) | Preparación/prueba de versión candidata. |
| `hotfix/*` | main → main (y develop) | Correcciones urgentes o rechazadas por el grupo de pruebas. |

### Reglas de oro

1. **Nadie hace commit directo a `main` ni a `develop`**: todo entra por Pull Request.
2. **Una feature = un aporte = una tarea de Taiga = un RF.** Ramas pequeñas.
3. **`main` nunca se reescribe.** Las correcciones van en `hotfix` con commits nuevos.
4. **Cada versión se etiqueta (tag)** y se referencia en las notas de validación.

## 4. Flujo de trabajo

1. Tomar una tarea de Taiga asociada a un RF.
2. Crear `feature/*` desde `develop`.
3. Desarrollar en VS Code, con commits ligados a la tarea y pruebas locales en Docker.
4. Push y abrir Pull Request hacia `develop`.
5. Validar el aporte contra los criterios de aceptación del RF (Formato Anexo A).
6. Revisión del PR por el líder AIoT y el líder de desarrollo (vía broker).
7. Visto bueno final del grupo de pruebas (quality gate).
8. Merge a `develop`, eliminar la feature y cerrar la tarea.

**Flujo alterno (rechazo):** el aporte rechazado vuelve a `hotfix/*` con motivo
escrito; se corrige con commits nuevos y se re-valida. Si no hay acuerdo, se
escala al líder AIoT. Toda la discusión queda en el PR y en la tarjeta de Taiga.

**Flujo alterno (pruebas):** un defecto se corrige dentro de `release` (o un
`fix/*` derivado); un cambio de estándar/requerimiento se tramita como solicitud
de cambio (se actualiza el RF) **antes** de tocar código.

## 5. Convenciones técnicas del repositorio

### Separación de capas (respetar estrictamente)

- `app/api/` — solo HTTPS, no toca MQTT ni BD directamente.
- `app/mqtt/` — solo MQTT, no toca BD.
- `app/services/` — orquesta, llama a `repositories` y `publisher`.
- `app/db/repositories/` — acceso a datos (SPs y consultas).
- `app/core/` — logging, errores, enums.

### Base de datos

- **No crear esquemas ni correr migraciones** en este repositorio: la BD es
  compartida y ya está versionada con Alembic en el proyecto principal.
- La ingesta usa los stored procedures existentes: `modulo3.fn_ingesta_telemetria`
  y el trigger de `modulo3.heartbeats` (`fn_procesar_heartbeat`). No reimplementar
  esa lógica en Python.
- Los enums de `app/core/enums.py` deben coincidir con los tipos enum de PostgreSQL.

### Topics MQTT

`<prefix>/<serial>/<telemetry|heartbeat|status|command>`; el `serial` es el
`serial` de `modulo9.dispositivos_iot`. El contrato de payload está en `app/schemas.py`
y en el README; cualquier cambio debe validarse con el equipo IoT.

## 6. Comandos

```bash
pip install -e ".[dev]"          # dependencias
docker compose up -d mosquitto   # broker de desarrollo
uvicorn app.main:app --reload    # API + cliente MQTT
pytest                           # pruebas
ruff check .                     # lint
```

## 7. Reglas para agentes (IA)

- No hacer commit ni push a `main` o `develop`; trabajar en `feature/*` o `hotfix/*`.
- No crear migraciones ni modificar el esquema de la base de datos.
- Mantener la separación de capas y no añadir dependencias sin justificarlas.
- No inventar contratos de payload/topics: si algo no está definido, dejar un
  `TODO` explícito y señalarlo en el PR.
- Documentar en el PR el vínculo con la tarea de Taiga y el RF correspondiente.
