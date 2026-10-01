from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


class ElementRecord(Base):
    __tablename__ = "elements"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    title: Mapped[str] = mapped_column(String(250))
    description: Mapped[str] = mapped_column(Text)
    interpretation: Mapped[str] = mapped_column(Text, default="")
    approval_status: Mapped[str] = mapped_column(String(20), default="draft")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authorized_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authorized_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    credits: Mapped[str] = mapped_column(Text, default="")
    sources: Mapped[str] = mapped_column(Text, default="")
    restrictions: Mapped[str] = mapped_column(Text, default="")
    resources: Mapped[list[ResourceRecord]] = relationship(back_populates="element")


class ResourceRecord(Base):
    __tablename__ = "resources"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    element_id: Mapped[str] = mapped_column(ForeignKey("elements.id"))
    name: Mapped[str] = mapped_column(String(250))
    mime: Mapped[str] = mapped_column(String(100))
    private_path: Mapped[str] = mapped_column(Text)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    authorized_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authorized_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    element: Mapped[ElementRecord] = relationship(back_populates="resources")


class TourRecord(Base):
    __tablename__ = "tours"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(250))
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    guide_key: Mapped[str | None] = mapped_column(String(100))
    guide_name: Mapped[str | None] = mapped_column(String(250))
    guide_room_key: Mapped[str | None] = mapped_column(String(100))
    guide_available: Mapped[bool] = mapped_column(Boolean, default=False)


class RoomRecord(Base):
    __tablename__ = "rooms"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    tour_id: Mapped[str] = mapped_column(String(100), index=True)
    order: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(250), default="")
    short_description: Mapped[str] = mapped_column(Text, default="")
    points: Mapped[list[PointRecord]] = relationship(back_populates="room")


class PointRecord(Base):
    __tablename__ = "points"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.id"))
    order: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(250), default="")
    activation: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["keyboard"])
    room: Mapped[RoomRecord] = relationship(back_populates="points")
    elements: Mapped[list[PointElementRecord]] = relationship(back_populates="point")


class PointElementRecord(Base):
    __tablename__ = "point_elements"

    point_id: Mapped[str] = mapped_column(ForeignKey("points.id"), primary_key=True)
    element_id: Mapped[str] = mapped_column(ForeignKey("elements.id"), primary_key=True)
    order: Mapped[int] = mapped_column(Integer)
    point: Mapped[PointRecord] = relationship(back_populates="elements")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def is_current(start: datetime | None, end: datetime | None, revoked: datetime | None, now: datetime) -> bool:
    beginning = aware(start)
    ending = aware(end)
    return beginning is not None and beginning <= now and (ending is None or now < ending) and revoked is None


def is_public_element(element: ElementRecord, now: datetime) -> bool:
    return (
        element.approval_status == "approved"
        and element.approved_at is not None
        and is_current(element.authorized_from, element.authorized_until, element.revoked_at, now)
    )


def is_public_resource(resource: ResourceRecord, now: datetime) -> bool:
    return resource.approved and is_current(
        resource.authorized_from, resource.authorized_until, resource.revoked_at, now
    )


def make_session_factory(database_url: str | None = None):
    url = database_url or os.getenv("MUSIYO_DATABASE_URL", "sqlite:///./musiyo.db")
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite:") else {}
    engine = create_engine(url, **kwargs)
    return sessionmaker(engine, expire_on_commit=False)


def storage_root() -> Path:
    return Path(os.getenv("MUSIYO_STORAGE_ROOT", "storage")).resolve()
