# Musiyo API

API pública de Musiyo Bëtsknaté. Python 3.12, PostgreSQL y almacenamiento privado; contratos canónicos en inglés en `contracts/`.

## Desarrollo

Instalar `pip install -e ".[dev]"`. Para SQLite: `musiyo migrate`, `musiyo import test-data`, `uvicorn app.main:app --reload`. La API nunca cambia el esquema al importarse.

Para una vista previa existente, `powershell -File scripts/start-local-api.ps1 -DatabasePath <base.db> -StorageRoot <carpeta-privada> -WebOrigin http://localhost:5174 -Port 8002` configura juntos la base SQLite y sus archivos. Las rutas son obligatorias; el script no crea ni migra bases. Una ficha puede existir aunque su archivo falte: el acceso al recurso devolverá 404. Mantén los paquetes culturales y sus bases fuera del repositorio.

Para Docker, copiar `.env.example` a `.env` y completar credenciales locales propias (alfanuméricas para la contraseña de PostgreSQL). Ejecutar `docker compose up --build -d`, luego `docker compose exec api musiyo import test-data`. API: `http://localhost:8000/docs`; MinIO: `http://localhost:9001`. Los volúmenes conservan los datos. El dataset es sintético, sin contenido cultural.

La migración inicial conserva las filas del prototipo y convierte tablas, campos y estados a inglés. Respaldar cualquier base existente antes de migrarla. Un esquema parcial o mezclado se rechaza.

La migración `0003` normaliza fichas, fuentes, categorías, colecciones, recursos y autorizaciones. Las consultas públicas usan vistas de vigencia; las claves existentes se conservan. Los datos legados sin autor o acta conocida mantienen esos campos vacíos.

`musiyo import museum-test-data` prepara `museum-main`: seis salas y las 16 anclas de Unity, sin piezas culturales. El prototipo `recorrido-prueba` sigue disponible. Los recorridos retirados no se entregan por API.

Paquetes privados JSON: `musiyo import ruta/manifest.json --dry-run`, luego el mismo comando sin `--dry-run`. Formato en `schemas/content-package.v1.schema.json`. Administración: `revoke --element SLUG --reason MOTIVO` (o `--resource ID`), `withdraw --tour KEY`, `verify-validity`, `reindex`, `create-validator --username USER --name NAME` y `export-state`. `reindex` prepara fragmentos; los embeddings requieren el adaptador de T-50. No ejecuta llamadas a proveedores.

Cuentas mediante `create-validator` (contraseña solicitada por consola). La API usa sesiones de 8 horas, cookie segura y CSRF para correcciones de texto. Configurar `MUSIYO_WEB_ORIGIN` con el origen exacto de la Web; en producción servir Web/API por HTTPS en el mismo origen.

## Comprobaciones

Los accesos a archivos duran 300 segundos. `MUSIYO_STORAGE_BACKEND=s3` usa un bucket privado existente; `musiyo configure-storage --web-origin https://tu-web.example` limita su CORS. La importación transfiere archivos solo a MinIO local; para almacenamiento remoto se necesita un flujo autorizado independiente. En local se usa entrega firmada por la API; configurar una `MUSIYO_RESOURCE_SIGNING_KEY` privada común si se ejecutan varios procesos.

`ruff check .`, `mypy` y `pytest`. Regenerar contratos con `python -m scripts.build_contracts` y sincronizar con `python -m scripts.sync_contracts`; Web: `npm.cmd run contracts:generate`. No incluir contenido privado ni claves en Git.

## Coordinación

Código y contratos en inglés; interfaz en español. El contenido y los recursos proceden de API; parámetros y estilos ajustables se centralizan en configuración. Figma es referencia visual, mientras Unity gobierna la visita y sus menús. Narraciones Studio, guía RAG y validación física Quest son tareas pendientes. `main` estable y `dev` de integración; tareas desde `dev` en ramas profesionales (`feature/`, `fix/`, `docs/`, `refactor/`). Ramas y títulos de commit describen el cambio y no llevan nombres de asistentes o herramientas. Commits por tarea; el push lo hace el propietario. En el checkout local, consulta `AGENTS.md` y `.local_docs/REPORT_2026-10-08_HANDOFF.md`; estas notas y el preview externo no se incluyen en Git.
