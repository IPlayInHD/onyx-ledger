"""Entry 11B5H3 — what replay is pinned to, and what it writes down.

Three questions the byte-preservation tests do not answer:

  * §22 — when a replay genuinely cannot run because a pinned executable is not
    the one deployed, does deletion of the live source change that verdict? It
    must stay UNAVAILABLE. Turning it into MISMATCH would accuse a sealed
    result of being irreproducible on the strength of a missing engine.
  * §29, §30, §31 — is the historical calculation still pinned to the rules and
    versions it was sealed against, after the source is gone and after newer
    rules exist? Replay must not quietly re-run history against today's law.
  * §33, §34 — does the evidence replay writes carry anything it should not,
    and does any of this reach an AI provider?

The UNAVAILABLE fixture uses the same governed mechanism the existing
integrity suite uses — monkeypatching `EXECUTABLE_VERSIONS` to a version that
is not running — rather than corrupting a sealed row. Nothing here mutates an
immutable artifact to manufacture an outcome.
"""
from __future__ import annotations

import re
import uuid

import psycopg2
import pytest

from app.services.ioe.domain.integrity import IntegrityReason, IntegrityStatus
from app.services.ioe.replay.verification import IntegrityVerificationService
from tests.conftest import owner_dsn
from tests.security.test_sealed_history_after_purge import (
    EXPENSE,
    INCOME,
    LiveSourceWatch,
    _historical_account,
    _owner,
    _publish,
    _purge_account,
    _remaining,
)


@pytest.fixture(autouse=True)
async def _dispose_engines():
    yield
    from app.database.privacy_session import dispose_all_engines

    await dispose_all_engines()


# ---------------------------------------------------------------------- §22 --
async def test_a_missing_historical_executable_stays_unavailable_after_purge(
    monkeypatch,
):
    """The distinction that matters most to anyone reading an integrity report.

    UNAVAILABLE says "this could not be checked". MISMATCH says "this did not
    reproduce" — an accusation about the sealed result itself. Deleting a
    user's live data must never convert the first into the second.

    So the verdict is measured twice against the SAME unavailable condition:
    once before the purge and once after. Equality is the assertion.
    """
    uid, analysis_id, run_id, scenario_id = await _historical_account()

    monkeypatch.setattr(
        "app.services.ioe.replay.resolver.EXECUTABLE_VERSIONS",
        {"tax_engine_version": (
            "py-99.0.0", IntegrityReason.PINNED_ENGINE_VERSION_UNAVAILABLE)},
    )
    before = await IntegrityVerificationService(uid).verify("optimization", run_id)
    assert before.status is IntegrityStatus.UNAVAILABLE, (
        f"the fixture is not unavailable before the purge ({before.status}); "
        "this test cannot show the classification is preserved")
    assert before.reason_code is IntegrityReason.PINNED_ENGINE_VERSION_UNAVAILABLE

    admin = _owner()
    try:
        cur = admin.cursor()
        _purge_account(cur, uid)
        assert _remaining(cur, uid) == 0, "the purge did not run"

        after = await IntegrityVerificationService(uid).verify("optimization", run_id)

        assert after.status is not IntegrityStatus.MISMATCH, (
            "a replay that could not run was reported as a MISMATCH once the "
            "live source was deleted — deletion is being read as tampering")
        assert after.status is before.status, (
            f"the classification changed from {before.status} to {after.status} "
            "because live source data was deleted")
        assert after.reason_code is before.reason_code, (
            f"the reason changed from {before.reason_code} to {after.reason_code}")

        # And the sealed result is untouched by an unavailable outcome.
        cur.execute("SELECT optimization_result_hash, integrity_status "
                    "  FROM ioe.optimization_run WHERE id = %s", (str(run_id),))
        result_hash, status = cur.fetchone()
        assert result_hash, "the sealed result hash was cleared"
        assert status == "unavailable", f"entity status became {status}"
    finally:
        admin.close()


