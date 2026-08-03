"""The final partition invariant — the acceptance condition for migration 0036.

Four properties, asserted INDEPENDENTLY for every partition that exists now and
for one created during the test to stand for every partition created later:

  1. ENABLE and FORCE row level security, on the partition itself
  2. a correct ownership policy — USING and WITH CHECK both resolving to the
     tenant GUC
  3. minimal grants — no role holds a privilege on a partition that it does not
     hold on the parent
  4. deny-by-default — with no tenant context, the partition returns nothing

The distinction that matters is "independently". A partition inheriting nothing
from its parent is exactly why the vulnerability existed, so every check here
addresses the partition directly rather than reasoning from the parent.
"""
import uuid
from decimal import Decimal

import psycopg2
import pytest
from sqlalchemy import select, text

from app.database.models import IncomeSource, IncomeType, UserAccount
from app.database.session import unit_of_work
from tests.conftest import owner_dsn

PARTITION_QUERY = """
    SELECT n.nspname AS sch,
           c.relname AS partition,
           pn.nspname || '.' || p.relname AS parent,
           c.relrowsecurity,
           c.relforcerowsecurity
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
      JOIN pg_inherits i ON i.inhrelid = c.oid
      JOIN pg_class p ON p.oid = i.inhparent
      JOIN pg_namespace pn ON pn.oid = p.relnamespace
     WHERE c.relkind = 'r'
       AND p.relrowsecurity          -- parent is RLS-protected
     ORDER BY 1, 2
"""


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


async def _partitions() -> list[dict]:
    async with unit_of_work(actor_type="system") as s:
        rows = await s.execute(text(PARTITION_QUERY))
        return [dict(r._mapping) for r in rows]


async def _user_with_income(amount: str) -> uuid.UUID:
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"inv_{uuid.uuid4().hex[:8]}@test.ca", status="active")
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


# ---------------------------------------------------------------------------
# 1. ENABLE + FORCE, independently per partition
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_every_partition_independently_enables_and_forces_rls():
    partitions = await _partitions()
    assert partitions, "no partitions of RLS-protected parents — query is wrong"

    failures = [
        f"{p['sch']}.{p['partition']} (of {p['parent']}): "
        f"enabled={p['relrowsecurity']} forced={p['relforcerowsecurity']}"
        for p in partitions
        if not (p["relrowsecurity"] and p["relforcerowsecurity"])
    ]
    assert not failures, (
        "partitions without their own ENABLE+FORCE RLS — readable around the "
        f"parent's policy: {failures}"
    )


# ---------------------------------------------------------------------------
# 2. Correct policies, independently per partition
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_every_partition_has_a_correct_ownership_policy():
    partitions = await _partitions()
    async with unit_of_work(actor_type="system") as s:
        rows = await s.execute(text("""
            SELECT schemaname, tablename, policyname, cmd, qual, with_check
              FROM pg_policies
        """))
        policies: dict[tuple[str, str], list] = {}
        for sch, tbl, name, cmd, qual, with_check in rows:
            policies.setdefault((sch, tbl), []).append((name, cmd, qual, with_check))

    for p in partitions:
        key = (p["sch"], p["partition"])
        entries = policies.get(key)
        assert entries, f"{p['sch']}.{p['partition']} has no policy at all"
        for name, cmd, qual, with_check in entries:
            assert cmd == "ALL", (
                f"{key} policy {name} covers only {cmd}; a read-only policy "
                "would leave writes unconstrained"
            )
            assert qual and "current_app_user" in qual, (
                f"{key} policy {name}: USING does not resolve to the tenant GUC"
            )
            assert with_check and "current_app_user" in with_check, (
                f"{key} policy {name}: WITH CHECK does not resolve to the tenant "
                "GUC, so a row could be written for another tenant"
            )


