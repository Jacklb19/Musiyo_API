from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


class Elemento(Base):
    __tablename__ = "elementos"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    titulo: Mapped[str] = mapped_column(String(250))
    descripcion: Mapped[str] = mapped_column(Text)
    interpretacion: Mapped[str] = mapped_column(Text, default="")
    estado_aprobacion: Mapped[str] = mapped_column(String(20), default="borrador")
    aprobado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    autorizado_desde: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    autorizado_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    creditos: Mapped[str] = mapped_column(Text, default="")
    fuentes: Mapped[str] = mapped_column(Text, default="")
    restricciones: Mapped[str] = mapped_column(Text, default="")
    recursos: Mapped[list[Recurso]] = relationship(back_populates="elemento")


class Recurso(Base):
    __tablename__ = "recursos"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    elemento_id: Mapped[str] = mapped_column(ForeignKey("elementos.id"))
    nombre: Mapped[str] = mapped_column(String(250))
    tipo_mime: Mapped[str] = mapped_column(String(100))
    ruta_privada: Mapped[str] = mapped_column(Text)
    aprobado: Mapped[bool] = mapped_column(Boolean, default=False)
    autorizado_desde: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    autorizado_hasta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    elemento: Mapped[Elemento] = relationship(back_populates="recursos")


class Sala(Base):
    __tablename__ = "salas"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    recorrido_id: Mapped[str] = mapped_column(String(100), index=True)
    orden: Mapped[int] = mapped_column(Integer)
    puntos: Mapped[list[Punto]] = relationship(back_populates="sala")


class Punto(Base):
    __tablename__ = "puntos"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    sala_id: Mapped[str] = mapped_column(ForeignKey("salas.id"))
    orden: Mapped[int] = mapped_column(Integer)
    sala: Mapped[Sala] = relationship(back_populates="puntos")
    elementos: Mapped[list[PuntoElemento]] = relationship(back_populates="punto")


class PuntoElemento(Base):
    __tablename__ = "punto_elementos"

    punto_id: Mapped[str] = mapped_column(ForeignKey("puntos.id"), primary_key=True)
    elemento_id: Mapped[str] = mapped_column(ForeignKey("elementos.id"), primary_key=True)
    orden: Mapped[int] = mapped_column(Integer)
    punto: Mapped[Punto] = relationship(back_populates="elementos")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def vigente(desde: datetime | None, hasta: datetime | None, revocado: datetime | None, ahora: datetime) -> bool:
    inicio = aware(desde)
    fin = aware(hasta)
    return inicio is not None and inicio <= ahora and (fin is None or ahora < fin) and revocado is None


def elemento_publico(elemento: Elemento, ahora: datetime) -> bool:
    return (
        elemento.estado_aprobacion == "aprobado"
        and elemento.aprobado_en is not None
        and vigente(elemento.autorizado_desde, elemento.autorizado_hasta, elemento.revocado_en, ahora)
    )


def recurso_publico(recurso: Recurso, ahora: datetime) -> bool:
    return recurso.aprobado and vigente(
        recurso.autorizado_desde, recurso.autorizado_hasta, recurso.revocado_en, ahora
    )


def make_session_factory(database_url: str | None = None):
    url = database_url or os.getenv("MUSIYO_DATABASE_URL", "sqlite:///./musiyo.db")
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite:") else {}
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


def storage_root() -> Path:
    return Path(os.getenv("MUSIYO_STORAGE_ROOT", "storage")).resolve()
