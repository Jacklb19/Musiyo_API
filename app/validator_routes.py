"""Same-origin validator operations with secure cookies and session-bound CSRF."""
import os
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response

from .contracts import DetailCorrection, Element, LoginRequest, ValidatorSession
from .validator_access import COOKIE_NAME, COOKIE_PATH, SESSION_SECONDS, ValidatorAccess


def install_validator_routes(app, factory, now, web_origin=None):
    access = ValidatorAccess(factory, now)
    configured_origin = web_origin or os.getenv("MUSIYO_WEB_ORIGIN")

    def check_origin(request: Request):
        origin = request.headers.get("origin")
        if origin:
            expected = configured_origin or str(request.base_url).rstrip("/")
            try:
                parsed = urlsplit(origin)
            except ValueError:
                raise HTTPException(403, "Solicitud no autorizada")
            if origin != expected or parsed.username or parsed.path or parsed.query or parsed.fragment:
                raise HTTPException(403, "Solicitud no autorizada")

    def no_cache(response: Response):
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "Cookie"

    @app.get("/api/v1/auth/session", response_model=ValidatorSession)
    def session(request: Request, response: Response):
        no_cache(response)
        return access.session(request.cookies.get(COOKIE_NAME))

    @app.post("/api/v1/auth/login", response_model=ValidatorSession)
    def login(credentials: LoginRequest, request: Request, response: Response):
        check_origin(request)
        status, token, info = access.login(credentials.username, credentials.password.get_secret_value(),
            request.client.host if request.client else "unknown")
        if status != 200:
            raise HTTPException(status, "No fue posible iniciar sesión" if status == 401 else "Demasiados intentos; inténtalo más tarde")
        no_cache(response)
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True, secure=True,
            samesite="strict", path=COOKIE_PATH)
        return info

    @app.post("/api/v1/auth/logout", status_code=204)
    def logout(request: Request, response: Response):
        check_origin(request)
        access.logout(request.cookies.get(COOKIE_NAME), request.headers.get("x-csrf-token"))
        no_cache(response)
        response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH, secure=True, httponly=True, samesite="strict")

    @app.patch("/api/v1/validator/elements/{element_slug}", response_model=Element)
    def correct(element_slug: str, correction: DetailCorrection, request: Request, response: Response):
        check_origin(request)
        result = access.correct(element_slug, correction, request.cookies.get(COOKIE_NAME), request.headers.get("x-csrf-token"))
        no_cache(response)
        return result
