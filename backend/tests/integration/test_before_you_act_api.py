"""Entry 12B — the Before-You-Act comparison API and product contract.

The engine was certified separately (`test_scenario_comparison_engine.py`).
What is at stake HERE is the request path: that exposing a sealed comparison
over HTTP does not quietly acquire the properties the engine was built to avoid
— a live recalculation, a fallback, a leaked storage identifier, or an empty
success page where a refusal belongs.

Every test establishes its own governed rule state. Nothing is inherited from a
neighbouring test, because a shared database accumulates and the residue of
another test's rule would silently decide what this one sees.
"""
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, select

from app.core.security.jwt import create_access_token
from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisLineItem,
    AnalysisRun,
    Document,
    DocumentType,
    IncomeSource,
    IncomeType,
    Jurisdiction,
    RuleDeadline,
    RuleOutcome,
    RuleRequiredDocument,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import unit_of_work
from app.schemas.before_you_act import BEFORE_YOU_ACT_SCHEMA_VERSION
from app.services.ioe.domain.scenario import (
    SCENARIO_RESULT_SCHEMA_V2,
    ScenarioSpec,
)
from app.services.ioe.frozen.models import reconstruct_tax_input
from app.services.ioe.scenario.service import ScenarioService
from app.services.privacy.lifecycle import AccountLifecycleService
from tests.conftest import frozen_snapshot

API = "/api/v1/ioe"
TAX_YEAR = 2025
RRSP = "INCREASE_RRSP_DEDUCTION"


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _auth(user_id: uuid.UUID) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user_id))}"}


# ------------------------------------------------------------------ fixtures
async def _user_with_baseline_analysis() -> tuple[uuid.UUID, uuid.UUID]:
    from app.services.tax_engine.core.engine import compute

    async with unit_of_work(actor_type="system") as s:
        account = UserAccount(
            email=f"bya_{uuid.uuid4().hex[:8]}@test.ca", status="active")
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
        result = compute(reconstruct_tax_input(payload))
        for i, item in enumerate(result.line_items):
            s.add(AnalysisLineItem(
                analysis_id=run.id, kind=item["kind"], label=item["label"],
                amount=item["amount"], sort_order=i))
        await s.flush()
        return uid, run.id


async def _publish_rule(description: str = "12B api fixture") -> uuid.UUID:
    """This test's OWN governed rule."""
    async with unit_of_work(actor_type="admin") as s:
        jurisdiction = await s.scalar(
            select(Jurisdiction).where(Jurisdiction.code == "FED"))
        code = f"BYA_{uuid.uuid4().hex[:8].upper()}"
        rule = TaxRule(code=code, name=code, category="deduction",
                       jurisdiction_id=jurisdiction.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=TAX_YEAR,
            effective_date=date(TAX_YEAR, 1, 1), status="published",
            description=description, eligibility_basis_codes=["BASIS_BYA"])
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(rule_version_id=version.id, outcome_type="recommend",
                          priority=1, title_template=f"{description} opportunity"))
        s.add(RuleRequiredDocument(rule_version_id=version.id,
                                   document_type_code="T4", necessity="required"))
        s.add(RuleDeadline(rule_version_id=version.id, deadline_code=f"DL_{code}",
                           deadline_date=date(TAX_YEAR + 1, 4, 30), is_hard=True,
                           jurisdiction_code="FED"))
        await s.flush()
        return version.id


async def _hold(uid: uuid.UUID, code: str = "T4") -> uuid.UUID:
    async with unit_of_work(actor_type="system") as s:
        type_id = (await s.scalar(
            select(DocumentType).where(DocumentType.code == code))).id
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        document = Document(
            user_id=uid, document_type_id=type_id, tax_year=TAX_YEAR,
            bucket=f"onyx-bucket-{uuid.uuid4().hex}",
            object_key=f"{uid}/bya/{uuid.uuid4()}",
            content_hash=uuid.uuid4().hex, status="processed")
        s.add(document)
        await s.flush()
        return document.id


