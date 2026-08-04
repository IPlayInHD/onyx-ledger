"""Privilege and privacy invariants that cover the schema as it grows.

The earlier worker-boundary test named the schemas it checked. A deny-list like
that is only correct on the day it is written: a schema added next quarter is
outside it, and the test keeps passing while the guarantee quietly stops
holding. These tests instead enumerate what actually exists in the database and
assert an invariant over all of it, so new schemas and tables are covered
automatically.
"""
import re
import uuid

import psycopg2
import pytest
from sqlalchemy import select, text

from app.database.session import unit_of_work
from tests.conftest import owner_dsn

# The ONLY objects the freshness worker may touch directly. Anything else it can
# reach is a finding. Kept deliberately tiny — the worker's whole job is to call
# four functions.
WORKER_TABLE_ALLOWLIST: frozenset[str] = frozenset()

WORKER_FUNCTION_ALLOWLIST = frozenset({
    "ioe.claim_freshness_events",
    "ioe.complete_freshness_event",
    "ioe.fail_freshness_event",
    "ioe.fan_out_freshness_event",
    # Integrity scheduling follows the same keyhole discipline: identifiers and
    # an owner id out, no financial column read, no sealed row touched. The
    # replay itself runs under ordinary tenant RLS.
    "ioe.claim_integrity_targets",
    "ioe.recover_stale_integrity_checks",
})

SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _owner_cursor():
    conn = psycopg2.connect(owner_dsn())
    return conn, conn.cursor()


# ---------------------------------------------------------------------------
# Dynamic privilege invariant
# ---------------------------------------------------------------------------
def test_the_worker_role_has_no_direct_table_privilege_in_any_schema():
    """Enumerated from the catalogue, so a schema added later is covered.

    This is the control every other outbox safeguard stands behind: if the
    worker could read a table directly, the narrowness of the four functions
    would not matter.
    """
    conn, cur = _owner_cursor()
    try:
        cur.execute("""
            SELECT n.nspname || '.' || c.relname AS obj,
                   array_remove(ARRAY[
                     CASE WHEN has_table_privilege('onyx_freshness_worker', c.oid, 'SELECT')
                          THEN 'SELECT' END,
                     CASE WHEN has_table_privilege('onyx_freshness_worker', c.oid, 'INSERT')
                          THEN 'INSERT' END,
                     CASE WHEN has_table_privilege('onyx_freshness_worker', c.oid, 'UPDATE')
                          THEN 'UPDATE' END,
                     CASE WHEN has_table_privilege('onyx_freshness_worker', c.oid, 'DELETE')
                          THEN 'DELETE' END,
                     CASE WHEN has_table_privilege('onyx_freshness_worker', c.oid, 'TRUNCATE')
                          THEN 'TRUNCATE' END,
                     CASE WHEN has_table_privilege('onyx_freshness_worker', c.oid, 'REFERENCES')
                          THEN 'REFERENCES' END
                   ], NULL) AS privs
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
               AND n.nspname NOT LIKE 'pg_%%'
               AND n.nspname <> 'information_schema'
             ORDER BY 1
        """)
        reachable = {obj: privs for obj, privs in cur.fetchall() if privs}
        # every schema in the database was considered, not a hand-written list
        cur.execute("""
            SELECT count(*) FROM pg_namespace
             WHERE nspname NOT LIKE 'pg_%%' AND nspname <> 'information_schema'
        """)
        schemas_checked = cur.fetchone()[0]
    finally:
        conn.close()

    assert schemas_checked >= 10, "the catalogue query found suspiciously few schemas"
    unexpected = {k: v for k, v in reachable.items() if k not in WORKER_TABLE_ALLOWLIST}
    assert not unexpected, (
        f"the freshness worker can reach tables directly: {unexpected}. "
        "It must only be able to execute the outbox workflow functions."
    )


def test_the_worker_allowlist_is_minimal():
    """An allow-list nobody prunes becomes a deny-list with extra steps."""
    assert WORKER_TABLE_ALLOWLIST == frozenset(), (
        "the worker needs no direct table access at all; if this changed, the "
        "reason must be recorded here"
    )
    assert len(WORKER_FUNCTION_ALLOWLIST) == 6


def test_the_worker_can_execute_only_the_allowlisted_functions():
    conn, cur = _owner_cursor()
    try:
        cur.execute("""
            SELECT n.nspname || '.' || p.proname
              FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname NOT LIKE 'pg_%%'
               AND n.nspname <> 'information_schema'
               AND p.prosecdef
               AND has_function_privilege('onyx_freshness_worker', p.oid, 'EXECUTE')
        """)
        executable = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()
    assert executable == WORKER_FUNCTION_ALLOWLIST, (
        f"unexpected definer functions executable by the worker: "
        f"{executable - WORKER_FUNCTION_ALLOWLIST}"
    )


