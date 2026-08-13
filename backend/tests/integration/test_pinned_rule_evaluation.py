"""What a pinned rule-version set means to the evaluator.

THE DEFECT THIS FILE PINS. `evaluate` applied `status == 'published'` alongside
an explicit pin. The database permits exactly one published version per rule and
tax year (`uq_rule_version_published`), so publishing R2 necessarily moves R1 to
`superseded` — and the live status predicate then removed R1 from every
evaluation that had pinned it. A sealed artifact did not get R2's metadata
instead; it got nothing, and the deadlines, required documents and dependencies
it had pinned silently disappeared.

The published-only guarantee did not need that predicate and still holds:
`RuleSnapshotService._collect` selects published versions when the snapshot is
CAPTURED, which is the only moment the question can be answered correctly. These
tests hold both halves — the pin governs after publication moves on, and the
unpinned path is unchanged.
"""
import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.database.models import (
    Jurisdiction,
    RuleDeadline,
    RuleOutcome,
    TaxRule,
    TaxRuleVersion,
)
from app.database.session import unit_of_work
from app.services.ioe.snapshot.service import RuleSnapshotService
from app.services.tax_engine.rules_service import RulesEvaluatorService

TAX_YEAR = 2025
FACTS = {"profile.province": "ON", "income.total": 95000}


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


async def _rule(s) -> tuple[uuid.UUID, str]:
    code = f"PIN_{uuid.uuid4().hex[:8].upper()}"
    jurisdiction = await s.scalar(
        select(Jurisdiction).where(Jurisdiction.code == "FED"))
    rule = TaxRule(code=code, name=code, category="deduction",
                   jurisdiction_id=jurisdiction.id)
    s.add(rule)
    await s.flush()
    return rule.id, code


async def _version(
    s, rule_id: uuid.UUID, *, status: str, deadline_code: str,
) -> uuid.UUID:
    """A version with no condition gate, so it matches any fact map."""
    version = TaxRuleVersion(
        tax_rule_id=rule_id, tax_year=TAX_YEAR, effective_date=date(TAX_YEAR, 1, 1),
        status=status, description="pin fixture",
        eligibility_basis_codes=["BASIS_PIN"],
    )
    s.add(version)
    await s.flush()
    s.add(RuleOutcome(
        rule_version_id=version.id, outcome_type="recommend", priority=1,
        title_template="pinned opportunity",
    ))
    s.add(RuleDeadline(
        rule_version_id=version.id, deadline_code=deadline_code,
        deadline_date=date(TAX_YEAR + 1, 3, 1), is_hard=True,
    ))
    await s.flush()
    return version.id


def _deadlines(opportunities, code: str) -> set[str]:
    return {
        d.deadline_code for o in opportunities
        if o.opportunity_code == code.lower()
        for d in o.applicable_deadlines
    }


# ---------------------------------------------------------------------------
# The fix
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_pinned_version_survives_being_superseded():
    """THE REGRESSION. Pin R1, let R2 supersede it, evaluate the original pin.

    R1 must still evaluate and still carry its own deadline. Sealed evidence
    describes the rule set as it stood; an admin publishing a correction next
    month cannot be allowed to empty it.
    """
    async with unit_of_work(actor_type="admin") as s:
        rule_id, code = await _rule(s)
        r1 = await _version(s, rule_id, status="published", deadline_code="PIN_D1")

    async with unit_of_work(actor_type="admin") as s:
        # exactly the publication flow's order: retire, then publish
        (await s.get(TaxRuleVersion, r1)).status = "superseded"
        await s.flush()
        r2 = await _version(s, rule_id, status="published", deadline_code="PIN_D2")
        (await s.get(TaxRuleVersion, r1)).superseded_by_version_id = r2
        await s.flush()

    async with unit_of_work(actor_type="system") as s:
        opportunities = await RulesEvaluatorService(s).evaluate(
            TAX_YEAR, FACTS, pinned_rule_version_ids=[r1])

    assert [o.rule_version_id for o in opportunities] == [r1]
    assert _deadlines(opportunities, code) == {"PIN_D1"}