async def _seal(uid: uuid.UUID, analysis_id: uuid.UUID, amount="15000"):
    """The ordinary production path — `simulate`, not an internal seam."""
    return await ScenarioService(uid).simulate(
        analysis_id,
        ScenarioSpec.parse(
            [{"lever_code": RRSP, "parameters": {"amount": Decimal(amount)}}]))


async def _seal_legacy_v2(uid: uuid.UUID, analysis_id: uuid.UUID):
    return await ScenarioService(uid)._simulate(
        analysis_id,
        ScenarioSpec.parse(
            [{"lever_code": RRSP, "parameters": {"amount": Decimal("5000")}}]),
        result_schema_version=SCENARIO_RESULT_SCHEMA_V2)


async def _ready_scenario(amount="15000"):
    """A complete, comparable scenario with its own rule and held evidence."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _hold(uid)
    await _publish_rule()
    outcome = await _seal(uid, analysis_id, amount)
    return uid, analysis_id, outcome.scenario_id


def _url(scenario_id, *, include_unchanged=None) -> str:
    base = f"{API}/scenarios/{scenario_id}/comparison"
    if include_unchanged is None:
        return base
    return f"{base}?include_unchanged={'true' if include_unchanged else 'false'}"


def _all_changes(body: dict) -> list[dict]:
    families = ("tax_state_changes", "opportunity_changes", "evidence_changes",
                "deadline_changes", "fact_changes", "assumption_changes",
                "scenario_changes")
    return [c for f in families for c in body[f]]


# ===========================================================================
# The contract
# ===========================================================================
@pytest.mark.asyncio
async def test_a_sealed_scenario_returns_the_product_contract(client):
    """THE CENTRAL ACCEPTANCE PROOF for the request path."""
    uid, _, scenario_id = await _ready_scenario()

    response = await client.get(_url(scenario_id), headers=_auth(uid))

    assert response.status_code == 200, response.text
    body = response.json()

    assert body["schema_version"] == BEFORE_YOU_ACT_SCHEMA_VERSION
    assert body["direction"] == "BASELINE_TO_COUNTERFACTUAL"
    assert body["scenario"]["id"] == str(scenario_id)
    assert body["comparison_hash"]
    assert body["includes_unchanged"] is False

    for family in ("tax_state_changes", "opportunity_changes", "evidence_changes",
                   "deadline_changes", "fact_changes", "assumption_changes",
                   "scenario_changes", "family_applicability"):
        assert family in body, f"{family} missing from the contract"

    assert body["summary"]["node_counts_by_change"]
    assert _all_changes(body), "a comparable scenario produced no change records"


@pytest.mark.asyncio
async def test_the_product_version_is_not_the_sealed_protocol_version(client):
    """§ The contract carries its own version. A client renders THIS payload;
    coupling it to the seal would re-version every client for a change no client
    can see."""
    uid, _, scenario_id = await _ready_scenario()

    body = (await client.get(_url(scenario_id), headers=_auth(uid))).json()

    assert body["schema_version"] == BEFORE_YOU_ACT_SCHEMA_VERSION
    assert body["scenario"]["result_schema_version"] != body["schema_version"], (
        "the product contract version is tracking the sealed protocol version")


# ===========================================================================
# Authorization, tenancy, and the absence of an existence oracle
# ===========================================================================
@pytest.mark.asyncio
async def test_another_tenants_scenario_is_indistinguishable_from_a_missing_one(
        client):
    """The whole no-existence-oracle property in one assertion: a real scenario
    belonging to someone else and an id that never existed must produce the same
    response, byte for byte apart from the correlation id."""
    owner, _, scenario_id = await _ready_scenario()
    intruder, _ = await _user_with_baseline_analysis()

    theirs = await client.get(_url(scenario_id), headers=_auth(intruder))
    nowhere = await client.get(_url(uuid.uuid4()), headers=_auth(intruder))

    assert theirs.status_code == 404
    assert nowhere.status_code == 404

    a, b = theirs.json(), nowhere.json()
    a.pop("correlation_id", None)
    b.pop("correlation_id", None)
    assert a == b, "the response distinguishes someone else's scenario from nothing"

    leaked = theirs.text
    assert str(owner) not in leaked
    assert "line_items" not in leaked and "comparison_hash" not in leaked


@pytest.mark.asyncio
async def test_the_owner_still_reads_their_own_scenario(client):
    """The control for the test above: the 404 is about tenancy, not about the
    endpoint refusing everyone."""
    uid, _, scenario_id = await _ready_scenario()

    assert (await client.get(_url(scenario_id), headers=_auth(uid))).status_code == 200


@pytest.mark.asyncio
async def test_an_account_past_its_deletion_cutoff_may_not_read_a_comparison(client):
    """Inherited from `db_authed`, and asserted here because inheriting it is a
    property of the wiring that a later refactor could silently drop."""
    uid, _, scenario_id = await _ready_scenario()
    assert (await client.get(_url(scenario_id), headers=_auth(uid))).status_code == 200

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await AccountLifecycleService(s).request_deletion(uid)

    after = await client.get(_url(scenario_id), headers=_auth(uid))
    assert after.status_code == 403
    assert "comparison_hash" not in after.text


# ===========================================================================
# Failing closed
# ===========================================================================
@pytest.mark.asyncio
async def test_a_legacy_v2_scenario_is_refused_with_a_closed_reason_code(client):
    """A v2 seal carries no baseline opportunity authority, so it is not
    comparable. The refusal must be an error, never a 200 with empty arrays —
    an empty page reads as "nothing would change", which is a claim."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await _seal_legacy_v2(uid, analysis_id)

    response = await client.get(_url(outcome.scenario_id), headers=_auth(uid))

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error_code"] == "SEALED_EVIDENCE_INCOMPLETE"
    assert body["type"].endswith("/comparison-unavailable")
    assert "correlation_id" in body

    assert "tax_state_changes" not in body
    assert "Traceback" not in response.text and "sqlalchemy" not in response.text.lower()


