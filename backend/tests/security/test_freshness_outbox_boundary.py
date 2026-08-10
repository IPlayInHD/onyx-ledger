"""The privileged outbox interface is a keyhole, not a door.

WHO RUNS WHAT, AND WHY IT IS TWO PRINCIPALS
Keyhole calls — claim, complete, fail, fan-out — run as the dedicated
freshness runtime, exactly as the production relay does. Direct reads of
`ioe.freshness_outbox` and its audit table run as the ORDINARY application
session, because `onyx_freshness_worker` holds EXECUTE on the functions and
deliberately no table privileges: reaching queue rows only through the narrow
function is the entire point of a SECURITY DEFINER keyhole.

Until Entry 11B5E6 these tests did both through one application-authenticated
session, which worked only because `onyx_app_rw` inherited the capability —
that inheritance is PD-16. Granting the worker table access would have made
them pass and dismantled the keyhole; splitting the principals is the fix.

The relay needs to cross tenants, so it gets exactly enough privilege to move
rows in one queue table between four states — and these tests are what hold that
"exactly". They check the boundary from both sides: that the interface behaves
correctly under concurrency and failure, and that the role which can call it
cannot reach anything else.

The last test is the one that matters most: proof that the worker role cannot
read a single user financial table. Every other control here is defence in depth
behind that one.
"""
import uuid
from datetime import UTC, datetime, timedelta

import psycopg2
import pytest
from sqlalchemy import func, select, text

from app.database.models import FreshnessOutbox, FreshnessOutboxAudit
from app.database.privacy_session import freshness_unit_of_work
from app.database.session import unit_of_work
from app.services.ioe.freshness_events import FreshnessEvent, emit
from app.services.ioe.freshness_relay import FreshnessRelay
from tests.conftest import owner_dsn

OWNER_DSN = owner_dsn()

#: Alternating claim rounds per worker when draining. Bounded on purpose — a
#: `while True` here would hang rather than fail if the claim ever stopped
#: making progress. Twenty rounds of 50 is 2000 events, far past any backlog a
#: suite rerun can build.
_DRAIN_ROUNDS = 20


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()
    from app.database.privacy_session import dispose_worker_engines

    await dispose_worker_engines()


async def _pending_event(tax_year: int = 2025) -> uuid.UUID:
    key = f"boundary:{uuid.uuid4()}"
    async with unit_of_work(actor_type="system") as s:
        await emit(s, FreshnessEvent.RULE_PUBLISHED, tax_year=tax_year, dedupe_key=key)
    async with unit_of_work(actor_type="system") as s:
        return await s.scalar(
            select(FreshnessOutbox.id).where(FreshnessOutbox.dedupe_key == key)
        )


async def _claim(worker: str, batch: int = 10) -> list[dict]:
    async with freshness_unit_of_work() as s:
        rows = await s.execute(
            text(
                "SELECT out_event_id AS id, out_claim_token AS token "
                "FROM ioe.claim_freshness_events(:b, :w)"
            ),
            {"b": batch, "w": worker},
        )
        return [dict(r._mapping) for r in rows]


async def _quiesce(limit: int = 200) -> None:
    """Drain until the outbox stops yielding work.

    `FreshnessRelay.drain()` is bounded — `max_passes=10` — and deliberately
    so: a producer emitting faster than the relay drains must not be able to
    spin there forever. That bound is correct for production and wrong for a
    test that needs an empty queue, because the security gate's database
    carries a backlog far larger than ten passes will clear. Measured there:
    over a thousand events pending, against a drain that clears ten passes.

    So the loop lives here, in the test, and production keeps its bound. The
    exit is a pass that claims nothing; `limit` is only a runaway guard.
    """
    for _ in range(limit):
        report = await FreshnessRelay("tenant-context-drain").drain()
        if report.claimed == 0:
            return
    raise AssertionError(
        "the outbox never went quiet; the relay is not making progress")


