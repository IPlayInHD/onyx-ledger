"""Tenant-isolation inventory for every user-derived IOE evidence table.

Deliberately catalogue-driven rather than a hand-written list. A table added to
`ioe` in a later phase without an ownership policy fails here, so the isolation
guarantee cannot quietly decay as the schema grows.

Four properties are asserted for every table holding user-derived evidence:
  1. RLS is ENABLED and FORCED (so the table owner is subject to it too),
  2. an ownership policy exists that resolves to a user_id-bearing row,
  3. with `app.user_id` unset the table returns NOTHING — deny by default,
  4. the column each policy traverses is indexed.
"""
import pytest
from sqlalchemy import text

from app.database.session import unit_of_work
from app.services.ioe.orchestrator import OptimizationOrchestrator

# Tables in `ioe` that hold no user-derived data: pinned rule snapshots, scoring
# weights, and the assumption vocabulary are shared reference data. They are
# readable by design and are excluded from the ownership requirement.
SHARED_REFERENCE_TABLES = frozenset({
    "rule_snapshot",
    "rule_snapshot_artifact",
    "weight_config",
    "assumption_set",
    "assumption",
    "run_rule_snapshot",
    # Queue bookkeeping: transitions, worker ids and enumerated codes. Carries
    # no user data and is written only by the privileged outbox functions.
    "freshness_outbox_audit",
    # Deployment state (Entry 9): which version of each calculation component is
    # active. One row per component, no user_id, no per-tenant meaning — the
    # same class as `weight_config`.
    "active_calculation_version",
})

# How each user-derived table resolves to a user_id.
OWNERSHIP_CHAIN = {
    "optimization_run": "user_id (direct)",
    "scenario": "user_id (direct)",
    "optimization_candidate": "run_id -> optimization_run.user_id",
    "optimization_run_event": "run_id -> optimization_run.user_id",
    "recommendation_relationship": "run_id -> optimization_run.user_id",
    "run_rule_version": "run_id -> optimization_run.user_id",
    "strategy_portfolio": "run_id -> optimization_run.user_id",
    "multi_year_projection": "run_id -> optimization_run.user_id",
    "candidate_cost": "candidate_id -> optimization_candidate -> run",
    "candidate_economic_effect": "candidate_id -> optimization_candidate -> run",
    "confidence_component": "candidate_id -> optimization_candidate -> run",
    "score_component": "candidate_id -> optimization_candidate -> run",
    "portfolio_member": "portfolio_id -> strategy_portfolio -> run",
    "portfolio_evaluation_step": "portfolio_id -> strategy_portfolio -> run",
    "portfolio_exclusion": "portfolio_id -> strategy_portfolio -> run",
    "resource_ledger_entry": "portfolio_id -> strategy_portfolio -> run",
    "scenario_event": "scenario_id -> scenario.user_id",
    "scenario_input_change": "scenario_id -> scenario.user_id",
    "scenario_result": "scenario_id -> scenario.user_id",
    "scenario_lever": "scenario_id -> scenario.user_id",
    "scenario_assumption": "scenario_id -> scenario.user_id",
    "scenario_confidence_component": "scenario_id -> scenario.user_id",
    # Not sealed evidence — identifiers and codes only — but still tenant-scoped
    # so one user cannot enumerate another's activity. Rows with a NULL user_id
    # are global (an engine version moved) and are readable by design.
    "freshness_outbox": "user_id (direct, NULL = global event)",
    # Replay-verification history. Carries `user_id` directly and deliberately:
    # resolving ownership through three nullable entity parents on every row
    # would make the policy unindexable. Hashes here are diagnostic, never
    # authorization.
    "integrity_check": "user_id (direct)",
}

# The column each ownership policy resolves through, and the two parents the
# chains terminate at.
TRAVERSAL_COLUMN = {
    "optimization_candidate": "run_id", "optimization_run_event": "run_id",
    "recommendation_relationship": "run_id", "run_rule_version": "run_id",
    "strategy_portfolio": "run_id", "multi_year_projection": "run_id",
    "candidate_cost": "candidate_id", "candidate_economic_effect": "candidate_id",
    "confidence_component": "candidate_id", "score_component": "candidate_id",
    "portfolio_member": "portfolio_id", "portfolio_evaluation_step": "portfolio_id",
    "portfolio_exclusion": "portfolio_id", "resource_ledger_entry": "portfolio_id",
    "scenario_event": "scenario_id", "scenario_input_change": "scenario_id",
    "scenario_result": "scenario_id",
    "scenario_lever": "scenario_id", "scenario_assumption": "scenario_id",
    "scenario_confidence_component": "scenario_id",
    "optimization_run": "user_id", "scenario": "user_id",
    "freshness_outbox": "user_id",
}


@pytest.fixture(autouse=True)
async def _dispose_engine():
    yield
    from app.database.session import engine

    await engine.dispose()


@pytest.mark.asyncio
async def test_inventory_covers_every_ioe_table():
    """No table may be silently absent from the classification above."""
    async with unit_of_work(actor_type="system") as s:
        rows = await s.execute(text(
            "SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'ioe' AND c.relkind = 'r' ORDER BY c.relname"
        ))
        tables = {r[0] for r in rows}

    unclassified = tables - (set(OWNERSHIP_CHAIN) | SHARED_REFERENCE_TABLES)
    assert not unclassified, (
        "new ioe tables must be classified as user-derived (with an ownership "
        f"chain) or as shared reference data: {sorted(unclassified)}"
    )
    stale = (set(OWNERSHIP_CHAIN) | SHARED_REFERENCE_TABLES) - tables
    assert not stale, f"inventory names tables that no longer exist: {sorted(stale)}"


