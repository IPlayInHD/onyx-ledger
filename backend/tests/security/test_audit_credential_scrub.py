"""The historical credential detector and scrub, on synthetic rows (§2–§4).

Migration 0046 is forward-only. `audit.audit_log` is append-only by trigger, so
rows written before it keep whatever they contain and nothing in the ordinary
application can remove them. `scripts/audit_credential_scan.py` finds and repairs
them.

Every row here is fabricated by the test. No real credential is used, and the
detector is asserted to report counts and timestamps only — a tool for handling a
credential leak must not become one.
"""
from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

from tests.conftest import owner_dsn

# psycopg2 has no native adapter for `uuid.UUID` until this is called; without
# it every parameter below fails with "can't adapt type 'UUID'".
psycopg2.extras.register_uuid()

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_credential_scan.py"
_spec = importlib.util.spec_from_file_location("audit_credential_scan", _SCRIPT)
assert _spec and _spec.loader
scanner = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scanner)

#: Synthetic. A bcrypt-shaped string that is not a real hash of anything.
FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$FAKEFAKEFAKE$notarealhashvalue"
FAKE_EMAIL = "scrubprobe@example.invalid"


@pytest.fixture
def owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


def _insert(conn, payload: dict | None, *, previous: dict | None = None,
            table: str = "user_credential", action: str = "INSERT",
            when: datetime | None = None) -> uuid.UUID:
    """Write one audit row directly, as the pre-fix trigger would have."""
    row_id = uuid.uuid4()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit.audit_log
                (id, actor_type, actor_id, action, entity_schema, entity_table,
                 entity_id, previous_value, new_value, created_at)
            VALUES (%s, 'system', NULL, %s, 'identity', %s, %s, %s, %s, %s)
            """,
            (row_id, action, table, str(uuid.uuid4()),
             psycopg2.extras.Json(previous) if previous is not None else None,
             psycopg2.extras.Json(payload) if payload is not None else None,
             when or datetime.now(tz=UTC)),
        )
    return row_id


def _payload_of(conn, row_id: uuid.UUID) -> tuple[dict | None, dict | None]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT previous_value, new_value FROM audit.audit_log WHERE id = %s",
            (row_id,),
        )
        return cur.fetchone()


def _scrub(conn) -> None:
    """Run the repair exactly as the script does, trigger dance included."""
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE audit.audit_log DISABLE TRIGGER trg_audit_immutable")
        try:
            scanner.scrub(cur)
        finally:
            cur.execute("ALTER TABLE audit.audit_log ENABLE TRIGGER trg_audit_immutable")


def _candidates(conn) -> int:
    with conn.cursor() as cur:
        return sum(row[3] for row in scanner.scan(cur))


# ---------------------------------------------------------------------------
# the five fixtures §4 requires
# ---------------------------------------------------------------------------
def test_an_old_unsafe_registration_row_is_found_and_repaired(owner):
    unsafe = _insert(owner, {
        "user_id": str(uuid.uuid4()), "password_hash": FAKE_HASH,
        "algorithm": "argon2id", "must_reset": False,
    }, when=datetime.now(tz=UTC) - timedelta(days=30))

    before = _candidates(owner)
    assert before >= 1, "the detector did not see an unsafe row"

    _scrub(owner)

    _, new_value = _payload_of(owner, unsafe)
    assert new_value["password_hash"] == scanner.REDACTED
    # Everything else about the audit event survives.
    assert new_value["algorithm"] == "argon2id"
    assert new_value["must_reset"] is False
    assert "user_id" in new_value


def test_an_already_safe_row_is_untouched(owner):
    safe = _insert(owner, {
        "user_id": str(uuid.uuid4()), "password_hash": scanner.REDACTED,
        "algorithm": "argon2id",
    })
    before, _ = _payload_of(owner, safe)
    _, value_before = _payload_of(owner, safe)

    _scrub(owner)

    _, value_after = _payload_of(owner, safe)
    assert value_after == value_before, "an already-redacted row was rewritten"


def test_an_unrelated_audit_row_is_untouched(owner):
    unrelated = _insert(owner, {
        "id": str(uuid.uuid4()), "amount": "1234.56", "tax_year": 2025,
        "optimization_result_hash": "abc123",
    }, table="income_source", action="UPDATE")
    _, before = _payload_of(owner, unrelated)

    _scrub(owner)

    _, after = _payload_of(owner, unrelated)
    assert after == before, "a row with no credential key was modified"
    # Specifically: a sealed-evidence content hash must survive the scrub.
    assert after["optimization_result_hash"] == "abc123"


def test_a_malformed_payload_does_not_break_the_scan_or_the_scrub(owner):
    # A JSON scalar rather than an object — `?` is false, so it must simply not
    # match, without raising.
    with owner.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit.audit_log
                (id, actor_type, action, entity_schema, entity_table, entity_id,
                 new_value)
            VALUES (%s, 'system', 'INSERT', 'identity', 'user_credential', %s,
                    '"not-an-object"'::jsonb)
            """,
            (uuid.uuid4(), str(uuid.uuid4())),
        )
    _candidates(owner)   # must not raise
    _scrub(owner)        # must not raise


