"""Entry: Tax Assurance Map — the product API over real governed state.

The unit suite proves the derivation over synthetic graphs. What is at stake
here is the request path: that the map is derived from a real optimization
run's sealed candidates, that no business authority executes on a read, that
nothing after the graph build touches the database, that absence of a run
reads UNAVAILABLE rather than as an empty success, and that the surface leaks
neither another tenant's state nor any storage identity.

Every test establishes its own rule, evidence and run state.
"""
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.core.security.jwt import create_access_token
from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
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
from app.schemas.assurance import TAX_ASSURANCE_SCHEMA_VERSION
from app.services.ioe.orchestrator import OptimizationOrchestrator
from app.services.privacy.lifecycle import AccountLifecycleService
from tests.conftest import frozen_snapshot

API = "/api/v1/ioe/assurance"
TAX_YEAR = 2025
AS_OF = "2026-03-01"


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


def _suffix() -> str:
    return uuid.uuid4().hex[:6].upper()


async def _user_with_analysis(employment: str = "95000"):
    async with unit_of_work(actor_type="system") as s:
        account = UserAccount(
            email=f"assure_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(account)
        await s.flush()
        uid = account.id
        income_type_id = (await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment"))).id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=TAX_YEAR, income_type_id=income_type_id,
            amount=Decimal(employment), province_code="ON"))
        run = AnalysisRun(
            user_id=uid, tax_year=TAX_YEAR, province_code="ON",
            engine_version="py-1.0.0", status="completed",
            started_at=datetime.now(tz=UTC), completed_at=datetime.now(tz=UTC),
            data_verified=True)
        s.add(run)
        await s.flush()
        payload, digest = frozen_snapshot(employment_income=Decimal(employment))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=payload, snapshot_hash=digest))
        await s.flush()
        return uid, run.id


async def _published_rule(
    *, document_type_code: str = "T4",
    deadline_date: date = date(2026, 4, 30),
) -> uuid.UUID:
    """This test's OWN governed rule, with a required document AND a deadline,
    so readiness and urgency both resolve against governed metadata."""
    async with unit_of_work(actor_type="admin") as s:
        jurisdiction = await s.scalar(
            select(Jurisdiction).where(Jurisdiction.code == "FED"))
        code = f"ASSURE_{_suffix()}"
        rule = TaxRule(code=code, name=f"{code} rule", category="deduction",
                       jurisdiction_id=jurisdiction.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=TAX_YEAR,
            effective_date=date(TAX_YEAR, 1, 1), status="published",
            description="assurance fixture",
            eligibility_basis_codes=["BASIS_ASSURE"])
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            title_template=f"{code} opportunity",
            economic_effect_type="current_year_tax_reduction",
            reversibility="reversible",
            portfolio_lever_code="rrsp_contribution",
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
            deadline_date=deadline_date, is_hard=True,
            jurisdiction_code="FED"))
        await s.flush()
        # The rules layer derives opportunity_code from the rule code,
        # lowercased. Returned so a test can find ITS OWN item — a shared
        # database accumulates other tests' published rules, and "the first
        # opportunity" is whichever residue rule sorts first.
        return code.lower()


async def _hold(uid: uuid.UUID, code: str = "T4") -> tuple[uuid.UUID, str, str]:
    async with unit_of_work(actor_type="system") as s:
        type_id = (await s.scalar(
            select(DocumentType).where(DocumentType.code == code))).id
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        document = Document(
            user_id=uid, document_type_id=type_id, tax_year=TAX_YEAR,
            bucket=f"onyx-bucket-{uuid.uuid4().hex}",
            object_key=f"{uid}/assure/{uuid.uuid4()}",
            content_hash=uuid.uuid4().hex, status="processed")
        s.add(document)
        await s.flush()
        return document.id, document.bucket, document.object_key


