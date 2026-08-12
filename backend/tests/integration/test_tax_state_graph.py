"""Entry 12A — the Personal Tax State Graph, assembled from real sealed state.

The unit suite proves the contract holds in isolation. These prove it holds
against a database: that assembly emits zero reserved nodes when there is
genuinely something to assemble, that the hash is deterministic over real rows,
that the loading plan does not degrade into per-node lookups, that nothing about
where a document is stored escapes into the read model, and that no table was
added to the privacy universe to make any of it work.
"""
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import event, select, text

from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisRun,
    Document,
    DocumentType,
    IncomeSource,
    IncomeType,
    Jurisdiction,
    RuleAction,
    RuleOutcome,
    RuleRequiredDocument,
    TaxProfile,
    TaxRule,
    TaxRuleVersion,
    UserAccount,
)
from app.database.session import unit_of_work
from app.services.ioe.domain import canonical as c
from app.services.ioe.orchestrator import OptimizationOrchestrator
from app.services.state_graph import (
    PRODUCERS,
    RESERVED_NODE_TYPES,
    GraphLoader,
    NodeType,
    TaxStateGraphService,
    assemble_graph,
)
from app.services.state_graph.hashing import graph_hash_payload
from tests.conftest import frozen_snapshot


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


def _suffix() -> str:
    return uuid.uuid4().hex[:6].upper()


async def _user_with_analysis(
    *, income_rows: int = 1, employment: str = "95000"
) -> tuple[uuid.UUID, uuid.UUID]:
    """A user with real employment income and a completed analysis."""
    async with unit_of_work(actor_type="system") as s:
        account = UserAccount(
            email=f"graph_{uuid.uuid4().hex[:8]}@test.ca", status="active"
        )
        s.add(account)
        await s.flush()
        uid = account.id
        income_type = await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment")
        )
        income_type_id = income_type.id

    async with unit_of_work(user_id=uid, actor_type="user") as s:
        s.add(TaxProfile(user_id=uid, province_code="ON", marital_status="single"))
        for _ in range(income_rows):
            s.add(IncomeSource(
                user_id=uid, tax_year=2025, income_type_id=income_type_id,
                amount=Decimal(employment) / income_rows, province_code="ON",
            ))
        run = AnalysisRun(
            user_id=uid, tax_year=2025, province_code="ON", engine_version="py-1.0.0",
            status="completed", started_at=datetime.now(tz=UTC),
            completed_at=datetime.now(tz=UTC), data_verified=True,
        )
        s.add(run)
        await s.flush()
        payload, digest = frozen_snapshot(employment_income=Decimal(employment))
        s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=payload, snapshot_hash=digest,
        ))
        await s.flush()
        return uid, run.id


async def _published_rule_requiring(document_type_code: str) -> uuid.UUID:
    """A published rule that names a required document, so readiness has
    governed metadata to resolve rather than a fixture's opinion."""
    async with unit_of_work(actor_type="admin") as s:
        jurisdiction = await s.scalar(
            select(Jurisdiction).where(Jurisdiction.code == "FED")
        )
        code = f"GRAPH_{_suffix()}"
        rule = TaxRule(
            code=code, name=f"{code} rule", category="deduction",
            jurisdiction_id=jurisdiction.id,
        )
        s.add(rule)
        await s.flush()
        version = TaxRuleVersion(
            tax_rule_id=rule.id, tax_year=2025, effective_date=date(2025, 1, 1),
            status="published", description="12A fixture",
            eligibility_basis_codes=["BASIS_12A"],
        )
        s.add(version)
        await s.flush()
        s.add(RuleOutcome(
            rule_version_id=version.id, outcome_type="recommend", priority=1,
            title_template=f"{code} opportunity",
            economic_effect_type="current_year_tax_reduction",
            reversibility="reversible",
            portfolio_lever_code="rrsp_contribution",
            lever_parameters={"amount": "action.cost_amount"},
        ))
        s.add(RuleAction(
            rule_version_id=version.id, action_code="CONTRIBUTE",
            description="Contribute", effort_rating=2,
            cost_type="liquidity_commitment", cost_amount=Decimal("5000"),
        ))
        s.add(RuleRequiredDocument(
            rule_version_id=version.id,
            document_type_code=document_type_code,
            necessity="required",
        ))
        await s.flush()
        return version.id