@pytest.mark.asyncio
async def test_the_unpinned_path_still_sees_only_published_versions():
    """The other half. Removing the predicate from the pinned branch must not
    remove it from the live one — an unpinned evaluation is a question about
    today's law and superseded rules are not part of it."""
    async with unit_of_work(actor_type="admin") as s:
        rule_id, code = await _rule(s)
        r1 = await _version(s, rule_id, status="published", deadline_code="PIN_LIVE_1")

    async with unit_of_work(actor_type="admin") as s:
        (await s.get(TaxRuleVersion, r1)).status = "superseded"
        await s.flush()
        r2 = await _version(s, rule_id, status="published", deadline_code="PIN_LIVE_2")

    async with unit_of_work(actor_type="system") as s:
        opportunities = await RulesEvaluatorService(s).evaluate(TAX_YEAR, FACTS)

    reached = {o.rule_version_id for o in opportunities}
    assert r2 in reached
    assert r1 not in reached, (
        "a superseded version entered an evaluation that pinned nothing")
    assert _deadlines(opportunities, code) == {"PIN_LIVE_2"}


@pytest.mark.asyncio
async def test_an_unpublished_version_never_enters_a_live_evaluation():
    """Drafts and approved-but-unpublished versions stay out of the live path,
    which is where the published-only rule belongs."""
    async with unit_of_work(actor_type="admin") as s:
        rule_id, code = await _rule(s)
        draft = await _version(s, rule_id, status="draft", deadline_code="PIN_DRAFT")

    async with unit_of_work(actor_type="system") as s:
        opportunities = await RulesEvaluatorService(s).evaluate(TAX_YEAR, FACTS)

    assert draft not in {o.rule_version_id for o in opportunities}
    assert _deadlines(opportunities, code) == set()


@pytest.mark.asyncio
async def test_the_pin_producer_is_where_published_only_is_enforced():
    """Names the load-bearing fact: every id this argument can ever hold was
    published when the snapshot was captured. That is what makes dropping the
    evaluation-time predicate safe rather than merely convenient."""
    async with unit_of_work(actor_type="admin") as s:
        rule_id, _ = await _rule(s)
        published = await _version(
            s, rule_id, status="published", deadline_code="PIN_CAP_OK")
        other_id, _ = await _rule(s)
        draft = await _version(
            s, other_id, status="draft", deadline_code="PIN_CAP_DRAFT")

    async with unit_of_work(actor_type="system") as s:
        captured = (await RuleSnapshotService(s).capture(TAX_YEAR)).version_ids()

    assert published in captured
    assert draft not in captured, (
        "the snapshot pinned an unpublished version; the evaluator relies on "
        "this filter and has no second one behind it")


# ---------------------------------------------------------------------------
# None and empty stay different questions
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_empty_pin_yields_nothing_and_none_resolves_now():
    async with unit_of_work(actor_type="admin") as s:
        rule_id, _ = await _rule(s)
        await _version(s, rule_id, status="published", deadline_code="PIN_EMPTY")

    async with unit_of_work(actor_type="system") as s:
        evaluator = RulesEvaluatorService(s)
        assert await evaluator.evaluate(
            TAX_YEAR, FACTS, pinned_rule_version_ids=[]) == []
        assert await evaluator.evaluate(
            TAX_YEAR, FACTS, pinned_rule_version_ids=None) != []


@pytest.mark.asyncio
async def test_a_pin_admits_exactly_the_versions_it_names():
    async with unit_of_work(actor_type="admin") as s:
        rule_id, _ = await _rule(s)
        wanted = await _version(
            s, rule_id, status="published", deadline_code="PIN_ONLY_A")
        other_id, _ = await _rule(s)
        await _version(s, other_id, status="published", deadline_code="PIN_ONLY_B")

    async with unit_of_work(actor_type="system") as s:
        opportunities = await RulesEvaluatorService(s).evaluate(
            TAX_YEAR, FACTS, pinned_rule_version_ids=[wanted])

    assert {o.rule_version_id for o in opportunities} == {wanted}
