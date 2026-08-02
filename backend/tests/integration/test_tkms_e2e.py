"""TKMS end-to-end, concurrency, and regression against real PostgreSQL.

- e2e: an imported rule, once PUBLISHED through four-eyes, is consumed by the
  deterministic Tax Intelligence Engine (RulesEvaluatorService) — the whole
  point of the pipeline.
- concurrency: two simultaneous publishes of the same rule+year → exactly one
  wins (the partial-unique published index).
- regression: a fixed corpus extracts to identical canonical payloads across
  runs (deterministic), and rollback preserves the immutability guarantee.
"""
import asyncio
import json
import uuid
from decimal import Decimal

import pytest

from app.database.models import TaxRuleVersion
from app.database.session import unit_of_work
from app.services.admin.service import AdminService
from app.services.tax_engine.rules_service import RulesEvaluatorService
from app.services.tkms.extraction.service import ExtractionService
from app.services.tkms.governance.service import GovernanceService
from app.services.tkms.ingestion.service import ImportService
from app.services.tkms.publication.service import PublicationService
from app.services.tkms.validation.service import ValidationService


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


async def _admin(s) -> uuid.UUID:
    a = await AdminService(s).create_admin(
        f"e2e_{uuid.uuid4().hex[:8]}@onyx.io", "adminpass1", ["kb_admin"]
    )
    return a.id


def _rule_payload(code: str, *, age_gate: int, max_amount: str = "5000") -> bytes:
    """A complete rule: gate on profile.age, an RPN impact formula, an outcome."""
    return json.dumps([{
        "rule_code": code, "name": "Age-gated credit", "category": "credit",
        "jurisdiction": "FED", "tax_year": 2025, "max_amount": max_amount,
        "description": "A credit gated by age, consumed by the engine.",
        "source_url": "https://canada.ca/age-credit",
        "formula": {"code": f"{code}_F", "expression": "income 0.05 *",
                    "inputs": [["income", "income.total"]]},
        "eligibility_conditions": [
            {"fact_key": "profile.age", "operator": "gte", "value_type": "number",
             "value_number": str(age_gate)},
        ],
        "outcome": {"outcome_type": "recommend", "priority": 1,
                    "title_template": "Age-gated credit", "mechanism": "credit",
                    "why_template": "You meet the age threshold."},
    }]).encode()


async def _publish(s, code: str, age_gate: int, max_amount: str = "5000") -> TaxRuleVersion:
    a, b = await _admin(s), await _admin(s)
    imp = ImportService(s)
    job = await imp.create_job(source_org="CRA", fmt="json", tax_year=2025, operator_admin_id=a)
    await imp.store_raw(job.id, _rule_payload(code, age_gate=age_gate, max_amount=max_amount))
    await imp.parse(job.id)
    await imp.extract(job.id)
    v = (await ExtractionService(s).promote(job.id))[0]
    await ValidationService(s).validate_job(job.id)
    gov = GovernanceService(s)
    await gov.submit_for_review(a, v.id)
    await gov.approve(b, v.id)
    return await PublicationService(s).publish(b, v.id)


@pytest.mark.asyncio
async def test_published_rule_is_consumed_by_the_engine():
    # gate at age 150 so ONLY our crafted facts match (no pollution of other users)
    code = f"E2E_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        published = await _publish(s, code, age_gate=150)
        assert published.status == "published"

        engine = RulesEvaluatorService(s)

        # facts that satisfy the gate → the engine emits the opportunity
        matching = await engine.evaluate(2025, {"profile.age": 150, "income.total": Decimal("80000")})
        mine = [o for o in matching if o.rule_version_id == published.id]
        assert len(mine) == 1
        opp = mine[0]
        assert opp.opportunity_code == code.lower()
        assert opp.estimated_impact == Decimal("4000.00")     # 80000 * 0.05
        assert opp.why_text == "You meet the age threshold."

        # facts that fail the gate → the engine does NOT emit it
        non = await engine.evaluate(2025, {"profile.age": 40, "income.total": Decimal("80000")})
        assert all(o.rule_version_id != published.id for o in non)


@pytest.mark.asyncio
async def test_draft_is_never_consumed_by_the_engine():
    code = f"E2EDRAFT_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        a = await _admin(s)
        imp = ImportService(s)
        job = await imp.create_job(source_org="CRA", fmt="json", tax_year=2025, operator_admin_id=a)
        await imp.store_raw(job.id, _rule_payload(code, age_gate=150))
        await imp.parse(job.id)
        await imp.extract(job.id)
        v = (await ExtractionService(s).promote(job.id))[0]   # stays 'draft'
        assert v.status == "draft"

        opps = await RulesEvaluatorService(s).evaluate(
            2025, {"profile.age": 150, "income.total": Decimal("80000")}
        )
        assert all(o.rule_version_id != v.id for o in opps)   # inert


