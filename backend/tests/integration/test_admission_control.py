"""Admission control against real PostgreSQL (Entry 10).

The concurrency assertions here are the ones that matter. A limiter that is
merely *usually* right is not a limiter: the read-then-write race it exists to
close is invisible at low load and opens under exactly the traffic the limit was
written for. So these tests run genuinely concurrent transactions — separate
sessions, dispatched with `asyncio.gather` — rather than a sequential loop that
would pass against a completely broken implementation.

Time is injected, never slept. A window-expiry test built on `sleep(60)` is a
minute of CI per assertion and flakes the first time a runner is slow.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.database.session import unit_of_work
from app.services.admission.policy import (
    POLICIES,
    OperationClass,
    RejectionReason,
    ScopeType,
    StoreFailurePolicy,
)
from app.services.admission.service import (
    GLOBAL_SCOPE_ID,
    AdmissionRejected,
    AdmissionService,
)


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _scope() -> str:
    """A fresh principal per test, so no test inherits another's counters."""
    return str(uuid.uuid4())


async def _admit(operation: OperationClass, scope_id: str, **kw):
    """One admission in its OWN transaction — which is what makes the
    concurrency tests meaningful."""
    async with unit_of_work(actor_type="admin") as session:
        return await AdmissionService(session).admit(
            operation, scope_id=scope_id, **kw
        )


async def _gather_admissions(operation: OperationClass, scope_id: str, n: int, **kw):
    """n simultaneous admissions. Returns (accepted, rejections)."""
    results = await asyncio.gather(
        *(_admit(operation, scope_id, **kw) for _ in range(n)),
        return_exceptions=True,
    )
    accepted = [r for r in results if not isinstance(r, BaseException)]
    rejected = [r for r in results if isinstance(r, AdmissionRejected)]
    other = [
        r for r in results
        if isinstance(r, BaseException) and not isinstance(r, AdmissionRejected)
    ]
    assert not other, f"unexpected failures: {other!r}"
    return accepted, rejected


# ---------------------------------------------------------------- rate limit --
@pytest.mark.asyncio
async def test_requests_within_the_allowance_are_admitted():
    scope = _scope()
    policy = POLICIES[OperationClass.OPTIMIZATION_RUN]
    allowance = policy.rate_allowance
    assert allowance is not None

    # Concurrency would bind first, so release each lease as we go: this test is
    # about the RATE control alone.
    for _ in range(allowance):
        async with unit_of_work(actor_type="admin") as session:
            service = AdmissionService(session)
            ticket = await service.admit(
                OperationClass.OPTIMIZATION_RUN, scope_id=scope)
            await service.release_ticket(ticket)


@pytest.mark.asyncio
async def test_the_request_after_the_allowance_is_rejected():
    scope = _scope()
    allowance = POLICIES[OperationClass.OPTIMIZATION_RUN].rate_allowance
    assert allowance is not None

    for _ in range(allowance):
        async with unit_of_work(actor_type="admin") as session:
            service = AdmissionService(session)
            await service.release_ticket(
                await service.admit(OperationClass.OPTIMIZATION_RUN, scope_id=scope))

    with pytest.raises(AdmissionRejected) as caught:
        await _admit(OperationClass.OPTIMIZATION_RUN, scope)
    assert caught.value.reason is RejectionReason.USER_RATE_LIMIT
    assert caught.value.status_code == 429
    assert caught.value.retry_after_seconds > 0


@pytest.mark.asyncio
async def test_a_new_window_restores_the_allowance():
    """No sleep: the clock is a parameter, so the window boundary is exact."""
    scope = _scope()
    base = datetime.now(tz=UTC).replace(second=0, microsecond=0)
    allowance = POLICIES[OperationClass.OPTIMIZATION_RUN].rate_allowance
    assert allowance is not None

    for _ in range(allowance):
        async with unit_of_work(actor_type="admin") as session:
            service = AdmissionService(session)
            await service.release_ticket(await service.admit(
                OperationClass.OPTIMIZATION_RUN, scope_id=scope, now=base))

    with pytest.raises(AdmissionRejected):
        await _admit(OperationClass.OPTIMIZATION_RUN, scope, now=base)

    # One minute later the counter is a different row entirely.
    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        ticket = await service.admit(
            OperationClass.OPTIMIZATION_RUN, scope_id=scope,
            now=base + timedelta(minutes=1),
        )
        await service.release_ticket(ticket)


