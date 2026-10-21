"""Select explicit adapters without contacting a provider during application startup."""
from .guide_adapters import (
    DisabledGeneration,
    LocalExtractiveGeneration,
    LocalHashEmbeddings,
)
from .guide_config import GuideSettings
from .guide_groq import FallbackGeneration, GroqGeneration
from .guide_ollama import OllamaEmbeddings, OllamaGeneration


def configured_embeddings(settings: GuideSettings):
    return OllamaEmbeddings(settings) if settings.embeddings == "ollama" else LocalHashEmbeddings()


def configured_generation(settings: GuideSettings):
    if settings.generation == "disabled":
        return DisabledGeneration()
    if settings.generation == "groq":
        primary = GroqGeneration(settings)
        return FallbackGeneration(primary, LocalExtractiveGeneration()) if settings.generation_fallback == "local" else primary
    return OllamaGeneration(settings) if settings.generation == "ollama" else LocalExtractiveGeneration()