@pytest.mark.asyncio
async def test_concurrent_publish_of_same_rule_year_exactly_one_wins():
    code = f"E2ECONC_{uuid.uuid4().hex[:8].upper()}"
    # setup: two approved+validated drafts of the SAME rule+year
    async with unit_of_work(actor_type="admin") as s:
        a, b = await _admin(s), await _admin(s)
        imp = ImportService(s)
        gov = GovernanceService(s)
        vids = []
        for amount in ("1000", "2000"):
            job = await imp.create_job(source_org="CRA", fmt="json", tax_year=2025,
                                       operator_admin_id=a)
            await imp.store_raw(job.id, _rule_payload(code, age_gate=150, max_amount=amount))
            await imp.parse(job.id)
            await imp.extract(job.id)
            v = (await ExtractionService(s).promote(job.id))[0]
            await ValidationService(s).validate_job(job.id)
            await gov.submit_for_review(a, v.id)
            await gov.approve(b, v.id)
            vids.append(v.id)
        publisher = b

    async def _pub(vid):
        async with unit_of_work(user_id=publisher, actor_type="admin") as s:
            await PublicationService(s).publish(publisher, vid)

    # both may succeed (the later one supersedes the earlier) or one may lose the
    # unique-index race — either way the invariant holds: exactly one published.
    await asyncio.gather(_pub(vids[0]), _pub(vids[1]), return_exceptions=True)

    async with unit_of_work(actor_type="admin") as s:
        published = list(await s.scalars(
            TaxRuleVersion.__table__.select().where(
                TaxRuleVersion.id.in_(vids),
                TaxRuleVersion.status == "published",
            )
        ))
        assert len(published) == 1, published


@pytest.mark.asyncio
async def test_double_publish_blocked_by_unique_index():
    """The DB partial-unique index is the last-resort guard: two published rows
    for the same (rule, tax_year) are impossible even if the service is bypassed."""
    from sqlalchemy.exc import IntegrityError

    code = f"E2EIDX_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        v1 = await _publish(s, code, age_gate=150, max_amount="1000")
        assert v1.status == "published"

        # a second draft of the SAME rule+year, forced to published WITHOUT
        # superseding v1 → the unique index must reject it
        imp = ImportService(s)
        a = await _admin(s)
        job = await imp.create_job(source_org="CRA", fmt="json", tax_year=2025,
                                   operator_admin_id=a)
        await imp.store_raw(job.id, _rule_payload(code, age_gate=150, max_amount="2000"))
        await imp.parse(job.id)
        await imp.extract(job.id)
        v2 = (await ExtractionService(s).promote(job.id))[0]
        v2.status = "published"
        with pytest.raises(IntegrityError):
            await s.flush()


@pytest.mark.asyncio
async def test_regression_corpus_extracts_deterministically():
    # the same corpus, parsed twice, must yield identical canonical payloads
    from app.services.tkms.parsers.json_parser import GovernmentJsonParser

    corpus = json.dumps([
        {"rule_code": "REG_A", "name": "A", "category": "credit",
         "jurisdiction": "FED", "tax_year": 2025, "max_amount": "1000"},
        {"rule_code": "REG_B", "name": "B", "category": "deduction",
         "jurisdiction": "ON", "tax_year": 2025, "reduction_rate": "0.15"},
    ])
    p = GovernmentJsonParser()
    first = [r.as_payload() for r in p.extract_rules(corpus).rules]
    second = [r.as_payload() for r in p.extract_rules(corpus).rules]
    assert first == second
    assert [r["rule_code"] for r in first] == ["REG_A", "REG_B"]
    assert first[1]["province"] == "ON"          # province derived from jurisdiction


@pytest.mark.asyncio
async def test_rollback_preserves_historical_version_immutability():
    code = f"E2EIMMUT_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        # publish v1 (impact 0.05) then v2 (impact via a different amount)
        v1 = await _publish(s, code, age_gate=150, max_amount="1000")
        v1_effective = v1.effective_date
        v2 = await _publish(s, code, age_gate=150, max_amount="2000")

        v1 = await s.get(TaxRuleVersion, v1.id)
        assert v1.status == "superseded"
        assert v2.status == "published"

        # the superseded v1 row is UNCHANGED in its substantive fields (immutable)
        assert v1.effective_date == v1_effective
        assert v1.max_amount == Decimal("1000")
