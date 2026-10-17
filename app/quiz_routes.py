"""Optional quiz endpoints; only reviewed and still authorized evidence is delivered."""
from fastapi import HTTPException, Request, Response

from .interface_copy import GUIDE_RATE_LIMIT_MESSAGE
from .quiz_contracts import GuideQuiz, GuideQuizAnswer, GuideQuizAnswerResult


def install_quiz_routes(app, service, limiter):
    def accept(request, response):
        if not limiter.accept(request.client.host if request.client else 'unknown'):
            raise HTTPException(429, GUIDE_RATE_LIMIT_MESSAGE, headers={'Retry-After': '60'})
        response.headers['Cache-Control'] = 'private, no-store'

    @app.get('/api/v1/guide/quiz', response_model=GuideQuiz)
    def quiz(request: Request, response: Response, tour_key: str, room_key: str | None = None, schema_version: int = 1):
        accept(request, response)
        if schema_version != 1:
            raise HTTPException(409, 'Versión de encuesta incompatible')
        try:
            return service.list(tour_key, room_key)
        except (ValueError, OSError):
            raise HTTPException(503, 'La encuesta no está disponible') from None

    @app.post('/api/v1/guide/quiz/answers', response_model=GuideQuizAnswerResult)
    def answer(body: GuideQuizAnswer, request: Request, response: Response):
        accept(request, response)
        try:
            result = service.answer(body)
        except (OSError, ValueError):
            # Invalid options and malformed private bundles disclose no private content.
            raise HTTPException(422, 'La respuesta no está disponible') from None
        if result is None:
            raise HTTPException(404, 'Pregunta no disponible')
        return result
