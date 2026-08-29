"""What the schema refuses: forged keys, repointed artifacts, open vocabularies.

Every assertion here is a real statement against a real database, run as the
OWNER — the principal with every column privilege — so that a refusal proves
the DATA cannot be shaped that way, not merely that one role's grants are
narrow. The grant-level story is the privileges suite; this is the other half.
"""
from __future__ import annotations

import uuid

import psycopg2
import pytest

from tests.security.billshield.conftest import EVIDENCE, owner_cursor


@pytest.fixture
def rollback_cursor():
    """A cursor whose work is always thrown away."""
    from tests.conftest import owner_dsn
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = False
    try:
        yield conn.cursor()
    finally:
        conn.rollback()
        conn.close()


# ------------------------------------------------------------ storage identity

def test_the_storage_key_is_computed_from_its_own_row(tenants):
    a, b = tenants
    with owner_cursor() as cur:
        cur.execute(
            "SELECT id, user_id, storage_key FROM billshield.bill WHERE id IN (%s, %s)",
            (str(a.bill_id), str(b.bill_id)),
        )
        for row_id, user_id, key in cur.fetchall():
            assert key == f"{user_id}/billshield/v1/{row_id}"


def test_a_forged_storage_key_naming_another_tenant_cannot_be_written(tenants):
    """The whole reason the column is GENERATED rather than validated.

    A CHECK on the key's SHAPE would accept this string: it is a well-formed
    `{uuid}/billshield/v1/{uuid}`. What it is not is this row's key — and since
    no writer supplies the column at all, there is no statement that can set it.
    """
    a, b = tenants
    forged = f"{b.user_id}/billshield/v1/{b.bill_id}"
    with owner_cursor() as cur:
        with pytest.raises(psycopg2.errors.GeneratedAlways) as excinfo:
            cur.execute(
                "UPDATE billshield.bill SET storage_key = %s WHERE id = %s",
                (forged, str(a.bill_id)),
            )
        assert "can only be updated to DEFAULT" in str(excinfo.value)

    with owner_cursor() as cur:
        with pytest.raises(psycopg2.errors.GeneratedAlways):
            cur.execute(
                "INSERT INTO billshield.bill (user_id, storage_key) VALUES (%s, %s)",
                (str(a.user_id), forged),
            )


# --------------------------------------------------------- artifact provenance

@pytest.mark.parametrize("column,value,fragment", [
    ("file_sha256", "'" + "c" * 64 + "'", "artifact digest is immutable"),
    ("byte_size", "2048", "artifact size is immutable"),
    ("artifact_format", "'image_png'", "artifact format is immutable"),
    ("page_count", "9", "page count is immutable"),
    ("file_sha256", "NULL", "artifact digest is immutable"),
])
def test_finalized_artifact_facts_cannot_be_changed_or_cleared(
        tenants, rollback_cursor, column, value, fragment):
    """Each fact separately, because a loop that shares a poisoned transaction
    proves only that the FIRST statement failed."""
    a, _ = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        cur.execute(
            f"UPDATE billshield.bill SET {column} = {value} WHERE id = %s",
            (str(a.bill_id),))
    assert fragment in str(excinfo.value)


def test_artifact_facts_may_be_set_once_on_a_pending_bill(tenants, rollback_cursor):
    """The control: unset -> set is exactly what upload completion does."""
    a, _ = tenants
    cur = rollback_cursor
    cur.execute(
        "UPDATE billshield.bill SET file_sha256 = %s, byte_size = 10,"
        " artifact_format = 'pdf_native', page_count = 1, status = 'uploaded'"
        " WHERE id = %s",
        ("d" * 64, str(a.spare_bill_id)),
    )
    assert cur.rowcount == 1


def test_an_extraction_cannot_be_repointed_at_another_artifact(tenants, rollback_cursor):
    """Composite FK: the run's input digest IS its bill's finalized digest."""
    a, b = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.Error) as excinfo:
        cur.execute(
            "UPDATE billshield.extraction_run SET input_sha256 = %s WHERE id = %s",
            (b.file_sha256, str(a.run_id)))
    message = str(excinfo.value)
    assert "immutable" in message or "foreign key" in message


def test_an_extraction_cannot_claim_a_digest_its_bill_never_finalized(
        tenants, rollback_cursor):
    a, _ = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.errors.ForeignKeyViolation) as excinfo:
        cur.execute(
            "INSERT INTO billshield.extraction_run (bill_id, input_sha256)"
            " VALUES (%s, %s)", (str(a.bill_id), "e" * 64))
    assert "fk_billshield_extraction_run_bill_artifact" in str(excinfo.value)


def test_a_terminal_extraction_run_is_frozen(tenants, rollback_cursor):
    a, _ = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        cur.execute(
            "UPDATE billshield.extraction_run SET adapter_code = 'other' WHERE id = %s",
            (str(a.run_id),))
    assert "immutable once terminal" in str(excinfo.value)


@pytest.mark.parametrize("table,row_attr", [
    ("billshield.charge_candidate", "charge_id"),
    ("billshield.promotion_candidate", "promotion_id"),
])
def test_candidate_rows_are_immutable_extracted_facts(
        tenants, rollback_cursor, table, row_attr):
    """A correction is a new confirmed observation, never an edit here."""
    a, _ = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        cur.execute(f"UPDATE {table} SET created_at = now() WHERE id = %s",
                    (str(getattr(a, row_attr)),))
    assert "immutable extracted facts" in str(excinfo.value)


# ----------------------------------------------------------- closed vocabulary

@pytest.mark.parametrize("statement,token", [
    ("INSERT INTO billshield.job_outbox (user_id, bill_id, task_code, dedupe_key)"
     " VALUES (%(user)s, %(bill)s, 'EXTRACT_EVERYTHING', %(key)s)",
     "ck_billshield_job_outbox_task_code"),
    # An otherwise complete bill, so the status constraint is the ONLY one that
    # can fire and the assertion names the constraint that actually spoke.
    ("INSERT INTO billshield.bill (user_id, status, file_sha256, byte_size,"
     " artifact_format, page_count) VALUES (%(user)s, 'ARCHIVED',"
     " '" + "f" * 64 + "', 1, 'pdf_native', 1)",
     "ck_billshield_bill_status"),
    # Adapter identity and completion stamp supplied, so the coherence
    # constraints are all satisfied and only the status vocabulary can refuse.
    ("INSERT INTO billshield.extraction_run (bill_id, input_sha256, status,"
     " adapter_code, model_version, completed_at)"
     " VALUES (%(bill)s, %(digest)s, 'PARTIALLY_DONE', 'fixture', 'v1', now())",
     "ck_billshield_extraction_run_status"),
])
def test_an_unknown_uppercase_token_is_refused(tenants, rollback_cursor,
                                               statement, token):
    """Not merely prose with spaces — an unknown token that LOOKS like a code.

    A bounded character-class regex accepts every string here. Only a real value
    list refuses them, which is the difference between a closed vocabulary and
    text that resembles one.
    """
    a, _ = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        cur.execute(statement, {"user": str(a.user_id), "bill": str(a.bill_id),
                                "digest": a.file_sha256, "key": str(uuid.uuid4())})
    assert token in str(excinfo.value)


