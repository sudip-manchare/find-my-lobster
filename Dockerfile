# syntax=docker/dockerfile:1.7

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOBSTERLINK_HOST=0.0.0.0 \
    LOBSTERLINK_PORT=8080 \
    LOBSTERLINK_DB_PATH=/app/data/lobsterlink.db \
    LOBSTERLINK_SESSION_TTL_SECONDS=2592000 \
    DATINGOPENCLAW_BASE_URL=https://datingopenclaw.com/api

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin appuser

COPY server.py datingopenclaw_client.py /app/
COPY frontend /app/frontend

RUN mkdir -p /app/data \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8080
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8080/api/docs', timeout=4)" || exit 1

CMD ["python", "server.py"]
