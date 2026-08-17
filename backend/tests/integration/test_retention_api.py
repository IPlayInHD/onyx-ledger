"""Entry: Retention / "What Changed?" — the API over real governed state.

The unit suite proves the comparator over synthetic snapshots. What is at stake
here is the request path: that a GET never advances the baseline, that an
acknowledgement records only the state the client actually reviewed, that a
race loses safely, that first use invents no arrivals, and that the checkpoint
dies with the account.

Fixture isolation follows the lifecycle entry's hard-won rule: ONE governed
rule is published for the whole module. A fresh rule per test would compete
with itself for the optimizer's search budget and its lever resource, which is
exactly how the lifecycle suite once passed alone and failed in company.
"""
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, select, text

from app.core.security.jwt import create_access_token
from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisLineItem,
    AnalysisRun,
    CalcFormula,
    CalcFormulaInput,
    Document,
    DocumentType,
    IncomeSource,
    IncomeType,
    Jurisdiction,
    RetentionCheckpoint,
    RuleAction,
    RuleDeadline,
    RuleOutcome,
    RuleRequiredDocument,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import unit_of_work
from app.schemas.retention import RETENTION_CHANGES_SCHEMA_VERSION
from app.services.ioe.orchestrator import OptimizationOrchestrator
from app.services.ioe.retention.snapshot import (
    RETENTION_SNAPSHOT_SCHEMA_VERSION,
)
from app.services.privacy.lifecycle import AccountLifecycleService
from tests.conftest import frozen_snapshot

CHANGES = "/api/v1/ioe/changes"
ACK = "/api/v1/ioe/changes/acknowledge"
JOURNAL_API = "/api/v1/ioe/decision-journal"
TAX_YEAR = 2025
RRSP = "INCREASE_RRSP_DEDUCTION"

DEADLINE = date(2026, 4, 30)
#: Bands against DEADLINE: 211 days, 10 days, and one day past.
NORMAL_AS_OF = "2025-10-01"
URGENT_AS_OF = "2026-04-20"
EXPIRED_AS_OF = "2026-05-01"
IMPACT = Decimal("250000")


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _auth(user_id: uuid.UUID) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


def _url(*, tax_year: int = TAX_YEAR, as_of: str | None = NORMAL_AS_OF) -> str:
    base = f"{CHANGES}?tax_year={tax_year}"
    return f"{base}&as_of={as_of}" if as_of else base


def _ack_url(*, as_of: str | None = NORMAL_AS_OF) -> str:
    base = f"{ACK}?tax_year={TAX_YEAR}"
    return f"{base}&as_of={as_of}" if as_of else base


# ------------------------------------------------------------------ fixtures
async def _user_with_analysis() -> tuple[uuid.UUID, uuid.UUID]:
    from app.services.tax_engine.core.engine import compute

    async with unit_of_work(actor_type="system") as s:
        account = UserAccount(
            email=f"ret_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(account)
        await s.flush()
        uid = account.id
        income_type_id = (await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment"))).id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=TAX_YEAR, income_type_id=income_type_id,
            amount=Decimal("95000"), province_code="ON"))
        run = AnalysisRun(
            user_id=uid, tax_year=TAX_YEAR, province_code="ON",
            engine_version="py-1.0.0", status="completed",
            started_at=datetime.now(tz=UTC), completed_at=datetime.now(tz=UTC),
            data_verified=True)
        s.add(run)
        await s.flush()
        payload, digest = frozen_snapshot(employment_income=Decimal("95000"))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=payload, snapshot_hash=digest))
        result = compute(reconstruct(payload))
        for i, item in enumerate(result.line_items):
            s.add(AnalysisLineItem(
                analysis_id=run.id, kind=item["kind"], label=item["label"],
                amount=item["amount"], sort_order=i))
        await s.flush()
        return uid, run.id


def reconstruct(payload):
    from app.services.ioe.frozen.models import reconstruct_tax_input

    return reconstruct_tax_input(payload)


