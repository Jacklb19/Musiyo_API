"""Versioned private import format; no cultural material is generated."""
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import ContractModel, Element, Identifier


class PublicationDecision(ContractModel):
    reference: str = Field(min_length=1)
    responsible: str = Field(min_length=1)
    decision_date: date
    approved_culturally: bool
    authorized_from: datetime
    authorized_until: datetime | None = None

    @model_validator(mode="after")
    def validate_interval(self):
        if self.authorized_from.tzinfo is None or self.authorized_until and self.authorized_until.tzinfo is None:
            raise ValueError("Authorization timestamps require a timezone")
        if self.authorized_until and self.authorized_until <= self.authorized_from:
            raise ValueError("Authorization interval must be positive")
        if not self.reference.strip() or not self.responsible.strip():
            raise ValueError("Decision evidence fields cannot be blank")
        self.authorized_from = self.authorized_from.astimezone(timezone.utc)
        if self.authorized_until:
            self.authorized_until = self.authorized_until.astimezone(timezone.utc)
        return self


class ImportFile(ContractModel):
    file: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile: Literal["general", "web", "quest"] = "general"
    variant_of: Identifier | None = None
    room_key: Identifier | None = None
    point_key: Identifier | None = None
    paradata: dict | None = None
    metadata: dict | None = None


class ImportElement(ContractModel):
    record: Element
    kind: Literal["mask", "character", "clothing", "dance", "song", "story", "instrument", "space", "other"]
    point_keys: list[Identifier] = Field(default_factory=list)
    allow_assistant: bool = True
    show_in_tour: bool = True


class ContentPackage(ContractModel):
    manifest_version: Literal[1]
    decision: PublicationDecision
    elements: list[ImportElement] = Field(min_length=1)
    files: dict[str, ImportFile] = Field(default_factory=dict)

    @field_validator("manifest_version", mode="before")
    @classmethod
    def validate_version_type(cls, value):
        if type(value) is not int:
            raise ValueError("Manifest version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_graph(self):
        if type(self.manifest_version) is not int:
            raise ValueError("Manifest version must be an integer")
        slugs = [item.record.slug for item in self.elements]
        resources = [resource for item in self.elements for resource in item.record.resources]
        ids = [resource.id for resource in resources]
        if len(set(slugs)) != len(slugs) or len(set(ids)) != len(ids):
            raise ValueError("Element and resource identifiers must be unique")
        if set(ids) != set(self.files):
            raise ValueError("Each resource requires exactly one file definition")
        for item in self.elements:
            record = item.record
            if len(record.slug) > 100 or len(record.title) > 160 or len(record.description) > 5000:
                raise ValueError("Element fields exceed storage limits")
            if record.last_modified is not None:
                raise ValueError("Correction metadata is controlled by the validator")
            if len(set(item.point_keys)) != len(item.point_keys):
                raise ValueError("Repeated point key")
            for source in record.sources:
                if len(source.id) > 36:
                    raise ValueError("Source identifier exceeds storage limits")
            for block in record.blocks:
                if len(block.id) > 36 or block.kind == "interpretation" and not (block.source_id or block.context and block.context.strip()):
                    raise ValueError("An interpretation requires a source or nonblank context")
            own = {resource.id for resource in record.resources}
            profiles = set()
            for resource in record.resources:
                if len(resource.id) > 100 or not resource.credit or not resource.provenance:
                    raise ValueError("Resources require credit, provenance, and a bounded identifier")
                if resource.kind in ("model_3d", "image") and not resource.alternative_text:
                    raise ValueError("Visual resources require alternative text")
                if resource.kind in ("audio", "narration") and not resource.transcription:
                    raise ValueError("Audio resources require transcription")
                if resource.kind == "narration" and (resource.duration_seconds is None or resource.duration_seconds > 180):
                    raise ValueError("Narrations require a duration of at most 180 seconds")
                file = self.files[resource.id]
                if file.variant_of and (file.variant_of not in own or file.variant_of == resource.id):
                    raise ValueError("Variants must belong to the same element")
                if file.variant_of and self.files[file.variant_of].variant_of:
                    raise ValueError("Variant chains are not supported")
                if file.variant_of and (resource.kind != "model_3d" or file.profile == "general"):
                    raise ValueError("Model variants require a Web or Quest profile")
                profile = (file.variant_of or resource.id, file.profile)
                if profile in profiles:
                    raise ValueError("Repeated resource profile")
                profiles.add(profile)
                if resource.subtitles_resource_id and resource.subtitles_resource_id not in own:
                    raise ValueError("Subtitle resource belongs to another element")
                if resource.subtitles_resource_id and not any(candidate.id == resource.subtitles_resource_id and candidate.kind == "subtitles" for candidate in record.resources):
                    raise ValueError("Subtitle reference must target a subtitle resource")
                if resource.variants:
                    raise ValueError("Variants are derived from file profile definitions")
        return self
