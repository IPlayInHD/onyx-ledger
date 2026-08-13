"""Entry 12B1 Phase A1 — deriving the counterfactual state, against real rules.

The unit tests prove the sealed shape and the wiring. Three claims cannot be
proved without a database holding two versions of the same rule, and they are
the ones that matter most:

  §5  a scenario that pinned rule version R1 must still resolve R1's deadline
      after R2 supersedes it — argument inspection would pass while the query
      returned something else entirely;
  §2  retaining the engine output must not cost a second engine execution;
  §8  the sealed tax state must be the tax state the scenario was sealed from.

Nothing here writes counterfactual state. Phase A1 builds the derivation; where
it is persisted is Phase A2's decision.
"""
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
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
from app.services.ioe.domain import canonical as c
from app.services.ioe.domain.scenario import ScenarioSpec
from app.services.ioe.scenario import counterfactual
from app.services.ioe.scenario.service import ScenarioService
from tests.conftest import frozen_snapshot

RRSP = "INCREASE_RRSP_DEDUCTION"
TAX_YEAR = 2025


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _lever(amount="5000"):
    return {"lever_code": RRSP, "parameters": {"amount": Decimal(amount)}}


async def _user_with_analysis() -> tuple[uuid.UUID, uuid.UUID]:
    async with unit_of_work(actor_type="system") as s:
        u = UserAccount(email=f"a1_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(u)
        await s.flush()
        uid = u.id
        income_type_id = (await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment"))).id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=TAX_YEAR, income_type_id=income_type_id,
            amount=Decimal("95000"), province_code="ON",
        ))
        run = AnalysisRun(
            user_id=uid, tax_year=TAX_YEAR, province_code="ON",
            engine_version="py-1.0.0", status="completed",
            started_at=datetime.now(tz=UTC), completed_at=datetime.now(tz=UTC),
            data_verified=True,
        )
        s.add(run)
        await s.flush()
        snapshot, snapshot_hash = frozen_snapshot(employment_income=Decimal("95000"))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=snapshot, snapshot_hash=snapshot_hash))
        await s.flush()
        return uid, run.id


async def _publish_version(
    *, rule_id: uuid.UUID, deadline_code: str, document_code: str = "T4",
    supersedes: uuid.UUID | None = None,
) -> uuid.UUID:
    """Publish one version of `rule_id` carrying exactly one deadline.

    No condition tree, so the version matches any fact map: this test is about
    which VERSION's metadata is reached, not about gating.

    `supersedes` retires the outgoing version in the SAME transaction, which is
    what `RulePublicationService.publish` does and what
    `uq_rule_version_published` requires — the database permits exactly one
    published version per rule and tax year, so supersession is not an optional
    step a test could skip to keep both versions live.
    """
    async with unit_of_work(actor_type="admin") as s:
        if supersedes is not None:
            outgoing = await s.get(TaxRuleVersion, supersedes)
            outgoing.status = "superseded"
            await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule_id, tax_year=TAX_YEAR,
            effective_date=date(TAX_YEAR, 1, 1), status="published",
            description="A1 fixture", eligibility_basis_codes=["BASIS_A1"],
        )
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            title_template="A1 opportunity", economic_effect_type="current_year_tax_reduction",
            reversibility="reversible",
        ))
        s.add(RuleDeadline(
            rule_version_id=version.id, deadline_code=deadline_code,
            deadline_date=date(TAX_YEAR + 1, 3, 1), is_hard=True,
        ))
        s.add(RuleRequiredDocument(
            rule_version_id=version.id, document_type_code=document_code,
            necessity="required",
        ))
        await s.flush()
        return version.id


async def _new_rule() -> tuple[uuid.UUID, str]:
    code = f"A1_{uuid.uuid4().hex[:8].upper()}"
    async with unit_of_work(actor_type="admin") as s:
        jurisdiction = await s.scalar(
            select(Jurisdiction).where(Jurisdiction.code == "FED"))
        rule = TaxRule(code=code, name=code, category="deduction",
                       jurisdiction_id=jurisdiction.id)
        s.add(rule)
        await s.flush()
        return rule.id, code