async def _publish_rule(*, document_type_code: str = "T4") -> str:
    """A governed rule carrying a dominant impact, so it ranks first and the
    optimizer's 200-run search budget cannot strand it behind other suites'
    globally published rules."""
    async with unit_of_work(actor_type="admin") as s:
        jurisdiction = await s.scalar(
            select(Jurisdiction).where(Jurisdiction.code == "FED"))
        code = f"RETENTION_{uuid.uuid4().hex[:6].upper()}"
        rule = TaxRule(code=code, name=f"{code} rule", category="deduction",
                       jurisdiction_id=jurisdiction.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=TAX_YEAR,
            effective_date=date(TAX_YEAR, 1, 1), status="published",
            description="retention fixture",
            eligibility_basis_codes=["BASIS_RETENTION"])
        s.add(version)
        await s.flush()
        formula = CalcFormula(
            code=f"RETENTIONIMPACT_{uuid.uuid4().hex[:8]}",
            expression="amount", expression_lang="rpn", output_unit="CAD",
            description="fixed governed impact, large enough to rank first")
        s.add(formula)
        await s.flush()
        s.add(CalcFormulaInput(
            formula_id=formula.id, param_name="amount", literal_value=IMPACT))
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            impact_formula_id=formula.id,
            title_template=f"{code} opportunity",
            economic_effect_type="current_year_tax_reduction",
            reversibility="reversible", portfolio_lever_code=RRSP,
            lever_parameters={"amount": "action.cost_amount"}))
        s.add(RuleAction(
            rule_version_id=version.id, action_code="CONTRIBUTE",
            description="Contribute", effort_rating=2,
            cost_type="liquidity_commitment", cost_amount=Decimal("5000")))
        s.add(RuleRequiredDocument(
            rule_version_id=version.id,
            document_type_code=document_type_code, necessity="required"))
        s.add(RuleDeadline(
            rule_version_id=version.id, deadline_code=f"DL_{code}",
            deadline_date=DEADLINE, is_hard=True, jurisdiction_code="FED"))
        await s.flush()
        return code.lower()


#: ONE rule for the whole module — see the module docstring.
_SHARED_RULE: list[str] = []


async def _shared_rule() -> str:
    if not _SHARED_RULE:
        _SHARED_RULE.append(await _publish_rule())
    return _SHARED_RULE[0]


async def _hold(uid: uuid.UUID, code: str = "T4") -> tuple[str, str, str]:
    async with unit_of_work(actor_type="system") as s:
        type_id = (await s.scalar(
            select(DocumentType).where(DocumentType.code == code))).id
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        document = Document(
            user_id=uid, document_type_id=type_id, tax_year=TAX_YEAR,
            bucket=f"onyx-bucket-{uuid.uuid4().hex}",
            object_key=f"{uid}/retention/{uuid.uuid4()}",
            content_hash=uuid.uuid4().hex, status="processed")
        s.add(document)
        await s.flush()
        return str(document.id), document.bucket, document.object_key