def test_an_unknown_refusal_code_is_refused(tenants, rollback_cursor):
    a, _ = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        cur.execute(
            "INSERT INTO billshield.extraction_run (bill_id, input_sha256, status,"
            " refusal_code, adapter_code, model_version, completed_at)"
            " VALUES (%s, %s, 'refused', 'TOO_BLURRY', 'fixture', 'v1', now())",
            (str(a.bill_id), a.file_sha256))
    assert "refusal_code" in str(excinfo.value)


def test_worker_identity_cannot_hold_prose(tenants, rollback_cursor):
    """`claimed_by` is a bounded opaque token, not a place to write a sentence."""
    a, _ = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        cur.execute(
            "INSERT INTO billshield.job_outbox (user_id, bill_id, task_code,"
            " dedupe_key, claim_state, claimed_by, claim_token, claimed_at)"
            " VALUES (%s, %s, 'EXTRACT_BILL', %s, 'claimed',"
            " 'worker crashed: ValueError(...)', gen_random_uuid(), now())",
            (str(a.user_id), str(a.spare_bill_id), str(uuid.uuid4())))
    assert "claimed_by" in str(excinfo.value)


def test_the_outbox_has_no_column_that_could_hold_bill_content():
    """Structural, not a promise: the columns simply do not exist.

    Also pins the deliberate absence of a failure-code column — its Python
    authority arrives with the worker in Slice 3, and an open uppercase text
    column now would be a "closed code" that is really free text.
    """
    with owner_cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'billshield' AND table_name = 'job_outbox'")
        columns = {row[0] for row in cur.fetchall()}
    assert columns == {
        "id", "user_id", "bill_id", "task_code", "dedupe_key", "claim_state",
        "claimed_by", "claim_token", "claimed_at", "processed_at", "attempts",
        "created_at",
    }, f"the outbox column set changed: {sorted(columns)}"


def test_no_billshield_column_can_hold_a_filename_or_raw_payload():
    """A sweep, so a future column called `original_filename` fails here."""
    forbidden = ("filename", "file_name", "original_name", "raw_response",
                 "provider_response", "payload", "exception", "error_message",
                 "stack", "content", "bytes", "blob")
    with owner_cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, data_type FROM information_schema.columns"
            " WHERE table_schema = 'billshield'")
        rows = cur.fetchall()
    offenders = [f"{t}.{c}" for t, c, _ in rows
                 if any(word in c.lower() for word in forbidden)]
    assert not offenders, f"columns that could hold bill content: {offenders}"
    binary = [f"{t}.{c}" for t, c, dtype in rows if dtype in ("bytea", "oid")]
    assert not binary, f"binary columns in billshield: {binary}"


# ------------------------------------------------------- deletion truthfulness

def test_deletion_pending_requires_a_logical_timestamp_and_no_erasure(
        tenants, rollback_cursor):
    a, _ = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        cur.execute(
            "UPDATE billshield.bill SET status = 'deletion_pending' WHERE id = %s",
            (str(a.bill_id),))
    assert "deletion_timestamps" in str(excinfo.value)


def test_a_bill_cannot_claim_erasure_before_deletion(tenants, rollback_cursor):
    a, _ = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "UPDATE billshield.bill SET status = 'deleted', erased_at = now()"
            " WHERE id = %s", (str(a.bill_id),))


def test_the_logical_then_physical_deletion_sequence_is_accepted(
        tenants, rollback_cursor):
    """The control: pending carries only deleted_at, deleted carries both."""
    a, _ = tenants
    cur = rollback_cursor
    cur.execute(
        "UPDATE billshield.bill SET status = 'deletion_pending', deleted_at = now()"
        " WHERE id = %s", (str(a.bill_id),))
    assert cur.rowcount == 1
    cur.execute(
        "UPDATE billshield.bill SET status = 'deleted', erased_at = now()"
        " WHERE id = %s", (str(a.bill_id),))
    assert cur.rowcount == 1


@pytest.mark.parametrize("terminal", ["rejected", "failed"])
def test_a_terminal_bill_can_reach_deletion_pending(tenants, rollback_cursor,
                                                    terminal):
    """§7.5 promises erasure for unusable bills; the states must allow it.

    ONE bill per terminal state, and no reset. The earlier version of this test
    moved a bill back out of `deletion_pending` by clearing `deleted_at` — a
    resurrection path that is now correctly refused, and that should never have
    been the mechanism a passing test relied on.
    """
    a, _ = tenants
    cur = rollback_cursor
    cur.execute(
        "INSERT INTO billshield.bill (user_id, status, file_sha256, byte_size,"
        " artifact_format, page_count) VALUES (%s, %s, %s, 1, 'pdf_native', 1)"
        " RETURNING id",
        (str(a.user_id), terminal, uuid.uuid4().hex * 2))
    bill_id = cur.fetchone()[0]
    cur.execute(
        "UPDATE billshield.bill SET status = 'deletion_pending', deleted_at = now()"
        " WHERE id = %s", (str(bill_id),))
    assert cur.rowcount == 1


# ------------------------------------------------------------ catalogue shape

def test_a_provider_can_hold_many_categories(rollback_cursor):
    """The correction that made this a seven-table foundation.

    A Canadian provider spans mobile, internet, television, home phone and
    bundles. A scalar category column would have to misrepresent all but one.
    """
    cur = rollback_cursor
    cur.execute(
        "INSERT INTO billshield.provider (code, name) VALUES ('ACME_TELECOM',"
        " 'Acme Telecom') RETURNING id")
    provider_id = cur.fetchone()[0]
    for category in ("MOBILE", "INTERNET", "TV", "HOME_PHONE", "BUNDLE"):
        cur.execute(
            "INSERT INTO billshield.provider_category (provider_id, category)"
            " VALUES (%s, %s)", (str(provider_id), category))
    cur.execute(
        "SELECT count(*) FROM billshield.provider_category WHERE provider_id = %s",
        (str(provider_id),))
    assert cur.fetchone()[0] == 5

    with pytest.raises(psycopg2.errors.UniqueViolation):
        cur.execute(
            "INSERT INTO billshield.provider_category (provider_id, category)"
            " VALUES (%s, 'MOBILE')", (str(provider_id),))


