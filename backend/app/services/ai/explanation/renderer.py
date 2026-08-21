"""The provider seam and the deterministic fallback renderer.

`ExplanationProvider` is the ONLY interface a model integration may implement:
it receives `ExplanationInputV1` and returns a candidate `ExplanationOutputV1`.
It cannot retrieve anything — no session, no repository, no RAG index reaches
it — so a provider that wants more tax authority than its input has nowhere to
get it.

`DeterministicExplanationRenderer` is the fallback AND the launch default: a
template renderer with ZERO model calls that quotes only supplied values,
codes and governed prose, and therefore passes the output validators by
construction (a property the service re-checks on every render rather than
trusting).

`get_explanation_provider()` returns None: no external model provider is
configured in this build. Wiring one is production plumbing — it slots in
behind this seam without touching the contract, the validators, or the
fallback.
"""
from __future__ import annotations

from typing import Protocol

from app.schemas.explanation import (
    AssumptionNoteV1,
    ExplanationInputV1,
    ExplanationOutputV1,
    NextStepV1,
    ValueRecordV1,
)
from app.schemas.ioe import AssumptionInput
from app.services.ai.explanation import values as vf
from app.services.ioe.domain.confidence import SUPPORT_SCORE_DISCLAIMER


class ExplanationProvider(Protocol):
    async def generate(self, request: ExplanationInputV1) -> ExplanationOutputV1:
        """Produce a candidate explanation from the input ALONE."""
        ...


def get_explanation_provider() -> ExplanationProvider | None:
    """The production provider. None: deliberately unconfigured in this build —
    the deterministic renderer serves every request until real provider
    plumbing (credentials, models, timeouts) is added behind this seam."""
    return None


_BASE_LIMITATION = (
    "Educational information only — this is not tax advice, not a filing, and "
    "nothing here is submitted to the CRA."
)

_INTEGRITY_SENTENCES = {
    "verified": (
        "Replay verification reproduced this sealed result from its own pinned "
        "inputs; that is a statement about reproducibility, not about "
        "correctness or CRA acceptance."
    ),
    "non_reproducible": (
        "Replay verification could not reproduce this sealed result from its "
        "pinned inputs — treat the figures with caution and consider re-running "
        "the analysis."
    ),
    "unavailable": (
        "Replay verification could not run because a pinned artifact is "
        "unavailable; nothing was compared, and this says nothing bad about "
        "the result itself."
    ),
    "legacy_unverifiable": (
        "This result predates the replay guarantee, so its historical "
        "calculation version is unavailable in the current runtime; re-run to "
        "get a verifiable current result."
    ),
}


