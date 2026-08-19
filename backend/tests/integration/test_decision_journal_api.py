"""Entry: Tax Decision Journal — the API over real sealed state.

The unit suite proves the projection folds. What is at stake here: that the
journal records declarations without inferring anything from them, that history
is append-only against both the API and the database role, that tenancy leaks
nothing, that pinned artifact identity survives changes to the current world,
and that journal rows die with the account.

Every test creates its own user, rules, evidence, scenario, and journal.
"""
import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event, select, text

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
from app.schemas.decision_journal import DECISION_JOURNAL_SCHEMA_VERSION
from app.services.ioe.domain.scenario import (
    SCENARIO_RESULT_SCHEMA_V2,
    ScenarioSpec,
)
from app.services.ioe.frozen.models import reconstruct_tax_input
from app.services.ioe.scenario.service import ScenarioService
from app.services.privacy.lifecycle import AccountLifecycleService
from tests.conftest import frozen_snapshot

API = "/api/v1/ioe/decision-journal"
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
            email=f"dj_{uuid.uuid4().hex[:8]}@test.ca", status="active")
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


async def _publish_rule(description: str = "journal fixture") -> str:
    async with unit_of_work(actor_type="admin") as s:
        jurisdiction = await s.scalar(
            select(Jurisdiction).where(Jurisdiction.code == "FED"))
        code = f"DJ_{uuid.uuid4().hex[:8].upper()}"
        rule = TaxRule(code=code, name=code, category="deduction",
                       jurisdiction_id=jurisdiction.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=TAX_YEAR,
            effective_date=date(TAX_YEAR, 1, 1), status="published",
            description=description, eligibility_basis_codes=["BASIS_DJ"])
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(rule_version_id=version.id, outcome_type="recommend",
                          priority=1, title_template=f"{description} opportunity"))
        s.add(RuleRequiredDocument(rule_version_id=version.id,
                                   document_type_code="RRSP", necessity="required"))
        s.add(RuleDeadline(rule_version_id=version.id, deadline_code=f"DL_{code}",
                           deadline_date=date(TAX_YEAR + 1, 4, 30), is_hard=True,
                           jurisdiction_code="FED"))
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
            object_key=f"{uid}/dj/{uuid.uuid4()}",
            content_hash=uuid.uuid4().hex, status="processed")
        s.add(document)
        await s.flush()
        return str(document.id), document.bucket, document.object_key


async def _sealed_scenario(uid: uuid.UUID, analysis_id: uuid.UUID) -> uuid.UUID:
    outcome = await ScenarioService(uid).simulate(
        analysis_id,
        ScenarioSpec.parse(
            [{"lever_code": RRSP, "parameters": {"amount": Decimal("10000")}}]))
    return outcome.scenario_id