async def _record_successor(old_id: uuid.UUID, new_id: uuid.UUID) -> None:
    """The second half of what `RulePublicationService.publish` writes: the
    outgoing version names its successor. Replicated rather than driven through
    the four-eyes flow, which needs change requests and validation reports that
    have nothing to do with what is under test."""
    async with unit_of_work(actor_type="admin") as s:
        old = await s.get(TaxRuleVersion, old_id)
        old.superseded_by_version_id = new_id
        await s.flush()


async def _pin(service: ScenarioService, analysis_id: uuid.UUID, spec: ScenarioSpec):
    async with unit_of_work(user_id=service.user_id, actor_type="user") as s:
        return await service._pin_specification(s, analysis_id, spec, result_schema_version="1.0.0")


async def _derive(service: ScenarioService, pinned, computed):
    async with unit_of_work(user_id=service.user_id, actor_type="user") as s:
        return await service.build_counterfactual_derived_state(s, pinned, computed)


def _candidate(state, opportunity_code: str):
    matches = [x for x in state.candidates if x.opportunity_code == opportunity_code]
    assert len(matches) == 1, (
        f"expected exactly one candidate for {opportunity_code}, "
        f"got {[x.candidate_key for x in matches]}")
    return matches[0]


# ---------------------------------------------------------------------------
# §5 — THE HARD GATE. The pinned version governs, after supersession.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_pinned_rule_version_still_resolves_its_own_deadline_after_supersession():
    """R1 is pinned with deadline D1. R2 then supersedes it with D2.

    The counterfactual derived from the ORIGINAL pin must carry D1. Not D2, and
    not nothing: a scenario's evidence describes the law as it stood when the
    scenario was sealed, and substituting today's deadline would put a date on
    the user's screen that the sealed calculation never used.

    Measured through the real evaluator against real rows. Asserting that
    `evaluate` was CALLED with the right ids would pass while the query it built
    returned something else entirely, which is the failure mode this gate is
    for.
    """
    uid, analysis_id = await _user_with_analysis()
    rule_id, code = await _new_rule()
    r1 = await _publish_version(rule_id=rule_id, deadline_code="A1_DEADLINE_R1")

    service = ScenarioService(uid)
    spec = ScenarioSpec.parse([_lever()])
    pinned = await _pin(service, analysis_id, spec)
    assert r1 in pinned.pinned_rule_version_ids, "the fixture rule must be pinned"
    computed = service._compute(pinned)

    # Time passes. A new version of the SAME rule is published with a different
    # deadline, and the old one is superseded exactly as publication does it.
    r2 = await _publish_version(
        rule_id=rule_id, deadline_code="A1_DEADLINE_R2", supersedes=r1)
    await _record_successor(r1, r2)

    state = await _derive(service, pinned, computed)

    candidate = _candidate(state, code.lower())
    assert candidate.rule_version_id == str(r1), (
        "the counterfactual resolved a rule version this scenario never pinned")
    assert candidate.applicable_deadlines == ("A1_DEADLINE_R1",), (
        f"pinned R1's deadline was not resolved; got "
        f"{candidate.applicable_deadlines}")

    # Stated negatively as well: R2 must be unreachable from a scenario sealed
    # before it existed. Scoped to the version ids of THIS fixture rather than
    # to deadline strings — other tests publish rules into the same database,
    # and a global string assertion would be measuring their fixtures, not this
    # one's pin.
    assert str(r2) not in {x.rule_version_id for x in state.candidates}
    assert "A1_DEADLINE_R2" not in candidate.applicable_deadlines
    assert str(r2) not in [str(v) for v in pinned.pinned_rule_version_ids], (
        "a version published after the pin entered the pinned set itself")


@pytest.mark.asyncio
async def test_the_superseding_deadline_is_reachable_when_it_is_actually_pinned():
    """THE NEGATIVE CONTROL, without which the gate above proves nothing.

    If D2 were unreachable for some unrelated reason — a missing row, a broken
    join, a filter that drops every deadline — the gate would pass while
    measuring nothing. So the same fixture is re-pinned AFTER supersession and
    must now resolve D2. The only difference between the two tests is which
    version set was pinned, which is what makes the pin the demonstrated cause.
    """
    uid, analysis_id = await _user_with_analysis()
    rule_id, code = await _new_rule()
    r1 = await _publish_version(rule_id=rule_id, deadline_code="A1_CTRL_R1")

    service = ScenarioService(uid)
    spec = ScenarioSpec.parse([_lever()])
    await _pin(service, analysis_id, spec)          # R1 is pinnable at this point

    r2 = await _publish_version(
        rule_id=rule_id, deadline_code="A1_CTRL_R2", supersedes=r1)
    await _record_successor(r1, r2)

    repinned = await _pin(service, analysis_id, spec)
    assert r2 in repinned.pinned_rule_version_ids
    assert r1 not in repinned.pinned_rule_version_ids, (
        "a superseded version must not enter a NEW pin")

    state = await _derive(service, repinned, service._compute(repinned))
    candidate = _candidate(state, code.lower())
    assert candidate.rule_version_id == str(r2)
    assert candidate.applicable_deadlines == ("A1_CTRL_R2",)


