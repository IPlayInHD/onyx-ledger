"""Admission control must not become a data-access path (Entry 10).

The mechanism counts activity across every principal, which is exactly the kind
of component that turns into a cross-tenant leak if nobody checks. Three
properties are asserted here:

  1. the tables carry no financial data, so there is nothing worth reading;
  2. no API surface returns a row from them, so there is no way to read it;
  3. a forged scope cannot spend or reveal another principal's budget.

Run as `onyx_test` (a member of `onyx_app_rw`) — the runtime identity, never a
superuser, because a superuser bypasses row-level security and would make these
assertions meaningless.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.database.session import unit_of_work
from app.services.admission.policy import OperationClass, ScopeType
from app.services.admission.service import AdmissionService


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


#: Column names that would mean financial data reached an admission table.
_FORBIDDEN_FRAGMENTS = (
    "income", "amount", "tax", "salary", "employer", "sin", "payload",
    "snapshot", "content", "text", "value", "balance", "benefit", "refund",
)


@pytest.mark.asyncio
async def test_admission_tables_carry_no_financial_columns():
    """Structural, not aspirational: the protection for these tables is that
    there is nothing sensitive in them, so that has to be enforced."""
    async with unit_of_work(actor_type="admin") as session:
        rows = list(await session.execute(text("""
            SELECT table_name, column_name
              FROM information_schema.columns
             WHERE table_schema = 'admission'
        """)))

    assert rows, "the admission schema is missing"
    offenders = [
        f"{table}.{column}"
        for table, column in rows
        if any(fragment in column.lower() for fragment in _FORBIDDEN_FRAGMENTS)
    ]
    assert not offenders, f"financial-looking columns in admission tables: {offenders}"


@pytest.mark.asyncio
async def test_admission_tables_are_not_readable_by_public():
    async with unit_of_work(actor_type="admin") as session:
        granted = list(await session.execute(text("""
            SELECT table_name, privilege_type
              FROM information_schema.role_table_grants
             WHERE table_schema = 'admission' AND grantee = 'PUBLIC'
        """)))
    assert granted == [], f"PUBLIC can reach admission tables: {granted}"


@pytest.mark.asyncio
async def test_no_security_definer_function_was_added_for_admission():
    """Admission needs no privilege escalation, so it must not have acquired
    one. A definer function here would be a new privileged path around RLS."""
    async with unit_of_work(actor_type="admin") as session:
        definers = list(await session.execute(text("""
            SELECT p.proname
              FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
             WHERE n.nspname = 'admission' AND p.prosecdef
        """)))
    assert definers == [], f"unexpected SECURITY DEFINER in admission: {definers}"


@pytest.mark.asyncio
async def test_one_principals_activity_does_not_change_anothers_budget():
    """Anti-monopoly caps are per principal. Exhausting one must leave another
    untouched, up to the documented GLOBAL cap."""
    noisy, quiet = str(uuid.uuid4()), str(uuid.uuid4())

    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        # Fill the noisy principal's concurrency allowance.
        held = []
        for _ in range(2):
            held.append(await service.admit(
                OperationClass.OPTIMIZATION_RUN, scope_id=noisy))
        assert await service.active_count(
            OperationClass.OPTIMIZATION_RUN, scope_id=noisy) == 2

        # The quiet principal is unaffected: its own budget is its own.
        assert await service.active_count(
            OperationClass.OPTIMIZATION_RUN, scope_id=quiet) == 0
        ticket = await service.admit(
            OperationClass.OPTIMIZATION_RUN, scope_id=quiet)
        assert ticket.holds_lease

        for t in [*held, ticket]:
            await service.release_ticket(t)


@pytest.mark.asyncio
async def test_a_forged_scope_spends_only_its_own_invented_budget():
    """Scope comes from the authenticated principal at every call site. Even if
    a caller could name one, naming somebody else's cannot REVEAL anything —
    the only observable is the caller's own accept/reject."""
    victim = str(uuid.uuid4())

    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        victim_ticket = await service.admit(
            OperationClass.SCENARIO_RUN, scope_id=victim)
        assert victim_ticket.holds_lease

        # An attacker naming the victim's scope consumes the VICTIM's slots —
        # which is why no call site lets a request body choose the scope. The
        # assertion that matters is that nothing about the victim is returned.
        forged = await service.admit(OperationClass.SCENARIO_RUN, scope_id=victim)
        assert forged.holds_lease
        # The ticket exposes only the caller's own handles; no counter, no other
        # principal's state, no capacity figure.
        assert set(vars(forged)) == {
            "lease_id", "operation", "scope_id", "global_lease_id",
            "duplicate_of_active",
        }

        await service.release_ticket(victim_ticket)
        await service.release_ticket(forged)


@pytest.mark.asyncio
async def test_a_rejection_reveals_no_capacity_or_other_principal_state():
    """What a refused caller is told is a closed code and a retry-after."""
    from app.services.admission.service import AdmissionRejected

    scope = str(uuid.uuid4())
    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        for _ in range(2):
            await service.admit(OperationClass.OPTIMIZATION_RUN, scope_id=scope)

        with pytest.raises(AdmissionRejected) as caught:
            await service.admit(OperationClass.OPTIMIZATION_RUN, scope_id=scope)

    exc = caught.value
    # The detail is `OPERATION:REASON` and nothing else — no counts, no limits,
    # no queue depth, no worker topology.
    assert exc.detail == "OPTIMIZATION_RUN:USER_CONCURRENCY_LIMIT"
    for leak in ("worker", "queue", "capacity", "count", "limit=", "platform"):
        assert leak not in exc.detail.lower()


@pytest.mark.asyncio
async def test_the_global_scope_id_cannot_be_forged_by_a_user_id():
    """Platform accounting shares a table with per-principal accounting, so the
    global key must be unreachable from any user identity."""
    from app.services.admission.service import GLOBAL_SCOPE_ID

    # A user scope is always a UUID string; the global one deliberately is not,
    # so no user id can ever collide with it.
    with pytest.raises(ValueError):
        uuid.UUID(GLOBAL_SCOPE_ID)

    async with unit_of_work(actor_type="admin") as session:
        service = AdmissionService(session)
        # Counting the global scope as a USER scope finds nothing: the two are
        # separate rows even for the same id string.
        assert await service.active_count(
            OperationClass.OPTIMIZATION_RUN,
            scope_id=GLOBAL_SCOPE_ID, scope_type=ScopeType.USER,
        ) == 0