async def _ready(client):
    """A user with held evidence, a governed rule, a sealed v3 scenario, and
    one open journal thread. Returns (uid, scenario_id, journal_body)."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _hold(uid)
    await _publish_rule()
    scenario_id = await _sealed_scenario(uid, analysis_id)
    response = await client.post(API, headers=_auth(uid), json={
        "scenario_id": str(scenario_id), "request_id": str(uuid.uuid4())})
    assert response.status_code == 201, response.text
    return uid, scenario_id, response.json()


# ===========================================================================
# Creation
# ===========================================================================
@pytest.mark.asyncio
async def test_a_thread_opens_considering_with_pinned_identity(client):
    uid, scenario_id, body = await _ready(client)

    assert body["schema_version"] == DECISION_JOURNAL_SCHEMA_VERSION
    assert body["current_decision"] == "CONSIDERING"
    assert body["execution_state"] == "NOT_REPORTED"
    assert body["event_count"] == 1
    assert body["events"][0]["event_type"] == "CREATED"
    assert body["events"][0]["decision"] == "CONSIDERING"

    reference = body["scenario_reference"]
    assert reference["scenario_id"] == str(scenario_id)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        from app.database.models import Scenario

        scenario = await s.get(Scenario, scenario_id)
        assert reference["scenario_result_hash"] == scenario.scenario_result_hash
        assert (reference["scenario_result_schema_version"]
                == scenario.result_schema_version)
    assert reference["comparison_hash"], (
        "a v3 seal supports the certified comparison; its identity must be pinned")


@pytest.mark.asyncio
async def test_creation_is_idempotent_under_retry(client):
    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    scenario_id = await _sealed_scenario(uid, analysis_id)
    request_id = str(uuid.uuid4())
    body = {"scenario_id": str(scenario_id), "request_id": request_id}

    first = await client.post(API, headers=_auth(uid), json=body)
    second = await client.post(API, headers=_auth(uid), json=body)

    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    listed = (await client.get(API, headers=_auth(uid))).json()
    assert len(listed) == 1, "a retry created a second thread"


@pytest.mark.asyncio
async def test_a_foreign_scenario_is_indistinguishable_from_a_missing_one(client):
    owner, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    scenario_id = await _sealed_scenario(owner, analysis_id)
    intruder, _ = await _user_with_baseline_analysis()

    foreign = await client.post(API, headers=_auth(intruder), json={
        "scenario_id": str(scenario_id), "request_id": str(uuid.uuid4())})
    nowhere = await client.post(API, headers=_auth(intruder), json={
        "scenario_id": str(uuid.uuid4()), "request_id": str(uuid.uuid4())})

    assert foreign.status_code == 404 and nowhere.status_code == 404
    a, b = foreign.json(), nowhere.json()
    a.pop("correlation_id", None)
    b.pop("correlation_id", None)
    assert a == b


@pytest.mark.asyncio
async def test_an_unsealed_scenario_cannot_open_a_thread(client):
    """A journal records a decision about a sealed result. A pending scenario
    has none, and the refusal must say so rather than pin nothing."""
    uid, analysis_id = await _user_with_baseline_analysis()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        from app.database.models import Scenario

        scenario = Scenario(
            user_id=uid, base_analysis_id=analysis_id, tax_year=TAX_YEAR,
            workflow_status="pending")
        s.add(scenario)
        await s.flush()
        pending_id = scenario.id

    response = await client.post(API, headers=_auth(uid), json={
        "scenario_id": str(pending_id), "request_id": str(uuid.uuid4())})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_a_legacy_v2_scenario_opens_with_honestly_absent_comparison(client):
    """Older seals cannot answer for the certified comparison. The thread still
    opens — the decision is the user's to record — but the comparison identity
    is NULL, never a reconstruction."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    outcome = await ScenarioService(uid)._simulate(
        analysis_id,
        ScenarioSpec.parse(
            [{"lever_code": RRSP, "parameters": {"amount": Decimal("5000")}}]),
        result_schema_version=SCENARIO_RESULT_SCHEMA_V2)

    response = await client.post(API, headers=_auth(uid), json={
        "scenario_id": str(outcome.scenario_id),
        "request_id": str(uuid.uuid4())})

    assert response.status_code == 201
    reference = response.json()["scenario_reference"]
    assert reference["comparison_hash"] is None
    assert reference["comparison_schema_version"] is None
    assert reference["scenario_result_hash"], "the sealed result is still pinned"


@pytest.mark.asyncio
async def test_an_account_past_its_deletion_cutoff_may_not_touch_the_journal(client):
    uid, _, body = await _ready(client)
    journal_id = body["id"]

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        await AccountLifecycleService(s).request_deletion(uid)

    for attempt in (
        client.get(API, headers=_auth(uid)),
        client.get(f"{API}/{journal_id}", headers=_auth(uid)),
        client.post(f"{API}/{journal_id}/decision", headers=_auth(uid),
                    json={"decision": "PROCEED", "request_id": str(uuid.uuid4())}),
    ):
        response = await attempt
        assert response.status_code == 403


# ===========================================================================
# Decisions — declarations, supersession, retained history
# ===========================================================================
@pytest.mark.asyncio
async def test_each_declaration_is_recorded_and_current(client):
    uid, _, body = await _ready(client)
    journal_id = body["id"]

    for decision in ("PROCEED", "DEFER", "DECLINE"):
        response = await client.post(
            f"{API}/{journal_id}/decision", headers=_auth(uid),
            json={"decision": decision, "request_id": str(uuid.uuid4())})
        assert response.status_code == 201, response.text
        assert response.json()["current_decision"] == decision


