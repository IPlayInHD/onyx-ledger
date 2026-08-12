"""Evidence readiness — derived, never guessed.

The property under test is that readiness is a statement about set membership
over one shared vocabulary, and that the one case which is NOT knowable from
governed data says so instead of guessing.
"""
from app.services.state_graph.contracts import EvidenceReadiness
from app.services.state_graph.readiness import (
    DocumentRequirement,
    resolve_requirement,
    rollup_readiness,
)

VERSION = "11111111-1111-1111-1111-111111111111"


def _req(code: str, necessity: str = "required") -> DocumentRequirement:
    return DocumentRequirement(
        rule_version_id=VERSION, document_type_code=code, necessity=necessity
    )


def test_a_required_document_that_is_held_is_ready():
    coverage = resolve_requirement(_req("T4"), {"T4": ["doc-1"]})
    assert coverage.readiness is EvidenceReadiness.READY
    assert coverage.satisfying_document_ids == ("doc-1",)


def test_a_required_document_that_is_not_held_is_missing():
    assert resolve_requirement(_req("T4"), {}).readiness is EvidenceReadiness.MISSING


def test_a_conditional_requirement_is_unknown_even_when_a_document_is_held():
    """The decisive case. Whether a conditional requirement applies is carried
    only in a free-text note, so both MISSING and READY would be a guess —
    including the optimistic one."""
    assert resolve_requirement(
        _req("T2202", "conditional"), {"T2202": ["doc-9"]}
    ).readiness is EvidenceReadiness.UNKNOWN
    assert resolve_requirement(
        _req("T2202", "conditional"), {}
    ).readiness is EvidenceReadiness.UNKNOWN


def test_a_recommended_document_is_never_a_blocker():
    """Its absence does not make a rule's evidence incomplete, so reporting
    MISSING would send a user to find something they do not need."""
    assert resolve_requirement(
        _req("NOTE", "recommended"), {}
    ).readiness is EvidenceReadiness.NOT_REQUIRED


def test_partial_is_produced_by_the_rollup_and_never_by_one_requirement():
    """A single requirement is binary — held or not. Only the SET of them can be
    half satisfied, which is why PARTIAL exists at the rule level."""
    per_requirement = {
        resolve_requirement(r, {"T4": ["doc-1"]}).readiness
        for r in (_req("T4"), _req("T5"))
    }
    assert EvidenceReadiness.PARTIAL not in per_requirement

    coverages = [resolve_requirement(r, {"T4": ["doc-1"]}) for r in (_req("T4"), _req("T5"))]
    assert rollup_readiness(coverages) is EvidenceReadiness.PARTIAL


def test_a_rule_with_no_requirements_is_not_required():
    assert rollup_readiness([]) is EvidenceReadiness.NOT_REQUIRED


def test_all_held_rolls_up_to_ready_and_none_held_to_missing():
    held = {"T4": ["a"], "T5": ["b"]}
    coverages = [resolve_requirement(r, held) for r in (_req("T4"), _req("T5"))]
    assert rollup_readiness(coverages) is EvidenceReadiness.READY

    coverages = [resolve_requirement(r, {}) for r in (_req("T4"), _req("T5"))]
    assert rollup_readiness(coverages) is EvidenceReadiness.MISSING


def test_an_unknown_anywhere_outranks_a_confident_partial():
    """Ordering matters: if one requirement's applicability is unknowable, the
    rule's readiness is not knowable either, and PARTIAL would overstate what
    the data supports."""
    coverages = [
        resolve_requirement(_req("T4"), {"T4": ["a"]}),
        resolve_requirement(_req("T5"), {}),
        resolve_requirement(_req("T2202", "conditional"), {}),
    ]
    assert rollup_readiness(coverages) is EvidenceReadiness.UNKNOWN


def test_recommended_documents_do_not_drag_a_ready_rule_down():
    coverages = [
        resolve_requirement(_req("T4"), {"T4": ["a"]}),
        resolve_requirement(_req("NOTE", "recommended"), {}),
    ]
    assert rollup_readiness(coverages) is EvidenceReadiness.READY


def test_satisfying_document_ids_are_sorted_so_the_hash_is_stable():
    """Fetch order is not a fact about the user's evidence. This is the same
    defect class CI caught in the portfolio rebuild, where exclusions were
    hashed in the order the database happened to return them."""
    forward = resolve_requirement(_req("T4"), {"T4": ["b", "a", "c"]})
    reverse = resolve_requirement(_req("T4"), {"T4": ["c", "b", "a"]})
    assert forward.satisfying_document_ids == reverse.satisfying_document_ids == (
        "a", "b", "c",
    )