def test_the_provider_table_has_no_category_column_and_no_issuer_link():
    """Two absences, both load-bearing."""
    with owner_cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'billshield' AND table_name = 'provider'")
        columns = {row[0] for row in cur.fetchall()}
    assert "category" not in columns, "provider regained a scalar category column"

    with owner_cursor() as cur:
        cur.execute(
            """
            SELECT c.relname, a.attname
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_class f ON f.oid = con.confrelid
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(con.conkey)
            WHERE con.contype = 'f' AND n.nspname = 'billshield'
              AND f.relname IN ('provider', 'provider_category')
            """
        )
        edges = cur.fetchall()
    assert all(table == "provider_category" for table, _ in edges), (
        f"an operational table references the provider catalogue: {edges}. "
        "Nothing may resolve untrusted issuer text to a governed provider id "
        "in this slice.")


def test_promotions_cannot_reference_another_runs_charge(tenants, rollback_cursor):
    """The association is by position WITHIN one extraction, enforced."""
    a, b = tenants
    cur = rollback_cursor
    # B's run has a charge at position 0; A's promotion may not borrow it.
    with pytest.raises(psycopg2.errors.ForeignKeyViolation) as excinfo:
        cur.execute(
            "INSERT INTO billshield.promotion_candidate (extraction_run_id, position,"
            " charge_position, expiry_date, expiry_confidence, expiry_evidence)"
            " VALUES (%s, 1, 7, DATE '2026-06-30', 0.5,"
            " %s::billshield.evidence_locators)",
            (str(a.run_id), EVIDENCE))
    assert "fk_billshield_promotion_charge" in str(excinfo.value)
    assert str(b.run_id) not in str(excinfo.value)


# =============================================================================
# CORRECTION PASS — adversarial invariants
#
# Each test below was written against the previous implementation and observed
# FAILING for the stated reason before the schema was corrected.
# =============================================================================

# ------------------------------------------------- extraction failure codes

def test_a_failed_run_carries_a_committed_parse_code(tenants, rollback_cursor):
    """`failed` has an authority — ExtractionParseCode — so it must record one."""
    a, _ = tenants
    cur = rollback_cursor
    cur.execute(
        "INSERT INTO billshield.extraction_run (bill_id, input_sha256, status,"
        " failure_code, adapter_code, model_version, completed_at)"
        " VALUES (%s, %s, 'failed', 'EVIDENCE_OUT_OF_BOUNDS', 'fixture', 'v1', now())"
        " RETURNING id",
        (str(a.bill_id), a.file_sha256))
    assert cur.fetchone()[0] is not None


def test_an_unknown_uppercase_parse_code_is_refused(tenants, rollback_cursor):
    """A regex-only column would accept this. A value list does not."""
    a, _ = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        cur.execute(
            "INSERT INTO billshield.extraction_run (bill_id, input_sha256, status,"
            " failure_code, adapter_code, model_version, completed_at)"
            " VALUES (%s, %s, 'failed', 'EVIDENCE_WAS_WEIRD', 'fixture', 'v1', now())",
            (str(a.bill_id), a.file_sha256))
    assert "failure_code" in str(excinfo.value)


def test_a_failed_run_without_a_code_is_refused(tenants, rollback_cursor):
    a, _ = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        cur.execute(
            "INSERT INTO billshield.extraction_run (bill_id, input_sha256, status,"
            " adapter_code, model_version, completed_at)"
            " VALUES (%s, %s, 'failed', 'fixture', 'v1', now())",
            (str(a.bill_id), a.file_sha256))
    assert "failure_coherent" in str(excinfo.value)


#: For each non-failed status, a row that is valid in EVERY other respect.
#: The test then adds `failure_code` as the single defect, so the constraint
#: that fires can only be the failure-coherence one.
#:
#: The previous version of this test passed for the wrong reasons: it sent the
#: same fully-populated row for every status, so the `running` case also broke
#: terminal/success coherence, the `refused` case was missing its required
#: refusal code AND carried success-only fields, and a dead `extra` parameter
#: was never interpolated. A refusal proves nothing if several constraints
#: could have produced it.
_OTHERWISE_VALID_ROW = {
    # No completion stamp, no success fields, and adapter identity absent as a
    # coherent pair — a legitimate in-flight run.
    "running": {"status": "running"},
    # A complete refusal: its code, its stamp, and a whole adapter identity.
    "refused": {"status": "refused", "refusal_code": "UNREADABLE",
                "completed_at": "now()", "adapter_code": "'fixture'",
                "model_version": "'v1'"},
    # A complete success: all three success fields plus identity and stamp.
    "succeeded": {"status": "succeeded", "response_hash": "'" + "a" * 64 + "'",
                  "currency": "'CAD'", "extraction_schema_version": "'1.0.0'",
                  "completed_at": "now()", "adapter_code": "'fixture'",
                  "model_version": "'v1'"},
}


@pytest.mark.parametrize("status", ["running", "refused", "succeeded"])
def test_only_a_failed_run_may_carry_a_failure_code(tenants, rollback_cursor,
                                                    status):
    """A parse code on a running, refused or succeeded row is a contradiction.

    Each row below is otherwise valid — proven by the companion control test,
    which inserts the identical row WITHOUT `failure_code` and expects it to be
    accepted. So when the code is added and the insert is refused, the named
    constraint is the reason and nothing else could be.
    """
    a, _ = tenants
    cur = rollback_cursor
    row = dict(_OTHERWISE_VALID_ROW[status])
    row["failure_code"] = "'MALFORMED_VALUE'"
    columns = ["bill_id", "input_sha256"] + list(row)
    values = ["%s", "%s"] + [
        f"'{v}'" if key == "status" or key == "refusal_code" else str(v)
        for key, v in row.items()]
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        cur.execute(
            f"INSERT INTO billshield.extraction_run ({', '.join(columns)})"
            f" VALUES ({', '.join(values)})", (str(a.bill_id), a.file_sha256))
    assert "ck_billshield_extraction_run_failure_coherent" in str(excinfo.value), (
        "a constraint OTHER than failure-coherence refused this row, so the "
        "test does not prove what it claims")


