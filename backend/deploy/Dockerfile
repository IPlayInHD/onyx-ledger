# syntax=docker/dockerfile:1
#
# The production image installs from the HASH-PINNED LOCK, never from the
# declaration in pyproject.toml.
#
# It used to run `pip install .`, which resolves `pyproject.toml`'s version
# RANGES at build time. Two images built from the same commit could therefore
# contain different dependency trees, and neither would match the tree the
# quality gate tested — the gate installs `--require-hashes` from the lock, and
# the README already claimed production did the same. It did not.
#
# `--require-hashes` verifies every artifact, including transitive ones, against
# the committed hashes: a re-uploaded or substituted package fails the build
# instead of shipping. That is the supply-chain control this image was missing.

# ---- builder ----
FROM python:3.11-slim AS builder
WORKDIR /app
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1

# The lock first, and ONLY the lock: this layer is invalidated by a dependency
# change, not by an application edit.
COPY requirements.lock.txt ./
RUN pip install --upgrade pip \
 && pip install --prefix=/install --require-hashes -r requirements.lock.txt

# The package itself, with --no-deps so the editable/local install cannot
# resolve anything behind the lock's back.
COPY pyproject.toml README.md ./
COPY app ./app
COPY workers ./workers
RUN pip install --prefix=/install --no-deps .

# ---- runtime ----
FROM python:3.11-slim AS runtime
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