@pytest.mark.asyncio
async def test_a_rejected_burst_cannot_overshoot_the_rate_allowance():
    """The rate counter is a single conditional statement, so a simultaneous
    burst lands exactly on the allowance rather than past it."""
    scope = _scope()
    allowance = POLICIES[OperationClass.CHEAP_READ].rate_allowance
    assert allowance is not None

    # CHEAP_READ tracks no concurrency, so every acceptance here is a rate
    # decision and nothing else.
    accepted, rejected = await _gather_admissions(
        OperationClass.CHEAP_READ, scope, allowance + 20)
    assert len(accepted) == allowance
    assert len(rejected) == 20


# --------------------------------------------------------------- concurrency --
@pytest.mark.asyncio
async def test_ten_concurrent_optimizations_admit_exactly_the_user_limit():
    """THE race test. Ten simultaneous transactions, limit of 2, no overshoot."""
    scope = _scope()
    limit = POLICIES[OperationClass.OPTIMIZATION_RUN].max_active_per_user
    assert limit == 2

    accepted, rejected = await _gather_admissions(
        OperationClass.OPTIMIZATION_RUN, scope, 10)

    assert len(accepted) == limit, f"overshoot: {len(accepted)} admitted for limit {limit}"
    assert len(rejected) == 10 - limit
    assert {r.reason for r in rejected} <= {
        RejectionReason.USER_CONCURRENCY_LIMIT,
        RejectionReason.USER_RATE_LIMIT,
    }

    async with unit_of_work(actor_type="admin") as session:
        live = await AdmissionService(session).active_count(
            OperationClass.OPTIMIZATION_RUN, scope_id=scope)
    assert live == limit


@pytest.mark.asyncio
async def test_releasing_a_lease_returns_the_slot():
    scope = _scope()
    accepted, _ = await _gather_admissions(OperationClass.SCENARIO_RUN, scope, 3)
    limit = POLICIES[OperationClass.SCENARIO_RUN].max_active_per_user
    assert len(accepted) == limit

    with pytest.raises(AdmissionRejected):
        await _admit(OperationClass.SCENARIO_RUN, scope)

    async with unit_of_work(actor_type="admin") as session:
        await AdmissionService(session).release_ticket(accepted[0])

    ticket = await _admit(OperationClass.SCENARIO_RUN, scope)
    assert ticket.holds_lease


@pytest.mark.asyncio
async def test_the_global_cap_refuses_a_user_who_is_within_their_own_limit():
    """Platform capacity is a separate control, and it answers 503 not 429.

    Uses INTEGRITY_VERIFY, whose global cap (20) is reachable with distinct
    principals each well inside their own per-user limit of 2.
    """
    operation = OperationClass.INTEGRITY_VERIFY
    policy = POLICIES[operation]
    global_cap = policy.max_active_global
    assert global_cap is not None

    # Drain the global pool from a clean baseline.
    async with unit_of_work(actor_type="admin") as session:
        from sqlalchemy import text
        await session.execute(text(
            "UPDATE admission.lease SET released_at = now(), "
            "release_reason = 'CANCELLED' "
            "WHERE operation_code = :op AND released_at IS NULL"),
            {"op": operation.value})

    held = []
    for _ in range(global_cap):
        held.append(await _admit(operation, _scope()))
    assert len(held) == global_cap

    # A brand-new principal, zero personal usage, still refused.
    with pytest.raises(AdmissionRejected) as caught:
        await _admit(operation, _scope())
    assert caught.value.reason is RejectionReason.GLOBAL_CAPACITY
    assert caught.value.status_code == 503, "platform saturation is not the caller's fault"

    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        for ticket in held:
            await service.release_ticket(ticket)