async def _build(user_id: uuid.UUID, tax_year: int = 2025):
    async with unit_of_work(user_id=user_id, actor_type="user") as s:
        return await TaxStateGraphService(s, user_id).build(tax_year=tax_year)


# ---------------------------------------------------------------------------
# The invariant this entry exists to hold
# ---------------------------------------------------------------------------
async def test_a_real_assembly_emits_zero_obligation_and_zero_decision_nodes():
    """The required 12A invariant, on real output rather than an empty list.

    The assertion that the graph is NOT empty is load-bearing: a zero count is
    satisfied just as well by a graph that assembled nothing, and that version of
    the test would keep passing after the producers broke.
    """
    user_id, analysis_id = await _user_with_analysis()
    await _published_rule_requiring("T4")
    await OptimizationOrchestrator(user_id).generate(analysis_id)

    graph = await _build(user_id)

    assert graph.nodes, "nothing was assembled — the zero assertions below prove nothing"
    for reserved in RESERVED_NODE_TYPES:
        assert graph.nodes_of(reserved) == (), f"{reserved} was emitted"
        assert graph.summary.nodes_by_type[reserved.value] == 0


async def test_every_node_type_the_graph_emitted_has_a_declared_producer():
    """A node type appearing in output without a row in the producer matrix
    would be exactly the failure the matrix was written to prevent."""
    user_id, analysis_id = await _user_with_analysis()
    await _published_rule_requiring("T4")
    await OptimizationOrchestrator(user_id).generate(analysis_id)

    graph = await _build(user_id)
    emitted = {n.node_type for n in graph.nodes}
    assert emitted, "nothing was assembled"
    undeclared = sorted(t.value for t in emitted if t not in PRODUCERS)
    assert undeclared == [], f"emitted without a producer: {undeclared}"


# ---------------------------------------------------------------------------
# Determinism — the property that makes persistence unnecessary
# ---------------------------------------------------------------------------
async def test_the_same_underlying_state_assembles_to_the_same_hash():
    user_id, analysis_id = await _user_with_analysis()
    await OptimizationOrchestrator(user_id).generate(analysis_id)

    first = await _build(user_id)
    second = await _build(user_id)
    assert first.graph_hash == second.graph_hash
    assert first.nodes == second.nodes
    assert first.edges == second.edges


async def test_changed_state_changes_the_hash():
    """The other half. A hash that never moves is a constant, not a summary."""
    user_id, _ = await _user_with_analysis()
    before = await _build(user_id)

    async with unit_of_work(user_id=user_id, actor_type="user") as s:
        income_type = await s.scalar(
            select(IncomeType).where(IncomeType.code == "employment")
        )
        s.add(IncomeSource(
            user_id=user_id, tax_year=2025, income_type_id=income_type.id,
            amount=Decimal("1234.56"), province_code="ON",
        ))

    after = await _build(user_id)
    assert before.graph_hash != after.graph_hash


# ---------------------------------------------------------------------------
# The loading plan
# ---------------------------------------------------------------------------
def _count_statements() -> tuple[list[str], object]:
    from app.database.session import engine

    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    return seen, _record


def _stop_counting(listener: object) -> None:
    from app.database.session import engine

    event.remove(engine.sync_engine, "before_cursor_execute", listener)


async def test_assembly_issues_a_bounded_number_of_queries():
    """The regression this guards is a per-node lookup appearing in a later
    edit. Comparing a small graph against a much larger one is the only form of
    that promise a test can actually keep — a fixed ceiling alone would pass a
    loader that issued one query per node as long as the fixture stayed small.
    """
    small_user, _ = await _user_with_analysis(income_rows=1)
    large_user, _ = await _user_with_analysis(income_rows=25)

    seen, listener = _count_statements()
    try:
        del seen[:]
        small = await _build(small_user)
        small_queries = len(seen)

        del seen[:]
        large = await _build(large_user)
        large_queries = len(seen)
    finally:
        _stop_counting(listener)

    assert len(large.nodes) > len(small.nodes) + 20, (
        "the two fixtures produced similar graphs, so this measures nothing"
    )
    assert large_queries == small_queries, (
        f"query count grew with node count: {small_queries} -> {large_queries}"
    )