@pytest.mark.asyncio
async def test_a_change_of_mind_appends_and_the_original_survives(client):
    uid, _, body = await _ready(client)
    journal_id = body["id"]

    await client.post(f"{API}/{journal_id}/decision", headers=_auth(uid),
                      json={"decision": "DEFER", "request_id": str(uuid.uuid4())})
    final = await client.post(
        f"{API}/{journal_id}/decision", headers=_auth(uid),
        json={"decision": "PROCEED", "request_id": str(uuid.uuid4())})

    detail = final.json()
    assert detail["current_decision"] == "PROCEED"
    decisions = [e["decision"] for e in detail["events"]]
    assert decisions == ["CONSIDERING", "DEFER", "PROCEED"], (
        "the original declaration must survive its supersession")
    assert [e["sequence"] for e in detail["events"]] == [1, 2, 3]


@pytest.mark.asyncio
async def test_decision_append_is_idempotent_under_retry(client):
    uid, _, body = await _ready(client)
    journal_id = body["id"]
    request_id = str(uuid.uuid4())

    first = await client.post(
        f"{API}/{journal_id}/decision", headers=_auth(uid),
        json={"decision": "PROCEED", "request_id": request_id})
    second = await client.post(
        f"{API}/{journal_id}/decision", headers=_auth(uid),
        json={"decision": "PROCEED", "request_id": request_id})

    assert first.json()["event_count"] == 2
    assert second.json()["event_count"] == 2, "a retry appended a second event"


# ===========================================================================
# Execution — intent is not action, report is not verification
# ===========================================================================
@pytest.mark.asyncio
async def test_deciding_to_proceed_reports_no_action(client):
    uid, _, body = await _ready(client)
    journal_id = body["id"]

    detail = (await client.post(
        f"{API}/{journal_id}/decision", headers=_auth(uid),
        json={"decision": "PROCEED", "request_id": str(uuid.uuid4())})).json()

    assert detail["current_decision"] == "PROCEED"
    assert detail["execution_state"] == "NOT_REPORTED", (
        "an intent was silently promoted to an action")


@pytest.mark.asyncio
async def test_a_reported_action_is_user_reported_never_verified(client):
    uid, _, body = await _ready(client)
    journal_id = body["id"]
    reported = date(2026, 2, 27)

    detail = (await client.post(
        f"{API}/{journal_id}/action-report", headers=_auth(uid),
        json={"request_id": str(uuid.uuid4()),
              "action_date": reported.isoformat()})).json()

    assert detail["execution_state"] == "USER_REPORTED"
    assert detail["last_reported_action_date"] == reported.isoformat()
    assert "VERIFIED" not in detail["execution_state"]
    text_body = str(detail).lower()
    assert "system_verified" not in text_body


@pytest.mark.asyncio
async def test_a_future_action_date_is_refused(client):
    uid, _, body = await _ready(client)
    journal_id = body["id"]
    tomorrow = (datetime.now(tz=UTC).date() + timedelta(days=1)).isoformat()

    response = await client.post(
        f"{API}/{journal_id}/action-report", headers=_auth(uid),
        json={"request_id": str(uuid.uuid4()), "action_date": tomorrow})
    assert response.status_code == 422


# ===========================================================================
# History integrity
# ===========================================================================
@pytest.mark.asyncio
async def test_the_application_role_cannot_rewrite_history(client):
    """Append-only against the DATABASE, not just the API: the app role holds
    neither UPDATE nor DELETE on journal tables."""
    uid, _, body = await _ready(client)
    journal_id = body["id"]

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        for statement in (
            f"UPDATE ioe.decision_journal_event SET decision = 'PROCEED' "
            f"WHERE journal_id = '{journal_id}'",
            f"DELETE FROM ioe.decision_journal_event "
            f"WHERE journal_id = '{journal_id}'",
            f"UPDATE ioe.decision_journal SET subject_opportunity_code = 'x' "
            f"WHERE id = '{journal_id}'",
            f"DELETE FROM ioe.decision_journal WHERE id = '{journal_id}'",
        ):
            with pytest.raises(Exception, match="permission denied"):
                async with s.begin_nested():
                    await s.execute(text(statement))