@pytest.mark.asyncio
async def test_releasing_a_ticket_also_returns_the_platform_slot():
    """A per-principal release that left the global lease behind would leak
    platform capacity one job at a time until nothing could be admitted."""
    scope = _scope()
    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        before = await service.active_count(
            OperationClass.OPTIMIZATION_RUN,
            scope_id=GLOBAL_SCOPE_ID, scope_type=ScopeType.GLOBAL)

    ticket = await _admit(OperationClass.OPTIMIZATION_RUN, scope)
    assert ticket.global_lease_id is not None

    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        during = await service.active_count(
            OperationClass.OPTIMIZATION_RUN,
            scope_id=GLOBAL_SCOPE_ID, scope_type=ScopeType.GLOBAL)
        assert during == before + 1
        await service.release_ticket(ticket)
        after = await service.active_count(
            OperationClass.OPTIMIZATION_RUN,
            scope_id=GLOBAL_SCOPE_ID, scope_type=ScopeType.GLOBAL)
    assert after == before


@pytest.mark.asyncio
async def test_a_rejected_principal_does_not_consume_platform_capacity():
    """When the per-user slot is refused, the global slot taken a moment earlier
    must be given back — otherwise a user hammering their own limit drains the
    platform's."""
    scope = _scope()
    async with unit_of_work(actor_type="admin") as session:
        before = await AdmissionService(session).active_count(
            OperationClass.SCENARIO_RUN,
            scope_id=GLOBAL_SCOPE_ID, scope_type=ScopeType.GLOBAL)

    accepted, rejected = await _gather_admissions(OperationClass.SCENARIO_RUN, scope, 8)
    assert rejected

    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        during = await service.active_count(
            OperationClass.SCENARIO_RUN,
            scope_id=GLOBAL_SCOPE_ID, scope_type=ScopeType.GLOBAL)
        # Exactly one global slot per ADMITTED operation; none for the refused.
        assert during == before + len(accepted)
        for ticket in accepted:
            await service.release_ticket(ticket)


# ------------------------------------------------------------- retry storms --
@pytest.mark.asyncio
async def test_twenty_identical_requests_create_one_operation():
    """The client-timeout retry loop, which is how duplicate expensive jobs are
    really created — not by malice, by a frontend doing its job."""
    scope = _scope()
    key = f"opt:{uuid.uuid4()}"

    results = await asyncio.gather(
        *(_admit(OperationClass.OPTIMIZATION_RUN, scope, dedupe_key=key)
          for _ in range(20)),
        return_exceptions=True,
    )
    tickets = [r for r in results if not isinstance(r, BaseException)]
    holders = [t for t in tickets if t.holds_lease]

    assert len(holders) == 1, "a retry storm created more than one expensive job"
    for ticket in tickets:
        if not ticket.holds_lease:
            # Every non-holder points at the ONE running operation.
            assert ticket.duplicate_of_active
            assert ticket.lease_id == holders[0].lease_id


@pytest.mark.asyncio
async def test_a_different_specification_is_a_different_operation():
    """Dedupe keys must not merge unrelated work."""
    scope = _scope()
    first = await _admit(OperationClass.OPTIMIZATION_RUN, scope,
                         dedupe_key=f"a:{uuid.uuid4()}")
    second = await _admit(OperationClass.OPTIMIZATION_RUN, scope,
                          dedupe_key=f"b:{uuid.uuid4()}")
    assert first.holds_lease and second.holds_lease
    assert first.lease_id != second.lease_id


@pytest.mark.asyncio
async def test_one_users_dedupe_key_never_reaches_another_users_operation():
    """A client-supplied key is not an identity. If the same string presented by
    a different principal resolved to the first principal's operation, a key
    would be a way to reach into another account."""
    key = "shared-client-key"
    user_a, user_b = _scope(), _scope()

    ticket_a = await _admit(OperationClass.SCENARIO_RUN, user_a,
                            dedupe_key=f"{user_a}:{key}")
    ticket_b = await _admit(OperationClass.SCENARIO_RUN, user_b,
                            dedupe_key=f"{user_b}:{key}")

    assert ticket_a.holds_lease and ticket_b.holds_lease
    assert ticket_a.lease_id != ticket_b.lease_id
    assert not ticket_b.duplicate_of_active


