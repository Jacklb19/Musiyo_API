"""Bounded opt-in voice endpoints with explicit fallbacks and disconnect cancellation."""
import asyncio
import base64
from collections.abc import Awaitable, Callable
from typing import Literal, TypeVar

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .guide_adapters import ProviderUnavailable
from .guide_contracts import GuideQuestion, GuideResponse
from .guide_repository import GuideRepository
from .guide_service import GuideService
from .public_repository import PublicRepository
from .voice_adapters import (
    MAX_OUTPUT_BYTES,
    MAX_OUTPUT_SECONDS,
    DisabledVoice,
    Synthesizer,
    Transcriber,
    WhisperCppTranscriber,
    WindowsSapiSynthesizer,
    WindowsSapiTranscriber,
    validate_wave,
    vocabulary_terms,
)
from .voice_config import VoiceSettings
from .voice_contracts import (
    GuideSpeech,
    GuideTranscription,
    GuideVoiceAnswer,
    VoiceCapabilities,
)

T = TypeVar("T")


async def connected(request: Request, operation: Awaitable[T], timeout: int) -> T:
    task = asyncio.ensure_future(operation)
    try:
        async with asyncio.timeout(timeout):
            while not task.done():
                if await request.is_disconnected():
                    raise HTTPException(499, "Consulta cancelada")
                await asyncio.wait({task}, timeout=0.1)
            return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def catalogue_vocabulary(factory) -> list[str]:
    with factory() as db:
        return vocabulary_terms(element.title for element in PublicRepository(db).elements())


class VoiceService:
    def __init__(self, guide: GuideService, settings: VoiceSettings | None = None,
                 transcriber: Transcriber | None = None, synthesizer: Synthesizer | None = None):
        self.guide = guide
        self.settings = settings or VoiceSettings.from_environment()
        transcribers: dict[str, Callable[[], Transcriber]] = {
            "whisper_cpp": lambda: WhisperCppTranscriber(self.settings),
            "windows_sapi": lambda: WindowsSapiTranscriber(self.settings, self.vocabulary)}
        selected = transcribers.get(self.settings.transcription)
        self.transcriber = transcriber or (selected() if selected else DisabledVoice())
        self.synthesizer = synthesizer or (WindowsSapiSynthesizer(self.settings)
                            if self.settings.synthesis == "windows_sapi" else DisabledVoice())
        self.busy = False

    async def vocabulary(self) -> list[str]:
        # Read per request so revoked or unpublished titles never bias recognition.
        return await run_in_threadpool(catalogue_vocabulary, self.guide.factory)

    async def answer(self, question: GuideQuestion) -> GuideVoiceAnswer:
        response, evidence = await run_in_threadpool(self.guide.ask_with_evidence, question)
        speech_status: Literal["ready", "disabled", "unavailable", "not_applicable"] = "not_applicable"
        audio = None
        if response.status == "answered":
            speech_status = "disabled" if self.settings.synthesis == "disabled" else "unavailable"
            if self.settings.synthesis != "disabled":
                try:
                    data = await asyncio.wait_for(self.synthesizer.synthesize(response.answer or ""),
                                                  self.settings.engine_timeout_seconds)
                    validate_wave(data, MAX_OUTPUT_SECONDS, MAX_OUTPUT_BYTES, input_audio=False)
                    audio = GuideSpeech(data_base64=base64.b64encode(data).decode("ascii"))
                    speech_status = "ready"
                except (ProviderUnavailable, TimeoutError, ValueError, OSError):
                    pass
            # A revocation/edit while synthesis runs must remove both text and sound.
            def still_current():
                with self.guide.factory() as db:
                    return GuideRepository(db, self.guide.now).revalidate(evidence)
            if not await run_in_threadpool(still_current):
                response = GuideResponse(schema_version=1, status="abstained", answer=None,
                                         citations=[], reason="insufficient_evidence")
                audio, speech_status = None, "not_applicable"
        return GuideVoiceAnswer(response=response, speech_status=speech_status, audio=audio)


def install_voice_routes(app, service: VoiceService):
    settings = service.settings

    def admit(request: Request):
        if request.headers.get("x-musiyo-voice-consent") != "request-only-v1":
            raise HTTPException(422, "Confirma el procesamiento local sin conservación")
        if not app.state.guide_limiter.accept(request.client.host if request.client else "unknown"):
            raise HTTPException(429, "Demasiadas consultas", headers={"Retry-After": "60"})
        if service.busy:
            raise HTTPException(503, "La voz está ocupada. Puedes escribir tu pregunta")
        service.busy = True

    @app.middleware("http")
    async def voice_cache_policy(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/guide/"):
            response.headers["Cache-Control"] = "private, no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/v1/guide/voice-capabilities", response_model=VoiceCapabilities)
    def capabilities():
        # Configuration is not engine readiness, Spanish quality or device support.
        return VoiceCapabilities(transcription="disabled" if settings.transcription == "disabled" else "configured",
                                 synthesis="disabled" if settings.synthesis == "disabled" else "configured",
                                 max_input_bytes=settings.max_input_bytes, max_input_seconds=settings.max_input_seconds,
                                 request_timeout_seconds=settings.request_timeout_seconds)

    @app.post("/api/v1/guide/transcriptions", response_model=GuideTranscription)
    async def transcribe(request: Request, schema_version: int = 1):
        if schema_version != 1:
            raise HTTPException(409, "Versión de contrato incompatible")
        admit(request)
        async def operation():
            if request.headers.get("content-type", "").lower() != "audio/wav":
                raise HTTPException(422, "Usa WAV PCM16 mono a 16 kHz")
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > settings.max_input_bytes:
                    raise HTTPException(413, "Audio demasiado grande")
            try:
                validate_wave(bytes(data), settings.max_input_seconds, settings.max_input_bytes)
            except ValueError:
                raise HTTPException(422, "Audio inválido o demasiado largo")
            if settings.transcription == "disabled":
                raise HTTPException(503, "La transcripción no está habilitada. Escribe tu pregunta")
            async def recognize():
                return await asyncio.wait_for(service.transcriber.transcribe(bytes(data)),
                                              settings.engine_timeout_seconds)
            try:
                text = await connected(request, recognize(), settings.request_timeout_seconds)
                return GuideTranscription(text=text)
            except (ProviderUnavailable, TimeoutError, ValueError, OSError):
                raise HTTPException(503, "No pude entender el audio. Escribe tu pregunta")
        try:
            # Consume the request body before disconnect polling (ASGI has one receiver).
            return await asyncio.wait_for(operation(), settings.request_timeout_seconds)
        except TimeoutError:
            raise HTTPException(503, "La transcripción tardó demasiado. Escribe tu pregunta")
        finally:
            service.busy = False

    @app.post("/api/v1/guide/voice-answers", response_model=GuideVoiceAnswer)
    async def voice_answer(body: GuideQuestion, request: Request):
        admit(request)
        try:
            return await connected(request, service.answer(body), settings.request_timeout_seconds)
        except TimeoutError:
            raise HTTPException(503, "La consulta tardó demasiado. Puedes escribir tu pregunta")
        finally:
            service.busy = False