async def _ready(*, evidence: bool = True) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Held evidence, the shared governed rule, and a completed run."""
    uid, analysis_id = await _user_with_analysis()
    if evidence:
        await _hold(uid)
    code = await _shared_rule()
    await OptimizationOrchestrator(uid).generate(analysis_id)
    return uid, analysis_id, code


async def _acknowledge(client, uid, body_over: dict | None = None,
                       *, as_of: str = NORMAL_AS_OF, expect: int = 201):
    """Read the current state and acknowledge exactly it."""
    seen = (await client.get(_url(as_of=as_of), headers=_auth(uid))).json()
    body = {
        "snapshot_hash": seen["current_snapshot_hash"],
        "baseline_checkpoint_id": (
            seen["baseline_checkpoint"]["id"]
            if seen["baseline_checkpoint"] else None),
        "request_id": str(uuid.uuid4()),
    }
    body.update(body_over or {})
    response = await client.post(
        _ack_url(as_of=as_of), headers=_auth(uid), json=body)
    assert response.status_code == expect, response.text
    return response


def _mine(body: dict, code: str, *, category: str | None = None,
          kind: str | None = None) -> list[dict]:
    """This suite's OWN changes.

    Rules are published globally, so a user's snapshot also contains every
    other suite's fixture opportunity. Asserting over the whole change list
    would measure those too and would break whenever an unrelated suite adds a
    rule — which is exactly how this file failed in reverse order before it
    filtered.
    """
    return [
        c for c in body["changes"]
        if c["subject"] == code
        and (category is None or c["category"] == category)
        and (kind is None or c["kind"] == kind)
    ]


async def _open_thread(client, uid, analysis_id, *, subject, decision=None):
    from app.services.ioe.domain.scenario import ScenarioSpec
    from app.services.ioe.scenario.service import ScenarioService

    outcome = await ScenarioService(uid).simulate(
        analysis_id,
        ScenarioSpec.parse(
            [{"lever_code": RRSP, "parameters": {"amount": Decimal("10000")}}]))
    created = await client.post(JOURNAL_API, headers=_auth(uid), json={
        "scenario_id": str(outcome.scenario_id),
        "request_id": str(uuid.uuid4()),
        "subject_opportunity_code": subject})
    assert created.status_code == 201, created.text
    journal_id = created.json()["id"]
    if decision is not None:
        response = await client.post(
            f"{JOURNAL_API}/{journal_id}/decision", headers=_auth(uid),
            json={"decision": decision, "request_id": str(uuid.uuid4())})
        assert response.status_code == 201, response.text
    return journal_id


# ===========================================================================
# §50 / §26 — first use
# ===========================================================================
@pytest.mark.asyncio
async def test_first_use_reports_no_baseline_and_no_arrivals(client):
    """THE FIRST-USE ACCEPTANCE. A user with real opportunities and nothing
    acknowledged must not be told everything they own just happened."""
    uid, _, _ = await _ready()

    body = (await client.get(_url(), headers=_auth(uid))).json()

    assert body["schema_version"] == RETENTION_CHANGES_SCHEMA_VERSION
    assert body["baseline_status"] == "NO_BASELINE"
    assert body["baseline_checkpoint"] is None
    assert body["changes"] == []
    assert body["summary"]["total"] == 0
    assert body["summary"]["opportunities_added"] == 0
    assert body["current_snapshot_hash"], "no token to acknowledge with"


@pytest.mark.asyncio
async def test_acknowledging_first_use_establishes_a_baseline_then_silence(client):
    uid, _, _ = await _ready()

    created = await _acknowledge(client, uid)

    checkpoint = created.json()
    assert checkpoint["snapshot_schema_version"] == RETENTION_SNAPSHOT_SCHEMA_VERSION
    assert checkpoint["supersedes_checkpoint_id"] is None
    assert checkpoint["tax_year"] == TAX_YEAR

    after = (await client.get(_url(), headers=_auth(uid))).json()
    assert after["baseline_status"] == "ESTABLISHED"
    assert after["baseline_checkpoint"]["id"] == checkpoint["id"]
    assert after["changes"] == [], "an unchanged world reported changes"


# ===========================================================================
# §3 — a read must never advance the baseline
# ===========================================================================
@pytest.mark.asyncio
async def test_reading_ten_times_never_advances_the_baseline(client):
    """The invariant the whole design turns on: if a GET wrote, a background
    refresh could erase changes the user never saw."""
    uid, analysis_id, code = await _ready()
    await _acknowledge(client, uid)
    await _open_thread(client, uid, analysis_id, subject=code,
                       decision="PROCEED")

    bodies = [(await client.get(_url(), headers=_auth(uid))).json()
              for _ in range(10)]

    assert all(b == bodies[0] for b in bodies), (
        "repeated reads disagreed; something advanced between them")
    assert bodies[0]["changes"], "the control produced no change to preserve"

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        count = await s.scalar(text(
            "SELECT count(*) FROM ioe.retention_checkpoint WHERE user_id = :u"
        ), {"u": str(uid)})
    assert count == 1, f"{count} checkpoints exist; a GET wrote one"


# ===========================================================================
# §48 — the acknowledgement workflow, end to end
# ===========================================================================
@pytest.mark.asyncio
async def test_changes_are_relative_to_the_latest_checkpoint(client):
    """A -> F of §48: acknowledge C1, change state, read twice, acknowledge C2,
    read again, then change again and confirm the baseline really moved."""
    uid, analysis_id, code = await _ready()
    first = (await _acknowledge(client, uid)).json()

    await _open_thread(client, uid, analysis_id, subject=code,
                       decision="DEFER")

    seen_once = (await client.get(_url(), headers=_auth(uid))).json()
    seen_twice = (await client.get(_url(), headers=_auth(uid))).json()
    assert seen_once == seen_twice
    assert [c["category"] for c in seen_once["changes"]] == ["DECISION"]

    second = (await _acknowledge(client, uid)).json()
    assert second["supersedes_checkpoint_id"] == first["id"]

    settled = (await client.get(_url(), headers=_auth(uid))).json()
    assert settled["changes"] == [], "acknowledged changes came back"
    assert settled["baseline_checkpoint"]["id"] == second["id"]

    # A further change is measured from C2, not C1 — so the DEFER that C2
    # already covers must not reappear alongside the new decision.
    await _open_thread(client, uid, analysis_id, subject=code,
                       decision="PROCEED")
    latest = (await client.get(_url(), headers=_auth(uid))).json()
    (change,) = latest["changes"]
    assert change["category"] == "DECISION"
    assert change["transitions"][0]["before"] == "DEFER", (
        "the comparison ran against C1; the baseline never moved")
    assert change["transitions"][0]["after"] == "PROCEED"


# ===========================================================================
# §49 — the acknowledgement race
# ===========================================================================
@pytest.mark.asyncio
async def test_acknowledging_a_state_that_has_moved_is_refused(client):
    """THE RACE ACCEPTANCE. The client read S1, the world became S2, and the
    client tries to acknowledge S1. Accepting would record that the user
    reviewed a change they never saw."""
    uid, analysis_id, code = await _ready()
    await _acknowledge(client, uid)
    stale = (await client.get(_url(), headers=_auth(uid))).json()

    await _open_thread(client, uid, analysis_id, subject=code,
                       decision="PROCEED")

    refused = await client.post(_ack_url(), headers=_auth(uid), json={
        "snapshot_hash": stale["current_snapshot_hash"],
        "baseline_checkpoint_id": stale["baseline_checkpoint"]["id"],
        "request_id": str(uuid.uuid4())})

    assert refused.status_code == 409, refused.text
    assert refused.json()["error_code"] == "CURRENT_STATE_CHANGED_SINCE_READ"

    # S2 must remain unacknowledged.
    still = (await client.get(_url(), headers=_auth(uid))).json()
    assert still["changes"], "the refused acknowledgement swallowed the change"


@pytest.mark.asyncio
async def test_a_second_tab_cannot_acknowledge_from_a_superseded_baseline(client):
    """Two tabs, one baseline. The first wins; the second is told why rather
    than silently appending a second checkpoint from a stale predecessor."""
    uid, analysis_id, code = await _ready()
    await _acknowledge(client, uid)
    await _open_thread(client, uid, analysis_id, subject=code,
                       decision="PROCEED")

    tab_a = (await client.get(_url(), headers=_auth(uid))).json()
    tab_b = dict(tab_a)                      # both tabs read the same state

    first = await client.post(_ack_url(), headers=_auth(uid), json={
        "snapshot_hash": tab_a["current_snapshot_hash"],
        "baseline_checkpoint_id": tab_a["baseline_checkpoint"]["id"],
        "request_id": str(uuid.uuid4())})
    assert first.status_code == 201, first.text

    second = await client.post(_ack_url(), headers=_auth(uid), json={
        "snapshot_hash": tab_b["current_snapshot_hash"],
        "baseline_checkpoint_id": tab_b["baseline_checkpoint"]["id"],
        "request_id": str(uuid.uuid4())})

    assert second.status_code == 409, second.text
    assert second.json()["error_code"] == "BASELINE_ALREADY_SUPERSEDED"


@pytest.mark.asyncio
async def test_the_chain_constraint_refuses_a_duplicate_supersession(client):
    """The service check and the database constraint are not redundant: two
    requests can pass the check concurrently and only one may land. Inserting
    directly proves the constraint, not just the branch above it."""
    uid, _, _ = await _ready()
    first = (await _acknowledge(client, uid)).json()
    await _acknowledge(client, uid)          # supersedes `first`

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            s.add(RetentionCheckpoint(
                user_id=uid, tax_year=TAX_YEAR,
                snapshot_schema_version=RETENTION_SNAPSHOT_SCHEMA_VERSION,
                snapshot={}, snapshot_hash="x" * 64,
                evaluated_as_of=date.fromisoformat(NORMAL_AS_OF),
                supersedes_checkpoint_id=uuid.UUID(first["id"]),
                request_id=uuid.uuid4()))
            await s.flush()


@pytest.mark.asyncio
async def test_two_first_baselines_cannot_both_land(client):
    """NULLS NOT DISTINCT is what extends the chain guarantee to the very first
    baseline, where the superseded id is NULL and Postgres would otherwise
    treat every NULL as unique."""
    uid, _, _ = await _ready()
    await _acknowledge(client, uid)

    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            s.add(RetentionCheckpoint(
                user_id=uid, tax_year=TAX_YEAR,
                snapshot_schema_version=RETENTION_SNAPSHOT_SCHEMA_VERSION,
                snapshot={}, snapshot_hash="y" * 64,
                evaluated_as_of=date.fromisoformat(NORMAL_AS_OF),
                supersedes_checkpoint_id=None,
                request_id=uuid.uuid4()))
            await s.flush()


# ===========================================================================
# §27 — idempotency
# ===========================================================================
@pytest.mark.asyncio
async def test_a_retried_acknowledgement_returns_the_same_checkpoint(client):
    uid, _, _ = await _ready()
    seen = (await client.get(_url(), headers=_auth(uid))).json()
    body = {
        "snapshot_hash": seen["current_snapshot_hash"],
        "baseline_checkpoint_id": None,
        "request_id": str(uuid.uuid4()),
    }

    first = await client.post(_ack_url(), headers=_auth(uid), json=body)
    second = await client.post(_ack_url(), headers=_auth(uid), json=body)

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        count = await s.scalar(text(
            "SELECT count(*) FROM ioe.retention_checkpoint WHERE user_id = :u"
        ), {"u": str(uid)})
    assert count == 1, "a retry created a second checkpoint"


# ===========================================================================
# §15 / §47 — time alone, on the wire
# ===========================================================================
@pytest.mark.asyncio
async def test_a_band_transition_needs_no_data_mutation(client):
    """§15: the same stored state read on a later date must produce a real
    timing change. Nothing in the database moved between these two reads."""
    uid, _, code = await _ready()
    await _acknowledge(client, uid, as_of=NORMAL_AS_OF)

    later = (await client.get(
        _url(as_of=URGENT_AS_OF), headers=_auth(uid))).json()

    # Scoped to THIS suite's own opportunity. Rules are published globally and
    # other suites' fixtures share this deadline date, so their opportunities
    # cross the same bands on the same day — a count over every change would
    # measure them too and would move whenever another suite is added.
    (change,) = _mine(later, code, category="TIMING")
    (transition,) = [t for t in change["transitions"] if t["field"] == "timing"]
    assert transition["before"] == "NORMAL"
    assert transition["after"] == "URGENT"
    assert change["severity"] == "HIGH"
    assert later["summary"]["newly_urgent"] >= 1


@pytest.mark.asyncio
async def test_an_expiry_is_critical_and_is_not_a_removal(client):
    uid, _, code = await _ready()
    await _acknowledge(client, uid, as_of=NORMAL_AS_OF)

    later = (await client.get(
        _url(as_of=EXPIRED_AS_OF), headers=_auth(uid))).json()

    (change,) = _mine(later, code, category="TIMING")
    assert change["severity"] == "CRITICAL"
    assert later["summary"]["newly_expired"] >= 1
    assert later["summary"]["opportunities_removed"] == 0, (
        "an expiry was reported as a disappearance")
    assert _mine(later, code, kind="REMOVED") == [], (
        "the expired opportunity was also reported as removed")


@pytest.mark.asyncio
async def test_a_day_inside_one_band_produces_nothing(client):
    """The §47 acceptance on the wire: two dates 24h apart, same band.

    Scoped to this suite's own opportunity for the same reason as the band
    tests — a global count would silently depend on every other suite's
    deadline dates. The unconditional form of this acceptance lives in the
    unit suite, where the snapshots are synthetic and cannot drift.
    """
    uid, _, code = await _ready()
    await _acknowledge(client, uid, as_of="2025-10-01")

    later = (await client.get(
        _url(as_of="2025-10-02"), headers=_auth(uid))).json()

    assert _mine(later, code) == [], "a day passing was reported as news"


# ===========================================================================
# Journal-driven transitions through the lifecycle
# ===========================================================================
@pytest.mark.asyncio
async def test_a_decision_and_a_reported_action_are_separate_changes(client):
    uid, analysis_id, code = await _ready()
    await _acknowledge(client, uid)
    journal_id = await _open_thread(
        client, uid, analysis_id, subject=code, decision="PROCEED")
    response = await client.post(
        f"{JOURNAL_API}/{journal_id}/action-report", headers=_auth(uid),
        json={"request_id": str(uuid.uuid4()), "action_date": "2025-09-15"})
    assert response.status_code == 201, response.text

    body = (await client.get(_url(), headers=_auth(uid))).json()

    categories = {c["category"] for c in _mine(body, code)}
    assert categories == {"DECISION", "EXECUTION"}
    assert body["summary"]["execution_reports"] == 1
    assert "VERIFIED" not in response.text + str(body)


@pytest.mark.asyncio
async def test_the_journal_history_is_untouched_by_retention(client):
    """Retention consumes the Journal; it never becomes its authority."""
    uid, analysis_id, code = await _ready()
    journal_id = await _open_thread(
        client, uid, analysis_id, subject=code, decision="PROCEED")
    before = (await client.get(
        f"{JOURNAL_API}/{journal_id}", headers=_auth(uid))).json()

    await _acknowledge(client, uid)
    await client.get(_url(), headers=_auth(uid))

    after = (await client.get(
        f"{JOURNAL_API}/{journal_id}", headers=_auth(uid))).json()
    assert after == before


# ===========================================================================
# §35 — authority boundary, measured
# ===========================================================================
def _engine_bindings():
    """Every module actually holding a reference to the engine's `compute` —
    enumerated, because patching one import site while a caller binds another
    is how a counter test goes silently blind."""
    import sys

    from app.services.tax_engine.core.engine import compute as real

    return [
        module for name, module in list(sys.modules.items())
        if name.startswith("app.") and getattr(module, "compute", None) is real
    ]


@pytest.mark.asyncio
async def test_neither_reading_nor_acknowledging_runs_a_business_authority(client):
    """Zero engine runs and zero rule evaluations on both operations.

    Non-vacuous by construction: the same counters are installed around the
    fixture build first and are required to trip there.
    """
    import app.services.tax_engine.rules_service as rules_module
    from app.services.tax_engine.core.engine import compute as real_compute

    runs: list[str] = []
    evaluations: list[int] = []
    real_evaluate = rules_module.RulesEvaluatorService.evaluate

    def counting(inp):
        runs.append("compute")
        return real_compute(inp)

    async def counting_evaluate(self, *a, **kw):
        evaluations.append(1)
        return await real_evaluate(self, *a, **kw)

    bindings = _engine_bindings()
    assert bindings, "no module holds the engine's compute; the patch is blind"

    for module in bindings:
        module.compute = counting                                   # type: ignore[attr-defined]
    rules_module.RulesEvaluatorService.evaluate = counting_evaluate  # type: ignore[method-assign]
    try:
        uid, _, _ = await _ready()
        assert runs, "the compute counter never fired while producing the run"
        assert evaluations, "the rules counter never fired while producing the run"
        runs.clear()
        evaluations.clear()

        read = await client.get(_url(), headers=_auth(uid))
        acknowledged = await _acknowledge(client, uid)
    finally:
        for module in bindings:
            module.compute = real_compute                           # type: ignore[attr-defined]
        rules_module.RulesEvaluatorService.evaluate = real_evaluate  # type: ignore[method-assign]

    assert read.status_code == 200 and acknowledged.status_code == 201
    assert read.json()["current_snapshot_hash"], "no retention state was produced"
    assert runs == [], f"the engine ran {len(runs)} times"
    assert evaluations == [], f"rules evaluated {len(evaluations)} times"


@pytest.mark.asyncio
async def test_the_checkpoint_load_is_one_statement_and_does_not_scan_history(
    client,
):
    """Resolving the latest checkpoint must stay O(1) as history accumulates.
    Five checkpoints must cost exactly what one costs."""
    from app.database.session import engine

    async def statements_for(uid: uuid.UUID) -> int:
        seen: list[str] = []

        def record(conn, cur, statement, parameters, context, executemany):
            seen.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record)
        try:
            assert (await client.get(
                _url(), headers=_auth(uid))).status_code == 200
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record)
        return len(seen)

    # CHECKPOINT DEPTH IS THE ONLY VARIABLE. Acknowledging unchanged state
    # still supersedes, so history accumulates without touching the journal —
    # opening threads here would add journal rows to the lifecycle load and
    # measure those instead of the checkpoint lookup.
    shallow, _, _ = await _ready()
    await _acknowledge(client, shallow)

    deep, _, _ = await _ready()
    for _ in range(5):
        await _acknowledge(client, deep)

    one, many = await statements_for(shallow), await statements_for(deep)

    async with unit_of_work(user_id=deep, actor_type="user") as s:
        depth = await s.scalar(text(
            "SELECT count(*) FROM ioe.retention_checkpoint WHERE user_id = :u"
        ), {"u": str(deep)})
    assert depth == 5, f"the deep user has {depth} checkpoints; not a real test"
    print(f"\nretention SQL: 1_checkpoint={one} {depth}_checkpoints={many}")  # noqa: T201
    assert many == one, (
        f"{many - one} extra statements once history accumulated")


# ===========================================================================
# Tenancy, lifecycle and privacy
# ===========================================================================
@pytest.mark.asyncio
async def test_one_tenant_never_sees_anothers_baseline(client):
    owner, _, _ = await _ready()
    other, _, _ = await _ready()
    owner_checkpoint = (await _acknowledge(client, owner)).json()

    other_body = (await client.get(_url(), headers=_auth(other))).json()

    assert other_body["baseline_status"] == "NO_BASELINE", (
        "another tenant's acknowledgement established this user's baseline")
    assert owner_checkpoint["id"] not in (await client.get(
        _url(), headers=_auth(other))).text


@pytest.mark.asyncio
async def test_a_foreign_checkpoint_id_is_refused_without_an_existence_oracle(
    client,
):
    """Naming someone else's checkpoint and naming one that never existed must
    be indistinguishable."""
    owner, _, _ = await _ready()
    owner_checkpoint = (await _acknowledge(client, owner)).json()
    intruder, _, _ = await _ready()

    seen = (await client.get(_url(), headers=_auth(intruder))).json()
    foreign = await client.post(_ack_url(), headers=_auth(intruder), json={
        "snapshot_hash": seen["current_snapshot_hash"],
        "baseline_checkpoint_id": owner_checkpoint["id"],
        "request_id": str(uuid.uuid4())})
    nowhere = await client.post(_ack_url(), headers=_auth(intruder), json={
        "snapshot_hash": seen["current_snapshot_hash"],
        "baseline_checkpoint_id": str(uuid.uuid4()),
        "request_id": str(uuid.uuid4())})

    assert foreign.status_code == nowhere.status_code == 409
    a, b = foreign.json(), nowhere.json()
    a.pop("correlation_id", None)
    b.pop("correlation_id", None)
    assert a == b, "a foreign checkpoint answered differently from a missing one"


@pytest.mark.asyncio
async def test_an_account_past_its_deletion_cutoff_may_not_read_or_acknowledge(
    client,
):
    uid, _, _ = await _ready()
    await _acknowledge(client, uid)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await AccountLifecycleService(s).request_deletion(uid)

    read = await client.get(_url(), headers=_auth(uid))
    wrote = await client.post(_ack_url(), headers=_auth(uid), json={
        "snapshot_hash": "z" * 64, "baseline_checkpoint_id": None,
        "request_id": str(uuid.uuid4())})
    assert read.status_code == 403
    assert wrote.status_code == 403
    assert "changes" not in read.text


@pytest.mark.asyncio
async def test_the_checkpoint_cascade_is_structural(client):
    """§33: acknowledged snapshots die with the account.

    Asserted STRUCTURALLY, on the constraints themselves, because a
    count-after-deletion here would be vacuous: once the account row is gone
    every RLS-scoped read returns zero whether the rows survived or not, so
    such a test passes just as happily against a table with no cascade at all.

    The behavioural proof belongs to `tests/privacy`, which drives the real
    terminal-removal phases and enforces the cascade universe this table was
    added to. What is checked here is the thing this entry actually shipped:
    both FK edges cascade, so neither a checkpoint nor a successor that
    supersedes it can outlive its parent.
    """
    uid, _, _ = await _ready()
    await _acknowledge(client, uid)
    await _acknowledge(client, uid)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        mine = await s.scalar(text(
            "SELECT count(*) FROM ioe.retention_checkpoint WHERE user_id = :u"
        ), {"u": str(uid)})
        edges = (await s.execute(text("""
            SELECT confdeltype, confrelid::regclass::text
              FROM pg_constraint
             WHERE conrelid = 'ioe.retention_checkpoint'::regclass
               AND contype = 'f'
             ORDER BY confrelid::regclass::text
        """))).all()
    assert mine == 2, "no checkpoints exist; the structural claim is untested"

    # `confdeltype` is a "char" column, which the driver hands back as bytes.
    actions = {
        relation: action.decode() if isinstance(action, bytes) else action
        for action, relation in edges
    }
    assert actions == {
        "identity.user_account": "c",
        "ioe.retention_checkpoint": "c",
    }, f"a retention FK does not cascade: {actions}"


@pytest.mark.asyncio
async def test_the_checkpoint_table_is_registered_as_dying_with_the_account(
    client,
):
    """The registry is the deletion authority, and an unregistered table is how
    data quietly outlives an account. Asserting the classification here ties
    this entry's table to the certified universe rather than trusting it."""
    from tests.privacy.account_delete_registry import REGISTRY

    entry = REGISTRY["ioe.retention_checkpoint"]
    assert entry.classification == "LIVE_USER_DATA_DELETE"
    assert entry.depth == 1


