"""Entry 12B1 — historical held evidence, frozen at T1.

THE GAP THIS ENTRY CLOSES. `GraphLoader._held_documents` reads `docs.document`
LIVE. That is right for the CURRENT graph and wrong for a sealed scenario: a
scenario sealed in March describes a March baseline, and if the readiness beside
it is recomputed from today's document library then uploading a slip in June
silently rewrites what the sealed comparison says was missing.

Nothing shipped depends on that yet — the Before-You-Act comparison does not
exist — so this is a DESIGN GAP measured before it becomes a production defect,
which is the only cheap moment to fix it.

The tests are in two halves. The first reproduces the drift against current
behaviour and must keep passing afterwards, because the CURRENT graph is
supposed to move. The second proves the SEALED artifact does not.
"""
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    Document,
    DocumentType,
    IncomeSource,
    IncomeType,
    ScenarioResult,
    TaxProfile,
    UserAccount,
)
from app.database.session import unit_of_work
from app.services.ioe.domain.scenario import (
    SCENARIO_RESULT_SCHEMA_V1,
    SCENARIO_RESULT_SCHEMA_V2,
    ScenarioSpec,
)
from app.services.ioe.scenario.service import ScenarioService
from app.services.state_graph.loader import GraphLoader
from tests.conftest import frozen_snapshot

TAX_YEAR = 2025
RRSP = "INCREASE_RRSP_DEDUCTION"


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _spec(amount="5000"):
    return ScenarioSpec.parse(
        [{"lever_code": RRSP, "parameters": {"amount": Decimal(amount)}}])


async def _user_with_analysis() -> tuple[uuid.UUID, uuid.UUID]:
    async with unit_of_work(actor_type="system") as s:
        account = UserAccount(
            email=f"hev_{uuid.uuid4().hex[:8]}@test.ca", status="active")
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
        await s.flush()
        return uid, run.id


async def _document_type(code: str) -> uuid.UUID:
    async with unit_of_work(actor_type="system") as s:
        found = await s.scalar(select(DocumentType).where(DocumentType.code == code))
        assert found is not None, f"{code} must exist in ref.document_type"
        return found.id


async def _hold(uid: uuid.UUID, code: str, *, tax_year: int = TAX_YEAR,
                status: str = "processed", deleted: bool = False) -> uuid.UUID:
    type_id = await _document_type(code)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        document = Document(
            user_id=uid, document_type_id=type_id, tax_year=tax_year,
            bucket="b", object_key=f"{uid}/v2/{uuid.uuid4()}",
            content_hash=uuid.uuid4().hex, status=status,
            deleted_at=datetime.now(tz=UTC) if deleted else None,
        )
        s.add(document)
        await s.flush()
        return document.id


async def _tombstone(uid: uuid.UUID, document_id: uuid.UUID) -> None:
    """Delete as the product deletes: a tombstone, not a row removal."""
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        document = await s.get(Document, document_id)
        document.deleted_at = datetime.now(tz=UTC)
        await s.flush()


async def _live_held_types(uid: uuid.UUID) -> set[str]:
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        held = await GraphLoader(s, uid)._held_documents(TAX_YEAR)
    return {code for _, code in held}


async def _seal_v2(uid, analysis_id, amount="5000"):
    return await ScenarioService(uid)._simulate(
        analysis_id, _spec(amount),
        result_schema_version=SCENARIO_RESULT_SCHEMA_V2)


async def _sealed_payload(uid, scenario_id) -> dict:
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        result = await s.scalar(select(ScenarioResult).where(
            ScenarioResult.scenario_id == scenario_id))
        return result.counterfactual_derived_state


