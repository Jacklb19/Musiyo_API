"""Validator sessions and text-only corrections; publication is checked on every edit."""
import hashlib
import hmac
import logging
import os
import secrets
import uuid
from datetime import timedelta
from pathlib import Path

import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException

from .content_operations import reindex_in_session
from .content_tables import CONTENT
from .contracts import DetailCorrection, ValidatorProfile, ValidatorSession
from .db import aware, utc_now
from .public_repository import PublicRepository

COOKIE_NAME = "musiyo_session"
COOKIE_PATH = "/api/v1"
SESSION_SECONDS = 8 * 60 * 60
HASHER = PasswordHasher()
DUMMY_PASSWORD_HASH = HASHER.hash(secrets.token_urlsafe(32))
LOGGER = logging.getLogger(__name__)


INVITATION_FILE = "MUSIYO_VALIDATOR_INVITATION_FILE"
MINIMUM_INVITATION_LENGTH = 12


def invitation_code() -> str | None:
    """The shared code the owner hands to trusted validators, read at call time (rotation needs no restart).

    Registration stays closed unless a server-only file holds a code of at least 12 characters."""
    selected = os.getenv(INVITATION_FILE)
    if not selected:
        return None
    try:
        code = Path(selected).read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None
    return code if len(code) >= MINIMUM_INVITATION_LENGTH else None


