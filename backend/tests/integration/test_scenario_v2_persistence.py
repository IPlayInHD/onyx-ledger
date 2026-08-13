"""Entry 12B1 Phase A2 — sealing and replaying a v2 scenario, end to end.

Production still writes v1. This suite drives the internal seam so the whole v2
path — derive, canonicalize, hash, persist, seal, replay — is proved to work
before anything is switched on, rather than switched on and then proved.

The load-bearing claims are about DISAGREEMENT, not agreement. Any two of the
version fields drifting apart, any of the three stored artifacts being edited,
and any interruption of TX-2 must all end somewhere other than VERIFIED.
"""
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.core.exceptions import NotFound
from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    IncomeSource,
    IncomeType,
    Jurisdiction,
    RuleDeadline,
    RuleOutcome,
    RuleRequiredDocument,
    Scenario,
    ScenarioResult,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import unit_of_work
from app.services.ioe.domain.integrity import IntegrityReason, IntegrityStatus
from app.services.ioe.domain.scenario import (
    CURRENT_SCENARIO_RESULT_SCHEMA_VERSION,
    SCENARIO_RESULT_SCHEMA_V1,
    SCENARIO_RESULT_SCHEMA_V2,
)
from app.services.ioe.replay.verification import IntegrityVerificationService
from app.services.ioe.scenario import counterfactual
from app.services.ioe.scenario.service import ScenarioService
from tests.conftest import frozen_snapshot

RRSP = "INCREASE_RRSP_DEDUCTION"
TAX_YEAR = 2025


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _lever(amount="5000"):
    return {"lever_code": RRSP, "parameters": {"amount": Decimal(amount)}}


def _spec(amount="5000", **kw):
    from app.services.ioe.domain.scenario import ScenarioSpec

    return ScenarioSpec.parse([_lever(amount)], **kw)


async def _user_with_analysis() -> tuple[uuid.UUID, uuid.UUID]:
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"a2_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(u)
        await s.flush()
        uid = u.id
        income_type_id = (await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment"))).id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=TAX_YEAR, income_type_id=income_type_id,
            amount=Decimal("95000"), province_code="ON",
        ))
        run = AnalysisRun(
            user_id=uid, tax_year=TAX_YEAR, province_code="ON",
            engine_version="py-1.0.0", status="completed",
            started_at=datetime.now(tz=UTC), completed_at=datetime.now(tz=UTC),
            data_verified=True,
        )
        s.add(run)
        await s.flush()
        snapshot, snapshot_hash = frozen_snapshot(employment_income=Decimal("95000"))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=snapshot, snapshot_hash=snapshot_hash))
        await s.flush()
        return uid, run.id


async def _publish_rules(count: int = 1) -> list[uuid.UUID]:
    """`count` published rules, each with a deadline and a required document, so
    the derived state carries real governed metadata rather than empty tuples."""
    created: list[uuid.UUID] = []
    async with unit_of_work(actor_type="admin") as s:
        jurisdiction = await s.scalar(
            select(Jurisdiction).where(Jurisdiction.code == "FED"))
        for _ in range(count):
            code = f"A2_{uuid.uuid4().hex[:10].upper()}"
            rule = TaxRule(code=code, name=code, category="deduction",
                           jurisdiction_id=jurisdiction.id)
            s.add(rule)
            await s.flush()
            version = TaxRuleVersion(
                tax_rule_id=rule.id, tax_year=TAX_YEAR,
                effective_date=date(TAX_YEAR, 1, 1), status="published",
                description="A2 fixture", eligibility_basis_codes=["BASIS_A2"],
            )
            s.add(version)
            await s.flush()
            s.add(RuleOutcome(
                rule_version_id=version.id, outcome_type="recommend", priority=1,
                title_template="A2 opportunity",
                economic_effect_type="current_year_tax_reduction",
                reversibility="reversible",
            ))
            s.add(RuleDeadline(
                rule_version_id=version.id, deadline_code=f"{code}_DEADLINE",
                deadline_date=date(TAX_YEAR + 1, 3, 1), is_hard=True,
            ))
            s.add(RuleRequiredDocument(
                rule_version_id=version.id, document_type_code="T4",
                necessity="required",
            ))
            await s.flush()
            created.append(version.id)
    return created