# ----------------------------------------------------------------- §29, §30 --
async def test_replay_after_purge_stays_pinned_to_the_historical_rules():
    """A newer rule version is published AFTER the purge. The historical
    calculation must not be re-run against it.

    This is the difference between an audit trail and a moving target: if
    today's law could change what last year's sealed result replays to, the
    seal would verify nothing.
    """
    uid, analysis_id, run_id, scenario_id = await _historical_account()

    baseline = await IntegrityVerificationService(uid).verify("optimization", run_id)
    assert baseline.status is IntegrityStatus.VERIFIED, (
        f"the fixture does not verify before the purge ({baseline.status})")

    admin = _owner()
    try:
        cur = admin.cursor()

        def pins():
            cur.execute("""
                SELECT version_manifest::text, manifest_hash,
                       optimization_spec_hash, optimization_result_hash,
                       rule_snapshot_id::text, tax_year,
                       input_execution_policy_version
                  FROM ioe.optimization_run WHERE id = %s
            """, (str(run_id),))
            run = cur.fetchone()
            cur.execute("SELECT rs.snapshot_hash, rs.id::text "
                        "  FROM ioe.run_rule_snapshot rrs "
                        "  JOIN ioe.rule_snapshot rs ON rs.id = rrs.snapshot_id "
                        " WHERE rrs.run_id = %s", (str(run_id),))
            return run, cur.fetchall()

        pinned_before = pins()

        _purge_account(cur, uid)
        assert _remaining(cur, uid) == 0

        # Newer law arrives after the account's data is gone.
        tag = uuid.uuid4().hex[:8].upper()
        await _publish(f"NEW{tag}", "INCREASE_RRSP_DEDUCTION", "9999", "RRSP_ROOM")

        with LiveSourceWatch() as watch:
            after = await IntegrityVerificationService(uid).verify(
                "optimization", run_id)

        assert after.status is IntegrityStatus.VERIFIED, (
            f"publishing a newer rule changed the historical verdict to "
            f"{after.status}/{after.reason_code} — replay is not pinned")
        assert watch.touched() == [], (
            f"replay read live source tables {watch.touched()}")
        assert pins() == pinned_before, (
            "a version pin or rule snapshot moved after a newer rule was "
            "published — history was re-run against today's law")
    finally:
        admin.close()


# ---------------------------------------------------------------------------
# Field-aware payload privacy
# ---------------------------------------------------------------------------
# A financial amount is a short decimal. `EXPENSE` is 1750 — four digits, every
# one of them a valid hex character — so substring-searching a whole serialized
# row for it matches SHA-256 digests, UUIDs and timestamp fractions by chance.
# Measured on a database this suite had run against ten times: 3 of 590
# integrity_check rows contained "1750" and all three were coincidences (two
# inside a digest or UUID, one inside "...48.817504+00:00").
#
# So each persisted field is classified, and only the fields that can carry
# user-derived content are scanned. The classification is not a list of
# excuses: every OPAQUE_STRUCTURAL entry names the EVIDENCE that makes it
# opaque, and that evidence is checked against the live database below. A
# column is only allowed to be opaque because PostgreSQL says it is a uuid, a
# timestamp, a counter, or a value constrained to a closed set — or, for the
# digests, because every stored value is 64 hex characters.
CONTENT_CAPABLE = "CONTENT_CAPABLE"
OPAQUE_STRUCTURAL = "OPAQUE_STRUCTURAL"

#: column -> (classification, evidence kind). Evidence kinds are verified.
_INTEGRITY_FIELDS: dict[str, tuple[str, str]] = {
    "id": (OPAQUE_STRUCTURAL, "uuid"),
    "user_id": (OPAQUE_STRUCTURAL, "uuid"),
    "optimization_run_id": (OPAQUE_STRUCTURAL, "uuid"),
    "portfolio_id": (OPAQUE_STRUCTURAL, "uuid"),
    "scenario_id": (OPAQUE_STRUCTURAL, "uuid"),
    "operational_event_id": (OPAQUE_STRUCTURAL, "uuid"),
    "started_at": (OPAQUE_STRUCTURAL, "timestamp"),
    "completed_at": (OPAQUE_STRUCTURAL, "timestamp"),
    "created_at": (OPAQUE_STRUCTURAL, "timestamp"),
    "claim_expires_at": (OPAQUE_STRUCTURAL, "timestamp"),
    "engine_runs_used": (OPAQUE_STRUCTURAL, "counter"),
    "duration_ms": (OPAQUE_STRUCTURAL, "counter"),
    "entity_type": (OPAQUE_STRUCTURAL, "closed-code"),
    "status": (OPAQUE_STRUCTURAL, "closed-code"),
    "reason_code": (OPAQUE_STRUCTURAL, "closed-code"),
    "expected_spec_hash": (OPAQUE_STRUCTURAL, "digest"),
    "expected_result_hash": (OPAQUE_STRUCTURAL, "digest"),
    "actual_result_hash": (OPAQUE_STRUCTURAL, "digest"),
    # Free text as far as the schema is concerned. Written by code today, but
    # nothing stops a value reaching them, so they are scanned.
    "verifier_version": (CONTENT_CAPABLE, "free-text"),
    "canonical_serialization_version": (CONTENT_CAPABLE, "free-text"),
    "integrity_check_policy_version": (CONTENT_CAPABLE, "free-text"),
    "claimed_by": (CONTENT_CAPABLE, "free-text"),
}