@pytest.mark.asyncio
async def test_required_documents_also_come_from_the_pinned_version():
    """Deadlines are the gate, but every piece of governed metadata reached
    through a candidate travels the same path, so a second field is checked to
    show the property is about the version pin rather than about deadlines."""
    uid, analysis_id = await _user_with_analysis()
    rule_id, code = await _new_rule()
    r1 = await _publish_version(
        rule_id=rule_id, deadline_code="A1_DOC_R1", document_code="T4")

    service = ScenarioService(uid)
    pinned = await _pin(service, analysis_id, ScenarioSpec.parse([_lever()]))
    computed = service._compute(pinned)

    r2 = await _publish_version(
        rule_id=rule_id, deadline_code="A1_DOC_R2", document_code="T4A",
        supersedes=r1)
    await _record_successor(r1, r2)

    candidate = _candidate(await _derive(service, pinned, computed), code.lower())
    assert candidate.required_documents == (("T4", "required"),)


# ---------------------------------------------------------------------------
# §2 — retained, not recomputed
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_retaining_the_engine_output_costs_no_extra_engine_execution(monkeypatch):
    """Counted, not reasoned about.

    Every module-level binding of the engine entry point is wrapped, so a helper
    that re-imports `compute` behind `_compute`'s back is counted too — which is
    exactly what `eligibility.engine_facts_for` does.
    """
    import app.services.ioe.portfolio.eligibility as eligibility
    import app.services.ioe.portfolio.service as portfolio
    import app.services.ioe.scenario.service as scenario_service
    import app.services.tax_engine.core.engine as engine
    import app.services.tax_engine.service as tax_engine_service

    real = engine.compute
    executions: list[int] = []

    def counting(inp):
        executions.append(1)
        return real(inp)

    for module in (engine, scenario_service, portfolio, eligibility,
                   tax_engine_service):
        if getattr(module, "compute", None) is not None:
            monkeypatch.setattr(module, "compute", counting)

    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    pinned = await _pin(service, analysis_id, ScenarioSpec.parse([_lever()]))

    executions.clear()
    computed = service._compute(pinned)

    assert len(executions) == 1, (
        f"_compute ran the tax engine {len(executions)} times. Retaining the "
        "engine's own output must not cost a second execution, and a second "
        "execution is a second chance to disagree with the number this "
        "scenario is sealed from.")
    assert computed["facts"], "the fact map must be retained, not empty"
    assert computed["line_items"], "the tax state must be retained, not empty"


# ---------------------------------------------------------------------------
# §8 — the sealed tax state is the scenario's tax state
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_sealed_tax_state_matches_the_scenario_the_result_was_sealed_from():
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    pinned = await _pin(service, analysis_id, ScenarioSpec.parse([_lever("9000")]))
    computed = service._compute(pinned)
    state = await _derive(service, pinned, computed)

    assert len(state.line_items) == len(computed["line_items"])
    assert {(i.kind, i.label, i.amount) for i in state.line_items} == {
        (item["kind"], item["label"], c.money(item["amount"]))
        for item in computed["line_items"]
    }
    # and the retained state is the LEVERED one, not the baseline
    taxable = next(i for i in state.line_items if i.label == "Total deductions")
    assert Decimal(taxable.amount) >= Decimal("9000.00")


# ---------------------------------------------------------------------------
# §6, §10 — identity and determinism over the real evaluator
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_candidate_identity_is_code_and_pinned_version():
    uid, analysis_id = await _user_with_analysis()
    rule_id, code = await _new_rule()
    r1 = await _publish_version(rule_id=rule_id, deadline_code="A1_ID_R1")

    service = ScenarioService(uid)
    pinned = await _pin(service, analysis_id, ScenarioSpec.parse([_lever()]))
    state = await _derive(service, pinned, service._compute(pinned))

    assert _candidate(state, code.lower()).candidate_key == f"{code.lower()}:{r1}"
    keys = [x.candidate_key for x in state.candidates]
    assert keys == sorted(keys), "candidates must be sealed in identity order"
    assert len(keys) == len(set(keys)), "candidate identity must be unique"


