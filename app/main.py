from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from .contracts import Credit, Element, ElementSummary, Health, Point, Problem, Restriction, Room, Source, TextBlock, Tour, TourMetadata

from .db import (
    Elemento, Punto, PuntoElemento, Recurso, Sala, elemento_publico,
    make_session_factory, recurso_publico, storage_root, utc_now,
)


def public_element(element: Elemento) -> Element:
    return Element(
        slug=element.id,
        title=element.titulo,
        description=element.descripcion,
        blocks=[TextBlock(id="legacy-interpretation", kind="interpretation", text=element.interpretacion,
                         source_id="legacy-source" if element.fuentes else None)] if element.interpretacion else [],
        credits=[Credit(name=element.creditos)] if element.creditos else [],
        sources=[Source(id="legacy-source", kind="other", reference=element.fuentes)] if element.fuentes else [],
        restrictions=[Restriction(kind="other", description=element.restricciones)] if element.restricciones else [],
    )


def create_app(
    session_factory: sessionmaker | None = None,
    now=utc_now,
    private_storage: Path | None = None,
) -> FastAPI:
    factory = session_factory or make_session_factory()
    storage = (private_storage or storage_root()).resolve()
    app = FastAPI(title="Musiyo API", version="1.0.0", responses={
        status: {"model": Problem, "content": {"application/problem+json": {}}}
        for status in (404, 409, 422)
    })

    def problem_response(status: int, detail: str):
        code = {404: "not_found", 409: "schema_incompatible"}.get(status, "validation")
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
        current_time = now()
        return [
            public_element(element)
            for element in db.scalars(select(Elemento).order_by(Elemento.id))
            if elemento_publico(element, current_time)
        ]

    @app.get("/api/v1/elements/{element_id}", response_model=Element)
    def get_element(element_id: str, db: Db):
        element = db.get(Elemento, element_id)
        if element is None or not elemento_publico(element, now()):
            raise HTTPException(404, "Contenido no disponible")
        return public_element(element)

    @app.get("/api/v1/elements/{element_id}/resources/{resource_id}")
    def get_resource(element_id: str, resource_id: str, db: Db):
        current_time = now()
        element = db.get(Elemento, element_id)
        resource = db.get(Recurso, resource_id)
        if (
            element is None or not elemento_publico(element, current_time)
            or resource is None or resource.elemento_id != element_id
            or not recurso_publico(resource, current_time)
        ):
            raise HTTPException(404, "Recurso no disponible")
        try:
            path = (storage / resource.ruta_privada).resolve()
            path.relative_to(storage)
        except ValueError:
            raise HTTPException(404, "Recurso no disponible")
        if not path.is_file():
            raise HTTPException(404, "Recurso no disponible")
        return FileResponse(
            path,
            media_type=resource.tipo_mime,
            filename=resource.nombre,
            content_disposition_type="inline",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/v1/tours/{tour_id}", response_model=Tour)
    def get_tour(tour_id: str, db: Db, schema_version: int = 1):
        if schema_version != 1:
            raise HTTPException(409, "Versión de contrato incompatible")
        rooms = list(db.scalars(
            select(Sala).where(Sala.recorrido_id == tour_id).order_by(Sala.orden, Sala.id)
        ))
        if not rooms:
            raise HTTPException(404, "Recorrido no disponible")
        current_time = now()
        visible_elements = {
            element.id: ElementSummary(slug=element.id, title=element.titulo) for element in db.scalars(select(Elemento))
            if elemento_publico(element, current_time)
        }
        result = []
        for room in rooms:
            points = list(db.scalars(
                select(Punto).where(Punto.sala_id == room.id).order_by(Punto.orden, Punto.id)
            ))
            result.append(Room(
                key=room.id,
                name=room.id,
                order=room.orden,
                points=[
                    Point(
                        key=point.id,
                        name=point.id,
                        order=point.orden,
                        activation=["keyboard"],
                        elements=[
                            visible_elements[link.elemento_id] for link in db.scalars(
                                select(PuntoElemento)
                                .where(PuntoElemento.punto_id == point.id)
                                .order_by(PuntoElemento.orden, PuntoElemento.elemento_id)
                            )
                            if link.elemento_id in visible_elements
                        ],
                    )
                    for point in points
                ],
            ))
        return Tour(schema_version=1, tour=TourMetadata(key=tour_id, name=tour_id), rooms=result)

    return app


app = create_app()
