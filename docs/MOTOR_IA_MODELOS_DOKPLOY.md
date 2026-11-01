# Motor de inferencia M04: cómo se cargan los modelos en Dokploy

Aplica a las Applications `motor-ia-dev`, `motor-ia-test` y `motor-ia-main` (una por ambiente,
construidas con `Dockerfile.motor-ia`). Es un procedimiento provisional (RF-66): sirve hasta que
existan la entrega de artefactos del backend (B2) y la descarga con caché del motor (M3). Desde
ese momento el motor bajará solo las versiones activas de RF-69 de su ambiente.

## Por qué no van en git ni en la imagen

- Son binarios (`.onnx`) y pesan: solo los poblacionales suman 53 MB. Cada versión nueva inflaría
  el historial para siempre.
- Cambian por ambiente y por versión (RF-69), no por release del broker. Si vinieran en la imagen,
  cambiar un modelo obligaría a reconstruirla.
- Un File Mount de Dokploy (como el `firebase.json` del backend) tampoco sirve: es texto que se
  pega en un editor, y un `.onnx` es binario.

Por eso `Dockerfile.motor-ia.dockerignore` excluye `*.onnx`/`*.pt`/`modelos/`, y `.gitignore`
excluye `modelos/`.

## Qué se sube

Una carpeta con `catalogo.json` y, por animal, los tres archivos que entrega ET-02:

```
modelos/
├── catalogo.json
├── BOV-001.onnx
├── BOV-001_linea_base.json
└── BOV-001_metadatos_rf69.json
```

```json
{"modelos": [{"id_activo_biologico": 885, "id_version_modelo": 12,
              "onnx": "BOV-001.onnx", "linea_base": "BOV-001_linea_base.json",
              "metadatos": "BOV-001_metadatos_rf69.json"}]}
```

- `id_activo_biologico` y `id_version_modelo` son los IDs **de la BD de ese ambiente**: el mismo
  animal tiene IDs distintos en DEV, TEST y PROD. Cada ambiente lleva su propio `catalogo.json`.
- Al arrancar, el motor verifica el SHA-256 del `.onnx` contra `hash_artefacto_modelo` de los
  metadatos y que la línea base sea del mismo animal. Si algo falla, no arranca, y el log dice
  qué archivo falló.

## Paso a paso (una vez por ambiente)

Ejemplo con TEST; en DEV y PROD solo cambia el nombre de la carpeta. Los valores de despliegue son
los definidos desde el PR #49 y no cambian: puerto **8100** (Container Port del dominio) y prefijo
`/model-ia-dev` · `/model-ia-test` · `/model-ia` con Strip Path.

1. **Copiar la carpeta al servidor** de Dokploy, fuera de las carpetas que administra Dokploy:

   ```bash
   ssh <servidor> 'sudo mkdir -p /srv/sgpmp/motor-ia-test/modelos'
   scp -r modelos <servidor>:/tmp/modelos-motor-ia
   ssh <servidor> 'sudo cp /tmp/modelos-motor-ia/* /srv/sgpmp/motor-ia-test/modelos/ \
     && sudo chmod -R a+rX /srv/sgpmp/motor-ia-test/modelos && rm -rf /tmp/modelos-motor-ia'
   ```

   El contenedor corre como el usuario 1001, así que los archivos tienen que ser legibles para
   todos (`a+rX`); no hace falta cambiarles el dueño.

2. **Montarla en la Application** `motor-ia-test` → *Advanced* → *Volumes / Mounts* → *Add* →
   **Bind Mount**:
   - Host Path: `/srv/sgpmp/motor-ia-test/modelos`
   - Mount Path: `/data/modelos`

3. **Activar el catálogo** en *Environment*: `MOTOR_IA_DIR_MODELOS=/data/modelos`, junto con
   `IOT_INFERENCIA_M04_TOKEN` y `BACKEND_URL` (ver `.env.motor-ia.example`). Con modelos cargados,
   `BACKEND_URL` es obligatoria.

4. **Redeploy** y comprobar:

   ```bash
   curl -s https://api.inmero.co/model-ia-test/v1/healthz
   # {"status":"ok","modelos_cargados":1,"backend":"https://api.inmero.co/api-sgpmp-test/..."}
   ```

   Si `modelos_cargados` es 0, `MOTOR_IA_DIR_MODELOS` no quedó definida. Si el contenedor se
   reinicia en bucle, el log muestra `CatalogoInvalidoError` con el archivo que falló.

## Cambiar o agregar un modelo

El catálogo se lee **solo al arrancar**. Para actualizar: copiar los archivos nuevos a la carpeta
del servidor (paso 1), editar `catalogo.json` y hacer *Restart* de la Application. No hace falta
reconstruir la imagen.

Para dejar el motor sin modelos, se borra `MOTOR_IA_DIR_MODELOS` y se reinicia. Ojo: si la variable
está definida y falta `catalogo.json`, el motor no arranca. Un catálogo vacío válido es
`{"modelos": []}`.

## Local

```bash
mkdir -p modelos && cp tests/fixtures/et02_bov001/* modelos/   # o los de ET-02
cp .env.motor-ia.example .env.motor-ia   # completar el token y MOTOR_IA_DIR_MODELOS=/data/modelos
docker compose --env-file .env.motor-ia -f docker-compose.motor-ia.yml up --build
```

`./modelos` se monta como solo lectura en `/data/modelos` y está ignorada por git.

## Cuando llegue M3

El motor escribirá en `/data/modelos`, que pasará a ser su caché. En ese momento:

- se cambia el Bind Mount por un **Volume Mount** (volumen con nombre), cuyo dueño será 1001 porque
  la imagen ya crea `/data/modelos` así;
- `catalogo.json` deja de editarse a mano: lo arma el motor con lo que esté activo en RF-69.