@pytest.mark.parametrize("status", ["running", "refused", "succeeded"])
def test_the_same_row_without_a_failure_code_is_accepted(tenants, rollback_cursor,
                                                         status):
    """The control that makes the test above non-vacuous.

    Without this, a row that violated three constraints would still "pass" the
    refusal test, and nobody would know the failure code was irrelevant.
    """
    a, _ = tenants
    cur = rollback_cursor
    row = dict(_OTHERWISE_VALID_ROW[status])
    columns = ["bill_id", "input_sha256"] + list(row)
    values = ["%s", "%s"] + [
        f"'{v}'" if key in ("status", "refusal_code") else str(v)
        for key, v in row.items()]
    cur.execute(
        f"INSERT INTO billshield.extraction_run ({', '.join(columns)})"
        f" VALUES ({', '.join(values)}) RETURNING id",
        (str(a.bill_id), a.file_sha256))
    assert cur.fetchone()[0] is not None


def test_a_run_cannot_be_both_refused_and_failed(tenants, rollback_cursor):
    a, _ = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "INSERT INTO billshield.extraction_run (bill_id, input_sha256, status,"
            " refusal_code, failure_code, adapter_code, model_version, completed_at)"
            " VALUES (%s, %s, 'refused', 'UNREADABLE', 'MALFORMED_VALUE',"
            " 'fixture', 'v1', now())",
            (str(a.bill_id), a.file_sha256))


def test_the_outbox_still_has_no_failure_code_column():
    """Correction 1 adds the code that HAS an authority, not the one that does not."""
    with owner_cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'billshield' AND table_name = 'job_outbox'")
        columns = {row[0] for row in cur.fetchall()}
    assert "last_error_code" not in columns
    assert "failure_code" not in columns


# ------------------------------------------------------- outbox idempotency

def test_one_intent_per_task_and_bill(tenants, rollback_cursor):
    """A fresh UUID must NOT buy a second EXTRACT_BILL for the same bill.

    UNIQUE(dedupe_key) alone cannot say this: the caller picks the key, so a
    caller that generates a new UUID enqueues the same work twice.
    """
    a, _ = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.errors.UniqueViolation) as excinfo:
        cur.execute(
            "INSERT INTO billshield.job_outbox (user_id, bill_id, task_code,"
            " dedupe_key) VALUES (%s, %s, 'EXTRACT_BILL', %s)",
            (str(a.user_id), str(a.bill_id), str(uuid.uuid4())))
    assert "uq_billshield_job_outbox_intent" in str(excinfo.value)


def test_a_dedupe_key_cannot_be_reused_on_another_bill(tenants, rollback_cursor):
    """Global key uniqueness still holds; the two constraints are independent."""
    a, _ = tenants
    cur = rollback_cursor
    cur.execute("SELECT dedupe_key FROM billshield.job_outbox WHERE id = %s",
                (str(a.outbox_id),))
    existing = cur.fetchone()[0]
    with pytest.raises(psycopg2.errors.UniqueViolation) as excinfo:
        cur.execute(
            "INSERT INTO billshield.job_outbox (user_id, bill_id, task_code,"
            " dedupe_key) VALUES (%s, %s, 'EXTRACT_BILL', %s)",
            (str(a.user_id), str(a.spare_bill_id), str(existing)))
    assert "uq_billshield_job_outbox_dedupe" in str(excinfo.value)


def test_a_different_bill_with_a_different_key_is_accepted(tenants, rollback_cursor):
    """The control: idempotency must not become a queue that accepts nothing."""
    a, _ = tenants
    cur = rollback_cursor
    cur.execute(
        "INSERT INTO billshield.job_outbox (user_id, bill_id, task_code, dedupe_key)"
        " VALUES (%s, %s, 'EXTRACT_BILL', %s) RETURNING id",
        (str(a.user_id), str(a.spare_bill_id), str(uuid.uuid4())))
    assert cur.fetchone()[0] is not None


def test_a_retry_is_a_state_transition_on_the_original_row(tenants, rollback_cursor):
    """Retries live in the state machine, not in new rows."""
    a, _ = tenants
    cur = rollback_cursor
    cur.execute(
        "UPDATE billshield.job_outbox SET claim_state = 'pending', attempts = 1"
        " WHERE id = %s", (str(a.outbox_id),))
    assert cur.rowcount == 1
    cur.execute("SELECT count(*) FROM billshield.job_outbox WHERE bill_id = %s"
                " AND task_code = 'EXTRACT_BILL'", (str(a.bill_id),))
    assert cur.fetchone()[0] == 1


# ---------------------------------------------------------- evidence geometry

@pytest.mark.parametrize("x0,y0,x1,y1,why", [
    ("0.500000", "0.100000", "0.500000", "0.400000", "x0 == x1, zero width"),
    ("0.900000", "0.100000", "0.100000", "0.400000", "x0 > x1, inverted"),
    ("0.100000", "0.500000", "0.900000", "0.500000", "y0 == y1, zero height"),
    ("0.100000", "0.900000", "0.900000", "0.400000", "y0 > y1, inverted"),
    # Lexicographically "0.1" < "0.10" — only a NUMERIC comparison sees that
    # this box has zero width.
    ("0.1", "0.100000", "0.10", "0.400000", "zero width via trailing zeros"),
])
def test_a_degenerate_box_is_refused(tenants, rollback_cursor, x0, y0, x1, y1, why):
    a, _ = tenants
    cur = rollback_cursor
    locator = (f'[{{"page": 1, "x0": "{x0}", "y0": "{y0}",'
               f' "x1": "{x1}", "y1": "{y1}"}}]')
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        cur.execute(
            "INSERT INTO billshield.extraction_run (bill_id, input_sha256,"
            " amount_due_value, amount_due_confidence, amount_due_evidence)"
            " VALUES (%s, %s, 1.00, 0.5, %s::billshield.evidence_locators)",
            (str(a.bill_id), a.file_sha256, locator))
    assert "evidence_locators" in str(excinfo.value), why


def test_a_well_formed_box_is_still_accepted(tenants, rollback_cursor):
    """The control for the geometry constraint."""
    cur = rollback_cursor
    cur.execute("SELECT %s::billshield.evidence_locators", (EVIDENCE,))
    assert cur.fetchone()[0] is not None


# ------------------------------------------------------- extraction coherence

