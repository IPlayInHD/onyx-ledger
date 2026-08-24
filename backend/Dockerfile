# =============================================================================
# Onyx backend — production image
# =============================================================================
# One image, several commands. The API, every Celery worker, the beat scheduler
# and the migration task all run this artefact; the ECS task definition decides
# which. That is what makes "the image staging tested" and "the image production
# runs" the same bytes rather than the same tag.
#
# Two stages. The builder holds a compiler and the wheels it produced; the
# runtime holds neither. What ships is the interpreter, the installed packages
# and the application — no build toolchain to be borrowed by anything that gets
# in, and a smaller surface to have CVEs reported against.

# --- builder -----------------------------------------------------------------
# Pinned by digest, not by tag. `python:3.11-slim` is a moving target and a
# rebuild three weeks from now would silently resolve to different bytes; the
# reproducibility this image claims has to start at its own base.
#
# The digest is the one Docker Hub served for `3.11.15-slim-bookworm` when this
# file was written, read from the registry rather than copied from anywhere.
# Updating it is a deliberate act: change the tag AND the digest together, and
# let the dependency scan run against the result.
FROM python:3.11.15-slim-bookworm@sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build dependencies only — asyncpg and psycopg2 compile C extensions. None of
# this reaches the runtime stage.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      libpq-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# `--require-hashes` is the point of the lock file. Every one of the 1079 hashes
# in it must match or the build fails — a compromised or substituted package
# cannot be installed quietly. Copying only the lock first keeps this layer
# cached across application changes.
COPY requirements.lock.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r requirements.lock.txt

# --- runtime -----------------------------------------------------------------
FROM python:3.11.15-slim-bookworm@sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app

# libpq at runtime; the -dev headers and the compiler stay behind in the builder.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 \
 && rm -rf /var/lib/apt/lists/* \
 && apt-get purge -y --auto-remove

# A named, unprivileged user with no shell and no home to write to. `nobody`
# would work and is shared with everything else in the base image; a dedicated
# uid means a file owned by this application is identifiably this application's.
RUN groupadd --system --gid 10001 onyx \
 && useradd --system --uid 10001 --gid onyx --no-create-home --shell /usr/sbin/nologin onyx

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=onyx:onyx app ./app
COPY --chown=onyx:onyx workers ./workers
COPY --chown=onyx:onyx migrations ./migrations
COPY --chown=onyx:onyx alembic.ini ./alembic.ini
COPY --chown=onyx:onyx db ./db

# NOT COPIED, and each absence is deliberate:
#   tests/     the suite and its fixtures, including seeded credentials
#   scripts/   provisioning and gate tooling that expects a developer's machine
#   .env       never exists in an image; configuration arrives from Secrets Manager
#
# `.dockerignore` enforces this at the build-context level so a stray COPY
# cannot reintroduce them.

USER onyx

EXPOSE 8000

# No shell form anywhere: exec form means the process is PID 1 and receives
# SIGTERM directly, which is what makes graceful shutdown work. A shell wrapper
# would swallow the signal and ECS would eventually kill the container mid-request.
#
# The command is supplied by the task definition — this default exists so the
# image is runnable by hand for a smoke test.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "25"]