async def _ready_user():
    """A user with an analysis, held evidence, a governed rule with a deadline,
    and a completed optimization run — the ordinary production shape.

    Returns this test's own opportunity code alongside the ids: assertions
    about "the" item must name it, never take the first of a list other
    tests' residue rules may also populate."""
    uid, analysis_id = await _user_with_analysis()
    await _hold(uid)
    code = await _published_rule()
    await OptimizationOrchestrator(uid).generate(analysis_id)
    return uid, analysis_id, code


def _mine(body: dict, code: str) -> dict:
    (item,) = [i for i in body["opportunities"] if i["opportunity_code"] == code]
    return item


# ===========================================================================
# The contract
# ===========================================================================
@pytest.mark.asyncio
async def test_a_real_run_produces_the_assurance_contract(client):
    """THE CENTRAL ACCEPTANCE PROOF for the request path."""
    uid, _, code = await _ready_user()

    response = await client.get(_url(), headers=_auth(uid))

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["schema_version"] == TAX_ASSURANCE_SCHEMA_VERSION
    assert body["view"] == "current"
    assert body["tax_year"] == TAX_YEAR
    assert body["as_of"] == AS_OF
    assert body["graph_hash"]

    assert body["opportunities"], "a completed run produced no assurance items"
    families = {f["family"]: f for f in body["families"]}
    assert families["OPPORTUNITY"]["status"] == "READY"
    assert families["OPPORTUNITY"]["item_count"] == len(body["opportunities"])

    item_ids = {i["source_id"] for i in body["opportunities"]}
    assert set(body["attention"]) == item_ids, (
        "the attention queue and the item list disagree about what exists")
    assert body["summary"]["opportunity_count"] == len(body["opportunities"])


@pytest.mark.asyncio
async def test_items_carry_governed_state_not_recomputed_state(client):
    uid, _, code = await _ready_user()

    body = (await client.get(_url(), headers=_auth(uid))).json()
    item = _mine(body, code)

    assert item["eligibility_status"] in (
        "eligible", "conditionally_eligible", "indeterminate")
    assert item["evidence_readiness"] in (
        "READY", "PARTIAL", "MISSING", "NOT_REQUIRED", "UNKNOWN")
    assert item["status"] in (
        "READY", "EVIDENCE_REQUIRED", "REVIEW_REQUIRED", "BLOCKED")
    assert item["support"]["disclaimer"], (
        "support traveled without its disclaimer")
    # The governed deadline this test's own rule declared, resolved and dated.
    assert item["deadline"] is not None
    assert item["deadline"]["days_remaining"] == (
        date(2026, 4, 30) - date.fromisoformat(AS_OF)).days


@pytest.mark.asyncio
async def test_no_optimization_run_reads_unavailable_not_empty_success(client):
    """The empty-versus-missing rule, on the wire. Zero opportunities under no
    authority must say UNAVAILABLE — a bare empty list would read as 'you have
    no opportunities', which nobody determined."""
    uid, _ = await _user_with_analysis()

    body = (await client.get(_url(), headers=_auth(uid))).json()

    families = {f["family"]: f for f in body["families"]}
    assert families["OPPORTUNITY"]["status"] == "UNAVAILABLE"
    assert families["OPPORTUNITY"]["reason_code"] == (
        "NO_OPTIMIZATION_RUN_FOR_TAX_YEAR")
    assert body["opportunities"] == []
    assert families["TAX_STATE"]["status"] == "READY"


# ===========================================================================
# Tenancy and lifecycle
# ===========================================================================
@pytest.mark.asyncio
async def test_one_tenants_map_never_contains_anothers_state(client):
    owner, _, _ = await _ready_user()
    other, _ = await _user_with_analysis()

    owner_body = (await client.get(_url(), headers=_auth(owner))).json()
    other_body = (await client.get(_url(), headers=_auth(other))).json()

    assert owner_body["opportunities"], "the control produced nothing"
    assert other_body["opportunities"] == []
    owner_ids = {i["source_id"] for i in owner_body["opportunities"]}
    assert not owner_ids & {
        i["source_id"] for i in other_body["opportunities"]}
    assert owner_body["graph_hash"] != other_body["graph_hash"]