async def _claim_mine(worker: str, event_id: uuid.UUID,
                      rounds: int = _DRAIN_ROUNDS) -> dict:
    """Claim until THIS event belongs to `worker`, or fail loudly.

    Same reason as the drain in the concurrency test above. `_claim` takes a
    BOUNDED batch ORDERED oldest-first, so calling it once and expecting your
    own event back is an assertion about how much unrelated work is queued —
    and every ownership, retry, recovery and audit test below needs its event
    claimed by a named worker before it can test anything at all. None of them
    is testing batch position.

    Reproduced rather than assumed: with claimed rows ageing past
    `ioe.freshness_claim_timeout()` DURING a run — which is what the gate proof
    does, since it reruns this suite for twenty-odd minutes in one database —
    recovered rows re-enter the queue ahead of anything new, and the fourth run
    failed `test_a_different_worker_cannot_complete_someone_elses_claim` and
    `test_a_different_worker_cannot_fail_someone_elses_claim` together.
    """
    for _ in range(rounds):
        rows = await _claim(worker, batch=50)
        if not rows:
            raise AssertionError(
                f"the queue emptied before {worker} reached the event under "
                f"test; it is not claimable"
            )
        for row in rows:
            if row["id"] == event_id:
                return row
    raise AssertionError(f"{worker} never claimed the event under test")


async def _never_claimed(worker: str, event_id: uuid.UUID,
                         rounds: int = _DRAIN_ROUNDS) -> None:
    """The opposite, and it needs draining for the opposite reason: asserting
    an event is absent from ONE bounded batch is satisfied by the batch simply
    not reaching it. Drain, then assert it never appeared."""
    for _ in range(rounds):
        rows = await _claim(worker, batch=50)
        if not rows:
            return
        assert event_id not in {r["id"] for r in rows}, (
            "a terminal event was handed out again"
        )


async def _was_recovered(event_id: uuid.UUID) -> bool:
    """Did this event legitimately return to the queue between two claims?

    A second worker holding an event a first worker already had is a defect
    ONLY if the event never went back to the queue. `claim_freshness_events`
    recovers claims older than `ioe.freshness_claim_timeout()`, and a recovered
    event being handed to someone else is the abandoned-worker path working
    exactly as designed — the thing
    `test_an_abandoned_claim_is_recovered_and_audited` asserts must happen.

    The recovery writes a `claim_recovered` audit row, so the two cases are
    distinguishable from evidence rather than from timing. Without this, a
    recovery landing inside the drain loop reads as a double claim; a ten
    minute timeout makes that rare in reality and certain under a harness that
    ages claims deliberately.
    """
    async with unit_of_work(actor_type="system") as s:
        return bool(await s.scalar(
            select(func.count())
            .select_from(FreshnessOutboxAudit)
            .where(FreshnessOutboxAudit.event_id == event_id,
                   FreshnessOutboxAudit.transition == "claim_recovered")
        ))


async def _state(event_id: uuid.UUID) -> tuple[str, str | None, int]:
    async with unit_of_work(actor_type="system") as s:
        row = await s.execute(
            text(
                "SELECT claim_state, claimed_by, attempts "
                "FROM ioe.freshness_outbox WHERE id = :id"
            ),
            {"id": event_id},
        )
        return tuple(row.one())


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_two_workers_never_claim_the_same_event():
    """FOR UPDATE SKIP LOCKED plus the pending predicate: an event belongs to
    exactly one worker.

    THE CLAIM IS DRAINED RATHER THAN TAKEN ONCE, and that is not a stylistic
    preference. `claim_freshness_events` takes a BOUNDED batch ORDERED
    oldest-first, so what a single claim returns depends on how much unrelated
    work is already queued. Two 50-row claims against an empty queue happen to
    contain the six events this test just created; against a queue with fifty
    older rows in it they contain none of them, and the original assertion
    ("worker A claimed nothing") failed for a reason that had nothing to do
    with exclusivity.

    That is not hypothetical. `scripts/prove_security_gate.sh` reruns this
    suite around twenty-three times in ONE database, each run leaves claimed
    rows behind, and after ten minutes `ioe.freshness_claim_timeout()` recovers
    them to pending — ahead of anything new, because they are older. The test
    failed from the third run onward before Entry 11B4 and from the second run
    after it, which is what brought it inside the harness's window.

    Draining makes the assertion say what it means: every event this test
    created is claimed by exactly one worker, whatever else is in the queue.

    THE ROUNDS RUN TO COMPLETION rather than stopping once all six are owned.
    Stopping early made this test robust to the DEFECT as well as to the
    backlog — the first worker took all six, the loop exited, and the second
    worker never got the chance to take them a second time. Verified by
    injection: with the pending predicate widened to `IN ('pending','claimed')`
    the early-exit version passed. It has to keep asking.
    """
    ids = {await _pending_event() for _ in range(6)}

    owner: dict[uuid.UUID, str] = {}
    for worker in ("worker-a", "worker-b") * _DRAIN_ROUNDS:
        for row in await _claim(worker, batch=50):
            if row["id"] not in ids:
                continue
            if row["id"] in owner and not await _was_recovered(row["id"]):
                raise AssertionError(
                    f"event claimed by {owner[row['id']]} and again by {worker}"
                )
            owner[row["id"]] = worker

    assert set(owner) == ids, (
        f"only {len(owner)} of {len(ids)} events were claimed after "
        f"{2 * _DRAIN_ROUNDS} bounded claims"
    )


