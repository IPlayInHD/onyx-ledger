"""The registry's own invariants, and the terminal-delete gate that fails closed.

Two different questions are asked here, and keeping them apart is the point of
the whole exercise:

  QUESTION A — evidence-survival safety. "Does deleting the account destroy
  something that has to survive?" Answerable from a single table proven to be
  retained. It does not wait on anything else being classified, and it is
  already answered: `ioe.optimization_run` is a direct CASCADE child of the root
  and is proven to require survival. That makes the root edge unsafe today.

  QUESTION B — terminal privacy completeness. "Is it safe to run the terminal
  account delete?" Answerable only when every cascade-reachable table carries a
  classification. `assert_terminal_account_delete_ready()` is the gate, and it
  fails closed on the 66 tables nobody has looked at yet.

The failure of B does not block A, and A being answered does not discharge B.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from tests.privacy.account_delete_registry import (
    CLASSIFIED,
    DELETING_CLASSIFICATIONS,
    REGISTRY,
    RETAINING_CLASSIFICATIONS,
    UNCLASSIFIED_BLOCKING,
    VALID_CLASSIFICATIONS,
    VALID_EVIDENCE_QUALITIES,
    assert_terminal_account_delete_ready,
    proven_deletable_tables,
    proven_retained_tables,
    terminal_delete_blockers,
)

BACKEND = pathlib.Path(__file__).resolve().parents[2]
UNIVERSE_DOC = BACKEND.parent / "docs/privacy/11b6-account-delete-cascade-universe.md"
UNIVERSE_ROW = re.compile(r"^\| (\d+) \| `([a-z0-9_]+\.[a-z0-9_]+)` \| — \|$")


# --------------------------------------------------------------- §26 invariants

def test_the_registry_covers_the_certified_universe_exactly():
    """One entry per certified table, no strays, no duplicates.

    `REGISTRY` is a dict, so "exactly once" is structural — what this checks is
    that the *set* matches the document the classification work is scoped to.
    """
    doc = {}
    for line in UNIVERSE_DOC.read_text().splitlines():
        m = UNIVERSE_ROW.match(line)
        if m:
            assert m.group(2) not in doc, f"duplicate row in universe doc: {m.group(2)}"
            doc[m.group(2)] = int(m.group(1))

    assert len(doc) == 70, f"the universe doc parsed to {len(doc)} tables, not 70"
    assert sorted(set(doc) - set(REGISTRY)) == []
    assert sorted(set(REGISTRY) - set(doc)) == []
    assert {t: e.depth for t, e in REGISTRY.items()} == doc


def test_every_entry_is_in_one_of_the_two_states():
    for table, entry in REGISTRY.items():
        assert entry.state in (CLASSIFIED, UNCLASSIFIED_BLOCKING), (
            f"{table} is in an unknown state {entry.state!r}"
        )


def test_a_classified_entry_carries_its_full_justification():
    """A classification without a reason, a quality, and a reference is a guess."""
    for table, entry in REGISTRY.items():
        if entry.state != CLASSIFIED:
            continue
        assert entry.classification in VALID_CLASSIFICATIONS, (
            f"{table}: {entry.classification!r} is not a recognised classification"
        )
        assert entry.reason_code, f"{table} is classified with no reason_code"
        assert entry.evidence_quality in VALID_EVIDENCE_QUALITIES, (
            f"{table}: {entry.evidence_quality!r} is not a recognised evidence quality"
        )
        assert entry.rationale and len(entry.rationale) > 40, (
            f"{table} has no rationale worth the name"
        )
        assert entry.evidence_references, f"{table} cites no evidence"


def test_every_cited_evidence_reference_resolves():
    """A citation that points nowhere is worse than no citation.

    This is what stops the registry from decaying into folklore as files move:
    the reference has to name a real file, and a `path:line` reference has to
    name a line that exists.
    """
    for table, entry in REGISTRY.items():
        for ref in entry.evidence_references:
            path, _, line = ref.partition(":")
            target = BACKEND / path
            assert target.is_file(), f"{table} cites a missing file: {ref}"
            if line:
                count = len(target.read_text().splitlines())
                assert 0 < int(line) <= count, (
                    f"{table} cites {ref}, but that file has {count} lines"
                )


def test_a_row_state_dependent_entry_says_what_the_other_states_still_owe():
    """§14 — "retain" on a mixed table is only honest with the cleanup attached.

    A table whose sealed rows must survive and whose live rows must not is
    classified for the survival, because that is the constraint the cascade
    violates. Left there, it reads as permission to keep everything. The
    cleanup note is what stops that.
    """
    for table, entry in REGISTRY.items():
        if not entry.row_state_dependent:
            continue
        assert entry.state == CLASSIFIED, f"{table} is unclassified but state-dependent"
        assert entry.state_conditioned_cleanup, (
            f"{table} is row-state dependent and does not say what happens to "
            "the states it does not retain"
        )
    for table, entry in REGISTRY.items():
        if entry.state_conditioned_cleanup:
            assert entry.row_state_dependent, (
                f"{table} carries cleanup metadata without being marked "
                "row-state dependent"
            )


def test_an_unclassified_entry_carries_no_classification_fields():
    """`UNCLASSIFIED_BLOCKING` is a workflow state, not a quiet classification."""
    for table, entry in REGISTRY.items():
        if entry.state != UNCLASSIFIED_BLOCKING:
            continue
        assert entry.classification is None, f"{table} is unclassified but classified"
        assert entry.reason_code is None
        assert entry.evidence_quality is None
        assert entry.evidence_references == ()
        assert entry.row_state_dependent is False
        assert entry.state_conditioned_cleanup is None


def test_an_unclassified_entry_never_reads_as_approved_for_deletion():
    """The invariant the whole default-deny design exists to hold.

    `protected_from_destructive_cascade` is `None`, not `False`, for an unknown
    table. `False` means "proven safe to destroy", and a caller writing
    `if not entry.protected_from_destructive_cascade: drop_it()` on a `False`
    that really meant "nobody has checked" is the exact accident this prevents.
    So the unknown case is asserted to be distinguishable by identity, not just
    by truthiness.
    """
    for table, entry in REGISTRY.items():
        if entry.state != UNCLASSIFIED_BLOCKING:
            continue
        verdict = entry.protected_from_destructive_cascade
        assert verdict is None, f"{table} is unclassified but reports {verdict!r}"
        assert verdict is not False, f"{table} reads as proven-deletable"

    assert set(proven_deletable_tables()).isdisjoint(terminal_delete_blockers())
    assert set(proven_retained_tables()).isdisjoint(terminal_delete_blockers())


def test_the_two_classification_families_do_not_overlap():
    """`RETAINING` and `DELETING` are opposite verdicts; a table cannot be both."""
    assert RETAINING_CLASSIFICATIONS.isdisjoint(DELETING_CLASSIFICATIONS)
    assert VALID_CLASSIFICATIONS == RETAINING_CLASSIFICATIONS | DELETING_CLASSIFICATIONS
    assert UNCLASSIFIED_BLOCKING not in VALID_CLASSIFICATIONS, (
        "the workflow state leaked into the retention vocabulary"
    )


def test_the_helper_partition_is_total():
    """Retained, deletable and blocking together account for every table, once."""
    retained = set(proven_retained_tables())
    deletable = set(proven_deletable_tables())
    blocking = set(terminal_delete_blockers())
    assert retained | deletable | blocking == set(REGISTRY)
    assert len(retained) + len(deletable) + len(blocking) == len(REGISTRY)


# ------------------------------------------------------- §27 semantic anchors

def test_the_evidenced_classifications_are_exactly_the_ones_that_were_read():
    """Exactly the ones somebody read the evidence for, and no more.

    Seeding one extra from a plausible-looking name is how the superseded
    "31 protected" figure was produced, so the list is spelled out rather than
    counted.
    """
    classified = sorted(t for t, e in REGISTRY.items() if e.state == CLASSIFIED)
    assert classified == [
        "analysis.analysis_run",
        "docs.document",
        "docs.document_extraction",
        "docs.extraction_field",
        "ioe.freshness_outbox",
        "ioe.integrity_check",
        "ioe.optimization_run",
        "ioe.run_rule_snapshot",
        "ioe.scenario",
        "reco.recommendation",
    ]
    assert len(terminal_delete_blockers()) == 60


def test_the_sealed_replay_output_is_retained():
    """`ioe.optimization_run` holds the sealed hashes replay verifies against."""
    entry = REGISTRY["ioe.optimization_run"]
    assert entry.classification == "REPLAY_REQUIRED_RETAIN"
    assert entry.reason_code == "SEALED_REPLAY_OUTPUT"
    assert entry.protected_from_destructive_cascade is True


def test_the_rule_version_pin_is_retained():
    """`ioe.run_rule_snapshot` pins the rules a retained run was computed under.

    Note the disagreement this encodes: `app/privacy/classification.py` declares
    this table `CASCADE_DELETE`, while `test_sealed_history_after_purge` captures
    its rule pin and asserts the bytes are identical after the account purge. The
    test measures behaviour; the declaration states intent. The registry follows
    the measurement, and the conflict is recorded rather than smoothed over.
    """
    entry = REGISTRY["ioe.run_rule_snapshot"]
    assert entry.classification == "SEALED_IMMUTABLE_RETAIN"
    assert entry.reason_code == "RULE_VERSION_PIN"
    assert entry.evidence_quality == "DIRECT_TEST_EVIDENCE"
    assert entry.protected_from_destructive_cascade is True


def test_the_live_recommendation_is_deletable():
    """`reco.recommendation` is the subject's own product state, not evidence."""
    entry = REGISTRY["reco.recommendation"]
    assert entry.classification == "LIVE_USER_DATA_DELETE"
    assert entry.protected_from_destructive_cascade is False