@pytest.mark.asyncio
async def test_concurrent_appends_serialize_into_a_total_order(client):
    """Two simultaneous declarations must produce two events with distinct
    consecutive sequences — never a tie, never a lost write."""
    uid, _, body = await _ready(client)
    journal_id = body["id"]

    async def declare(decision: str):
        return await client.post(
            f"{API}/{journal_id}/decision", headers=_auth(uid),
            json={"decision": decision, "request_id": str(uuid.uuid4())})

    first, second = await asyncio.gather(declare("DEFER"), declare("PROCEED"))
    assert first.status_code == 201 and second.status_code == 201

    detail = (await client.get(f"{API}/{journal_id}", headers=_auth(uid))).json()
    assert [e["sequence"] for e in detail["events"]] == [1, 2, 3]
    assert detail["event_count"] == 3


# ===========================================================================
# Scenario link — the pin survives the present
# ===========================================================================
@pytest.mark.asyncio
async def test_changing_the_current_world_does_not_rewrite_the_pin(client):
    uid, _, body = await _ready(client)
    journal_id = body["id"]
    before = body["scenario_reference"]

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        row = await s.scalar(select(IncomeSource).where(IncomeSource.user_id == uid))
        row.amount = Decimal("250000")
    await _publish_rule("post-decision rule")
    await _hold(uid, "RRSP")

    after = (await client.get(
        f"{API}/{journal_id}", headers=_auth(uid))).json()["scenario_reference"]
    assert after == before, "the pinned decision context moved with the present"


# ===========================================================================
# Evidence — governed readiness beside the record
# ===========================================================================
@pytest.mark.asyncio
async def test_evidence_context_distinguishes_sealed_from_current(client):
    """The NOV-21 story: the user holds the required document AFTER deciding.
    Sealed readiness must not move; current observed readiness must."""
    uid, _, body = await _ready(client)
    journal_id = body["id"]

    sealed_before = body["evidence_context"]["sealed_readiness"]
    current_before = body["evidence_context"]["current_observed_readiness"]
    rrsp_before = [r for r in current_before
                   if r["document_type_code"] == "RRSP"]
    assert rrsp_before and rrsp_before[0]["readiness"] == "MISSING"

    await _hold(uid, "RRSP")  # the user now holds the required receipt

    context = (await client.get(
        f"{API}/{journal_id}", headers=_auth(uid))).json()["evidence_context"]
    assert context["sealed_readiness"] == sealed_before, (
        "history was rewritten by a new document")
    rrsp_now = [r for r in context["current_observed_readiness"]
                if r["document_type_code"] == "RRSP"]
    assert rrsp_now and rrsp_now[0]["readiness"] == "READY", (
        "current observed readiness did not observe the new document")
    assert context["sealed_deadline_codes"], "the pinned deadline context vanished"


@pytest.mark.asyncio
async def test_no_storage_identity_reaches_the_journal(client):
    uid, analysis_id = await _user_with_baseline_analysis()
    document_id, bucket, object_key = await _hold(uid)
    await _publish_rule()
    scenario_id = await _sealed_scenario(uid, analysis_id)

    created = await client.post(API, headers=_auth(uid), json={
        "scenario_id": str(scenario_id), "request_id": str(uuid.uuid4())})
    listed = await client.get(API, headers=_auth(uid))
    detail = await client.get(
        f"{API}/{created.json()['id']}", headers=_auth(uid))

    for response in (created, listed, detail):
        for secret in (document_id, bucket, object_key):
            assert secret not in response.text
        assert "docs.document" not in response.text


# ===========================================================================
# Tenancy
# ===========================================================================
@pytest.mark.asyncio
async def test_cross_tenant_reads_and_writes_are_denied_without_an_oracle(client):
    owner, _, body = await _ready(client)
    journal_id = body["id"]
    intruder, _ = await _user_with_baseline_analysis()

    foreign_get = await client.get(f"{API}/{journal_id}", headers=_auth(intruder))
    nowhere_get = await client.get(
        f"{API}/{uuid.uuid4()}", headers=_auth(intruder))
    foreign_post = await client.post(
        f"{API}/{journal_id}/decision", headers=_auth(intruder),
        json={"decision": "PROCEED", "request_id": str(uuid.uuid4())})

    assert foreign_get.status_code == 404
    assert nowhere_get.status_code == 404
    assert foreign_post.status_code == 404
    a, b = foreign_get.json(), nowhere_get.json()
    a.pop("correlation_id", None)
    b.pop("correlation_id", None)
    assert a == b

    # And nothing of the owner's appeared anywhere in the refusals.
    assert (await client.get(API, headers=_auth(intruder))).json() == []
    detail = (await client.get(f"{API}/{journal_id}", headers=_auth(owner))).json()
    assert detail["event_count"] == 1, "the foreign write went through"