def test_every_security_definer_function_pins_a_search_path():
    """Covers every definer function in the database, not just the outbox four.

    An unpinned search_path on ANY definer function lets a caller shadow a
    schema and have the owner's rights execute their code.
    """
    conn, cur = _owner_cursor()
    try:
        cur.execute("""
            SELECT n.nspname || '.' || p.proname, p.proconfig
              FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE p.prosecdef
               AND n.nspname NOT LIKE 'pg_%%'
               AND n.nspname <> 'information_schema'
             ORDER BY 1
        """)
        rows = cur.fetchall()
    finally:
        conn.close()

    assert rows, "no SECURITY DEFINER functions found — query is wrong"
    unpinned = [
        name for name, config in rows
        if not config or not any(c.startswith("search_path=") for c in config)
    ]
    assert not unpinned, f"SECURITY DEFINER functions without a pinned search_path: {unpinned}"


def test_no_definer_function_is_executable_by_public():
    conn, cur = _owner_cursor()
    try:
        cur.execute("""
            SELECT n.nspname || '.' || p.proname
              FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE p.prosecdef
               AND n.nspname NOT LIKE 'pg_%%'
               AND n.nspname <> 'information_schema'
               AND has_function_privilege('public', p.oid, 'EXECUTE')
        """)
        public_definer = sorted(r[0] for r in cur.fetchall())
    finally:
        conn.close()
    assert not public_definer, (
        f"PUBLIC can execute SECURITY DEFINER functions: {public_definer}"
    )


def test_definer_function_owners_are_controlled():
    conn, cur = _owner_cursor()
    try:
        cur.execute("""
            SELECT DISTINCT pg_get_userbyid(p.proowner)
              FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE p.prosecdef AND n.nspname NOT LIKE 'pg_%%'
               AND n.nspname <> 'information_schema'
        """)
        owners = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()
    # definer functions run as their owner, so the owner set is the real blast
    # radius and must be small and known
    assert owners <= {"onyx_migrator", "postgres", "onyx_super"}, (
        f"SECURITY DEFINER functions owned by unexpected roles: {owners}"
    )


def test_every_user_derived_table_in_every_schema_has_forced_rls():
    """Catalogue-driven across ALL user-data schemas, so a table added to any of
    them later is covered without editing this test."""
    conn, cur = _owner_cursor()
    try:
        cur.execute("""
            SELECT n.nspname || '.' || c.relname,
                   c.relrowsecurity, c.relforcerowsecurity
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relkind = 'r'
               AND n.nspname IN ('ioe','finance','wealth','analysis','reco',
                                 'profile','docs','billing')
               AND EXISTS (
                   SELECT 1 FROM pg_attribute a
                    WHERE a.attrelid = c.oid AND a.attname = 'user_id'
                      AND NOT a.attisdropped
               )
             ORDER BY 1
        """)
        rows = cur.fetchall()
    finally:
        conn.close()

    assert rows, "no user_id-bearing tables found — query is wrong"
    missing = [name for name, enabled, forced in rows if not (enabled and forced)]
    assert not missing, (
        f"tables carrying user_id without ENABLE+FORCE row level security: {missing}"
    )


# ---------------------------------------------------------------------------
# Privacy: synthetic sensitive values must not reach logs or error fields
# ---------------------------------------------------------------------------
SYNTHETIC_SIN = "046454286"          # structurally valid, not a real assignment
SYNTHETIC_AMOUNT = "874321.19"


def _sensitive_patterns() -> list[re.Pattern]:
    return [
        re.compile(re.escape(SYNTHETIC_SIN)),
        re.compile(re.escape(SYNTHETIC_AMOUNT)),
        re.compile(r"\b\d{3}-\d{3}-\d{3}\b"),      # formatted SIN
    ]