# ===========================================================================
# §1 — THE DRIFT, reproduced against current behaviour
# ===========================================================================
@pytest.mark.asyncio
async def test_the_current_graph_held_set_moves_when_the_library_moves():
    """DESIGN_GAP — HISTORICAL_HELD_EVIDENCE_NOT_FROZEN, measured.

    Three independent mutations, each of which changes what
    `GraphLoader._held_documents` returns for the SAME (user, tax year). That is
    correct for the current graph and is exactly why a sealed scenario cannot be
    allowed to read it later.
    """
    uid, _ = await _user_with_analysis()
    t4 = await _hold(uid, "T4")
    assert await _live_held_types(uid) == {"T4"}

    # A. a previously-missing type arrives
    await _hold(uid, "RRSP")
    assert await _live_held_types(uid) == {"T4", "RRSP"}

    # B. a previously-held document is deleted
    await _tombstone(uid, t4)
    assert await _live_held_types(uid) == {"RRSP"}

    # C. a document that stops qualifying
    other = await _hold(uid, "T4A")
    assert "T4A" in await _live_held_types(uid)
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        (await s.get(Document, other)).status = "quarantined"
    assert "T4A" not in await _live_held_types(uid), (
        "a quarantined document still counts as evidence")


@pytest.mark.asyncio
async def test_a_v1_scenario_records_nothing_about_what_was_held():
    """The other half of the gap, and why legacy rows can never answer it.

    A v1 scenario carries no derived state, so "what evidence was held when this
    was sealed" has no source but the live library — the drift above. Production
    now writes v2, so this is reached through the internal seam: the point is
    about the v1 CONTRACT, which every historical row is still sealed under and
    which is never backfilled.
    """
    uid, analysis_id = await _user_with_analysis()
    await _hold(uid, "T4")
    outcome = await ScenarioService(uid)._simulate(
        analysis_id, _spec(), result_schema_version=SCENARIO_RESULT_SCHEMA_V1)

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        scenario_result = await s.scalar(select(ScenarioResult).where(
            ScenarioResult.scenario_id == outcome.scenario_id))
    assert scenario_result.result_schema_version == SCENARIO_RESULT_SCHEMA_V1
    assert scenario_result.counterfactual_derived_state is None


# ===========================================================================
# §13 — the sealed answer does not move when the library does
# ===========================================================================
@pytest.mark.asyncio
async def test_the_sealed_snapshot_survives_every_later_library_change():
    """THE CENTRAL PROOF. Seal at T1, churn the library at T2, read T1 back.

    The three mutations are the same three that move the current graph in the
    reproduction above, so this is not a weaker fixture — it is the identical
    churn with a sealed artifact in front of it.
    """
    uid, analysis_id = await _user_with_analysis()
    t4 = await _hold(uid, "T4")
    await _hold(uid, "RRSP")

    outcome = await _seal_v2(uid, analysis_id)
    sealed_t1 = (await _sealed_payload(uid, outcome.scenario_id))[
        "baseline_held_evidence"]
    assert sealed_t1["document_type_codes"] == ["RRSP", "T4"]

    # ---- T2: the library moves in all three directions ----
    await _hold(uid, "T4A")                       # A. a new type arrives
    await _tombstone(uid, t4)                     # B. a held document is deleted
    other = await _hold(uid, "T5")                # C. one stops qualifying
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        (await s.get(Document, other)).status = "quarantined"

    # the CURRENT view legitimately reflects T2 ...
    live_now = await _live_held_types(uid)
    assert live_now != {"RRSP", "T4"}, "the fixture did not actually change anything"
    assert "T4A" in live_now and "T4" not in live_now

    # ... and the SEALED artifact does not.
    sealed_t2 = (await _sealed_payload(uid, outcome.scenario_id))[
        "baseline_held_evidence"]
    assert sealed_t2 == sealed_t1
    assert sealed_t2["document_type_codes"] == ["RRSP", "T4"]


