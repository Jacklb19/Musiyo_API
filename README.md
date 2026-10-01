# Musiyo API

API pública de Musiyo Bëtsknaté. Python 3.12, PostgreSQL y almacenamiento privado; contratos canónicos en inglés en `contracts/`.

## Desarrollo

Instalar `pip install -e ".[dev]"`. Para SQLite: `musiyo migrate`, `musiyo import test-data`, `uvicorn app.main:app --reload`. La API nunca cambia el esquema al importarse.

Para Docker, copiar `.env.example` a `.env` y completar credenciales locales propias (alfanuméricas para la contraseña de PostgreSQL). Ejecutar `docker compose up --build -d`, luego `docker compose exec api musiyo import test-data`. API: `http://localhost:8000/docs`; MinIO: `http://localhost:9001`. Los volúmenes conservan los datos. El dataset es sintético, sin contenido cultural.

La migración inicial conserva las filas del prototipo y convierte tablas, campos y estados a inglés. Respaldar cualquier base existente antes de migrarla. Un esquema parcial o mezclado se rechaza.

La migración `0003` normaliza fichas, fuentes, categorías, colecciones, recursos y autorizaciones. Las consultas públicas usan vistas de vigencia; las claves existentes se conservan. Los datos legados sin autor o acta conocida mantienen esos campos vacíos.

`musiyo import museum-test-data` prepara `museum-main`: seis salas y las 16 anclas de Unity, sin piezas culturales. El prototipo `recorrido-prueba` sigue disponible. Los recorridos retirados no se entregan por API.

Paquetes privados JSON: `musiyo import ruta/manifest.json --dry-run`, luego el mismo comando sin `--dry-run`. Formato en `schemas/content-package.v1.schema.json`. Administración: `revoke --element SLUG --reason MOTIVO` (o `--resource ID`), `withdraw --tour KEY`, `verify-validity`, `reindex`, `create-validator --username USER --name NAME` y `export-state`. `reindex` prepara fragmentos; los embeddings requieren el adaptador de T-50. No ejecuta llamadas a proveedores.

## Comprobaciones

Los accesos a archivos duran 300 segundos. `MUSIYO_STORAGE_BACKEND=s3` usa un bucket privado existente; `musiyo configure-storage --web-origin https://tu-web.example` limita su CORS. La importación transfiere archivos solo a MinIO local; para almacenamiento remoto se necesita un flujo autorizado independiente. En local se usa entrega firmada por la API; configurar una `MUSIYO_RESOURCE_SIGNING_KEY` privada común si se ejecutan varios procesos.

`ruff check .`, `mypy` y `pytest`. Regenerar contratos con `python -m scripts.build_contracts` y sincronizar con `python -m scripts.sync_contracts`; Web: `npm.cmd run contracts:generate`. No incluir contenido privado ni claves en Git.