@pytest.mark.asyncio
async def test_an_account_past_its_deletion_cutoff_may_not_read_assurance(client):
    uid, _, code = await _ready_user()
    assert (await client.get(_url(), headers=_auth(uid))).status_code == 200

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await AccountLifecycleService(s).request_deletion(uid)

    after = await client.get(_url(), headers=_auth(uid))
    assert after.status_code == 403
    assert "opportunities" not in after.text


# ===========================================================================
# Authority boundary — measured, not asserted
# ===========================================================================
@pytest.mark.asyncio
async def test_the_read_executes_no_business_authority(client):
    """Zero engine runs, zero rule evaluations — proved non-vacuous by real
    assurance items coming back."""
    import app.services.tax_engine.rules_service as rules_module
    import app.services.tax_engine.service as engine_service

    uid, _, code = await _ready_user()

    runs: list[int] = []
    evaluations: list[int] = []
    real_compute = engine_service.compute
    real_evaluate = rules_module.RulesEvaluatorService.evaluate

    def counting(inp):
        runs.append(1)
        return real_compute(inp)

    async def counting_evaluate(self, *a, **kw):
        evaluations.append(1)
        return await real_evaluate(self, *a, **kw)

    engine_service.compute = counting                              # type: ignore[assignment]
    rules_module.RulesEvaluatorService.evaluate = counting_evaluate  # type: ignore[method-assign]
    try:
        response = await client.get(_url(), headers=_auth(uid))
    finally:
        engine_service.compute = real_compute                      # type: ignore[assignment]
        rules_module.RulesEvaluatorService.evaluate = real_evaluate  # type: ignore[method-assign]

    assert response.status_code == 200
    assert response.json()["opportunities"], "no assurance was produced"
    assert runs == [], f"the engine ran {len(runs)} times on an assurance read"
    assert evaluations == [], f"rules evaluated {len(evaluations)} times"


@pytest.mark.asyncio
async def test_nothing_after_the_graph_build_touches_the_database(client):
    """Loading is the graph loader's job; deriving and serializing are pure.
    A per-item lookup would appear here as post-build statements."""
    from app.services.state_graph.service import TaxStateGraphService

    uid, _, code = await _ready_user()

    statements: list[str] = []
    at_build_return: list[int] = []
    real_build = TaxStateGraphService.build

    async def recording_build(self, **kw):
        graph = await real_build(self, **kw)
        at_build_return.append(len(statements))
        return graph

    def record(conn, cur, statement, parameters, context, executemany):
        statements.append(statement.lower())

    from app.database.session import engine

    TaxStateGraphService.build = recording_build    # type: ignore[method-assign]
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        response = await client.get(_url(), headers=_auth(uid))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
        TaxStateGraphService.build = real_build     # type: ignore[method-assign]

    assert response.status_code == 200
    assert at_build_return, "the graph build was never reached"
    total = len(statements)
    post_build = total - at_build_return[0]
    body = response.json()
    print(f"\nassurance API SQL: total={total} "                    # noqa: T201
          f"through-graph-build={at_build_return[0]} post-build={post_build} "
          f"items={len(body['opportunities'])} "
          f"payload_bytes={len(response.content)}")
    assert post_build == 0, (
        f"{post_build} statements after the graph build: "
        f"{statements[at_build_return[0]:][:3]}")


@pytest.mark.asyncio
async def test_a_rule_published_after_the_run_changes_nothing(client):
    """Current mode reads the run's sealed candidates, never latest rules. A
    rule published after the run must not conjure a new assurance item."""
    uid, _, code = await _ready_user()
    before = (await client.get(_url(), headers=_auth(uid))).json()

    await _published_rule(document_type_code="RRSP")

    after = (await client.get(_url(), headers=_auth(uid))).json()
    assert after == before