@pytest.mark.asyncio
async def test_a_claim_is_bounded_however_large_a_batch_is_requested():
    for _ in range(5):
        await _pending_event()
    async with freshness_unit_of_work() as s:
        rows = await s.execute(
            text(
                "SELECT count(*) FROM ioe.claim_freshness_events(:b, :w)"
            ),
            {"b": 100000, "w": "greedy-worker"},
        )
        claimed = rows.scalar()
    assert claimed <= 200, "the batch ceiling was not enforced"


@pytest.mark.asyncio
async def test_claim_ordering_is_deterministic():
    """Ordered by (created_at, id): a unique tiebreak, so two workers can never
    disagree about which row is next."""
    async with unit_of_work(actor_type="system") as s:
        plan = await s.execute(text(
            "SELECT prosrc FROM pg_proc p JOIN pg_namespace n "
            "ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'ioe' AND p.proname = 'claim_freshness_events'"
        ))
        source = plan.scalar()
    assert "ORDER BY c.created_at, c.id" in source
    assert "FOR UPDATE SKIP LOCKED" in source


# ---------------------------------------------------------------------------
# Ownership and idempotency
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_different_worker_cannot_complete_someone_elses_claim():
    event_id = await _pending_event()
    await _claim_mine("worker-owner", event_id)

    async with freshness_unit_of_work() as s:
        stolen = await s.scalar(
            text("SELECT ioe.complete_freshness_event(:id, :w)"),
            {"id": event_id, "w": "worker-impostor"},
        )
    assert stolen is False, "a worker completed an event it did not claim"

    state, owner, _ = await _state(event_id)
    assert state == "claimed"
    assert owner == "worker-owner"


@pytest.mark.asyncio
async def test_a_different_worker_cannot_fail_someone_elses_claim():
    event_id = await _pending_event()
    await _claim_mine("worker-owner", event_id)

    async with freshness_unit_of_work() as s:
        stolen = await s.scalar(
            text("SELECT ioe.fail_freshness_event(:id, :w, :e)"),
            {"id": event_id, "w": "worker-impostor", "e": "SOME_CODE"},
        )
    assert stolen is False
    assert (await _state(event_id))[0] == "claimed"


@pytest.mark.asyncio
async def test_duplicate_acknowledgement_is_a_no_op_not_an_error():
    """At-least-once delivery guarantees this happens, so it must be harmless."""
    event_id = await _pending_event()
    await _claim_mine("worker-dupe", event_id)

    async with freshness_unit_of_work() as s:
        first = await s.scalar(
            text("SELECT ioe.complete_freshness_event(:id, :w)"),
            {"id": event_id, "w": "worker-dupe"},
        )
        second = await s.scalar(
            text("SELECT ioe.complete_freshness_event(:id, :w)"),
            {"id": event_id, "w": "worker-dupe"},
        )
    assert first is True
    assert second is True, "a duplicate acknowledgement must be accepted quietly"
    assert (await _state(event_id))[0] == "completed"


@pytest.mark.asyncio
async def test_acknowledging_an_unknown_event_is_refused_not_fatal():
    async with freshness_unit_of_work() as s:
        result = await s.scalar(
            text("SELECT ioe.complete_freshness_event(:id, :w)"),
            {"id": uuid.uuid4(), "w": "worker-x"},
        )
    assert result is False


@pytest.mark.asyncio
async def test_a_failure_code_must_be_enumerated():
    """Exception text carries SQL fragments and row values. A queue record is
    not the place for either."""
    event_id = await _pending_event()
    await _claim_mine("worker-fail", event_id)

    for bad in ("could not connect: user 42 balance 1234.56", "lower case", ""):
        async with freshness_unit_of_work() as s:
            with pytest.raises(Exception, match="enumerated code"):
                await s.execute(
                    text("SELECT ioe.fail_freshness_event(:id, :w, :e)"),
                    {"id": event_id, "w": "worker-fail", "e": bad},
                )
            await s.rollback()