def test_a_null_payload_does_not_break_the_scan_or_the_scrub(owner):
    row = _insert(owner, None, previous=None, action="INSERT")
    _candidates(owner)
    _scrub(owner)
    previous, new_value = _payload_of(owner, row)
    assert previous is None and new_value is None


# ---------------------------------------------------------------------------
# idempotency and output discipline
# ---------------------------------------------------------------------------
def test_the_scrub_is_idempotent(owner):
    _insert(owner, {"user_id": str(uuid.uuid4()), "password_hash": FAKE_HASH})

    _scrub(owner)
    first = _candidates(owner)
    assert first == 0, "the first scrub left candidates behind"

    with owner.cursor() as cur:
        cur.execute("SELECT count(*), md5(string_agg(new_value::text, '|' "
                    "ORDER BY id::text)) FROM audit.audit_log "
                    "WHERE entity_table = 'user_credential' AND new_value IS NOT NULL")
        snapshot = cur.fetchone()

    _scrub(owner)

    with owner.cursor() as cur:
        cur.execute("SELECT count(*), md5(string_agg(new_value::text, '|' "
                    "ORDER BY id::text)) FROM audit.audit_log "
                    "WHERE entity_table = 'user_credential' AND new_value IS NOT NULL")
        assert cur.fetchone() == snapshot, (
            "a second scrub changed rows; the operation is not deterministic"
        )
    assert _candidates(owner) == 0


def test_previous_value_is_scrubbed_as_well_as_new_value(owner):
    """A password CHANGE writes the old hash into `previous_value`. Scrubbing
    only `new_value` would leave the superseded credential behind — and a
    superseded hash is exactly as crackable as a current one."""
    row = _insert(
        owner,
        {"user_id": str(uuid.uuid4()), "password_hash": FAKE_HASH + "new"},
        previous={"user_id": str(uuid.uuid4()), "password_hash": FAKE_HASH},
        action="UPDATE",
    )
    _scrub(owner)
    previous, new_value = _payload_of(owner, row)
    assert previous["password_hash"] == scanner.REDACTED
    assert new_value["password_hash"] == scanner.REDACTED


def test_the_detector_reports_no_payload_content(owner, capsys):
    """The report is counts, timestamps and table names. A tool for handling a
    credential leak must not print one."""
    _insert(owner, {"user_id": str(uuid.uuid4()), "password_hash": FAKE_HASH,
                    "email": FAKE_EMAIL})

    with owner.cursor() as cur:
        scanner.report(scanner.scan(cur))
    printed = capsys.readouterr().out

    assert FAKE_HASH not in printed
    assert FAKE_EMAIL not in printed
    assert "$argon2" not in printed
    assert "user_credential" in printed, "the report should still be useful"

    _scrub(owner)


def test_the_scrub_key_list_matches_the_trigger(owner):
    """If the script and the SQL redaction list drift, the scrub stops matching
    what the trigger now protects — and the gap is invisible until an audit."""
    sql = (Path(__file__).resolve().parents[2] / "db" / "sql"
           / "40_audit_secret_redaction.sql").read_text()
    for key in scanner.SECRET_KEYS:
        assert f"'{key}'" in sql, (
            f"{key} is scrubbed by the script but not redacted by the trigger"
        )


def test_the_append_only_trigger_is_still_enforced_afterwards(owner):
    """The scrub disables the immutability trigger inside its own transaction.
    If it ever failed to restore it, every later audit row would be mutable."""
    row = _insert(owner, {"user_id": str(uuid.uuid4())})
    _scrub(owner)

    with owner.cursor() as cur, pytest.raises(psycopg2.errors.RaiseException):
        cur.execute("UPDATE audit.audit_log SET actor_type = 'tampered' "
                    "WHERE id = %s", (row,))
