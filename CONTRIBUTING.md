# Convención de commits — BROKER-MQTT-SGPMP

Este documento aplica a **todo commit que llegue a `develop`** (directo o
vía PR). No es una preferencia de estilo: `develop` corre un pipeline de
versionamiento automatizado (`semantic-release`) que **lee el historial de
commits para decidir qué versión publicar, qué entra al `CHANGELOG.md` y
qué se registra en `docs/trazabilidad/TRAZABILIDAD_CAMBIOS.md`**. Un commit
que no sigue este formato es invisible para esas tres cosas — no rompe el
build, pero desaparece de la trazabilidad silenciosamente.

**Herramientas de IA (Claude Code y otras) que trabajen en este repo deben
seguir esta convención al generar mensajes de commit.**

Misma convención que `sgpmp-backend`/`SGPMP-FRONT-END-PWA` — resumen acá:

---

## Formato

```
tipo(scope): descripción corta en minúscula, sin punto final
```

`tipo` es obligatorio: `feat`/`fix`/`perf`/`refactor` (generan versión),
`docs`/`chore`/`test`/`build`/`ci`/`style` (no generan versión, pero sí
quedan documentados si llevan el prefijo correcto).

## Tipos y qué provocan en la versión

| Tipo | Efecto |
|---|---|
| `feat` | minor |
| `fix`, `perf`, `refactor` | patch |
| `docs`, `chore`, `test`, `build`, `ci`, `style` | ninguno |
| Breaking change (`!` o `BREAKING CHANGE:`) | major |

## Referenciar RF / RNF / RFC / BUG

```
feat(rf23): generar credencial MQTT dedicada para dispositivos IoT
fix(dokploy): default MQTT_PORT a 1884, el 1883 ya está ocupado por EMQX
```

## Reglas adicionales, no negociables

- **No crear tags de Git manualmente.**
- **No hacer squash-merge de PRs a `develop`.**
- El tag `v0.1.0` existente es de antes de activar este pipeline — no
  representa un release automatizado real, queda como referencia histórica
  únicamente. La numeración real desde ahora es `1.x.y-rc.N` en `develop`,
  `1.x.y` en `main`.
