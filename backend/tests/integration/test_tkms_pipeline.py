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


@pytest.mark.asyncio
async def test_validation_passes_clean_job_and_advances_status():
    from app.database.models import ValidationFinding as VF
    from app.services.tkms.validation.service import ValidationService

    code = f"TKMS_VOK_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        imp = ImportService(s)
        job = await imp.create_job(source_org="CRA", fmt="csv", tax_year=2025)
        await imp.store_raw(job.id, _csv(code))
        await imp.parse(job.id)
        await imp.extract(job.id)

        report = await ValidationService(s).validate_job(job.id)
        assert report.status == "passed"
        assert job.validation_status == "passed"
        assert job.status == "validated"
        errors = list(await s.scalars(
            select(VF).where(VF.report_id == report.id, VF.severity == "error")
        ))
        assert errors == []


@pytest.mark.asyncio
async def test_validation_fails_bad_year_and_blocks():
    from app.services.tkms.validation.service import ValidationService

    # tax_year 1999 is not a seeded reference year → error, job cannot advance
    code = f"TKMS_VBAD_{uuid.uuid4().hex[:8].upper()}"
    raw = (
        "code,name,category,jurisdiction,tax_year\n"
        f"{code},Bad year rule,credit,FED,1999\n"
    ).encode()
    async with unit_of_work(actor_type="admin") as s:
        imp = ImportService(s)
        job = await imp.create_job(source_org="CRA", fmt="csv", tax_year=2025)
        await imp.store_raw(job.id, raw)
        await imp.parse(job.id)
        await imp.extract(job.id)
        report = await ValidationService(s).validate_job(job.id)
        assert report.status == "failed"
        assert job.validation_status == "failed"
        assert job.status != "validated"        # blocked


@pytest.mark.asyncio
async def test_comparison_reports_changes_vs_published():
    from app.database.models import ChangeItem as CI
    from app.services.tkms.comparison.service import ComparisonService
    from app.services.tkms.extraction.service import ExtractionService

    code = f"TKMS_CMP_{uuid.uuid4().hex[:8].upper()}"

    def _raw(max_amount: str) -> bytes:
        return (
            "code,name,category,jurisdiction,tax_year,max_amount\n"
            f"{code},Credit,credit,FED,2025,{max_amount}\n"
        ).encode()

    async with unit_of_work(actor_type="admin") as s:
        imp = ImportService(s)
        # first import → promote → mark published (simulate a live baseline)
        job1 = await imp.create_job(source_org="CRA", fmt="csv", tax_year=2025)
        await imp.store_raw(job1.id, _raw("2000"))
        await imp.parse(job1.id)
        await imp.extract(job1.id)
        v1 = (await ExtractionService(s).promote(job1.id))[0]
        v1.status = "published"
        await s.flush()

        # second import of the SAME rule with a changed amount → draft
        job2 = await imp.create_job(source_org="CRA", fmt="csv", tax_year=2025)
        await imp.store_raw(job2.id, _raw("3000"))
        await imp.parse(job2.id)
        await imp.extract(job2.id)
        v2 = (await ExtractionService(s).promote(job2.id))[0]

        report = await ComparisonService(s).compare(v2.id)
        assert report.baseline_version_id == v1.id
        items = list(await s.scalars(select(CI).where(CI.change_report_id == report.id)))
        changed = {i.field: i for i in items}
        assert "max_amount" in changed
        assert changed["max_amount"].old_value == "2000"
        assert changed["max_amount"].new_value == "3000"
