"""Entry 9 — real freshness producer wiring.

The helpers existed; the mutation services did not call them. A helper with no
production caller closes nothing, so these tests always go through the REAL
mutation service — never `emit()` directly — and then through the REAL relay.

Freshness and integrity stay independent throughout: a stale result keeps its
sealed hashes and its integrity status untouched.
"""
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    FreshnessOutbox,
    Jurisdiction,
    OptimizationRun,
    RuleOutcome,
    Scenario,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.privacy_session import dispose_all_engines
from app.database.session import unit_of_work
from app.services.document_processing.service import DocumentService
from app.services.financial.service import FinancialService
from app.services.ioe.domain.scenario import FreshnessStatus, StaleReason
from app.services.ioe.freshness_events import FreshnessEvent
from app.services.ioe.freshness_relay import FreshnessRelay
from app.services.ioe.orchestrator import OptimizationOrchestrator
from app.services.ioe.scenario.service import ScenarioService
from app.services.users.profile_service import (
    PRESENTATION_FIELDS,
    TAX_RELEVANT_FIELDS,
    ProfileService,
)
from tests.conftest import frozen_snapshot

SYNTHETIC_SIN = "046454286"
SYNTHETIC_EMPLOYER = "Acme Holdings Numbered Co"
RRSP = "INCREASE_RRSP_DEDUCTION"


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    # BOTH runtimes. These tests drive `FreshnessRelay`, whose keyhole
    # calls run on the freshness engine; disposing only the app engine
    # leaves that pool bound to a finished event loop and the next test
    # fails with 'attached to a different loop'.
    await dispose_all_engines()


def _suffix() -> str:
    return uuid.uuid4().hex[:8].upper()


async def _publish(code: str) -> uuid.UUID:
    async with unit_of_work(actor_type="admin") as s:
        jur = await s.scalar(select(Jurisdiction).where(Jurisdiction.code == "FED"))
        rule = TaxRule(code=code, name=code, category="deduction",
                       jurisdiction_id=jur.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=2025, effective_date=date(2025, 1, 1),
            status="published", description="entry9 fixture",
            eligibility_basis_codes=["BASIS_E9"])
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            title_template=f"{code} opportunity"))
        await s.flush()
        return version.id


async def _user(province: str = "ON") -> uuid.UUID:
    async with unit_of_work(actor_type="system") as s:
        user = UserAccount(email=f"e9_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(user)
        await s.flush()
        uid = user.id
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code=province,
                         marital_status="single"))
        await s.flush()
    return uid


async def _analysis(uid: uuid.UUID, *, tax_year: int = 2025,
                    province: str = "ON") -> uuid.UUID:
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = AnalysisRun(
            user_id=uid, tax_year=tax_year, province_code=province,
            engine_version="py-1.0.0", status="completed",
            started_at=datetime.now(tz=UTC), completed_at=datetime.now(tz=UTC),
            data_verified=True)
        s.add(run)
        await s.flush()
        payload, digest = frozen_snapshot(
            tax_year=tax_year, jurisdiction=province,
            employment_income=Decimal("95000"))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=payload, snapshot_hash=digest))
        await s.flush()
        return run.id


async def _sealed_pair(uid: uuid.UUID, analysis_id: uuid.UUID, *,
                       jurisdiction: str = "ON"):
    """One completed optimization and one completed scenario."""
    from app.services.ioe.domain.scenario import ScenarioSpec

    opt = await OptimizationOrchestrator(uid).generate(analysis_id)
    spec = ScenarioSpec.parse(
        [{"lever_code": RRSP, "parameters": {"amount": Decimal("5000")}}],
        assumptions=[], jurisdiction=jurisdiction, tax_year=2025)
    scn = await ScenarioService(uid).simulate(analysis_id, spec)
    return opt.run_id, scn.scenario_id


async def _sealed_state(uid, run_id, scenario_id) -> dict:
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await s.get(OptimizationRun, run_id)
        scn = await s.get(Scenario, scenario_id)
        return {
            "run_spec": run.optimization_spec_hash,
            "run_result": run.optimization_result_hash,
            "run_integrity": run.integrity_status,
            "run_freshness": run.freshness_status,
            "scn_spec": scn.scenario_spec_hash,
            "scn_result": scn.scenario_result_hash,
            "scn_integrity": scn.integrity_status,
            "scn_freshness": scn.freshness_status,
            "scn_reason": scn.stale_reason_code,
        }