# ===========================================================================
# Deletion — journal rows die with the account
# ===========================================================================
@pytest.mark.asyncio
async def test_journal_rows_are_removed_by_the_cascade_chain(client):
    """Journal rows die with the account, and the proof must not manufacture
    the very orphan state migration 0060 forbids. Deleting `user_account` raw
    here would strand the detached sealed roots and fail the 0060
    irreversibility check for every test after this one — the terminal removal
    only fires after the privacy phases have emptied them.

    So the claim is proved in two legal halves: STRUCTURALLY, both FK edges
    are `ON DELETE CASCADE` in the live catalog — user_account → journal →
    event — which is the same evidence standard the delete registry cites; and
    BEHAVIORALLY, deleting the thread row sweeps its history through the
    journal → event edge of that same chain. (The scenario and account edges
    are exercised only structurally: raw parent deletes here would fight the
    sealed-evidence triggers and the 0060 orphan check, which is precisely
    why terminal removal is a governed workflow.)
    """
    uid, scenario_id, body = await _ready(client)
    journal_id = uuid.UUID(body["id"])

    import psycopg2

    from tests.conftest import owner_dsn

    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT conrelid::regclass::text, confrelid::regclass::text,
                   confdeltype
              FROM pg_constraint
             WHERE contype = 'f'
               AND conrelid::regclass::text IN
                   ('ioe.decision_journal', 'ioe.decision_journal_event')
            """)
        edges = {(row[0], row[1]): row[2] for row in cur.fetchall()}
        assert edges[("ioe.decision_journal",
                      "identity.user_account")] == "c", (
            "the account edge is not ON DELETE CASCADE")
        assert edges[("ioe.decision_journal", "ioe.scenario")] == "c"
        assert edges[("ioe.decision_journal_event",
                      "ioe.decision_journal")] == "c"

        cur.execute(
            "SELECT count(*) FROM ioe.decision_journal_event "
            "WHERE journal_id = %s", (str(journal_id),))
        assert cur.fetchone()[0] >= 1, "no history to prove the cascade against"

        # The thread goes — as any parent cascade would take it — and the
        # chain must take its history with it. The owner connection bypasses
        # RLS, so zero means gone, not hidden.
        cur.execute(
            "DELETE FROM ioe.decision_journal WHERE id = %s",
            (str(journal_id),))
        cur.execute(
            "SELECT count(*) FROM ioe.decision_journal WHERE id = %s",
            (str(journal_id),))
        journals = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM ioe.decision_journal_event "
            "WHERE journal_id = %s", (str(journal_id),))
        events = cur.fetchone()[0]
    conn.close()
    assert journals == 0 and events == 0, (
        "journal rows survived the cascade")


# ===========================================================================
# Determinism and cost
# ===========================================================================
@pytest.mark.asyncio
async def test_repeated_reads_return_the_same_body(client):
    uid, _, body = await _ready(client)
    journal_id = body["id"]
    await client.post(f"{API}/{journal_id}/decision", headers=_auth(uid),
                      json={"decision": "PROCEED", "request_id": str(uuid.uuid4())})

    bodies = [
        (await client.get(f"{API}/{journal_id}", headers=_auth(uid))).json()
        for _ in range(3)
    ]
    assert bodies[0] == bodies[1] == bodies[2]
    assert "correlation_id" not in bodies[0]


@pytest.mark.asyncio
async def test_the_journal_executes_no_business_authority(client):
    """Creation pins the comparison through the certified PURE engine over
    sealed rows; nothing anywhere computes tax or evaluates rules."""
    import app.services.tax_engine.rules_service as rules_module
    import app.services.tax_engine.service as engine_service

    uid, analysis_id = await _user_with_baseline_analysis()
    await _hold(uid)
    await _publish_rule()
    scenario_id = await _sealed_scenario(uid, analysis_id)

    runs: list[int] = []
    evaluations: list[int] = []
    real_compute = engine_service.compute
    real_evaluate = rules_module.RulesEvaluatorService.evaluate

    def counting(inp, dataset=None):
        # Mirrors `compute`'s signature. A stub taking fewer
        # arguments than what it replaces fails on the call
        # instead of on this test's actual assertion.
        runs.append(1)
        return real_compute(inp, dataset)

    async def counting_evaluate(self, *a, **kw):
        evaluations.append(1)
        return await real_evaluate(self, *a, **kw)

    engine_service.compute = counting                              # type: ignore[assignment]
    rules_module.RulesEvaluatorService.evaluate = counting_evaluate  # type: ignore[method-assign]
    try:
        created = await client.post(API, headers=_auth(uid), json={
            "scenario_id": str(scenario_id), "request_id": str(uuid.uuid4())})
        journal_id = created.json()["id"]
        await client.post(f"{API}/{journal_id}/decision", headers=_auth(uid),
                          json={"decision": "PROCEED",
                                "request_id": str(uuid.uuid4())})
        detail = await client.get(f"{API}/{journal_id}", headers=_auth(uid))
    finally:
        engine_service.compute = real_compute                      # type: ignore[assignment]
        rules_module.RulesEvaluatorService.evaluate = real_evaluate  # type: ignore[method-assign]

    assert created.status_code == 201 and detail.status_code == 200
    assert created.json()["scenario_reference"]["comparison_hash"]
    assert runs == [], f"the engine ran {len(runs)} times"
    assert evaluations == [], f"rules evaluated {len(evaluations)} times"


@pytest.mark.asyncio
async def test_listing_many_threads_with_long_histories_is_two_queries(client):
    """The list loads every thread's history in one events query — measured,
    so an N+1 cannot creep in as thread counts grow."""
    uid, analysis_id = await _user_with_baseline_analysis()
    await _publish_rule()
    journal_ids = []
    for _ in range(4):
        scenario_id = await _sealed_scenario(uid, analysis_id)
        created = await client.post(API, headers=_auth(uid), json={
            "scenario_id": str(scenario_id), "request_id": str(uuid.uuid4())})
        journal_ids.append(created.json()["id"])
    for journal_id in journal_ids:
        for _ in range(10):
            await client.post(
                f"{API}/{journal_id}/decision", headers=_auth(uid),
                json={"decision": "DEFER", "request_id": str(uuid.uuid4())})

    statements: list[str] = []

    def record(conn, cur, statement, parameters, context, executemany):
        lowered = statement.lower()
        if "decision_journal" in lowered:
            statements.append(lowered)

    from app.database.session import engine

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        listed = await client.get(API, headers=_auth(uid))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert listed.status_code == 200
    assert len(listed.json()) == 4
    assert all(entry["event_count"] == 11 for entry in listed.json())
    print(f"\njournal list SQL over journal tables: {len(statements)}")  # noqa: T201
    assert len(statements) == 2, (
        f"listing issued {len(statements)} journal queries; expected thread "
        f"query + one events query")


# ===========================================================================
# Surface
# ===========================================================================
@pytest.mark.asyncio
async def test_the_endpoints_are_published_in_the_openapi_document(client):
    document = (await client.get("/api/v1/openapi.json")).json()
    paths = document["paths"]
    assert "post" in paths["/api/v1/ioe/decision-journal"]
    assert "get" in paths["/api/v1/ioe/decision-journal"]
    assert "get" in paths["/api/v1/ioe/decision-journal/{journal_id}"]
    assert "post" in paths["/api/v1/ioe/decision-journal/{journal_id}/decision"]
    assert "post" in paths[
        "/api/v1/ioe/decision-journal/{journal_id}/action-report"]

    schema = document["components"]["schemas"]["DecisionJournalDetailOut"]
    for field in ("schema_version", "scenario_reference", "current_decision",
                  "execution_state", "events", "evidence_context"):
        assert field in schema["properties"], f"{field} missing from the schema"