@pytest.mark.asyncio
async def test_two_derivations_of_one_scenario_agree_byte_for_byte():
    uid, analysis_id = await _user_with_analysis()
    await _publish_version(
        rule_id=(await _new_rule())[0], deadline_code="A1_DET_R1")

    service = ScenarioService(uid)
    pinned = await _pin(service, analysis_id, ScenarioSpec.parse([_lever()]))
    computed = service._compute(pinned)

    first = await _derive(service, pinned, computed)
    second = await _derive(service, pinned, computed)
    assert counterfactual.canonical_payload(first) == (
        counterfactual.canonical_payload(second))
    assert counterfactual.derived_state_hash(first) == (
        counterfactual.derived_state_hash(second))


@pytest.mark.asyncio
async def test_a_scenario_that_pins_no_rules_seals_an_evaluated_empty_state():
    """§10. Zero candidates is a RESULT — "the pinned rule universe produced
    nothing" — and it must be a well-formed sealed state, because the alternative
    is that emptiness and never-having-evaluated look identical to a reader."""
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    pinned = await _pin(service, analysis_id, ScenarioSpec.parse([_lever()]))
    computed = service._compute(pinned)

    pinned.pinned_rule_version_ids = []
    state = await _derive(service, pinned, computed)

    assert state.candidates == ()
    assert state.pinned_rule_version_ids == ()
    assert state.line_items, "an empty candidate set does not empty the tax state"
    assert state.schema_version == (
        counterfactual.COUNTERFACTUAL_DERIVED_STATE_SCHEMA_VERSION)
    assert len(counterfactual.derived_state_hash(state)) == 64


@pytest.mark.asyncio
async def test_the_derived_state_hash_is_stable_across_hash_seeds(tmp_path):
    """§10. `PYTHONHASHSEED` randomizes set and dict iteration order, so a hash
    built from anything unordered moves between processes. Run in subprocesses
    because the seed is fixed at interpreter start."""
    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    rule_id, _ = await _new_rule()
    await _publish_version(rule_id=rule_id, deadline_code="A1_SEED_R1")
    pinned = await _pin(service, analysis_id, ScenarioSpec.parse([_lever()]))
    computed = service._compute(pinned)
    state = await _derive(service, pinned, computed)
    payload = counterfactual.canonical_payload(state)

    script = tmp_path / "seeded.py"
    script.write_text(
        "import json, sys\n"
        "from app.services.ioe.domain import canonical as c\n"
        "payload = json.loads(sys.argv[1])\n"
        "print(c.domain_hash(c.DOMAIN_COUNTERFACTUAL_DERIVED_STATE, payload))\n"
    )
    encoded = json.dumps(payload)
    digests = set()
    for seed in ("0", "1", "42"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": "."}
        out = subprocess.run(                      # noqa: S603
            [sys.executable, str(script), encoded],
            capture_output=True, text=True, check=True, env=env,
        )
        digests.add(out.stdout.strip())

    assert len(digests) == 1, (
        f"the derived-state hash moved with PYTHONHASHSEED: {digests}")
    assert digests == {counterfactual.derived_state_hash(state)}


# ---------------------------------------------------------------------------
# §11 — A1 persists nothing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_deriving_writes_no_counterfactual_column():
    """The scope boundary, measured against the database rather than the diff."""
    from app.database.models import ScenarioResult

    uid, analysis_id = await _user_with_analysis()
    service = ScenarioService(uid)
    spec = ScenarioSpec.parse([_lever()])
    outcome = await service.simulate(analysis_id, spec)

    pinned = await _pin(service, analysis_id, spec)
    await _derive(service, pinned, service._compute(pinned))

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        result = await s.scalar(
            select(ScenarioResult).where(
                ScenarioResult.scenario_id == outcome.scenario_id))
        assert result.counterfactual_derived_state is None
        assert result.counterfactual_derived_state_hash is None
        assert result.result_schema_version == "1.0.0"