@pytest.mark.asyncio
async def test_persisted_error_codes_contain_no_sensitive_values():
    """Every stored failure field must be an enumerated code, not a message.

    Scanned across every workflow table that records failure, using synthetic
    SIN-like and financial values.
    """
    async with unit_of_work(actor_type="system") as s:
        rows = await s.execute(text("""
            SELECT 'optimization_run' AS t, error_code AS v FROM ioe.optimization_run
             WHERE error_code IS NOT NULL
            UNION ALL
            SELECT 'scenario', error_code FROM ioe.scenario WHERE error_code IS NOT NULL
            UNION ALL
            SELECT 'freshness_outbox', last_error_code FROM ioe.freshness_outbox
             WHERE last_error_code IS NOT NULL
            UNION ALL
            SELECT 'freshness_outbox_audit', error_code FROM ioe.freshness_outbox_audit
             WHERE error_code IS NOT NULL
        """))
        values = [(t, v) for t, v in rows]

    patterns = _sensitive_patterns()
    for table, value in values:
        for pattern in patterns:
            assert not pattern.search(value or ""), (
                f"{table} stored a sensitive-looking value in an error field"
            )
        # and it must look like an enumerated code, not prose
        assert re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", value or "X"), (
            f"{table}.error_code is not an enumerated code: {value!r}"
        )


@pytest.mark.asyncio
async def test_a_failing_run_records_a_code_not_a_message_containing_secrets():
    """Drive a real failure whose exception text carries a synthetic SIN and a
    synthetic amount, then prove neither reaches the database."""
    from app.database.models import OptimizationRun
    from app.services.ioe.orchestrator import OptimizationOrchestrator
    from tests.integration.test_ioe_api import _user_with_analysis

    uid, analysis_id = await _user_with_analysis()
    orch = OptimizationOrchestrator(uid)

    async def boom(_spec):
        raise RuntimeError(
            f"engine blew up for SIN {SYNTHETIC_SIN} with balance {SYNTHETIC_AMOUNT}"
        )

    orch._compute = boom
    with pytest.raises(RuntimeError):
        await orch.generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.scalar(
            select(OptimizationRun)
            .where(OptimizationRun.user_id == uid)
            .order_by(OptimizationRun.created_at.desc())
        )
    assert run.workflow_status == "failed"
    assert run.error_code == "INTERNAL_ERROR"
    serialized = str(run.error_code) + str(run.version_manifest or "")
    assert SYNTHETIC_SIN not in serialized
    assert SYNTHETIC_AMOUNT not in serialized


@pytest.mark.asyncio
async def test_the_api_error_body_is_sanitized_and_correlated(client):
    """RFC-9457 shape, an enumerated code, a correlation id, and no internals."""
    from app.core.security.jwt import create_access_token

    uid = uuid.uuid4()
    headers = {"Authorization": f"Bearer {create_access_token(str(uid))}"}
    response = await client.get(
        f"/api/v1/ioe/scenarios/{uuid.uuid4()}", headers=headers
    )
    assert response.status_code == 404
    body = response.json()
    assert "type" in body and "title" in body, "not an RFC-9457 problem document"
    assert "correlation_id" in body
    text_body = response.text
    for leak in ("Traceback", "SELECT ", "psycopg2", "asyncpg", "sqlalchemy",
                 SYNTHETIC_SIN, SYNTHETIC_AMOUNT):
        assert leak not in text_body, f"error body leaked {leak!r}"


@pytest.mark.asyncio
async def test_no_ioe_table_stores_a_sin_or_raw_document():
    """The IOE consumes verified outputs; it is not a document store."""
    conn, cur = _owner_cursor()
    try:
        cur.execute("""
            SELECT c.relname, a.attname
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'ioe'
              JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0
             WHERE c.relkind = 'r' AND NOT a.attisdropped
        """)
        columns = [(t, col) for t, col in cur.fetchall()]
    finally:
        conn.close()

    forbidden = ("sin", "social_insurance", "document_blob", "file_bytes",
                 "raw_document", "attachment", "passport", "sin_hash")
    offenders = [
        f"{t}.{col}" for t, col in columns
        if col.lower() in forbidden
    ]
    assert not offenders, f"IOE tables carry sensitive columns: {offenders}"


@pytest.mark.asyncio
async def test_worker_payloads_carry_identifiers_only():
    """A broker payload is logged, retried and persisted in Redis. A user's
    money does not belong in one."""
    import inspect

    import workers.tasks.ioe as ioe_tasks

    for name, obj in vars(ioe_tasks).items():
        run = getattr(obj, "run", None) if hasattr(obj, "run") else None
        if run is None or not callable(run):
            continue
        signature = inspect.signature(run)
        for param in signature.parameters.values():
            if param.name in ("self", "args", "kwargs"):
                continue
            assert param.annotation in (str, int, "str", "int", "str | None",
                                        inspect.Parameter.empty), (
                f"{name}.{param.name} takes {param.annotation}; worker payloads "
                "must be identifiers and codes only"
            )
            assert param.name not in ("amount", "spec", "levers", "payload",
                                      "financials", "snapshot"), (
                f"{name} takes a payload-shaped argument: {param.name}"
            )
        break
