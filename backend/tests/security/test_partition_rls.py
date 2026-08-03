"""Partitions must not be a way around row-level security.

RLS in PostgreSQL is not inherited by partitions. A policy on a partitioned
parent is applied when the query names the parent and skipped entirely when the
query names a partition directly. The runtime role holds SELECT on the
partitions, so before migration 0036 this was a live cross-tenant read of every
user's income and expense rows.

`test_a_partition_cannot_be_read_directly_across_tenants` is the regression
test: it fails against the schema as it stood before that migration.
"""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.database.models import IncomeSource, IncomeType, UserAccount
from app.database.session import unit_of_work


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


async def _user_with_income(amount: str) -> uuid.UUID:
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"part_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(u)
        await s.flush()
        uid = u.id
        itype = await s.scalar(select(IncomeType).where(IncomeType.code == "employment"))
        type_id = itype.id
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(IncomeSource(
            user_id=uid, tax_year=2025, income_type_id=type_id,
            amount=Decimal(amount), province_code="ON",
        ))
        await s.flush()
    return uid


@pytest.mark.asyncio
async def test_every_partition_of_an_rls_protected_parent_has_its_own_rls():
    async with unit_of_work(actor_type="system") as s:
        rows = await s.execute(text("""
            SELECT n.nspname || '.' || c.relname,
                   c.relrowsecurity, c.relforcerowsecurity
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_inherits i ON i.inhrelid = c.oid
              JOIN pg_class p ON p.oid = i.inhparent
             WHERE c.relkind = 'r' AND p.relrowsecurity
             ORDER BY 1
        """))
        partitions = list(rows)

    assert partitions, "no partitions of RLS-protected parents found"
    unprotected = [
        name for name, enabled, forced in partitions if not (enabled and forced)
    ]
    assert not unprotected, (
        f"partitions readable around their parent's RLS: {unprotected}"
    )


@pytest.mark.asyncio
async def test_every_such_partition_carries_a_self_ownership_policy():
    async with unit_of_work(actor_type="system") as s:
        rows = await s.execute(text("""
            SELECT n.nspname || '.' || c.relname, pol.qual, pol.with_check
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_inherits i ON i.inhrelid = c.oid
              JOIN pg_class p ON p.oid = i.inhparent
              LEFT JOIN pg_policies pol
                     ON pol.schemaname = n.nspname AND pol.tablename = c.relname
             WHERE c.relkind = 'r' AND p.relrowsecurity
        """))
        entries = list(rows)

    for name, qual, with_check in entries:
        assert qual and "current_app_user" in qual, f"{name} has no ownership USING"
        assert with_check and "current_app_user" in with_check, (
            f"{name} has no ownership WITH CHECK"
        )


@pytest.mark.asyncio
async def test_a_partition_cannot_be_read_directly_across_tenants():
    """The regression test for the finding. Fails without migration 0036."""
    marker = "874321.19"
    owner = await _user_with_income(marker)
    other = await _user_with_income("11111.11")

    async with unit_of_work(user_id=owner, actor_type="user") as s:
        via_parent = await s.scalar(
            text("SELECT count(*) FROM finance.income_source WHERE amount = :a"),
            {"a": Decimal(marker)},
        )
        via_partition = await s.scalar(
            text("SELECT count(*) FROM finance.income_source_y2025 WHERE amount = :a"),
            {"a": Decimal(marker)},
        )
    assert via_parent == 1 and via_partition == 1, "the owner cannot see their own row"

    # a DIFFERENT tenant must see it through neither path
    async with unit_of_work(user_id=other, actor_type="user") as s:
        parent_leak = await s.scalar(
            text("SELECT count(*) FROM finance.income_source WHERE amount = :a"),
            {"a": Decimal(marker)},
        )
        partition_leak = await s.scalar(
            text("SELECT count(*) FROM finance.income_source_y2025 WHERE amount = :a"),
            {"a": Decimal(marker)},
        )
    assert parent_leak == 0, "cross-tenant read through the partitioned parent"
    assert partition_leak == 0, (
        "cross-tenant read by naming the partition directly — RLS is not "
        "inherited by partitions"
    )


@pytest.mark.asyncio
async def test_a_partition_denies_by_default_with_no_tenant_context():
    await _user_with_income("22222.22")
    async with unit_of_work(actor_type="system") as s:
        await s.execute(text("SELECT set_config('app.user_id', '', true)"))
        for table in ("finance.income_source", "finance.income_source_y2025",
                      "finance.income_source_y2024", "finance.income_source_default"):
            count = await s.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
            assert count == 0, f"{table} returned rows with no tenant context"


@pytest.mark.asyncio
async def test_a_partition_created_later_is_secured_automatically():
    """A yearly partition is added by hand, and "remember to enable RLS" is
    exactly the step that gets missed once."""
    import psycopg2

    from tests.conftest import owner_dsn

    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS finance.income_source_y2031 "
            "PARTITION OF finance.income_source FOR VALUES IN (2031)"
        )
        cur.execute("""
            SELECT c.relrowsecurity, c.relforcerowsecurity
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'finance' AND c.relname = 'income_source_y2031'
        """)
        enabled, forced = cur.fetchone()
        cur.execute("""
            SELECT count(*) FROM pg_policies
             WHERE schemaname = 'finance' AND tablename = 'income_source_y2031'
        """)
        policies = cur.fetchone()[0]
    finally:
        try:
            conn.cursor().execute(
                "DROP TABLE IF EXISTS finance.income_source_y2031 CASCADE"
            )
        finally:
            conn.close()

    assert enabled and forced, "a newly created partition was left without RLS"
    assert policies >= 1, "a newly created partition was left without a policy"


@pytest.mark.asyncio
async def test_writes_through_a_partition_cannot_forge_another_tenants_row():
    """WITH CHECK must hold on the partition too, or a direct INSERT could
    attribute a row to someone else."""
    owner = await _user_with_income("33333.33")
    victim = await _user_with_income("44444.44")

    async with unit_of_work(user_id=owner, actor_type="user") as s:
        itype = await s.scalar(select(IncomeType).where(IncomeType.code == "employment"))
        with pytest.raises(Exception, match="row-level security|violates"):
            await s.execute(
                text(
                    "INSERT INTO finance.income_source_y2025 "
                    "(user_id, tax_year, income_type_id, amount, province_code) "
                    "VALUES (:u, 2025, :t, 1.00, 'ON')"
                ),
                {"u": victim, "t": itype.id},
            )
        await s.rollback()

