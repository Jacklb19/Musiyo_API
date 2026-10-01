"""Validator sessions and text-only corrections; publication is checked on every edit."""
import hashlib
import logging
import secrets
from datetime import timedelta

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

    def login(self, username: str, password: str, ip: str):
        now = self.now()
        ip_hash = hashlib.sha256(ip.encode()).hexdigest()
        accounts, attempts, sessions = (CONTENT[name] for name in ("validators", "validator_login_attempts", "validator_sessions"))
        with self.factory.begin() as db:
            self.write_lock(db)
            if db.bind.dialect.name == "postgresql":
                # Serialize each source across workers without keeping the source address.
                lock_key = int(ip_hash[:15], 16)
                db.execute(sa.select(sa.func.pg_advisory_xact_lock(lock_key)))
            db.execute(attempts.delete().where(attempts.c.occurred_at <= now - timedelta(seconds=60)))
            count = db.scalar(sa.select(sa.func.count()).select_from(attempts).where(attempts.c.ip_hash == ip_hash)) or 0
            if count >= 10:
                return 429, None, None
            db.execute(attempts.insert().values(ip_hash=ip_hash, occurred_at=now))
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
            db.execute(sessions.delete().where(sessions.c.expires_at <= now))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.execute(sessions.insert().values(id=self.token_hash(token), validator_id=account["id"],
                csrf_token=csrf, expires_at=now + timedelta(seconds=SESSION_SECONDS)))
            info = ValidatorSession(schema_version=1, authenticated=True,
                user=ValidatorProfile(username=account["username"], name=account["name"]), csrf_token=csrf)
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