@pytest.mark.asyncio
async def test_a_failure_below_the_ceiling_returns_the_event_to_the_queue():
    event_id = await _pending_event()
    await _claim_mine("worker-retry", event_id)

    async with freshness_unit_of_work() as s:
        await s.execute(
            text("SELECT ioe.fail_freshness_event(:id, :w, :e)"),
            {"id": event_id, "w": "worker-retry", "e": "TENANT_APPLY_FAILED"},
        )
    state, owner, attempts = await _state(event_id)
    assert state == "pending", "a retryable failure must return to the queue"
    assert owner is None
    assert attempts == 1


@pytest.mark.asyncio
async def test_a_failure_at_the_ceiling_terminates():
    """A permanently broken event must stop, not spin."""
    event_id = await _pending_event()
    async with unit_of_work(actor_type="system") as s:
        await s.execute(
            text("UPDATE ioe.freshness_outbox SET attempts = 4 WHERE id = :id"),
            {"id": event_id},
        )
    await _claim_mine("worker-terminal", event_id)   # attempts becomes 5

    async with freshness_unit_of_work() as s:
        await s.execute(
            text("SELECT ioe.fail_freshness_event(:id, :w, :e)"),
            {"id": event_id, "w": "worker-terminal", "e": "TENANT_APPLY_FAILED"},
        )
    state, _owner, _attempts = await _state(event_id)
    assert state == "failed"

    # and it is not handed out again
    await _never_claimed("worker-terminal", event_id)


# ---------------------------------------------------------------------------
# Abandoned-claim recovery
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_abandoned_claim_is_recovered_and_audited():
    """A worker that dies mid-event must not strand it forever."""
    event_id = await _pending_event()
    await _claim_mine("worker-that-dies", event_id)
    assert (await _state(event_id))[0] == "claimed"

    # age the claim past the timeout
    async with unit_of_work(actor_type="system") as s:
        await s.execute(
            text("UPDATE ioe.freshness_outbox SET claimed_at = :old WHERE id = :id"),
            {"old": datetime.now(tz=UTC) - timedelta(hours=2), "id": event_id},
        )

    await _claim_mine("worker-that-lives", event_id)

    state, owner, _ = await _state(event_id)
    assert state == "claimed"
    assert owner == "worker-that-lives"

    async with unit_of_work(actor_type="system") as s:
        transitions = [
            r.transition for r in await s.scalars(
                select(FreshnessOutboxAudit)
                .where(FreshnessOutboxAudit.event_id == event_id)
                .order_by(FreshnessOutboxAudit.created_at)
            )
        ]
    assert "claim_recovered" in transitions


@pytest.mark.asyncio
async def test_a_worker_whose_claim_was_recovered_cannot_acknowledge_late():
    """The late acknowledgement must not overwrite whoever picked the work up."""
    event_id = await _pending_event()
    await _claim_mine("worker-slow", event_id)
    async with unit_of_work(actor_type="system") as s:
        await s.execute(
            text("UPDATE ioe.freshness_outbox SET claimed_at = :old WHERE id = :id"),
            {"old": datetime.now(tz=UTC) - timedelta(hours=2), "id": event_id},
        )
    await _claim_mine("worker-fast", event_id)

    async with freshness_unit_of_work() as s:
        late = await s.scalar(
            text("SELECT ioe.complete_freshness_event(:id, :w)"),
            {"id": event_id, "w": "worker-slow"},
        )
    assert late is False
    state, owner, _ = await _state(event_id)
    assert state == "claimed" and owner == "worker-fast"


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_every_claim_and_terminal_transition_is_audited():
    event_id = await _pending_event()
    await _claim_mine("worker-audited", event_id)
    async with freshness_unit_of_work() as s:
        await s.execute(
            text("SELECT ioe.complete_freshness_event(:id, :w)"),
            {"id": event_id, "w": "worker-audited"},
        )

    async with unit_of_work(actor_type="system") as s:
        rows = list(await s.scalars(
            select(FreshnessOutboxAudit)
            .where(FreshnessOutboxAudit.event_id == event_id)
            .order_by(FreshnessOutboxAudit.created_at)
        ))
    transitions = [r.transition for r in rows]
    assert "claimed" in transitions
    assert "completed" in transitions
    assert all(r.worker_id == "worker-audited" for r in rows)
    # the audit carries codes and ids, never a financial value
    columns = {c.name for c in FreshnessOutboxAudit.__table__.columns}
    assert not columns & {"amount", "tax", "income", "payload", "snapshot"}


