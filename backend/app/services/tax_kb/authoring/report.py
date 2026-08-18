"""The publishability report — the deterministic answer to "may this publish?".

Deterministic in three senses that matter:

1. **Ordering.** Findings sort on their own content, so the same draft produces
   the same report bytes whatever order the checks happened to run in.
2. **Verdict.** `publishable` is a function of ERROR findings alone. A warning
   never blocks and never unblocks, so nothing here can quietly turn a hard
   requirement into advice.
3. **Coverage.** Every family reports a `Readiness`, including the ones that do
   not apply. A family the pipeline could not assess is never reported as an
   empty success.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.services.tax_kb.authoring.codes import (
    PUBLISHABILITY_REPORT_SCHEMA_VERSION,
    Family,
    Readiness,
    Severity,
    ValidationCode,
)


@dataclass(frozen=True, order=True)
class Finding:
    """One validation result, sortable by its own content.

    `object_ref` is the authored code (a rule code, a formula code, a
    reference-data key) rather than a row id: a draft has no row id yet, and an
    operator correcting a manifest needs to know which entry to open.
    """

    object_ref: str
    family: Family
    code: ValidationCode
    severity: Severity
    #: Free text for a person. Never parsed, never compared, never a gate.
    detail: str = ""

    @property
    def blocks(self) -> bool:
        return self.severity is Severity.ERROR

    def as_payload(self) -> dict:
        return {
            "object_ref": self.object_ref,
            "family": str(self.family),
            "code": str(self.code),
            "severity": str(self.severity),
            "detail": self.detail,
        }


def error(object_ref: str, family: Family, code: ValidationCode,
          detail: str = "") -> Finding:
    return Finding(object_ref, family, code, Severity.ERROR, detail)


def warning(object_ref: str, family: Family, code: ValidationCode,
            detail: str = "") -> Finding:
    return Finding(object_ref, family, code, Severity.WARNING, detail)


@dataclass(frozen=True)
class PublishabilityReport:
    """What validation concluded about one draft, or one pack of them."""

    #: What the report was computed over. `None` for a pack-level report.
    spec_hash: str | None
    spec_schema_version: str | None
    publication_policy_version: str
    findings: tuple[Finding, ...] = ()
    readiness: tuple[tuple[Family, Readiness], ...] = ()
    #: For a pack: one entry per member, in the pack's declared order.
    members: tuple[PublishabilityReport, ...] = ()
    report_schema_version: str = PUBLISHABILITY_REPORT_SCHEMA_VERSION

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.WARNING)

    @property
    def publishable(self) -> bool:
        """Hard errors alone decide, here and in every member.

        A pack whose own checks passed while a member failed is not publishable:
        §75 forbids publishing the valid nine of ten.
        """
        return not self.errors and all(m.publishable for m in self.members)

    def readiness_of(self, family: Family) -> Readiness:
        for name, state in self.readiness:
            if name is family:
                return state
        raise KeyError(
            f"family {family!r} was never assessed; a family with no readiness "
            "is an unanswered question, not a pass")

    def as_payload(self) -> dict:
        return {
            "report_schema_version": self.report_schema_version,
            "spec_hash": self.spec_hash,
            "spec_schema_version": self.spec_schema_version,
            "publication_policy_version": self.publication_policy_version,
            "publishable": self.publishable,
            "findings": [f.as_payload() for f in self.findings],
            "readiness": {str(name): str(state) for name, state in self.readiness},
            "members": [m.as_payload() for m in self.members],
        }

    def error_codes(self) -> tuple[str, ...]:
        """Every blocking code, this report's and its members', deduplicated."""
        codes = {str(f.code) for f in self.errors}
        for member in self.members:
            codes.update(member.error_codes())
        return tuple(sorted(codes))


@dataclass
class ReportBuilder:
    """Accumulates findings and readiness, then freezes them into a report.

    Sorting happens once, at `build()`. Checks may run in any order, add
    findings in any order, and assess families in any order; what comes out is
    ordered by content.
    """

    findings: list[Finding] = field(default_factory=list)
    _readiness: dict[Family, Readiness] = field(default_factory=dict)

    def add(self, *findings: Finding) -> None:
        self.findings.extend(findings)

    def extend(self, findings: list[Finding]) -> None:
        self.findings.extend(findings)

    def assess(self, family: Family, state: Readiness) -> None:
        self._readiness[family] = state

    def resolve(self, family: Family, object_ref: str, *,
                declared: bool, applicable: bool = True) -> None:
        """Derive a family's readiness from the findings already recorded.

        A family with a blocking finding is INVALID when it was declared and
        MISSING when whatever it named could not be resolved. Both are distinct
        from the NOT_APPLICABLE of a family the draft never claimed to have.
        """
        if not applicable:
            self._readiness[family] = Readiness.NOT_APPLICABLE
            return
        blocking = [
            f for f in self.findings
            if f.family is family and f.severity is Severity.ERROR
            and (f.object_ref == object_ref or not object_ref)
        ]
        if not blocking:
            self._readiness[family] = Readiness.READY
        elif declared:
            self._readiness[family] = Readiness.INVALID
        else:
            self._readiness[family] = Readiness.MISSING

    def build(self, *, spec_hash: str | None, spec_schema_version: str | None,
              policy_version: str,
              members: tuple[PublishabilityReport, ...] = ()) -> PublishabilityReport:
        for family in Family:
            self._readiness.setdefault(family, Readiness.NOT_APPLICABLE)
        return PublishabilityReport(
            spec_hash=spec_hash,
            spec_schema_version=spec_schema_version,
            publication_policy_version=policy_version,
            findings=tuple(sorted(set(self.findings))),
            readiness=tuple(
                (family, self._readiness[family]) for family in Family
            ),
            members=members,
        )