# ---------------------------------------------------------------------------
# 3. Minimal grants — a partition grants no more than its parent
# ---------------------------------------------------------------------------
def test_no_partition_grants_more_than_its_parent():
    """A partition holding a privilege its parent does not is a way in that the
    parent's own access review would never surface."""
    conn = psycopg2.connect(owner_dsn())
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT n.nspname || '.' || c.relname AS partition,
                   pn.nspname || '.' || p.relname AS parent,
                   r.rolname,
                   priv.privilege,
                   has_table_privilege(r.oid, c.oid, priv.privilege) AS on_partition,
                   has_table_privilege(r.oid, p.oid, priv.privilege) AS on_parent
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_inherits i ON i.inhrelid = c.oid
              JOIN pg_class p ON p.oid = i.inhparent
              JOIN pg_namespace pn ON pn.oid = p.relnamespace
             CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),
                                ('TRUNCATE'),('REFERENCES'),('TRIGGER')) AS priv(privilege)
             CROSS JOIN pg_roles r
             WHERE c.relkind = 'r'
               AND p.relrowsecurity
               AND r.rolname LIKE 'onyx%%'
        """)
        excess = [
            (part, parent, role, privilege)
            for part, parent, role, privilege, on_part, on_parent in cur.fetchall()
            if on_part and not on_parent
        ]
    finally:
        conn.close()

    assert not excess, (
        f"partitions grant privileges their parents do not: {excess}"
    )


def test_the_freshness_worker_holds_nothing_on_any_partition():
    """The restricted worker must not reach a partition either — the earlier
    invariant covers tables generally, this one names partitions explicitly."""
    conn = psycopg2.connect(owner_dsn())
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT n.nspname || '.' || c.relname
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
              JOIN pg_inherits i ON i.inhrelid = c.oid
              JOIN pg_class p ON p.oid = i.inhparent
             WHERE c.relkind = 'r' AND p.relrowsecurity
               AND (has_table_privilege('onyx_freshness_worker', c.oid, 'SELECT')
                 OR has_table_privilege('onyx_freshness_worker', c.oid, 'INSERT')
                 OR has_table_privilege('onyx_freshness_worker', c.oid, 'UPDATE')
                 OR has_table_privilege('onyx_freshness_worker', c.oid, 'DELETE'))
        """)
        reachable = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    assert not reachable, f"the freshness worker can reach partitions: {reachable}"


# ---------------------------------------------------------------------------
# 4. Deny-by-default, independently per partition
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_every_partition_denies_by_default_with_no_tenant_context():
    """Populated first, so the assertion can never pass vacuously."""
    await _user_with_income("55555.55")
    partitions = await _partitions()

    async with unit_of_work(actor_type="system") as s:
        # confirm there is something to hide
        total = await s.scalar(text(
            "SELECT count(*) FROM finance.income_source_y2025"
        ))

    assert total == 0, (
        "a system session with no tenant context could already read the "
        "partition — deny-by-default is not holding"
    )

    async with unit_of_work(actor_type="system") as s:
        await s.execute(text("SELECT set_config('app.user_id', '', true)"))
        for p in partitions:
            count = await s.scalar(
                text(f"SELECT count(*) FROM {p['sch']}.{p['partition']}")  # noqa: S608
            )
            assert count == 0, (
                f"{p['sch']}.{p['partition']} returned {count} rows with no "
                "tenant context set"
            )


