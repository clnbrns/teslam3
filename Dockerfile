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
# Pin to 1 worker — single-VIN dashboard doesn't need parallelism, and extra
# workers each duplicate the ~200MB Python/httpx/SQLite footprint (was ~$26/mo
# in Railway memory billing). --limit-max-requests recycles the worker every
# 5000 requests as a belt-and-suspenders guard against slow memory creep.
CMD ["sh", "-c", "mkdir -p /data && exec uvicorn tesla_fleet.api:app --host 0.0.0.0 --port ${PORT} --workers 1 --no-access-log --limit-max-requests 5000"]