@pytest.mark.parametrize("columns,values,why", [
    # Partial success fields on a NON-succeeded row: previously accepted,
    # because equality on a 3-way num_nonnulls only pinned the count.
    ("status, response_hash, adapter_code, model_version, completed_at",
     f"'failed', '{'a' * 64}', 'fixture', 'v1', now()",
     "a failed run carrying a response hash"),
    ("status, currency, adapter_code, model_version, completed_at",
     "'refused', 'CAD', 'fixture', 'v1', now()",
     "a refused run carrying a currency"),
    ("status, extraction_schema_version, adapter_code, model_version, completed_at",
     "'refused', '1.0.0', 'fixture', 'v1', now()",
     "a refused run carrying a schema version"),
    # Partial adapter identity on a terminal row.
    ("status, adapter_code, completed_at, refusal_code",
     "'refused', 'fixture', now(), 'UNREADABLE'",
     "adapter_code without model_version"),
    ("status, model_version, completed_at, refusal_code",
     "'refused', 'v1', now(), 'UNREADABLE'",
     "model_version without adapter_code"),
    # prompt_version without adapter identity at all.
    ("status, prompt_version",
     "'running', 'p1'",
     "prompt_version with no adapter identity"),
    # A partially-populated pair on a running row.
    ("status, adapter_code", "'running', 'fixture'",
     "a running row with half an adapter identity"),
])
def test_a_partial_extraction_state_is_refused(tenants, rollback_cursor,
                                               columns, values, why):
    a, _ = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            f"INSERT INTO billshield.extraction_run (bill_id, input_sha256, {columns})"
            f" VALUES (%s, %s, {values})", (str(a.bill_id), a.file_sha256))


def test_a_succeeded_run_carries_all_three_success_fields(tenants, rollback_cursor):
    """The positive control for the tightened success coherence."""
    a, _ = tenants
    cur = rollback_cursor
    cur.execute(
        "INSERT INTO billshield.extraction_run (bill_id, input_sha256, status,"
        " response_hash, currency, extraction_schema_version, adapter_code,"
        " model_version, prompt_version, completed_at, amount_due_value,"
        " amount_due_confidence, amount_due_evidence)"
        f" VALUES (%s, %s, 'succeeded', '{'c' * 64}', 'CAD', '1.0.0', 'fixture',"
        " 'v1', 'p1', now(), 5.00, 0.5, %s::billshield.evidence_locators)"
        " RETURNING id",
        (str(a.bill_id), a.file_sha256, EVIDENCE))
    assert cur.fetchone()[0] is not None


def test_a_running_run_has_no_completion_stamp(tenants, rollback_cursor):
    a, _ = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "INSERT INTO billshield.extraction_run (bill_id, input_sha256, status,"
            " completed_at) VALUES (%s, %s, 'running', now())",
            (str(a.bill_id), a.file_sha256))


def test_an_upload_pending_bill_cannot_already_be_finalized(tenants, rollback_cursor):
    """Artifact facts arrive AT upload completion, not before it."""
    a, _ = tenants
    cur = rollback_cursor
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        cur.execute(
            "INSERT INTO billshield.bill (user_id, status, file_sha256, byte_size,"
            " artifact_format, page_count)"
            f" VALUES (%s, 'upload_pending', '{'a' * 64}', 1, 'pdf_native', 1)",
            (str(a.user_id),))
    assert "ck_billshield_bill_pending_is_not_finalized" in str(excinfo.value)


# --------------------------------------------------- deletion fact immutability

def test_a_recorded_deletion_timestamp_cannot_change(tenants, rollback_cursor):
    a, _ = tenants
    cur = rollback_cursor
    cur.execute(
        "UPDATE billshield.bill SET status = 'deletion_pending', deleted_at = now()"
        " WHERE id = %s", (str(a.spare_bill_id),))
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        cur.execute(
            "UPDATE billshield.bill SET deleted_at = now() + interval '1 day'"
            " WHERE id = %s", (str(a.spare_bill_id),))
    assert "deleted_at is immutable" in str(excinfo.value)


def test_a_recorded_deletion_timestamp_cannot_be_cleared(tenants, rollback_cursor):
    """The resurrection path: pending -> back to a live state, tombstone erased."""
    a, _ = tenants
    cur = rollback_cursor
    cur.execute(
        "UPDATE billshield.bill SET status = 'deletion_pending', deleted_at = now()"
        " WHERE id = %s", (str(a.spare_bill_id),))
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        cur.execute(
            "UPDATE billshield.bill SET status = 'upload_pending', deleted_at = NULL"
            " WHERE id = %s", (str(a.spare_bill_id),))
    assert "deleted_at is immutable" in str(excinfo.value)


def test_a_recorded_erasure_timestamp_cannot_change_or_clear(tenants, rollback_cursor):
    a, _ = tenants
    cur = rollback_cursor
    cur.execute(
        "UPDATE billshield.bill SET status = 'deletion_pending', deleted_at = now()"
        " WHERE id = %s", (str(a.spare_bill_id),))
    cur.execute(
        "UPDATE billshield.bill SET status = 'deleted', erased_at = now()"
        " WHERE id = %s", (str(a.spare_bill_id),))
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        cur.execute(
            "UPDATE billshield.bill SET erased_at = now() + interval '1 day'"
            " WHERE id = %s", (str(a.spare_bill_id),))
    assert "erased_at is immutable" in str(excinfo.value)


def test_the_account_cascade_still_removes_a_deleted_bill(rollback_cursor):
    """Immutability must not make an account undeletable."""
    cur = rollback_cursor
    cur.execute("INSERT INTO identity.user_account (email) VALUES (%s) RETURNING id",
                (f"cascade-{uuid.uuid4().hex[:8]}@example.test",))
    user_id = cur.fetchone()[0]
    cur.execute("INSERT INTO billshield.bill (user_id) VALUES (%s) RETURNING id",
                (str(user_id),))
    bill_id = cur.fetchone()[0]
    cur.execute(
        "UPDATE billshield.bill SET status = 'deletion_pending', deleted_at = now()"
        " WHERE id = %s", (str(bill_id),))
    cur.execute(
        "UPDATE billshield.bill SET status = 'deleted', erased_at = now()"
        " WHERE id = %s", (str(bill_id),))
    cur.execute("DELETE FROM identity.user_account WHERE id = %s", (str(user_id),))
    cur.execute("SELECT count(*) FROM billshield.bill WHERE id = %s", (str(bill_id),))
    assert cur.fetchone()[0] == 0


# ------------------------------------------------------ promotion association

