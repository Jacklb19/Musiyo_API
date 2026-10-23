"""Closing-conversation survey endpoints: questions for everyone, storage only with explicit consent."""
from fastapi import Header, HTTPException, Request, Response

from .interface_copy import GUIDE_RATE_LIMIT_MESSAGE
from .survey_contracts import GuideSurvey, GuideSurveyResponse, GuideSurveyStored
from .survey_service import CONSENT, SurveyUnavailable


def install_survey_routes(app, service, limiter):
    def accept(request, response):
        if not limiter.accept(request.client.host if request.client else 'unknown'):
            raise HTTPException(429, GUIDE_RATE_LIMIT_MESSAGE, headers={'Retry-After': '60'})
        response.headers['Cache-Control'] = 'private, no-store'

    @app.get('/api/v1/guide/survey', response_model=GuideSurvey)
    def survey(request: Request, response: Response, tour_key: str, schema_version: int = 1):
        accept(request, response)
        if schema_version != 1:
            raise HTTPException(409, 'Versión de encuesta incompatible')
        try:
            current = service.survey(tour_key)
        except (ValueError, OSError):
            raise HTTPException(503, 'Las preguntas no están disponibles') from None
        if current is None:
            raise HTTPException(404, 'No hay preguntas publicadas')
        return current

    @app.post('/api/v1/guide/survey/responses', response_model=GuideSurveyStored, status_code=201)
    def store(body: GuideSurveyResponse, request: Request, response: Response,
              consent: str | None = Header(default=None, alias='X-Musiyo-Survey-Consent')):
        accept(request, response)
        if consent != CONSENT:
            raise HTTPException(403, 'Hace falta el consentimiento para guardar las respuestas')
        try:
            service.store(body)
        except SurveyUnavailable:
            raise HTTPException(503, 'Ahora no se pueden guardar las respuestas') from None
        except (ValueError, OSError):
            raise HTTPException(422, 'Las respuestas no corresponden a las preguntas publicadas') from None
        return GuideSurveyStored()
