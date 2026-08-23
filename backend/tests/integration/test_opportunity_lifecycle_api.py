"""Entry: Opportunity Expiry / Decay + Lifecycle — the API over real state.

The unit suite proves the join over synthetic inputs. What is at stake here is
the request path: that the lifecycle is derived from a real optimization run
and the user's real journal threads, that no business authority executes on a
read, that the Journal load does not scale per opportunity, that an expired
window stays visible instead of vanishing, that a self-report never becomes
verification, and that the surface leaks neither another tenant's state nor any
storage identity.

Every test establishes its own user, rule, evidence, run and threads.
"""
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, select

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
from app.schemas.opportunity_lifecycle import OPPORTUNITY_LIFECYCLE_SCHEMA_VERSION
from app.services.ioe.domain.scenario import ScenarioSpec
from app.services.ioe.frozen.models import reconstruct_tax_input
from app.services.ioe.lifecycle.domain import LifecycleActionability
from app.services.ioe.orchestrator import OptimizationOrchestrator
from app.services.ioe.scenario.service import ScenarioService
from app.services.privacy.lifecycle import AccountLifecycleService
from app.services.state_graph.assurance import derive_assurance_map
from app.services.state_graph.contracts import NodeFreshness
from app.services.state_graph.service import TaxStateGraphService
from tests.conftest import frozen_snapshot, grant_required_legal

API = "/api/v1/ioe/opportunity-lifecycle"
JOURNAL_API = "/api/v1/ioe/decision-journal"
TAX_YEAR = 2025
RRSP = "INCREASE_RRSP_DEDUCTION"

#: Every fixture rule carries this governed deadline, so the three evaluation
#: dates below land in known bands without any test restating a threshold.
DEADLINE = date(2026, 4, 30)

#: The fixture rule's governed impact. Large on purpose — see `_published_rule`.
IMPACT = Decimal("250000")
AS_OF = "2025-10-01"          # 211 days out — NORMAL
URGENT_AS_OF = "2026-04-20"   #  10 days out — URGENT
EXPIRED_AS_OF = "2026-05-01"  #  -1 days out — EXPIRED


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _auth(user_id: uuid.UUID) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


def _url(*, tax_year: int = TAX_YEAR, as_of: str | None = AS_OF) -> str:
    base = f"{API}?tax_year={tax_year}"
    return f"{base}&as_of={as_of}" if as_of else base