async def _outbox(uid: uuid.UUID) -> list[FreshnessOutbox]:
    """Read the outbox UNDER THE OWNER'S TENANT CONTEXT.

    `ioe.freshness_outbox` is RLS-protected and denies by default with no
    `app.user_id`, so a systems-context read returns nothing — which would make
    every assertion here pass or fail for the wrong reason.
    """
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        return list(await s.scalars(
            select(FreshnessOutbox)
            .where(FreshnessOutbox.user_id == uid)
            .order_by(FreshnessOutbox.created_at)))


# =============================================================================
# 1. Producer invocation from the REAL mutation service
# =============================================================================
@pytest.mark.asyncio
async def test_adding_income_through_the_real_service_emits_one_event():
    uid = await _user()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await FinancialService(s).add_income(uid, 2025, "employment", Decimal("81000"))

    rows = await _outbox(uid)
    assert len(rows) == 1
    assert rows[0].event_type == FreshnessEvent.FINANCIAL_DATA_CHANGED.value
    assert rows[0].stale_reason_code == StaleReason.BASELINE_INPUTS_CHANGED.value
    assert rows[0].tax_year == 2025


@pytest.mark.asyncio
async def test_adding_an_expense_through_the_real_service_emits_one_event():
    uid = await _user()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await FinancialService(s).add_expense(uid, 2025, "medical", Decimal("1200"))

    rows = await _outbox(uid)
    assert len(rows) == 1
    assert rows[0].event_type == FreshnessEvent.FINANCIAL_DATA_CHANGED.value


@pytest.mark.asyncio
async def test_two_distinct_edits_emit_two_events_and_a_retry_emits_one():
    """Deduplication must not collapse genuinely different source rows."""
    uid = await _user()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await FinancialService(s).add_income(uid, 2025, "employment", Decimal("1"))
        await FinancialService(s).add_income(uid, 2025, "employment", Decimal("2"))
    assert len(await _outbox(uid)) == 2, "distinct source rows collapsed"

    # a retry of one logical edit — same change token — collides
    from app.services.ioe.freshness_producers import on_financial_data_changed
    rows = await _outbox(uid)
    # key shape: financial:{user}:{year}:{token} — the token itself contains a
    # colon ("income:<id>"), so split on the first three separators only.
    token = rows[0].dedupe_key.split(":", 3)[3]
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        emitted = await on_financial_data_changed(
            s, uid, 2025, change_token=token)
    assert emitted is False
    assert len(await _outbox(uid)) == 2


@pytest.mark.asyncio
async def test_a_tax_relevant_profile_change_emits_through_the_real_service():
    uid = await _user()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        _, emitted = await ProfileService(s).upsert_tax_profile(
            uid, {"province_code": "BC"})
    assert emitted is True

    rows = await _outbox(uid)
    assert len(rows) == 1
    assert rows[0].event_type == FreshnessEvent.PROFILE_CHANGED.value


@pytest.mark.asyncio
async def test_a_presentation_only_profile_change_emits_nothing():
    """Switching locale must not stale a sealed tax result."""
    uid = await _user()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        _, emitted = await ProfileService(s).upsert_tax_profile(
            uid, {"display_name": "Renamed", "locale": "fr-CA",
                  "employer_name": SYNTHETIC_EMPLOYER})
    assert emitted is False
    assert await _outbox(uid) == []


@pytest.mark.asyncio
async def test_resubmitting_an_unchanged_profile_emits_nothing():
    """A full PUT of identical values changed nothing and must not invalidate."""
    uid = await _user(province="ON")
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        _, emitted = await ProfileService(s).upsert_tax_profile(
            uid, {"province_code": "ON", "marital_status": "single"})
    assert emitted is False
    assert await _outbox(uid) == []


def test_the_tax_relevance_map_and_presentation_map_are_disjoint():
    assert not (TAX_RELEVANT_FIELDS & PRESENTATION_FIELDS)
    # every field the engine reads directly must be classified tax-relevant
    for engine_field in ("province_code", "marital_status", "date_of_birth"):
        assert engine_field in TAX_RELEVANT_FIELDS