# ---------------------------------------------------------------------------
# What must not escape into the read model
# ---------------------------------------------------------------------------
async def test_no_document_location_reaches_the_graph():
    """A graph says a document of some type exists. It never says where it is
    stored or what it contains — object key, bucket, content hash and filename
    are all absent, asserted against the actual stored values rather than
    against a list of field names the code might have renamed."""
    user_id, _ = await _user_with_analysis()
    marker = f"secret-object-key-{uuid.uuid4().hex}"
    content_marker = f"content-hash-{uuid.uuid4().hex}"

    async with unit_of_work(user_id=user_id, actor_type="user") as s:
        doc_type = await s.scalar(select(DocumentType).limit(1))
        s.add(Document(
            user_id=user_id, document_type_id=doc_type.id, tax_year=2025,
            bucket="private-bucket", object_key=marker,
            content_hash=content_marker, status="processed",
        ))

    graph = await _build(user_id)
    rendered = c.canonical_text(graph_hash_payload(
        scope=graph.scope, anchors=graph.anchors, nodes=graph.nodes,
        edges=graph.edges, summary=graph.summary,
    ))

    assert graph.nodes_of(NodeType.EVIDENCE), "no evidence node was produced"
    assert marker not in rendered
    assert content_marker not in rendered
    assert "private-bucket" not in rendered


async def test_readiness_is_carried_alongside_evidence_status_not_instead_of_it():
    """Two axes, both present. Collapsing them is the mistake the repository
    already refuses one level down between EvidenceStatus and CalculationBasis.
    """
    user_id, analysis_id = await _user_with_analysis()
    await _published_rule_requiring("T4")
    await OptimizationOrchestrator(user_id).generate(analysis_id)

    graph = await _build(user_id)
    opportunities = graph.nodes_of(NodeType.OPPORTUNITY)
    if not opportunities:
        pytest.skip(
            "no candidate survived rule evaluation in this shared database; "
            "the two-axis assertion needs a candidate to carry them"
        )
    for node in opportunities:
        assert "evidence_status" in node.attributes
        assert "readiness" in node.attributes


async def test_requirements_are_reported_even_when_nothing_satisfies_them():
    """MISSING has to be a state rather than an absence, which is why an
    EVIDENCE node is keyed by the requirement and not by a document."""
    user_id, analysis_id = await _user_with_analysis()
    await _published_rule_requiring("T4")
    await OptimizationOrchestrator(user_id).generate(analysis_id)

    graph = await _build(user_id)
    requirements = [
        n for n in graph.nodes_of(NodeType.EVIDENCE)
        if n.attributes.get("evidence_kind") == "requirement"
    ]
    if not requirements:
        pytest.skip("no candidate pinned a rule version carrying a requirement")
    assert any(
        n.attributes.get("readiness") in ("MISSING", "READY", "UNKNOWN")
        for n in requirements
    )


# ---------------------------------------------------------------------------
# Lifecycle and persistence
# ---------------------------------------------------------------------------
async def test_a_deleting_account_is_refused_by_the_ordinary_admission_model():
    """No graph-specific exemption. The cutoff that refuses every other
    protected read refuses this one, through the same call."""
    from app.services.privacy.lifecycle import (
        AccountDeletionInProgress,
        AccountLifecycleService,
    )

    user_id, _ = await _user_with_analysis()
    await _build(user_id)  # works before the cutoff

    async with unit_of_work(user_id=user_id, actor_type="user") as s:
        await AccountLifecycleService(s).request_deletion(user_id)

    async with unit_of_work(user_id=user_id, actor_type="user") as s:
        with pytest.raises(AccountDeletionInProgress):
            await AccountLifecycleService(s).assert_may_act(user_id)