# ------------------------------------------------------------ lease recovery --
@pytest.mark.asyncio
async def test_an_expired_lease_stops_consuming_quota():
    """A worker that is OOM-killed never runs its release path. If quota
    recovery depended on that callback, one crash would lock a user out of
    their own account until an operator intervened."""
    scope = _scope()
    policy = POLICIES[OperationClass.OPTIMIZATION_RUN]
    limit = policy.max_active_per_user
    assert limit is not None

    now = datetime.now(tz=UTC)
    for _ in range(limit):
        await _admit(OperationClass.OPTIMIZATION_RUN, scope, now=now)

    with pytest.raises(AdmissionRejected) as caught:
        await _admit(OperationClass.OPTIMIZATION_RUN, scope, now=now)
    assert caught.value.reason is RejectionReason.USER_CONCURRENCY_LIMIT

    # The workers vanish. Nothing releases anything. Time simply passes.
    later = now + timedelta(seconds=policy.lease_seconds + 1)
    ticket = await _admit(OperationClass.OPTIMIZATION_RUN, scope, now=later)
    assert ticket.holds_lease, "an abandoned lease permanently consumed quota"


@pytest.mark.asyncio
async def test_the_sweep_marks_lapsed_leases_without_being_load_bearing():
    scope = _scope()
    now = datetime.now(tz=UTC)
    await _admit(OperationClass.SCENARIO_RUN, scope, now=now)

    later = now + timedelta(seconds=POLICIES[OperationClass.SCENARIO_RUN]
                            .lease_seconds + 1)
    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        # Already zero BEFORE the sweep: admission ignores expired leases, so
        # recovery does not depend on housekeeping having run.
        assert await service.active_count(
            OperationClass.SCENARIO_RUN, scope_id=scope, now=later) == 0
        assert await service.sweep_expired(now=later) >= 1


# ------------------------------------------------------------------- policy --
@pytest.mark.asyncio
async def test_every_operation_class_has_a_validated_policy():
    assert set(POLICIES) == set(OperationClass)
    for operation, policy in POLICIES.items():
        assert policy.operation is operation
        for value in (policy.per_user_per_minute, policy.max_active_per_user,
                      policy.max_active_global):
            assert value is None or value > 0
        if policy.max_active_per_user and policy.max_active_global:
            assert policy.max_active_per_user <= policy.max_active_global


@pytest.mark.asyncio
async def test_expensive_classes_fail_closed_and_cheap_reads_fail_open():
    """The whole point of stating the policy per class."""
    expensive = (
        OperationClass.OPTIMIZATION_RUN,
        OperationClass.SCENARIO_RUN,
        OperationClass.ANALYSIS_RUN,
        OperationClass.DOCUMENT_PROCESS,
        OperationClass.IMPORT_RUN,
        OperationClass.INTEGRITY_VERIFY,
        OperationClass.AUTH_ATTEMPT,
    )
    for operation in expensive:
        assert POLICIES[operation].on_store_failure is StoreFailurePolicy.FAIL_CLOSED, (
            f"{operation} would lose its protection during a database incident"
        )
    assert POLICIES[OperationClass.CHEAP_READ].on_store_failure is (
        StoreFailurePolicy.FAIL_OPEN
    )


