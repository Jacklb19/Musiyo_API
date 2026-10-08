FROM python:3.12.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .
COPY alembic.ini ./
COPY migrations ./migrations
COPY fixtures ./fixtures
RUN useradd --uid 10001 --create-home musiyo
USER musiyo
CMD ["sh", "-c", "musiyo migrate && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
