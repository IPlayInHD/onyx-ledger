# syntax=docker/dockerfile:1
# ---- builder ----
FROM python:3.12-slim AS builder
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
COPY pyproject.toml .
RUN pip install --upgrade pip && pip install --prefix=/install .

# ---- runtime ----
FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
# non-root
RUN useradd --create-home --uid 10001 onyx
COPY --from=builder /install /usr/local
COPY app ./app
COPY workers ./workers
COPY db ./db
COPY scripts ./scripts
USER onyx
EXPOSE 8000
# One image runs the API or a worker, chosen by command.
#   api:    uvicorn app.main:app --host 0.0.0.0 --port 8000
#   worker: celery -A workers.celery_app worker -Q analysis,documents,ingestion,notify
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