class ValidatorAccess:
    def __init__(self, factory, now=utc_now):
        self.factory, self.now = factory, now

    @staticmethod
    def token_hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def session_row(self, db, token: str | None):
        if not token or len(token) > 256:
            return None
        sessions, accounts = CONTENT["validator_sessions"], CONTENT["validators"]
        return db.execute(sa.select(sessions.c.id, sessions.c.csrf_token, accounts.c.id.label("validator_id"),
            accounts.c.username, accounts.c.name).join(accounts, accounts.c.id == sessions.c.validator_id)
            .where(sessions.c.id == self.token_hash(token), sessions.c.expires_at > self.now(), accounts.c.active.is_(True))).mappings().first()

    @staticmethod
    def info(row) -> ValidatorSession:
        return ValidatorSession(schema_version=1, authenticated=row is not None,
            user=ValidatorProfile(username=row["username"], name=row["name"]) if row else None,
            csrf_token=row["csrf_token"] if row else None)

    def session(self, token: str | None) -> ValidatorSession:
        with self.factory() as db:
            return self.info(self.session_row(db, token))

    @staticmethod
    def write_lock(db):
        if db.bind.dialect.name == "sqlite":
            db.execute(sa.text("BEGIN IMMEDIATE"))

    @staticmethod
    def registration_enabled() -> bool:
        return invitation_code() is not None

    def limited(self, db, ip: str, now) -> bool:
        """Shared per-source budget for login and registration attempts (10 per minute).

        On PostgreSQL an advisory lock serializes each source across workers without keeping its address."""
        attempts = CONTENT["validator_login_attempts"]
        ip_hash = hashlib.sha256(ip.encode()).hexdigest()
        if db.bind.dialect.name == "postgresql":
            db.execute(sa.select(sa.func.pg_advisory_xact_lock(int(ip_hash[:15], 16))))
        db.execute(attempts.delete().where(attempts.c.occurred_at <= now - timedelta(seconds=60)))
        count = db.scalar(sa.select(sa.func.count()).select_from(attempts).where(attempts.c.ip_hash == ip_hash)) or 0
        if count >= 10:
            return True
        db.execute(attempts.insert().values(ip_hash=ip_hash, occurred_at=now))
        return False

    def open_session(self, db, account_id: str, username: str, name: str, now):
        sessions = CONTENT["validator_sessions"]
        db.execute(sessions.delete().where(sessions.c.expires_at <= now))
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute(sessions.insert().values(id=self.token_hash(token), validator_id=account_id,
            csrf_token=csrf, expires_at=now + timedelta(seconds=SESSION_SECONDS)))
        return token, ValidatorSession(schema_version=1, authenticated=True,
            user=ValidatorProfile(username=username, name=name), csrf_token=csrf)

    def register(self, username: str, name: str, password: str, code: str, ip: str):
        """Create an active validator when the invitation code matches, then sign it in."""
        expected = invitation_code()
        if expected is None:
            return 403, None, None
        now = self.now()
        accounts = CONTENT["validators"]
        with self.factory.begin() as db:
            self.write_lock(db)
            if self.limited(db, ip, now):
                return 429, None, None
            given = hashlib.sha256(code.strip().encode()).digest()
            if not hmac.compare_digest(given, hashlib.sha256(expected.encode()).digest()):
                return 401, None, None
            username = username.strip().lower()
            if db.scalar(sa.select(accounts.c.id).where(sa.func.lower(accounts.c.username) == username)):
                return 409, None, None
            account_id = str(uuid.uuid4())
            db.execute(accounts.insert().values(id=account_id, username=username, name=" ".join(name.split()),
                                                password_hash=HASHER.hash(password)))
            token, info = self.open_session(db, account_id, username, " ".join(name.split()), now)
        LOGGER.info("validator_registered validator_id=%s", account_id)
        return 200, token, info

    def login(self, username: str, password: str, ip: str):
        now = self.now()
        accounts = CONTENT["validators"]
        with self.factory.begin() as db:
            self.write_lock(db)
            if self.limited(db, ip, now):
                return 429, None, None
            matches = db.execute(sa.select(accounts).where(sa.or_(accounts.c.username == username.strip(),
                sa.func.lower(accounts.c.username) == username.strip().lower())).with_for_update()).mappings().all()
            # Legacy case variants must not select an arbitrary identity.
            account = matches[0] if len(matches) == 1 else None
            if account is not None and account["locked_until"] and aware(account["locked_until"]) > now:
                return 401, None, None
            matched = False
            try:
                matched = HASHER.verify(account["password_hash"] if account is not None else DUMMY_PASSWORD_HASH, password)
            except (VerificationError, InvalidHashError):
                pass
            if account is None:
                return 401, None, None
            if not matched or not account["active"]:
                # The attempt count is committed before the route returns a generic rejection.
                previous = account["failed_attempts"] if not account["locked_until"] else 0
                failed = previous + 1
                db.execute(accounts.update().where(accounts.c.id == account["id"]).values(
                    failed_attempts=failed, locked_until=now + timedelta(minutes=15) if failed >= 5 else None))
                return 401, None, None
            db.execute(accounts.update().where(accounts.c.id == account["id"]).values(failed_attempts=0, locked_until=None))
            token, info = self.open_session(db, account["id"], account["username"], account["name"], now)
            return 200, token, info

    def require_session(self, db, token: str | None, csrf: str | None):
        row = self.session_row(db, token)
        if row is None:
            raise HTTPException(401, "Sesión no disponible")
        if not csrf or len(csrf) > 256 or not secrets.compare_digest(csrf.encode(), row["csrf_token"].encode()):
            raise HTTPException(403, "Solicitud no autorizada")
        return row

    def logout(self, token: str | None, csrf: str | None):
        with self.factory.begin() as db:
            self.write_lock(db)
            row = self.require_session(db, token, csrf)
            table = CONTENT["validator_sessions"]
            db.execute(table.delete().where(table.c.id == row["id"]))

    def correct(self, slug: str, correction: DetailCorrection, token: str | None, csrf: str | None):
        now = self.now()
        with self.factory.begin() as db:
            self.write_lock(db)
            actor = self.require_session(db, token, csrf)
            public = PublicRepository(db, self.now)
            if public.element(slug) is None:
                raise HTTPException(403, "Ficha no disponible para corregir")
            details, blocks = CONTENT["element_details"], CONTENT["detail_blocks"]
            detail = db.execute(sa.select(details).where(details.c.element_id == slug).with_for_update()).mappings().first()
            if detail is None:
                raise HTTPException(403, "La ficha necesita migración antes de corregirse")
            known = {block["id"]: block for block in db.execute(sa.select(blocks).where(blocks.c.element_id == slug)).mappings()}
            if any(item.block_id not in known or known[item.block_id]["kind"] != "interpretation" for item in correction.interpretations):
                raise HTTPException(422, "Solo pueden corregirse las interpretaciones de esta ficha")
            if any(not known[item.block_id]["source_id"] and not (known[item.block_id]["context"] or "").strip() for item in correction.interpretations):
                raise HTTPException(422, "La interpretación necesita fuente o contexto antes de corregirse")
            values = {field: getattr(correction, field) for field in ("title", "description") if getattr(correction, field) is not None}
            fields = list(values)
            for item in correction.interpretations:
                db.execute(blocks.update().where(blocks.c.id == item.block_id).values(text=item.text))
                fields.append("interpretations." + item.block_id)
            db.execute(details.update().where(details.c.element_id == slug).values(**values,
                corrected_by=actor["validator_id"], corrected_at=now))
            reindex_in_session(db, slug, self.now)
            result = public.element(slug)
            if result is None:
                raise HTTPException(403, "Ficha no disponible para corregir")
            db.execute(CONTENT["validator_corrections"].insert().values(validator_id=actor["validator_id"],
                element_id=slug, fields=fields, corrected_at=now))
        # Logs contain identifiers and field names, never passwords or original/current text.
        LOGGER.info("validator_correction validator_id=%s element_id=%s fields=%s", actor["validator_id"], slug, fields)
        return result
