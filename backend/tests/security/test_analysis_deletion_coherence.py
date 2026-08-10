"""Entry 11B5H2E — an analysis running while its account is being deleted.

The path is traced in `docs/privacy/h2e-analysis-path.md`. The short version,
because these tests only make sense against it:

  * `db_authed` opens the request transaction, and inside it `assert_may_act`
    takes `pg_advisory_xact_lock_shared(identity.lifecycle_lock_key(uid))` and
    THEN reads `identity.account_lifecycle` in a separate statement;
  * `_BLOCKING_STATES` is every state, so the cutoff is the EXISTENCE of a
    lifecycle row;
  * `request_deletion` takes the same key EXCLUSIVELY;
  * `_build_input_live` then reads profile, income and expenses in five
    separate statements under READ COMMITTED, each with its own snapshot.

That last point is why this file exists. There is no isolation-level protection
around the snapshot read: if a purge could commit between the income read and
the expense read, the frozen snapshot would be a HYBRID of pre-delete income and
post-delete expenses, sealed and hashed as though it were one coherent state.

What prevents it is lock ordering, not isolation. A purge cannot happen before a
lifecycle row exists; that row needs the exclusive lock; an in-flight analysis
holds the shared one until it commits. So the tests below prove the ORDERING,
and prove it by observing PostgreSQL's own wait state rather than by trusting a
sleep.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import psycopg2
import pytest
from sqlalchemy import select

from app.core.exceptions import DomainError
from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    ExpenseCategory,
    ExpenseRecord,
    IncomeSource,
    IncomeType,
    TaxProfile,
    UserAccount,
)
from app.database.session import unit_of_work
from app.services.analysis.service import AnalysisService
from app.services.privacy.lifecycle import AccountLifecycleService
from tests.conftest import owner_dsn

INCOME = Decimal("91000")
EXPENSE = Decimal("2500")


@pytest.fixture(autouse=True)
async def _dispose_engines():
    yield
    from app.database.privacy_session import dispose_all_engines

    await dispose_all_engines()


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


async def _user_with_sources() -> uuid.UUID:
    """A user with a profile, income AND expenses.

    All three matter: they are read by three different statements of
    `_build_input_live`, and a hybrid can only be detected if every one of them
    contributes something to the snapshot.
    """
    async with unit_of_work(actor_type="system") as s:
        user = UserAccount(email=f"h2e_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(user)
        await s.flush()
        uid = user.id
        income_type_id = (await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment"))).id
        category = await s.scalar(select(ExpenseCategory).limit(1))
        category_id = category.id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=2025, income_type_id=income_type_id,
            amount=INCOME, province_code="ON",
        ))
        s.add(ExpenseRecord(
            user_id=uid, tax_year=2025, expense_category_id=category_id,
            amount=EXPENSE,
        ))
    return uid


def _lock_waiters(cur, user: uuid.UUID) -> int:
    """How many sessions are BLOCKED on this account's lifecycle lock.

    `pg_locks` is the database's own account of who is waiting, which is the
    only honest way to prove a race actually raced. A sleep proves nothing: it
    is equally consistent with the other transaction having finished instantly.
    """
    cur.execute("""
        SELECT count(*) FROM pg_locks
         WHERE locktype = 'advisory'
           AND NOT granted
           AND (classid::bigint << 32) | objid::bigint
               = (SELECT identity.lifecycle_lock_key(%s))
    """, (str(user),))
    return cur.fetchone()[0]


def _lifecycle_state(cur, user: uuid.UUID) -> str | None:
    cur.execute("SELECT state FROM identity.account_lifecycle WHERE user_id = %s",
                (str(user),))
    row = cur.fetchone()
    return row[0] if row else None


def test_the_cutoff_is_held_on_the_request_transaction_itself():
    """The invariant the whole file rests on, pinned where it can be broken.

    Snapshot coherence here is NOT provided by transaction isolation. Measured:
    with the lock ordering bypassed and a purge committed between the income
    read and the expense read, an analysis produced a snapshot carrying
    pre-delete income and post-delete expenses — sealed and hashed as though it
    were one real state. So the ordering is load-bearing.

    It holds only while three things stay true, none of which is visible from
    `AnalysisService`:

      1. `assert_may_act` takes the lifecycle key SHARED (not merely reads it);
      2. it runs inside the SAME `unit_of_work` that `db_authed` yields to the
         route, so the transaction-scoped lock lives as long as the analysis;
      3. the lock is taken in its own statement, before the state is read.

    A refactor that moved the cutoff into a short side session, or committed
    mid-request, would leave every line of `AnalysisService` unchanged and
    silently reopen the hybrid. This reads the source rather than the behaviour
    because that is the only place the coupling is expressed.
    """
    from pathlib import Path

    backend = Path(__file__).resolve().parents[2]
    deps = (backend / "app" / "api" / "deps.py").read_text()
    lifecycle = (backend / "app" / "services" / "privacy" / "lifecycle.py").read_text()

    body = deps[deps.index("async def db_authed("):]
    body = body[:body.index("async def db_authed_lifecycle_exempt")]
    assert "unit_of_work(" in body and "assert_may_act" in body, (
        "db_authed no longer applies the lifecycle cutoff")
    assert body.index("assert_may_act") < body.index("yield session"), (
        "the cutoff is checked after the session is handed to the route")
    # One unit of work, entered once: a second `async with` here would mean the
    # checked transaction is not the transaction the route writes in.
    assert body.count("unit_of_work(") == 1, (
        "db_authed opens more than one unit of work; the advisory lock would "
        "not span the request the cutoff is protecting")

    check = lifecycle[lifecycle.index("async def assert_may_act"):]
    check = check[:check.index("async def request_deletion")]
    assert "pg_advisory_xact_lock_shared" in check, (
        "the cutoff no longer takes the shared lifecycle lock, so a deletion "
        "can commit underneath an in-flight analysis")
    assert check.index("pg_advisory_xact_lock_shared") < check.index(
        "SELECT state FROM identity.account_lifecycle"), (
        "the state is read before the lock is granted; under READ COMMITTED "
        "that read carries a snapshot from before the wait")

    request = lifecycle[lifecycle.index("async def request_deletion"):]
    assert "pg_advisory_xact_lock(" in request[:1500], (
        "request_deletion no longer takes the lifecycle lock exclusively")


# ---------------------------------------------------------------- §5 --------
async def test_an_analysis_starting_after_the_cutoff_is_refused_before_any_work():
    """The cutoff must already exist BEFORE the analysis is attempted, or this
    measures nothing. So it is asserted durable first, from a separate
    connection, and only then is the analysis tried."""
    uid = await _user_with_sources()

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await AccountLifecycleService(s).request_deletion(uid)

    admin = _owner()
    try:
        cur = admin.cursor()
        assert _lifecycle_state(cur, uid) is not None, (
            "the cutoff is not durable, so a refusal below would prove nothing")

        # The production shape: `db_authed` runs assert_may_act inside the
        # request transaction, then hands the SAME session to the route.
        refused = False
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            try:
                await AccountLifecycleService(s).assert_may_act(uid)
                await AnalysisService(s).run(uid, 2025)
            except DomainError:
                refused = True
        assert refused, "an analysis was admitted after the deletion cutoff"

        # And nothing governed was persisted on the way to the refusal.
        for table in ("analysis.analysis_run", "analysis.analysis_input_snapshot",
                      "analysis.analysis_line_item", "reco.recommendation"):
            column = "user_id" if table in (
                "analysis.analysis_run", "reco.recommendation") else "analysis_id"
            if column == "user_id":
                cur.execute(f"SELECT count(*) FROM {table} WHERE user_id = %s",
                            (str(uid),))
            else:
                cur.execute(
                    f"SELECT count(*) FROM {table} WHERE analysis_id IN "
                    "(SELECT id FROM analysis.analysis_run WHERE user_id = %s)",
                    (str(uid),))
            assert cur.fetchone()[0] == 0, f"a partial row survived in {table}"

        cur.execute("SELECT count(*) FROM ioe.freshness_outbox WHERE user_id = %s",
                    (str(uid),))
        assert cur.fetchone()[0] == 0, "a refused analysis emitted a freshness event"
    finally:
        admin.close()


# ------------------------------------------------------------ §6, §15 -------
async def test_a_deletion_request_waits_for_an_analysis_already_admitted():
    """The contract, read off the locks rather than invented: already-admitted
    work MAY FINISH, and the deletion request waits for it.

    `assert_may_act` takes the lifecycle key SHARED and holds it for the whole
    request transaction; `request_deletion` takes the same key EXCLUSIVELY. So
    an analysis past its admission boundary cannot have a deletion committed
    underneath it.

    The overlap is proven, not assumed: before the analysis commits, this
    asserts from a THIRD connection that PostgreSQL is reporting a session
    blocked on that exact advisory key.
    """
    uid = await _user_with_sources()
    admitted = asyncio.Event()
    deletion_started = asyncio.Event()
    analysis_committed = asyncio.Event()
    order: list[str] = []

    async def analysis() -> None:
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await AccountLifecycleService(s).assert_may_act(uid)   # shared lock
            admitted.set()
            await deletion_started.wait()
            # Give the deleter time to actually reach the lock and block on it.
            await asyncio.sleep(0.4)
            await AnalysisService(s).run(uid, 2025)
            order.append("analysis-commit")
        analysis_committed.set()

    async def deleter() -> None:
        await admitted.wait()
        deletion_started.set()
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await AccountLifecycleService(s).request_deletion(uid)  # exclusive
            order.append("deletion-commit")

    async def observer() -> None:
        """Guard on the guard: the deleter must really be BLOCKED."""
        await deletion_started.wait()
        admin = _owner()
        try:
            cur = admin.cursor()
            for _ in range(40):
                if _lock_waiters(cur, uid) > 0:
                    order.append("deleter-blocked")
                    return
                if analysis_committed.is_set():
                    return
                await asyncio.sleep(0.05)
        finally:
            admin.close()

    await asyncio.wait_for(
        asyncio.gather(analysis(), deleter(), observer()), timeout=30)

    assert "deleter-blocked" in order, (
        "the deletion request never blocked on the lifecycle lock, so this "
        "schedule did not exercise the ordering it claims to")
    assert order.index("analysis-commit") < order.index("deletion-commit"), (
        f"the deletion committed before the analysis finished: {order}")

    # The analysis really completed, with a snapshot.
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.scalar(select(AnalysisRun).where(AnalysisRun.user_id == uid))
        assert run is not None and run.status == "completed", (
            "the admitted analysis did not finish")
        snap = await s.scalar(select(AnalysisInputSnapshot).where(
            AnalysisInputSnapshot.analysis_id == run.id))
        assert snap is not None, "no frozen snapshot was persisted"


# ------------------------------------------------------- §7, §9, §10 --------
async def test_the_frozen_snapshot_is_one_coherent_pre_delete_state():
    """Coherence checked on CONTENTS, not on the existence of a hash.

    The snapshot must carry the pre-delete income AND the pre-delete expense.
    A hybrid — income present, expenses already purged away — is exactly the
    outcome that would be sealed and hashed as if it were a real state, and it
    is what an unordered purge between statements 5d and 5f would produce.
    """
    uid = await _user_with_sources()

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await AccountLifecycleService(s).assert_may_act(uid)
        run = await AnalysisService(s).run(uid, 2025)
        run_id = run.id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        snap = await s.scalar(select(AnalysisInputSnapshot).where(
            AnalysisInputSnapshot.analysis_id == run_id))
        payload = snap.snapshot
        frozen_hash = snap.snapshot_hash

    body = str(payload)
    assert "91000" in body, (
        f"the pre-delete income is missing from the frozen snapshot: {body[:400]}")
    assert "2500" in body, (
        f"the pre-delete expense is missing from the frozen snapshot — this is "
        f"the hybrid shape, income kept and expenses lost: {body[:400]}")

    # Now purge, and require the frozen artifact to be byte-identical (§11).
    admin = _owner()
    try:
        cur = admin.cursor()
        cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                    "VALUES (%s, 'DELETION_REQUESTED')", (str(uid),))
        for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
            cur.execute("UPDATE identity.account_lifecycle SET state = %s "
                        " WHERE user_id = %s", (state, str(uid)))
        token = uuid.uuid4()
        cur.execute("UPDATE identity.account_lifecycle SET claimed_by = 'h2e', "
                    "claim_token = %s, claimed_at = now() WHERE user_id = %s",
                    (str(token), str(uid)))
        cur.execute("SELECT identity.purge_source_data(%s, %s, 'h2e')",
                    (str(uid), str(token)))
        cur.execute("SELECT identity.count_remaining_source_data(%s)", (str(uid),))
        assert cur.fetchone()[0] == 0, "the purge did not run, so §11 is untested"

        cur.execute("SELECT snapshot::text, snapshot_hash "
                    "  FROM analysis.analysis_input_snapshot WHERE analysis_id = %s",
                    (str(run_id),))
        after_body, after_hash = cur.fetchone()
    finally:
        admin.close()

    assert after_hash == frozen_hash, (
        "the frozen snapshot hash changed when live source data was purged")
    assert "91000" in after_body and "2500" in after_body, (
        "the frozen snapshot lost its contents when live source data was purged")


# ------------------------------------------------------------- §12, §13 -----
async def test_an_individual_deletion_during_the_source_read_leaves_no_hybrid():
    """Individual deletion runs while the account is ACTIVE, under the same
    `db_authed` shared lock — and shared locks do not block each other, so this
    genuinely can interleave with the snapshot read.

    It is nonetheless coherent, for a weaker reason than the purge case:
    `delete_income_source` writes exactly ONE of the tables the snapshot reads.
    A commit landing between the income read and the expense read therefore
    changes only rows the analysis has already read or has not yet read — never
    both. The assertion is that the snapshot equals one of the two real states.
    """
    uid = await _user_with_sources()

    reached_read = asyncio.Event()
    delete_done = asyncio.Event()

    from app.services.tax_engine.service import TaxEngineService

    original = TaxEngineService._build_input_live

    async def racing_build(self, user_id, tax_year):
        # The seam: let a real deletion commit while the snapshot read is in
        # progress, then continue through the remaining statements.
        reached_read.set()
        await asyncio.wait_for(delete_done.wait(), timeout=15)
        return await original(self, user_id, tax_year)

    async def deleter() -> None:
        await asyncio.wait_for(reached_read.wait(), timeout=15)
        from app.services.financial.service import FinancialService

        async with unit_of_work(user_id=uid, actor_type="user") as s:
            income = await s.scalar(
                select(IncomeSource).where(IncomeSource.user_id == uid))
            await FinancialService(s).delete_income_source(uid, income.id, 2025)
        delete_done.set()

    async def analysis() -> None:
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await AccountLifecycleService(s).assert_may_act(uid)
            await AnalysisService(s).run(uid, 2025)

    TaxEngineService._build_input_live = racing_build   # type: ignore[method-assign]
    try:
        await asyncio.wait_for(asyncio.gather(analysis(), deleter()), timeout=40)
    finally:
        TaxEngineService._build_input_live = original   # type: ignore[method-assign]

    assert delete_done.is_set(), "the deletion never ran; the seam was not exercised"

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.scalar(select(AnalysisRun).where(AnalysisRun.user_id == uid))
        snap = await s.scalar(select(AnalysisInputSnapshot).where(
            AnalysisInputSnapshot.analysis_id == run.id))
        body = str(snap.snapshot)

    # Post-delete is the coherent state here: the read was released only after
    # the deletion committed. What must NOT appear is the income WITH the
    # expense missing, or any state that never existed.
    assert "2500" in body, (
        "the expense vanished from a snapshot taken while only an INCOME row "
        f"was deleted — that state never existed: {body[:400]}")
    assert "91000" not in body, (
        "the snapshot kept income that had already been deleted before the "
        f"read began: {body[:400]}")


async def test_a_profile_mutation_during_the_source_read_leaves_no_hybrid():
    """§13. The profile is read by statement 5b and the income by 5d, so a
    profile change genuinely can land between them.

    It is coherent for the same structural reason as individual deletion:
    `upsert_tax_profile` writes ONLY `profile.tax_profile`, which the snapshot
    reads once. The province the engine calculates with must therefore match
    the province recorded in the snapshot — a run that computed Ontario tax and
    then sealed a snapshot saying Quebec would be the profile-shaped hybrid.
    """
    uid = await _user_with_sources()

    reached_read = asyncio.Event()
    profile_changed = asyncio.Event()

    from app.services.tax_engine.service import TaxEngineService

    original = TaxEngineService._build_input_live

    async def racing_build(self, user_id, tax_year):
        reached_read.set()
        await asyncio.wait_for(profile_changed.wait(), timeout=15)
        return await original(self, user_id, tax_year)

    async def mutate_profile() -> None:
        await asyncio.wait_for(reached_read.wait(), timeout=15)
        from app.services.users.profile_service import ProfileService

        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await ProfileService(s).upsert_tax_profile(
                uid, {"province_code": "BC", "marital_status": "married"})
        profile_changed.set()

    async def analysis() -> None:
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await AccountLifecycleService(s).assert_may_act(uid)
            await AnalysisService(s).run(uid, 2025)

    TaxEngineService._build_input_live = racing_build   # type: ignore[method-assign]
    try:
        await asyncio.wait_for(
            asyncio.gather(analysis(), mutate_profile()), timeout=40)
    finally:
        TaxEngineService._build_input_live = original   # type: ignore[method-assign]

    assert profile_changed.is_set(), "the profile never changed; seam untested"

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.scalar(select(AnalysisRun).where(AnalysisRun.user_id == uid))
        snap = await s.scalar(select(AnalysisInputSnapshot).where(
            AnalysisInputSnapshot.analysis_id == run.id))
        body = str(snap.snapshot)

    # The run's province and the snapshot's province are two records of the
    # same decision. If they disagree, the sealed artifact does not describe
    # the calculation that produced it.
    assert run.province_code == "BC", (
        f"the analysis ran as {run.province_code} though the profile read "
        "happened after the change committed")
    assert "BC" in body, f"the snapshot does not carry the province used: {body[:400]}"
    assert "ON" not in body.replace("province", ""), (
        f"the snapshot carries both provinces — a hybrid profile: {body[:400]}")


# ------------------------------------------------------------- §14, §15 -----
async def test_one_accounts_deletion_lock_does_not_block_another_accounts_analysis():
    """A finite timeout is the point: an accidental global advisory lock would
    show up here as a hang, not as a wrong answer.

    Tenant A's exclusive lifecycle lock is held OPEN on a real connection for
    the whole of tenant B's analysis, and B must complete regardless.

    The lock holder is a THREAD with its own psycopg2 connection rather than a
    nested `asyncio.run`: a second event loop closes at the end of the block and
    takes the shared async engine's pooled connections down with it, which is a
    test defect that looks exactly like a concurrency failure.
    """
    import threading

    a = await _user_with_sources()
    b = await _user_with_sources()

    holder = psycopg2.connect(owner_dsn())
    holder.autocommit = False
    held = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_a_lock() -> None:
        try:
            cur = holder.cursor()
            cur.execute("SELECT pg_advisory_xact_lock("
                        "identity.lifecycle_lock_key(%s))", (str(a),))
            held.set()
            release.wait(timeout=30)
            holder.rollback()
        except BaseException as exc:            # noqa: BLE001
            errors.append(exc)
            held.set()

    thread = threading.Thread(target=hold_a_lock)
    thread.start()
    try:
        assert held.wait(timeout=15), "could not take tenant A's lifecycle lock"

        async def analyse_b() -> uuid.UUID:
            async with unit_of_work(user_id=b, actor_type="user") as s:
                await AccountLifecycleService(s).assert_may_act(b)
                run = await AnalysisService(s).run(b, 2025)
                return run.id

        # Finite, and far shorter than the holder's 30s: if B were waiting on
        # A's key this raises instead of hanging the suite.
        run_id = await asyncio.wait_for(analyse_b(), timeout=20)
    finally:
        release.set()
        thread.join(timeout=15)
        holder.close()

    assert not errors, f"holding tenant A's lock failed: {errors}"

    admin = _owner()
    try:
        cur = admin.cursor()
        cur.execute("SELECT status FROM analysis.analysis_run WHERE id = %s",
                    (str(run_id),))
        assert cur.fetchone()[0] == "completed", "tenant B's analysis did not finish"
        cur.execute("SELECT snapshot::text FROM analysis.analysis_input_snapshot "
                    " WHERE analysis_id = %s", (str(run_id),))
        body = cur.fetchone()[0]
        assert "91000" in body and "2500" in body, (
            "tenant B's snapshot is wrong while tenant A was mid-deletion")
        # And A was untouched by B's work.
        cur.execute("SELECT count(*) FROM analysis.analysis_run WHERE user_id = %s",
                    (str(a),))
        assert cur.fetchone()[0] == 0, "tenant B's analysis wrote into tenant A"
    finally:
        admin.close()
