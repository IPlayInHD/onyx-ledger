"""Entry 11B5H3 — sealed history, and replay after the live source is gone.

The inventory is in `docs/privacy/h3-sealed-artifact-inventory.md`. What matters
here:

  * the production replay entry point is
    `IntegrityVerificationService(user_id).verify(entity_type, entity_id)`, and
    `EntityType` is exactly OPTIMIZATION, PORTFOLIO, SCENARIO — there is no
    ANALYSIS, so the analysis snapshot is exercised INDIRECTLY, as the baseline
    every optimization and scenario replay resolves;
  * `ReplayDependencyResolver.baseline_input` reads
    `analysis.analysis_input_snapshot` and reconstructs a `TaxInput` through the
    shared codec. Its docstring says the quiet part out loud: using
    `TaxEngineService.build_input`, which reads the live financial tables, would
    report a mismatch every time a user edited last year's income.

So the question H3 answers is whether that intent is actually true of the
running code once the live rows are gone — not whether the comment says so.

THE FIXTURES ARE BUILT BY PRODUCTION SERVICES. `AnalysisService.run` freezes the
snapshot from real `finance.income_source` / `finance.expense_record` /
`profile.tax_profile` rows, and `OptimizationOrchestrator.generate` seals from
that snapshot. A hand-written snapshot would prove nothing about deletion,
because it would never have depended on the rows being deleted.

SEALED BYTES ARE COMPARED AS STORED. Every capture reads `::text` of the jsonb
straight out of PostgreSQL rather than deserializing and re-serializing through
current code, which is the only comparison a serializer change cannot hide.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import psycopg2
import pytest
from sqlalchemy import event, select

from app.database.models import (
    ExpenseCategory,
    ExpenseRecord,
    IncomeSource,
    IncomeType,
    Jurisdiction,
    RuleAction,
    RuleOutcome,
    RuleSharedResource,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import unit_of_work
from app.services.analysis.service import AnalysisService
from app.services.ioe.domain.integrity import IntegrityStatus
from app.services.ioe.domain.scenario import ScenarioSpec
from app.services.ioe.orchestrator import OptimizationOrchestrator
from app.services.ioe.replay.verification import IntegrityVerificationService
from app.services.ioe.scenario.service import ScenarioService
from tests.conftest import owner_dsn

INCOME = Decimal("88000")
EXPENSE = Decimal("1750")
PHASE = "SOURCE_DATA"

#: The governed live SOURCE_DATA tables a replay fallback would read. Taken from
#: the Entry 11B5 classification matrix, not guessed: these are the three that
#: carry values `_build_input_live` actually consumes.
LIVE_SOURCE_TABLES = ("income_source", "expense_record", "tax_profile")


@pytest.fixture(autouse=True)
async def _dispose_engines():
    yield
    from app.database.privacy_session import dispose_all_engines

    await dispose_all_engines()


def _owner():
    conn = psycopg2.connect(owner_dsn())
    conn.autocommit = True
    return conn


# --------------------------------------------------------------- detector ---
class LiveSourceWatch:
    """Records every SQL statement, so a live-source read cannot pass unseen.

    §16 asks for the least invasive reliable mechanism. A SQLAlchemy
    `before_cursor_execute` hook on the engine sees the FINAL SQL text of every
    statement the replay issues, including ones built by the ORM that no
    repository-level sentinel would catch, and it changes no production code.

    It is deliberately dumb: it stores statements and answers questions about
    them afterwards. Anything cleverer would be a second implementation of the
    thing under test.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __enter__(self) -> LiveSourceWatch:
        from app.database.session import engine

        self._engine = engine.sync_engine

        def record(conn, cursor, statement, parameters, context, executemany):
            self.statements.append(statement)

        self._record = record
        event.listen(self._engine, "before_cursor_execute", record)
        return self

    def __exit__(self, *exc) -> None:
        event.remove(self._engine, "before_cursor_execute", self._record)

    def touched(self) -> list[str]:
        """Which governed live source tables were read, if any."""
        hits = []
        for statement in self.statements:
            lowered = statement.lower()
            for table in LIVE_SOURCE_TABLES:
                if table in lowered and table not in hits:
                    hits.append(table)
        return hits