@pytest.mark.asyncio
async def test_every_user_derived_table_has_rls_enabled_and_forced():
    async with unit_of_work(actor_type="system") as s:
        rows = await s.execute(text(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'ioe' AND c.relkind = 'r'"
        ))
        state = {r[0]: (r[1], r[2]) for r in rows}

    missing = [t for t in OWNERSHIP_CHAIN if state.get(t) != (True, True)]
    assert not missing, (
        "user-derived evidence must have ENABLE + FORCE ROW LEVEL SECURITY; "
        f"missing on: {sorted(missing)}"
    )


@pytest.mark.asyncio
async def test_every_user_derived_table_has_an_ownership_policy():
    async with unit_of_work(actor_type="system") as s:
        rows = await s.execute(text(
            "SELECT tablename, policyname, qual, with_check "
            "FROM pg_policies WHERE schemaname = 'ioe'"
        ))
        policies: dict[str, list[tuple]] = {}
        for table, name, qual, with_check in rows:
            policies.setdefault(table, []).append((name, qual, with_check))

    for table in OWNERSHIP_CHAIN:
        entries = policies.get(table)
        assert entries, f"ioe.{table} has no RLS policy"
        for name, qual, with_check in entries:
            # every policy must reduce to the tenant GUC, directly or by lookup
            assert "current_app_user" in (qual or ""), (
                f"{table}.{name} USING clause does not resolve to the app user"
            )
            assert "current_app_user" in (with_check or ""), (
                f"{table}.{name} WITH CHECK does not resolve to the app user"
            )


@pytest.mark.asyncio
async def test_ownership_traversal_columns_are_indexed():
    """Every RLS lookup must have an index to resolve through.

    RLS is the correctness boundary; these indexes are what keep it from also
    being a performance cliff on the child tables.
    """
    async with unit_of_work(actor_type="system") as s:
        for table, column in TRAVERSAL_COLUMN.items():
            leading = await s.scalar(text("""
                SELECT count(*) FROM pg_index x
                  JOIN pg_class t ON t.oid = x.indrelid
                  JOIN pg_namespace n ON n.oid = t.relnamespace AND n.nspname = 'ioe'
                 WHERE t.relname = :t
                   AND (SELECT a.attname FROM pg_attribute a
                         WHERE a.attrelid = t.oid AND a.attnum = x.indkey[0]) = :c
            """), {"t": table, "c": column})
            assert leading > 0, (
                f"ioe.{table}.{column} is an RLS traversal key with no index "
                "leading on it"
            )


@pytest.mark.asyncio
async def test_deny_by_default_when_app_user_id_is_unset():
    """The GUC resolves to NULL and every ownership predicate is then false.

    Evidence is generated first, so this can never pass merely because the
    tables happen to be empty.
    """
    from tests.integration.test_ioe_portfolio_persistence import (
        _publish_lever_rule,
        _suffix,
        _user_with_income,
    )

    uid, analysis_id = await _user_with_income()
    await _publish_lever_rule(
        f"RLSDENY_{_suffix()}", lever_code="INCREASE_RRSP_DEDUCTION", amount="6000",
    )
    await OptimizationOrchestrator(uid).generate(analysis_id)

    populated = (
        "optimization_run", "optimization_candidate", "strategy_portfolio",
        "portfolio_member", "portfolio_evaluation_step", "portfolio_exclusion",
        "score_component", "candidate_cost", "resource_ledger_entry",
    )
    async with unit_of_work(user_id=uid, actor_type="user") as s:
        for table in populated:
            count = await s.scalar(text(f"SELECT count(*) FROM ioe.{table}"))  # noqa: S608
            assert count > 0, f"ioe.{table} is empty; the deny check would be vacuous"

    async with unit_of_work(actor_type="system") as s:
        await s.execute(text("SELECT set_config('app.user_id', '', true)"))
        for table in sorted(OWNERSHIP_CHAIN):
            if table == "freshness_outbox":
                # global events (user_id IS NULL) are readable by design:
                # they say only "the engine version changed"
                continue
            count = await s.scalar(text(f"SELECT count(*) FROM ioe.{table}"))  # noqa: S608
            assert count == 0, (
                f"ioe.{table} returned {count} rows with no tenant context set"
            )


@pytest.mark.asyncio
async def test_a_second_user_sees_none_of_the_first_users_evidence():
    """The end-to-end statement of the same property, on real generated rows."""
    from tests.integration.test_ioe_portfolio_persistence import (
        _publish_lever_rule,
        _suffix,
        _user_with_income,
    )

    uid_a, analysis_a = await _user_with_income()
    uid_b, _ = await _user_with_income()
    await _publish_lever_rule(
        f"RLSINV_{_suffix()}", lever_code="INCREASE_RRSP_DEDUCTION", amount="6000",
    )
    outcome = await OptimizationOrchestrator(uid_a).generate(analysis_a)

    async with unit_of_work(user_id=uid_a, actor_type="user") as s:
        owned = await s.scalar(text("SELECT count(*) FROM ioe.strategy_portfolio"))
        steps = await s.scalar(text("SELECT count(*) FROM ioe.portfolio_evaluation_step"))
    assert owned > 0 and steps > 0, "fixture produced no evidence to hide"

    async with unit_of_work(user_id=uid_b, actor_type="user") as s:
        for table in sorted(OWNERSHIP_CHAIN):
            if table == "freshness_outbox":
                continue          # global rows are shared by design
            count = await s.scalar(text(f"SELECT count(*) FROM ioe.{table}"))  # noqa: S608
            assert count == 0, f"user B can read {count} rows of ioe.{table}"

        # a targeted read of a known id is refused exactly as a scan is
        found = await s.scalar(
            text("SELECT count(*) FROM ioe.strategy_portfolio WHERE run_id = :r"),
            {"r": outcome.run_id},
        )
        assert found == 0
