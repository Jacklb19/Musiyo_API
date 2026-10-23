FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.runtime.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.runtime.txt
# Local fallback voice (Piper + es_AR daniela), fetched and verified at build time. Set MUSIYO_WITH_PIPER=0 to skip.
ARG MUSIYO_WITH_PIPER=1
COPY deployment/fetch_piper.py /tmp/fetch_piper.py
RUN if [ "$MUSIYO_WITH_PIPER" = "1" ]; then python /tmp/fetch_piper.py /opt/musiyo-voice; fi && rm /tmp/fetch_piper.py
COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
COPY fixtures ./fixtures
COPY scripts/transfer_museum.py ./scripts/transfer_museum.py
COPY deployment/fetch_content.py deployment/start.sh ./deployment/
COPY deployment/survey ./deployment/survey
RUN pip install --no-cache-dir --no-deps -e . && useradd --uid 10001 --create-home musiyo
USER musiyo
# Restores content on hosts that wipe the disk (MUSIYO_CONTENT_ARCHIVE_URL), then listens on $PORT (8000 by default).
CMD ["sh", "/app/deployment/start.sh"]
