"""Entry 11B5E — the SOURCE_DATA phase, driven by the real worker path.

Until this entry nothing drove the account lifecycle at all: Entry 11B2 built
`claim`/`advance`/`fail` and only tests called them. These exercise the service
the Celery task calls, against live PostgreSQL.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import psycopg2
import pytest
from sqlalchemy import func, select

from app.database.models import IncomeSource, UserAccount
from app.database.privacy_session import privacy_unit_of_work
from app.database.session import unit_of_work
from app.services.financial.service import FinancialService
from app.services.privacy import (
    AccountLifecycleService,
    LifecycleState,
    SourceDataPurgeService,
)
from app.services.users.profile_service import ProfileService
from tests.conftest import owner_dsn


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.privacy_session import dispose_privacy_engine
    from app.database.session import engine

    await engine.dispose()
    # The privacy engine is a module-level singleton too, and it holds POOLED
    # connections. Disposing only the application engine left them bound to a
    # dead event loop, so the next test failed on the loop rather than on
    # anything it asserted — the same trap `test_freshness_outbox_boundary.py`
    # documents, arriving through the second engine this slice introduced.
    await dispose_privacy_engine()


def _owner_cursor():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


async def _subject_with_source_data() -> uuid.UUID:
    """An account with profile, income across TWO tax years, and an expense."""
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"phase_{uuid.uuid4().hex[:10]}@example.com",
                        status="active")
        s.add(u)
        await s.flush()
        uid = u.id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await ProfileService(s).upsert_tax_profile(
            uid, {"province_code": "ON", "marital_status": "single"})
        fin = FinancialService(s)
        await fin.add_income(uid, 2024, "employment", Decimal("1000.00"), "Acme")
        await fin.add_income(uid, 2025, "employment", Decimal("2000.00"), "Acme")
        await fin.add_expense(uid, 2025, "medical", Decimal("50.00"))
    return uid


async def _walk_to_purging(user_id: uuid.UUID):
    """Drive the lifecycle to PURGING through the real transitions and return a
    fresh claim, which is what the worker holds when it runs a phase."""
    async with unit_of_work(user_id=user_id, actor_type="user") as s:
        await AccountLifecycleService(s).request_deletion(user_id)

    for nxt in (LifecycleState.ACCESS_DISABLED, LifecycleState.PURGE_PENDING,
                LifecycleState.PURGING):
        async with unit_of_work(actor_type="system") as s:
            svc = AccountLifecycleService(s)
            claimed = await _claim_subject(svc, user_id)
            assert claimed is not None, f"could not claim to reach {nxt}"
            assert await svc.advance(claimed, nxt, worker_id="test-worker")

    async with unit_of_work(actor_type="system") as s:
        return await _claim_subject(AccountLifecycleService(s), user_id)


async def _claim_subject(service, user_id: uuid.UUID, rounds: int = 20):
    """Claim until THIS subject appears.

    `claim_account_lifecycle` takes a bounded batch ordered oldest-first, so
    claiming once and expecting your own subject is an assertion about how many
    other lifecycles exist — the defect Entry 11B4I fixed in the PD-9 tests.
    """
    for _ in range(rounds):
        batch = await service.claim(worker_id="test-worker", batch_size=50)
        if not batch:
            return None
        for item in batch:
            if item.user_id == user_id:
                return item
    return None


def _remaining(user_id: uuid.UUID) -> int:
    conn = _owner_cursor()
    try:
        cur = conn.cursor()
        cur.execute("SELECT identity.count_remaining_source_data(%s)",
                    (str(user_id),))
        return cur.fetchone()[0]
    finally:
        conn.close()


def _phase_row(user_id: uuid.UUID):
    conn = _owner_cursor()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status, attempts, last_failure_code "
                    "  FROM identity.account_lifecycle_phase "
                    " WHERE user_id = %s AND phase = 'SOURCE_DATA'",
                    (str(user_id),))
        return cur.fetchone()
    finally:
        conn.close()


def _account_state(user_id: uuid.UUID) -> str:
    conn = _owner_cursor()
    try:
        cur = conn.cursor()
        cur.execute("SELECT state FROM identity.account_lifecycle "
                    " WHERE user_id = %s", (str(user_id),))
        return cur.fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Normal completion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_phase_purges_every_governed_source_class_and_completes():
    user = await _subject_with_source_data()
    assert _remaining(user) > 0, "the fixture created nothing to purge"

    claimed = await _walk_to_purging(user)
    assert claimed is not None

    async with privacy_unit_of_work() as s:
        outcome = await SourceDataPurgeService(s).run(
            claimed, worker_id="test-worker")

    assert outcome.completed, f"phase did not complete: {outcome.failure_code}"
    assert outcome.remaining == 0
    assert _remaining(user) == 0
    assert _phase_row(user)[0] == "COMPLETE"


@pytest.mark.asyncio
async def test_source_data_completing_does_not_complete_the_account():
    """Documents, audit and authentication de-identification all remain. A
    lifecycle that said COMPLETE here would be a status lying in the direction
    that matters."""
    user = await _subject_with_source_data()
    claimed = await _walk_to_purging(user)

    async with privacy_unit_of_work() as s:
        outcome = await SourceDataPurgeService(s).run(
            claimed, worker_id="test-worker")

    assert outcome.completed
    assert _phase_row(user)[0] == "COMPLETE"
    assert _account_state(user) == "PURGING", (
        "the account lifecycle advanced past PURGING on source data alone")


@pytest.mark.asyncio
async def test_the_purge_reaches_every_tax_year_partition():
    """Income exists for 2024 and 2025. The purge names the partitioned PARENT
    and PostgreSQL routes it; a hard-coded year list would leave one behind."""
    user = await _subject_with_source_data()
    claimed = await _walk_to_purging(user)

    async with privacy_unit_of_work() as s:
        await SourceDataPurgeService(s).run(claimed, worker_id="test-worker")

    conn = _owner_cursor()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM finance.income_source "
                    " WHERE user_id = %s", (str(user),))
        assert cur.fetchone()[0] == 0, "a tax-year partition was missed"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cross-account safety
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_purging_one_subject_leaves_another_untouched():
    victim = await _subject_with_source_data()
    bystander = await _subject_with_source_data()
    before = _remaining(bystander)
    assert before > 0

    claimed = await _walk_to_purging(victim)
    async with privacy_unit_of_work() as s:
        await SourceDataPurgeService(s).run(claimed, worker_id="test-worker")

    assert _remaining(victim) == 0
    assert _remaining(bystander) == before, "the purge crossed accounts"


# ---------------------------------------------------------------------------
# Completeness is authoritative
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_completion_is_refused_while_one_governed_row_remains():
    """The completion condition is the remaining COUNT, not what the deletes
    reported. A table the purge forgot reports nothing, which looks exactly
    like a table that was already empty."""
    user = await _subject_with_source_data()
    claimed = await _walk_to_purging(user)

    # Re-insert a governed row immediately after the purge, from outside the
    # keyhole, so the count is non-zero when completion is attempted.
    conn = _owner_cursor()
    try:
        cur = conn.cursor()
        cur.execute("SELECT identity.purge_source_data(%s, %s, 'test-worker')",
                    (str(user), str(claimed.claim_token)))
        cur.execute("""
            INSERT INTO finance.income_source
                (user_id, tax_year, income_type_id, amount, province_code)
            SELECT %s, 2025, id, 1, 'ON' FROM ref.income_type
             WHERE code = 'employment'
        """, (str(user),))
    finally:
        conn.close()

    assert _remaining(user) == 1

    async with privacy_unit_of_work() as s:
        outcome = await SourceDataPurgeService(s).run(
            claimed, worker_id="test-worker")

    # The purge inside `run` removes it again, so the count reaches zero and
    # the phase legitimately completes — which is the CONVERGING behaviour.
    # What must never happen is completion while the count is non-zero, and
    # that is asserted directly below against the database function.
    assert outcome.remaining == 0

    conn = _owner_cursor()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO finance.income_source
                (user_id, tax_year, income_type_id, amount, province_code)
            SELECT %s, 2025, id, 1, 'ON' FROM ref.income_type
             WHERE code = 'employment'
        """, (str(user),))
        with pytest.raises(psycopg2.Error) as caught:
            cur.execute(
                "SELECT identity.complete_lifecycle_phase("
                "  %s, 'SOURCE_DATA', %s, 'test-worker')",
                (str(user), str(claimed.claim_token)))
        assert "in-scope rows remain" in str(caught.value)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Crash and retry
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_crash_after_the_purge_converges_on_retry():
    """The worker purged and died before recording completion. The retry must
    purge again — idempotently, finding nothing — see a zero count, and
    complete. No error because the rows are already gone."""
    user = await _subject_with_source_data()
    claimed = await _walk_to_purging(user)

    # The purge happens; completion never does.
    conn = _owner_cursor()
    try:
        cur = conn.cursor()
        cur.execute("SELECT identity.purge_source_data(%s, %s, 'dead-worker')",
                    (str(user), str(claimed.claim_token)))
    finally:
        conn.close()
    assert _remaining(user) == 0
    assert _phase_row(user) is None, "no phase progress was recorded"

    async with privacy_unit_of_work() as s:
        outcome = await SourceDataPurgeService(s).run(
            claimed, worker_id="retry-worker")

    assert outcome.completed
    assert outcome.remaining == 0
    assert _phase_row(user)[0] == "COMPLETE"


