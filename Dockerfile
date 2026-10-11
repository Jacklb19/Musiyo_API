FROM python:3.12.13-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.runtime.txt ./
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.runtime.txt
COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
COPY fixtures ./fixtures
RUN pip install --no-cache-dir --no-deps -e . && useradd --uid 10001 --create-home musiyo
USER musiyo
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers"]