def test_the_graph_does_not_grant_itself_a_lifecycle_exemption():
    """Structural, because the runtime test above passes either way: the graph
    could still be wired to the exempt dependency at a route. The only exemption
    in the repository belongs to the deletion endpoints themselves."""
    import pathlib

    package = pathlib.Path("app/services/state_graph")
    sources = "\n".join(p.read_text() for p in package.rglob("*.py"))
    assert "db_authed_lifecycle_exempt" not in sources
    # A CALL, not a mention: `service.py` explains in prose why the check lives
    # in `db_authed` instead, and a test that could not tell the two apart would
    # forbid documenting the decision.
    assert "assert_may_act(" not in sources
    assert "AccountLifecycleService" not in sources


async def test_no_graph_table_was_added_to_the_database():
    """The §44 persistence decision, asserted rather than asserted-in-prose. A
    graph table would need a registry classification, a purge keyhole, a
    completion guard, a write cutoff and a lifecycle phase slot."""
    async with unit_of_work(actor_type="system") as s:
        found = await s.scalars(text(
            "SELECT table_schema || '.' || table_name "
            "FROM information_schema.tables "
            "WHERE table_name LIKE '%%state_graph%%' OR table_name LIKE '%%tax_graph%%'"
        ))
        assert list(found) == []


async def test_the_loader_reads_only_what_the_producers_declare():
    """The producer matrix names the tables each type reads. A loader that
    reached beyond them would mean the matrix had stopped describing the code.

    The fixture RUNS THE OPTIMIZER on purpose. An earlier version did not, so
    `run` was None, the whole portfolio branch never executed, and the test
    silently certified a loading plan it had never observed.
    """
    user_id, analysis_id = await _user_with_analysis()
    await _published_rule_requiring("T4")
    await OptimizationOrchestrator(user_id).generate(analysis_id)
    declared = {
        table.split(".")[0] + "." + table.split(".")[1]
        for spec in PRODUCERS.values() for table in spec.source_tables
    }
    # The join `docs.document -> ref.document_type` is how a held document's
    # governed type code is resolved; it is declared on the EVIDENCE producer.
    seen, listener = _count_statements()
    try:
        del seen[:]
        async with unit_of_work(user_id=user_id, actor_type="user") as s:
            await GraphLoader(s, user_id).load(tax_year=2025)
    finally:
        _stop_counting(listener)

    read_tables = {
        f"{schema}.{table}"
        for statement in seen
        for schema, table in _tables_in(statement)
    }
    assert len(read_tables) >= 8, (
        f"the table extractor found only {sorted(read_tables)}; a test that sees "
        "almost nothing cannot certify what the loader reads"
    )
    undeclared = sorted(read_tables - declared)
    assert undeclared == [], f"the loader read undeclared tables: {undeclared}"


def _tables_in(statement: str) -> set[tuple[str, str]]:
    """Schema-qualified table references in a statement.

    Deliberately crude and deliberately over-inclusive: it is looking for a read
    the producer matrix does not mention, so a false positive is a finding to
    investigate and a false negative would be the failure.
    """
    import re

    found: set[tuple[str, str]] = set()
    for match in re.finditer(
        r"\b(?:FROM|JOIN)\s+(\w+)\.(\w+)", statement, flags=re.IGNORECASE
    ):
        found.add((match.group(1), match.group(2)))
    return found


async def test_an_account_with_no_analysis_still_reports_its_declared_facts():
    """An empty graph and a graph of facts with no analysis are different
    situations and must not look the same — the second is a user who has entered
    data and not yet run anything."""
    user_id, _ = await _user_with_analysis()
    graph = await _build(user_id, tax_year=2019)  # a year with no analysis
    assert graph.nodes_of(NodeType.TAX_STATE) == ()
    assert graph.nodes_of(NodeType.FACT), (
        "profile and wealth facts are not tax-year scoped and should still appear"
    )


async def test_assembly_is_pure_and_needs_no_session():
    """The assembler cannot reach back to the database for a value it forgot,
    which is what keeps it a projection rather than somewhere a second engine
    could grow."""
    user_id, _ = await _user_with_analysis()
    async with unit_of_work(user_id=user_id, actor_type="user") as s:
        sources = await GraphLoader(s, user_id).load(tax_year=2025)

    # No session in scope at all — this would raise if assembly touched one.
    graph = assemble_graph(sources, user_id=user_id, tax_year=2025)
    assert graph.graph_hash
