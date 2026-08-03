"""RuleSnapshotService — content-addressed pinning of the rules a run used.

A rule-version id is not its content: the artifacts that determine a computed
result (formula expressions, condition trees, outcomes, calc constants) hang off
shared tables, and the engine's reference dataset lives in code. A snapshot
captures the CONTENT hash of everything read, so drift is detectable rather than
silent (architecture Revision 2.1 §A).

The snapshot is not merely recorded — it CONSTRAINS evaluation. `version_ids()`
returns the exact immutable set handed to `RulesEvaluatorService`, so a rule
published after the snapshot was taken cannot enter an in-flight run.

Snapshots are content-addressed and shared: identical rule content across many
runs stores one row, so storage grows with distinct rule states rather than with
run count.
"""
from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.bulk import bulk_insert
from app.database.models import (
    CalcConstant,
    CalcFormula,
    CalcFormulaInput,
    RuleCondition,
    RuleConditionGroup,
    RuleOutcome,
    RuleSnapshot,
    RuleSnapshotArtifact,
    TaxRuleVersion,
)
from app.services.ioe.domain import canonical as c
from app.services.tax_engine.core import data as engine_data

# Artifacts whose content is materialized (not just hashed), so a drifted run
# remains explainable. Deliberately the small, high-risk set (decision D-10).
MATERIALIZED_KINDS = frozenset({
    "calc_formula", "calc_constant", "engine_reference_dataset",
})


@dataclass
class SnapshotArtifact:
    artifact_kind: str
    artifact_key: str
    content: dict
    artifact_id: uuid.UUID | None = None

    @property
    def content_hash(self) -> str:
        return c.canonical_hash(self.content)

    def as_hash_input(self) -> dict:
        return {
            "artifact_kind": self.artifact_kind,
            "artifact_key": self.artifact_key,
            "content_hash": self.content_hash,
        }


@dataclass
class PinnedSnapshot:
    snapshot_id: uuid.UUID
    snapshot_hash: str
    artifacts: list[SnapshotArtifact] = field(default_factory=list)

    def version_ids(self) -> list[uuid.UUID]:
        """The immutable rule-version set this snapshot pins."""
        return [
            a.artifact_id for a in self.artifacts
            if a.artifact_kind == "tax_rule_version" and a.artifact_id is not None
        ]