class DeterministicExplanationRenderer:
    """Zero-model template rendering for every launch explanation type."""

    def render(self, inp: ExplanationInputV1) -> ExplanationOutputV1:
        builder = {
            "TAX_POSITION": self._tax_position,
            "OPPORTUNITY": self._opportunity,
            "PORTFOLIO": self._portfolio,
            "SCENARIO": self._scenario,
            "COMPARISON": self._comparison,
            "EVIDENCE_READINESS": self._evidence,
            "WHAT_CHANGED": self._what_changed,
        }[inp.explanation_type]
        return builder(inp)

    # ------------------------------------------------------------- shared --
    @staticmethod
    def _money(inp: ExplanationInputV1, key: str) -> str | None:
        record = inp.value_table.get(key)
        if record is None or record.kind not in ("money", "text_number"):
            return None
        try:
            return vf.money_display(f"{float(record.rendered):.2f}")
        except ValueError:
            return record.rendered

    @staticmethod
    def _record(inp: ExplanationInputV1, key: str) -> ValueRecordV1 | None:
        return inp.value_table.get(key)

    @staticmethod
    def _freshness_notice(inp: ExplanationInputV1) -> str | None:
        status = inp.freshness.freshness_status
        if status == "stale":
            reason = inp.freshness.stale_reason_code
            suffix = f" ({reason})" if reason else ""
            return (
                "This result may be out of date"
                f"{suffix}. Newer information could change it — re-run to get "
                "a statement about today."
            )
        if status == "superseded":
            return (
                "A newer scenario supersedes this result; what you are reading "
                "is the historical version."
            )
        return None

    def _limitations(self, inp: ExplanationInputV1, *, with_support: bool) -> str:
        parts = [_BASE_LIMITATION]
        if with_support:
            parts.append(SUPPORT_SCORE_DISCLAIMER)
        sentence = _INTEGRITY_SENTENCES.get(inp.integrity.integrity_state)
        if sentence:
            parts.append(sentence)
        return " ".join(parts)

    def _assumption_notes(
        self, assumptions: list[AssumptionInput]
    ) -> list[AssumptionNoteV1]:
        notes = []
        for assumption in assumptions:
            value = (vf.money_display(f"{assumption.value_number:.2f}")
                     if assumption.value_number is not None
                     else (assumption.value_text or str(assumption.value_boolean)))
            code = assumption.assumption_code
            if assumption.certainty == "statutory_known":
                note = (f"This calculation uses the published limit of {value} "
                        f"for {code}.")
            elif assumption.certainty == "user_asserted":
                note = (f"This scenario uses the {value} you stated for {code}.")
            elif assumption.certainty == "derived_from_data":
                note = (f"The value {value} for {code} was derived from your "
                        "recorded data.")
            else:  # platform_default
                note = (
                    f"This scenario assumes {value} of available {code}. That "
                    "is a platform assumption, not a recorded fact — check "
                    "your actual available amount before acting."
                )
            notes.append(AssumptionNoteV1(
                assumption_code=code,
                source=assumption.source,
                certainty=assumption.certainty,
                note=note,
            ))
        return notes

    # -------------------------------------------------------------- types --
    def _tax_position(self, inp: ExplanationInputV1) -> ExplanationOutputV1:
        section = inp.tax_position
        assert section is not None
        estimated = self._money(inp, "tax_position.estimated_tax")
        taxable = self._money(inp, "tax_position.taxable_income")
        if estimated and taxable:
            summary = (
                f"For {inp.tax_year}, your estimated tax is {estimated} on "
                f"taxable income of {taxable}."
            )
        else:
            summary = f"Your {inp.tax_year} tax analysis has been recorded."
        value_refs: list[str] = [
            k for k in ("tax_position.estimated_tax",
                        "tax_position.taxable_income", "tax_year")
            if k in inp.value_table]

        why_parts = []
        for idx, item in enumerate(section.line_items[:8]):
            amount = self._money(inp, f"tax_position.line_item.{idx}")
            if amount is not None:
                why_parts.append(f"{item.label}: {amount}")
                value_refs.append(f"tax_position.line_item.{idx}")
        marginal = self._record(inp, "tax_position.marginal_rate")
        rate_sentence = ""
        if marginal is not None:
            rate_sentence = (f" Your marginal rate is {marginal.rendered}.")
            value_refs.append("tax_position.marginal_rate")
        why = ("; ".join(why_parts) + "." if why_parts else None)
        if why and rate_sentence:
            why += rate_sentence

        failed = [c for c in section.reconciliation if c.status != "passed"]
        constraints = None
        if failed:
            constraints = (
                "Some tie-out checks did not pass: "
                + "; ".join(c.label for c in failed[:4]) + "."
            )
        return ExplanationOutputV1(
            explanation_type="TAX_POSITION",
            summary=summary,
            why_this_result=why,
            constraints_and_exclusions=constraints,
            freshness_notice=self._freshness_notice(inp),
            limitations=self._limitations(inp, with_support=False),
            value_refs_used=value_refs,
        )

    def _opportunity(self, inp: ExplanationInputV1) -> ExplanationOutputV1:
        section = inp.opportunity
        assert section is not None
        standing = section.standing
        code = standing.opportunity_code
        status_sentence = {
            "eligible": (
                f"The governed evaluation records you as eligible for {code}."),
            "conditionally_eligible": (
                f"The governed evaluation records you as conditionally "
                f"eligible for {code} — conditions apply and are listed on the "
                "opportunity."),
            "ineligible": (
                f"The governed evaluation records {code} as not available to "
                "you."),
        }.get(standing.eligibility_status,
              f"Eligibility for {code} could not be determined from the "
              "information available.")
        summary = status_sentence

        why = None
        citation_refs = [c.citation_id for c in inp.citations]
        if section.contract is not None:
            authored = (section.contract.authored_explanation
                        or section.contract.rule_description)
            if authored:
                why = authored
            if inp.citations:
                source_note = f" Source: {inp.citations[0].citation_text}."
                why = (why or "") + source_note

        effect = None
        value_refs = ["tax_year"] if "tax_year" in inp.value_table else []
        potential = self._money(inp, "opportunity.standalone_potential")
        if potential is not None:
            effect = (
                f"The governed optimizer sealed a standalone potential of "
                f"{potential} for this opportunity."
            )
            value_refs.append("opportunity.standalone_potential")

        steps: list[NextStepV1] = []
        if (section.contract is not None
                and standing.eligibility_status in
                ("eligible", "conditionally_eligible")):
            steps = [NextStepV1(action_ref=a.action_code, description=a.description)
                     for a in section.contract.actions[:5]]
        needed = [r.document_type_code for r in standing.evidence_requirements
                  if r.readiness != "ready"]

        constraints = None
        if standing.blocked_reason_code:
            constraints = (
                f"This opportunity is currently blocked: "
                f"{standing.blocked_reason_code}."
            )

        return ExplanationOutputV1(
            explanation_type="OPPORTUNITY",
            summary=summary,
            why_this_applies=why,
            estimated_effect_explanation=effect,
            what_you_can_do=steps,
            what_you_need=needed,
            constraints_and_exclusions=constraints,
            freshness_notice=self._freshness_notice(inp),
            limitations=self._limitations(inp, with_support=True),
            citation_refs=citation_refs,
            value_refs_used=value_refs,
        )

    def _portfolio(self, inp: ExplanationInputV1) -> ExplanationOutputV1:
        portfolio = inp.portfolio
        assert portfolio is not None
        total = self._money(inp, "portfolio.total_benefit")
        member_count = len(portfolio.members)
        summary = (
            f"Your {inp.tax_year} strategy set contains {member_count} "
            f"strategies"
            + (f" with a combined benefit of {total}." if total else ".")
        )
        value_refs: list[str] = [
            k for k in ("portfolio.total_benefit", "tax_year")
            if k in inp.value_table]

        effect_parts = []
        for key, label in (
            ("portfolio.total_tax_reduction", "current-year tax reduction"),
            ("portfolio.total_refundable_benefit", "refundable benefit"),
            ("portfolio.total_deferral_amount",
             "tax deferral (a timing benefit, not a permanent saving)"),
        ):
            amount = self._money(inp, key)
            if amount is not None:
                effect_parts.append(f"{amount} of {label}")
                value_refs.append(key)
        effect = (
            "Kept separate because they are different kinds of money: "
            + "; ".join(effect_parts) + "."
        ) if effect_parts else None

        cash = self._money(inp, "portfolio.total_liquidity_commitment")
        required = None
        if cash is not None:
            required = (
                f"Acting on the full set requires {cash} of cash to be "
                "available — committed, not lost."
            )
            value_refs.append("portfolio.total_liquidity_commitment")

        constraints = None
        if portfolio.exclusions:
            reasons = ", ".join(sorted({
                e.reason_code for e in portfolio.exclusions})[:4])
            constraints = (
                f"{len(portfolio.exclusions)} candidate strategies were "
                f"excluded ({reasons}). An exclusion is itself a result — it "
                "is shown, not hidden."
            )
        steps = []
        seen_options: set[str] = set()
        for exclusion in portfolio.exclusions:
            for option in exclusion.resolution_options:
                if option not in seen_options:
                    seen_options.add(option)
                    steps.append(NextStepV1(
                        action_ref=f"resolution:{option}",
                        description=f"Resolution option: {option}",
                    ))

        limitations = self._limitations(inp, with_support=False) + (
            " This is a feasible, deterministic, engine-evaluated strategy "
            "set — not a globally optimal portfolio."
        )
        return ExplanationOutputV1(
            explanation_type="PORTFOLIO",
            summary=summary,
            estimated_effect_explanation=effect,
            required_cash_or_resource=required,
            constraints_and_exclusions=constraints,
            what_you_can_do=steps[:5],
            freshness_notice=self._freshness_notice(inp),
            limitations=limitations,
            value_refs_used=value_refs,
        )

    def _scenario(self, inp: ExplanationInputV1) -> ExplanationOutputV1:
        scenario = inp.scenario
        assert scenario is not None
        baseline = self._money(inp, "scenario.baseline_tax")
        after = self._money(inp, "scenario.scenario_tax")
        delta = self._money(inp, "scenario.tax_delta")
        value_refs = [k for k in ("scenario.baseline_tax", "scenario.scenario_tax",
                                  "scenario.tax_delta", "tax_year")
                      if k in inp.value_table]
        if baseline and after and delta:
            summary = (
                f"This what-if changes your estimated tax from {baseline} to "
                f"{after}, a difference of {delta}. It is a simulation — "
                "nothing has been filed or recorded as fact."
            )
        else:
            summary = (
                "This what-if scenario has been simulated; its sealed figures "
                "are shown on the scenario itself."
            )
        lever_codes = ", ".join(lever.lever_code for lever in scenario.levers)
        why = (
            f"The scenario applies your selected lever(s): {lever_codes}. The "
            "engine recomputed the full position with only those changes "
            "applied."
        )
        return ExplanationOutputV1(
            explanation_type="SCENARIO",
            summary=summary,
            why_this_result=why,
            important_assumptions=self._assumption_notes(inp.assumptions),
            freshness_notice=self._freshness_notice(inp),
            limitations=self._limitations(inp, with_support=True),
            value_refs_used=value_refs,
        )

    def _comparison(self, inp: ExplanationInputV1) -> ExplanationOutputV1:
        comparison = inp.comparison
        assert comparison is not None
        changed = comparison.summary.changed_families
        total_nodes = sum(comparison.summary.node_counts_by_change.values())
        summary = (
            "Compared with your sealed baseline, this scenario changes "
            f"{len(changed)} area(s) of your position"
            + (f": {', '.join(changed)}." if changed else ".")
        )
        what_changed_parts = []
        for family_label, records in (
            ("tax state", comparison.tax_state_changes),
            ("opportunities", comparison.opportunity_changes),
            ("evidence", comparison.evidence_changes),
            ("assumptions", comparison.assumption_changes),
        ):
            if records:
                what_changed_parts.append(f"{len(records)} {family_label} change(s)")
        what_changed = (
            "; ".join(what_changed_parts) + "." if what_changed_parts else None
        )
        _ = total_nodes  # counts stay in the structured summary, not in prose
        return ExplanationOutputV1(
            explanation_type="COMPARISON",
            summary=summary,
            what_changed=what_changed,
            important_assumptions=self._assumption_notes(inp.assumptions),
            freshness_notice=self._freshness_notice(inp),
            limitations=self._limitations(inp, with_support=False),
            value_refs_used=["tax_year"] if "tax_year" in inp.value_table else [],
        )

    def _evidence(self, inp: ExplanationInputV1) -> ExplanationOutputV1:
        evidence = inp.evidence
        assert evidence is not None
        missing_now = [r.document_type_code
                       for r in evidence.current_observed_readiness
                       if r.readiness != "ready"]
        summary = (
            "Your evidence standing for this decision: "
            f"{evidence.status}."
            + (" Some governed requirements are not ready yet."
               if missing_now else
               " Every governed requirement currently reads ready.")
        )
        what_changed = (
            "Sealed readiness is what the scenario recorded and never moves; "
            "current readiness is the same governed check over the documents "
            "held now. Neither verifies that any reported action occurred."
        )
        return ExplanationOutputV1(
            explanation_type="EVIDENCE_READINESS",
            summary=summary,
            what_you_need=missing_now,
            what_changed=what_changed,
            freshness_notice=self._freshness_notice(inp),
            limitations=self._limitations(inp, with_support=False),
            value_refs_used=["tax_year"] if "tax_year" in inp.value_table else [],
        )

    def _what_changed(self, inp: ExplanationInputV1) -> ExplanationOutputV1:
        changes = inp.changes
        assert changes is not None
        total = changes.summary.total
        if changes.baseline_status == "NO_BASELINE":
            summary = (
                "No acknowledged baseline exists yet, so there is no prior "
                "state to compare against. Review your position and "
                "acknowledge it to start tracking changes."
            )
        elif total == 0:
            summary = (
                f"Nothing material has changed in your {inp.tax_year} tax "
                "state since the baseline you acknowledged."
            )
        else:
            summary = (
                f"Since the state you last acknowledged, {total} material "
                f"change(s) occurred in your {inp.tax_year} tax position."
            )
        details = []
        for change in changes.changes[:6]:
            details.append(f"{change.kind} — {change.subject} ({change.severity})")
        what_changed = "; ".join(details) + "." if details else None
        return ExplanationOutputV1(
            explanation_type="WHAT_CHANGED",
            summary=summary,
            what_changed=what_changed,
            freshness_notice=self._freshness_notice(inp),
            limitations=self._limitations(inp, with_support=False),
            value_refs_used=[k for k in ("changes.total", "tax_year")
                             if k in inp.value_table],
        )


__all__ = [
    "DeterministicExplanationRenderer",
    "ExplanationProvider",
    "get_explanation_provider",
]
