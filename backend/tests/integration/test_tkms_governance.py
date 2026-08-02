"""TKMS governance: four-eyes submit/approve/publish, gating, and rollback.

Runs the services against real PostgreSQL (RLS + audit triggers + the four-eyes
DB CHECK + the partial-unique published index live). Admins and rule codes are
uuid-suffixed so the suite is rerunnable on any database.
"""
import uuid

import pytest

from app.core.exceptions import Conflict, Forbidden
from app.database.models import RulePublication, TaxRuleVersion
from app.database.session import unit_of_work
from app.services.admin.service import AdminService
from app.services.tkms.extraction.service import ExtractionService
from app.services.tkms.governance.service import GovernanceService
from app.services.tkms.ingestion.service import ImportService
from app.services.tkms.publication.service import PublicationService
from app.services.tkms.rollback.service import RollbackService
from app.services.tkms.validation.service import ValidationService


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


async def _admin(s) -> uuid.UUID:
    a = await AdminService(s).create_admin(
        f"tkms_{uuid.uuid4().hex[:8]}@onyx.io", "adminpass1", ["kb_admin"]
    )
    return a.id


def _csv(code: str, max_amount: str) -> bytes:
    return (
        "code,name,category,jurisdiction,tax_year,max_amount\n"
        f"{code},Credit {code},credit,FED,2025,{max_amount}\n"
    ).encode()


async def _draft(s, operator: uuid.UUID, code: str, max_amount: str = "2000",
                 validate: bool = True) -> TaxRuleVersion:
    imp = ImportService(s)
    job = await imp.create_job(source_org="CRA", fmt="csv", tax_year=2025,
                               operator_admin_id=operator)
    await imp.store_raw(job.id, _csv(code, max_amount))
    await imp.parse(job.id)
    await imp.extract(job.id)
    version = (await ExtractionService(s).promote(job.id))[0]
    if validate:
        await ValidationService(s).validate_job(job.id)
    return version


@pytest.mark.asyncio
async def test_four_eyes_submit_approve_publish():
    code = f"GOV_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        a, b = await _admin(s), await _admin(s)
        v = await _draft(s, a, code)

        gov = GovernanceService(s)
        await gov.submit_for_review(a, v.id)
        assert v.status == "pending_review"

        # the submitter cannot approve their own version
        with pytest.raises(Forbidden):
            await gov.approve(a, v.id)

        await gov.approve(b, v.id)
        assert v.status == "approved"

        published = await PublicationService(s).publish(b, v.id)
        assert published.status == "published"
        assert published.published_at is not None
        assert published.validation_report_id is not None      # provenance wired

        pub_row = await s.scalar(
            RulePublication.__table__.select().where(
                RulePublication.tax_rule_version_id == v.id
            )
        )
        assert pub_row is not None


@pytest.mark.asyncio
async def test_publish_requires_approved_change_request():
    code = f"GOVNOAPP_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        a, b = await _admin(s), await _admin(s)
        v = await _draft(s, a, code)
        await GovernanceService(s).submit_for_review(a, v.id)   # pending, not approved
        # pending_review → published is not a legal lifecycle move
        with pytest.raises(ValueError):
            await PublicationService(s).publish(b, v.id)


@pytest.mark.asyncio
async def test_publish_requires_passed_validation():
    code = f"GOVNOVAL_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        a, b = await _admin(s), await _admin(s)
        v = await _draft(s, a, code, validate=False)           # no validation report
        gov = GovernanceService(s)
        await gov.submit_for_review(a, v.id)
        await gov.approve(b, v.id)
        with pytest.raises(Forbidden):
            await PublicationService(s).publish(b, v.id)


@pytest.mark.asyncio
async def test_reject_sends_back_to_draft():
    code = f"GOVREJ_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        a, b = await _admin(s), await _admin(s)
        v = await _draft(s, a, code)
        gov = GovernanceService(s)
        await gov.submit_for_review(a, v.id)
        await gov.reject(b, v.id, reason="needs rework")
        assert v.status == "draft"


@pytest.mark.asyncio
async def test_rollback_republishes_prior_version_under_four_eyes():
    code = f"GOVRB_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        a, b = await _admin(s), await _admin(s)
        gov, pub = GovernanceService(s), PublicationService(s)

        # publish v1 (amount 2000)
        v1 = await _draft(s, a, code, "2000")
        await gov.submit_for_review(a, v1.id)
        await gov.approve(b, v1.id)
        await pub.publish(b, v1.id)

        # publish v2 (amount 3000) → supersedes v1
        v2 = await _draft(s, a, code, "3000")
        await gov.submit_for_review(a, v2.id)
        await gov.approve(b, v2.id)
        await pub.publish(b, v2.id)

        v1 = await s.get(TaxRuleVersion, v1.id)
        assert v1.status == "superseded"

        # roll back to v1 under four-eyes
        rb = RollbackService(s)
        rec = await rb.request(a, v1.id, reason="reverting the increase")
        with pytest.raises(Forbidden):
            await rb.approve(a, rec.id)          # requester cannot approve own
        await rb.approve(b, rec.id)

        v1 = await s.get(TaxRuleVersion, v1.id)
        v2 = await s.get(TaxRuleVersion, v2.id)
        assert v1.status == "published"
        assert v2.status == "superseded"
        assert rec.status == "executed"


@pytest.mark.asyncio
async def test_rollback_target_must_be_superseded():
    code = f"GOVRBBAD_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        a = await _admin(s)
        v = await _draft(s, a, code)             # still a draft, never published
        with pytest.raises(Conflict):
            await RollbackService(s).request(a, v.id, reason="nope")