@pytest.mark.asyncio
async def test_the_refusal_path_executes_no_business_authority(client):
    """A refusal that quietly recomputed would be the same defect as a fallback,
    just with a worse status code."""
    import app.services.tax_engine.rules_service as rules_module
    import app.services.tax_engine.service as engine_service

    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await _seal_legacy_v2(uid, analysis_id)

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
        response = await client.get(_url(outcome.scenario_id), headers=_auth(uid))
    finally:
        engine_service.compute = real_compute                      # type: ignore[assignment]
        rules_module.RulesEvaluatorService.evaluate = real_evaluate  # type: ignore[method-assign]

    assert response.status_code == 409
    assert runs == [] and evaluations == []


# ===========================================================================
# Family serialization
# ===========================================================================
@pytest.mark.asyncio
async def test_a_moved_tax_line_carries_before_after_and_a_signed_delta(client):
    """TAX_STATE is the family a customer reads first, so the arithmetic has to
    survive serialization intact."""
    uid, _, scenario_id = await _ready_scenario(amount="15000")

    body = (await client.get(_url(scenario_id), headers=_auth(uid))).json()
    changed = [c for c in body["tax_state_changes"] if c["change"] == "CHANGED"]
    assert changed, "an RRSP lever moved no tax line"

    amounts = [f for c in changed for f in c["fields"]
               if f["field"] == "attributes.amount" and f["delta"] is not None]
    assert amounts, "no monetary delta reached the contract"
    for field in amounts:
        assert Decimal(field["delta"]) == (
            Decimal(field["after"]) - Decimal(field["before"]))


@pytest.mark.asyncio
async def test_the_sealed_opportunity_is_not_reported_as_something_created(client):
    """The phantom-addition defect, seen through the API."""
    uid, _, scenario_id = await _ready_scenario()

    body = (await client.get(_url(scenario_id, include_unchanged=True),
                             headers=_auth(uid))).json()
    opportunities = body["opportunity_changes"]

    assert opportunities, "no opportunity reached the contract"
    assert any(c["change"] == "UNCHANGED" for c in opportunities), (
        "every opportunity read as a difference; the baseline side is missing")
    assert not [c for c in opportunities if c["change"] == "ADDED"]


