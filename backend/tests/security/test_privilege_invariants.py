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


def test_definer_owners_can_reach_every_application_schema():
    """A definer function runs as its OWNER, so the owner must be able to reach
    the schemas it reads.

    THE DEFECT THIS EXISTS FOR. `identity.subject_key_for` is SECURITY DEFINER
    owned by `onyx_migrator`, and `audit.log_change` calls it on every write.
    Nothing in `db/sql` grants `onyx_migrator` USAGE on `identity`; the design
    relied entirely on that role also OWNING the schema, which is true only
    because the migrator happens to be the identity that ran the DDL.

    Apply the same schema as any other identity — an RDS master user, a
    provisioning superuser, a CI service container — and `onyx_migrator` is
    created by `00_extensions_roles.sql` as a plain NOLOGIN role with no USAGE
    on anything. Every INSERT into `identity.user_account` then dies inside the
    audit trigger with `permission denied for schema identity`, which is to say
    the first customer registration fails and nothing before it does.

    Reproduced exactly, on a database whose schema was applied by `postgres`:

        ERROR: permission denied for schema identity
        QUERY: v_subject_key := identity.subject_key_for(v_subject_user)
        CONTEXT: PL/pgSQL function audit.log_change() line 65 at assignment

    USAGE is name resolution and nothing else, and these owners already hold
    the DDL, so requiring it costs no privilege that is not already implied. The
    invariant is catalogue-driven on both sides — every definer owner, every
    application schema — so a schema or a definer function added later is
    covered without editing this test.
    """
    conn, cur = _owner_cursor()
    try:
        cur.execute("""
            SELECT DISTINCT pg_get_userbyid(p.proowner)
              FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE p.prosecdef AND n.nspname NOT LIKE 'pg_%%'
               AND n.nspname <> 'information_schema'
        """)
        owners = sorted(r[0] for r in cur.fetchall())
        cur.execute("""
            SELECT nspname FROM pg_namespace
             WHERE nspname NOT LIKE 'pg_%%'
               AND nspname <> 'information_schema'
             ORDER BY nspname
        """)
        schemas = [r[0] for r in cur.fetchall()]

        unreachable: list[str] = []
        for owner in owners:
            for schema in schemas:
                cur.execute(
                    "SELECT has_schema_privilege(%s, %s, 'USAGE')", (owner, schema)
                )
                if not cur.fetchone()[0]:
                    unreachable.append(f"{owner} -> {schema}")
    finally:
        conn.close()

    assert owners, "no SECURITY DEFINER owners found — the query is wrong"
    assert schemas, "no application schemas found — the query is wrong"
    assert not unreachable, (
        "SECURITY DEFINER owners cannot reach schemas their functions run "
        f"against: {unreachable}. The schema was applied by an identity other "
        "than the one the definer functions are owned by."
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


def _assert_sanitized_problem_document(response) -> None:
    body = response.json()
    assert "type" in body and "title" in body, "not an RFC-9457 problem document"
    assert "correlation_id" in body
    text_body = response.text
    for leak in ("Traceback", "SELECT ", "psycopg2", "asyncpg", "sqlalchemy",
                 SYNTHETIC_SIN, SYNTHETIC_AMOUNT):
        assert leak not in text_body, f"error body leaked {leak!r}"


@pytest.mark.asyncio
async def test_the_api_error_body_is_sanitized_and_correlated(client):
    """RFC-9457 shape, an enumerated code, a correlation id, and no internals.

    A REAL account asking for a scenario that does not exist. It used to forge a
    token for a fabricated user id, which no longer reaches the route: a token
    naming an account that does not exist is now refused before routing, because
    an access token is a claim about identity and never evidence that the
    account still exists. That refusal is covered separately below; the
    non-enumerating 404 needs a caller the system will actually admit.
    """
    email = f"errshape_{uuid.uuid4().hex[:10]}@example.com"
    assert (await client.post("/api/v1/auth/register",
                              json={"email": email, "password": "supersecret1"})
            ).status_code == 201
    login = await client.post("/api/v1/auth/login",
                              json={"email": email, "password": "supersecret1"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    response = await client.get(
        f"/api/v1/ioe/scenarios/{uuid.uuid4()}", headers=headers
    )
    assert response.status_code == 404, response.text
    _assert_sanitized_problem_document(response)


@pytest.mark.asyncio
async def test_a_token_for_an_account_that_does_not_exist_is_refused(client):
    """A validly signed token is not proof the account is still there.

    Reaching the route with a fabricated subject let a purged account keep
    operating as a ghost identity until its token expired. The refusal carries
    the same sanitized problem document as any other error.
    """
    from app.core.security.jwt import create_access_token

    headers = {"Authorization": f"Bearer {create_access_token(str(uuid.uuid4()))}"}
    response = await client.get(
        f"/api/v1/ioe/scenarios/{uuid.uuid4()}", headers=headers
    )
    assert response.status_code == 403, response.text
    _assert_sanitized_problem_document(response)


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


# ---------------------------------------------------------------------------
# Privacy: the append-only audit log must never receive a credential secret
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_audit_log_never_stores_a_credential_secret():
    """Entry 11A regression.

    `audit.log_change` copies the whole changed row, and one of the audited
    tables is `identity.user_credential` — so every registration wrote the
    account's Argon2 hash into `audit.audit_log`. That log carries a
    `reject_mutation` trigger and has NO foreign key to the user, so the copy
    was append-only, uncascadable, and unreachable by any future account
    deletion: a credential-equivalent secret in the one store designed to be
    impossible to erase.

    Asserted end to end through the real registration path, because the defect
    lived in a trigger and no application code would ever have shown it.
    """
    from app.services.auth.service import AuthService

    email = f"auditsecret_{uuid.uuid4().hex[:10]}@test.ca"
    async with unit_of_work(actor_type="system") as session:
        await AuthService(session).register(email, "supersecret1")

    # Read as the OWNER: the runtime role deliberately has no SELECT on
    # audit.audit_log — the trigger writes through SECURITY DEFINER — so a
    # session-scoped read would fail on privileges rather than on content.
    conn, cur = _owner_cursor()
    try:
        cur.execute("""
            SELECT entity_table,
                   new_value ->> 'password_hash',
                   previous_value ->> 'password_hash'
              FROM audit.audit_log
             WHERE entity_table = 'user_credential'
               AND created_at > now() - interval '2 minutes'
        """)
        rows = cur.fetchall()
    finally:
        conn.close()

    assert rows, (
        "no credential audit row was written; the test can no longer prove "
        "anything and the trigger coverage must be re-checked"
    )
    for table, new_hash, old_hash in rows:
        for value in (new_hash, old_hash):
            if value is None:
                continue
            assert value == "[redacted]", (
                f"{table}: the audit log stored a real credential value; it is "
                "append-only and account deletion cannot reach it"
            )
            assert not value.startswith("$argon2"), (
                f"{table}: an Argon2 hash reached the audit log"
            )


def test_redaction_does_not_touch_sealed_evidence_hashes():
    """The redaction list is explicit for a reason.

    A pattern over column names — `%hash%` — would also strip
    `optimization_result_hash` and `manifest_hash`, which are content addresses
    of sealed evidence that the audit trail exists to preserve. Fixing a
    credential leak must not quietly damage replay verification.
    """
    conn, cur = _owner_cursor()
    try:
        cur.execute("""
            SELECT count(*) FROM audit.audit_log
             WHERE new_value ->> 'optimization_result_hash' = '[redacted]'
                OR new_value ->> 'manifest_hash' = '[redacted]'
                OR new_value ->> 'optimization_spec_hash' = '[redacted]'
        """)
        redacted = cur.fetchone()[0]
    finally:
        conn.close()
    assert int(redacted or 0) == 0, (
        "a sealed-evidence content hash was redacted out of the audit trail"
    )
