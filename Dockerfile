# Crypto Market Analysis & Spot Signal Dashboard
# Phase 1 image: FastAPI backend. The React frontend build stage is added in Phase 3
# and served by the same service, so Railway runs a single web service + PostgreSQL.

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r /app/backend/requirements.txt

COPY backend /app/backend
COPY scripts/start.sh /app/start.sh
RUN chmod +x /app/start.sh && chown -R appuser:appuser /app

USER appuser
WORKDIR /app/backend
EXPOSE 8000

CMD ["/app/start.sh"]
