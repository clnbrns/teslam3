FROM python:3.12-slim

# Cache-bust marker — bump on big static-asset changes
ARG BUILD_REV=2026-05-04-mobile-pass

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/

RUN pip install --no-cache-dir -e .

# Railway mounts the volume here; SQLite + token store both live in /data.
ENV TESLA_DB_PATH=/data/tesla.db \
    TOKEN_STORE_PATH=/data/.tokens.json \
    PORT=8080

EXPOSE 8080

# Honor Railway's $PORT (defaults to 8080 locally).
CMD ["sh", "-c", "mkdir -p /data && uvicorn tesla_fleet.api:app --host 0.0.0.0 --port ${PORT}"]
