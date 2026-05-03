FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/

RUN pip install --no-cache-dir -e .

ENV TESLA_DB_PATH=/data/tesla.db
ENV TOKEN_STORE_PATH=/data/.tokens.json
EXPOSE 8080

CMD ["uvicorn", "tesla_fleet.api:app", "--host", "0.0.0.0", "--port", "8080"]
