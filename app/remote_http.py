"""Bounded HTTPS calls to owner-selected external providers. Keys are server-only files.

No redirects, no inherited proxies, bounded responses, and errors never carry provider bodies,
keys or request content back to clients or logs.
"""
import json
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .guide_adapters import ProviderUnavailable

REPOSITORY = Path(__file__).resolve().parents[1]
ALLOWED_HOSTS = {"api.groq.com", "api.elevenlabs.io"}


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_secret(path: str | None) -> str:
    """Read a provider key from a small external file at call time (rotation needs no restart)."""
    if not path:
        raise ProviderUnavailable("Provider key not configured")
    try:
        selected = Path(path).resolve(strict=True)
        if selected.is_relative_to(REPOSITORY) or selected.stat().st_size > 512:
            raise ProviderUnavailable("Provider key must be a small file outside the repository")
        value = selected.read_text(encoding="utf-8-sig").strip()
    except OSError as error:
        raise ProviderUnavailable("Provider key unavailable") from error
    if not value or any(character.isspace() for character in value):
        raise ProviderUnavailable("Provider key unavailable")
    return value


def post(url: str, body: bytes, headers: dict[str, str], timeout: float, max_bytes: int) -> bytes:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password:
        raise ProviderUnavailable("Provider URL is not allowed")
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        with opener.open(Request(url, data=body, headers=headers, method="POST"), timeout=timeout) as response:
            if response.status != 200:
                raise ProviderUnavailable("Provider request failed")
            data = response.read(max_bytes + 1)
    except HTTPError as error:
        # 401/403 (key), 429 (quota) and 5xx are all an unavailable provider for the visitor.
        raise ProviderUnavailable(f"Provider returned {error.code}") from None
    except (URLError, TimeoutError, OSError) as error:
        raise ProviderUnavailable("Provider unreachable") from error
    if len(data) > max_bytes:
        raise ProviderUnavailable("Provider response too large")
    return data


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float, max_bytes: int) -> Any:
    data = post(url, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers | {"Content-Type": "application/json", "Accept": "application/json"}, timeout, max_bytes)
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as error:
        raise ProviderUnavailable("Provider returned invalid JSON") from error


def post_multipart(url: str, fields: dict[str, str], file: tuple[str, bytes, str], headers: dict[str, str],
                   timeout: float, max_bytes: int) -> Any:
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
             for name, value in fields.items()]
    name, content, mime = file
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                 f'Content-Type: {mime}\r\n\r\n'.encode() + content + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    data = post(url, b"".join(parts), headers | {"Content-Type": f"multipart/form-data; boundary={boundary}",
                                                 "Accept": "application/json"}, timeout, max_bytes)
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as error:
        raise ProviderUnavailable("Provider returned invalid JSON") from error