@pytest.mark.asyncio
async def test_no_storage_identity_reaches_the_snapshot_or_the_changes(client):
    """§34: the stored bytes and the response both. Evidence is readiness."""
    uid, analysis_id = await _user_with_analysis()
    document_id, bucket, object_key = await _hold(uid)
    await _shared_rule()
    await OptimizationOrchestrator(uid).generate(analysis_id)

    response = await client.get(_url(), headers=_auth(uid))
    await _acknowledge(client, uid)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        stored = await s.scalar(text(
            "SELECT snapshot::text FROM ioe.retention_checkpoint "
            " WHERE user_id = :u"), {"u": str(uid)})

    for haystack, where in ((response.text, "the changes response"),
                            (stored, "the stored snapshot")):
        for secret, label in ((document_id, "document id"), (bucket, "bucket"),
                              (object_key, "object key")):
            assert secret not in haystack, f"{where} exposes a {label}"
    # Non-vacuous: the evidence axis really did resolve against that document.
    assert '"evidence": "READY"' in stored or '"evidence":"READY"' in stored


@pytest.mark.asyncio
async def test_the_response_makes_no_score_or_money_claim(client):
    uid, _, _ = await _ready()
    await _acknowledge(client, uid, as_of=NORMAL_AS_OF)
    text_body = (await client.get(
        _url(as_of=EXPIRED_AS_OF), headers=_auth(uid))).text.lower()

    for claim in ("retention_score", "priority_score", "urgency_score",
                  "missed", "lost", "savings", "you saved"):
        assert claim not in text_body, f"the contract asserts {claim}"