@pytest.mark.asyncio
async def test_deadline_and_evidence_families_serialize(client):
    """Both families exist in the projection, so their absence from the contract
    would be a silent serialization gap rather than an empty comparison."""
    uid, _, scenario_id = await _ready_scenario()

    body = (await client.get(_url(scenario_id, include_unchanged=True),
                             headers=_auth(uid))).json()

    assert body["deadline_changes"], "no deadline reached the contract"
    assert body["evidence_changes"], "no evidence record reached the contract"


@pytest.mark.asyncio
async def test_evidence_never_carries_a_storage_identity(client):
    """READINESS SEMANTICS ONLY. Evidence says whether a requirement is met; it
    must not become a way to enumerate a person's documents."""
    uid, _, scenario_id = await _ready_scenario()

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        document = await s.scalar(select(Document).where(Document.user_id == uid))
        bucket, object_key = document.bucket, document.object_key
        content_hash, document_id = document.content_hash, str(document.id)

    text = (await client.get(_url(scenario_id, include_unchanged=True),
                             headers=_auth(uid))).text

    for secret, label in ((bucket, "bucket"), (object_key, "object key"),
                          (content_hash, "content hash"), (document_id, "document id")):
        assert secret not in text, f"the response exposes a {label}"
    assert "docs.document" not in text


# ===========================================================================
# Applicability
# ===========================================================================
@pytest.mark.asyncio
async def test_resource_is_reported_inapplicable_rather_than_empty(client):
    """A single scenario has no portfolio, so RESOURCE does not apply. Rendering
    it as an empty comparable family would invite "you had resources and now you
    do not"."""
    uid, _, scenario_id = await _ready_scenario()

    body = (await client.get(_url(scenario_id), headers=_auth(uid))).json()
    by_family = {e["family"]: e for e in body["family_applicability"]}

    assert by_family["RESOURCE"]["status"] == (
        "NOT_APPLICABLE_IN_SINGLE_SCENARIO_COMPARISON")
    assert by_family["RESOURCE"]["reason_code"] == (
        "SINGLE_SCENARIO_HAS_NO_PORTFOLIO_LEDGER")
    assert "resource_changes" not in body
    assert not [c for c in _all_changes(body) if c["key"].startswith("RESOURCE:")], (
        "an inapplicable family produced comparison records")


@pytest.mark.asyncio
async def test_every_live_family_declares_its_applicability(client):
    """Applicability travels with the comparison so a reader can tell a family
    holding nothing from one that does not apply."""
    from app.services.state_graph.contracts import LIVE_NODE_TYPES

    uid, _, scenario_id = await _ready_scenario()

    body = (await client.get(_url(scenario_id), headers=_auth(uid))).json()
    declared = {e["family"] for e in body["family_applicability"]}

    for node_type in LIVE_NODE_TYPES:
        assert node_type.value in declared, f"{node_type.value} declares nothing"
    for entry in body["family_applicability"]:
        assert entry["status"] and entry["reason_code"]


# ===========================================================================
# Product safety
# ===========================================================================
@pytest.mark.asyncio
async def test_money_is_never_serialized_as_a_float(client):
    """A float is not a money type. Values arrive as the canonicalizer rendered
    them at seal time and must leave unchanged."""
    uid, _, scenario_id = await _ready_scenario()

    body = (await client.get(_url(scenario_id, include_unchanged=True),
                             headers=_auth(uid))).json()

    for change in _all_changes(body):
        for field in change["fields"]:
            for slot in ("before", "after", "delta"):
                assert not isinstance(field[slot], float), (
                    f"{change['key']}.{field['field']}.{slot} serialized as a float")