class RuleSnapshotService:
    def __init__(self, session: AsyncSession):
        self.s = session

    async def capture(self, tax_year: int) -> PinnedSnapshot:
        """Capture the published rule content for a year and persist it."""
        artifacts = await self._collect(tax_year)
        snapshot_hash = c.rule_snapshot_hash([a.as_hash_input() for a in artifacts])

        existing = await self.s.scalar(
            select(RuleSnapshot).where(RuleSnapshot.snapshot_hash == snapshot_hash)
        )
        if existing is not None:
            # Content-addressed: identical rule state reuses the stored snapshot.
            return PinnedSnapshot(existing.id, snapshot_hash, artifacts)

        # Content-addressed insert under a SAVEPOINT. Two concurrent runs over
        # identical rule content will both reach here; the unique index on
        # snapshot_hash arbitrates, and the loser adopts the winner's row rather
        # than failing. The savepoint keeps the outer transaction usable.
        try:
            async with self.s.begin_nested():
                snapshot = RuleSnapshot(
                    snapshot_hash=snapshot_hash, artifact_count=len(artifacts)
                )
                self.s.add(snapshot)
                await self.s.flush()
                await bulk_insert(self.s, RuleSnapshotArtifact, [
                    {
                        "snapshot_id": snapshot.id,
                        "artifact_kind": a.artifact_kind,
                        "artifact_id": a.artifact_id,
                        "artifact_key": a.artifact_key,
                        "content_hash": a.content_hash,
                        "content": (
                            a.content if a.artifact_kind in MATERIALIZED_KINDS else None
                        ),
                    }
                    for a in artifacts
                ])
                await self.s.flush()
                snapshot_id = snapshot.id
        except IntegrityError:
            adopted = await self.s.scalar(
                select(RuleSnapshot).where(RuleSnapshot.snapshot_hash == snapshot_hash)
            )
            if adopted is None:      # a different constraint failed — not ours
                raise
            return PinnedSnapshot(adopted.id, snapshot_hash, artifacts)

        return PinnedSnapshot(snapshot_id, snapshot_hash, artifacts)

    async def verify(self, snapshot_id: uuid.UUID, tax_year: int) -> tuple[str, list[dict]]:
        """Recompute current content and compare against a stored snapshot.

        Returns (replay_status, drifted_artifacts). Drift is reported, never
        silently reconciled.
        """
        stored = list(await self.s.scalars(
            select(RuleSnapshotArtifact).where(
                RuleSnapshotArtifact.snapshot_id == snapshot_id
            )
        ))
        stored_by_key = {(a.artifact_kind, a.artifact_key): a.content_hash for a in stored}
        current = {
            (a.artifact_kind, a.artifact_key): a.content_hash
            for a in await self._collect(tax_year)
        }

        drifted: list[dict] = []
        for key, pinned_hash in stored_by_key.items():
            current_hash = current.get(key)
            if current_hash is None:
                drifted.append({"artifact_kind": key[0], "artifact_key": key[1],
                                "pinned_hash": pinned_hash, "current_hash": None})
            elif current_hash != pinned_hash:
                drifted.append({"artifact_kind": key[0], "artifact_key": key[1],
                                "pinned_hash": pinned_hash, "current_hash": current_hash})

        if not drifted:
            return "verified", []
        if any(d["current_hash"] is None for d in drifted):
            return "unavailable", drifted
        return "drifted", drifted

    # ---- collection ---------------------------------------------------------
    async def _collect(self, tax_year: int) -> list[SnapshotArtifact]:
        artifacts: list[SnapshotArtifact] = [self._engine_reference_artifact()]

        versions = list(await self.s.scalars(
            select(TaxRuleVersion).where(
                TaxRuleVersion.tax_year == tax_year,
                TaxRuleVersion.status == "published",
            )
        ))
        version_ids = [v.id for v in versions]

        for v in versions:
            artifacts.append(SnapshotArtifact(
                artifact_kind="tax_rule_version", artifact_id=v.id,
                artifact_key=str(v.id),
                content={
                    "tax_rule_id": str(v.tax_rule_id),
                    "tax_year": v.tax_year,
                    "status": v.status,
                    "effective_date": v.effective_date,
                    "expiry_date": v.expiry_date,
                    "max_amount": c.money(v.max_amount),
                    "min_amount": c.money(v.min_amount),
                    "income_threshold_low": c.money(v.income_threshold_low),
                    "income_threshold_high": c.money(v.income_threshold_high),
                    "reduction_rate": c.rate(v.reduction_rate),
                    "formula_id": str(v.formula_id) if v.formula_id else None,
                    "eligibility_basis_codes": list(v.eligibility_basis_codes or ()),
                },
            ))

        if version_ids:
            artifacts.extend(await self._formula_artifacts(versions))
            artifacts.extend(await self._condition_artifacts(version_ids))
            artifacts.extend(await self._outcome_artifacts(version_ids))
        artifacts.extend(await self._constant_artifacts(tax_year))
        return artifacts

    def _engine_reference_artifact(self) -> SnapshotArtifact:
        """Hash the engine's IN-CODE reference dataset.

        Nothing else can detect that this changed: it is not in the database, so
        without this artifact a corrected bracket would alter replay silently.
        """
        federal = engine_data.FEDERAL_2025
        content = {
            "reference_data_version": engine_data.REFERENCE_DATA_VERSION,
            "federal": _describe(federal),
        }
        provinces = getattr(engine_data, "PROVINCES", None)
        if isinstance(provinces, dict):
            content["provinces"] = {
                code: _describe(provinces[code]) for code in sorted(provinces)
            }
        return SnapshotArtifact(
            artifact_kind="engine_reference_dataset",
            artifact_key=f"engine:{engine_data.REFERENCE_DATA_VERSION}",
            content=content,
        )

    async def _formula_artifacts(self, versions) -> list[SnapshotArtifact]:
        formula_ids = [v.formula_id for v in versions if v.formula_id]
        if not formula_ids:
            return []
        # One query for every formula's inputs, grouped in memory. Reading them
        # per formula made snapshot capture cost a round trip per rule version.
        inputs_by_formula: dict[uuid.UUID, list] = defaultdict(list)
        for i in await self.s.scalars(
            select(CalcFormulaInput).where(CalcFormulaInput.formula_id.in_(formula_ids))
        ):
            inputs_by_formula[i.formula_id].append(i)

        out: list[SnapshotArtifact] = []
        for f in await self.s.scalars(
            select(CalcFormula).where(CalcFormula.id.in_(formula_ids))
        ):
            inputs = inputs_by_formula.get(f.id, [])
            out.append(SnapshotArtifact(
                artifact_kind="calc_formula", artifact_id=f.id, artifact_key=f.code,
                content={
                    "code": f.code,
                    "expression": f.expression,
                    "expression_lang": f.expression_lang,
                    "inputs": c.ordered(
                        [{"param_name": i.param_name, "fact_key": i.fact_key,
                          "literal_value": c.quantity(i.literal_value)} for i in inputs],
                        key=lambda i: str(i["param_name"]),
                    ),
                },
            ))
        return out

    async def _condition_artifacts(self, version_ids) -> list[SnapshotArtifact]:
        groups = list(await self.s.scalars(
            select(RuleConditionGroup).where(
                RuleConditionGroup.rule_version_id.in_(version_ids)
            )
        ))
        # Same shape as the formula inputs: all leaves in one query, grouped in
        # memory, rather than a round trip per condition group.
        leaves_by_group: dict[uuid.UUID, list] = defaultdict(list)
        if groups:
            for x in await self.s.scalars(
                select(RuleCondition).where(
                    RuleCondition.group_id.in_([g.id for g in groups])
                )
            ):
                leaves_by_group[x.group_id].append(x)

        out: list[SnapshotArtifact] = []
        for g in groups:
            leaves = leaves_by_group.get(g.id, [])
            out.append(SnapshotArtifact(
                artifact_kind="rule_condition_group", artifact_id=g.id,
                artifact_key=str(g.id),
                content={
                    "rule_version_id": str(g.rule_version_id),
                    "logical_op": g.logical_op,
                    "parent_group_id": str(g.parent_group_id) if g.parent_group_id else None,
                    "conditions": c.ordered(
                        [{"fact_key": x.fact_key, "operator": x.operator,
                          "value_type": x.value_type,
                          "value_number": c.quantity(x.value_number),
                          "value_number_high": c.quantity(x.value_number_high),
                          "value_text": x.value_text,
                          "value_boolean": x.value_boolean,
                          "value_date": x.value_date} for x in leaves],
                        key=lambda x: (str(x["fact_key"]), str(x["operator"])),
                    ),
                },
            ))
        return out

    async def _outcome_artifacts(self, version_ids) -> list[SnapshotArtifact]:
        out: list[SnapshotArtifact] = []
        for o in await self.s.scalars(
            select(RuleOutcome).where(RuleOutcome.rule_version_id.in_(version_ids))
        ):
            out.append(SnapshotArtifact(
                artifact_kind="rule_outcome", artifact_id=o.id, artifact_key=str(o.id),
                content={
                    "rule_version_id": str(o.rule_version_id),
                    "outcome_type": o.outcome_type,
                    "impact_formula_id": str(o.impact_formula_id) if o.impact_formula_id else None,
                    "priority": o.priority,
                    "economic_effect_type": o.economic_effect_type,
                    "reversibility": o.reversibility,
                    "portfolio_lever_code": o.portfolio_lever_code,
                    "lever_parameters": o.lever_parameters,
                },
            ))
        return out

    async def _constant_artifacts(self, tax_year: int) -> list[SnapshotArtifact]:
        out: list[SnapshotArtifact] = []
        for k in await self.s.scalars(
            select(CalcConstant).where(CalcConstant.tax_year == tax_year)
        ):
            out.append(SnapshotArtifact(
                artifact_kind="calc_constant", artifact_id=k.id,
                artifact_key=f"{k.code}:{k.tax_year}",
                content={"code": k.code, "tax_year": k.tax_year,
                         "value": c.quantity(k.value), "unit": k.unit},
            ))
        return out


def _describe(dataset) -> dict:
    """Canonical description of an in-code dataclass dataset (public fields only)."""
    from dataclasses import fields, is_dataclass

    if not is_dataclass(dataset):
        return {"repr": repr(dataset)}
    out: dict = {}
    for f in sorted(fields(dataset), key=lambda x: x.name):
        value = getattr(dataset, f.name)
        out[f.name] = _describe_value(value)
    return out


def _describe_value(value):
    from dataclasses import is_dataclass
    from decimal import Decimal

    if isinstance(value, Decimal):
        return c.quantity(value)
    if is_dataclass(value):
        return _describe(value)
    if isinstance(value, (list, tuple)):
        return [_describe_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _describe_value(value[k]) for k in sorted(value, key=str)}
    return value