@pytest.mark.asyncio
async def test_a_refused_attempt_still_counts_against_the_rate_window():
    """THE regression test for a defect that made the rate limit inert.

    `_reject` used to raise from inside the caller's transaction, and the raise
    rolled back the rate-counter increment the decision had just made. So the
    counter only ever recorded admissions that SUCCEEDED. A caller sitting at
    its concurrency cap was refused, its attempt vanished from the ledger, and
    it could retry without limit — each retry paying a full advisory-lock
    acquisition and a count query while holding a pooled connection. The rate
    limit, whose entire purpose is to make that storm cheap, never engaged.

    Measured before the fix: 50 attempts against a capped scope, allowance 8,
    left request_count at 2 — exactly the number that succeeded.

    Asserted here through `admission_guard`, because the fix lives at that seam:
    the decision is returned, the transaction commits, and the rejection is
    raised afterwards.
    """
    from app.services.admission.guard import admission_guard

    policy = POLICIES[OperationClass.OPTIMIZATION_RUN]
    allowance = policy.rate_allowance
    cap = policy.max_active_per_user
    assert allowance is not None and cap is not None
    assert allowance > cap, "the test needs headroom between the two limits"

    scope = _scope()
    held = []

    # Occupy the concurrency cap with leases that stay live.
    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        for _ in range(cap):
            ticket = await service.admit(
                OperationClass.OPTIMIZATION_RUN, scope_id=scope)
            assert ticket.holds_lease
            held.append(ticket)

    reasons: dict[str, int] = {}
    for _ in range(allowance * 3):
        try:
            async with admission_guard(
                OperationClass.OPTIMIZATION_RUN, scope_id=scope
            ):
                pass
            reasons["admitted"] = reasons.get("admitted", 0) + 1
        except AdmissionRejected as exc:
            reasons[exc.reason.value] = reasons.get(exc.reason.value, 0) + 1

    # The window fills, so the LATER retries are refused by the cheap rate
    # check rather than by the expensive concurrency path.
    assert reasons.get("USER_RATE_LIMIT", 0) > 0, (
        f"a capped caller retried {allowance * 3} times without ever hitting "
        f"the rate limit: {reasons}"
    )

    async with unit_of_work(actor_type="admin") as session:
        counted = await session.scalar(
            text("""
                SELECT coalesce(sum(request_count), 0)
                  FROM admission.rate_counter
                 WHERE scope_id = :s AND operation_code = :o
            """),
            {"s": scope, "o": OperationClass.OPTIMIZATION_RUN.value},
        )
    assert int(counted or 0) == allowance, (
        f"the window recorded {counted} attempts, not {allowance}; refusals "
        "are not being counted"
    )

    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        for ticket in held:
            await service.release_ticket(ticket)


@pytest.mark.asyncio
async def test_a_refused_admission_does_not_leak_a_platform_slot():
    """The rejection path now COMMITS, so the global lease it took on the way
    has to be handed back explicitly rather than rolled back.

    Before the decision survived the commit, a user-concurrency rejection
    discarded the global lease by rolling the transaction back. Now the
    transaction commits, and an unreleased platform slot would leak on every
    such rejection — draining global capacity through the one path that is
    supposed to cost nothing.
    """
    from app.services.admission.guard import admission_guard

    policy = POLICIES[OperationClass.OPTIMIZATION_RUN]
    cap = policy.max_active_per_user
    assert cap is not None

    scope = _scope()
    held = []
    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        for _ in range(cap):
            held.append(await service.admit(
                OperationClass.OPTIMIZATION_RUN, scope_id=scope))

    async with unit_of_work(actor_type="admin") as session:
        before = await AdmissionService(session).active_count(
            OperationClass.OPTIMIZATION_RUN,
            scope_id=GLOBAL_SCOPE_ID, scope_type=ScopeType.GLOBAL)

    with pytest.raises(AdmissionRejected):
        async with admission_guard(
            OperationClass.OPTIMIZATION_RUN, scope_id=scope
        ):
            pass

    async with unit_of_work(actor_type="admin") as session:
        after = await AdmissionService(session).active_count(
            OperationClass.OPTIMIZATION_RUN,
            scope_id=GLOBAL_SCOPE_ID, scope_type=ScopeType.GLOBAL)
    assert after == before, (
        "a rejected admission left a platform-capacity slot held"
    )

    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        for ticket in held:
            await service.release_ticket(ticket)
