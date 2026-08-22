"""Admission control under failure (Entry 10 §45).

A gate that has only been watched succeeding is an assumption. Each case here
breaks something on purpose and asserts the DOCUMENTED behaviour — in particular
that the fail-open/fail-closed choice is real and per class, not a comment.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import OperationalError

from app.database.session import unit_of_work
from app.services.admission.policy import (
    AdmissionPolicy,
    OperationClass,
    RejectionReason,
    StoreFailurePolicy,
)
from app.services.admission.service import AdmissionRejected, AdmissionService


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


class _BrokenSession:
    """A session whose every statement fails, like a database that has gone away."""

    async def execute(self, *args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection lost"))

    async def scalar(self, *args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("connection lost"))


@pytest.mark.asyncio
async def test_an_expensive_operation_fails_closed_when_the_store_is_down():
    """The protection must not disappear during exactly the incident that makes
    overload most likely."""
    service = AdmissionService(_BrokenSession())  # type: ignore[arg-type]

    with pytest.raises(AdmissionRejected) as caught:
        await service.admit(
            OperationClass.OPTIMIZATION_RUN, scope_id=str(uuid.uuid4()))

    assert caught.value.reason is RejectionReason.ADMISSION_STORE_UNAVAILABLE
    assert caught.value.status_code == 429


@pytest.mark.asyncio
async def test_a_cheap_read_fails_open_when_the_store_is_down():
    """A limiter fault must not become a site outage for reads."""
    service = AdmissionService(_BrokenSession())  # type: ignore[arg-type]

    ticket = await service.admit(
        OperationClass.CHEAP_READ, scope_id=str(uuid.uuid4()))
    assert ticket.lease_id is None, "a fail-open admission must not invent a lease"


@pytest.mark.asyncio
async def test_the_store_failure_choice_is_stated_for_every_class():
    from app.services.admission.policy import POLICIES

    for operation, policy in POLICIES.items():
        assert isinstance(policy.on_store_failure, StoreFailurePolicy), operation


@pytest.mark.asyncio
async def test_a_stale_lease_does_not_block_a_later_admission():
    """Covered from the service side in test_admission_control; asserted here
    against a lease written to look abandoned by a crashed worker."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    scope = str(uuid.uuid4())
    async with unit_of_work(actor_type="admin") as session:
        # Two leases that expired an hour ago and were never released — exactly
        # what an OOM-killed worker leaves behind.
        for _ in range(2):
            await session.execute(text("""
                INSERT INTO admission.lease
                    (id, scope_type, scope_id, operation_code, acquired_at, expires_at)
                VALUES (:id, 'USER', :scope, 'OPTIMIZATION_RUN', :then, :expired)
            """), {
                "id": uuid.uuid4(), "scope": scope,
                "then": datetime.now(tz=UTC) - timedelta(hours=2),
                "expired": datetime.now(tz=UTC) - timedelta(hours=1),
            })

        service = AdmissionService(session)
        assert await service.active_count(
            OperationClass.OPTIMIZATION_RUN, scope_id=scope) == 0
        ticket = await service.admit(
            OperationClass.OPTIMIZATION_RUN, scope_id=scope)
        assert ticket.holds_lease, "abandoned leases blocked a legitimate admission"


@pytest.mark.asyncio
async def test_a_malformed_policy_is_refused_at_construction():
    """A limit of zero would admit nothing; a negative one is meaningless; a
    per-user cap above the platform cap could never be reached. All three are
    construction failures rather than limits that quietly misbehave."""
    with pytest.raises(ValueError, match="must be positive"):
        AdmissionPolicy(OperationClass.SCENARIO_RUN, per_user_per_minute=0)

    with pytest.raises(ValueError, match="must be positive"):
        AdmissionPolicy(OperationClass.SCENARIO_RUN, max_active_per_user=-1)

    with pytest.raises(ValueError, match="exceeds max_active_global"):
        AdmissionPolicy(
            OperationClass.SCENARIO_RUN,
            max_active_per_user=10, max_active_global=5,
        )

    with pytest.raises(ValueError, match="lease_seconds must be positive"):
        AdmissionPolicy(OperationClass.SCENARIO_RUN, lease_seconds=0)