def test_the_transient_outbox_is_deletable():
    """`ioe.freshness_outbox` is drained and deleted in ordinary operation."""
    entry = REGISTRY["ioe.freshness_outbox"]
    assert entry.classification == "DERIVED_DELETE"
    assert entry.reason_code == "OPERATIONAL_QUEUE"
    assert entry.protected_from_destructive_cascade is False


# ------------------------------------------- §6 question A / §19 question B

def test_question_a_the_root_cascade_edge_is_already_unsafe():
    """Root-first: one proven-retained direct CASCADE child settles this.

    `ioe.optimization_run` sits at depth 1 — a direct `ON DELETE CASCADE` child
    of `identity.user_account` — and is proven to require survival. So
    `DELETE FROM identity.user_account` destroys retained evidence *today*,
    without waiting on the other 66 classifications. Question A is answered, and
    the answer is unsafe.
    """
    retained = REGISTRY["ioe.optimization_run"]
    assert retained.depth == 1, (
        "the evidence-survival argument depended on this being a DIRECT cascade "
        "child; it is no longer at depth 1 and the argument must be rebuilt"
    )
    assert retained.protected_from_destructive_cascade is True

    unsafe_root_children = [
        table
        for table, entry in REGISTRY.items()
        if entry.depth == 1 and entry.protected_from_destructive_cascade is True
    ]
    assert unsafe_root_children, (
        "no proven-retained direct cascade child remains; question A is no longer "
        "answered and must be re-established before relying on it"
    )


