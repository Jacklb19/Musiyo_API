"""Canonical v1 wire models shared by API, Web, and Unity."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


Identifier = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S+$")]
Activation = Literal["proximity", "gaze", "keyboard"]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ElementSummary(ContractModel):
    slug: Identifier
    title: str
    community: str | None = None
    thumbnail_resource_id: Identifier | None = None
    has_3d_model: bool = False
    has_narration: bool = False


class Point(ContractModel):
    key: Identifier
    name: str
    order: int = Field(ge=0)
    activation: list[Activation] = Field(min_length=1, json_schema_extra={"uniqueItems": True})
    elements: list[ElementSummary]

    @model_validator(mode="after")
    def validate_unique_values(self):
        if len(set(self.activation)) != len(self.activation):
            raise ValueError("Repeated activation method")
        slugs = [element.slug for element in self.elements]
        if len(set(slugs)) != len(slugs):
            raise ValueError("Repeated element within a point")
        return self


class Room(ContractModel):
    key: Identifier
    name: str
    order: int = Field(ge=0)
    short_description: str = ""
    ambient_audio_resource_id: Identifier | None = None
    points: list[Point]


class TourMetadata(ContractModel):
    key: Identifier
    name: str
    revision: datetime | None = None


class Guide(ContractModel):
    key: Identifier
    name: str
    room_key: Identifier
    available: bool


class Tour(ContractModel):
    schema_version: Literal[1]
    tour: TourMetadata
    rooms: list[Room] = Field(min_length=1)
    guide: Guide | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_version_type(cls, value):
        if type(value) is not int:
            raise ValueError("Schema version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_locations(self):
        room_keys = [room.key for room in self.rooms]
        point_keys = [point.key for room in self.rooms for point in room.points]
        if len(set(room_keys)) != len(room_keys) or len(set(point_keys)) != len(point_keys):
            raise ValueError("Repeated room or point key")
        room_orders = [room.order for room in self.rooms]
        if room_orders != sorted(set(room_orders)):
            raise ValueError("Room orders must be unique and ascending")
        for room in self.rooms:
            orders = [point.order for point in room.points]
            if orders != sorted(set(orders)):
                raise ValueError("Point orders must be unique and ascending")
        if self.guide and self.guide.room_key not in room_keys:
            raise ValueError("Unknown guide room")
        return self


class NamedTerm(ContractModel):
    slug: Identifier
    name: str


class Community(ContractModel):
    name: str
    people: Identifier


class TextBlock(ContractModel):
    id: Identifier
    kind: Literal["documented_fact", "testimony", "interpretation"]
    text: str
    source_id: Identifier | None = None
    attribution: str | None = None
    context: str | None = None


class Source(ContractModel):
    id: Identifier
    kind: Literal["publication", "interview", "other"]
    reference: str
    url: str | None = None


class Credit(ContractModel):
    role: str | None = None
    name: str


class Restriction(ContractModel):
    kind: Literal["no_download", "no_reuse", "other"]
    description: str


class ResourceVariant(ContractModel):
    id: Identifier
    profile: Literal["web", "quest"]


class Resource(ContractModel):
    id: Identifier
    kind: Literal["model_3d", "image", "audio", "video", "narration", "subtitles"]
    mime: str
    byte_count: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)
    alternative_text: str | None = None
    credit: str | None = None
    provenance: str | None = None
    variants: list[ResourceVariant] = Field(default_factory=list)
    transcription: str | None = None
    subtitles_resource_id: Identifier | None = None


class LastModified(ContractModel):
    date: datetime
    author: str


class Element(ContractModel):
    slug: Identifier
    language: Literal["es"] = "es"
    title: str
    description: str
    community: Community | None = None
    category: NamedTerm | None = None
    collections: list[NamedTerm] = Field(default_factory=list)
    technique: str | None = None
    materials: list[str] = Field(default_factory=list)
    blocks: list[TextBlock] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    credits: list[Credit] = Field(default_factory=list)
    restrictions: list[Restriction] = Field(default_factory=list)
    resources: list[Resource] = Field(default_factory=list)
    last_modified: LastModified | None = None

    @model_validator(mode="after")
    def validate_source_references(self):
        source_ids = [source.id for source in self.sources]
        block_ids = [block.id for block in self.blocks]
        if len(set(source_ids)) != len(source_ids) or len(set(block_ids)) != len(block_ids):
            raise ValueError("Repeated block or source identifier")
        if any(block.source_id and block.source_id not in source_ids for block in self.blocks):
            raise ValueError("Unknown block source")
        return self


class Problem(ContractModel):
    type: str
    title: str
    status: int
    code: Literal["not_found", "schema_incompatible", "validation"]
    detail: str


class Health(ContractModel):
    status: Literal["ok"]


class SelectionData(ContractModel):
    tour_key: Identifier
    point_key: Identifier
    element_slug: Identifier | None = None


class SelectionConfirmed(ContractModel):
    source: Literal["musiyo-unity"]
    type: Literal["selection_confirmed"]
    version: Literal[1]
    data: SelectionData

    @field_validator("version", mode="before")
    @classmethod
    def validate_version_type(cls, value):
        if type(value) is not int:
            raise ValueError("Message version must be an integer")
        return value


class ClearedSelectionData(ContractModel):
    tour_key: Identifier


class SelectionCleared(ContractModel):
    source: Literal["musiyo-unity"]
    type: Literal["selection_cleared"]
    version: Literal[1]
    data: ClearedSelectionData

    @field_validator("version", mode="before")
    @classmethod
    def validate_version_type(cls, value):
        if type(value) is not int:
            raise ValueError("Message version must be an integer")
        return value
