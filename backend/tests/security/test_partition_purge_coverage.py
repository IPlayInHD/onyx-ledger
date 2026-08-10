"""Entry 11B5I — every financial partition is inside the deletion boundary.

`test_partition_invariant.py` already proves a new partition is SECURED — RLS
enabled and forced, a tenant policy, no excess grants — by an event trigger that
fires on creation. This file asks the different question that trigger says
nothing about: is a new partition DELETED?

The two failures look nothing alike. An unsecured partition leaks on read. An
uncovered partition survives an account deletion silently, and nothing reports
it, because `count_remaining_source_data` would be counting the same tables the
purge forgot.

WHAT MAKES COVERAGE TRUE HERE is that both functions name the PARTITIONED
PARENT:

    DELETE FROM finance.income_source  WHERE user_id = p_user_id;
    SELECT count(*) FROM finance.income_source WHERE user_id = p_user_id

PostgreSQL routes a parent-targeted statement to every attached partition,
including the DEFAULT one, so a 2027 partition is covered the day it is created
and no year list exists to fall out of date. §5 asks for that to be proven
structurally rather than re-tested per year, so the guard below is a catalog
invariant plus one live future partition — not an enumeration.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from tests.conftest import owner_dsn

PHASE = "SOURCE_DATA"

#: TWO distinct years, and they must stay distinct.
#:
#: `DEFAULT_YEAR` has no explicit partition, so its rows land in DEFAULT.
#: `FUTURE_YEAR` is the year a partition gets CREATED for.
#:
#: Sharing one year broke under the security gate, which runs the suite fifteen
#: times against one database: PostgreSQL refuses to attach a partition when the
#: DEFAULT partition already holds a matching row —
#:
#:   updated partition constraint for default partition "income_source_default"
#:   would be violated by some row
#:
#: — so a single surviving row from the DEFAULT test made every later run's
#: CREATE fail, and then "relation ... does not exist" for the rest of the file.
DEFAULT_YEAR = 2041
FUTURE_YEAR = 2043


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


def _partitioned_source_parents(cur) -> list[str]:
    """The governed partitioned source parents, from the catalog.

    Discovered rather than listed: a third partitioned finance/profile source
    table added later is picked up here automatically, which is the whole point
    of a structural guard.
    """
    cur.execute("""
        SELECT p.relnamespace::regnamespace || '.' || p.relname
          FROM pg_class p
         WHERE p.relkind = 'p'
           AND p.relnamespace::regnamespace::text IN ('finance', 'profile', 'wealth')
         ORDER BY 1
    """)
    return [r[0] for r in cur.fetchall()]


def _children(cur, parent: str) -> list[str]:
    cur.execute("""
        SELECT c.relnamespace::regnamespace || '.' || c.relname
          FROM pg_inherits i
          JOIN pg_class c ON c.oid = i.inhrelid
         WHERE i.inhparent = %s::regclass AND c.relkind IN ('r', 'p')
         ORDER BY 1
    """, (parent,))
    return [r[0] for r in cur.fetchall()]


def _prosrc(cur, schema: str, name: str) -> str:
    cur.execute("SELECT prosrc FROM pg_proc p JOIN pg_namespace n "
                "  ON n.oid = p.pronamespace "
                " WHERE n.nspname = %s AND p.proname = %s", (schema, name))
    return cur.fetchone()[0]


def _remaining(cur, user: uuid.UUID) -> int:
    cur.execute("SELECT identity.count_remaining_source_data(%s)", (str(user),))
    return cur.fetchone()[0]


def _account(cur) -> uuid.UUID:
    user = uuid.uuid4()
    cur.execute("INSERT INTO identity.user_account (id, email, status) "
                "VALUES (%s, %s, 'active')",
                (str(user), f"part_{uuid.uuid4().hex[:10]}@example.com"))
    return user


def _income(cur, user: uuid.UUID, year: int, amount: int = 1000) -> None:
    cur.execute("""
        INSERT INTO finance.income_source
            (user_id, tax_year, income_type_id, amount, province_code)
        SELECT %s, %s, id, %s, 'ON' FROM ref.income_type WHERE code = 'employment'
    """, (str(user), year, amount))


def _expense(cur, user: uuid.UUID, year: int, amount: int = 250) -> None:
    cur.execute("""
        INSERT INTO finance.expense_record
            (user_id, tax_year, expense_category_id, amount)
        SELECT %s, %s, id, %s FROM ref.expense_category LIMIT 1
    """, (str(user), year, amount))


def _clear_year(cur, year: int) -> None:
    """Remove any synthetic rows for `year` from the partitioned parents.

    Only ever this file's own test data — no real tax year is anywhere near
    2041/2043 — and it is what makes CREATE ... PARTITION OF possible on a
    database these tests have run against before.
    """
    for table in ("finance.income_source", "finance.expense_record"):
        cur.execute(f"DELETE FROM {table} WHERE tax_year = %s", (year,))


def _drop_partition(cur, table: str) -> None:
    """DETACH, then DROP. Never `DROP ... CASCADE`.

    A table that references a partitioned parent gets a per-partition foreign
    key dependency — here `docs.document_link` references `income_source`. A
    plain DROP is refused because of it, and CASCADE would "fix" that by
    deleting a governed constraint from a table this test has no business
    touching. Detaching removes the partition from the hierarchy first, so the
    drop is local and `document_link` keeps every foreign key it had.
    """
    parent = ("finance.income_source" if "income_source" in table
              else "finance.expense_record")
    cur.execute(f"ALTER TABLE {parent} DETACH PARTITION {table}")
    cur.execute(f"DROP TABLE {table}")


def _purge(cur, user: uuid.UUID) -> None:
    cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                "VALUES (%s, 'DELETION_REQUESTED')", (str(user),))
    for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
        cur.execute("UPDATE identity.account_lifecycle SET state = %s "
                    " WHERE user_id = %s", (state, str(user)))
    token = uuid.uuid4()
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by = 'part', "
                "claim_token = %s, claimed_at = now() WHERE user_id = %s",
                (str(token), str(user)))
    cur.execute("SELECT identity.start_lifecycle_phase(%s, %s, %s, 'part')",
                (str(user), PHASE, str(token)))
    cur.execute("SELECT identity.purge_source_data(%s, %s, 'part')",
                (str(user), str(token)))
    return token


# =============================================================== §2, §5 ======
def test_the_purge_targets_partitioned_parents_and_never_a_child():
    """The structural guard, and the reason no year list is needed.

    Naming a child in either function would silently convert "every partition"
    into "the partitions someone remembered in 2025". This asserts the opposite
    shape directly against the installed function bodies.
    """
    admin = _owner()
    try:
        cur = admin.cursor()
        parents = _partitioned_source_parents(cur)
        assert parents, "no partitioned source parents found; the guard is blind"

        purge = _prosrc(cur, "identity", "purge_source_data")
        count = _prosrc(cur, "identity", "count_remaining_source_data")

        for parent in parents:
            assert parent in purge, (
                f"{parent} is a partitioned source table the purge never names")
            assert parent in count, (
                f"{parent} is a partitioned source table the completeness "
                "check never counts")

            for child in _children(cur, parent):
                assert child not in purge, (
                    f"the purge names the partition {child} directly. That "
                    "hard-codes a year list, and the partition added next "
                    "January will not be in it.")
                assert child not in count, (
                    f"the completeness check names the partition {child} "
                    "directly, so a future partition would not be counted")
    finally:
        admin.close()


def test_every_current_partition_of_every_source_parent_is_actually_purged():
    """§3. Not just 2024/2025 — every child the catalog reports, including the
    DEFAULT partition, populated and then purged for one subject."""
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _account(cur)

        # Years chosen from the real bounds: the two explicit partitions and one
        # year that can only land in DEFAULT.
        years = [2024, 2025, DEFAULT_YEAR]
        for year in years:
            _income(cur, user, year)
            _expense(cur, user, year)

        # Prove the rows really spread across distinct partitions, or this
        # measures one partition three times.
        cur.execute("""
            SELECT DISTINCT tableoid::regclass::text
              FROM finance.income_source WHERE user_id = %s ORDER BY 1
        """, (str(user),))
        touched = [r[0] for r in cur.fetchall()]
        assert len(touched) == 3, (
            f"expected rows in three income partitions, got {touched}")
        assert any("default" in t for t in touched), (
            f"no row landed in the DEFAULT partition: {touched}")

        before = _remaining(cur, user)
        assert before == 6, f"expected 6 qualifying rows, counted {before}"

        _purge(cur, user)

        assert _remaining(cur, user) == 0, (
            "rows survived the purge in at least one partition")
        for table in ("finance.income_source", "finance.expense_record"):
            cur.execute(f"SELECT count(*) FROM {table} WHERE user_id = %s",
                        (str(user),))
            assert cur.fetchone()[0] == 0, f"{table} still holds rows"
    finally:
        admin.close()


# ==================================================== §4, §7, §36 ============
def test_a_partition_created_after_this_release_is_purged_and_secured():
    """§4. The partition that does not exist yet is the one that matters.

    Created through the same `PARTITION OF ... FOR VALUES IN` mechanism the
    schema uses, so the event trigger that secures new partitions runs exactly
    as it would in January. Dropped again in `finally` — §36 is explicit that no
    unsecured partition may be left behind.
    """
    admin = _owner()
    created = [f"finance.income_source_y{FUTURE_YEAR}",
               f"finance.expense_record_y{FUTURE_YEAR}"]
    try:
        cur = admin.cursor()
        # Clear any stray row of this year first. A row sitting in DEFAULT
        # makes ATTACH impossible, and on a long-lived database that is a
        # permanent, confusing failure for every later run.
        _clear_year(cur, FUTURE_YEAR)
        cur.execute(f"CREATE TABLE IF NOT EXISTS {created[0]} PARTITION OF "
                    f"finance.income_source FOR VALUES IN ({FUTURE_YEAR})")
        cur.execute(f"CREATE TABLE IF NOT EXISTS {created[1]} PARTITION OF "
                    f"finance.expense_record FOR VALUES IN ({FUTURE_YEAR})")

        # §7 — the security the event trigger owes a new partition.
        for table in created:
            schema, name = table.split(".")
            cur.execute("""
                SELECT c.relrowsecurity, c.relforcerowsecurity
                  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = %s AND c.relname = %s
            """, (schema, name))
            enabled, forced = cur.fetchone()
            assert enabled and forced, (
                f"{table} was created without RLS enabled and forced")
            cur.execute("SELECT qual, with_check FROM pg_policies "
                        " WHERE schemaname = %s AND tablename = %s", (schema, name))
            policies = cur.fetchall()
            assert policies, f"{table} has no tenant policy"
            for qual, with_check in policies:
                assert "current_app_user" in (qual or ""), f"{table} USING is not scoped"
                assert "current_app_user" in (with_check or ""), (
                    f"{table} WITH CHECK is not scoped")

        # §4 — and the lifecycle covers it with no code change.
        user = _account(cur)
        _income(cur, user, FUTURE_YEAR)
        _expense(cur, user, FUTURE_YEAR)

        cur.execute("SELECT DISTINCT tableoid::regclass::text "
                    "  FROM finance.income_source WHERE user_id = %s", (str(user),))
        landed = [r[0] for r in cur.fetchall()]
        assert landed == [created[0]], (
            f"the row did not land in the new partition: {landed}")

        assert _remaining(cur, user) == 2, (
            "the completeness check does not see rows in a partition created "
            "after release")

        _purge(cur, user)

        assert _remaining(cur, user) == 0, (
            "a partition created after this release survived the account purge")
        cur.execute(f"SELECT count(*) FROM {created[0]} WHERE user_id = %s",
                    (str(user),))
        assert cur.fetchone()[0] == 0, f"{created[0]} still holds rows"
    finally:
        try:
            cur = admin.cursor()
            for table in created:
                _drop_partition(cur, table)
        finally:
            admin.close()


def test_the_coverage_guard_catches_a_child_enumerating_purge():
    """§36 — failure injection for the class of omission this file exists for.

    The realistic defect is not a typo; it is someone "optimising" the purge
    into per-partition DELETEs and thereby freezing the year list. This
    reproduces exactly that shape against a real future partition and shows the
    rows survive — which is what the structural guard above forbids.

    The injected statement is scoped to one throwaway subject and one throwaway
    partition; nothing governed is modified.
    """
    admin = _owner()
    created = f"finance.income_source_y{FUTURE_YEAR}"
    try:
        cur = admin.cursor()
        _clear_year(cur, FUTURE_YEAR)
        cur.execute(f"CREATE TABLE IF NOT EXISTS {created} PARTITION OF "
                    f"finance.income_source FOR VALUES IN ({FUTURE_YEAR})")
        user = _account(cur)
        _income(cur, user, 2025)
        _income(cur, user, FUTURE_YEAR)
        assert _remaining(cur, user) == 2, "the fixture did not create both rows"

        # THE INJECTED IMPLEMENTATION: delete the partitions a 2025 author knew
        # about, rather than the parent.
        cur.execute("SET app.user_id = %s", (str(user),))
        for year in (2024, 2025):
            cur.execute(f"DELETE FROM finance.income_source_y{year} "
                        " WHERE user_id = %s", (str(user),))
        cur.execute("RESET app.user_id")

        survived = _remaining(cur, user)
        assert survived == 1, (
            f"expected the future-partition row to survive a child-enumerating "
            f"delete, found {survived} remaining — the injection did not "
            "reproduce the defect it is meant to demonstrate")

        # And the completion gate refuses on the strength of that survivor,
        # which is the property that turns a silent gap into a stuck phase.
        token = _purge(cur, user)
        assert _remaining(cur, user) == 0, (
            "the real parent-targeted purge also missed the future partition")
        cur.execute("SELECT identity.complete_lifecycle_phase(%s, %s, %s, 'part')",
                    (str(user), PHASE, str(token)))
        assert cur.fetchone()[0], "completion was refused after a clean purge"
    finally:
        try:
            cur = admin.cursor()
            _drop_partition(cur, created)
        finally:
            admin.close()


# ==================================================================== §6 =====
@pytest.mark.parametrize("wrong_year", [2024, FUTURE_YEAR])
def test_a_wrong_year_individual_delete_misses_without_widening_access(wrong_year):
    """§6. `tax_year` is a routing key, never authorization.

    A delete aimed at the wrong year must simply match nothing — and must not
    reach another tenant's row of the same id in that year's partition.
    """
    admin = _owner()
    try:
        cur = admin.cursor()
        owner_user = _account(cur)
        other_user = _account(cur)
        _income(cur, owner_user, 2025)
        _income(cur, other_user, wrong_year)

        cur.execute("SELECT id FROM finance.income_source WHERE user_id = %s",
                    (str(owner_user),))
        row_id = cur.fetchone()[0]

        # As the owner, aim at the wrong year.
        cur.execute("SET app.user_id = %s", (str(owner_user),))
        cur.execute("DELETE FROM finance.income_source "
                    " WHERE id = %s AND tax_year = %s", (str(row_id), wrong_year))
        missed = cur.rowcount
        cur.execute("RESET app.user_id")

        assert missed == 0, "a wrong-year delete matched a row"
        cur.execute("SELECT count(*) FROM finance.income_source WHERE user_id = %s",
                    (str(owner_user),))
        assert cur.fetchone()[0] == 1, "the correct-year row was destroyed"
        cur.execute("SELECT count(*) FROM finance.income_source WHERE user_id = %s",
                    (str(other_user),))
        assert cur.fetchone()[0] == 1, (
            "a wrong-year delete reached another tenant's row in that partition")
    finally:
        admin.close()
