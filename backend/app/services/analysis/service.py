"""Analysis orchestrator — the bridge where a user snapshot meets the law.

Freezes an immutable input snapshot, runs the pure engine, evaluates DB rules,
ranks opportunities, and persists analysis_run + line items + reconciliation
checks + recommendations (each citing its rule version).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AnalysisInputSnapshot,
    AnalysisLineItem,
    AnalysisRun,
    ReconciliationCheck,
    Recommendation,
)
from app.services.optimization.service import rank
from app.services.tax_engine.rules_service import RulesEvaluatorService
from app.services.tax_engine.service import ENGINE_VERSION, TaxEngineService


class AnalysisService:
    def __init__(self, session: AsyncSession):
        self.s = session
        self.engine = TaxEngineService(session)
        self.rules = RulesEvaluatorService(session)

    async def run(self, user_id: uuid.UUID, tax_year: int) -> AnalysisRun:
        inp = await self.engine.build_input(user_id, tax_year)
        result = self.engine.run(inp)
        facts = self.engine.facts(inp, result)

        run = AnalysisRun(
            user_id=user_id, tax_year=tax_year, province_code=inp.province,
            engine_version=ENGINE_VERSION, status="running",
            started_at=datetime.now(tz=timezone.utc),
        )
        self.s.add(run)
        await self.s.flush()

        snapshot = {k: str(v) for k, v in vars(inp).items()}
        self.s.add(AnalysisInputSnapshot(
            analysis_id=run.id, snapshot=snapshot,
            snapshot_hash=hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        ))

        for i, li in enumerate(result.line_items):
            self.s.add(AnalysisLineItem(
                analysis_id=run.id, kind=li["kind"], label=li["label"],
                amount=li["amount"], sort_order=i,
            ))

        # reconciliation tie-out (assurance layer)
        tie = abs(float(result.federal_tax + result.provincial_tax - result.income_tax)) < 0.05
        self.s.add(ReconciliationCheck(
            analysis_id=run.id, check_code="tie-out",
            status="pass" if tie else "flag", label="The return balances",
            detail="Federal + provincial tax reconcile to the total.",
        ))

        opportunities = await self.rules.evaluate(tax_year, facts)
        total_savings = Decimal(0)
        for opp, confidence, priority in rank(opportunities):
            self.s.add(Recommendation(
                analysis_id=run.id, user_id=user_id,
                tax_rule_version_id=opp.rule_version_id,
                opportunity_code=opp.opportunity_code, category=opp.category,
                title=opp.title, mechanism=opp.mechanism, where_text=opp.where_text,
                how_text=opp.how_text, why_text=opp.why_text,
                estimated_impact=opp.estimated_impact, confidence_score=confidence,
                priority=priority, citation=opp.citation, status="generated",
            ))
            if opp.estimated_impact:
                total_savings += opp.estimated_impact

        run.status = "completed"
        run.completed_at = datetime.now(tz=timezone.utc)
        run.total_income = result.total_income
        run.taxable_income = result.taxable_income
        run.estimated_tax = result.income_tax
        run.estimated_savings = total_savings
        run.marginal_rate = result.marginal_rate
        run.average_rate = result.average_rate
        run.confidence_score = 90 if tie else 50
        run.data_verified = True
        await self.s.flush()
        return run