# ---------------------------------------------------------------- fixture ---
_LEVERS = [
    ("INCREASE_RRSP_DEDUCTION", "RRSP_ROOM"),
    ("INCREASE_FHSA_DEDUCTION", "FHSA_ROOM"),
    ("INCREASE_DONATIONS", None),
]


async def _publish(code: str, lever_code: str, amount: str, resource: str | None):
    async with unit_of_work(actor_type="admin") as s:
        jur = await s.scalar(select(Jurisdiction).where(Jurisdiction.code == "FED"))
        rule = TaxRule(code=code, name=f"{code} rule", category="deduction",
                       jurisdiction_id=jur.id)
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=2025, effective_date=__import__(
                "datetime").date(2025, 1, 1),
            status="published", description="h3 fixture",
            eligibility_basis_codes=["BASIS_H3"],
        )
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            title_template=f"{code} opportunity",
            economic_effect_type="current_year_tax_reduction",
            reversibility="reversible", portfolio_lever_code=lever_code,
            lever_parameters={"amount": "action.cost_amount"},
        ))
        s.add(RuleAction(
            rule_version_id=version.id, action_code="CONTRIBUTE",
            description="Contribute", effort_rating=2,
            cost_type="liquidity_commitment", cost_amount=Decimal(amount),
        ))
        if resource:
            s.add(RuleSharedResource(
                rule_version_id=version.id, resource_code=resource))
        await s.flush()