# ------------------------------------------------------------------ fixtures
async def _user_with_analysis() -> tuple[uuid.UUID, uuid.UUID]:
    """A user with a completed analysis AND its line items — the latter because
    this suite seals scenarios as well as running the optimizer."""
    from app.services.tax_engine.core.engine import compute

    async with unit_of_work(actor_type="system") as s:
        account = UserAccount(
            email=f"lc_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(account)
        await s.flush()
        await grant_required_legal(s, account.id)
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
        result = compute(reconstruct_tax_input(payload))
        for i, item in enumerate(result.line_items):
            s.add(AnalysisLineItem(
                analysis_id=run.id, kind=item["kind"], label=item["label"],
                amount=item["amount"], sort_order=i))
        await s.flush()
        return uid, run.id


async def _published_rule(
    *, document_type_code: str = "T4", lever_code: str = RRSP,
) -> str:
    """This suite's OWN governed rule, with a required document and a hard
    deadline, so evidence readiness and timing both resolve against governed
    metadata. Returns the opportunity code the rules layer derives from it —
    a shared database accumulates other suites' rules, and "the first
    opportunity" is whichever residue sorts first.

    `lever_code` must name a REGISTERED lever. An unregistered one leaves the
    candidate with no lever application, which the optimizer excludes as
    NOT_PORTFOLIO_EVALUABLE and Assurance reports as BLOCKED — a governed
    state worth testing deliberately (see the BLOCKED case below), but a
    useless default, because BLOCKED outranks every axis this entry joins.

    IT ALSO CARRIES A DOMINANT IMPACT, and that is isolation, not decoration.
    Rules are published GLOBALLY while the optimizer's search budget is 200
    engine runs, so against a shared database this user accumulates hundreds
    of candidates — measured at 296 in a whole-directory run. Candidates are
    ranked by score, and a rule with no `impact_formula_id` carries zero
    economic value, sorts near the back, and is excluded as
    SEARCH_BUDGET_EXHAUSTED before the engine ever reaches it. That is a real
    governed exclusion, and it made every axis of this suite read BLOCKED in
    company while passing alone. A literal-valued formula puts this rule at
    the front of the ranking, where the budget cannot strand it.
    """
    async with unit_of_work(actor_type="admin") as s:
        jurisdiction = await s.scalar(
            select(Jurisdiction).where(Jurisdiction.code == "FED"))
        code = f"LIFECYCLE_{uuid.uuid4().hex[:6].upper()}"
        rule = TaxRule(code=code, name=f"{code} rule", category="deduction",
                       jurisdiction_id=jurisdiction.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=TAX_YEAR,
            effective_date=date(TAX_YEAR, 1, 1), status="published",
            description="lifecycle fixture",
            eligibility_basis_codes=["BASIS_LIFECYCLE"])
        s.add(version)
        await s.flush()
        formula = CalcFormula(
            code=f"LIFECYCLEIMPACT_{uuid.uuid4().hex[:8]}",
            expression="amount", expression_lang="rpn", output_unit="CAD",
            description="fixed governed impact, large enough to rank first")
        s.add(formula)
        await s.flush()
        s.add(CalcFormulaInput(
            formula_id=formula.id, param_name="amount",
            literal_value=IMPACT))
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            impact_formula_id=formula.id,
            title_template=f"{code} opportunity",
            economic_effect_type="current_year_tax_reduction",
            reversibility="reversible",
            portfolio_lever_code=lever_code,
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


async def _hold(uid: uuid.UUID, code: str = "T4") -> tuple[str, str, str]:
    async with unit_of_work(actor_type="system") as s:
        type_id = (await s.scalar(
            select(DocumentType).where(DocumentType.code == code))).id
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        document = Document(
            user_id=uid, document_type_id=type_id, tax_year=TAX_YEAR,
            bucket=f"onyx-bucket-{uuid.uuid4().hex}",
            object_key=f"{uid}/lifecycle/{uuid.uuid4()}",
            content_hash=uuid.uuid4().hex, status="processed")
        s.add(document)
        await s.flush()
        return str(document.id), document.bucket, document.object_key


#: The ONE governed rule every ordinary test in this module shares.
_SHARED_RULE: list[str] = []


async def _shared_rule() -> str:
    """Published once for the whole module, and that is the isolation.

    A fresh rule per test would compete WITH ITSELF: rules are global, so by
    the fifteenth test this user has fifteen identical top-ranked candidates
    all drawing on the same RRSP room. Exactly one is admitted and the rest are
    deferred as NO_STANDALONE_IMPROVEMENT_AT_THIS_POINT — and because ties
    break on a random opportunity code, the admitted one usually belongs to a
    different test. Measured: the fixture reached candidate_rank 19 and was
    deferred, which Assurance reports as BLOCKED.

    One shared rule means one candidate, deterministically admitted. Nothing
    else is shared: every test still builds its own user, evidence, run and
    journal threads. The rule is immutable published metadata, identical for
    every caller, so sharing it removes accumulation rather than creating
    coupling.
    """
    if not _SHARED_RULE:
        _SHARED_RULE.append(await _published_rule())
    return _SHARED_RULE[0]


async def _ready() -> tuple[uuid.UUID, uuid.UUID, str]:
    """Held evidence, a governed rule, and a completed optimization run — the
    ordinary production shape. Returns (uid, analysis_id, opportunity_code).

    The post-condition is the isolation guard. If a shared database ever
    strands this fixture behind the optimizer again, it would otherwise
    surface as an unexplained BLOCKED on every axis at once, in six tests,
    none of which mention the optimizer. Asserting it here fails once, early,
    and says why.
    """
    uid, analysis_id = await _user_with_analysis()
    await _hold(uid)
    code = await _shared_rule()
    await OptimizationOrchestrator(uid).generate(analysis_id)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        graph = await TaxStateGraphService(s, uid).build(tax_year=TAX_YEAR)
    (item,) = [
        i for i in derive_assurance_map(
            graph, as_of=date.fromisoformat(AS_OF)).opportunities
        if i.opportunity_code == code
    ]
    assert item.blocked_reason_code is None, (
        f"the fixture opportunity is governed-BLOCKED as "
        f"{item.blocked_reason_code!r} (rank {item.candidate_rank}) before any "
        "assertion runs, so every axis below would read BLOCKED for a reason "
        "that has nothing to do with the lifecycle. SEARCH_BUDGET_EXHAUSTED "
        "means globally published rules pushed it past the optimizer's 200-run "
        "budget — raise IMPACT. NO_STANDALONE_IMPROVEMENT_AT_THIS_POINT means "
        "it is competing with another candidate for the same lever resource — "
        "keep one shared rule per module. Do not weaken the assertions below.")
    return uid, analysis_id, code


async def _thread(
    client, uid: uuid.UUID, analysis_id: uuid.UUID, *,
    subject: str | None,
    decision: str | None = None,
    reported_on: date | None = None,
) -> str:
    """A real Journal thread through the real Journal API — never a hand-built
    row, so the lifecycle consumes what the certified writer actually stores."""
    outcome = await ScenarioService(uid).simulate(
        analysis_id,
        ScenarioSpec.parse(
            [{"lever_code": RRSP, "parameters": {"amount": Decimal("10000")}}]))
    body: dict = {
        "scenario_id": str(outcome.scenario_id), "request_id": str(uuid.uuid4())}
    if subject is not None:
        body["subject_opportunity_code"] = subject
    created = await client.post(JOURNAL_API, headers=_auth(uid), json=body)
    assert created.status_code == 201, created.text
    journal_id = created.json()["id"]

    if decision is not None:
        response = await client.post(
            f"{JOURNAL_API}/{journal_id}/decision", headers=_auth(uid),
            json={"decision": decision, "request_id": str(uuid.uuid4())})
        assert response.status_code == 201, response.text
    if reported_on is not None:
        response = await client.post(
            f"{JOURNAL_API}/{journal_id}/action-report", headers=_auth(uid),
            json={"request_id": str(uuid.uuid4()),
                  "action_date": reported_on.isoformat()})
        assert response.status_code == 201, response.text
    return journal_id


def _mine(body: dict, code: str) -> dict:
    (item,) = [i for i in body["opportunities"] if i["opportunity_code"] == code]
    return item


# ===========================================================================
# The contract
# ===========================================================================
@pytest.mark.asyncio
async def test_a_real_run_produces_the_lifecycle_contract(client):
    """THE CENTRAL ACCEPTANCE PROOF for the request path."""
    uid, _, code = await _ready()

    response = await client.get(_url(), headers=_auth(uid))

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["schema_version"] == OPPORTUNITY_LIFECYCLE_SCHEMA_VERSION
    assert body["tax_year"] == TAX_YEAR
    assert body["as_of"] == AS_OF
    assert body["graph_hash"]
    assert body["opportunity_authority"] == "READY"
    assert body["opportunities"], "a completed run produced no lifecycle items"

    item = _mine(body, code)
    # Seven axes, each present and each drawn from its own vocabulary. The
    # governed fixture is eligible, evidenced, and 211 days from its deadline,
    # so each axis is pinned exactly rather than to a set of allowed values.
    assert item["availability"] == "AVAILABLE"
    assert item["decision"] == "NO_DECISION"
    assert item["execution"] == "NOT_REPORTED"
    assert item["evidence"] == "READY"
    assert item["timing"] == "NORMAL"
    # Asserted against the graph's own enum, not a list retyped here.
    assert item["freshness"] in {f.value for f in NodeFreshness}
    assert item["integrity"]
    assert item["journal"] is None

    assert item["deadline"]["deadline_code"].startswith("DL_LIFECYCLE_")
    assert item["deadline"]["deadline_date"] == DEADLINE.isoformat()
    assert item["days_remaining"] == (DEADLINE - date.fromisoformat(AS_OF)).days

    ids = {i["source_id"] for i in body["opportunities"]}
    assert set(body["attention_order"]) == ids, (
        "the attention order and the item list disagree about what exists")
    assert body["summary"]["opportunity_count"] == len(body["opportunities"])
    assert body["summary"]["unlinked_thread_count"] == 0


@pytest.mark.asyncio
async def test_no_optimization_run_reads_unavailable_not_empty_success(client):
    """The empty-versus-missing rule, on the wire. Zero opportunities under no
    authority must say UNAVAILABLE — a bare empty list would read as 'you have
    no opportunities', which nobody determined."""
    uid, _ = await _user_with_analysis()

    body = (await client.get(_url(), headers=_auth(uid))).json()

    assert body["opportunities"] == []
    assert body["opportunity_authority"] == "UNAVAILABLE"
    assert body["opportunity_authority_reason"] == (
        "NO_OPTIMIZATION_RUN_FOR_TAX_YEAR")


# ===========================================================================
# The Journal join — the axes that only exist once user intent is in scope
# ===========================================================================
@pytest.mark.asyncio
async def test_a_thread_naming_an_opportunity_carries_its_decision(client):
    uid, analysis_id, code = await _ready()
    journal_id = await _thread(
        client, uid, analysis_id, subject=code, decision="PROCEED")

    item = _mine((await client.get(_url(), headers=_auth(uid))).json(), code)

    assert item["decision"] == "PROCEED"
    assert item["journal"]["journal_id"] == journal_id
    assert item["journal"]["thread_count"] == 1
    # PROCEED is intent. It is not execution, and it is not evidence.
    assert item["execution"] == "NOT_REPORTED"
    assert item["actionability"] != "ACTION_REPORTED"


@pytest.mark.asyncio
async def test_a_reported_action_with_missing_evidence_is_not_complete(client):
    """The state §8 requires to stay expressible: the user says they acted and
    the evidence is still not there. Neither fact may erase the other."""
    uid, analysis_id = await _user_with_analysis()
    # The SHARED rule, whose required T4 this user simply never holds — a
    # second evaluable rule would compete with it for the same lever room.
    code = await _shared_rule()
    await OptimizationOrchestrator(uid).generate(analysis_id)
    reported = date(2025, 9, 15)
    await _thread(client, uid, analysis_id, subject=code,
                  decision="PROCEED", reported_on=reported)

    body = (await client.get(_url(), headers=_auth(uid))).json()
    item = _mine(body, code)

    assert item["decision"] == "PROCEED"
    assert item["execution"] == "USER_REPORTED"
    assert item["evidence"] in ("MISSING", "PARTIAL", "UNKNOWN")
    assert item["actionability"] == "ACTION_REPORTED"
    assert item["last_reported_action_date"] == reported.isoformat()
    assert item["attention"]["needs_evidence"] is True, (
        "a reported action silenced the outstanding evidence gap")
    assert "USER_REPORTED_ACTION" in item["reason_codes"]
    assert "REQUIRED_EVIDENCE_INCOMPLETE" in item["reason_codes"]

    # §14: nothing governed defines completion, so no axis may claim it. Read
    # off the vocabularies and the item's own values — a substring sweep of the
    # payload would match REQUIRED_EVIDENCE_INCOMPLETE and assert its opposite.
    assert "COMPLETE" not in {v.value for v in LifecycleActionability}
    assert "COMPLETE" not in {
        item[axis] for axis in
        ("availability", "decision", "execution", "evidence", "timing",
         "actionability")}
    assert "COMPLETE" not in body["summary"]["by_actionability"]


@pytest.mark.asyncio
async def test_declining_does_not_make_an_opportunity_unavailable(client):
    """DECLINE is a statement about the user, not about eligibility. The
    governed availability must be unchanged by it."""
    uid, analysis_id, code = await _ready()
    before = _mine((await client.get(_url(), headers=_auth(uid))).json(), code)

    await _thread(client, uid, analysis_id, subject=code, decision="DECLINE")

    after = _mine((await client.get(_url(), headers=_auth(uid))).json(), code)
    assert after["availability"] == before["availability"]
    assert after["decision"] == "DECLINE"
    assert after["actionability"] == "DECLINED"
    assert "USER_DECLINED" in after["reason_codes"]
    assert after["timing"] == before["timing"], "a decline moved the clock"


@pytest.mark.asyncio
async def test_deferring_is_not_expiry(client):
    uid, analysis_id, code = await _ready()
    await _thread(client, uid, analysis_id, subject=code, decision="DEFER")

    item = _mine((await client.get(_url(), headers=_auth(uid))).json(), code)

    assert item["decision"] == "DEFER"
    assert item["actionability"] == "DEFERRED"
    assert item["timing"] == "NORMAL"
    assert item["attention"]["expired"] is False
    assert "GOVERNED_DEADLINE_PASSED" not in item["reason_codes"]


@pytest.mark.asyncio
async def test_a_thread_naming_no_opportunity_is_counted_not_dropped(client):
    uid, analysis_id, code = await _ready()
    await _thread(client, uid, analysis_id, subject=None, decision="PROCEED")

    body = (await client.get(_url(), headers=_auth(uid))).json()

    assert body["summary"]["unlinked_thread_count"] == 1
    assert _mine(body, code)["journal"] is None, (
        "an unlinked thread was attached to an opportunity it never named")
    assert _mine(body, code)["decision"] == "NO_DECISION"


@pytest.mark.asyncio
async def test_two_threads_on_one_opportunity_report_the_newest_and_say_so(client):
    uid, analysis_id, code = await _ready()
    await _thread(client, uid, analysis_id, subject=code, decision="DEFER")
    newest = await _thread(
        client, uid, analysis_id, subject=code, decision="PROCEED")

    item = _mine((await client.get(_url(), headers=_auth(uid))).json(), code)

    assert item["journal"]["journal_id"] == newest
    assert item["journal"]["thread_count"] == 2, (
        "the earlier thread was hidden rather than counted")
    assert item["decision"] == "PROCEED"


@pytest.mark.asyncio
async def test_a_governed_exclusion_outranks_every_other_axis(client):
    """BLOCKED means the user cannot act at all, so nothing below it in the
    precedence changes the next move. The other axes still report themselves —
    outranked is not overwritten.

    The exclusion is produced the way the optimizer really produces one: a rule
    naming a lever the registry does not know leaves the candidate with no
    lever application, which is excluded as NOT_PORTFOLIO_EVALUABLE.
    """
    uid, analysis_id = await _user_with_analysis()
    await _hold(uid)
    code = await _published_rule(lever_code="NOT_A_REGISTERED_LEVER")
    await OptimizationOrchestrator(uid).generate(analysis_id)
    await _thread(client, uid, analysis_id, subject=code, decision="PROCEED")

    item = _mine((await client.get(_url(), headers=_auth(uid))).json(), code)

    assert item["availability"] == "BLOCKED"
    assert item["actionability"] == "BLOCKED"
    assert "GOVERNED_EXCLUSION_APPLIES" in item["reason_codes"]
    assert item["attention"]["blocked"] is True
    # Outranked, not erased: the user's decision is still reported, and a
    # blocked opportunity does not ask for a decision it cannot use.
    assert item["decision"] == "PROCEED"
    assert item["journal"] is not None
    assert item["attention"]["needs_decision"] is False
    assert item["timing"] == "NORMAL", "the exclusion rewrote the clock"


# ===========================================================================
# Timing — the governed deadline, never a threshold restated here
# ===========================================================================
@pytest.mark.asyncio
async def test_as_of_moves_timing_and_only_timing(client):
    uid, _, code = await _ready()

    normal = (await client.get(_url(), headers=_auth(uid))).json()
    urgent = (await client.get(
        _url(as_of=URGENT_AS_OF), headers=_auth(uid))).json()

    assert normal["as_of"] == AS_OF and urgent["as_of"] == URGENT_AS_OF
    assert normal["graph_hash"] == urgent["graph_hash"]

    a, b = _mine(normal, code), _mine(urgent, code)
    assert a["timing"] == "NORMAL" and b["timing"] == "URGENT"
    assert b["attention"]["deadline_urgent"] is True
    assert a["attention"]["deadline_urgent"] is False
    # Every other axis is untouched by the clock.
    for axis in ("availability", "decision", "execution", "evidence",
                 "freshness", "integrity", "support", "candidate_rank",
                 "standalone_potential"):
        assert a[axis] == b[axis], f"{axis} moved with as_of"


@pytest.mark.asyncio
async def test_an_expired_opportunity_stays_visible(client):
    """§11: expired state is product information. A customer must be able to
    tell a closed window from one that never existed."""
    uid, _, code = await _ready()

    body = (await client.get(
        _url(as_of=EXPIRED_AS_OF), headers=_auth(uid))).json()
    item = _mine(body, code)

    assert item["timing"] == "EXPIRED"
    assert item["actionability"] == "EXPIRED"
    overdue = (DEADLINE - date.fromisoformat(EXPIRED_AS_OF)).days
    assert overdue < 0, "the fixture is not actually past its deadline"
    assert item["days_remaining"] == overdue
    assert item["attention"]["expired"] is True
    assert "GOVERNED_DEADLINE_PASSED" in item["reason_codes"]
    # Still carrying its governed figures — not a tombstone.
    assert item["deadline"]["deadline_date"] == DEADLINE.isoformat()
    assert item["support"]["display_support_score"]


@pytest.mark.asyncio
async def test_a_reported_action_outranks_expiry(client):
    """Labelling a window the user says they already used as EXPIRED would
    imply they missed it. Timing still reports the truth on its own axis."""
    uid, analysis_id, code = await _ready()
    await _thread(client, uid, analysis_id, subject=code,
                  decision="PROCEED", reported_on=date(2026, 4, 1))

    item = _mine((await client.get(
        _url(as_of=EXPIRED_AS_OF), headers=_auth(uid))).json(), code)

    assert item["actionability"] == "ACTION_REPORTED"
    assert item["timing"] == "EXPIRED", "the axis was rewritten, not outranked"
    assert item["attention"]["expired"] is True


# ===========================================================================
# Authority boundary — measured, not asserted
# ===========================================================================
def _engine_bindings():
    """Every module that actually holds a reference to the engine's `compute`.

    Enumerated from `sys.modules` rather than listed, because patching one
    import site while a caller binds another is how a counter test goes
    silently blind.
    """
    import sys

    from app.services.tax_engine.core.engine import compute as real

    return [
        module for name, module in list(sys.modules.items())
        if name.startswith("app.") and getattr(module, "compute", None) is real
    ]


@pytest.mark.asyncio
async def test_the_read_executes_no_business_authority(client):
    """Zero engine runs, zero rule evaluations on the read path.

    Non-vacuous by construction: the same counters are installed around the
    fixture build first, and the fixture is required to trip them. A counter
    that cannot detect real work proves nothing about its absence.
    """
    import app.services.tax_engine.rules_service as rules_module
    from app.services.tax_engine.core.engine import compute as real_compute

    runs: list[str] = []
    evaluations: list[int] = []
    real_evaluate = rules_module.RulesEvaluatorService.evaluate

    def counting(inp, dataset=None):
        # Mirrors `compute`'s signature exactly. A stub that takes fewer
        # arguments than the function it replaces fails on the call rather than
        # on the assertion, which reports a TypeError where this test means to
        # report "a business authority ran".
        runs.append("compute")
        return real_compute(inp, dataset)

    async def counting_evaluate(self, *a, **kw):
        evaluations.append(1)
        return await real_evaluate(self, *a, **kw)

    bindings = _engine_bindings()
    assert bindings, "no module holds the engine's compute; the patch is blind"

    for module in bindings:
        module.compute = counting                                   # type: ignore[attr-defined]
    rules_module.RulesEvaluatorService.evaluate = counting_evaluate  # type: ignore[method-assign]
    try:
        uid, analysis_id, code = await _ready()
        await _thread(client, uid, analysis_id, subject=code, decision="PROCEED")
        # The control: building the fixture DOES run both authorities.
        assert runs, "the compute counter never fired while producing the run"
        assert evaluations, "the rules counter never fired while producing the run"
        runs.clear()
        evaluations.clear()

        response = await client.get(_url(), headers=_auth(uid))
    finally:
        for module in bindings:
            module.compute = real_compute                           # type: ignore[attr-defined]
        rules_module.RulesEvaluatorService.evaluate = real_evaluate  # type: ignore[method-assign]

    assert response.status_code == 200
    assert response.json()["opportunities"], "no lifecycle was produced"
    assert runs == [], f"the engine ran {len(runs)} times on a lifecycle read"
    assert evaluations == [], f"rules evaluated {len(evaluations)} times"


@pytest.mark.asyncio
async def test_nothing_after_the_journal_load_touches_the_database(client):
    """The read loads the graph, then the journals, then derives purely.

    Unlike the Assurance map, this path legitimately queries after the graph
    build — the Journal is its second source. So the boundary measured here is
    the LAST load: zero statements after `list_journals` returns.
    """
    from app.services.ioe.journal.service import DecisionJournalService

    uid, analysis_id, code = await _ready()
    await _thread(client, uid, analysis_id, subject=code, decision="PROCEED")

    statements: list[str] = []
    at_journal_return: list[int] = []
    real_list = DecisionJournalService.list_journals

    async def recording_list(self, *a, **kw):
        loaded = await real_list(self, *a, **kw)
        at_journal_return.append(len(statements))
        return loaded

    def record(conn, cur, statement, parameters, context, executemany):
        statements.append(statement.lower())

    from app.database.session import engine

    DecisionJournalService.list_journals = recording_list  # type: ignore[method-assign]
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        response = await client.get(_url(), headers=_auth(uid))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
        DecisionJournalService.list_journals = real_list    # type: ignore[method-assign]

    assert response.status_code == 200
    assert at_journal_return, "the journal load was never reached"
    total = len(statements)
    post_load = total - at_journal_return[0]
    body = response.json()
    print(f"\nlifecycle API SQL: total={total} "                      # noqa: T201
          f"through-journal-load={at_journal_return[0]} post-load={post_load} "
          f"items={len(body['opportunities'])} "
          f"payload_bytes={len(response.content)}")
    assert post_load == 0, (
        f"{post_load} statements after the journal load: "
        f"{statements[at_journal_return[0]:][:3]}")


@pytest.mark.asyncio
async def test_the_read_does_not_scale_with_thread_count(client):
    """One thread or four, the request costs the same number of statements. A
    per-thread event lookup — or a per-thread scenario load — would show here
    as a rising count.

    THREAD COUNT IS THE ONLY VARIABLE. Both users are optimized against the
    identical published-rule set, because `_published_rule` publishes globally
    and calling it twice would give the second user more opportunities — which
    would confound an N+1 in the Journal with an N+1 over opportunities. The
    opportunity counts are asserted equal below so the control is visible
    rather than assumed.
    """
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

    code = await _shared_rule()
    one_uid, one_analysis = await _user_with_analysis()
    many_uid, many_analysis = await _user_with_analysis()
    await _hold(one_uid)
    await _hold(many_uid)
    # Both runs see the same rule set — no rule is published between them.
    await OptimizationOrchestrator(one_uid).generate(one_analysis)
    await OptimizationOrchestrator(many_uid).generate(many_analysis)

    await _thread(client, one_uid, one_analysis, subject=code,
                  decision="PROCEED")
    for decision in ("PROCEED", "DEFER", "DECLINE", "CONSIDERING"):
        await _thread(client, many_uid, many_analysis, subject=code,
                      decision=decision)

    one, many = await statements_for(one_uid), await statements_for(many_uid)

    one_body = (await client.get(_url(), headers=_auth(one_uid))).json()
    many_body = (await client.get(_url(), headers=_auth(many_uid))).json()
    assert (len(one_body["opportunities"])
            == len(many_body["opportunities"])), (
        "the two users hold different opportunity counts; the comparison would "
        "measure opportunities, not threads")
    assert _mine(one_body, code)["journal"]["thread_count"] == 1
    assert _mine(many_body, code)["journal"]["thread_count"] == 4, (
        "the four threads were never created; the comparison is vacuous")
    print(f"\nlifecycle SQL by threads: 1_thread={one} 4_threads={many} "  # noqa: T201
          f"opportunities={len(one_body['opportunities'])}")
    assert many == one, (
        f"{many - one} extra statements for three extra threads (N+1)")


# ===========================================================================
# Tenancy and lifecycle
# ===========================================================================
@pytest.mark.asyncio
async def test_one_tenants_lifecycle_never_contains_anothers_state(client):
    owner, owner_analysis, owner_code = await _ready()
    owner_journal = await _thread(
        client, owner, owner_analysis, subject=owner_code, decision="PROCEED")
    other, _ = await _user_with_analysis()

    owner_body = (await client.get(_url(), headers=_auth(owner))).json()
    other_body = (await client.get(_url(), headers=_auth(other))).json()

    assert owner_body["opportunities"], "the control produced nothing"
    assert other_body["opportunities"] == []
    assert owner_journal not in (await client.get(
        _url(), headers=_auth(other))).text, "a foreign journal id leaked"
    assert other_body["summary"]["unlinked_thread_count"] == 0
    assert owner_body["graph_hash"] != other_body["graph_hash"]


@pytest.mark.asyncio
async def test_a_foreign_thread_never_decides_another_users_opportunity(client):
    """Both users hold the SAME opportunity code — the join key. Only the
    caller's own thread may reach their lifecycle."""
    code = await _shared_rule()
    decider, decider_analysis = await _user_with_analysis()
    bystander, bystander_analysis = await _user_with_analysis()
    await _hold(decider)
    await _hold(bystander)
    await OptimizationOrchestrator(decider).generate(decider_analysis)
    await OptimizationOrchestrator(bystander).generate(bystander_analysis)
    await _thread(client, decider, decider_analysis, subject=code,
                  decision="DECLINE")

    mine = _mine((await client.get(_url(), headers=_auth(decider))).json(), code)
    theirs = _mine(
        (await client.get(_url(), headers=_auth(bystander))).json(), code)

    assert mine["decision"] == "DECLINE" and mine["journal"] is not None
    assert theirs["decision"] == "NO_DECISION", (
        "another user's decision reached this opportunity")
    assert theirs["journal"] is None


@pytest.mark.asyncio
async def test_an_account_past_its_deletion_cutoff_may_not_read_the_lifecycle(
    client,
):
    uid, _, _ = await _ready()
    assert (await client.get(_url(), headers=_auth(uid))).status_code == 200

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await AccountLifecycleService(s).request_deletion(uid)

    after = await client.get(_url(), headers=_auth(uid))
    assert after.status_code == 403
    assert "opportunities" not in after.text


# ===========================================================================
# Determinism
# ===========================================================================
@pytest.mark.asyncio
async def test_repeated_requests_return_the_same_body(client):
    uid, analysis_id, code = await _ready()
    await _thread(client, uid, analysis_id, subject=code, decision="PROCEED")

    bodies = [
        (await client.get(_url(), headers=_auth(uid))).json() for _ in range(3)]

    assert bodies[0] == bodies[1] == bodies[2]
    assert "correlation_id" not in bodies[0]


@pytest.mark.asyncio
async def test_omitting_as_of_uses_today_and_says_so(client):
    uid, _, _ = await _ready()
    body = (await client.get(_url(as_of=None), headers=_auth(uid))).json()
    today = datetime.now(tz=UTC).date()
    # Either side of a midnight boundary during the request is acceptable.
    assert body["as_of"] in (
        today.isoformat(), (today - timedelta(days=1)).isoformat())


@pytest.mark.asyncio
async def test_a_rule_published_after_the_run_changes_nothing(client):
    """Current mode reads the run's sealed candidates, never the latest rules.
    A rule published afterwards must not conjure a lifecycle item."""
    uid, _, _ = await _ready()
    before = (await client.get(_url(), headers=_auth(uid))).json()

    # Published with an UNREGISTERED lever, so it cannot become an evaluable
    # competitor for later tests. The point stands either way: a rule that
    # appears after the run must not change a sealed answer.
    await _published_rule(document_type_code="RRSP",
                          lever_code="NOT_A_REGISTERED_LEVER")

    assert (await client.get(_url(), headers=_auth(uid))).json() == before


# ===========================================================================
# Privacy and product surface
# ===========================================================================
@pytest.mark.asyncio
async def test_no_storage_identity_reaches_the_lifecycle(client):
    uid, analysis_id = await _user_with_analysis()
    document_id, bucket, object_key = await _hold(uid)
    code = await _shared_rule()
    await OptimizationOrchestrator(uid).generate(analysis_id)

    response = await client.get(_url(), headers=_auth(uid))
    text = response.text

    for secret, label in ((document_id, "document id"), (bucket, "bucket"),
                          (object_key, "object key")):
        assert secret not in text, f"the lifecycle exposes a {label}"
    assert "docs.document" not in text
    # Non-vacuous: the evidence axis for this opportunity really did resolve
    # against that held document, so the absences above are not absence of
    # evidence handling.
    assert _mine(response.json(), code)["evidence"] == "READY"


@pytest.mark.asyncio
async def test_the_lifecycle_makes_no_score_or_loss_claim(client):
    """§11 and §12 on the wire: no decay score, and no money the user 'lost'."""
    uid, analysis_id, code = await _ready()
    await _thread(client, uid, analysis_id, subject=code, decision="DECLINE")

    text = (await client.get(
        _url(as_of=EXPIRED_AS_OF), headers=_auth(uid))).text.lower()

    for claim in ("decay_score", "opportunity_score", "urgency_score",
                  "priority_score", "missed", "lost", "you lost",
                  '"savings"', '"best', '"recommended', '"guaranteed'):
        assert claim not in text, f"the contract asserts {claim}"


@pytest.mark.asyncio
async def test_the_endpoint_is_published_in_the_openapi_document(client):
    document = (await client.get("/api/v1/openapi.json")).json()
    path = document["paths"]["/api/v1/ioe/opportunity-lifecycle"]
    assert "get" in path
    schema = document["components"]["schemas"]["OpportunityLifecycleOut"]
    for field in ("schema_version", "tax_year", "as_of", "graph_hash",
                  "opportunity_authority", "opportunity_authority_reason",
                  "opportunities", "attention_order", "summary"):
        assert field in schema["properties"], f"{field} missing from the schema"
    item = document["components"]["schemas"]["OpportunityLifecycleItemOut"]
    for axis in ("availability", "decision", "execution", "evidence", "timing",
                 "freshness", "integrity"):
        assert axis in item["properties"], f"the {axis} axis is unpublished"
