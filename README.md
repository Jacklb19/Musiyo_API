# Musiyo API

API pública de Musiyo Bëtsknaté, desarrollada con FastAPI y SQLAlchemy. La base de datos puede ser PostgreSQL mediante `MUSIYO_DATABASE_URL`; para desarrollo local usa SQLite. El almacenamiento de recursos es privado y está fuera del repositorio.

## Inicio local

1. Crear un entorno Python 3.11+ e instalar: `pip install -e ".[dev]"`.
2. Crear únicamente la estructura sintética del recorrido: `python -m app.seed`.
3. Iniciar: `uvicorn app.main:app --reload`.
4. Abrir `http://127.0.0.1:8000/docs`.

La API vacía no publica fichas. El seed crea una sala y tres puntos sin elementos. No incluye contenido cultural ni autorizaciones reales. Se puede configurar `MUSIYO_DATABASE_URL` y `MUSIYO_STORAGE_ROOT`; ver `.env.example`. Las variables no se cargan automáticamente desde ese archivo.

## Contrato v1

- `GET /api/v1/health`
- `GET /api/v1/elements`
- `GET /api/v1/elements/{element_id}`
- `GET /api/v1/elements/{element_id}/resources/{resource_id}`
- `GET /api/v1/tours/{tour_id}?schema_version=1`

Los esquemas v1 y ejemplos sintéticos canónicos están en `contracts/`: recorrido (`tour`, `rooms`, `guide`), ficha con bloques tipados y mensaje `selection_confirmed`. Los errores usan `application/problem+json`. La publicación sigue exigiendo aprobación y autorización vigentes.

Desde la raíz, ejecutar `python -m scripts.build_contracts` y `python -m scripts.sync_contracts` (o `sh scripts/sync_contracts.sh`) para copiar JSON, hashes SHA-256 y DTO Unity a los repositorios hermanos. Web genera sus tipos con `npm.cmd run contracts:generate`. La persistencia española es legado: los valores desconocidos se entregan vacíos y su migración queda para T-05.

## Pruebas

`pytest`

La persistencia ya admite PostgreSQL, pero aún falta incorporar migraciones Alembic, autenticación del Validador Cultural y un flujo administrativo para registrar aprobaciones verificadas. No se deben insertar materiales culturales reales por fuera de ese flujo.