def test_question_b_the_terminal_delete_gate_fails_closed():
    """66 of 70 unclassified, so the terminal delete must not be certified."""
    with pytest.raises(AssertionError) as excinfo:
        assert_terminal_account_delete_ready()

    message = str(excinfo.value)
    assert "60" in message and "70" in message
    assert "UNCLASSIFIED_BLOCKING" in message, (
        "the failure must name the workflow state, or the reader will read it as "
        "a retention verdict"
    )


def test_the_terminal_gate_would_pass_only_on_a_fully_classified_registry():
    """Guard on the guard: the gate is not a `raise` with the condition inverted.

    Without this, a gate that failed unconditionally would look identical to one
    that fails for the right reason, and would keep "passing" its own test long
    after the 66 were classified.
    """
    from tests.privacy import account_delete_registry as mod

    original = mod.REGISTRY
    mod.REGISTRY = {
        table: (
            entry
            if entry.state == CLASSIFIED
            else mod.Entry(
                state=CLASSIFIED,
                depth=entry.depth,
                classification="LIVE_USER_DATA_DELETE",
                reason_code="TEST_ONLY",
                evidence_quality="DIRECT_TEST_EVIDENCE",
                rationale="in-memory substitution for the guard-on-the-guard only",
                evidence_references=("tests/privacy/test_account_delete_registry.py:1",),
            )
        )
        for table, entry in original.items()
    }
    try:
        assert mod.terminal_delete_blockers() == []
        mod.assert_terminal_account_delete_ready()
    finally:
        mod.REGISTRY = original

    assert len(terminal_delete_blockers()) == 60, "the substitution leaked"
