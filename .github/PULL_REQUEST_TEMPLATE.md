## Descripción

<!-- Qué hace este PR y por qué. -->

## Trazabilidad

- **RF asociado:** RF-XX
- **Tarjeta de Taiga:** <!-- enlace o código de la tarjeta -->

## Tipo de rama

- [ ] `feature/*` (nace de `develop`, integra en `develop`)
- [ ] `release/*` (nace de `develop`, integra en `main` y `develop`)
- [ ] `hotfix/*` (nace de `main`, integra en `main` y `develop`)

## Criterios de aceptación validados (Anexo A)

<!-- Lista de los criterios del RF validados. Adjuntar o enlazar el Formato de Validación de Criterios diligenciado. -->

- [ ] Criterio 1
- [ ] Criterio 2

## Checklist

- [ ] `ruff check .` sin errores
- [ ] `pytest` en verde
- [ ] No se tocó la separación de capas (`api/`, `mqtt/`, `services/`, `db/`, `core/`)
- [ ] No se crearon migraciones ni se modificó el esquema de la base de datos
- [ ] Si hubo cambio de contrato (topics/payload), fue validado con el equipo IoT

## Notas para el revisor

<!-- Contexto adicional, riesgos, cosas a probar manualmente. -->