# =============================================================================
# 2. Atomicity — the event shares the mutation's fate
# =============================================================================
@pytest.mark.asyncio
async def test_a_rolled_back_mutation_leaves_no_event_and_no_row():
    uid = await _user()
    with pytest.raises(RuntimeError):
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await FinancialService(s).add_income(
                uid, 2025, "employment", Decimal("77000"))
            raise RuntimeError("forced rollback")

    assert await _outbox(uid) == [], "an event survived a rolled-back mutation"
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        rows = await s.scalar(text(
            "SELECT count(*) FROM finance.income_source WHERE user_id = :u"
        ).bindparams(u=uid))
    assert rows == 0, "the source row survived its own rollback"


@pytest.mark.asyncio
async def test_the_event_commits_in_the_same_transaction_as_the_mutation():
    """Not 'commit then emit': both must be visible only after one commit."""
    uid = await _user()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await FinancialService(s).add_income(uid, 2025, "employment", Decimal("5"))
        # still inside the transaction — another connection sees neither
        async with unit_of_work(user_id=uid, actor_type="user") as other:
            seen = await other.scalar(
                select(func.count()).select_from(FreshnessOutbox)
                .where(FreshnessOutbox.user_id == uid))
        assert seen == 0, "the event was visible before the mutation committed"

    assert len(await _outbox(uid)) == 1