# ---------------------------------------------------------------------------
# search_path hardening
# ---------------------------------------------------------------------------
def test_every_privileged_function_pins_its_search_path():
    """Without a pinned search_path a caller could shadow `ioe` with a schema of
    their own and have the definer's rights execute it."""
    conn = psycopg2.connect(OWNER_DSN)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.proname, p.prosecdef, p.proconfig
              FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'ioe'
               AND p.proname IN ('claim_freshness_events',
                                 'complete_freshness_event',
                                 'fail_freshness_event',
                                 'fan_out_freshness_event')
        """)
        rows = cur.fetchall()
        assert len(rows) == 4, f"expected 4 privileged functions, found {len(rows)}"
        for name, is_definer, config in rows:
            assert is_definer, f"{name} is not SECURITY DEFINER"
            assert config, f"{name} has no pinned search_path"
            pinned = [c for c in config if c.startswith("search_path=")]
            assert pinned, f"{name} does not pin search_path: {config}"
            assert "pg_catalog" in pinned[0], f"{name} search_path omits pg_catalog"
    finally:
        conn.close()


def test_no_privileged_function_accepts_a_table_name_or_sql_fragment():
    conn = psycopg2.connect(OWNER_DSN)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.proname, pg_get_function_arguments(p.oid)
              FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'ioe'
               AND p.proname IN ('claim_freshness_events',
                                 'complete_freshness_event',
                                 'fail_freshness_event',
                                 'fan_out_freshness_event')
        """)
        for name, args in cur.fetchall():
            lowered = args.lower()
            for smell in ("regclass", "query", "sql", "filter", "where",
                          "table", "column"):
                assert smell not in lowered, (
                    f"{name} takes a {smell!r}-shaped argument: {args}"
                )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The privilege boundary itself
# ---------------------------------------------------------------------------
def test_the_worker_role_cannot_read_any_user_financial_table():
    """The control everything else stands behind.

    The worker role exists to move queue rows. If it could read a financial
    table, the narrowness of the functions would not matter.
    """
    conn = psycopg2.connect(OWNER_DSN)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT n.nspname, c.relname
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relkind = 'r'
               AND n.nspname IN ('finance','wealth','analysis','reco','ioe',
                                 'identity','profile','docs','billing')
               AND (has_table_privilege('onyx_freshness_worker', c.oid, 'SELECT')
                 OR has_table_privilege('onyx_freshness_worker', c.oid, 'INSERT')
                 OR has_table_privilege('onyx_freshness_worker', c.oid, 'UPDATE')
                 OR has_table_privilege('onyx_freshness_worker', c.oid, 'DELETE'))
        """)
        reachable = cur.fetchall()
        assert not reachable, (
            "the freshness worker role can reach user tables directly: "
            f"{reachable}"
        )
    finally:
        conn.close()


def test_the_worker_role_can_execute_only_the_keyhole_functions():
    conn = psycopg2.connect(OWNER_DSN)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.proname
              FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname NOT IN ('pg_catalog','information_schema')
               AND p.prosecdef
               AND has_function_privilege('onyx_freshness_worker', p.oid, 'EXECUTE')
             ORDER BY p.proname
        """)
        executable = sorted({r[0] for r in cur.fetchall()})
        assert executable == [
            "claim_freshness_events",
            # integrity scheduling: identifiers only, same keyhole discipline
            "claim_integrity_targets",
            "complete_freshness_event",
            "fail_freshness_event",
            "fan_out_freshness_event",
            "recover_stale_integrity_checks",
        ], f"the worker can execute unexpected definer functions: {executable}"
    finally:
        conn.close()


