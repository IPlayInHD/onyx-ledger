"""The closed vocabularies the authoring pipeline decides with.

Publication is a machine decision, so it is made on machine-readable codes. A
message explains a refusal to a person; it never *is* the refusal, because a
string comparison is not a governed check and a reworded message would silently
change what publishes.

Three vocabularies live here:

* `ValidationCode`  — WHAT is wrong, one closed code per implemented constraint.
* `Severity`        — whether it blocks. `ERROR` blocks; `WARNING` never does.
* `Readiness`       — per family, whether the pipeline can vouch for it at all.

Every code below is emitted by a real check. The set is deliberately derived
from the constraints that exist rather than from a wish list: a code nothing can
produce is a promise the pipeline does not keep, and a test asserts the two
agree.
"""
from __future__ import annotations

from enum import StrEnum

#: The report contract's own version, independent of the draft-spec version.
PUBLISHABILITY_REPORT_SCHEMA_VERSION = "1.0.0"


class Severity(StrEnum):
    """Whether a finding blocks publication.

    Two levels, not three. A hard requirement that can be downgraded is not a
    requirement, so nothing here converts an ERROR into advice — §23.
    """

    ERROR = "ERROR"
    WARNING = "WARNING"


class Readiness(StrEnum):
    """What the pipeline can say about one knowledge family.

    The negative states are kept apart on purpose. A draft that declares
    no formula (`NOT_APPLICABLE`) and a draft whose formula could not be
    resolved (`MISSING`) are different facts, and reporting either as an empty
    success is how an unanswered question comes to look like a clean bill of
    health — §74.
    """

    READY = "READY"
    #: Declared, but something it names could not be resolved or is invalid.
    MISSING = "MISSING"
    #: The draft declares this family does not apply. An authoritative absence.
    NOT_APPLICABLE = "NOT_APPLICABLE"
    #: Declared and present, but a hard check failed on its contents.
    INVALID = "INVALID"
    #: Not answerable at validation time; a later stage decides it. Distinct
    #: from NOT_APPLICABLE, which asserts the question does not arise at all —
    #: publication authority certainly arises, it is simply not validation's to
    #: answer, and saying "not applicable" would claim otherwise.
    DEFERRED_TO_PUBLICATION = "DEFERRED_TO_PUBLICATION"


class Family(StrEnum):
    """The readiness axes a publication decision is made across."""

    STRUCTURE = "structure"
    PROVENANCE = "provenance"
    CONDITIONS = "conditions"
    DEPENDENCIES = "dependencies"
    OUTCOMES = "outcomes"
    FORMULA = "formula"
    REFERENCE_DATA = "reference_data"
    EVIDENCE = "evidence"
    DEADLINES = "deadlines"
    ASSUMPTIONS = "assumptions"
    EXAMPLES = "examples"
    VERSIONING = "versioning"
    AUTHORITY = "authority"


