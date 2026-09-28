from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .db import (
    Elemento, Punto, PuntoElemento, Recurso, Sala, elemento_publico,
    make_session_factory, recurso_publico, storage_root, utc_now,
)


class ElementoPublico(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    titulo: str
    descripcion: str
    interpretacion: str
    creditos: str
    fuentes: str
    restricciones: str


class PuntoPublico(BaseModel):
    anclajeId: str
    elementoIds: list[str]


class SalaPublica(BaseModel):
    id: str
    orden: int
    puntos: list[PuntoPublico]


class ContratoRecorrido(BaseModel):
    schemaVersion: int = 1
    recorridoId: str
    salas: list[SalaPublica]


def create_app(
    session_factory: sessionmaker | None = None,
    now=utc_now,
    private_storage: Path | None = None,
) -> FastAPI:
    factory = session_factory or make_session_factory()
    storage = (private_storage or storage_root()).resolve()
    app = FastAPI(title="Musiyo API", version="0.1.0")

    def get_db():
        with factory() as db:
            yield db

    Db = Annotated[Session, Depends(get_db)]

    @app.get("/api/v1/salud")
    def salud():
        return {"estado": "ok"}

    @app.get("/api/v1/elementos", response_model=list[ElementoPublico])
    def listar_elementos(db: Db):
        ahora = now()
        return [
            elemento for elemento in db.scalars(select(Elemento).order_by(Elemento.id))
            if elemento_publico(elemento, ahora)
        ]

    @app.get("/api/v1/elementos/{elemento_id}", response_model=ElementoPublico)
    def obtener_elemento(elemento_id: str, db: Db):
        elemento = db.get(Elemento, elemento_id)
        if elemento is None or not elemento_publico(elemento, now()):
            raise HTTPException(404, "Elemento no disponible")
        return elemento

    @app.get("/api/v1/elementos/{elemento_id}/recursos/{recurso_id}")
    def obtener_recurso(elemento_id: str, recurso_id: str, db: Db):
        ahora = now()
        elemento = db.get(Elemento, elemento_id)
        recurso = db.get(Recurso, recurso_id)
        if (
            elemento is None or not elemento_publico(elemento, ahora)
            or recurso is None or recurso.elemento_id != elemento_id
            or not recurso_publico(recurso, ahora)
        ):
            raise HTTPException(404, "Recurso no disponible")
        try:
            ruta = (storage / recurso.ruta_privada).resolve()
            ruta.relative_to(storage)
        except ValueError:
            raise HTTPException(404, "Recurso no disponible")
        if not ruta.is_file():
            raise HTTPException(404, "Recurso no disponible")
        return FileResponse(
            ruta,
            media_type=recurso.tipo_mime,
            filename=recurso.nombre,
            content_disposition_type="inline",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
        )

    @app.get("/api/v1/recorridos/{recorrido_id}", response_model=ContratoRecorrido)
    def obtener_recorrido(recorrido_id: str, db: Db):
        salas = list(db.scalars(
            select(Sala).where(Sala.recorrido_id == recorrido_id).order_by(Sala.orden, Sala.id)
        ))
        if not salas:
            raise HTTPException(404, "Recorrido no disponible")
        ahora = now()
        visibles = {
            elemento.id for elemento in db.scalars(select(Elemento))
            if elemento_publico(elemento, ahora)
        }
        salida = []
        for sala in salas:
            puntos = list(db.scalars(
                select(Punto).where(Punto.sala_id == sala.id).order_by(Punto.orden, Punto.id)
            ))
            salida.append(SalaPublica(
                id=sala.id,
                orden=sala.orden,
                puntos=[
                    PuntoPublico(
                        anclajeId=punto.id,
                        elementoIds=[
                            vinculo.elemento_id for vinculo in db.scalars(
                                select(PuntoElemento)
                                .where(PuntoElemento.punto_id == punto.id)
                                .order_by(PuntoElemento.orden, PuntoElemento.elemento_id)
                            )
                            if vinculo.elemento_id in visibles
                        ],
                    )
                    for punto in puntos
                ],
            ))
        return ContratoRecorrido(recorridoId=recorrido_id, salas=salida)

    return app


app = create_app()
