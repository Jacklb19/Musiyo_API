from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from .contracts import Credit, Element, ElementSummary, Guide, Health, Point, Problem, Restriction, Room, Source, TextBlock, Tour, TourMetadata

from .db import (
    ElementRecord, PointRecord, PointElementRecord, ResourceRecord, RoomRecord, TourRecord, is_public_element,
    make_session_factory, is_public_resource, storage_root, utc_now,
)


def public_element(element: ElementRecord) -> Element:
    return Element(
        slug=element.id,
        title=element.title,
        description=element.description,
        blocks=[TextBlock(id="legacy-interpretation", kind="interpretation", text=element.interpretation,
                         source_id="legacy-source" if element.sources else None)] if element.interpretation else [],
        credits=[Credit(name=element.credits)] if element.credits else [],
        sources=[Source(id="legacy-source", kind="other", reference=element.sources)] if element.sources else [],
        restrictions=[Restriction(kind="other", description=element.restrictions)] if element.restrictions else [],
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
            for element in db.scalars(select(ElementRecord).order_by(ElementRecord.id))
            if is_public_element(element, current_time)
        ]

    @app.get("/api/v1/elements/{element_id}", response_model=Element)
    def get_element(element_id: str, db: Db):
        element = db.get(ElementRecord, element_id)
        if element is None or not is_public_element(element, now()):
            raise HTTPException(404, "Contenido no disponible")
        return public_element(element)

    @app.get("/api/v1/elements/{element_id}/resources/{resource_id}")
    def get_resource(element_id: str, resource_id: str, db: Db):
        current_time = now()
        element = db.get(ElementRecord, element_id)
        resource = db.get(ResourceRecord, resource_id)
        if (
            element is None or not is_public_element(element, current_time)
            or resource is None or resource.element_id != element_id
            or not is_public_resource(resource, current_time)
        ):
            raise HTTPException(404, "Recurso no disponible")
        try:
            path = (storage / resource.private_path).resolve()
            path.relative_to(storage)
        except ValueError:
            raise HTTPException(404, "Recurso no disponible")
        if not path.is_file():
            raise HTTPException(404, "Recurso no disponible")
        return FileResponse(
            path,
            media_type=resource.mime,
            filename=resource.name,
            content_disposition_type="inline",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/v1/tours/{tour_id}", response_model=Tour)
    def get_tour(tour_id: str, db: Db, schema_version: int = 1):
        if schema_version != 1:
            raise HTTPException(409, "Versión de contrato incompatible")
        tour = db.get(TourRecord, tour_id)
        if tour is None or not tour.published:
            raise HTTPException(404, "Recorrido no disponible")
        rooms = list(db.scalars(
            select(RoomRecord).where(RoomRecord.tour_id == tour_id).order_by(RoomRecord.order, RoomRecord.id)
        ))
        if not rooms:
            raise HTTPException(404, "Recorrido no disponible")
        current_time = now()
        visible_elements = {
            element.id: ElementSummary(slug=element.id, title=element.title) for element in db.scalars(select(ElementRecord))
            if is_public_element(element, current_time)
        }
        result = []
        for room in rooms:
            points = list(db.scalars(
                select(PointRecord).where(PointRecord.room_id == room.id).order_by(PointRecord.order, PointRecord.id)
            ))
            result.append(Room(
                key=room.id,
                name=room.name or room.id,
                order=room.order,
                short_description=room.short_description,
                points=[
                    Point(
                        key=point.id,
                        name=point.name or point.id,
                        order=point.order,
                        activation=point.activation,
                        elements=[
                            visible_elements[link.element_id] for link in db.scalars(
                                select(PointElementRecord)
                                .where(PointElementRecord.point_id == point.id)
                                .order_by(PointElementRecord.order, PointElementRecord.element_id)
                            )
                            if link.element_id in visible_elements
                        ],
                    )
                    for point in points
                ],
            ))
        guide = Guide(key=tour.guide_key, name=tour.guide_name, room_key=tour.guide_room_key,
                      available=tour.guide_available) if tour.guide_key else None
        return Tour(schema_version=1,
                    tour=TourMetadata(key=tour.id, name=tour.name, revision=tour.revision),
                    rooms=result, guide=guide)

    return app


app = create_app()