def test_a_promotion_cannot_borrow_a_charge_position_from_another_run(
        tenants, rollback_cursor):
    """Plant the position in run B, then prove run A cannot reference it.

    The earlier version of this test used position 7, which existed nowhere —
    so it proved only that a dangling reference fails, not that the reference
    is scoped to ONE run.
    """
    a, b = tenants
    cur = rollback_cursor
    cur.execute(
        "INSERT INTO billshield.charge_candidate (extraction_run_id, position,"
        " label_text, label_confidence, label_evidence, amount, amount_confidence,"
        " amount_evidence, kind, kind_confidence)"
        " VALUES (%s, 7, 'B-only charge', 0.9, %s::billshield.evidence_locators,"
        " 1.00, 0.9, %s::billshield.evidence_locators, 'ONE_TIME', 0.9)",
        (str(b.run_id), EVIDENCE, EVIDENCE))
    cur.execute(
        "SELECT count(*) FROM billshield.charge_candidate"
        " WHERE extraction_run_id = %s AND position = 7", (str(b.run_id),))
    assert cur.fetchone()[0] == 1, "position 7 must really exist in run B"

    with pytest.raises(psycopg2.errors.ForeignKeyViolation) as excinfo:
        cur.execute(
            "INSERT INTO billshield.promotion_candidate (extraction_run_id, position,"
            " charge_position, expiry_date, expiry_confidence, expiry_evidence)"
            " VALUES (%s, 5, 7, DATE '2026-06-30', 0.5,"
            " %s::billshield.evidence_locators)",
            (str(a.run_id), EVIDENCE))
    assert "fk_billshield_promotion_charge" in str(excinfo.value)


# =============================================================================
# LIVE PERSISTENCE ROUND TRIP
#
# The parity test proves the MAPPING is lossless without a database, which is
# fast and catches most drift. It cannot prove that PostgreSQL returns what was
# stored: NUMERIC scale, JSONB key order, date types and text encoding all sit
# between the two, and every one of them can quietly change a hash.
#
# So this walks the whole path — parse, insert into the real seven tables, read
# back, rebuild the contract object — and compares the canonical form and the
# identity hash against the original AND against the response_hash the row
# carries.
# =============================================================================

def _fetch_extraction(cur, run_id):
    """Read one extraction back out of the real schema, as rows."""
    cur.execute(
        "SELECT extraction_schema_version, currency,"
        " issuer_name_value, issuer_name_confidence, issuer_name_evidence,"
        " service_category_value, service_category_confidence,"
        " service_category_evidence,"
        " statement_date_value, statement_date_confidence, statement_date_evidence,"
        " billing_period_start, billing_period_end, billing_period_confidence,"
        " billing_period_evidence,"
        " amount_due_value, amount_due_confidence, amount_due_evidence,"
        " previous_balance_value, previous_balance_confidence,"
        " previous_balance_evidence,"
        " payments_applied_value, payments_applied_confidence,"
        " payments_applied_evidence,"
        " subtotal_before_tax_value, subtotal_before_tax_confidence,"
        " subtotal_before_tax_evidence,"
        " total_tax_value, total_tax_confidence, total_tax_evidence,"
        " response_hash"
        " FROM billshield.extraction_run WHERE id = %s", (str(run_id),))
    row = cur.fetchone()
    scalar_fields = ("bill_issuer_name", "service_category", "statement_date")
    run = {"extraction_schema_version": row[0], "currency": row[1]}
    index = 2
    for name in scalar_fields:
        run[name] = {"value": row[index], "confidence": row[index + 1],
                     "evidence": row[index + 2]}
        index += 3
    run["billing_period"] = {"start": row[index], "end": row[index + 1],
                             "confidence": row[index + 2],
                             "evidence": row[index + 3]}
    index += 4
    for name in ("amount_due", "previous_balance", "payments_applied",
                 "subtotal_before_tax", "total_tax"):
        run[name] = {"value": row[index], "confidence": row[index + 1],
                     "evidence": row[index + 2]}
        index += 3
    stored_hash = row[index]

    cur.execute(
        "SELECT position, label_text, label_confidence, label_evidence, amount,"
        " amount_confidence, amount_evidence, kind, kind_confidence,"
        " cadence_value, cadence_confidence, cadence_evidence,"
        " service_period_start, service_period_end, service_period_confidence,"
        " service_period_evidence"
        " FROM billshield.charge_candidate WHERE extraction_run_id = %s"
        " ORDER BY position", (str(run_id),))
    charges = [
        {"position": c[0], "label_text": c[1], "label_confidence": c[2],
         "label_evidence": c[3], "amount": c[4], "amount_confidence": c[5],
         "amount_evidence": c[6], "kind": c[7], "kind_confidence": c[8],
         "cadence": {"value": c[9], "confidence": c[10], "evidence": c[11]},
         "service_period": {"start": c[12], "end": c[13], "confidence": c[14],
                            "evidence": c[15]}}
        for c in cur.fetchall()
    ]

    cur.execute(
        "SELECT position, charge_position, expiry_date, expiry_confidence,"
        " expiry_evidence FROM billshield.promotion_candidate"
        " WHERE extraction_run_id = %s ORDER BY position", (str(run_id),))
    promotions = [
        {"position": p[0], "charge_position": p[1], "expiry_date": p[2],
         "expiry_confidence": p[3], "expiry_evidence": p[4]}
        for p in cur.fetchall()
    ]
    return {"run": run, "charges": charges, "promotions": promotions}, stored_hash