# ===========================================================================
# §55 — stored snapshots stay interpretable
# ===========================================================================
@pytest.mark.asyncio
async def test_a_stored_snapshot_is_read_back_verbatim_not_reconstructed(client):
    """A checkpoint must be compared as WRITTEN. If current state could refill
    it, the very changes this engine exists to report would vanish."""
    uid, analysis_id, code = await _ready()
    acknowledged = (await _acknowledge(client, uid)).json()

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        stored = await s.scalar(select(RetentionCheckpoint.snapshot).where(
            RetentionCheckpoint.id == uuid.UUID(acknowledged["id"])))

    await _open_thread(client, uid, analysis_id, subject=code,
                       decision="PROCEED")
    body = (await client.get(_url(), headers=_auth(uid))).json()
    assert body["changes"], "state did not actually move"

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        after = await s.scalar(select(RetentionCheckpoint.snapshot).where(
            RetentionCheckpoint.id == uuid.UUID(acknowledged["id"])))
    assert after == stored, "the acknowledged snapshot was rewritten"
    assert body["baseline_checkpoint"]["snapshot_hash"] == \
        acknowledged["snapshot_hash"]


@pytest.mark.asyncio
async def test_the_stored_snapshot_carries_its_own_schema_version(client):
    """§29/§30: the persisted bytes and the read contract version separately."""
    uid, _, _ = await _ready()
    checkpoint = (await _acknowledge(client, uid)).json()
    body = (await client.get(_url(), headers=_auth(uid))).json()

    assert checkpoint["snapshot_schema_version"] == RETENTION_SNAPSHOT_SCHEMA_VERSION
    assert body["schema_version"] == RETENTION_CHANGES_SCHEMA_VERSION

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        stored = await s.scalar(text(
            "SELECT snapshot->>'schema_version' FROM ioe.retention_checkpoint "
            " WHERE user_id = :u"), {"u": str(uid)})
    assert stored == RETENTION_SNAPSHOT_SCHEMA_VERSION, (
        "the stored bytes do not name the shape they were written in")


# ===========================================================================
# Determinism and surface
# ===========================================================================
@pytest.mark.asyncio
async def test_repeated_requests_return_the_same_body(client):
    uid, analysis_id, code = await _ready()
    await _acknowledge(client, uid)
    await _open_thread(client, uid, analysis_id, subject=code,
                       decision="PROCEED")

    bodies = [(await client.get(_url(), headers=_auth(uid))).json()
              for _ in range(3)]

    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]["changes"][0]["change_id"] == \
        bodies[2]["changes"][0]["change_id"], "change identity moved"
    assert "correlation_id" not in bodies[0]


@pytest.mark.asyncio
async def test_the_endpoints_are_published_in_the_openapi_document(client):
    document = (await client.get("/api/v1/openapi.json")).json()
    assert "get" in document["paths"]["/api/v1/ioe/changes"]
    assert "post" in document["paths"]["/api/v1/ioe/changes/acknowledge"]
    schema = document["components"]["schemas"]["RetentionChangesOut"]
    for field in ("schema_version", "tax_year", "as_of", "baseline_status",
                  "baseline_checkpoint", "current_snapshot_hash", "changes",
                  "summary"):
        assert field in schema["properties"], f"{field} missing from the schema"