# ===========================================================================
# Determinism and time
# ===========================================================================
@pytest.mark.asyncio
async def test_repeated_requests_return_the_same_body(client):
    uid, _, code = await _ready_user()
    bodies = [
        (await client.get(_url(), headers=_auth(uid))).json()
        for _ in range(3)
    ]
    assert bodies[0] == bodies[1] == bodies[2]
    assert "correlation_id" not in bodies[0]


@pytest.mark.asyncio
async def test_as_of_moves_urgency_and_only_urgency(client):
    """The one field the clock touches, injected and echoed."""
    uid, _, code = await _ready_user()

    near = (await client.get(
        _url(as_of="2026-04-20"), headers=_auth(uid))).json()
    far = (await client.get(
        _url(as_of="2025-10-01"), headers=_auth(uid))).json()

    assert near["as_of"] == "2026-04-20" and far["as_of"] == "2025-10-01"
    assert near["graph_hash"] == far["graph_hash"]

    near_item = _mine(near, code)
    far_item = _mine(far, code)
    assert near_item["urgency"] == "URGENT"       # Apr 30 is 10 days out
    assert far_item["urgency"] == "NORMAL"        # and 211 days out
    assert near_item["status"] == far_item["status"]
    assert near_item["support"] == far_item["support"]


@pytest.mark.asyncio
async def test_omitting_as_of_uses_today_and_says_so(client):
    uid, _, code = await _ready_user()
    body = (await client.get(_url(as_of=None), headers=_auth(uid))).json()
    today = datetime.now(tz=UTC).date()
    # Either side of a midnight boundary during the request is acceptable.
    assert body["as_of"] in (
        today.isoformat(), (today - timedelta(days=1)).isoformat())


# ===========================================================================
# Privacy surface
# ===========================================================================
@pytest.mark.asyncio
async def test_no_storage_identity_reaches_the_map(client):
    uid, analysis_id = await _user_with_analysis()
    document_id, bucket, object_key = await _hold(uid)
    await _published_rule()
    await OptimizationOrchestrator(uid).generate(analysis_id)

    response = await client.get(_url(), headers=_auth(uid))
    text = response.text

    for secret, label in ((str(document_id), "document id"),
                          (bucket, "bucket"), (object_key, "object key")):
        assert secret not in text, f"the map exposes a {label}"
    assert "docs.document" not in text
    body = response.json()
    requirement_types = {
        r["document_type_code"]
        for i in body["opportunities"] for r in i["evidence_requirements"]}
    assert "T4" in requirement_types, (
        "the requirement vanished along with the storage identity; the "
        "absence assertions above prove nothing")


@pytest.mark.asyncio
async def test_the_map_makes_no_probability_or_strategy_claim(client):
    uid, _, code = await _ready_user()
    text = (await client.get(_url(), headers=_auth(uid))).text.lower()
    for claim in ('"savings"', '"best', '"recommended', '"guaranteed',
                  '"audit_risk', '"acceptance'):
        assert claim not in text, f"the contract asserts {claim}"
    # 'probability' appears exactly once per support block: inside the governed
    # disclaimer SAYING it is not one. Assert the denial is the only use.
    body = (await client.get(_url(), headers=_auth(uid))).json()
    for item in body["opportunities"]:
        assert "not" in item["support"]["disclaimer"].lower()


# ===========================================================================
# Surface
# ===========================================================================
@pytest.mark.asyncio
async def test_the_endpoint_is_published_in_the_openapi_document(client):
    document = (await client.get("/api/v1/openapi.json")).json()
    path = document["paths"]["/api/v1/ioe/assurance"]
    assert "get" in path
    schema = document["components"]["schemas"]["TaxAssuranceOut"]
    for field in ("schema_version", "view", "tax_year", "as_of", "graph_hash",
                  "families", "opportunities", "attention",
                  "assumption_codes", "summary"):
        assert field in schema["properties"], f"{field} missing from the schema"
