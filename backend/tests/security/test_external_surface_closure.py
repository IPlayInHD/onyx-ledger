"""Entry 11B5I — the external-surface classifications, pinned where they drift.

`docs/privacy/h5i-external-surface-matrix.md` records the classification. A
document cannot notice when someone installs an SDK or adds a task argument, so
the claims that would silently become false are asserted here.

Each test below corresponds to a row of that matrix that is true today BECAUSE
OF AN ABSENCE — no provider client, no subject argument, no result payload. An
absence is exactly the kind of property that regresses without anyone deciding
to change it.
"""
from __future__ import annotations

import inspect
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent


# ---------------------------------------------------------------------- §30 --
def test_no_external_ai_provider_sdk_is_installed():
    """The strongest form of "no external AI copy": the client library that
    would be needed is not in the dependency lock at all.

    Checked against the LOCK rather than the imports, because an import can be
    added in one line while a dependency has to be deliberately installed and
    reviewed.
    """
    locks = [BACKEND / "requirements.lock.txt",
             BACKEND / "requirements-dev.lock.txt"]
    present = [p for p in locks if p.exists()]
    assert present, "no dependency lock found; this test would pass vacuously"

    forbidden = ("anthropic", "openai", "cohere", "google-generativeai",
                 "replicate", "litellm", "langchain")
    for lock in present:
        for line in lock.read_text().splitlines():
            name = line.split("==")[0].split("[")[0].strip().lower()
            assert name not in forbidden, (
                f"{lock.name} installs {name!r}. Entry 11B5's matrix records "
                "NO EXTERNAL AI COPY; an installed provider SDK makes that "
                "claim unverifiable.")


def test_the_shipped_llm_and_embedder_are_in_process():
    """The adapters that ARE wired must stay local. The Anthropic adapter in
    `llm.py` is commented-out example code and must remain so until someone
    classifies the retention implications of sending tenant data out."""
    from app.integrations.embeddings import LocalHashEmbedder
    from app.integrations.llm import TemplateLlmClient

    for cls in (TemplateLlmClient, LocalHashEmbedder):
        source = inspect.getsource(cls)
        for token in ("http", "requests", "aiohttp", "httpx", "urllib"):
            assert token not in source.lower(), (
                f"{cls.__name__} performs network I/O ({token}); it is "
                "classified as in-process")


# ---------------------------------------------------------------------- §28 --
def test_the_privacy_task_takes_no_subject_argument():
    """A broker payload is durable for as long as the queue holds it. The task
    claims its own work from the database precisely so that no user id and no
    financial value is ever serialized into a Celery message."""
    from workers.tasks.privacy import run_account_deletion_phases

    fn = getattr(run_account_deletion_phases, "__wrapped__",
                 run_account_deletion_phases.run)
    parameters = list(inspect.signature(fn).parameters.values())

    assert all(p.default is not inspect.Parameter.empty for p in parameters), (
        f"the privacy task has a required argument: "
        f"{[p.name for p in parameters if p.default is inspect.Parameter.empty]}. "
        "It must claim its own subjects, so nothing identifying reaches the "
        "broker payload.")
    for parameter in parameters:
        assert "user" not in parameter.name and "subject" not in parameter.name, (
            f"the privacy task accepts {parameter.name!r}; a subject "
            "identifier in a task argument is a durable copy in Redis")


def test_celery_never_stores_task_results_or_error_payloads():
    """Entry 11A's finding, re-pinned: a SQLAlchemy error string renders the
    failing statement AND its parameters, so a database error during a
    financial write would put the amount into Redis."""
    from workers.celery_app import celery_app

    assert celery_app.conf.task_ignore_result is True, (
        "task results are being stored again")
    assert celery_app.conf.task_store_errors_even_if_ignored is False, (
        "error payloads are stored despite results being ignored — this is the "
        "single setting that puts exception text back into Redis")


# ---------------------------------------------------------------------- §22 --
def test_every_lifecycle_task_has_an_explicit_queue():
    """The privacy task fell through to Celery's default queue once already
    (found by the H2D sweep). The lifecycle also depends indirectly on the
    freshness relay and the integrity scheduler, so all three are pinned."""
    from workers.celery_app import celery_app

    routes = celery_app.conf.task_routes or {}
    expected = {
        "workers.tasks.privacy.run_account_deletion_phases": "privacy",
        "workers.tasks.ioe.relay_freshness_outbox": "ioe_freshness",
        "workers.tasks.ioe.verify_sealed_integrity": "ioe_integrity",
    }
    for task, queue in expected.items():
        assert task in routes, (
            f"{task} has no explicit route and would fall to the default queue")
        assert routes[task]["queue"] == queue, (
            f"{task} is routed to {routes[task]['queue']!r}, expected {queue!r}")


# ---------------------------------------------------------------------- §27 --
def test_structured_source_data_never_reaches_object_storage():
    """Object storage holds document binaries, governed by 11B4. Structured
    financial and profile state must not acquire a second home there — that
    would be a durable copy outside the SOURCE_DATA purge."""
    services = [
        BACKEND / "app" / "services" / "financial" / "service.py",
        BACKEND / "app" / "services" / "users" / "profile_service.py",
    ]
    for path in services:
        assert path.exists(), f"{path} not found; the test is looking in the wrong place"
        source = path.read_text()
        for token in ("ObjectStore", "presign_put", "presign_get", "put_object",
                      "s3", "boto"):
            assert token not in source, (
                f"{path.name} references object storage ({token}); structured "
                "source data would then live outside the purge boundary")


# ---------------------------------------------------------------------- §23 --
def test_the_node_application_shares_no_database_with_the_backend():
    """The Node classification rests on there being no join between the two
    systems. If `server/` ever gained a PostgreSQL client, an account deleted
    in the backend could leave a live counterpart there — and SEPARATE_APPLICATION
    would stop being true.
    """
    server = REPO / "server"
    if not server.exists():                      # nothing to classify
        return

    sources = []
    for pattern in ("*.js", "engine/*.js", "netlify/**/*.js", "scripts/*.cjs"):
        sources.extend(p for p in server.glob(pattern) if "node_modules" not in p.parts)
    assert sources, "no Node sources found; this test would pass vacuously"

    for path in sources:
        text = path.read_text(errors="ignore").lower()
        for token in ("require('pg')", 'require("pg")', "postgres://",
                      "postgresql://"):
            assert token not in text, (
                f"{path.name} talks to PostgreSQL ({token}). The Node app is "
                "classified SEPARATE_APPLICATION precisely because it shares no "
                "database with the backend; if that changed, PD-14 becomes an "
                "Entry 11B5 concern rather than an 11B9 one.")

    package = server / "package.json"
    if package.exists():
        deps = package.read_text().lower()
        for token in ('"pg"', '"postgres"', '"knex"', '"sequelize"', '"prisma"'):
            assert token not in deps, (
                f"server/package.json declares {token}; the Node app has "
                "acquired a database client")