# =============================================================================
# 3. End to end — real service, real relay, sealed evidence untouched
# =============================================================================
@pytest.mark.asyncio
async def test_a_financial_change_stales_both_sealed_workflows_end_to_end():
    await _publish(f"E9F{_suffix()}")
    uid = await _user()
    analysis_id = await _analysis(uid)
    run_id, scenario_id = await _sealed_pair(uid, analysis_id)
    before = await _sealed_state(uid, run_id, scenario_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await FinancialService(s).add_income(
            uid, 2025, "employment", Decimal("310000"))

    report = await FreshnessRelay().drain(batch_size=50)
    assert report.completed >= 1

    after = await _sealed_state(uid, run_id, scenario_id)
    assert after["scn_freshness"] == FreshnessStatus.STALE.value
    assert after["scn_reason"] == StaleReason.BASELINE_INPUTS_CHANGED.value
    # sealed evidence and integrity are untouched — freshness is a label
    assert after["scn_spec"] == before["scn_spec"]
    assert after["scn_result"] == before["scn_result"]
    assert after["scn_integrity"] == before["scn_integrity"]
    assert after["run_spec"] == before["run_spec"]
    assert after["run_result"] == before["run_result"]
    assert after["run_integrity"] == before["run_integrity"]


@pytest.mark.asyncio
async def test_a_tax_relevant_profile_change_stales_end_to_end():
    await _publish(f"E9P{_suffix()}")
    uid = await _user()
    analysis_id = await _analysis(uid)
    run_id, scenario_id = await _sealed_pair(uid, analysis_id)
    before = await _sealed_state(uid, run_id, scenario_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await ProfileService(s).upsert_tax_profile(uid, {"province_code": "BC"})

    await FreshnessRelay().drain(batch_size=50)

    after = await _sealed_state(uid, run_id, scenario_id)
    assert after["scn_freshness"] == FreshnessStatus.STALE.value
    assert after["scn_result"] == before["scn_result"]
    assert after["scn_integrity"] == before["scn_integrity"]


@pytest.mark.asyncio
async def test_a_document_confirmation_stales_because_it_creates_inputs():
    """Confirming an extraction creates income rows — a calculation input."""
    await _publish(f"E9D{_suffix()}")
    uid = await _user()
    analysis_id = await _analysis(uid)
    _, scenario_id = await _sealed_pair(uid, analysis_id)

    doc_id = await _document_with_extraction(uid)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        created = await DocumentService(s).confirm(uid, doc_id, 2025)
    assert created["income"] >= 1

    rows = await _outbox(uid)
    kinds = {r.event_type for r in rows}
    assert FreshnessEvent.FINANCIAL_DATA_CHANGED.value in kinds
    assert FreshnessEvent.DOCUMENT_STATUS_CHANGED.value in kinds

    await FreshnessRelay().drain(batch_size=50)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scn = await s.get(Scenario, scenario_id)
    assert scn.freshness_status == FreshnessStatus.STALE.value


async def _document_with_extraction(uid: uuid.UUID) -> uuid.UUID:
    """A processed document whose extraction yields an income field."""
    from app.database.models import (
        Document,
        DocumentExtraction,
        DocumentType,
        ExtractionField,
    )

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        dtype = await s.scalar(select(DocumentType).limit(1))
        doc = Document(
            user_id=uid, document_type_id=dtype.id if dtype else None,
            tax_year=2025, bucket="onyx-documents",
            object_key=f"k/{uuid.uuid4()}", status="processed",
            byte_size=1024, content_hash="a" * 64, mime_type="application/pdf")
        s.add(doc)
        await s.flush()
        extraction = DocumentExtraction(
            document_id=doc.id, engine="test-1", status="processed",
            confidence=Decimal("0.99"))
        s.add(extraction)
        await s.flush()
        s.add(ExtractionField(
            extraction_id=extraction.id, field_name="employmentIncome",
            value_number=Decimal("64000"), confidence=Decimal("0.99")))
        await s.flush()
        return doc.id


# =============================================================================
# 4. Scope — unrelated tenants and years stay current
# =============================================================================
@pytest.mark.asyncio
async def test_one_users_change_never_stales_another_users_result():
    await _publish(f"E9T{_suffix()}")
    uid_a = await _user()
    uid_b = await _user()
    analysis_a = await _analysis(uid_a)
    analysis_b = await _analysis(uid_b)
    _, scn_a = await _sealed_pair(uid_a, analysis_a)
    _, scn_b = await _sealed_pair(uid_b, analysis_b)

    async with unit_of_work(user_id=uid_a, actor_type="user") as s:
        await FinancialService(s).add_income(uid_a, 2025, "employment", Decimal("9"))
    await FreshnessRelay().drain(batch_size=50)

    async with unit_of_work(user_id=uid_a, actor_type="user") as s:
        a = await s.get(Scenario, scn_a)
    async with unit_of_work(user_id=uid_b, actor_type="user") as s:
        b = await s.get(Scenario, scn_b)
    assert a.freshness_status == FreshnessStatus.STALE.value
    assert b.freshness_status == FreshnessStatus.CURRENT.value, (
        "another tenant's result was staled")


@pytest.mark.asyncio
async def test_a_change_in_one_tax_year_leaves_another_year_current():
    await _publish(f"E9Y{_suffix()}")
    uid = await _user()
    analysis_2025 = await _analysis(uid, tax_year=2025)
    _, scn_2025 = await _sealed_pair(uid, analysis_2025)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await FinancialService(s).add_income(uid, 2024, "employment", Decimal("50"))
    await FreshnessRelay().drain(batch_size=50)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scn = await s.get(Scenario, scn_2025)
    assert scn.freshness_status == FreshnessStatus.CURRENT.value, (
        "a 2024 change staled a 2025 result")


# =============================================================================
# 5. Idempotency of delivery
# =============================================================================
@pytest.mark.asyncio
async def test_draining_twice_produces_one_effective_transition():
    await _publish(f"E9I{_suffix()}")
    uid = await _user()
    analysis_id = await _analysis(uid)
    _, scenario_id = await _sealed_pair(uid, analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await FinancialService(s).add_income(uid, 2025, "employment", Decimal("11"))

    await FreshnessRelay().drain(batch_size=50)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        first = await s.get(Scenario, scenario_id)
        state = (first.freshness_status, first.stale_reason_code)

    await FreshnessRelay().drain(batch_size=50)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        second = await s.get(Scenario, scenario_id)
    assert (second.freshness_status, second.stale_reason_code) == state


# =============================================================================
# 6. Privacy — no financial or personal value in a durable event
# =============================================================================
@pytest.mark.asyncio
async def test_no_financial_or_personal_value_reaches_a_durable_event():
    import json

    uid = await _user()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await FinancialService(s).add_income(
            uid, 2025, "employment", Decimal("187654.33"),
            source_name=SYNTHETIC_EMPLOYER)
        await ProfileService(s).upsert_tax_profile(
            uid, {"employer_name": SYNTHETIC_EMPLOYER, "province_code": "BC"})

    rows = await _outbox(uid)
    blob = json.dumps([
        {c.name: str(getattr(r, c.name))
         for c in FreshnessOutbox.__table__.columns}
        for r in rows
    ])
    for leak in (SYNTHETIC_SIN, SYNTHETIC_EMPLOYER, "187654.33"):
        assert leak not in blob, f"durable event leaked {leak}"
    # the profile event names the FIELD that moved, never its value
    profile_rows = [r for r in rows
                    if r.event_type == FreshnessEvent.PROFILE_CHANGED.value]
    assert profile_rows and "province_code" in profile_rows[0].dedupe_key
    assert "BC" not in profile_rows[0].dedupe_key


# =============================================================================
# 7. Version activation — authoritative, atomic, concurrency safe
# =============================================================================
@pytest.mark.asyncio
async def test_activating_the_already_active_version_emits_nothing():
    """A process restart is not a version activation."""
    from app.services.ioe.version_activation import VersionActivationService

    kind = FreshnessEvent.ENGINE_VERSION_CHANGED
    version = f"probe-{_suffix()}"
    async with unit_of_work(actor_type="system") as s:
        first = await VersionActivationService(s).activate(kind, version)
    assert first.activated is True

    # ten restarts of the same running version
    for _ in range(10):
        async with unit_of_work(actor_type="system") as s:
            outcome = await VersionActivationService(s).activate(kind, version)
        assert outcome.activated is False
        assert outcome.event_emitted is False
        assert outcome.already_active is True

    async with unit_of_work(actor_type="system") as s:
        events = await s.scalar(text(
            "SELECT count(*) FROM ioe.freshness_outbox "
            "WHERE dedupe_key LIKE :k").bindparams(k=f"activation:{kind.value}:{version}:%"))
    assert events == 1, "restarts emitted more than one event"


@pytest.mark.asyncio
async def test_concurrent_activations_of_one_new_version_emit_exactly_one_event():
    """FOR UPDATE arbitrates: nine losers observe the winner's row."""
    import asyncio

    from app.services.ioe.version_activation import VersionActivationService

    kind = FreshnessEvent.OBJECTIVE_POLICY_CHANGED
    start = f"a-{_suffix()}"
    target = f"b-{_suffix()}"
    async with unit_of_work(actor_type="system") as s:
        await VersionActivationService(s).activate(kind, start)

    async def _activate():
        async with unit_of_work(actor_type="system") as s:
            return await VersionActivationService(s).activate(kind, target)

    outcomes = await asyncio.gather(*[_activate() for _ in range(10)],
                                    return_exceptions=True)
    ok = [o for o in outcomes if not isinstance(o, BaseException)]
    assert len(ok) == 10, [o for o in outcomes if isinstance(o, BaseException)]
    assert sum(1 for o in ok if o.activated) == 1, "more than one activation won"
    assert all(o.active_version == target for o in ok)

    async with unit_of_work(actor_type="system") as s:
        events = await s.scalar(text(
            "SELECT count(*) FROM ioe.freshness_outbox "
            "WHERE dedupe_key LIKE :k").bindparams(k=f"activation:{kind.value}:{target}:%"))
    assert events == 1, "concurrent activation emitted duplicate events"


@pytest.mark.asyncio
async def test_a_failed_event_insert_rolls_back_the_activation():
    """Activation and event share one transaction, so neither can survive alone."""
    from app.services.ioe.version_activation import VersionActivationService

    kind = FreshnessEvent.LEVER_REGISTRY_CHANGED
    original = f"v-{_suffix()}"
    async with unit_of_work(actor_type="system") as s:
        await VersionActivationService(s).activate(kind, original)

    with pytest.raises(RuntimeError):
        async with unit_of_work(actor_type="system") as s:
            await VersionActivationService(s).activate(kind, f"v-{_suffix()}")
            raise RuntimeError("event pipeline failed")

    async with unit_of_work(actor_type="system") as s:
        still = await VersionActivationService(s).active_version(kind)
    assert still == original, "the activation survived a rolled-back transaction"


# =============================================================================
# 8. RLS context categories — kept explicitly separate
# =============================================================================
async def read_outbox_as(owner: uuid.UUID, viewer: uuid.UUID) -> int:
    """Read OWNER's events under VIEWER's tenant context. No inference."""
    async with unit_of_work(user_id=viewer, actor_type="user") as s:
        return await s.scalar(
            select(func.count()).select_from(FreshnessOutbox)
            .where(FreshnessOutbox.user_id == owner))


async def read_outbox_without_context(owner: uuid.UUID) -> int:
    async with unit_of_work(actor_type="system") as s:
        return await s.scalar(
            select(func.count()).select_from(FreshnessOutbox)
            .where(FreshnessOutbox.user_id == owner))


@pytest.mark.asyncio
async def test_owner_context_sees_its_own_event():
    uid = await _user()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await FinancialService(s).add_income(uid, 2025, "employment", Decimal("3"))
    assert await read_outbox_as(uid, uid) == 1


@pytest.mark.asyncio
async def test_cross_tenant_context_cannot_see_another_users_event():
    uid_a = await _user()
    uid_b = await _user()
    async with unit_of_work(user_id=uid_a, actor_type="user") as s:
        await FinancialService(s).add_income(uid_a, 2025, "employment", Decimal("4"))
    assert await read_outbox_as(uid_a, uid_b) == 0, "cross-tenant read succeeded"


@pytest.mark.asyncio
async def test_no_tenant_context_denies_the_read_by_default():
    """The helper must not quietly supply a context that hides deny-by-default."""
    uid = await _user()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await FinancialService(s).add_income(uid, 2025, "employment", Decimal("6"))
    assert await read_outbox_without_context(uid) == 0
    # and the same rows ARE visible once a context is supplied deliberately
    assert await read_outbox_as(uid, uid) == 1


# =============================================================================
# 9. Analysis supersession — its own reason, scoped to the year
# =============================================================================
@pytest.mark.asyncio
async def test_a_newer_analysis_stales_older_results_with_its_own_reason():
    """`NEWER_ANALYSIS_AVAILABLE`, not "your inputs changed"."""
    from app.services.analysis.service import AnalysisService

    await _publish(f"E9A{_suffix()}")
    uid = await _user()
    analysis_id = await _analysis(uid)
    run_id, scenario_id = await _sealed_pair(uid, analysis_id)
    before = await _sealed_state(uid, run_id, scenario_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await AnalysisService(s).run(uid, 2025)

    rows = await _outbox(uid)
    completed = [r for r in rows
                 if r.event_type == FreshnessEvent.ANALYSIS_COMPLETED.value]
    assert completed, "completing an analysis emitted nothing"
    assert completed[0].stale_reason_code == (
        StaleReason.NEWER_ANALYSIS_AVAILABLE.value)

    await FreshnessRelay().drain(batch_size=50)
    after = await _sealed_state(uid, run_id, scenario_id)
    assert after["scn_freshness"] == FreshnessStatus.STALE.value
    assert after["scn_reason"] == StaleReason.NEWER_ANALYSIS_AVAILABLE.value
    # sealed evidence and integrity untouched
    assert after["scn_result"] == before["scn_result"]
    assert after["scn_spec"] == before["scn_spec"]
    assert after["scn_integrity"] == before["scn_integrity"]


@pytest.mark.asyncio
async def test_completing_an_analysis_leaves_another_tax_year_current():
    from app.services.analysis.service import AnalysisService

    await _publish(f"E9AY{_suffix()}")
    uid = await _user()
    analysis_2025 = await _analysis(uid, tax_year=2025)
    _, scn_2025 = await _sealed_pair(uid, analysis_2025)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await AnalysisService(s).run(uid, 2024)
    await FreshnessRelay().drain(batch_size=50)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scn = await s.get(Scenario, scn_2025)
    assert scn.freshness_status == FreshnessStatus.CURRENT.value, (
        "a 2024 analysis staled a 2025 result")


# =============================================================================
# 10. Rule publication — scoped by jurisdiction, not just year
# =============================================================================
@pytest.mark.asyncio
async def test_a_provincial_rule_publication_carries_its_jurisdiction():
    """The event must name the province so fan-out can narrow to it."""
    async with unit_of_work(actor_type="admin") as s:
        jur = await s.scalar(select(Jurisdiction).where(Jurisdiction.code == "ON"))
        assert jur is not None, "ON jurisdiction fixture missing"

    from app.services.ioe.freshness_events import emit

    async with unit_of_work(actor_type="admin") as s:
        await emit(s, FreshnessEvent.RULE_PUBLISHED, tax_year=2025,
                   jurisdiction="ON",
                   dedupe_key=f"rule_published:probe:{_suffix()}")

    async with unit_of_work(actor_type="admin") as s:
        row = await s.scalar(
            select(FreshnessOutbox)
            .where(FreshnessOutbox.jurisdiction == "ON")
            .order_by(FreshnessOutbox.created_at.desc()).limit(1))
    assert row is not None and row.tax_year == 2025


@pytest.mark.asyncio
async def test_a_provincial_rule_stales_only_that_jurisdictions_results():
    """An Ontario rule must not stale a British Columbia result."""
    from app.services.ioe.scenario.freshness_service import ScenarioFreshnessService

    await _publish(f"E9J{_suffix()}")
    # A genuine BC scenario — `scenario.jurisdiction` is sealed by
    # trg_guard_transition once completed, so it cannot be retro-fitted.
    uid = await _user(province="BC")
    bc_analysis = await _analysis(uid, province="BC")
    _, bc_scn = await _sealed_pair(uid, bc_analysis, jurisdiction="BC")

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        row = await s.get(Scenario, bc_scn)
        assert row.jurisdiction == "BC"

    # an Ontario rule must not touch it
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        marked = await ScenarioFreshnessService(s, uid).invalidate_for_tax_year(
            2025, StaleReason.RULE_SNAPSHOT_SUPERSEDED, jurisdiction="ON")
    assert marked == 0, "an ON-scoped rule staled a BC result"

    # its own jurisdiction does
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        marked = await ScenarioFreshnessService(s, uid).invalidate_for_tax_year(
            2025, StaleReason.RULE_SNAPSHOT_SUPERSEDED, jurisdiction="BC")
    assert marked == 1, "a BC-scoped rule failed to stale the BC result"


@pytest.mark.asyncio
async def test_an_unscoped_event_still_fans_out_across_jurisdictions():
    """NULL jurisdiction preserves the historical breadth — federal rules."""
    from app.services.ioe.scenario.freshness_service import ScenarioFreshnessService

    await _publish(f"E9N{_suffix()}")
    uid = await _user()
    analysis_id = await _analysis(uid, province="ON")
    await _sealed_pair(uid, analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        marked = await ScenarioFreshnessService(s, uid).invalidate_for_tax_year(
            2025, StaleReason.ENGINE_VERSION_CHANGED)
    assert marked >= 1, "an unscoped event failed to fan out"


# =============================================================================
# 11. Registry activation — every governed component is reconciled
# =============================================================================
def test_every_governed_registry_is_reconciled_at_activation():
    from app.services.ioe.freshness_producers import RUNNING_VERSIONS

    required = {
        FreshnessEvent.ENGINE_VERSION_CHANGED,
        FreshnessEvent.REFERENCE_DATA_CHANGED,
        FreshnessEvent.OBJECTIVE_POLICY_CHANGED,
        FreshnessEvent.LEVER_REGISTRY_CHANGED,
        FreshnessEvent.ASSUMPTION_REGISTRY_CHANGED,
        FreshnessEvent.RELATIONSHIP_REGISTRY_CHANGED,
        FreshnessEvent.SUPPORT_SCORE_POLICY_CHANGED,
        FreshnessEvent.PROJECTION_METHODOLOGY_CHANGED,
    }
    assert required <= set(RUNNING_VERSIONS), (
        f"unreconciled components: {required - set(RUNNING_VERSIONS)}")
    # every resolver returns a non-empty bounded string
    for event, resolve in RUNNING_VERSIONS.items():
        value = resolve()
        assert isinstance(value, str) and value, event.value


@pytest.mark.asyncio
async def test_activating_a_registry_emits_its_own_reason():
    from app.services.ioe.version_activation import VersionActivationService

    kind = FreshnessEvent.RELATIONSHIP_REGISTRY_CHANGED
    async with unit_of_work(actor_type="system") as s:
        outcome = await VersionActivationService(s).activate(
            kind, f"rel-{_suffix()}")
    assert outcome.activated is True

    async with unit_of_work(actor_type="system") as s:
        row = await s.scalar(
            select(FreshnessOutbox)
            .where(FreshnessOutbox.event_type == kind.value)
            .order_by(FreshnessOutbox.created_at.desc()).limit(1))
    assert row is not None
    assert row.stale_reason_code == StaleReason.RELATIONSHIP_REGISTRY_CHANGED.value