_OUTBOX_FIELDS: dict[str, tuple[str, str]] = {
    "id": (OPAQUE_STRUCTURAL, "uuid"),
    "user_id": (OPAQUE_STRUCTURAL, "uuid"),
    "analysis_id": (OPAQUE_STRUCTURAL, "uuid"),
    "claim_token": (OPAQUE_STRUCTURAL, "uuid"),
    "claimed_at": (OPAQUE_STRUCTURAL, "timestamp"),
    "processed_at": (OPAQUE_STRUCTURAL, "timestamp"),
    "created_at": (OPAQUE_STRUCTURAL, "timestamp"),
    "attempts": (OPAQUE_STRUCTURAL, "counter"),
    "event_type": (OPAQUE_STRUCTURAL, "closed-code"),
    "claim_state": (OPAQUE_STRUCTURAL, "closed-code"),
    # NOT closed by the schema — `stale_reason_code` and `last_error_code` have
    # no CHECK constraint, so "code" is a convention rather than a guarantee.
    # `tax_year` is user-derived, not a counter. `dedupe_key` is free text and
    # is exactly where a careless producer would put something identifying.
    "stale_reason_code": (CONTENT_CAPABLE, "free-text"),
    "last_error_code": (CONTENT_CAPABLE, "free-text"),
    "dedupe_key": (CONTENT_CAPABLE, "free-text"),
    "claimed_by": (CONTENT_CAPABLE, "free-text"),
    "jurisdiction": (CONTENT_CAPABLE, "free-text"),
    "tax_year": (CONTENT_CAPABLE, "user-derived"),
}

_TABLES = {
    "integrity_check": _INTEGRITY_FIELDS,
    "freshness_outbox": _OUTBOX_FIELDS,
}


def _live_columns(cur, table: str) -> dict[str, str]:
    cur.execute("SELECT column_name, data_type FROM information_schema.columns "
                " WHERE table_schema = 'ioe' AND table_name = %s", (table,))
    return dict(cur.fetchall())


def _opaque(fields: dict[str, tuple[str, str]]) -> list[str]:
    return [c for c, (kind, _) in fields.items() if kind is OPAQUE_STRUCTURAL]


def _content_capable_text(cur, table: str, where: str, params: tuple) -> str:
    """Serialize ONLY the content-capable fields of the matching rows.

    Subtracting the opaque set rather than selecting the known content-capable
    one is deliberate: a column added tomorrow stays in the result and is
    scanned, so the scan fails safe while
    `test_every_persisted_field_is_classified` fails loudly.
    """
    cur.execute(
        f"SELECT coalesce(string_agg((to_jsonb(t) - %s::text[])::text, ' '), '') "  # noqa: S608
        f"  FROM ioe.{table} t WHERE {where}",
        (_opaque(_TABLES[table]), *params))
    return cur.fetchone()[0]


# ---------------------------------------------------------------------- §33 --
async def test_the_evidence_replay_writes_carries_no_financial_value():
    """Integrity evidence outlives the data it describes, so what it records
    matters. Identifiers, hashes, versions and closed codes are policy; amounts
    and profile values are not."""
    uid, analysis_id, run_id, scenario_id = await _historical_account()

    admin = _owner()
    try:
        cur = admin.cursor()
        _purge_account(cur, uid)
        result = await IntegrityVerificationService(uid).verify("optimization", run_id)
        assert result.status is IntegrityStatus.VERIFIED

        # Only the fields that can carry user-derived content are scanned.
        # See the classification tables above for why each of the others is
        # opaque, and `test_the_opaque_classification_matches_the_database`
        # for the evidence being checked rather than assumed.
        cur.execute("SELECT count(*) FROM ioe.integrity_check "
                    " WHERE optimization_run_id = %s", (str(run_id),))
        assert cur.fetchone()[0] > 0, (
            "no integrity evidence was written; this proves nothing")

        body = _content_capable_text(
            cur, "integrity_check", "t.optimization_run_id = %s", (str(run_id),))
        for forbidden, label in ((str(int(INCOME)), "income amount"),
                                 (str(int(EXPENSE)), "expense amount")):
            assert forbidden not in body, (
                f"the {label} reached a content-capable integrity check field")

        outbox = _content_capable_text(
            cur, "freshness_outbox", "t.user_id = %s", (str(uid),))
        assert str(int(INCOME)) not in outbox, (
            "an income amount reached a content-capable freshness outbox field")

        # The account's email must not travel into integrity evidence either.
        cur.execute("SELECT email FROM identity.user_account WHERE id = %s",
                    (str(uid),))
        row = cur.fetchone()
        if row:                       # the account row survives SOURCE_DATA
            assert row[0] not in body, "an email reached the integrity evidence"
    finally:
        admin.close()


