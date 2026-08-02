"""TKMS ingestion + extraction against real PostgreSQL.

Exercises create_job → store_raw (checksum/dedupe) → parse → extract (staged
canonical rows) → promote (draft tax_rule_version + rules.* + provenance).
Rule codes are uuid-suffixed so the suite is rerunnable on any database.
"""
import uuid

import pytest
from sqlalchemy import select

from app.database.models import (
    ExtractedRule as ExtractedRuleRow,
)
from app.database.models import (
    RuleCondition,
    RuleConditionGroup,
)
from app.database.session import unit_of_work
from app.services.tkms.extraction.service import ExtractionService
from app.services.tkms.ingestion.service import ImportService


@pytest.fixture(autouse=True)
async def _dispose_engine():
    """Dispose the async pool after each test so connections aren't reused
    across per-test event loops (these tests use unit_of_work, not `client`)."""
    yield
    from app.database.session import engine

    await engine.dispose()


def _csv(code: str) -> bytes:
    return (
        "code,name,category,jurisdiction,tax_year,max_amount,reduction_rate\n"
        f"{code},Medical credit,credit,FED,2025,2759,0.03\n"
    ).encode()


def _json_with_conditions(code: str) -> bytes:
    import json

    return json.dumps([{
        "rule_code": code,
        "name": "RRSP deduction",
        "category": "deduction",
        "jurisdiction": "FED",
        "tax_year": 2025,
        "formula": {"code": f"{code}_F", "expression": "income 0.18 *",
                    "inputs": [["income", "income.total"]]},
        "eligibility_conditions": [
            {"fact_key": "profile.age", "operator": "lt", "value_type": "number", "value_number": "71"},
            {"fact_key": "unknown.fact", "operator": "eq", "value_type": "number", "value_number": "1"},
        ],
    }]).encode()


@pytest.mark.asyncio
async def test_ingest_parse_extract_promote_to_draft():
    code = f"TKMS_MED_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        imp = ImportService(s)
        job = await imp.create_job(source_org="CRA", fmt="csv", tax_year=2025,
                                   jurisdiction_code="FED")
        assert job.status == "received"

        doc = await imp.store_raw(job.id, _csv(code), mime_type="text/csv")
        assert job.status == "stored"
        assert job.checksum == doc.content_hash

        pr = await imp.parse(job.id)
        assert pr.status == "succeeded"
        assert job.status == "parsed"

        rows = await imp.extract(job.id)
        assert len(rows) == 1
        assert job.status == "extracted"
        assert rows[0].payload["rule_code"] == code

        versions = await ExtractionService(s).promote(job.id)
        assert len(versions) == 1
        v = versions[0]
        assert v.status == "draft"                      # inert to the engine
        assert v.import_job_id == job.id                # provenance wired
        assert v.parser_version is not None
        assert v.parser_confidence is not None

        # staged row now links to the draft it became
        row = await s.get(ExtractedRuleRow, rows[0].id)
        assert row.promoted_version_id == v.id


@pytest.mark.asyncio
async def test_duplicate_checksum_is_rejected():
    from app.core.exceptions import Conflict

    payload = _csv(f"TKMS_DUP_{uuid.uuid4().hex[:8].upper()}")
    async with unit_of_work(actor_type="admin") as s:
        imp = ImportService(s)
        job1 = await imp.create_job(source_org="CRA", fmt="csv", tax_year=2025)
        await imp.store_raw(job1.id, payload)
        job2 = await imp.create_job(source_org="CRA", fmt="csv", tax_year=2025)
        with pytest.raises(Conflict):
            await imp.store_raw(job2.id, payload)


@pytest.mark.asyncio
async def test_promotion_stages_conditions_and_skips_unknown_facts():
    code = f"TKMS_RRSP_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        imp = ImportService(s)
        job = await imp.create_job(source_org="CRA", fmt="json", tax_year=2025)
        await imp.store_raw(job.id, _json_with_conditions(code))
        await imp.parse(job.id)
        await imp.extract(job.id)
        versions = await ExtractionService(s).promote(job.id)
        v = versions[0]

        # a formula was staged and linked
        assert v.formula_id is not None

        # the condition tree has exactly ONE leaf — the known fact; the unknown
        # fact is omitted from the engine projection (validation will flag it).
        group = await s.scalar(
            select(RuleConditionGroup).where(RuleConditionGroup.rule_version_id == v.id)
        )
        assert group is not None
        leaves = list(await s.scalars(
            select(RuleCondition).where(RuleCondition.group_id == group.id)
        ))
        assert len(leaves) == 1
        assert leaves[0].fact_key == "profile.age"


@pytest.mark.asyncio
async def test_extract_is_idempotent():
    code = f"TKMS_IDEM_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        imp = ImportService(s)
        job = await imp.create_job(source_org="CRA", fmt="csv", tax_year=2025)
        await imp.store_raw(job.id, _csv(code))
        await imp.parse(job.id)
        first = await imp.extract(job.id)
        second = await imp.extract(job.id)     # re-run resumes, does not duplicate
        assert {r.id for r in first} == {r.id for r in second}