class ValidationCode(StrEnum):
    """One code per implemented constraint. Closed, and machine-readable."""

    # ---- specification structure -------------------------------------------
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    SPEC_SCHEMA_VERSION_UNSUPPORTED = "SPEC_SCHEMA_VERSION_UNSUPPORTED"
    MALFORMED_VALUE = "MALFORMED_VALUE"

    # ---- jurisdiction / scope ----------------------------------------------
    UNKNOWN_JURISDICTION = "UNKNOWN_JURISDICTION"
    UNKNOWN_PROVINCE = "UNKNOWN_PROVINCE"
    FEDERAL_RULE_WITH_PROVINCE = "FEDERAL_RULE_WITH_PROVINCE"
    UNKNOWN_TAX_YEAR = "UNKNOWN_TAX_YEAR"
    UNKNOWN_CATEGORY = "UNKNOWN_CATEGORY"

    # ---- effective period ---------------------------------------------------
    INVALID_EFFECTIVE_PERIOD = "INVALID_EFFECTIVE_PERIOD"
    EFFECTIVE_PERIOD_OUTSIDE_TAX_YEAR = "EFFECTIVE_PERIOD_OUTSIDE_TAX_YEAR"
    INTRA_YEAR_EFFECTIVE_SCOPE_UNSUPPORTED = "INTRA_YEAR_EFFECTIVE_SCOPE_UNSUPPORTED"

    # ---- conditions ---------------------------------------------------------
    UNKNOWN_CONDITION_FIELD = "UNKNOWN_CONDITION_FIELD"
    UNKNOWN_OPERATOR = "UNKNOWN_OPERATOR"
    UNKNOWN_VALUE_TYPE = "UNKNOWN_VALUE_TYPE"
    OPERAND_TYPE_MISMATCH = "OPERAND_TYPE_MISMATCH"
    CONDITION_OPERAND_MISSING = "CONDITION_OPERAND_MISSING"
    UNKNOWN_LOGICAL_OPERATOR = "UNKNOWN_LOGICAL_OPERATOR"
    CONDITION_GROUP_TOO_DEEP = "CONDITION_GROUP_TOO_DEEP"
    DUPLICATE_CONDITION = "DUPLICATE_CONDITION"

    # ---- dependencies -------------------------------------------------------
    UNKNOWN_DEPENDENCY_TARGET = "UNKNOWN_DEPENDENCY_TARGET"
    UNKNOWN_DEPENDENCY_KIND = "UNKNOWN_DEPENDENCY_KIND"
    SELF_DEPENDENCY = "SELF_DEPENDENCY"
    DEPENDENCY_CYCLE = "DEPENDENCY_CYCLE"
    CROSS_JURISDICTION_DEPENDENCY = "CROSS_JURISDICTION_DEPENDENCY"
    DUPLICATE_DEPENDENCY = "DUPLICATE_DEPENDENCY"

    # ---- outcomes -----------------------------------------------------------
    UNKNOWN_OUTCOME_TYPE = "UNKNOWN_OUTCOME_TYPE"
    OUTCOME_REQUIRED = "OUTCOME_REQUIRED"
    OUTCOME_FIELD_REQUIRED = "OUTCOME_FIELD_REQUIRED"
    UNKNOWN_ECONOMIC_EFFECT_TYPE = "UNKNOWN_ECONOMIC_EFFECT_TYPE"
    UNKNOWN_REVERSIBILITY = "UNKNOWN_REVERSIBILITY"
    UNKNOWN_PORTFOLIO_LEVER = "UNKNOWN_PORTFOLIO_LEVER"
    LEVER_PARAMETER_UNRESOLVABLE = "LEVER_PARAMETER_UNRESOLVABLE"
    DUPLICATE_OUTCOME = "DUPLICATE_OUTCOME"

    # ---- formula ------------------------------------------------------------
    # An unregistered operation and an undeclared input are ONE code, not two:
    # the RPN evaluator resolves any non-operator token as a variable, so `pow`
    # genuinely reaches it as an undeclared input. The message names the
    # registered operations, which is what an author needs; a second code would
    # imply a distinction the evaluator does not make.
    FORMULA_INPUT_UNDECLARED = "FORMULA_INPUT_UNDECLARED"
    FORMULA_INPUT_UNKNOWN_FACT = "FORMULA_INPUT_UNKNOWN_FACT"
    FORMULA_MALFORMED = "FORMULA_MALFORMED"
    FORMULA_LANGUAGE_UNSUPPORTED = "FORMULA_LANGUAGE_UNSUPPORTED"
    FORMULA_DIVIDES_BY_ZERO = "FORMULA_DIVIDES_BY_ZERO"
    FORMULA_NON_DECIMAL_LITERAL = "FORMULA_NON_DECIMAL_LITERAL"
    FORMULA_PRECISION_EXCEEDED = "FORMULA_PRECISION_EXCEEDED"
    FORMULA_CONFLICTS_WITH_PUBLISHED = "FORMULA_CONFLICTS_WITH_PUBLISHED"
    FORMULA_REQUIRED = "FORMULA_REQUIRED"

    # ---- reference data -----------------------------------------------------
    REFERENCE_DATA_MISSING = "REFERENCE_DATA_MISSING"
    UNKNOWN_REFERENCE_DATA_KIND = "UNKNOWN_REFERENCE_DATA_KIND"
    BRACKETS_NOT_ORDERED = "BRACKETS_NOT_ORDERED"
    BRACKETS_OVERLAP = "BRACKETS_OVERLAP"
    BRACKETS_NOT_CONTIGUOUS = "BRACKETS_NOT_CONTIGUOUS"
    TERMINAL_BRACKET_MISSING = "TERMINAL_BRACKET_MISSING"
    RATE_OUT_OF_RANGE = "RATE_OUT_OF_RANGE"
    REFERENCE_DATA_ALREADY_PUBLISHED = "REFERENCE_DATA_ALREADY_PUBLISHED"
    UNKNOWN_REGISTERED_TYPE = "UNKNOWN_REGISTERED_TYPE"

    # ---- evidence / deadlines / assumptions ---------------------------------
    EVIDENCE_TYPE_UNKNOWN = "EVIDENCE_TYPE_UNKNOWN"
    UNKNOWN_EVIDENCE_NECESSITY = "UNKNOWN_EVIDENCE_NECESSITY"
    DUPLICATE_EVIDENCE = "DUPLICATE_EVIDENCE"
    DEADLINE_INVALID = "DEADLINE_INVALID"
    DEADLINE_CODE_UNKNOWN = "DEADLINE_CODE_UNKNOWN"
    DUPLICATE_DEADLINE = "DUPLICATE_DEADLINE"
    ASSUMPTION_CODE_UNKNOWN = "ASSUMPTION_CODE_UNKNOWN"

    # ---- provenance ---------------------------------------------------------
    SOURCE_REQUIRED = "SOURCE_REQUIRED"
    SOURCE_CITATION_UNKNOWN = "SOURCE_CITATION_UNKNOWN"
    SOURCE_NOT_PRODUCTION_QUALIFYING = "SOURCE_NOT_PRODUCTION_QUALIFYING"
    SOURCE_WITHDRAWN = "SOURCE_WITHDRAWN"
    SOURCE_SUPERSEDED = "SOURCE_SUPERSEDED"
    SOURCE_INTEGRITY_UNAVAILABLE = "SOURCE_INTEGRITY_UNAVAILABLE"
    DUPLICATE_CITATION = "DUPLICATE_CITATION"

    # ---- examples -----------------------------------------------------------
    EXAMPLES_REQUIRED = "EXAMPLES_REQUIRED"
    POSITIVE_EXAMPLE_REQUIRED = "POSITIVE_EXAMPLE_REQUIRED"
    NEGATIVE_EXAMPLE_REQUIRED = "NEGATIVE_EXAMPLE_REQUIRED"
    EXAMPLE_FAILED = "EXAMPLE_FAILED"
    EXAMPLE_FACT_UNKNOWN = "EXAMPLE_FACT_UNKNOWN"
    FORMULA_VECTOR_FAILED = "FORMULA_VECTOR_FAILED"

    # ---- versioning / publication -------------------------------------------
    RULE_CODE_CONFLICT = "RULE_CODE_CONFLICT"
    SUPERSESSION_TARGET_INVALID = "SUPERSESSION_TARGET_INVALID"
    SUPERSESSION_TARGET_MISSING = "SUPERSESSION_TARGET_MISSING"
    SUPERSESSION_REQUIRED = "SUPERSESSION_REQUIRED"
    # No ACTIVE_VERSION_OVERLAP: `uq_rule_version_published` permits one
    # published version per (rule, tax_year), and an effective period outside
    # its own tax year is refused, so two active versions cannot overlap. A code
    # for a condition the schema makes impossible would be a promise about a
    # check that does not exist.
    SPEC_CHANGED_SINCE_VALIDATION = "SPEC_CHANGED_SINCE_VALIDATION"
    PACK_DUPLICATE_OBJECT = "PACK_DUPLICATE_OBJECT"


#: Codes that describe a *pack*, not a single object. Reported against the pack
#: so a batch result never hides a collision inside one member's findings.
PACK_SCOPED_CODES = frozenset({
    ValidationCode.PACK_DUPLICATE_OBJECT,
    ValidationCode.DEPENDENCY_CYCLE,
})