@pytest.mark.asyncio
async def test_historical_readiness_is_computed_from_the_seal_not_the_library():
    """The readiness verdict, not just the raw set.

    A requirement that was READY at T1 stays READY after the document backing it
    is deleted — because the question "was this ready when you sealed it" has one
    answer forever.
    """
    from app.services.ioe.scenario.held_evidence import (
        from_payload,
        historical_readiness,
    )
    from app.services.state_graph.contracts import EvidenceReadiness
    from app.services.state_graph.readiness import DocumentRequirement

    uid, analysis_id = await _user_with_analysis()
    t4 = await _hold(uid, "T4")
    outcome = await _seal_v2(uid, analysis_id)

    requirements = [
        DocumentRequirement(rule_version_id="r", document_type_code="T4",
                            necessity="required"),
        DocumentRequirement(rule_version_id="r", document_type_code="RRSP",
                            necessity="required"),
    ]

    def verdicts(payload):
        snapshot = from_payload(payload["baseline_held_evidence"])
        return {r.document_type_code: v
                for r, v in historical_readiness(snapshot, requirements)}

    before = verdicts(await _sealed_payload(uid, outcome.scenario_id))
    assert before == {"T4": EvidenceReadiness.READY,
                      "RRSP": EvidenceReadiness.MISSING}

    await _tombstone(uid, t4)
    await _hold(uid, "RRSP")

    after = verdicts(await _sealed_payload(uid, outcome.scenario_id))
    assert after == before, (
        "historical readiness followed the live document library")


# ===========================================================================
# §12 — one T1 snapshot, both sides of the comparison
# ===========================================================================
@pytest.mark.asyncio
async def test_a_new_requirement_moves_readiness_without_touching_held_state():
    """`READY → MISSING` must mean "this scenario needs a document you do not
    have", never "your library changed". Held evidence is the same object on
    both sides; only the requirement differs."""
    from app.services.ioe.scenario.held_evidence import (
        from_payload,
        historical_readiness,
    )
    from app.services.state_graph.contracts import EvidenceReadiness
    from app.services.state_graph.readiness import DocumentRequirement

    uid, analysis_id = await _user_with_analysis()
    await _hold(uid, "T4")
    outcome = await _seal_v2(uid, analysis_id)
    snapshot = from_payload(
        (await _sealed_payload(uid, outcome.scenario_id))["baseline_held_evidence"])

    baseline_requirement = DocumentRequirement(
        rule_version_id="r", document_type_code="T4", necessity="required")
    counterfactual_requirement = DocumentRequirement(
        rule_version_id="r", document_type_code="RRSP", necessity="required")

    (_, baseline_verdict), = historical_readiness(snapshot, [baseline_requirement])
    (_, counterfactual_verdict), = historical_readiness(
        snapshot, [counterfactual_requirement])

    assert baseline_verdict is EvidenceReadiness.READY
    assert counterfactual_verdict is EvidenceReadiness.MISSING


# ===========================================================================
# §14 / §15 — canonicalization and qualification
# ===========================================================================
@pytest.mark.asyncio
async def test_duplicate_documents_of_one_type_seal_as_one_type():
    """Readiness asks whether the type is present, never how many. Two T4 slips
    and one T4 slip are the same fact, so they must seal identically — otherwise
    uploading a second copy would change a historical hash."""
    uid_one, analysis_one = await _user_with_analysis()
    await _hold(uid_one, "T4")
    single = await _seal_v2(uid_one, analysis_one)

    uid_many, analysis_many = await _user_with_analysis()
    for _ in range(3):
        await _hold(uid_many, "T4")
    many = await _seal_v2(uid_many, analysis_many)

    left = (await _sealed_payload(uid_one, single.scenario_id))[
        "baseline_held_evidence"]
    right = (await _sealed_payload(uid_many, many.scenario_id))[
        "baseline_held_evidence"]
    assert left == right == {"schema_version": "1.0.0",
                             "document_type_codes": ["T4"]}


@pytest.mark.asyncio
async def test_only_qualifying_documents_enter_the_snapshot():
    """§15. Each exclusion is the current graph's rule, not a new one."""
    uid, analysis_id = await _user_with_analysis()
    other_uid, _ = await _user_with_analysis()

    await _hold(uid, "T4")                                   # qualifies
    await _hold(uid, "T4A", tax_year=TAX_YEAR - 1)           # wrong tax year
    await _hold(uid, "T5", deleted=True)                     # tombstoned
    await _hold(uid, "RRSP", status="uploaded")              # not ingested yet
    await _hold(uid, "T2202", status="quarantined")          # not evidence
    await _hold(other_uid, "T3")                             # another tenant

    outcome = await _seal_v2(uid, analysis_id)
    sealed = (await _sealed_payload(uid, outcome.scenario_id))[
        "baseline_held_evidence"]
    assert sealed["document_type_codes"] == ["T4"]