@pytest.mark.asyncio
async def test_running_a_completed_phase_again_is_not_destructive():
    user = await _subject_with_source_data()
    claimed = await _walk_to_purging(user)

    async with privacy_unit_of_work() as s:
        assert (await SourceDataPurgeService(s).run(
            claimed, worker_id="test-worker")).completed

    async with privacy_unit_of_work() as s:
        again = await SourceDataPurgeService(s).run(
            claimed, worker_id="test-worker")

    assert again.remaining == 0
    assert _phase_row(user)[0] == "COMPLETE"


@pytest.mark.asyncio
async def test_a_lost_claim_is_reported_rather_than_forced():
    """A worker whose lease expired and was taken by another must not be able
    to purge or complete the subject it no longer holds."""
    user = await _subject_with_source_data()
    claimed = await _walk_to_purging(user)

    stale = claimed.__class__(
        user_id=claimed.user_id, state=claimed.state,
        requested_at=claimed.requested_at, claim_token=uuid.uuid4(),
        revision=claimed.revision)

    async with privacy_unit_of_work() as s:
        outcome = await SourceDataPurgeService(s).run(
            stale, worker_id="impostor")

    assert not outcome.completed
    assert outcome.failure_code == "CLAIM_LOST"
    assert _remaining(user) > 0, "an expired claim still purged the subject"