def _persist_extraction(cur, bill_id, digest, extraction):
    """Write the extraction into the real schema and return the run id."""
    import json

    from tests.unit.billshield.test_persistence_contract_parity import _rows_from

    rows = _rows_from(extraction)
    run, params = rows["run"], {}
    columns = ["bill_id", "input_sha256", "status", "adapter_code", "model_version",
               "extraction_schema_version", "currency", "response_hash",
               "completed_at"]
    values = ["%(bill_id)s", "%(digest)s", "'succeeded'", "'fixture'",
              "'fixture-1.0.0'", "%(schema_version)s", "%(currency)s",
              "%(response_hash)s", "now()"]
    params.update(bill_id=str(bill_id), digest=digest,
                  schema_version=run["extraction_schema_version"],
                  currency=run["currency"],
                  response_hash=extraction.extraction_identity())

    field_columns = {
        "bill_issuer_name": "issuer_name", "service_category": "service_category",
        "statement_date": "statement_date", "amount_due": "amount_due",
        "previous_balance": "previous_balance",
        "payments_applied": "payments_applied",
        "subtotal_before_tax": "subtotal_before_tax", "total_tax": "total_tax",
    }
    for name, prefix in field_columns.items():
        triple = run[name]
        if triple["value"] is None:
            continue
        columns += [f"{prefix}_value", f"{prefix}_confidence", f"{prefix}_evidence"]
        values += [f"%({prefix}_v)s", f"%({prefix}_c)s",
                   f"%({prefix}_e)s::billshield.evidence_locators"]
        value = triple["value"]
        params[f"{prefix}_v"] = value.value if hasattr(value, "value") else value
        params[f"{prefix}_c"] = triple["confidence"]
        params[f"{prefix}_e"] = json.dumps(triple["evidence"])

    period = run["billing_period"]
    if period["start"] is not None:
        columns += ["billing_period_start", "billing_period_end",
                    "billing_period_confidence", "billing_period_evidence"]
        values += ["%(bp_s)s", "%(bp_e)s", "%(bp_c)s",
                   "%(bp_ev)s::billshield.evidence_locators"]
        params.update(bp_s=period["start"], bp_e=period["end"],
                      bp_c=period["confidence"],
                      bp_ev=json.dumps(period["evidence"]))

    cur.execute(f"INSERT INTO billshield.extraction_run ({', '.join(columns)})"
                f" VALUES ({', '.join(values)}) RETURNING id", params)
    run_id = cur.fetchone()[0]

    for charge in rows["charges"]:
        cur.execute(
            "INSERT INTO billshield.charge_candidate (extraction_run_id, position,"
            " label_text, label_confidence, label_evidence, amount,"
            " amount_confidence, amount_evidence, kind, kind_confidence,"
            " cadence_value, cadence_confidence, cadence_evidence,"
            " service_period_start, service_period_end, service_period_confidence,"
            " service_period_evidence)"
            " VALUES (%(run)s, %(pos)s, %(label)s, %(lc)s,"
            " %(le)s::billshield.evidence_locators, %(amount)s, %(ac)s,"
            " %(ae)s::billshield.evidence_locators, %(kind)s, %(kc)s,"
            " %(cad)s, %(cadc)s, %(cade)s::billshield.evidence_locators,"
            " %(sps)s, %(spe)s, %(spc)s,"
            " %(spev)s::billshield.evidence_locators)",
            {"run": str(run_id), "pos": charge["position"],
             "label": charge["label_text"], "lc": charge["label_confidence"],
             "le": json.dumps(charge["label_evidence"]),
             "amount": charge["amount"], "ac": charge["amount_confidence"],
             "ae": json.dumps(charge["amount_evidence"]),
             "kind": charge["kind"], "kc": charge["kind_confidence"],
             "cad": charge["cadence"]["value"],
             "cadc": charge["cadence"]["confidence"],
             "cade": (json.dumps(charge["cadence"]["evidence"])
                      if charge["cadence"]["evidence"] is not None else None),
             "sps": charge["service_period"]["start"],
             "spe": charge["service_period"]["end"],
             "spc": charge["service_period"]["confidence"],
             "spev": (json.dumps(charge["service_period"]["evidence"])
                      if charge["service_period"]["evidence"] is not None else None)})

    for promo in rows["promotions"]:
        cur.execute(
            "INSERT INTO billshield.promotion_candidate (extraction_run_id, position,"
            " charge_position, expiry_date, expiry_confidence, expiry_evidence)"
            " VALUES (%s, %s, %s, %s, %s, %s::billshield.evidence_locators)",
            (str(run_id), promo["position"], promo["charge_position"],
             promo["expiry_date"], promo["expiry_confidence"],
             json.dumps(promo["expiry_evidence"])))
    return run_id


@pytest.fixture
def persisted_run(tenants, rollback_cursor):
    """A rich parser-validated extraction, written to the real schema."""
    from tests.unit.billshield.test_persistence_contract_parity import (
        _extraction_from,
    )
    from tests.unit.billshield.test_persistence_contract_parity import (
        extraction as _extraction_fixture,
    )
    # Build the same payload the parity fixture uses, without pytest indirection.
    extraction = _extraction_fixture.__wrapped__()
    a, _ = tenants
    cur = rollback_cursor
    cur.execute(
        "INSERT INTO billshield.bill (user_id, status, file_sha256, byte_size,"
        " artifact_format, page_count) VALUES (%s, 'needs_review', %s, 4096,"
        " 'pdf_native', 2) RETURNING id",
        (str(a.user_id), uuid.uuid4().hex * 2))
    bill_id = cur.fetchone()[0]
    cur.execute("SELECT file_sha256 FROM billshield.bill WHERE id = %s",
                (str(bill_id),))
    digest = cur.fetchone()[0]
    run_id = _persist_extraction(cur, bill_id, digest, extraction)
    return extraction, run_id, cur, _extraction_from


def test_a_persisted_extraction_survives_the_database_round_trip(persisted_run):
    """Parse -> PostgreSQL -> read back -> rebuild -> identical hash."""
    extraction, run_id, cur, rebuild = persisted_run
    rows, stored_hash = _fetch_extraction(cur, run_id)
    rebuilt = rebuild(rows)
    assert rebuilt.as_canonical() == extraction.as_canonical()
    assert rebuilt.extraction_identity() == extraction.extraction_identity()
    assert stored_hash == extraction.extraction_identity(), (
        "the response_hash column does not match the extraction it describes")


def test_the_database_preserves_charge_and_promotion_order(persisted_run):
    extraction, run_id, cur, _ = persisted_run
    rows, _ = _fetch_extraction(cur, run_id)
    assert [c["position"] for c in rows["charges"]] == list(
        range(len(extraction.charges)))
    assert [c["label_text"] for c in rows["charges"]] == [
        c.label.value for c in extraction.charges]
    assert [p["charge_position"] for p in rows["promotions"]] == [
        p.charge_index for p in extraction.promotions]


def test_the_database_preserves_decimal_scale_and_coordinate_strings(persisted_run):
    extraction, run_id, cur, _ = persisted_run
    rows, _ = _fetch_extraction(cur, run_id)
    assert rows["run"]["amount_due"]["value"] == extraction.amount_due.value
    assert str(rows["run"]["amount_due"]["value"]) == str(
        extraction.amount_due.value), "NUMERIC scale changed through the database"
    locator = rows["run"]["amount_due"]["evidence"][0]
    assert set(locator) == {"page", "x0", "y0", "x1", "y1"}
    assert all(isinstance(locator[k], str) for k in ("x0", "y0", "x1", "y1"))


