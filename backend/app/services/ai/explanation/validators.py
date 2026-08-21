"""Deterministic validators for `ExplanationOutputV1`.

Every check here compares generated text against the STRUCTURED input and
rejects on material disagreement. Nothing is repaired: a candidate that fails
any check is discarded whole and the deterministic renderer takes over. The
reason codes are a closed vocabulary so observability can count failures
without reading prose.

Numeric checking is context-aware rather than digit-phobic: date-shaped tokens
are validated against supplied date records first and removed; remaining
numeric tokens must match a supplied value's accepted forms when they are
money-shaped (currency sign, decimals, or magnitude ≥ 100), percent-shaped, or
day counts. Small bare integers — list positions, "two of three" style counts —
are ordinary prose and pass. There is NO tolerance for tax amounts: matching is
exact string equality over deterministic formatting variants of the same
value, never numeric closeness.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.explanation import ExplanationInputV1, ExplanationOutputV1
from app.services.ai.explanation import values as vf

# ----------------------------------------------------------- reason codes --
SCHEMA_INVALID = "SCHEMA_INVALID"
EXPLANATION_TYPE_MISMATCH = "EXPLANATION_TYPE_MISMATCH"
CITATION_NOT_SUPPLIED = "CITATION_NOT_SUPPLIED"
VALUE_REF_UNKNOWN = "VALUE_REF_UNKNOWN"
NUMERIC_VALUE_UNSUPPORTED = "NUMERIC_VALUE_UNSUPPORTED"
NUMERIC_ROLE_MISMATCH = "NUMERIC_ROLE_MISMATCH"
ACTION_NOT_SUPPLIED = "ACTION_NOT_SUPPLIED"
EVIDENCE_INVENTED = "EVIDENCE_INVENTED"
MATERIAL_ASSUMPTION_OMITTED = "MATERIAL_ASSUMPTION_OMITTED"
ASSUMPTION_ORIGIN_MISSTATED = "ASSUMPTION_ORIGIN_MISSTATED"
DEADLINE_INVENTED = "DEADLINE_INVENTED"
ELIGIBILITY_OVERSTATED = "ELIGIBILITY_OVERSTATED"
CONSTRAINT_OMITTED = "CONSTRAINT_OMITTED"
FRESHNESS_MISSTATED = "FRESHNESS_MISSTATED"
INTEGRITY_MISSTATED = "INTEGRITY_MISSTATED"
SUPPORT_SCORE_PROBABILITY_CLAIM = "SUPPORT_SCORE_PROBABILITY_CLAIM"
FORBIDDEN_CLAIM = "FORBIDDEN_CLAIM"
INTERNAL_IDENTITY_LEAKED = "INTERNAL_IDENTITY_LEAKED"
UNTRUSTED_DISPLAY_ECHOED = "UNTRUSTED_DISPLAY_ECHOED"


@dataclass(frozen=True)
class ExplanationViolation:
    code: str
    detail: str


#: Output fields whose free text is scanned. `what_you_need` is deliberately
#: absent — it is a code list checked as a set, and document codes may contain
#: digits that are not numbers.
_PROSE_FIELDS = (
    "summary",
    "why_this_result",
    "why_this_applies",
    "estimated_effect_explanation",
    "required_cash_or_resource",
    "constraints_and_exclusions",
    "what_changed",
    "freshness_notice",
    "limitations",
)

_ISO_DATE = vf.ISO_DATE_RE
_SPELLED_DATE = vf.SPELLED_DATE_RE
_NUMBER = vf.NUMBER_TOKEN_RE
_DAYS = vf.DAYS_RE
_HEX_IDENTITY = re.compile(r"\b[0-9a-f]{40,}\b", re.IGNORECASE)

_CURRENT_CLAIMS = ("is current", "up to date", "up-to-date", "still current")
_VERIFIED_CLAIMS = (
    "is verified", "was verified", "has been verified", "have been verified",
    "successfully reproduced", "reproduced successfully",
)
_CORRUPTION_CLAIMS = ("corrupt", "tampered", "damaged")
_SUPPORT_CLAIMS = (
    "cra will accept", "cra will approve", "probability", "likelihood",
    "% chance", "chance the cra", "chance that the cra", "acceptance rate",
)
_FORBIDDEN_PHRASES = (
    # "guaranteed"/"we guarantee" are claims; the bare noun is not — an honest
    # sentence like "predates the replay guarantee" must stay expressible.
    "guaranteed", "we guarantee", "definitely qualify", "certainly qualify",
    "risk-free", "cra has approved", "action has been verified",
    "we verified your",
)
_AFFIRMATIVE_ELIGIBILITY = (
    "you qualify", "you are eligible", "you're eligible", "you will receive",
)
_PLATFORM_DEFAULT_FORBIDDEN = (
    "you entered", "you declared", "your declared", "you provided", "on file",
    "known room", "confirmed", "account-confirmed", "cra confirmed",
)
_USER_ASSERTED_FORBIDDEN = ("published limit", "statutory")
_NEGATORS = ("not", "no", "never", "isn't", "is not", "aren't", "cannot")


def _fields(output: ExplanationOutputV1) -> list[tuple[str, str]]:
    named = [(f, getattr(output, f)) for f in _PROSE_FIELDS]
    named += [("what_you_can_do", step.description) for step in output.what_you_can_do]
    named += [("important_assumptions", n.note) for n in output.important_assumptions]
    return [(name, text) for name, text in named if text]


def _has_unnegated(text: str, phrase: str, window: int = 40) -> bool:
    """True when `phrase` occurs WITHOUT a negator in the preceding window.

    "not a globally optimal portfolio" and "not a probability that the CRA
    will accept" are honest sentences; the same words without the negation are
    forbidden claims. The window is character-based and deterministic.
    """
    lower = text.lower()
    start = 0
    while True:
        idx = lower.find(phrase, start)
        if idx == -1:
            return False
        preceding = lower[max(0, idx - window):idx]
        if not any(neg in preceding for neg in _NEGATORS):
            return True
        start = idx + len(phrase)


def validate_explanation(
    inp: ExplanationInputV1, output: ExplanationOutputV1
) -> list[ExplanationViolation]:
    v: list[ExplanationViolation] = []
    fields = _fields(output)
    all_text = " ".join(text for _, text in fields)
    lower_all = all_text.lower()

    # 2. type echo -----------------------------------------------------------
    if output.explanation_type != inp.explanation_type:
        v.append(ExplanationViolation(
            EXPLANATION_TYPE_MISMATCH,
            f"output says {output.explanation_type}, input is {inp.explanation_type}",
        ))

    # 3. citations -----------------------------------------------------------
    supplied_citations = {c.citation_id for c in inp.citations}
    for ref in output.citation_refs:
        if ref not in supplied_citations:
            v.append(ExplanationViolation(
                CITATION_NOT_SUPPLIED, f"citation_ref {ref!r} was not supplied"))

    # 4./5. numbers and dates ------------------------------------------------
    date_forms: set[str] = set()
    number_forms: set[str] = set()
    scope_by_form: dict[str, set[str] | None] = {}
    for record in inp.value_table.values():
        forms = {vf.normalize_token(f) for f in record.accepted_forms}
        if record.kind == "date":
            date_forms |= forms
        else:
            number_forms |= forms
            scope = set(record.field_scope) if record.field_scope else None
            for form in forms:
                if form in scope_by_form:
                    existing = scope_by_form[form]
                    # An unscoped record anywhere makes the form usable anywhere.
                    scope_by_form[form] = (
                        None if existing is None or scope is None
                        else existing | scope
                    )
                else:
                    scope_by_form[form] = scope

    for field_name, text in fields:
        remaining = text
        for pattern in (_ISO_DATE, _SPELLED_DATE):
            for match in pattern.findall(remaining):
                if vf.normalize_token(match) not in date_forms:
                    v.append(ExplanationViolation(
                        DEADLINE_INVENTED,
                        f"{field_name}: date {match!r} was not supplied"))
            remaining = pattern.sub(" ", remaining)

        for days_match in _DAYS.finditer(remaining):
            token = vf.normalize_token(days_match.group(1))
            if token not in number_forms:
                v.append(ExplanationViolation(
                    DEADLINE_INVENTED,
                    f"{field_name}: day count {days_match.group(0)!r} not supplied"))
        remaining_for_numbers = _DAYS.sub(" ", remaining)

        for raw in _NUMBER.findall(remaining_for_numbers):
            token = vf.normalize_token(raw)
            if not vf.is_material_token(raw):
                continue
            if token not in number_forms:
                v.append(ExplanationViolation(
                    NUMERIC_VALUE_UNSUPPORTED,
                    f"{field_name}: number {raw.strip()!r} does not match any "
                    "supplied value"))
                continue
            scope = scope_by_form.get(token)
            if scope is not None and field_name not in scope:
                v.append(ExplanationViolation(
                    NUMERIC_ROLE_MISMATCH,
                    f"{field_name}: value {raw.strip()!r} is confined to "
                    f"{sorted(scope)}"))

    for ref in output.value_refs_used:
        if ref not in inp.value_table:
            v.append(ExplanationViolation(
                VALUE_REF_UNKNOWN, f"value_ref {ref!r} was not supplied"))

    # 6. eligibility ---------------------------------------------------------
    standing = inp.opportunity.standing if inp.opportunity else None
    if standing is not None:
        status = standing.eligibility_status
        if status in ("ineligible", "indeterminate"):
            for phrase in _AFFIRMATIVE_ELIGIBILITY:
                if _has_unnegated(all_text, phrase):
                    v.append(ExplanationViolation(
                        ELIGIBILITY_OVERSTATED,
                        f"{phrase!r} claimed while status is {status}"))
                    break
        if status == "conditionally_eligible":
            claims = any(_has_unnegated(all_text, p)
                         for p in _AFFIRMATIVE_ELIGIBILITY)
            if claims and "condition" not in lower_all:
                v.append(ExplanationViolation(
                    ELIGIBILITY_OVERSTATED,
                    "conditional eligibility rendered without its conditions"))

    # 7. exclusions / constraints -------------------------------------------
    if inp.portfolio is not None and inp.portfolio.exclusions:
        if not (output.constraints_and_exclusions or "").strip():
            v.append(ExplanationViolation(
                CONSTRAINT_OMITTED,
                "portfolio has exclusions but constraints_and_exclusions is empty"))
    if standing is not None and standing.blocked_reason_code:
        if not (output.constraints_and_exclusions or "").strip():
            v.append(ExplanationViolation(
                CONSTRAINT_OMITTED,
                "opportunity is blocked but constraints_and_exclusions is empty"))

    # 8./9. assumptions ------------------------------------------------------
    noted = {n.assumption_code: n for n in output.important_assumptions}
    by_code = {a.assumption_code: a for a in inp.assumptions}
    for assumption in inp.assumptions:
        material = assumption.materiality == "high" or assumption.affects_eligibility
        if material and assumption.assumption_code not in noted:
            v.append(ExplanationViolation(
                MATERIAL_ASSUMPTION_OMITTED,
                f"material assumption {assumption.assumption_code} not rendered"))
    for code, note in noted.items():
        supplied = by_code.get(code)
        if supplied is None:
            v.append(ExplanationViolation(
                ASSUMPTION_ORIGIN_MISSTATED,
                f"assumption {code} was not supplied"))
            continue
        if note.source != supplied.source or note.certainty != supplied.certainty:
            v.append(ExplanationViolation(
                ASSUMPTION_ORIGIN_MISSTATED,
                f"assumption {code}: origin rendered as "
                f"{note.source}/{note.certainty}, supplied "
                f"{supplied.source}/{supplied.certainty}"))
            continue
        lower_note = note.note.lower()
        if supplied.certainty == "platform_default":
            if not ("assum" in lower_note or "estimate" in lower_note):
                v.append(ExplanationViolation(
                    ASSUMPTION_ORIGIN_MISSTATED,
                    f"assumption {code}: a platform default must be described "
                    "as an assumption"))
            for phrase in _PLATFORM_DEFAULT_FORBIDDEN:
                if phrase in lower_note:
                    v.append(ExplanationViolation(
                        ASSUMPTION_ORIGIN_MISSTATED,
                        f"assumption {code}: platform default described as "
                        f"{phrase!r}"))
                    break
        elif supplied.certainty == "user_asserted":
            for phrase in _USER_ASSERTED_FORBIDDEN:
                if phrase in lower_note:
                    v.append(ExplanationViolation(
                        ASSUMPTION_ORIGIN_MISSTATED,
                        f"assumption {code}: user declaration described as "
                        f"{phrase!r}"))
                    break

    # 10. actions ------------------------------------------------------------
    allowed_actions: set[str] = set()
    if inp.opportunity and inp.opportunity.contract:
        allowed_actions |= {a.action_code for a in inp.opportunity.contract.actions}
    if inp.portfolio is not None:
        for exclusion in inp.portfolio.exclusions:
            allowed_actions |= {
                f"resolution:{opt}" for opt in exclusion.resolution_options}
    for step in output.what_you_can_do:
        if step.action_ref not in allowed_actions:
            v.append(ExplanationViolation(
                ACTION_NOT_SUPPLIED,
                f"next step {step.action_ref!r} references no supplied action"))

    # 11. evidence -----------------------------------------------------------
    allowed_documents: set[str] = set()
    if inp.opportunity is not None:
        allowed_documents |= {
            r.document_type_code
            for r in inp.opportunity.standing.evidence_requirements}
        if inp.opportunity.contract:
            allowed_documents |= {
                d.document_type_code for d in inp.opportunity.contract.documents}
    if inp.evidence is not None:
        allowed_documents |= {
            r.document_type_code
            for r in (*inp.evidence.sealed_readiness,
                      *inp.evidence.current_observed_readiness)}
    for code in output.what_you_need:
        if code not in allowed_documents:
            v.append(ExplanationViolation(
                EVIDENCE_INVENTED, f"document {code!r} was not supplied"))

    # 12. freshness ----------------------------------------------------------
    status = inp.freshness.freshness_status
    if status in ("stale", "superseded") and not (output.freshness_notice or "").strip():
        v.append(ExplanationViolation(
            FRESHNESS_MISSTATED, f"{status} result rendered without a notice"))
    if status != "current":
        for phrase in _CURRENT_CLAIMS:
            if _has_unnegated(all_text, phrase):
                v.append(ExplanationViolation(
                    FRESHNESS_MISSTATED,
                    f"{phrase!r} claimed while freshness is {status}"))
                break

    # 13. integrity ----------------------------------------------------------
    state = inp.integrity.integrity_state
    if state != "verified":
        for phrase in _VERIFIED_CLAIMS:
            if _has_unnegated(all_text, phrase):
                v.append(ExplanationViolation(
                    INTEGRITY_MISSTATED,
                    f"{phrase!r} claimed while integrity is {state}"))
                break
    if state in ("unavailable", "legacy_unverifiable"):
        for phrase in _CORRUPTION_CLAIMS:
            if phrase in lower_all:
                v.append(ExplanationViolation(
                    INTEGRITY_MISSTATED,
                    f"{phrase!r} implied for a result that was never compared"))
                break

    # 14. support ------------------------------------------------------------
    for phrase in _SUPPORT_CLAIMS:
        if _has_unnegated(all_text, phrase):
            v.append(ExplanationViolation(
                SUPPORT_SCORE_PROBABILITY_CLAIM,
                f"{phrase!r} presents support as acceptance probability"))
            break

    # forbidden claims -------------------------------------------------------
    for phrase in _FORBIDDEN_PHRASES:
        if _has_unnegated(all_text, phrase):
            v.append(ExplanationViolation(
                FORBIDDEN_CLAIM, f"{phrase!r} is a forbidden claim"))
            break
    if inp.portfolio is not None and inp.portfolio.optimality_claim == "none":
        if _has_unnegated(all_text, "optimal"):
            v.append(ExplanationViolation(
                FORBIDDEN_CLAIM,
                "portfolio described as optimal while optimality_claim is none"))

    # 15. leakage ------------------------------------------------------------
    for identity in inp.subject_binding.identity_values():
        if identity and identity in all_text:
            v.append(ExplanationViolation(
                INTERNAL_IDENTITY_LEAKED, "subject-binding identity in output"))
            break
    else:
        if _HEX_IDENTITY.search(all_text):
            v.append(ExplanationViolation(
                INTERNAL_IDENTITY_LEAKED, "hash-shaped identity in output"))

    # 16. untrusted echo -----------------------------------------------------
    for key, value in inp.display_context.untrusted.items():
        if len(value) >= 12 and value in all_text:
            v.append(ExplanationViolation(
                UNTRUSTED_DISPLAY_ECHOED,
                f"user-authored display text {key} echoed verbatim"))
            break

    return v


__all__ = [
    "ACTION_NOT_SUPPLIED",
    "ASSUMPTION_ORIGIN_MISSTATED",
    "CITATION_NOT_SUPPLIED",
    "CONSTRAINT_OMITTED",
    "DEADLINE_INVENTED",
    "ELIGIBILITY_OVERSTATED",
    "EVIDENCE_INVENTED",
    "EXPLANATION_TYPE_MISMATCH",
    "FORBIDDEN_CLAIM",
    "FRESHNESS_MISSTATED",
    "INTEGRITY_MISSTATED",
    "INTERNAL_IDENTITY_LEAKED",
    "MATERIAL_ASSUMPTION_OMITTED",
    "NUMERIC_ROLE_MISMATCH",
    "NUMERIC_VALUE_UNSUPPORTED",
    "SCHEMA_INVALID",
    "SUPPORT_SCORE_PROBABILITY_CLAIM",
    "UNTRUSTED_DISPLAY_ECHOED",
    "VALUE_REF_UNKNOWN",
    "ExplanationViolation",
    "validate_explanation",
]