@pytest.mark.asyncio
async def test_the_contract_makes_no_strategy_claim(client):
    """This endpoint reports WHAT CHANGED. A savings figure or a ranking would be
    a tax decision wearing a comparison's clothes."""
    uid, _, scenario_id = await _ready_scenario()

    text = (await client.get(_url(scenario_id, include_unchanged=True),
                             headers=_auth(uid))).text.lower()

    for claim in ('"savings"', '"best', '"recommended', '"recommendation"',
                  '"guaranteed', '"probability', '"optimal'):
        assert claim not in text, f"the contract asserts {claim}"


# ===========================================================================
# The UNCHANGED policy is presentation only
# ===========================================================================
@pytest.mark.asyncio
async def test_filtering_unchanged_records_changes_nothing_but_the_rendering(client):
    """The policy is safe only if the hash and the summary are computed from the
    full comparison. If filtering could move either, a client would be able to
    change what the comparison SAYS by changing what it ASKS FOR."""
    uid, _, scenario_id = await _ready_scenario()

    lean = (await client.get(_url(scenario_id, include_unchanged=False),
                             headers=_auth(uid))).json()
    full = (await client.get(_url(scenario_id, include_unchanged=True),
                             headers=_auth(uid))).json()

    assert lean["comparison_hash"] == full["comparison_hash"]
    assert lean["summary"] == full["summary"]
    assert lean["includes_unchanged"] is False and full["includes_unchanged"] is True

    assert not [c for c in _all_changes(lean) if c["change"] == "UNCHANGED"]
    assert [c for c in _all_changes(full) if c["change"] == "UNCHANGED"], (
        "include_unchanged returned nothing extra; the test proves nothing")

    lean_keys = {c["key"] for c in _all_changes(lean)}
    full_keys = {c["key"] for c in _all_changes(full)}
    assert lean_keys <= full_keys


# ===========================================================================
# Determinism
# ===========================================================================
@pytest.mark.asyncio
async def test_repeating_the_request_returns_the_same_response(client):
    """Three requests, and the only thing allowed to differ is the correlation
    id — which lives in the error envelope, not in the artifact."""
    uid, _, scenario_id = await _ready_scenario()

    bodies = [
        (await client.get(_url(scenario_id, include_unchanged=True),
                          headers=_auth(uid))).json()
        for _ in range(3)
    ]

    assert bodies[0] == bodies[1] == bodies[2]
    assert len({b["comparison_hash"] for b in bodies}) == 1
    assert "correlation_id" not in bodies[0]


@pytest.mark.asyncio
async def test_the_response_does_not_move_when_live_state_moves(client):
    """NO LIVE FALLBACK, through HTTP. Seal, read, then churn current financials,
    publish a new rule, and hold a new document. The payload must be identical."""
    import app.services.tax_engine.rules_service as rules_module
    import app.services.tax_engine.service as engine_service

    uid, _, scenario_id = await _ready_scenario()

    before = (await client.get(_url(scenario_id, include_unchanged=True),
                               headers=_auth(uid))).json()

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        row = await s.scalar(select(IncomeSource).where(IncomeSource.user_id == uid))
        row.amount = Decimal("250000")
    await _publish_rule("post-seal rule")
    await _hold(uid, "RRSP")

    runs: list[int] = []
    evaluations: list[int] = []
    statements: list[str] = []
    real_compute = engine_service.compute
    real_evaluate = rules_module.RulesEvaluatorService.evaluate

    def counting(inp):
        runs.append(1)
        return real_compute(inp)

    async def counting_evaluate(self, *a, **kw):
        evaluations.append(1)
        return await real_evaluate(self, *a, **kw)

    def record(conn, cur, statement, parameters, context, executemany):
        statements.append(statement.lower())

    from app.database.session import engine

    engine_service.compute = counting                              # type: ignore[assignment]
    rules_module.RulesEvaluatorService.evaluate = counting_evaluate  # type: ignore[method-assign]
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        after = (await client.get(_url(scenario_id, include_unchanged=True),
                                  headers=_auth(uid))).json()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
        engine_service.compute = real_compute                      # type: ignore[assignment]
        rules_module.RulesEvaluatorService.evaluate = real_evaluate  # type: ignore[method-assign]

    assert after == before
    assert after["comparison_hash"] == before["comparison_hash"]
    assert runs == [] and evaluations == []
    assert [x for x in statements if "docs.document" in x] == []
    assert [x for x in statements
            if "optimization_run" in x or "reco.recommendation" in x] == []