def test_the_round_trip_fails_if_a_promotion_is_dropped(persisted_run):
    """Non-vacuity, against the LIVE rows rather than an in-memory dict."""
    extraction, run_id, cur, rebuild = persisted_run
    cur.execute(
        "DELETE FROM billshield.promotion_candidate WHERE extraction_run_id = %s"
        " AND position = (SELECT max(position) FROM billshield.promotion_candidate"
        " WHERE extraction_run_id = %s)", (str(run_id), str(run_id)))
    assert cur.rowcount == 1
    rows, _ = _fetch_extraction(cur, run_id)
    assert rebuild(rows).extraction_identity() != extraction.extraction_identity()


def test_the_round_trip_fails_if_a_stored_coordinate_differs(persisted_run, tenants):
    """A different coordinate in the database is a different extraction.

    Persisted as its OWN row rather than as an edit to the first: a terminal run
    is immutable, so at insert time is the only moment such a value could ever
    reach the database. Comparing the rebuilt identity against the original is
    what makes the round-trip test above non-vacuous — if the hash did not cover
    evidence, this would pass unchanged.
    """
    extraction, _, cur, rebuild = persisted_run
    a, _ = tenants
    digest = uuid.uuid4().hex * 2
    cur.execute(
        "INSERT INTO billshield.bill (user_id, status, file_sha256, byte_size,"
        " artifact_format, page_count) VALUES (%s, 'needs_review', %s, 4096,"
        " 'pdf_native', 2) RETURNING id", (str(a.user_id), digest))
    perturbed_bill = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO billshield.extraction_run (bill_id, input_sha256, status,"
        " adapter_code, model_version, extraction_schema_version, currency,"
        " response_hash, completed_at, amount_due_value, amount_due_confidence,"
        " amount_due_evidence)"
        " VALUES (%s, %s, 'succeeded', 'fixture', 'fixture-1.0.0', '1.0.0', 'CAD',"
        " %s, now(), %s, %s, %s::billshield.evidence_locators) RETURNING id",
        (str(perturbed_bill), digest, extraction.extraction_identity(),
         extraction.amount_due.value, extraction.amount_due.confidence,
         '[{"page": 1, "x0": "0.100001", "y0": "0.300000", "x1": "0.900000",'
         ' "y1": "0.400000"}]'))
    perturbed_run = cur.fetchone()[0]
    rows, _ = _fetch_extraction(cur, perturbed_run)
    assert rebuild(rows).extraction_identity() != extraction.extraction_identity(), (
        "a changed evidence coordinate did not change the identity — the hash "
        "is not covering what it claims to cover")


def test_the_round_trip_fails_if_charge_positions_are_reordered(persisted_run, tenants):
    """Charge ORDER, and nothing else, changes the reconstructed identity.

    Document order is semantic — the extraction contract folds it into the
    hash — so a database that returned charges in some other order would
    silently change what an extraction means.

    WHY THIS TEST IS BUILT THE WAY IT IS. An earlier version compared a run
    persisted through the full helper against a run inserted by hand with only
    `amount_due` populated. Those two runs differed in every other bill-level
    field as well, so the identities would have differed even if order were
    ignored entirely — the test could not distinguish "order matters" from
    "these are different extractions".

    So both sides are now complete `BillExtractionV1` values built with
    `dataclasses.replace` from the SAME parsed fixture: identical currency,
    schema version, and all nine bill-level candidates, identical charge
    content, promotions removed from BOTH (a promotion references a charge by
    POSITION, and reordering charges would otherwise silently re-point it), and
    differing only in the sequence of `charges`. Both go through the same
    `_persist_extraction` helper and the same `_fetch_extraction` read path.
    """
    import dataclasses

    extraction, _, cur, rebuild = persisted_run
    a, _ = tenants

    assert len(extraction.charges) >= 2, (
        "the fixture must carry at least two charges for a reorder to mean "
        f"anything; it has {len(extraction.charges)}")

    # Promotions removed from BOTH sides, never one: `charge_index` is a
    # position, so a swap would move what a promotion points at and that
    # difference would confound the one under test.
    control = dataclasses.replace(extraction, promotions=())
    swapped_charges = list(control.charges)
    swapped_charges[0], swapped_charges[-1] = swapped_charges[-1], swapped_charges[0]
    swapped = dataclasses.replace(control, charges=tuple(swapped_charges))

    # The two inputs are identical in every non-charge field ...
    control_fields = {f.name: getattr(control, f.name)
                      for f in dataclasses.fields(control) if f.name != "charges"}
    swapped_fields = {f.name: getattr(swapped, f.name)
                      for f in dataclasses.fields(swapped) if f.name != "charges"}
    assert control_fields == swapped_fields, (
        "the two extractions differ outside `charges`, so any identity "
        "difference would not isolate order")
    # ... hold the same charge VALUES ...
    assert sorted(control.charges, key=repr) == sorted(swapped.charges, key=repr), (
        "the swap changed charge content, not only its sequence")
    # ... and really are in a different sequence.
    assert list(control.charges) != list(swapped.charges), (
        "the swap did not change the sequence")
    assert control.extraction_identity() != swapped.extraction_identity(), (
        "the canonical identity is insensitive to charge order before the "
        "database is involved at all")

    def _persist(value):
        digest = uuid.uuid4().hex * 2
        cur.execute(
            "INSERT INTO billshield.bill (user_id, status, file_sha256, byte_size,"
            " artifact_format, page_count) VALUES (%s, 'needs_review', %s, 4096,"
            " 'pdf_native', 2) RETURNING id", (str(a.user_id), digest))
        bill_id = cur.fetchone()[0]
        return _persist_extraction(cur, bill_id, digest, value)

    control_rows, _ = _fetch_extraction(cur, _persist(control))
    swapped_rows, _ = _fetch_extraction(cur, _persist(swapped))

    assert len(control_rows["charges"]) == len(swapped_rows["charges"]) >= 2, (
        "both runs must persist every charge for the comparison to mean anything")
    assert [c["label_text"] for c in control_rows["charges"]] != \
        [c["label_text"] for c in swapped_rows["charges"]], (
            "the database returned the two runs in the same order, so the swap "
            "did not survive persistence")

    control_read = rebuild(control_rows)
    swapped_read = rebuild(swapped_rows)

    # Each side round-trips to ITS OWN input identity: the database changed
    # neither extraction, so the only surviving difference is the one injected.
    assert control_read.extraction_identity() == control.extraction_identity(), (
        "the control did not survive the database round trip unchanged")
    assert swapped_read.extraction_identity() == swapped.extraction_identity(), (
        "the swapped extraction did not survive the database round trip")

    assert control_read.extraction_identity() != swapped_read.extraction_identity(), (
        "two extractions differing ONLY in charge order produced the same "
        "identity — document order is not part of the hash, or the read path "
        "re-sorted it")
