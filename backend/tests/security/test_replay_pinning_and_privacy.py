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

import uuid

import pytest

from app.services.ioe.domain.integrity import IntegrityReason, IntegrityStatus
from app.services.ioe.replay.verification import IntegrityVerificationService
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

        cur.execute("SELECT count(*), coalesce(string_agg(to_jsonb(c)::text, ' '), '') "
                    "  FROM ioe.integrity_check c "
                    " WHERE c.optimization_run_id = %s", (str(run_id),))
        count, body = cur.fetchone()
        assert count > 0, "no integrity evidence was written; this proves nothing"

        for forbidden, label in ((str(int(INCOME)), "income amount"),
                                 (str(int(EXPENSE)), "expense amount")):
            assert forbidden not in body, (
                f"the {label} reached the integrity check record")

        cur.execute("SELECT coalesce(string_agg(to_jsonb(o)::text, ' '), '') "
                    "  FROM ioe.freshness_outbox o WHERE o.user_id = %s", (str(uid),))
        outbox = cur.fetchone()[0]
        assert str(int(INCOME)) not in outbox, (
            "an income amount reached the freshness outbox")

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