# ---------------------------------------------------------------------- §34 --
def test_the_replay_path_reaches_no_ai_provider():
    """Replay is a deterministic recomputation. An explanation layer in this
    path would make a verification result depend on a remote model, which is
    the opposite of what a seal is for.

    Asserted from the code, since the honest claim is about what the module
    can reach, not about what one execution happened to call.
    """
    from pathlib import Path

    replay = Path(__file__).resolve().parents[2] / "app" / "services" / "ioe" / "replay"
    sources = {p.name: p.read_text() for p in replay.glob("*.py")}
    assert sources, "the replay package was not found"

    forbidden = ("anthropic", "openai", "langchain", "litellm", "httpx.AsyncClient",
                 "requests.post", "ai_provider", "llm")
    for name, text in sources.items():
        lowered = text.lower()
        for token in forbidden:
            assert token.lower() not in lowered, (
                f"{name} references {token!r}; replay must stay deterministic "
                "and local")


# ---------------------------------------------------------------------------
# The classification itself is under test
# ---------------------------------------------------------------------------
def test_every_persisted_field_is_classified():
    """FAIL CLOSED ON SCHEMA EVOLUTION.

    A column added tomorrow — say `raw_detail` or `amount_cents` — must not
    slip past the privacy scan merely because nobody updated a list. The scan
    itself fails safe (it subtracts the opaque set, so an unknown column is
    still scanned); this test is the loud half, so the classification is
    updated deliberately rather than discovered later.
    """
    admin = _owner()
    try:
        cur = admin.cursor()
        for table, fields in _TABLES.items():
            live = set(_live_columns(cur, table))
            classified = set(fields)
            assert live == classified, (
                f"ioe.{table}: the schema and the privacy classification "
                f"disagree.\n  unclassified (add them, do not assume safe): "
                f"{sorted(live - classified)}\n  classified but gone: "
                f"{sorted(classified - live)}")
    finally:
        admin.close()


def test_the_opaque_classification_matches_the_database():
    """Every OPAQUE_STRUCTURAL field names its evidence; here the evidence is
    checked against PostgreSQL rather than taken on trust. Without this, the
    classification would just be a list of fields somebody wanted skipped."""
    admin = _owner()
    try:
        cur = admin.cursor()
        for table, fields in _TABLES.items():
            types = _live_columns(cur, table)
            cur.execute(
                "SELECT coalesce(string_agg(pg_get_constraintdef(c.oid), ' '), '') "
                "  FROM pg_constraint c JOIN pg_class r ON r.oid = c.conrelid "
                "  JOIN pg_namespace n ON n.oid = r.relnamespace "
                " WHERE n.nspname = 'ioe' AND r.relname = %s AND c.contype = 'c'",
                (table,))
            checks = cur.fetchone()[0]

            for column, (kind, evidence) in fields.items():
                if kind is CONTENT_CAPABLE:
                    continue
                declared = types[column]
                if evidence == "uuid":
                    assert declared == "uuid", (
                        f"{table}.{column} is classified opaque as a uuid but "
                        f"is {declared}")
                elif evidence == "timestamp":
                    assert declared.startswith("timestamp"), (
                        f"{table}.{column} is classified opaque as a timestamp "
                        f"but is {declared}")
                elif evidence == "counter":
                    assert declared in ("integer", "smallint", "bigint"), (
                        f"{table}.{column} is classified opaque as a counter "
                        f"but is {declared} — a numeric type wide enough for "
                        "an amount is not a counter")
                elif evidence == "closed-code":
                    assert f"({column} = ANY (ARRAY[" in checks, (
                        f"{table}.{column} is classified opaque as a closed "
                        "code, but no CHECK constraint restricts it to a set "
                        "of literals — so it can hold arbitrary text")
                elif evidence == "digest":
                    cur.execute(
                        f"SELECT count(*) FROM ioe.{table} "  # noqa: S608
                        f" WHERE {column} IS NOT NULL "
                        f"   AND {column} !~ '^[0-9a-f]{{64}}$'")
                    assert cur.fetchone()[0] == 0, (
                        f"{table}.{column} is classified opaque as a digest "
                        "but holds values that are not 64 hex characters")
                else:
                    raise AssertionError(
                        f"{table}.{column}: unknown evidence kind {evidence!r}")
    finally:
        admin.close()


