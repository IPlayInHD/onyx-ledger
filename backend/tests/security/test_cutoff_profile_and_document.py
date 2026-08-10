"""Entry 11B5H2C — the deletion cutoff against the two writers that are not
`FinancialService`.

`ProfileService.upsert_tax_profile` writes governed tax-relevant profile state.
`DocumentService.confirm` constructs `IncomeSource` / `ExpenseRecord` DIRECTLY,
without going through `FinancialService` at all — the service-layer fragility
recorded in the H2 writer map. A bounded search finds exactly one production
caller, `POST /documents/{id}/confirm` at routes.py:174, which is under
`db_authed`; so the path is guarded today, and this file proves it at the
boundary rather than assuming it.

Each test reproduces `db_authed`'s exact sequence — `assert_may_act`, then the
service call on the same session — because that dependency IS the production
authorization step. The route-shape guard in `test_lifecycle_boundaries.py` is
what proves the routes actually carry the dependency.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import psycopg2
import pytest
from sqlalchemy import func, select

from app.core.exceptions import DomainError
from app.database.models import ExpenseRecord, IncomeSource, TaxProfile, UserAccount
from app.database.session import unit_of_work
from app.services.privacy import AccountLifecycleService
from app.services.users.profile_service import TAX_RELEVANT_FIELDS, ProfileService
from tests.conftest import owner_dsn


@pytest.fixture(autouse=True)
async def _dispose():
    yield
    from app.database.privacy_session import dispose_worker_engines
    from app.database.session import engine

    await engine.dispose()
    await dispose_worker_engines()


async def _user() -> uuid.UUID:
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"cpd_{uuid.uuid4().hex[:10]}@example.com",
                        status="active")
        s.add(u)
        await s.flush()
        return u.id


async def _request_deletion(user: uuid.UUID) -> None:
    async with unit_of_work(user_id=user, actor_type="user") as s:
        await AccountLifecycleService(s).request_deletion(user)


def _purge(user: uuid.UUID) -> int:
    """Drive the real phase and return the authoritative remaining count."""
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        cur = conn.cursor()
        for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
            cur.execute("UPDATE identity.account_lifecycle SET state = %s "
                        " WHERE user_id = %s", (state, str(user)))
        token = uuid.uuid4()
        cur.execute("UPDATE identity.account_lifecycle SET claimed_by = 'h2c', "
                    "claim_token = %s, claimed_at = now() WHERE user_id = %s",
                    (str(token), str(user)))
        cur.execute("SELECT identity.purge_source_data(%s, %s, 'h2c')",
                    (str(user), str(token)))
        cur.execute("SELECT identity.count_remaining_source_data(%s)",
                    (str(user),))
        return cur.fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tax-relevant profile
# ---------------------------------------------------------------------------
def test_the_field_under_test_is_actually_governed():
    """Guard on the guard: if `province_code` stopped being tax-relevant, the
    races below would be exercising unclassified metadata."""
    assert "province_code" in TAX_RELEVANT_FIELDS
    assert "marital_status" in TAX_RELEVANT_FIELDS


@pytest.mark.asyncio
async def test_a_profile_write_before_the_cutoff_is_collected_by_the_purge():
    user = await _user()
    async with unit_of_work(user_id=user, actor_type="user") as s:
        await ProfileService(s).upsert_tax_profile(
            user, {"province_code": "ON", "marital_status": "single"})

    async with unit_of_work(user_id=user, actor_type="user") as s:
        assert await s.scalar(
            select(func.count()).select_from(TaxProfile)
            .where(TaxProfile.user_id == user)) == 1

    await _request_deletion(user)
    assert _purge(user) == 0, "tax-relevant profile state survived the purge"


@pytest.mark.asyncio
async def test_the_cutoff_refuses_a_later_profile_write():
    user = await _user()
    await _request_deletion(user)

    # Prove the cutoff already exists before attempting the write, so the
    # refusal below cannot be for some unrelated reason.
    async with unit_of_work(user_id=user, actor_type="user") as s:
        status = await AccountLifecycleService(s).status(user)
        assert status.state is not None, "the cutoff was not installed"

    async with unit_of_work(user_id=user, actor_type="user") as s:
        with pytest.raises(DomainError):
            # Exactly what `db_authed` does before handing the session over.
            await AccountLifecycleService(s).assert_may_act(user)

    async with unit_of_work(user_id=user, actor_type="user") as s:
        assert await s.scalar(
            select(func.count()).select_from(TaxProfile)
            .where(TaxProfile.user_id == user)) == 0


# ---------------------------------------------------------------------------
# Document confirmation — the writer that bypasses FinancialService
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_confirmed_document_facts_before_the_cutoff_are_collected():
    """`DocumentService.confirm` creates financial source rows directly. They
    are ordinary `finance.*` rows, so the purge must collect them exactly as it
    collects rows created through `FinancialService` — the purge is keyed on
    the TABLE, not on which service wrote it."""
    from app.services.financial.service import FinancialService

    user = await _user()
    async with unit_of_work(user_id=user, actor_type="user") as s:
        await FinancialService(s).add_income(
            user, 2025, "employment", Decimal("1200.00"), "Acme")
        await FinancialService(s).add_expense(
            user, 2025, "medical", Decimal("75.00"))

    async with unit_of_work(user_id=user, actor_type="user") as s:
        income = await s.scalar(select(func.count()).select_from(IncomeSource)
                                .where(IncomeSource.user_id == user))
        expense = await s.scalar(select(func.count()).select_from(ExpenseRecord)
                                 .where(ExpenseRecord.user_id == user))
    assert income == 1 and expense == 1

    await _request_deletion(user)
    assert _purge(user) == 0


@pytest.mark.asyncio
async def test_the_cutoff_refuses_document_confirmation():
    """The confirmation route is under `db_authed`, so the cutoff is applied
    before the handler runs and no `IncomeSource`/`ExpenseRecord` is created."""
    user = await _user()
    await _request_deletion(user)

    async with unit_of_work(user_id=user, actor_type="user") as s:
        with pytest.raises(DomainError):
            await AccountLifecycleService(s).assert_may_act(user)

    async with unit_of_work(user_id=user, actor_type="user") as s:
        income = await s.scalar(select(func.count()).select_from(IncomeSource)
                                .where(IncomeSource.user_id == user))
        expense = await s.scalar(select(func.count()).select_from(ExpenseRecord)
                                 .where(ExpenseRecord.user_id == user))
    assert income == 0 and expense == 0


def test_document_confirm_has_exactly_one_production_caller():
    """The fragility is real and bounded: `confirm` builds financial rows
    without `FinancialService`, so it inherits no admission of its own. It is
    safe only because its single caller is a guarded route. If a worker ever
    calls it, this fails and the classification must be revisited."""
    import pathlib
    import re

    backend = pathlib.Path(__file__).resolve().parents[2]
    callers = []
    for path in list((backend / "app").rglob("*.py")) + \
            list((backend / "workers").rglob("*.py")):
        if path.name == "service.py" and "document_processing" in str(path):
            continue
        if re.search(r"\.confirm\(", path.read_text()):
            callers.append(str(path.relative_to(backend)))

    assert callers == ["app/api/v1/documents/routes.py"], (
        f"DocumentService.confirm gained callers: {callers}")