# ---------------------------------------------------------------------------
# Privacy of the phase record itself
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_phase_record_carries_no_source_content():
    """Assert the record EXISTS before asserting what it does not contain — a
    zero-row query would otherwise satisfy this vacuously, which is exactly how
    two Entry 11B5D tests passed while proving nothing."""
    user = await _subject_with_source_data()
    claimed = await _walk_to_purging(user)

    async with privacy_unit_of_work() as s:
        await SourceDataPurgeService(s).run(claimed, worker_id="test-worker")

    row = _phase_row(user)
    assert row is not None, "no phase record was written, so this proves nothing"

    blob = " ".join(str(v) for v in row)
    for marker in ("Acme", "1000", "2000", "example.com", "ON"):
        assert marker not in blob, f"{marker!r} reached the phase record"


@pytest.mark.asyncio
async def test_income_rows_are_gone_from_the_orm_view_too():
    """The purge runs inside a SECURITY DEFINER function under the subject's own
    RLS. Confirm the effect is visible through the ordinary application path,
    not only to the owner cursor."""
    user = await _subject_with_source_data()
    claimed = await _walk_to_purging(user)

    async with privacy_unit_of_work() as s:
        await SourceDataPurgeService(s).run(claimed, worker_id="test-worker")

    async with unit_of_work(user_id=user, actor_type="user") as s:
        remaining = await s.scalar(
            select(func.count()).select_from(IncomeSource)
            .where(IncomeSource.user_id == user))
    assert remaining == 0