def test_the_scan_catches_a_planted_amount_and_ignores_an_opaque_coincidence():
    """GUARD ON THE GUARD, both directions.

    A scan that skips fields is only worth having if it still fails when a
    value lands in a field it does read — and only worth trusting if it does
    NOT fail when the same digits appear inside a digest or a timestamp, which
    is the false alarm this whole classification exists to remove.

    The probe row is INSERTED rather than an existing record updated: a
    finished integrity check is immutable
    (`guard_integrity_check_transition` rejects verified -> verified), and that
    immutability is a property worth keeping rather than working around.
    Everything runs in one transaction that is rolled back.
    """
    sentinel = str(int(EXPENSE))
    admin = psycopg2.connect(owner_dsn())        # NOT autocommit: we roll back
    try:
        cur = admin.cursor()
        cur.execute("SELECT user_id, optimization_run_id FROM ioe.integrity_check "
                    " WHERE optimization_run_id IS NOT NULL LIMIT 1")
        row = cur.fetchone()
        if row is None:
            pytest.skip("no integrity evidence on this database yet")
        user_id, run_id = row
        probe = uuid.uuid4()

        clean = "b" * 64

        def insert(claimed_by: str, digest: str, expires: str) -> None:
            cur.execute(
                "INSERT INTO ioe.integrity_check "
                "  (id, user_id, entity_type, optimization_run_id, status, "
                "   reason_code, claimed_by, expected_spec_hash, "
                "   expected_result_hash, actual_result_hash, "
                "   verifier_version, canonical_serialization_version, "
                "   integrity_check_policy_version, "
                "   claim_expires_at, started_at) "
                # A probe-only verifier_version: ux_ioe_integrity_check_active_run
                # is unique on (optimization_run_id, verifier_version), and the
                # chosen run may already have an active check of its own.
                "VALUES (%s, %s, 'optimization', %s, 'running', 'NONE', %s, "
                "        %s, %s, %s, %s, '1.1.0', '1.0.0', %s, now())",
                (str(probe), str(user_id), str(run_id), claimed_by,
                 clean, clean, digest, f"probe-{probe}", expires))

        def scan() -> str:
            return _content_capable_text(
                cur, "integrity_check", "t.id = %s", (str(probe),))

        # 1. A readable field. `claimed_by` is free text and IS scanned.
        insert(f"worker-{sentinel}", clean, "2030-01-01 00:00:00+00")
        assert sentinel in scan(), (
            "an amount planted in a content-capable field was NOT detected — "
            "the classification is skipping a field that can carry content")
        cur.execute("ROLLBACK")

        # 2. The same digits inside opaque fields only. This must NOT read as
        #    a leak: it is exactly the coincidence measured on the real
        #    database — inside a digest, and inside a timestamp's microseconds.
        insert("integrity-worker",
               sentinel + "a" * (64 - len(sentinel)),
               "2026-08-10 23:39:48.817504+00")
        planted = scan()
        assert sentinel not in planted, (
            "digits inside a digest and a timestamp were reported as a "
            f"financial leak — the false alarm this removes: {planted!r}")
        cur.execute("ROLLBACK")
    finally:
        admin.close()


def test_the_evidence_schema_carries_no_source_value_columns():
    """Keys, not just values. A scan over content proves nothing about a
    column that should not exist at all — an `amount`, a `province_code`, an
    `email` on the integrity record would be a design defect whether or not
    any row happened to have one populated today."""
    forbidden = re.compile(
        r"amount|income|expense|balance|salary|revenue|profit|"
        r"email|phone|sin\b|address|postal|"
        r"filename|file_name|document_text|extracted|ocr|"
        r"first_name|last_name|birth|marital|dependent",
        re.IGNORECASE)
    admin = _owner()
    try:
        cur = admin.cursor()
        for table in _TABLES:
            offenders = [c for c in _live_columns(cur, table) if forbidden.search(c)]
            assert not offenders, (
                f"ioe.{table} has columns shaped like source data: {offenders}")
    finally:
        admin.close()