# ===========================================================================
# §16 — the privacy content boundary
# ===========================================================================
@pytest.mark.asyncio
async def test_no_document_identity_or_location_reaches_the_seal():
    """Asserted against the ACTUAL stored values, not a list of field names a
    rename could slip past."""
    import json

    uid, analysis_id = await _user_with_analysis()
    type_id = await _document_type("T4")
    marker_key = f"object-key-{uuid.uuid4().hex}"
    marker_hash = f"content-hash-{uuid.uuid4().hex}"
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        document = Document(
            user_id=uid, document_type_id=type_id, tax_year=TAX_YEAR,
            bucket="private-bucket", object_key=marker_key,
            content_hash=marker_hash, status="processed")
        s.add(document)
        await s.flush()
        document_id = str(document.id)

    outcome = await _seal_v2(uid, analysis_id)
    rendered = json.dumps(await _sealed_payload(uid, outcome.scenario_id))

    for forbidden in (marker_key, marker_hash, "private-bucket", document_id):
        assert forbidden not in rendered, f"{forbidden!r} reached the seal"

    evidence = (await _sealed_payload(uid, outcome.scenario_id))[
        "baseline_held_evidence"]
    assert set(evidence) == {"schema_version", "document_type_codes"}, (
        f"the snapshot grew a field beyond the readiness contract: {evidence}")


# ===========================================================================
# §21 / §9 — replay never re-reads the library
# ===========================================================================
@pytest.mark.asyncio
async def test_replaying_a_v2_scenario_reads_no_document_row():
    """INSTRUMENTED, not argued. Every statement the verification issues is
    recorded; none may touch `docs.document`.

    This is the property the whole entry exists for. If replay re-read the
    library, a document uploaded after sealing would change the rebuilt derived
    state, the inner hash would move, and verification would report tampering
    for an artifact nobody touched.
    """
    from sqlalchemy import event

    from app.services.ioe.domain.integrity import IntegrityStatus
    from app.services.ioe.replay.verification import IntegrityVerificationService

    uid, analysis_id = await _user_with_analysis()
    await _hold(uid, "T4")
    outcome = await _seal_v2(uid, analysis_id)

    # the library moves AFTER sealing — replay must not notice
    await _hold(uid, "RRSP")

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    from app.database.session import engine
    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        verdict = await IntegrityVerificationService(uid).verify(
            "scenario", outcome.scenario_id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert verdict.status is IntegrityStatus.VERIFIED, (
        f"{verdict.status} / {verdict.reason_code}")

    touched = [s for s in statements if "docs.document" in s.lower()]
    assert touched == [], (
        f"replay read the live document library {len(touched)} time(s); "
        f"first: {touched[:1]}")


@pytest.mark.asyncio
async def test_the_readiness_primitive_touches_nothing():
    """§26. Pure: no session is available to it, so it cannot query even by
    mistake — asserted over the signature rather than by hoping."""
    import inspect

    from app.services.ioe.scenario import held_evidence

    signature = inspect.signature(held_evidence.historical_readiness)
    rendered = str(signature)
    assert "session" not in rendered and "Session" not in rendered
    assert not inspect.iscoroutinefunction(held_evidence.historical_readiness)

    # Over the parsed calls rather than over the text: `DocumentRequirement` is
    # a pure dataclass in the signature, so a substring search for "Document"
    # flags a type annotation and proves nothing. What matters is what the body
    # CALLS.
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(held_evidence.historical_readiness)))
    called = {
        ast.unparse(node.func) for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert called <= {"tuple", "readiness_for", "snapshot.holds"}, (
        f"the readiness primitive calls something beyond the pure decision: "
        f"{sorted(called)}")
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Await)], (
        "the readiness primitive awaits something; it must be pure")