async def _live_account() -> uuid.UUID:
    async with unit_of_work(actor_type="system") as s:
        user = UserAccount(email=f"h3_{uuid.uuid4().hex[:8]}@test.ca", status="active")
        s.add(user)
        await s.flush()
        uid = user.id
        income_type_id = (await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment"))).id
        category_id = (await s.scalar(select(ExpenseCategory).limit(1))).id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        s.add(IncomeSource(
            user_id=uid, tax_year=2025, income_type_id=income_type_id,
            amount=INCOME, province_code="ON"))
        s.add(ExpenseRecord(
            user_id=uid, tax_year=2025, expense_category_id=category_id,
            amount=EXPENSE))
    return uid


async def _historical_account() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A full production-sealed chain: analysis -> optimization -> scenario.

    Every artifact is created by the service that creates it in production, so
    the sealed snapshot genuinely derives from the live rows this file deletes.
    """
    tag = uuid.uuid4().hex[:8].upper()
    for i in range(3):
        lever, resource = _LEVERS[i % len(_LEVERS)]
        await _publish(f"H3{tag}_{i}", lever, str(2000 + i * 400), resource)

    uid = await _live_account()
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        run = await AnalysisService(s).run(uid, 2025)
        analysis_id = run.id

    outcome = await OptimizationOrchestrator(uid).generate(analysis_id)
    scenario = await ScenarioService(uid).simulate(
        analysis_id,
        ScenarioSpec.parse([{"lever_code": "INCREASE_RRSP_DEDUCTION",
                             "parameters": {"amount": Decimal("5000")}}]),
    )
    return uid, analysis_id, outcome.run_id, scenario.scenario_id


# ----------------------------------------------------------------- capture --
def _capture(cur, analysis_id, run_id, scenario_id) -> dict[str, object]:
    """The AUTHORITATIVE stored representation (§39).

    `::text` of the jsonb as PostgreSQL holds it — never a Python round trip,
    because re-serializing with current code is exactly how a historical
    mutation would be hidden.
    """
    cur.execute("SELECT snapshot::text, snapshot_hash "
                "  FROM analysis.analysis_input_snapshot WHERE analysis_id = %s",
                (str(analysis_id),))
    snapshot = cur.fetchone()

    cur.execute("""
        SELECT optimization_result_hash, optimization_spec_hash, manifest_hash,
               version_manifest::text, user_constraints::text,
               assumption_set::text, rule_snapshot_id::text, tax_year,
               input_execution_policy_version
          FROM ioe.optimization_run WHERE id = %s
    """, (str(run_id),))
    run = cur.fetchone()

    cur.execute("""
        SELECT scenario_result_hash, scenario_spec_hash, manifest_hash,
               baseline_input_snapshot_hash, baseline_result_hash, tax_year,
               jurisdiction
          FROM ioe.scenario WHERE id = %s
    """, (str(scenario_id),))
    scenario = cur.fetchone()

    cur.execute("SELECT portfolio_result_hash FROM ioe.strategy_portfolio "
                " WHERE run_id = %s", (str(run_id),))
    portfolio = cur.fetchall()

    cur.execute("SELECT rs.snapshot_hash FROM ioe.run_rule_snapshot rrs "
                "  JOIN ioe.rule_snapshot rs ON rs.id = rrs.snapshot_id "
                " WHERE rrs.run_id = %s", (str(run_id),))
    rule_pin = cur.fetchall()

    return {"snapshot": snapshot, "run": run, "scenario": scenario,
            "portfolio": portfolio, "rule_pin": rule_pin}


def _artifact_counts(cur, uid, analysis_id, run_id) -> dict[str, int]:
    counts = {}
    cur.execute("SELECT count(*) FROM analysis.analysis_input_snapshot "
                " WHERE analysis_id = %s", (str(analysis_id),))
    counts["snapshots"] = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM analysis.analysis_run WHERE user_id = %s",
                (str(uid),))
    counts["analysis_runs"] = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM ioe.optimization_run WHERE user_id = %s",
                (str(uid),))
    counts["optimization_runs"] = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM ioe.scenario WHERE user_id = %s", (str(uid),))
    counts["scenarios"] = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM ioe.strategy_portfolio WHERE run_id = %s",
                (str(run_id),))
    counts["portfolios"] = cur.fetchone()[0]
    return counts


def _purge_account(cur, uid) -> None:
    cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                "VALUES (%s, 'DELETION_REQUESTED')", (str(uid),))
    for state in ("ACCESS_DISABLED", "PURGE_PENDING", "PURGING"):
        cur.execute("UPDATE identity.account_lifecycle SET state = %s "
                    " WHERE user_id = %s", (state, str(uid)))
    token = uuid.uuid4()
    cur.execute("UPDATE identity.account_lifecycle SET claimed_by = 'h3', "
                "claim_token = %s, claimed_at = now() WHERE user_id = %s",
                (str(token), str(uid)))
    cur.execute("SELECT identity.start_lifecycle_phase(%s, %s, %s, 'h3')",
                (str(uid), PHASE, str(token)))
    cur.execute("SELECT identity.purge_source_data(%s, %s, 'h3')",
                (str(uid), str(token)))
    cur.execute("SELECT identity.complete_lifecycle_phase(%s, %s, %s, 'h3')",
                (str(uid), PHASE, str(token)))


def _remaining(cur, uid) -> int:
    cur.execute("SELECT identity.count_remaining_source_data(%s)", (str(uid),))
    return cur.fetchone()[0]


# =========================================================== §18 detector ====
async def test_the_live_source_detector_fires_on_an_ordinary_calculation():
    """Guard on the guard, and the most important test in the file.

    Every later claim of the form 'replay did not read live source data' is
    worth exactly as much as this. A recorder that never catches anything would
    make silence meaningless, so it is first pointed at a calculation that
    unambiguously DOES read those tables.
    """
    uid = await _live_account()

    with LiveSourceWatch() as watch:
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await AnalysisService(s).run(uid, 2025)

    touched = watch.touched()
    assert watch.statements, "the recorder saw no SQL at all"
    for table in LIVE_SOURCE_TABLES:
        assert table in touched, (
            f"an ordinary live analysis did not read {table}; the detector "
            f"cannot be trusted to notice a replay that does. saw: {touched}")


# ================================================= §7, §8, §9, §10, §39 ======
async def test_individual_income_deletion_leaves_every_seal_byte_identical():
    """§8. The live row the snapshot was frozen from is deleted through the
    governed production path; the sealed evidence must not move."""
    uid, analysis_id, run_id, scenario_id = await _historical_account()

    baseline = await IntegrityVerificationService(uid).verify("optimization", run_id)
    assert baseline.status is IntegrityStatus.VERIFIED, (
        f"the fixture does not replay cleanly before deletion "
        f"({baseline.status}/{baseline.reason_code}); a post-deletion result "
        "would prove nothing")

    admin = _owner()
    try:
        cur = admin.cursor()
        before = _capture(cur, analysis_id, run_id, scenario_id)
        counts_before = _artifact_counts(cur, uid, analysis_id, run_id)

        from app.services.financial.service import FinancialService

        async with unit_of_work(user_id=uid, actor_type="user") as s:
            income = await s.scalar(
                select(IncomeSource).where(IncomeSource.user_id == uid))
            assert income is not None, "no live income to delete"
            assert await FinancialService(s).delete_income_source(
                uid, income.id, 2025), "the governed deletion refused"

        cur.execute("SELECT count(*) FROM finance.income_source WHERE user_id = %s",
                    (str(uid),))
        assert cur.fetchone()[0] == 0, "the live income source was not deleted"

        assert _capture(cur, analysis_id, run_id, scenario_id) == before, (
            "a sealed artifact changed when a live income source was deleted")
        assert _artifact_counts(cur, uid, analysis_id, run_id) == counts_before, (
            "the artifact population changed on deletion")
    finally:
        admin.close()


async def test_individual_expense_deletion_leaves_every_seal_byte_identical():
    """§9. Directly tested rather than argued from the income case: the two run
    through different service methods and different partitioned tables, and the
    test is cheap."""
    uid, analysis_id, run_id, scenario_id = await _historical_account()

    admin = _owner()
    try:
        cur = admin.cursor()
        before = _capture(cur, analysis_id, run_id, scenario_id)

        from app.services.financial.service import FinancialService

        async with unit_of_work(user_id=uid, actor_type="user") as s:
            expense = await s.scalar(
                select(ExpenseRecord).where(ExpenseRecord.user_id == uid))
            assert expense is not None, "no live expense to delete"
            assert await FinancialService(s).delete_expense(
                uid, expense.id, 2025), "the governed deletion refused"

        cur.execute("SELECT count(*) FROM finance.expense_record WHERE user_id = %s",
                    (str(uid),))
        assert cur.fetchone()[0] == 0, "the live expense was not deleted"

        assert _capture(cur, analysis_id, run_id, scenario_id) == before, (
            "a sealed artifact changed when a live expense was deleted")
    finally:
        admin.close()


# ======================================== §11, §12, §25, §26, §29, §40 =======
async def test_the_account_purge_leaves_every_seal_byte_identical():
    """§11. The whole SOURCE_DATA lifecycle, then the sealed evidence."""
    uid, analysis_id, run_id, scenario_id = await _historical_account()

    admin = _owner()
    try:
        cur = admin.cursor()
        before = _capture(cur, analysis_id, run_id, scenario_id)
        counts_before = _artifact_counts(cur, uid, analysis_id, run_id)
        assert _remaining(cur, uid) > 0, "nothing to purge; the test is empty"

        _purge_account(cur, uid)

        assert _remaining(cur, uid) == 0, "the purge did not complete"
        cur.execute("SELECT status FROM identity.account_lifecycle_phase "
                    " WHERE user_id = %s AND phase = %s", (str(uid), PHASE))
        assert cur.fetchone()[0] == "COMPLETE", "the phase did not complete"

        # §12 — SOURCE_DATA COMPLETE is not account deletion complete.
        cur.execute("SELECT state FROM identity.account_lifecycle WHERE user_id = %s",
                    (str(uid),))
        assert cur.fetchone()[0] != "COMPLETE", (
            "the account reached COMPLETE though only SOURCE_DATA has run")

        assert _capture(cur, analysis_id, run_id, scenario_id) == before, (
            "a sealed artifact changed during the account SOURCE_DATA purge")
        assert _artifact_counts(cur, uid, analysis_id, run_id) == counts_before, (
            "sealed artifacts disappeared or were duplicated by the purge")
    finally:
        admin.close()


# ============================== §14, §15, §16, §17, §19, §22, §25, §26, §29 ==
@pytest.mark.parametrize("entity", ["optimization", "scenario"])
async def test_replay_after_the_account_purge_verifies_from_sealed_inputs(entity):
    """THE DEFINING H3 TEST.

    A real sealed result, verified before deletion; the account's SOURCE_DATA is
    then purged; the same production replay entry point is invoked again.

    Two things must hold, and the second is the one that cannot be inferred
    from the first. VERIFIED after the purge shows the replay still reproduces
    the sealed identity — but a buggy implementation could query the live
    tables, get empty results and still stumble onto a plausible answer. So the
    SQL is recorded and required to touch none of the governed live source
    tables at all.
    """
    uid, analysis_id, run_id, scenario_id = await _historical_account()
    entity_id = run_id if entity == "optimization" else scenario_id

    baseline = await IntegrityVerificationService(uid).verify(entity, entity_id)
    assert baseline.status is IntegrityStatus.VERIFIED, (
        f"{entity} does not replay cleanly BEFORE deletion "
        f"({baseline.status}/{baseline.reason_code}) — a post-purge result "
        "would prove nothing")

    admin = _owner()
    try:
        cur = admin.cursor()
        sealed_before = _capture(cur, analysis_id, run_id, scenario_id)
        counts_before = _artifact_counts(cur, uid, analysis_id, run_id)
        cur.execute("SELECT count(*) FROM ioe.integrity_check "
                    " WHERE optimization_run_id = %s OR scenario_id = %s",
                    (str(run_id), str(scenario_id)))
        checks_before = cur.fetchone()[0]

        _purge_account(cur, uid)
        assert _remaining(cur, uid) == 0, "the purge did not complete"
        cur.execute("SELECT state FROM identity.account_lifecycle WHERE user_id = %s",
                    (str(uid),))
        lifecycle_before_replay = cur.fetchone()[0]

        # ---- the replay, watched -------------------------------------------
        with LiveSourceWatch() as watch:
            after = await IntegrityVerificationService(uid).verify(entity, entity_id)

        # §22 — deletion alone must never turn a good replay into a mismatch.
        assert after.status is not IntegrityStatus.MISMATCH, (
            f"deleting live source data turned a VERIFIED {entity} into a "
            f"MISMATCH ({after.reason_code}) — replay depends on live source")
        assert after.status is IntegrityStatus.VERIFIED, (
            f"{entity} replay after purge was {after.status}/{after.reason_code}, "
            "though every pinned version is present")

        # §16, §17 — and it got there without reading the live tables.
        touched = watch.touched()
        assert watch.statements, "the recorder saw no SQL; it was not attached"
        assert touched == [], (
            f"replay after purge read governed live source tables {touched}; "
            "it is reconstructing historical input from current tenant state "
            "instead of from the sealed snapshot")

        # §25 — nothing was recreated.
        for table in ("finance.income_source", "finance.expense_record",
                      "profile.tax_profile"):
            cur.execute(f"SELECT count(*) FROM {table} WHERE user_id = %s",
                        (str(uid),))
            assert cur.fetchone()[0] == 0, f"replay recreated rows in {table}"
        assert _remaining(cur, uid) == 0, "replay resurrected source data"

        # §26 — the deletion lifecycle is untouched.
        cur.execute("SELECT state FROM identity.account_lifecycle WHERE user_id = %s",
                    (str(uid),))
        assert cur.fetchone()[0] == lifecycle_before_replay, (
            "replay moved the account lifecycle")
        cur.execute("SELECT status FROM identity.account_lifecycle_phase "
                    " WHERE user_id = %s AND phase = %s", (str(uid), PHASE))
        assert cur.fetchone()[0] == "COMPLETE", "replay moved the SOURCE_DATA phase"

        # §27, §29, §40 — the seal and its pins are exactly as before, and no
        # replacement snapshot was quietly written.
        assert _capture(cur, analysis_id, run_id, scenario_id) == sealed_before, (
            "replay mutated a sealed artifact or its version manifest")
        assert _artifact_counts(cur, uid, analysis_id, run_id) == counts_before, (
            "replay created or removed a sealed artifact")

        # §28 — integrity evidence is append-only, and is the ONLY thing that grew.
        cur.execute("SELECT count(*) FROM ioe.integrity_check "
                    " WHERE optimization_run_id = %s OR scenario_id = %s",
                    (str(run_id), str(scenario_id)))
        assert cur.fetchone()[0] == checks_before + 1, (
            "replay did not append exactly one integrity check")
    finally:
        admin.close()


# ---------------------------------------------------------------- §23, §24 --
async def test_replay_after_purge_does_not_make_old_advice_current_again():
    """Integrity and freshness are different questions. A historical result can
    be VERIFIED — it reproduces — while being unfit as current advice, which is
    exactly the state of an account whose data has been deleted."""
    uid, analysis_id, run_id, scenario_id = await _historical_account()

    admin = _owner()
    try:
        cur = admin.cursor()
        # Put the run into a stale, non-current state first, so there is
        # something for a resurrection bug to wrongly clear.
        cur.execute("UPDATE ioe.optimization_run "
                    "   SET freshness_status = 'stale', "
                    "       stale_reason_codes = ARRAY['BASELINE_INPUTS_CHANGED'] "
                    " WHERE id = %s", (str(run_id),))
        cur.execute("SELECT freshness_status, stale_reason_codes "
                    "  FROM ioe.optimization_run WHERE id = %s", (str(run_id),))
        freshness_before = cur.fetchone()
        assert freshness_before[0] == "stale", "the fixture is not stale"

        _purge_account(cur, uid)
        result = await IntegrityVerificationService(uid).verify("optimization", run_id)
        assert result.status is IntegrityStatus.VERIFIED

        cur.execute("SELECT freshness_status, stale_reason_codes "
                    "  FROM ioe.optimization_run WHERE id = %s", (str(run_id),))
        assert cur.fetchone() == freshness_before, (
            "verifying historical integrity re-enabled the result as current "
            "advice — integrity is not freshness")

        # The integrity columns DID move: that is the append-only evidence.
        cur.execute("SELECT integrity_status FROM ioe.optimization_run WHERE id = %s",
                    (str(run_id),))
        assert cur.fetchone()[0] == "verified", "the integrity verdict was not recorded"
    finally:
        admin.close()


# ---------------------------------------------------------------- §13, §37 --
async def test_sealed_evidence_survives_purge_lease_loss_and_retry():
    """§13. The H2 recovery sequence, measured against sealed evidence rather
    than against the lifecycle: purge, worker loss, reclaim, idempotent repeat,
    completion."""
    uid, analysis_id, run_id, scenario_id = await _historical_account()

    admin = _owner()
    try:
        cur = admin.cursor()
        before = _capture(cur, analysis_id, run_id, scenario_id)
        counts_before = _artifact_counts(cur, uid, analysis_id, run_id)

        cur.execute("INSERT INTO identity.account_lifecycle (user_id, state) "
                    "VALUES (%s, 'DELETION_REQUESTED')", (str(uid),))
        for state in ("ACCESS_DISABLED", "PURGE_PENDING"):
            cur.execute("UPDATE identity.account_lifecycle SET state = %s "
                        " WHERE user_id = %s", (state, str(uid)))

        cur.execute("SELECT out_user_id, out_claim_token "
                    "  FROM identity.claim_account_lifecycle(50, 'h3-a')")
        a_token = next(t for u, t in cur.fetchall() if str(u) == str(uid))
        cur.execute("SELECT identity.start_lifecycle_phase(%s, %s, %s, 'h3-a')",
                    (str(uid), PHASE, str(a_token)))
        cur.execute("SELECT identity.purge_source_data(%s, %s, 'h3-a')",
                    (str(uid), str(a_token)))
        assert _remaining(cur, uid) == 0, "worker A's purge did not run"

        # A dies before completing; the lease expires.
        cur.execute("UPDATE identity.account_lifecycle "
                    "   SET claimed_at = now() - identity.lifecycle_claim_timeout() "
                    "                  - interval '1 minute' WHERE user_id = %s",
                    (str(uid),))
        cur.execute("SELECT out_user_id, out_claim_token "
                    "  FROM identity.claim_account_lifecycle(50, 'h3-b')")
        b_token = next(t for u, t in cur.fetchall() if str(u) == str(uid))

        cur.execute("SELECT identity.purge_source_data(%s, %s, 'h3-b')",
                    (str(uid), str(b_token)))
        cur.execute("SELECT identity.complete_lifecycle_phase(%s, %s, %s, 'h3-b')",
                    (str(uid), PHASE, str(b_token)))
        assert cur.fetchone()[0], "worker B could not complete after recovery"

        assert _capture(cur, analysis_id, run_id, scenario_id) == before, (
            "a sealed artifact changed across purge, lease loss and retry")
        assert _artifact_counts(cur, uid, analysis_id, run_id) == counts_before, (
            "the retry duplicated or removed a sealed artifact")

        # And the evidence is still replayable afterwards.
        result = await IntegrityVerificationService(uid).verify("optimization", run_id)
        assert result.status is IntegrityStatus.VERIFIED, (
            f"replay after crash recovery was {result.status}/{result.reason_code}")
    finally:
        admin.close()


# ---------------------------------------------------------------------- §32 --
async def test_one_tenants_purge_and_replay_cannot_reach_another_tenant():
    """Both accounts are real, both sealed. A is purged and replayed under its
    OWN identity through the ordinary RLS-scoped service — no superuser, since
    a tenant-isolation claim proven as superuser proves nothing."""
    a_uid, a_analysis, a_run, a_scenario = await _historical_account()
    b_uid, b_analysis, b_run, b_scenario = await _historical_account()

    admin = _owner()
    try:
        cur = admin.cursor()
        b_before = _capture(cur, b_analysis, b_run, b_scenario)
        cur.execute("SELECT count(*) FROM finance.income_source WHERE user_id = %s",
                    (str(b_uid),))
        b_income_before = cur.fetchone()[0]
        assert b_income_before > 0, "tenant B has no live data to protect"

        _purge_account(cur, a_uid)
        assert _remaining(cur, a_uid) == 0
        assert _remaining(cur, b_uid) > 0, "tenant A's purge reached tenant B"

        with LiveSourceWatch() as watch:
            result = await IntegrityVerificationService(a_uid).verify(
                "optimization", a_run)
        assert result.status is IntegrityStatus.VERIFIED
        assert watch.touched() == [], (
            f"tenant A's replay read live source tables {watch.touched()}")

        assert _capture(cur, b_analysis, b_run, b_scenario) == b_before, (
            "tenant A's purge and replay altered tenant B's sealed evidence")
        cur.execute("SELECT count(*) FROM finance.income_source WHERE user_id = %s",
                    (str(b_uid),))
        assert cur.fetchone()[0] == b_income_before, "tenant B lost live data"

        # A cannot verify B's entity: ownership is checked in the service AND
        # by RLS, and the refusal is NotFound rather than Forbidden.
        from app.core.exceptions import DomainError

        with pytest.raises(DomainError):
            await IntegrityVerificationService(a_uid).verify("optimization", b_run)
    finally:
        admin.close()
