"""Which file is the authority on what account deletion does, and where they diverge.

Two registries in this repository make retention claims about the same tables:

  `app/privacy/classification.py` — DECLARED treatment. Hand-maintained, one
  entry per user-derived table, covering privacy class, RLS, export, the
  SOURCE_DATA purge, and `on_account_deletion`. Consumed by
  `scripts/export_privacy_inventory.py` (a CSV view) and by
  `tests/security/test_privacy_inventory.py` (the gate that refuses an
  unclassified user-derived table). **No production code reads it**, and it
  drives no SQL mutation — grep for `LIFECYCLE` outside tests and that one
  script and there is nothing.

  `tests/privacy/account_delete_registry.py` — MEASURED treatment. Membership
  re-derived from `pg_constraint` on every run, verdicts backed by execution.
  Test and certification only; no production consumer at all.

**They answer the same question.** `DeletionAction`'s own docstring is "What
ACCOUNT DELETION does to this table", which is exactly what the registry
decides. So per Entry 11B6C §7 they must agree, and an unexplained disagreement
is a defect rather than a nuance.

Where they do disagree, the registry wins, because it is measured and the
declaration is not. That is not a licence to ignore the declaration: every
divergence has to be listed below with a reason, and a divergence nobody listed
fails this file.
"""

from __future__ import annotations

from app.privacy import LIFECYCLE, DeletionAction
from tests.privacy.account_delete_registry import (
    CLASSIFIED,
    DELETING_CLASSIFICATIONS,
    REGISTRY,
    RETAINING_CLASSIFICATIONS,
)

#: `DeletionAction` values that leave the row in place.
_DECLARED_RETAINING = frozenset({
    DeletionAction.RETAIN,
    DeletionAction.DE_IDENTIFY,
    DeletionAction.TOMBSTONE,
})

#: `DeletionAction` values that remove it.
_DECLARED_DELETING = frozenset({
    DeletionAction.HARD_DELETE,
    DeletionAction.CASCADE_DELETE,
})

#: `CUSTOM_WORKFLOW` is "needs its own logic", which is a statement about how
#: the answer is reached rather than what it is. It is compatible with either
#: verdict and is deliberately in neither set above.
_DECLARED_UNDECIDED = frozenset({DeletionAction.CUSTOM_WORKFLOW})


#: Tables where the two registries genuinely disagree, each with the reason it
#: is tolerated. Empty is the goal. A disagreement not listed here fails.
KNOWN_AUTHORITY_DIVERGENCES: dict[str, str] = {}


def _declared_verdict(action: DeletionAction) -> bool | None:
    if action in _DECLARED_RETAINING:
        return True
    if action in _DECLARED_DELETING:
        return False
    return None


def _comparable() -> list[tuple[str, bool, bool | None]]:
    """(table, registry_retains, declared_retains) for every table in both."""
    out = []
    for table, entry in REGISTRY.items():
        if entry.state != CLASSIFIED:
            continue
        declared = LIFECYCLE.get(table)
        if declared is None:
            continue
        registry_retains = entry.protected_from_destructive_cascade
        assert registry_retains is not None, f"{table} is CLASSIFIED with no verdict"
        out.append((table, registry_retains, _declared_verdict(declared.on_account_deletion)))
    return out


def test_the_two_registries_answer_the_same_question():
    """Stated so the next reader does not have to re-derive it.

    If this ever stops being true — if `DeletionAction` is rescoped to mean
    something narrower than account deletion — this file's whole premise
    changes, and the docstring it asserts against has to change with it.
    """
    assert "ACCOUNT DELETION" in (DeletionAction.__doc__ or "").upper(), (
        "DeletionAction no longer claims to describe account deletion; the "
        "reconciliation in this file assumes it does"
    )


def test_every_classified_table_agrees_with_its_declaration():
    """The reconciliation. Registry verdict vs declared verdict, per table."""
    disagreements = {
        table: (
            f"registry={'RETAIN' if reg else 'DELETE'} "
            f"declared={LIFECYCLE[table].on_account_deletion.value}"
        )
        for table, reg, declared in _comparable()
        if declared is not None and declared != reg
    }
    unexplained = sorted(set(disagreements) - set(KNOWN_AUTHORITY_DIVERGENCES))
    assert not unexplained, (
        "these tables are classified in both registries and disagree, with no "
        "recorded reason:\n  "
        + "\n  ".join(f"{t}: {disagreements[t]}" for t in unexplained)
        + "\nEither correct the declaration or record the divergence in "
        "KNOWN_AUTHORITY_DIVERGENCES with the reason it is tolerated."
    )


def test_no_stale_divergence_entries():
    """A recorded divergence that no longer exists is a claim about nothing."""
    live = {
        table
        for table, reg, declared in _comparable()
        if declared is not None and declared != reg
    }
    stale = sorted(set(KNOWN_AUTHORITY_DIVERGENCES) - live)
    assert not stale, f"KNOWN_AUTHORITY_DIVERGENCES names resolved entries: {stale}"


def test_a_replay_dependency_never_declares_an_unqualified_destructive_action():
    """The defect class behind the `run_rule_snapshot` contradiction.

    `replay_dependency=True` says a sealed historical result cannot be verified
    without this row. `CASCADE_DELETE` says account deletion removes it. Both
    cannot be true, and the inventory gate only forbids the `HARD_DELETE`
    spelling of it — so the contradiction was expressible and got written down.

    This is scoped to tables the account-delete registry has actually
    classified. Twenty further replay-dependent tables still declare
    CASCADE_DELETE; they are UNCLASSIFIED_BLOCKING in the registry, so nothing
    has been measured about them yet and asserting on them here would be a
    guess. Widening this to every replay dependency is the follow-up.
    """
    offenders = sorted(
        f"{table}: {LIFECYCLE[table].on_account_deletion.value}"
        for table, entry in REGISTRY.items()
        if entry.state == CLASSIFIED
        and table in LIFECYCLE
        and LIFECYCLE[table].replay_dependency
        and LIFECYCLE[table].on_account_deletion in _DECLARED_DELETING
    )
    assert not offenders, (
        "replay depends on these rows and the declaration destroys them:\n  "
        + "\n  ".join(offenders)
    )


def test_the_registry_is_not_wired_into_production():
    """The registry is certification, not a runtime deletion authority.

    Recorded as a test because the distinction matters for how much weight a
    reader should put on it, and because wiring it in later should be a
    deliberate act that fails here first.
    """
    import pathlib

    backend = pathlib.Path(__file__).resolve().parents[2]
    importers = [
        path.relative_to(backend).as_posix()
        for path in list((backend / "app").rglob("*.py"))
        + list((backend / "workers").rglob("*.py"))
        if "account_delete_registry" in path.read_text()
    ]
    assert importers == [], (
        f"production code now imports the account-delete registry: {importers}. "
        "That is a real change in its authority — update this file's docstring "
        "and this test deliberately rather than deleting the assertion."
    )


def test_the_classified_verdicts_partition_cleanly():
    """No classification is simultaneously retaining and deleting."""
    for table, entry in REGISTRY.items():
        if entry.state != CLASSIFIED:
            continue
        assert (entry.classification in RETAINING_CLASSIFICATIONS) != (
            entry.classification in DELETING_CLASSIFICATIONS
        ), f"{table}: {entry.classification} is in both families or neither"
