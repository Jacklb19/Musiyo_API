from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from .contracts import (
    CatalogFacet,
    CatalogPage,
    Element,
    Health,
    Problem,
    ResourceAccess,
    Tour,
)
from .db import (
    make_session_factory,
    storage_root,
    utc_now,
)
from .public_repository import PublicRepository
from .resource_storage import ACCESS_SECONDS, LocalDelivery, S3Storage, configured_s3


def create_app(
    session_factory: sessionmaker | None = None,
    now=utc_now,
    private_storage: Path | None = None,
    resource_signing_key: bytes | None = None,
    object_storage: S3Storage | None = None,
) -> FastAPI:
    factory = session_factory or make_session_factory()
    storage = (private_storage or storage_root()).resolve()
    local_delivery = LocalDelivery(storage, resource_signing_key)
    remote_storage = object_storage or configured_s3()
    app = FastAPI(title="Musiyo API", version="1.0.0", responses={
        status: {"model": Problem, "content": {"application/problem+json": {}}}
        for status in (404, 409, 422, 503)
    })

    def problem_response(status: int, detail: str):
        codes: dict[int, Literal["not_found", "schema_incompatible", "validation", "service_unavailable"]] = {
            404: "not_found", 409: "schema_incompatible", 503: "service_unavailable"
        }
        code = codes.get(status, "validation")
        problem = Problem(type="about:blank", title=detail, status=status, code=code, detail=detail)
        return JSONResponse(problem.model_dump(), status_code=status, media_type="application/problem+json")

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request, error):
        return problem_response(error.status_code, str(error.detail))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request, error):
        return problem_response(422, "Solicitud inválida")

    def get_db():
        with factory() as db:
            yield db

    Db = Annotated[Session, Depends(get_db)]

    @app.get("/api/v1/health", response_model=Health)
    def health():
        return {"status": "ok"}

    @app.get("/api/v1/elements", response_model=list[Element])
    def list_elements(db: Db):
        return PublicRepository(db, now).elements()

    @app.get("/api/v1/categories", response_model=list[CatalogFacet])
    def list_categories(db: Db):
        return PublicRepository(db, now).facets("categories")

    @app.get("/api/v1/collections", response_model=list[CatalogFacet])
    def list_collections(db: Db):
        return PublicRepository(db, now).facets("collections")

    @app.get("/api/v1/catalog", response_model=CatalogPage)
    def get_catalog(db: Db, query: Annotated[str, Query(max_length=200)] = "",
                    category: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
                    collection: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
                    highlighted: bool = False, limit: Annotated[int, Query(ge=1, le=100)] = 24,
                    offset: Annotated[int, Query(ge=0, le=100000)] = 0, schema_version: int = 1):
        if schema_version != 1:
            raise HTTPException(409, "Versión de contrato incompatible")
        return PublicRepository(db, now).catalog(query, category, collection, highlighted, limit, offset)

    @app.get("/api/v1/elements/{element_id}", response_model=Element)
    def get_element(element_id: str, db: Db):
        element = PublicRepository(db, now).element(element_id)
        if element is None:
            raise HTTPException(404, "Contenido no disponible")
        return element

    @app.get("/api/v1/elements/{element_id}/resources/{resource_id}")
    def get_resource(element_id: str, resource_id: str, db: Db):
        resource = PublicRepository(db, now).resource(element_id, resource_id)
        if resource is None:
            raise HTTPException(404, "Recurso no disponible")
        access = issue_access(dict(resource))
        return RedirectResponse(access.url, status_code=307, headers={"Cache-Control": "private, no-store"})

    def issue_access(resource):
        expires = int(now().timestamp()) + ACCESS_SECONDS
        try:
            url = (remote_storage or local_delivery).access(resource, expires)
        except ClientError as error:
            if str(error.response.get("Error", {}).get("Code")) in ("404", "NoSuchKey", "NotFound"):
                raise HTTPException(404, "Recurso no disponible")
            raise HTTPException(503, "El almacenamiento no está disponible")
        except (BotoCoreError, ValueError):
            raise HTTPException(503, "El almacenamiento no está disponible")
        if url is None:
            raise HTTPException(404, "Recurso no disponible")
        return ResourceAccess(schema_version=1, url=url, expires_at=datetime.fromtimestamp(expires, timezone.utc),
            mime=resource["mime"], byte_count=resource["byte_count"], sha256=resource["sha256"])

    @app.post("/api/v1/resources/{resource_id}/access", response_model=ResourceAccess)
    def get_resource_access(resource_id: str, db: Db, schema_version: int = 1):
        if schema_version != 1:
            raise HTTPException(409, "Versión de contrato incompatible")
        # resource_by_id reads v_current_resources, not the persistence table.
        # That view requires a current resource grant and current element/room/point parents.
        resource = PublicRepository(db, now).resource_by_id(resource_id)
        if resource is None:
            raise HTTPException(404, "Recurso no disponible")
        return JSONResponse(issue_access(dict(resource)).model_dump(mode="json"), headers={"Cache-Control": "private, no-store"})

    @app.get("/api/v1/resource-delivery/{resource_id}")
    def deliver_resource(resource_id: str, db: Db, expires: int,
                         signature: Annotated[str, Query(pattern=r"^[a-f0-9]{64}$")]):
        if not local_delivery.valid(resource_id, expires, signature, now().timestamp()):
            raise HTTPException(404, "Recurso no disponible")
        # A valid signature never bypasses the publication view; revocation denies delivery too.
        resource = PublicRepository(db, now).resource_by_id(resource_id)
        path = local_delivery.path(dict(resource)) if resource is not None else None
        if resource is None or path is None:
            raise HTTPException(404, "Recurso no disponible")
        return FileResponse(
            path,
            media_type=resource["mime"],
            filename=resource["name"],
            content_disposition_type="inline",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/v1/tours/{tour_id}", response_model=Tour)
    def get_tour(tour_id: str, db: Db, schema_version: int = 1):
        if schema_version != 1:
            raise HTTPException(409, "Versión de contrato incompatible")
        tour = PublicRepository(db, now).tour(tour_id)
        if tour is None:
            raise HTTPException(404, "Recorrido no disponible")
        return tour

    return app


app = create_app()
