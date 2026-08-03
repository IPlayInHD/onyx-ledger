"""Re-evaluation of eligibility after earlier portfolio actions (§P4 item 4).

An earlier action can change the very facts a later candidate's eligibility was
determined from. Contributing to an RRSP lowers net income, and a rule gated on
net income may stop — or start — matching. Acting on eligibility decided against
the ORIGINAL facts would be acting on a determination that is no longer true.

The order of preference is fixed:

  1. RE-EVALUATE against the same pinned rule-version set. The condition trees
     are loaded ONCE, before assembly begins, from the pinned versions only.
     Evaluation during assembly is then pure and in-process, so no query can
     reach a rule published after the run was pinned — there is no code path
     from here back to the rules tables at all.
  2. If the candidate cannot be re-evaluated — no pinned tree was loaded for its
     rule version, or it carries no rule version — return INDETERMINATE. The
     caller excludes it as `requires_re_evaluation` rather than assuming either
     answer.

Facts are recomputed by the tax engine, exactly as in the initial evaluation, so
re-evaluation and first evaluation agree by construction rather than by
coincidence.
"""
from __future__ import annotations

import uuid
from collections.abc import Collection
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import RuleCondition, RuleConditionGroup
from app.services.ioe.domain.models import OptimizationCandidate
from app.services.tax_engine.core.condition_eval import eval_group
from app.services.tax_engine.core.engine import compute

ELIGIBILITY_RECHECK_VERSION = "1.0.0"


class RecheckResult(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    INDETERMINATE = "indeterminate"


class PinnedEligibilityRechecker:
    """Pure, in-process re-evaluation over PRE-LOADED pinned condition trees.

    Holds no session. It cannot query, which is precisely why a newly published
    rule cannot enter an in-flight run through this path.
    """

    def __init__(self, condition_trees: dict[str, dict | None], facts_fn):
        # Keyed by rule_version_id as text. A key present with value None means
        # "pinned, and unconditional" — that rule always matches.
        self._trees = condition_trees
        self._facts_fn = facts_fn

    @property
    def pinned_version_count(self) -> int:
        return len(self._trees)

    def check(self, candidate: OptimizationCandidate, inputs: dict) -> RecheckResult:
        version_id = candidate.rule_version_id
        if version_id is None or version_id not in self._trees:
            # Never guess: an unpinnable candidate is reported as unresolved.
            return RecheckResult.INDETERMINATE
        tree = self._trees[version_id]
        if tree is None:
            return RecheckResult.ELIGIBLE          # unconditional rule
        facts = self._facts_fn(inputs)
        return (
            RecheckResult.ELIGIBLE if eval_group(tree, facts)
            else RecheckResult.INELIGIBLE
        )


async def load_pinned_condition_trees(
    session: AsyncSession, pinned_rule_version_ids: Collection[uuid.UUID]
) -> dict[str, dict | None]:
    """Load condition trees for the pinned versions, and ONLY those.

    Mirrors `RulesEvaluatorService._load_condition_tree` in shape so the same
    `eval_group` yields the same answer; loading it here once keeps the
    per-step recheck out of the database entirely.
    """
    pinned = list(pinned_rule_version_ids)
    trees: dict[str, dict | None] = {str(v): None for v in pinned}
    if not pinned:
        return trees

    groups = list(await session.scalars(
        select(RuleConditionGroup).where(
            RuleConditionGroup.rule_version_id.in_(pinned)
        )
    ))
    if not groups:
        return trees

    conditions = list(await session.scalars(
        select(RuleCondition).where(
            RuleCondition.group_id.in_([g.id for g in groups])
        )
    ))
    by_group: dict[uuid.UUID, list] = {}
    for c in conditions:
        by_group.setdefault(c.group_id, []).append(c)

    nodes = {
        g.id: {
            "logical_op": g.logical_op, "conditions": [], "groups": [],
            "_parent": g.parent_group_id, "_version": str(g.rule_version_id),
        }
        for g in groups
    }
    for group_id, node in nodes.items():
        for c in by_group.get(group_id, ()):
            node["conditions"].append({
                "fact_key": c.fact_key, "operator": c.operator,
                "value_type": c.value_type, "value_number": c.value_number,
                "value_number_high": c.value_number_high,
                "value_text": c.value_text, "value_boolean": c.value_boolean,
                "value_set": None,
            })
    for node in list(nodes.values()):
        parent = node.pop("_parent")
        version = node.pop("_version")
        if parent is None:
            trees[version] = node
        else:
            nodes[parent]["groups"].append(node)
    return trees


def engine_facts_for(inputs: dict) -> dict:
    """Recompute the fact map from hypothetical inputs, via the engine only."""
    from app.services.ioe.portfolio.service import to_tax_input
    from app.services.tax_engine.service import TaxEngineService

    inp = to_tax_input(inputs)
    return TaxEngineService.facts_for(inp, compute(inp))


__all__ = [
    "ELIGIBILITY_RECHECK_VERSION",
    "PinnedEligibilityRechecker",
    "RecheckResult",
    "engine_facts_for",
    "load_pinned_condition_trees",
]