# ===========================================================================
# The database boundary
# ===========================================================================
@pytest.mark.asyncio
async def test_nothing_after_the_sealed_source_load_touches_the_database(client):
    """§ The measured boundary. Loading is I/O; projecting, comparing and
    serializing are not. A per-change lookup would show up here as a statement
    count that grows with the comparison."""
    import app.services.ioe.scenario.before_you_act as service_module

    uid, _, scenario_id = await _ready_scenario()

    statements: list[str] = []
    at_load_return: list[int] = []
    real_load = service_module.load_sealed_sides

    async def recording_load(*a, **kw):
        sides = await real_load(*a, **kw)
        at_load_return.append(len(statements))
        return sides

    def record(conn, cur, statement, parameters, context, executemany):
        statements.append(statement.lower())

    from app.database.session import engine

    service_module.load_sealed_sides = recording_load   # type: ignore[assignment]
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        response = await client.get(_url(scenario_id, include_unchanged=True),
                                    headers=_auth(uid))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)
        service_module.load_sealed_sides = real_load    # type: ignore[assignment]

    assert response.status_code == 200
    assert at_load_return, "the source loader was never reached"

    total = len(statements)
    post_load = total - at_load_return[0]
    print(f"\n12B comparison API SQL: total={total} "                # noqa: T201
          f"through-source-load={at_load_return[0]} post-source-load={post_load} "
          f"records={len(_all_changes(response.json()))}")

    assert post_load == 0, (
        f"{post_load} statements were issued after the sealed source load: "
        f"{statements[at_load_return[0]:][:3]}")


@pytest.mark.asyncio
async def test_the_request_executes_no_business_authority(client):
    """Zero engine runs, zero rule evaluations, no live document read — with the
    counters proved non-vacuous by a real comparison coming back."""
    import app.services.ioe.portfolio.eligibility as eligibility
    import app.services.ioe.portfolio.service as portfolio
    import app.services.tax_engine.rules_service as rules_module
    import app.services.tax_engine.service as engine_service

    uid, _, scenario_id = await _ready_scenario()

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
    for module in (portfolio, eligibility):
        if getattr(module, "compute", None) is not None:
            module.compute = counting                              # type: ignore[assignment]
    rules_module.RulesEvaluatorService.evaluate = counting_evaluate  # type: ignore[method-assign]
    try:
        response = await client.get(_url(scenario_id), headers=_auth(uid))
    finally:
        engine_service.compute = real_compute                      # type: ignore[assignment]
        for module in (portfolio, eligibility):
            if getattr(module, "compute", None) is not None:
                module.compute = real_compute                      # type: ignore[assignment]
        rules_module.RulesEvaluatorService.evaluate = real_evaluate  # type: ignore[method-assign]

    assert runs == [], f"the engine ran {len(runs)} times on a historical read"
    assert evaluations == [], f"rules evaluated {len(evaluations)} times"
    assert response.status_code == 200
    assert _all_changes(response.json()), "no comparison was produced"


# ===========================================================================
# Surface
# ===========================================================================
@pytest.mark.asyncio
async def test_the_endpoint_is_published_in_the_openapi_document(client):
    """A product contract nobody can discover is not a contract."""
    document = (await client.get("/api/v1/openapi.json")).json()
    path = document["paths"]["/api/v1/ioe/scenarios/{scenario_id}/comparison"]

    assert "get" in path
    schema = document["components"]["schemas"]["BeforeYouActComparisonOut"]
    for field in ("schema_version", "summary", "tax_state_changes",
                  "opportunity_changes", "evidence_changes", "deadline_changes",
                  "fact_changes", "assumption_changes", "scenario_changes",
                  "family_applicability", "comparison_hash"):
        assert field in schema["properties"], f"{field} missing from the schema"