@pytest.mark.asyncio
async def test_every_partition_is_owner_scoped_for_reads_and_writes():
    """The end-to-end statement: one tenant's row is invisible and unforgeable
    through every partition."""
    marker = "66666.66"
    owner = await _user_with_income(marker)
    other = await _user_with_income("77777.77")
    partitions = [p for p in await _partitions() if p["partition"].startswith("income_source")]
    assert partitions

    async with unit_of_work(user_id=owner, actor_type="user") as s:
        visible = await s.scalar(
            text("SELECT count(*) FROM finance.income_source WHERE amount = :a"),
            {"a": Decimal(marker)},
        )
    assert visible == 1, "the owner cannot see their own row"

    async with unit_of_work(user_id=other, actor_type="user") as s:
        for p in partitions:
            leaked = await s.scalar(
                text(  # noqa: S608
                    f"SELECT count(*) FROM {p['sch']}.{p['partition']} "
                    "WHERE amount = :a"
                ),
                {"a": Decimal(marker)},
            )
            assert leaked == 0, (
                f"cross-tenant read through {p['sch']}.{p['partition']}"
            )

        itype = await s.scalar(select(IncomeType).where(IncomeType.code == "employment"))
        with pytest.raises(Exception, match="row-level security|violates"):
            await s.execute(
                text(
                    "INSERT INTO finance.income_source_y2025 "
                    "(user_id, tax_year, income_type_id, amount, province_code) "
                    "VALUES (:u, 2025, :t, 1.00, 'ON')"
                ),
                {"u": owner, "t": itype.id},
            )
        await s.rollback()


# ---------------------------------------------------------------------------
# The same four properties for a partition created LATER
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_future_partition_satisfies_all_four_properties():
    """Stands for every partition created after this release.

    A yearly partition is added by hand and "remember to enable RLS" is exactly
    the step that gets missed once — which is how the original defect happened.
    """
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    created = "finance.income_source_y2042"
    try:
        cur = conn.cursor()
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {created} "
            "PARTITION OF finance.income_source FOR VALUES IN (2042)"
        )

        # 1. ENABLE + FORCE
        cur.execute("""
            SELECT c.relrowsecurity, c.relforcerowsecurity
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'finance' AND c.relname = 'income_source_y2042'
        """)
        enabled, forced = cur.fetchone()

        # 2. policy with USING and WITH CHECK
        cur.execute("""
            SELECT qual, with_check, cmd FROM pg_policies
             WHERE schemaname = 'finance' AND tablename = 'income_source_y2042'
        """)
        policies = cur.fetchall()

        # 3. grants no wider than the parent
        cur.execute("""
            SELECT r.rolname, priv.privilege
              FROM pg_roles r
             CROSS JOIN (VALUES ('SELECT'),('INSERT'),('UPDATE'),('DELETE')) priv(privilege)
             WHERE r.rolname LIKE 'onyx%%'
               AND has_table_privilege(r.oid, 'finance.income_source_y2042', priv.privilege)
               AND NOT has_table_privilege(r.oid, 'finance.income_source', priv.privilege)
        """)
        excess = cur.fetchall()
    finally:
        try:
            conn.cursor().execute(f"DROP TABLE IF EXISTS {created} CASCADE")
        finally:
            conn.close()

    assert enabled and forced, (
        "a partition created after this release was left without RLS"
    )
    assert policies, "a partition created after this release was left without a policy"
    for qual, with_check, cmd in policies:
        assert "current_app_user" in (qual or ""), "USING does not scope to the tenant"
        assert "current_app_user" in (with_check or ""), "WITH CHECK does not scope"
        assert cmd == "ALL"
    assert not excess, f"a new partition was granted more than its parent: {excess}"


@pytest.mark.asyncio
async def test_the_event_trigger_that_secures_new_partitions_is_installed():
    """If the trigger is ever dropped, future partitions silently regress."""
    async with unit_of_work(actor_type="system") as s:
        row = await s.execute(text("""
            SELECT evtname, evtenabled, p.proname, p.prosecdef, p.proconfig
              FROM pg_event_trigger e
              JOIN pg_proc p ON p.oid = e.evtfoid
             WHERE e.evtname = 'trg_secure_new_partitions'
        """))
        entry = row.first()

    assert entry is not None, "the partition-securing event trigger is missing"
    name, enabled, function, is_definer, config = entry
    assert enabled != "D", "the event trigger is disabled"
    assert function == "secure_new_partitions"
    assert is_definer, "the trigger function must run with owner rights"
    assert config and any(c.startswith("search_path=") for c in config), (
        "the trigger function does not pin search_path"
    )