# ===========================================================================
# §22 — corrupting the snapshot cannot pass verification
# ===========================================================================
@pytest.mark.asyncio
async def test_editing_the_sealed_held_evidence_breaks_verification():
    """The snapshot is inside the hashed payload, so tampering with it is caught
    by the same inner reconciliation that protects everything else — no second
    digest needed."""
    import json

    from app.services.ioe.domain.integrity import IntegrityStatus
    from app.services.ioe.replay.verification import IntegrityVerificationService
    from tests.integration.test_scenario_v2_persistence import _privileged

    uid, analysis_id = await _user_with_analysis()
    await _hold(uid, "T4")
    outcome = await _seal_v2(uid, analysis_id)

    payload = await _sealed_payload(uid, outcome.scenario_id)
    payload["baseline_held_evidence"]["document_type_codes"] = ["T4", "RRSP"]
    await _privileged(
        "UPDATE ioe.scenario_result SET counterfactual_derived_state = "
        "CAST(:p AS jsonb) WHERE scenario_id = :sid",
        p=json.dumps(payload), sid=outcome.scenario_id,
    )

    verdict = await IntegrityVerificationService(uid).verify(
        "scenario", outcome.scenario_id)
    assert verdict.status is not IntegrityStatus.VERIFIED


@pytest.mark.asyncio
async def test_a_semantic_change_to_held_evidence_moves_both_hashes():
    """§8. The snapshot is bound transitively: inner hash then outer hash. Two
    scenarios differing ONLY in held evidence must differ in both."""
    from app.services.ioe.scenario import counterfactual
    from app.services.ioe.scenario.held_evidence import build_snapshot

    base = dict(line_items=[], opportunities=[], pinned_rule_version_ids=[])
    one = counterfactual.build_derived_state(
        **base, baseline_held_evidence=build_snapshot(["T4"]))
    two = counterfactual.build_derived_state(
        **base, baseline_held_evidence=build_snapshot(["T4", "RRSP"]))

    assert counterfactual.derived_state_hash(one) != (
        counterfactual.derived_state_hash(two))

    outer_one = ScenarioService.canonical_result(
        _computed_stub(), result_schema_version=SCENARIO_RESULT_SCHEMA_V2,
        counterfactual_derived_state_hash=counterfactual.derived_state_hash(one))
    outer_two = ScenarioService.canonical_result(
        _computed_stub(), result_schema_version=SCENARIO_RESULT_SCHEMA_V2,
        counterfactual_derived_state_hash=counterfactual.derived_state_hash(two))
    from app.services.ioe.domain import canonical as c

    assert c.scenario_result_hash(spec_hash="0" * 64, result=outer_one) != (
        c.scenario_result_hash(spec_hash="0" * 64, result=outer_two))


def _computed_stub() -> dict:
    import types

    from app.services.ioe.domain import confidence as support
    from app.services.ioe.domain.enums import CalculationBasis, EvidenceStatus

    return {
        "scenario_tax": Decimal("18500.00"), "tax_delta": Decimal("1500.00"),
        "objective_baseline": Decimal("20000.00"),
        "objective_scenario": Decimal("18500.00"),
        "objective_delta": Decimal("1500.00"),
        "support": support.compute(
            evidence_status=EvidenceStatus.DOCUMENTED_VERIFIED,
            calculation_basis=CalculationBasis.SCENARIO_ESTIMATE),
        "changes": [types.SimpleNamespace(
            apply_order=0, lever_code="rrsp_contribution",
            field="rrsp_deduction", new_value=Decimal("5000.00"))],
    }