@pytest.mark.asyncio
async def test_a_malformed_settings_value_is_refused_at_startup():
    from pydantic import ValidationError as PydanticValidationError

    from app.core.config import Settings

    for bad in (
        {"max_active_optimizations_per_user": 0},
        {"max_active_optimizations_per_user": -3},
        {"rate_limit_analysis_per_minute": 0},
        # soft > hard
        {"max_active_optimizations_per_user": 999,
         "global_max_active_optimizations": 10},
    ):
        with pytest.raises(PydanticValidationError):
            Settings(jwt_secret="x" * 40, **bad)


@pytest.mark.asyncio
async def test_a_failed_operation_returns_its_slot():
    """The guard releases in a `finally`, so a raising body does not hold a slot
    until expiry."""
    from app.services.admission.guard import admission_guard

    scope = str(uuid.uuid4())

    with pytest.raises(RuntimeError):
        async with admission_guard(OperationClass.SCENARIO_RUN, scope_id=scope):
            raise RuntimeError("the operation failed")

    async with unit_of_work(actor_type="admin") as session:
        assert await AdmissionService(session).active_count(
            OperationClass.SCENARIO_RUN, scope_id=scope) == 0


@pytest.mark.asyncio
async def test_the_incident_switch_actually_switches_admission_off():
    """`admission_enabled` is documented as the way to turn every limit off
    during an incident without a deploy. Before this test it was read by
    nothing: an operator would have flipped it, watched the limits keep
    refusing, and had no way to tell the switch from a bug.

    Asserted in both directions, because a kill switch that cannot be turned
    back on is worse than none.
    """
    from app.core.config import get_settings
    from app.services.admission.policy import POLICIES
    from app.services.admission.service import ADMISSION_BYPASSED

    settings = get_settings()
    scope = str(uuid.uuid4())
    allowance = POLICIES[OperationClass.ANALYSIS_RUN].rate_allowance
    assert allowance is not None

    # Exhaust the allowance so the NEXT admission must be refused.
    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        for _ in range(allowance):
            await service.release_ticket(
                await service.admit(OperationClass.ANALYSIS_RUN, scope_id=scope))

    async with unit_of_work(actor_type="admin") as session:
        with pytest.raises(AdmissionRejected):
            await AdmissionService(session).admit(
                OperationClass.ANALYSIS_RUN, scope_id=scope)

    settings.admission_enabled = False
    try:
        before = ADMISSION_BYPASSED[OperationClass.ANALYSIS_RUN.value]
        async with unit_of_work(actor_type="admin") as session:
            ticket = await AdmissionService(session).admit(
                OperationClass.ANALYSIS_RUN, scope_id=scope)
        assert not ticket.holds_lease, (
            "a bypassed admission must not hold a slot it never checked for"
        )
        assert ADMISSION_BYPASSED[OperationClass.ANALYSIS_RUN.value] == before + 1, (
            "the bypass was not countable"
        )

        # The credential surface honours it too, or an incident would leave
        # login throttled while everything else was open.
        async with unit_of_work(actor_type="admin") as session:
            decision = await AdmissionService(session).charge_preauth_attempt(
                OperationClass.AUTH_ATTEMPT,
                source_scope_id="bypass-source", subject_scope_id="bypass-subject")
        assert decision.accepted
    finally:
        settings.admission_enabled = True

    # And it comes back on.
    async with unit_of_work(actor_type="admin") as session:
        with pytest.raises(AdmissionRejected):
            await AdmissionService(session).admit(
                OperationClass.ANALYSIS_RUN, scope_id=scope)
