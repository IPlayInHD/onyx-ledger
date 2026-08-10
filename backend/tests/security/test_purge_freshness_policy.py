"""Entry 11B5H2C — what does an account-wide SOURCE_DATA purge emit?

Established from the implementation first, then measured: neither
`identity.purge_source_data`, nor `SourceDataPurgeService`, nor
`workers/tasks/privacy.py` emits a freshness event. The account purge emits
ZERO, and that is coherent rather than an oversight — freshness answers "is
this user's CURRENT advice still valid", and an account past the deletion
cutoff has no current advice to keep valid. Emitting per deleted row would be
worse than useless: it would be O(rows) queue traffic describing analyses
nobody may look at again, for a user whose data is being removed.

Individual deletion is the opposite case and DOES emit — the user is still
active and their remaining figures change meaning. That asymmetry is the
policy, and this file pins it so a later change has to be deliberate.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from tests.conftest import owner_dsn


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


def _account_with_rows(cur, rows: int) -> uuid.UUID:
    user = uuid.uuid4()
    cur.execute("INSERT INTO identity.user_account (id, email, status) "
                "VALUES (%s, %s, 'active')",
                (str(user), f"pol_{uuid.uuid4().hex[:10]}@example.com"))
    cur.execute("INSERT INTO profile.tax_profile (user_id, province_code, "
                "marital_status) VALUES (%s, 'ON', 'single')", (str(user),))
    cur.execute("""
        INSERT INTO finance.income_source
            (user_id, tax_year, income_type_id, amount, province_code)
        SELECT %s, 2025,
               (SELECT id FROM ref.income_type WHERE code = 'employment'),
               g, 'ON'
          FROM generate_series(1, %s) g
    """, (str(user), rows))
    return user


def _purge(cur, user: uuid.UUID) -> None:
    cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                "VALUES (%s, 'DELETION_REQUESTED')", (str(user),))
    for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
        cur.execute("UPDATE identity.account_lifecycle SET state = %s "
                    " WHERE user_id = %s", (state, str(user)))
    token = uuid.uuid4()
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by = 'pol', "
                "claim_token = %s, claimed_at = now() WHERE user_id = %s",
                (str(token), str(user)))
    cur.execute("SELECT identity.purge_source_data(%s, %s, 'pol')",
                (str(user), str(token)))


def _outbox_count(cur, user: uuid.UUID) -> int:
    cur.execute("SELECT count(*) FROM ioe.freshness_outbox WHERE user_id = %s",
                (str(user),))
    return cur.fetchone()[0]


@pytest.mark.parametrize("rows", [3, 40])
def test_the_account_purge_emits_no_freshness_regardless_of_row_count(rows):
    """The count is the point. If the purge emitted per row, 3 and 40 would
    differ; if it emitted one lifecycle signal, both would be 1. Both are 0."""
    admin = _owner()
    try:
        cur = admin.cursor()
        user = _account_with_rows(cur, rows)
        cur.execute("SELECT count(*) FROM finance.income_source "
                    " WHERE user_id = %s", (str(user),))
        assert cur.fetchone()[0] == rows, "the fixture did not create the rows"

        before = _outbox_count(cur, user)
        _purge(cur, user)
        after = _outbox_count(cur, user)

        cur.execute("SELECT identity.count_remaining_source_data(%s)",
                    (str(user),))
        assert cur.fetchone()[0] == 0, "the purge did not actually run"

        assert after == before == 0, (
            f"account purge of {rows} rows emitted {after - before} freshness "
            "events; the policy is zero")
    finally:
        admin.close()


def test_the_purge_does_not_scale_its_event_output_with_deleted_rows():
    """Stated separately from the parametrized case so the intent survives if
    the policy ever changes to emit ONE lifecycle-scoped signal: what must
    never happen is O(rows)."""
    admin = _owner()
    try:
        cur = admin.cursor()
        small = _account_with_rows(cur, 2)
        large = _account_with_rows(cur, 50)
        _purge(cur, small)
        _purge(cur, large)
        assert _outbox_count(cur, small) == _outbox_count(cur, large), (
            "freshness output scaled with the number of purged rows")
    finally:
        admin.close()