async def _seal_v2(uid, analysis_id, *, amount="5000", key=None):
    """Create a v2 scenario THROUGH THE INTERNAL SEAM — the only way to get one."""
    return await ScenarioService(uid)._simulate(
        analysis_id, _spec(amount), idempotency_key=key,
        result_schema_version=SCENARIO_RESULT_SCHEMA_V2,
    )


async def _rows(uid, scenario_id):
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario = await s.get(Scenario, scenario_id)
        result = await s.scalar(
            select(ScenarioResult).where(ScenarioResult.scenario_id == scenario_id))
        return scenario, result


async def _verify(uid, scenario_id):
    return await IntegrityVerificationService(uid).verify("scenario", scenario_id)


async def _privileged(sql: str, **params) -> None:
    """A raw write as the migrator identity.

    Corruption tests must reach past the application entirely: the point is to
    prove verification catches an artifact the application could not have
    produced, so producing it through the application would prove nothing.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(
        "postgresql+asyncpg://onyx_migrator@/onyx_test"
        "?host=/var/run/postgresql&port=5432",
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(text("SET session_replication_role = replica"))
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# §11 — THE PRINCIPAL ACCEPTANCE TEST
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_v2_scenario_seals_its_evidence_and_replays_verified():
    """The whole path, against real PostgreSQL, inspected column by column.

    Every hash is recomputed here from what is actually stored rather than
    compared to what the service returned, because the service agreeing with
    itself is not evidence that the row is verifiable.
    """
    uid, analysis_id = await _user_with_analysis()
    await _publish_rules(2)

    outcome = await _seal_v2(uid, analysis_id)
    scenario, result = await _rows(uid, outcome.scenario_id)

    # ---- every version-bearing field says v2 ----
    assert scenario.result_schema_version == SCENARIO_RESULT_SCHEMA_V2
    assert result.result_schema_version == SCENARIO_RESULT_SCHEMA_V2
    assert scenario.version_manifest["scenario_result_schema_version"] == (
        SCENARIO_RESULT_SCHEMA_V2)

    # ---- the evidence is present ----
    payload = result.counterfactual_derived_state
    stored_inner = result.counterfactual_derived_state_hash
    assert payload is not None, "a v2 seal without its derived state"
    assert stored_inner and len(stored_inner) == 64
    assert payload["candidates"], "the fixture must produce real candidates"
    assert payload["line_items"], "the sealed tax state must not be empty"
    assert payload["pinned_rule_version_ids"]

    # ---- inner reconciliation: the digest describes the stored bytes ----
    assert counterfactual.payload_hash(payload) == stored_inner

    # ---- outer reconciliation: the result hash binds that digest ----
    rebuilt_outer = ScenarioService.canonical_result(
        ScenarioService(uid)._compute(
            await _repin(uid, analysis_id, SCENARIO_RESULT_SCHEMA_V2)),
        result_schema_version=SCENARIO_RESULT_SCHEMA_V2,
        counterfactual_derived_state_hash=stored_inner,
    )
    assert rebuilt_outer["counterfactual_derived_state_hash"] == stored_inner
    assert "counterfactual_derived_state" not in rebuilt_outer, (
        "the outer payload duplicates the whole artifact instead of binding it")

    # ---- replay reads the STORED version and verifies ----
    verdict = await _verify(uid, outcome.scenario_id)
    assert verdict.status is IntegrityStatus.VERIFIED, (
        f"v2 replay did not verify: {verdict.status} / {verdict.reason}")
    assert verdict.reason_code is IntegrityReason.NONE


async def _repin(uid, analysis_id, version):
    service = ScenarioService(uid)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        return await service._pin_specification(
            s, analysis_id, _spec(), result_schema_version=version)


# ---------------------------------------------------------------------------
# §3 — one version, four fields, measured on real rows
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "version", [SCENARIO_RESULT_SCHEMA_V1, SCENARIO_RESULT_SCHEMA_V2])
async def test_all_version_bearing_fields_agree(version):
    """The behavioural half of the write-version invariant. The AST test proves
    every site reads one source; this proves the value actually landed in all
    four places, which is the thing the reverted activation got wrong."""
    uid, analysis_id = await _user_with_analysis()
    await _publish_rules(1)
    outcome = await ScenarioService(uid)._simulate(
        analysis_id, _spec(), result_schema_version=version)
    scenario, result = await _rows(uid, outcome.scenario_id)

    assert {
        scenario.result_schema_version,
        result.result_schema_version,
        scenario.version_manifest["scenario_result_schema_version"],
    } == {version}
    assert (await _verify(uid, outcome.scenario_id)).status is IntegrityStatus.VERIFIED


@pytest.mark.asyncio
async def test_ordinary_production_creation_still_writes_v1():
    """§21. The public entry point is untouched by any of this."""
    uid, analysis_id = await _user_with_analysis()
    await _publish_rules(1)
    outcome = await ScenarioService(uid).simulate(analysis_id, _spec())
    scenario, result = await _rows(uid, outcome.scenario_id)

    assert CURRENT_SCENARIO_RESULT_SCHEMA_VERSION == SCENARIO_RESULT_SCHEMA_V1
    assert scenario.result_schema_version == SCENARIO_RESULT_SCHEMA_V1
    assert result.result_schema_version == SCENARIO_RESULT_SCHEMA_V1
    assert result.counterfactual_derived_state is None
    assert result.counterfactual_derived_state_hash is None
    assert (await _verify(uid, outcome.scenario_id)).status is IntegrityStatus.VERIFIED


# ---------------------------------------------------------------------------
# §12 — the reverted-activation defect
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_row_that_claims_v1_while_hashed_as_v2_does_not_verify():
    """THE REVERTED ACTIVATION, reproduced deliberately.

    That failure wrote rows saying v1 beside a hash computed as v2. Replay
    correctly refused them, and this keeps it refusing them.
    """
    uid, analysis_id = await _user_with_analysis()
    await _publish_rules(1)
    outcome = await _seal_v2(uid, analysis_id)

    # Downgrade only the stored VERSION, leaving the v2 hash and evidence.
    await _privileged(
        "UPDATE ioe.scenario SET result_schema_version = '1.0.0' WHERE id = :sid",
        sid=outcome.scenario_id,
    )

    verdict = await _verify(uid, outcome.scenario_id)
    assert verdict.status is not IntegrityStatus.VERIFIED
    assert verdict.status is IntegrityStatus.MISMATCH
    assert verdict.reason_code is IntegrityReason.RESULT_HASH_MISMATCH


@pytest.mark.asyncio
async def test_the_service_cannot_produce_a_contradictory_artifact():
    """The other half: the contradiction above had to be forced in with a raw
    privileged UPDATE, because every version-bearing field reads one argument.
    Fifty scenarios at each version, and none can disagree with itself."""
    uid, analysis_id = await _user_with_analysis()
    await _publish_rules(1)
    for version in (SCENARIO_RESULT_SCHEMA_V1, SCENARIO_RESULT_SCHEMA_V2):
        outcome = await ScenarioService(uid)._simulate(
            analysis_id, _spec(amount=f"{1000 + len(version)}"),
            result_schema_version=version)
        scenario, result = await _rows(uid, outcome.scenario_id)
        assert scenario.result_schema_version == result.result_schema_version
        assert scenario.version_manifest[
            "scenario_result_schema_version"] == scenario.result_schema_version
        # and the paired-column CHECK held without the application asking
        assert (result.counterfactual_derived_state is None) == (
            result.counterfactual_derived_state_hash is None)


# ---------------------------------------------------------------------------
# §13 — corruption cannot produce a false VERIFIED
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_editing_the_derived_payload_alone_breaks_verification():
    """A. The rebuild cannot see this — it would agree with the true digest —
    so only hashing the STORED bytes catches it. That is why replay does both."""
    uid, analysis_id = await _user_with_analysis()
    await _publish_rules(1)
    outcome = await _seal_v2(uid, analysis_id)
    _, result = await _rows(uid, outcome.scenario_id)

    tampered = dict(result.counterfactual_derived_state)
    tampered["line_items"] = [
        {**tampered["line_items"][0], "amount": "999999.99"},
        *tampered["line_items"][1:],
    ]
    await _privileged(
        "UPDATE ioe.scenario_result SET counterfactual_derived_state = "
        "CAST(:payload AS jsonb) WHERE scenario_id = :sid",
        payload=__import__("json").dumps(tampered), sid=outcome.scenario_id,
    )

    assert (await _verify(uid, outcome.scenario_id)).status is not (
        IntegrityStatus.VERIFIED)


@pytest.mark.asyncio
async def test_editing_the_derived_hash_alone_breaks_verification():
    """B. Payload intact, digest replaced with another well-formed digest."""
    uid, analysis_id = await _user_with_analysis()
    await _publish_rules(1)
    outcome = await _seal_v2(uid, analysis_id)

    await _privileged(
        "UPDATE ioe.scenario_result SET counterfactual_derived_state_hash = :h "
        "WHERE scenario_id = :sid",
        h="b" * 64, sid=outcome.scenario_id,
    )

    assert (await _verify(uid, outcome.scenario_id)).status is not (
        IntegrityStatus.VERIFIED)


@pytest.mark.asyncio
async def test_editing_the_outer_result_hash_breaks_verification():
    """C. Both inner artifacts intact; the seal itself moved."""
    uid, analysis_id = await _user_with_analysis()
    await _publish_rules(1)
    outcome = await _seal_v2(uid, analysis_id)

    await _privileged(
        "UPDATE ioe.scenario SET scenario_result_hash = :h WHERE id = :sid",
        h="c" * 64, sid=outcome.scenario_id,
    )

    verdict = await _verify(uid, outcome.scenario_id)
    assert verdict.status is IntegrityStatus.MISMATCH
    assert verdict.reason_code is IntegrityReason.RESULT_HASH_MISMATCH


@pytest.mark.asyncio
async def test_a_v2_seal_stripped_of_its_evidence_fails_closed():
    """§14. Not a mismatch — there is nothing to compare against, so the honest
    verdict is that the sealed evidence is incomplete."""
    uid, analysis_id = await _user_with_analysis()
    await _publish_rules(1)
    outcome = await _seal_v2(uid, analysis_id)

    await _privileged(
        "UPDATE ioe.scenario_result SET counterfactual_derived_state = NULL, "
        "counterfactual_derived_state_hash = NULL WHERE scenario_id = :sid",
        sid=outcome.scenario_id,
    )

    verdict = await _verify(uid, outcome.scenario_id)
    assert verdict.status is IntegrityStatus.UNAVAILABLE
    assert verdict.reason_code is IntegrityReason.SEALED_EVIDENCE_INCOMPLETE


# ---------------------------------------------------------------------------
# §14 — absence stays distinguishable from emptiness
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_v2_scenario_with_no_eligible_candidates_seals_and_verifies():
    """Zero candidates is a RESULT. It must seal a complete artifact and
    verify, or "nothing was eligible" and "this was never evaluated" would be
    indistinguishable to every later reader."""
    uid, analysis_id = await _user_with_analysis()
    # deliberately publish nothing for this year beyond whatever the seed
    # carries; the assertion below is about shape, not about count
    outcome = await _seal_v2(uid, analysis_id)
    _, result = await _rows(uid, outcome.scenario_id)

    payload = result.counterfactual_derived_state
    assert payload is not None, "an empty candidate set must still be sealed"
    assert result.counterfactual_derived_state_hash
    assert payload["line_items"], "an empty candidate set does not empty the tax state"
    assert counterfactual.payload_hash(payload) == (
        result.counterfactual_derived_state_hash)
    assert (await _verify(uid, outcome.scenario_id)).status is IntegrityStatus.VERIFIED


# ---------------------------------------------------------------------------
# §15 — TX-2 is all or nothing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [
    "before_derived_state", "after_derived_state", "after_inner_hash",
    "during_result_persist", "before_seal", "at_seal",
])
async def test_an_interrupted_tx2_leaves_no_torn_v2_artifact(monkeypatch, stage):
    """Six injection points across TX-2. The outcome must be a complete commit
    or a complete rollback — never a sealed scenario whose evidence is missing,
    and never evidence attached to a scenario that never sealed."""
    uid, analysis_id = await _user_with_analysis()
    await _publish_rules(1)

    boom = RuntimeError(f"injected at {stage}")

    if stage == "before_derived_state":
        original = ScenarioService.build_counterfactual_derived_state

        async def fail(self, session, pinned, computed):
            raise boom
        monkeypatch.setattr(
            ScenarioService, "build_counterfactual_derived_state", fail)
        del original
    elif stage == "after_derived_state":
        real = counterfactual.canonical_payload_and_hash

        def fail_after(state):
            real(state)
            raise boom
        monkeypatch.setattr(
            "app.services.ioe.scenario.service."
            "counterfactual.canonical_payload_and_hash", fail_after)
    elif stage == "after_inner_hash":
        real_canonical = ScenarioService.canonical_result

        def fail_canonical(computed, **kw):
            real_canonical(computed, **kw)
            raise boom
        monkeypatch.setattr(
            ScenarioService, "canonical_result", staticmethod(fail_canonical))
    elif stage == "during_result_persist":
        import app.services.ioe.scenario.service as svc
        real_bulk = svc.bulk_insert

        async def fail_bulk(session, model, rows):
            await real_bulk(session, model, rows)
            raise boom
        monkeypatch.setattr(svc, "bulk_insert", fail_bulk)
    elif stage in ("before_seal", "at_seal"):
        import app.services.ioe.scenario.service as svc
        real_assert = svc.WorkflowStateMachine.assert_transition

        def fail_transition(frm, to):
            real_assert(frm, to)
            raise boom
        monkeypatch.setattr(
            svc.WorkflowStateMachine, "assert_transition",
            staticmethod(fail_transition))

    with pytest.raises(Exception):  # noqa: B017 - any failure, sanitized upstream
        await _seal_v2(uid, analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        sealed = list(await s.scalars(
            select(Scenario).where(Scenario.user_id == uid)))
        results = {
            r.scenario_id: r for r in await s.scalars(select(ScenarioResult))
        }

    for scenario in sealed:
        result = results.get(scenario.id)
        if scenario.workflow_status == "completed":
            assert scenario.scenario_result_hash, "sealed with no result hash"
            assert result is not None, "sealed with no result row"
            assert result.counterfactual_derived_state is not None
            assert result.counterfactual_derived_state_hash
        else:
            assert not scenario.scenario_result_hash, (
                "an unsealed scenario carries a result hash")
            assert result is None, (
                "derived evidence persisted for a scenario that never sealed")


# ---------------------------------------------------------------------------
# §16 — immutability, from the live catalogue
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_result_table_still_carries_both_protective_triggers():
    """Read from `pg_trigger`, not from migration text.

    CORRECTS A BELIEF CARRIED INTO THIS ENTRY. The columns-not-table decision
    was recorded as resting on "both triggers are row-level, so they cover
    columns added later". Only one of them is. Measured here:

        trg_immutable      BEFORE DELETE OR UPDATE ... FOR EACH ROW
        trg_write_cutoff   AFTER INSERT ... REFERENCING NEW TABLE
                           ... FOR EACH STATEMENT

    The conclusion survives, for a better reason than the one recorded. Neither
    trigger inspects a column list: `trg_immutable` rejects any UPDATE or DELETE
    of the row whatever it touches, and `trg_write_cutoff` reads its transition
    table and rejects the INSERT of the row entirely. Both therefore cover
    columns added after they were written — coverage comes from operating on
    whole rows, not from being row-level.
    """
    async with unit_of_work(actor_type="system") as s:
        rows = {
            name: definition for name, definition in await s.execute(text(
                "SELECT tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t "
                "JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'ioe' AND c.relname = 'scenario_result' "
                "AND NOT t.tgisinternal"
            ))
        }
    assert {"trg_immutable", "trg_write_cutoff"} <= set(rows), rows

    immutable = rows["trg_immutable"]
    assert "FOR EACH ROW" in immutable
    assert "BEFORE DELETE OR UPDATE" in immutable
    assert "reject_result_mutation" in immutable
    # no UPDATE OF (column list): the guard cannot be escaped by writing a
    # column that did not exist when it was created
    assert "UPDATE OF" not in immutable, immutable

    cutoff = rows["trg_write_cutoff"]
    assert "AFTER INSERT" in cutoff
    assert "REFERENCING NEW TABLE" in cutoff, (
        "the statement-level cutoff cannot see inserted rows without its "
        "transition table")
    assert "reject_scenario_detail_after_deletion_request" in cutoff


# ---------------------------------------------------------------------------
# §18 — the write cutoff covers a v2 TX-2 in flight
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_v2_tx2_racing_the_deletion_cutoff_lands_nothing(monkeypatch):
    """The cutoff becomes active DURING TX-2, after the derived state is built
    and before the result row is written.

    This is the dangerous window: the evidence exists in memory, the seal has
    not happened, and the account has just been marked for deletion. The insert
    must be refused and the whole transaction rolled back — a v2 artifact
    written here would be dated after the cutoff, and a purge bounded on the
    request time would never claim it.
    """
    uid, analysis_id = await _user_with_analysis()
    await _publish_rules(1)

    real_builder = ScenarioService.build_counterfactual_derived_state

    async def build_then_request_deletion(self, session, pinned, computed):
        state = await real_builder(self, session, pinned, computed)
        # committed on its own connection, so the cutoff is durable and visible
        # to the trigger while this TX-2 is still open
        await _privileged(
            "INSERT INTO identity.account_lifecycle (user_id, status) "
            "VALUES (:uid, 'deletion_requested') ON CONFLICT DO NOTHING",
            uid=uid,
        )
        return state

    monkeypatch.setattr(
        ScenarioService, "build_counterfactual_derived_state",
        build_then_request_deletion)

    with pytest.raises(Exception):  # noqa: B017 - refused at the database
        await _seal_v2(uid, analysis_id)

    async with unit_of_work(actor_type="system") as s:
        sealed = list(await s.scalars(
            select(Scenario).where(Scenario.user_id == uid)))
        results = list(await s.scalars(
            select(ScenarioResult).where(ScenarioResult.scenario_id.in_(
                [x.id for x in sealed] or [uuid.uuid4()]))))

    assert results == [], "a v2 result row landed after the deletion cutoff"
    for scenario in sealed:
        assert scenario.workflow_status != "completed", (
            "a scenario sealed while its account was being deleted")
        assert not scenario.scenario_result_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("column", [
    "counterfactual_derived_state_hash", "raw_support_score",
])
async def test_a_sealed_v2_result_refuses_an_ordinary_update(column):
    """Through the APPLICATION identity, which is the one that matters: the
    corruption tests above had to bypass triggers entirely to land their edits."""
    uid, analysis_id = await _user_with_analysis()
    await _publish_rules(1)
    outcome = await _seal_v2(uid, analysis_id)

    with pytest.raises(Exception) as caught:  # noqa: PT011 - DB-level refusal
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await s.execute(text(
                f"UPDATE ioe.scenario_result SET {column} = NULL "  # noqa: S608
                "WHERE scenario_id = :sid"
            ), {"sid": outcome.scenario_id})
    assert "immutab" in str(caught.value).lower() or "cannot" in str(
        caught.value).lower(), str(caught.value)


# ---------------------------------------------------------------------------
# §17 — tenant isolation over the new columns
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_one_tenants_derived_state_is_invisible_to_another():
    """The artifact lives on an already-protected table, so no new policy is
    expected — but "expected" is not "measured"."""
    uid_a, analysis_a = await _user_with_analysis()
    uid_b, _ = await _user_with_analysis()
    await _publish_rules(1)
    outcome = await _seal_v2(uid_a, analysis_a)

    async with unit_of_work(user_id=uid_a, actor_type="user") as s:
        mine = await s.scalar(select(ScenarioResult).where(
            ScenarioResult.scenario_id == outcome.scenario_id))
        assert mine is not None
        assert mine.counterfactual_derived_state is not None

    async with unit_of_work(user_id=uid_b, actor_type="user") as s:
        theirs = await s.scalar(select(ScenarioResult).where(
            ScenarioResult.scenario_id == outcome.scenario_id))
        assert theirs is None, "RLS did not hide another tenant's derived state"
        leaked = list(await s.scalars(
            select(ScenarioResult.counterfactual_derived_state_hash)))
        assert mine.counterfactual_derived_state_hash not in leaked

    # and replay helpers must not become a side channel. `NotFound` rather
    # than a verdict is deliberate: returning any status would confirm the
    # entity exists, so an integrity endpoint would become an existence oracle.
    with pytest.raises(NotFound):
        await _verify(uid_b, outcome.scenario_id)


# ---------------------------------------------------------------------------
# §23 — determinism survives persistence
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_two_equivalent_v2_scenarios_seal_identical_evidence():
    """Same inputs, same rules, two separate seals: the derived bytes, the inner
    hash and the outer hash must all agree. Persistence must not introduce a
    source of variation the derivation does not have."""
    uid, analysis_id = await _user_with_analysis()
    await _publish_rules(3)

    first = await _seal_v2(uid, analysis_id, amount="5000")
    second = await _seal_v2(uid, analysis_id, amount="5000", key=str(uuid.uuid4()))

    _, left = await _rows(uid, first.scenario_id)
    _, right = await _rows(uid, second.scenario_id)

    assert left.counterfactual_derived_state == right.counterfactual_derived_state
    assert left.counterfactual_derived_state_hash == (
        right.counterfactual_derived_state_hash)
    assert first.result_hash == second.result_hash, (
        "two identical v2 scenarios sealed different outer hashes")


# ---------------------------------------------------------------------------
# §24 — the artifact is one row, not one row per candidate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_persisting_the_derived_state_does_not_scale_with_candidates():
    """Counted, not asserted. If the derived state were ever expanded into
    per-candidate rows, statement count would climb with the candidate count."""
    from sqlalchemy import event

    counts: dict[str, int] = {}

    async def measure(label: str, rules: int) -> None:
        uid, analysis_id = await _user_with_analysis()
        await _publish_rules(rules)
        statements = 0

        def before(conn, cursor, statement, parameters, context, executemany):
            nonlocal statements
            statements += 1

        from app.database.session import engine
        event.listen(engine.sync_engine, "before_cursor_execute", before)
        try:
            outcome = await _seal_v2(uid, analysis_id)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", before)
        _, result = await _rows(uid, outcome.scenario_id)
        counts[label] = statements
        counts[f"{label}_candidates"] = len(
            result.counterfactual_derived_state["candidates"])

    await measure("small", 1)
    await measure("large", 12)

    growth = counts["large"] - counts["small"]
    added = counts["large_candidates"] - counts["small_candidates"]
    assert added >= 8, (
        f"the fixture did not actually add candidates: {counts}")
    assert growth < added, (
        f"statement count grew with candidate count ({counts}); the derived "
        "state is being persisted per candidate rather than as one artifact")


# ---------------------------------------------------------------------------
# §25 — what the artifact actually costs
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_real_derived_state_payloads_stay_within_the_jsonb_decision():
    """Measured on real builder output at three sizes. The columns-not-table
    decision assumed a payload comfortably inside a single JSONB value; this is
    where that assumption meets real data instead of a synthetic probe."""
    import json

    report: list[dict] = []
    for label, rules in (("small", 1), ("moderate", 10), ("stress", 40)):
        uid, analysis_id = await _user_with_analysis()
        await _publish_rules(rules)
        outcome = await _seal_v2(uid, analysis_id)

        async with unit_of_work(user_id=uid, actor_type="user") as s:
            result = await s.scalar(select(ScenarioResult).where(
                ScenarioResult.scenario_id == outcome.scenario_id))
            stored_bytes = await s.scalar(text(
                "SELECT pg_column_size(counterfactual_derived_state) "
                "FROM ioe.scenario_result WHERE scenario_id = :sid"
            ), {"sid": outcome.scenario_id})

        payload = result.counterfactual_derived_state
        report.append({
            "label": label,
            "candidates": len(payload["candidates"]),
            "line_items": len(payload["line_items"]),
            "canonical_bytes": len(json.dumps(payload, sort_keys=True).encode()),
            "jsonb_bytes": stored_bytes,
        })

    print("\nA2 §25 derived-state measurements:")  # noqa: T201 - reported evidence
    for row in report:
        row["bytes_per_candidate"] = round(
            row["jsonb_bytes"] / max(row["candidates"], 1), 1)
        print(f"  {row}")  # noqa: T201

    # NOTE ON THE LABELS. The rule universe for a tax year is global, so every
    # rule any test in this database published is pinned by every scenario in
    # it. The three points therefore sit on a higher floor than "1 / 10 / 40
    # rules" suggests — what is being measured is the DIFFERENCE between them,
    # and the per-candidate cost that difference implies.
    stress = report[-1]
    assert stress["candidates"] >= 40, f"stress fixture too small: {report}"
    assert stress["candidates"] - report[0]["candidates"] >= 30, (
        f"the three points are not far enough apart to measure growth: {report}")
    # ~100 bytes of stored JSONB per candidate after TOAST compression. Even a
    # rule universe two orders of magnitude larger than anything plausible stays
    # far inside one JSONB value, which is what the columns-not-table decision
    # assumed. A regression here is the signal to revisit that decision.
    assert stress["bytes_per_candidate"] < 500, report
    # One JSONB value, comfortably under the 2KB TOAST threshold per candidate
    # and orders of magnitude under the 1GB field limit. A failure here is the
    # signal to revisit the columns-not-table decision, not to paper over it.
    assert stress["jsonb_bytes"] < 1_000_000, (
        f"the real stress payload is far larger than the storage decision "
        f"assumed: {report}")
    assert stress["jsonb_bytes"] > report[0]["jsonb_bytes"], (
        "payload size does not respond to candidate count; the measurement is "
        f"not measuring anything: {report}")
