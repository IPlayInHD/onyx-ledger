"""The terminal account delete does not destroy sealed evidence. It cannot run.

Entry 11B6C set out to measure what `DELETE FROM identity.user_account` does to
retained evidence. The answer turned out to be stronger than "it destroys it":
the database refuses the statement outright. `ioe.integrity_check` carries an
append-only guard with no escape hatch, and an `ON DELETE CASCADE` foreign key
pointing at a table that refuses DELETE is a cascade that can never fire.

That is why these classifications are DIRECT_SCHEMA_EVIDENCE rather than
inference, and it is why the registry's terminal-readiness gate failing closed
matches the database's own behaviour instead of merely anticipating it.

THE FIXTURE IS BUILT BY PRODUCTION SERVICES, reusing
`tests/security/test_sealed_history_after_purge.py`. A hand-inserted row would
prove nothing here: the point is that a genuinely sealed chain — analysis,
optimization, scenario, verification — is what the cascade meets.

NOTHING IS COMMITTED. Every destructive statement runs inside a transaction that
is rolled back, so the refusals are observed without the suite ever removing an
account.
"""

from __future__ import annotations

import psycopg2

from app.services.ioe.domain.integrity import IntegrityStatus
from app.services.ioe.replay.verification import IntegrityVerificationService
from tests.conftest import owner_dsn
from tests.security.test_sealed_history_after_purge import (
    _historical_account,
    _purge_account,
)

#: The context `ioe.reject_result_mutation` yields to. It is the sanctioned way
#: to remove immutable calculation evidence, so testing only outside it would
#: prove much less than it appears to.
PURGE_CONTEXT = "SET LOCAL app.allow_evidence_purge = 'on'"


async def _sealed_account():
    """A production-sealed account whose SOURCE_DATA purge has already run.

    Built per test rather than shared. A module-scoped async fixture binds its
    connections to one event loop, and `asyncio_mode = "auto"` gives each test
    its own — the shared version failed in teardown with futures attached to a
    dead loop, which is a fixture bug masquerading as a database result.
    """
    uid, analysis_id, run_id, scenario_id = await _historical_account()

    verified = await IntegrityVerificationService(uid).verify("optimization", run_id)
    assert verified.status is IntegrityStatus.VERIFIED, (
        f"the fixture does not verify before deletion ({verified.status}); "
        "a refusal measured against it would prove nothing"
    )

    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    _purge_account(conn.cursor(), uid)
    conn.close()
    return uid, analysis_id, run_id, scenario_id


def _refusal(statement: str, params: dict, purge_context: bool) -> str | None:
    """Run `statement`, roll it back, return the first error line or None."""
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = False
    try:
        cur = conn.cursor()
        if purge_context:
            cur.execute(PURGE_CONTEXT)
        try:
            cur.execute(statement, params)
            return None
        except psycopg2.Error as exc:
            return str(exc).splitlines()[0]
    finally:
        conn.rollback()
        conn.close()


async def test_the_terminal_account_delete_is_refused():
    """Question A, answered by the database rather than by reading the FKs.

    Both spellings matter. Without the sanctioned purge context a reader could
    say the refusal is incidental and the real path would go through; with it,
    that answer is closed off too.
    """
    uid, _, _, _ = await _sealed_account()
    for purge_context in (False, True):
        error = _refusal(
            "DELETE FROM identity.user_account WHERE id=%(u)s",
            {"u": str(uid)}, purge_context,
        )
        assert error is not None, (
            f"DELETE FROM identity.user_account SUCCEEDED against a sealed "
            f"account (purge_context={purge_context}). The append-only guard "
            "that made the terminal delete impossible is gone, and sealed "
            "evidence is now destroyed by account deletion."
        )
        assert "integrity_check is append-only" in error, (
            f"refused for a different reason (purge_context={purge_context}): {error}"
        )


async def test_the_retained_branch_roots_refuse_deletion():
    """Each classified retained root, inside the sanctioned purge context."""
    uid, _, _, _ = await _sealed_account()
    for branch in ("ioe.optimization_run", "analysis.analysis_run"):
        error = _refusal(
            f"DELETE FROM {branch} WHERE user_id=%(u)s",
            {"u": str(uid)}, purge_context=True,
        )
        assert error is not None, (
            f"DELETE FROM {branch} succeeded inside the purge context against "
            "a sealed account; its REPLAY_REQUIRED_RETAIN classification rests "
            "on that being refused"
        )


async def test_the_verified_scenario_refuses_deletion_and_the_unverified_one_does_not():
    """`ioe.scenario` is row-state dependent, shown on two rows of one table.

    This is the whole justification for `row_state_dependent=True`. The same
    statement against the same table gets opposite answers depending only on
    whether the scenario was ever verified — so a single table-wide verdict of
    either kind would be a false statement about half the rows.
    """
    uid, _, _, scenario_id = await _historical_account()

    unverified = _refusal(
        "DELETE FROM ioe.scenario WHERE id=%(s)s",
        {"s": str(scenario_id)}, purge_context=True,
    )
    assert unverified is None, (
        f"an unverified scenario already refuses deletion ({unverified}); the "
        "row-state distinction this classification records no longer holds"
    )

    verified = await IntegrityVerificationService(uid).verify("scenario", scenario_id)
    assert verified.status is IntegrityStatus.VERIFIED

    after = _refusal(
        "DELETE FROM ioe.scenario WHERE id=%(s)s",
        {"s": str(scenario_id)}, purge_context=True,
    )
    assert after is not None, (
        "a VERIFIED scenario can be deleted; the sealed half of "
        "DEIDENTIFY_THEN_RETAIN is no longer protected"
    )
    assert "integrity_check is append-only" in after, after
