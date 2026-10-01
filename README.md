# Musiyo API

API pública de Musiyo Bëtsknaté. Python 3.12, PostgreSQL y almacenamiento privado; contratos canónicos en inglés en `contracts/`.

## Desarrollo

Instalar `pip install -e ".[dev]"`. Para SQLite: `musiyo migrate`, `musiyo import test-data`, `uvicorn app.main:app --reload`. La API nunca cambia el esquema al importarse.

Para Docker, copiar `.env.example` a `.env` y completar credenciales locales propias (alfanuméricas para la contraseña de PostgreSQL). Ejecutar `docker compose up --build -d`, luego `docker compose exec api musiyo import test-data`. API: `http://localhost:8000/docs`; MinIO: `http://localhost:9001`. Los volúmenes conservan los datos. El dataset es sintético, sin contenido cultural.

La migración inicial conserva las filas del prototipo y convierte tablas, campos y estados a inglés. Respaldar cualquier base existente antes de migrarla. Un esquema parcial o mezclado se rechaza.

`musiyo import museum-test-data` prepara `museum-main`: seis salas y las 16 anclas de Unity, sin piezas culturales. El prototipo `recorrido-prueba` sigue disponible. Los recorridos retirados no se entregan por API.

## Comprobaciones

`pytest`. Regenerar contratos con `python -m scripts.build_contracts` y sincronizar con `python -m scripts.sync_contracts`; Web: `npm.cmd run contracts:generate`. No incluir contenido privado ni claves en Git.