# ===========================================================================
# §19 — the snapshot inherits the row's protections
# ===========================================================================
@pytest.mark.asyncio
async def test_the_sealed_snapshot_is_immutable_and_tenant_scoped():
    from sqlalchemy import text

    uid, analysis_id = await _user_with_analysis()
    other_uid, _ = await _user_with_analysis()
    await _hold(uid, "T4")
    outcome = await _seal_v2(uid, analysis_id)

    # immutability: an ordinary UPDATE of the payload is refused
    with pytest.raises(Exception) as caught:  # noqa: PT011 - DB-level refusal
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            await s.execute(text(
                "UPDATE ioe.scenario_result SET counterfactual_derived_state = "
                "NULL WHERE scenario_id = :sid"), {"sid": outcome.scenario_id})
    assert "immutab" in str(caught.value).lower() or "cannot" in str(
        caught.value).lower()

    # RLS: another tenant sees no row at all, so no snapshot either
    async with unit_of_work(user_id=other_uid, actor_type="user") as s:
        assert await s.scalar(select(ScenarioResult).where(
            ScenarioResult.scenario_id == outcome.scenario_id)) is None
        leaked = [
            row for row in await s.scalars(
                select(ScenarioResult.counterfactual_derived_state))
            if row and row.get("baseline_held_evidence")
        ]
        assert leaked == []


# ===========================================================================
# §23 / §24 — determinism and cost
# ===========================================================================
@pytest.mark.asyncio
async def test_the_snapshot_hash_is_stable_across_hash_seeds(tmp_path):
    """Set iteration order is randomized by `PYTHONHASHSEED`, and the snapshot is
    built from a set — so this is exactly where a seed dependency would hide."""
    import json
    import os
    import subprocess
    import sys

    from app.services.ioe.scenario import held_evidence

    snapshot = held_evidence.build_snapshot(
        ["T4", "RRSP", "T4A", "T5", "T2202", "T4", "RRSP"])
    payload = held_evidence.canonical_payload(snapshot)

    script = tmp_path / "seeded.py"
    script.write_text(
        "import json, sys\n"
        "from app.services.ioe.domain import canonical as c\n"
        "print(c.canonical_text(json.loads(sys.argv[1])))\n"
    )
    rendered = set()
    for seed in ("0", "1", "42"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": "."}
        out = subprocess.run(                       # noqa: S603
            [sys.executable, str(script), json.dumps(payload)],
            capture_output=True, text=True, check=True, env=env)
        rendered.add(out.stdout.strip())
    assert len(rendered) == 1, rendered
    assert payload["document_type_codes"] == sorted(
        payload["document_type_codes"])


@pytest.mark.asyncio
async def test_the_snapshot_costs_almost_nothing_to_seal():
    """§24. Measured against the same artifact A2 sized, so the JSONB decision
    is re-checked with evidence rather than assumed to still hold."""
    import json

    from sqlalchemy import text

    report = []
    for label, codes in (("none", []), ("small", ["T4"]),
                         ("moderate", ["T4", "T4A", "RRSP", "T5"]),
                         ("stress", None)):
        uid, analysis_id = await _user_with_analysis()
        if codes is None:
            async with unit_of_work(actor_type="system") as s:
                codes = [
                    row for row in await s.scalars(select(DocumentType.code))]
        for code in codes:
            await _hold(uid, code)
        outcome = await _seal_v2(uid, analysis_id)

        payload = await _sealed_payload(uid, outcome.scenario_id)
        async with unit_of_work(user_id=uid, actor_type="user") as s:
            jsonb_bytes = await s.scalar(text(
                "SELECT pg_column_size(counterfactual_derived_state) "
                "FROM ioe.scenario_result WHERE scenario_id = :sid"),
                {"sid": outcome.scenario_id})
        evidence = payload["baseline_held_evidence"]
        report.append({
            "label": label,
            "held_types": len(evidence["document_type_codes"]),
            "evidence_canonical_bytes": len(json.dumps(evidence).encode()),
            "artifact_jsonb_bytes": jsonb_bytes,
        })

    print("\n12B1 held-evidence size measurements:")  # noqa: T201
    for row in report:
        print(f"  {row}")  # noqa: T201

    stress = report[-1]
    assert stress["held_types"] >= 4, f"stress fixture too small: {report}"
    # A tuple of governed codes. If this ever approaches the candidate payload
    # it would mean the contract grew beyond type codes.
    assert stress["evidence_canonical_bytes"] < 4096, report
