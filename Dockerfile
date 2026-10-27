# Attest: one container, FastAPI + server-rendered templates.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    ATTEST_DB=/app/state/attest.db \
    ATTEST_LOG_DIR=/app/logs \
    ATTEST_DATASET=data/v2/eval-block.jsonl.gz

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code, synthetic data, the committed findings snapshot, and eval history (for the eval panel).
COPY attest ./attest
COPY data ./data
COPY seed ./seed
COPY evals ./evals

RUN useradd --create-home --uid 10001 attest \
    && mkdir -p /app/state /app/logs && chown -R attest /app/state /app/logs
USER attest

EXPOSE 8080
# Cloud Run sets PORT. State is SQLite inside the container: fine for a single-instance demo,
# replaced by a managed database in production (see README).
CMD ["sh", "-c", "exec uvicorn attest.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