def test_the_worker_role_cannot_log_in():
    """It is an assumable role, not an identity anyone can connect as."""
    conn = psycopg2.connect(OWNER_DSN)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT rolcanlogin, rolsuper, rolbypassrls FROM pg_roles "
            "WHERE rolname = 'onyx_freshness_worker'"
        )
        can_login, is_super, bypasses_rls = cur.fetchone()
        assert can_login is False
        assert is_super is False
        assert bypasses_rls is False, "the worker role bypasses RLS"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tenant context is genuinely applied
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_relay_applies_events_under_ordinary_tenant_context():
    """The staling itself must not be privileged: it happens in a session
    scoped to exactly the tenant the claimed event names."""
    from app.database.models import Scenario
    from app.services.ioe.scenario.service import ScenarioService
    from tests.integration.test_ioe_freshness_events import (
        _spec,
        _user_with_analysis,
    )

    uid_a, analysis_a = await _user_with_analysis()
    uid_b, analysis_b = await _user_with_analysis()

    # QUIESCE THE QUEUE BEFORE THE SUBJECTS EXIST, not after.
    #
    # `drain()` processes the outbox, and a tax-year-scoped event left pending
    # by any earlier test fans out to every tenant holding results in that year
    # — including B. That is the relay working correctly, but it makes "B is
    # still current" a statement about the shared queue rather than about
    # tenant scoping. On a fresh database the queue happens to be empty and the
    # assertion held; under the security gate, which runs this suite against
    # one database many times, 63 year-scoped events were pending and B was
    # legitimately staled by one of them.
    #
    # Draining AFTER creating the scenarios — the first attempt at this fix —
    # is not enough, and Entry 11B5J's third gate run failed on exactly that:
    # the backlog is then applied to scenarios that already exist, so B is
    # staled by the drain itself and the guard below fails with "tenant B is
    # not current before the event under test is emitted". Creating the
    # scenarios AFTER quiescence means no pending event can reach them, and the
    # only event that touches them is the one this test emits.
    await _quiesce()

    scenario_a = await ScenarioService(uid_a).simulate(analysis_a, _spec())
    scenario_b = await ScenarioService(uid_b).simulate(analysis_b, _spec())

    async with unit_of_work(user_id=uid_b, actor_type="user") as s:
        before = await s.get(Scenario, scenario_b.scenario_id)
        assert before.freshness_status == "current", (
            "tenant B is not current before the event under test is emitted, "
            "so this schedule cannot show whether A's event reached it")

    # An event scoped to A's analysis only, emitted in A's own transaction —
    # which is how producers emit, and which the outbox's own RLS requires: a
    # user-scoped event can only be written by that user's session.
    async with unit_of_work(user_id=uid_a, actor_type="user") as s:
        await emit(
            s, FreshnessEvent.ANALYSIS_COMPLETED,
            user_id=uid_a, analysis_id=analysis_a,
            dedupe_key=f"tenantctx:{uuid.uuid4()}",
        )

    await FreshnessRelay("tenant-context-worker").drain()

    async with unit_of_work(user_id=uid_a, actor_type="user") as s:
        a = await s.get(Scenario, scenario_a.scenario_id)
        assert a.freshness_status == "stale"
    async with unit_of_work(user_id=uid_b, actor_type="user") as s:
        b = await s.get(Scenario, scenario_b.scenario_id)
        assert b.freshness_status == "current", (
            "the relay reached across tenants"
        )


@pytest.mark.asyncio
async def test_the_claim_payload_carries_no_financial_columns():
    await _pending_event()
    async with freshness_unit_of_work() as s:
        result = await s.execute(
            text("SELECT * FROM ioe.claim_freshness_events(:b, :w)"),
            {"b": 5, "w": "payload-worker"},
        )
        columns = set(result.keys())
    assert columns == {
        "out_event_id", "out_claim_token", "out_stale_reason_code",
        "out_analysis_id", "out_tax_year", "out_user_id",
        # Entry 9: a bounded province code from `ref.province`, used to narrow
        # rule fan-out to one jurisdiction. Not a financial value and not
        # user-identifying — the payload already carries the tenant id and the
        # tax year, so this widens nothing a compromised worker could learn.
        "out_jurisdiction",
    }, f"the claim payload shape changed: {columns}"


@pytest.mark.asyncio
async def test_producer_events_are_transactional_not_broker_dependent():
    """Redis and Celery are downstream transport. The outbox is the record."""
    uid = uuid.uuid4()
    key = f"transactional:{uuid.uuid4()}"
    try:
        async with unit_of_work(actor_type="system") as s:
            await emit(
                s, FreshnessEvent.PROFILE_CHANGED, user_id=None, dedupe_key=key
            )
            raise RuntimeError("the business change failed after emitting")
    except RuntimeError:
        pass

    async with unit_of_work(actor_type="system") as s:
        survived = await s.scalar(
            select(func.count(FreshnessOutbox.id))
            .where(FreshnessOutbox.dedupe_key == key)
        )
    assert survived == 0, "an event outlived the change that caused it"
    del uid
