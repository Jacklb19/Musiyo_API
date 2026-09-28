# Musiyo API

API pública de Musiyo Bëtsknaté, desarrollada con FastAPI y SQLAlchemy. La base de datos puede ser PostgreSQL mediante `MUSIYO_DATABASE_URL`; para desarrollo local usa SQLite. El almacenamiento de recursos es privado y está fuera del repositorio.

## Inicio local

1. Crear un entorno Python 3.11+ e instalar: `pip install -e ".[dev]"`.
2. Crear únicamente la estructura sintética del recorrido: `python -m app.seed`.
3. Iniciar: `uvicorn app.main:app --reload`.
4. Abrir `http://127.0.0.1:8000/docs`.

La API vacía no publica fichas. El seed crea una sala y tres puntos sin elementos. No incluye contenido cultural ni autorizaciones reales. Se puede configurar `MUSIYO_DATABASE_URL` y `MUSIYO_STORAGE_ROOT`; ver `.env.example`. Las variables no se cargan automáticamente desde ese archivo.

## Contrato v1

- `GET /api/v1/salud`
- `GET /api/v1/elementos`
- `GET /api/v1/elementos/{id}`
- `GET /api/v1/elementos/{id}/recursos/{recurso_id}`
- `GET /api/v1/recorridos/{id}`

El recorrido responde con `schemaVersion: 1`, `recorridoId`, salas ordenadas y puntos con `anclajeId` y `elementoIds`. Es compatible con el contrato de prueba del cliente Unity. Las respuestas filtran cualquier ficha sin aprobación y autorización vigentes. Los recursos también requieren aprobación y autorización propias; sus rutas se limitan al almacenamiento privado. Los contenidos no disponibles devuelven 404.

## Pruebas

`pytest`

La persistencia ya admite PostgreSQL, pero aún falta incorporar migraciones Alembic, autenticación del Validador Cultural y un flujo administrativo para registrar aprobaciones verificadas. No se deben insertar materiales culturales reales por fuera de ese flujo.
