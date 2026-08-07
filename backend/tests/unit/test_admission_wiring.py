"""Structural assertions about admission wiring (§8–§9, §13, §16–§17).

Entry 10 Phase 1's real failure was not a bug in the mechanism — the mechanism
was correct — it was six policies with no call site. That failure is invisible
to every ordinary test: nothing throws, nothing regresses, the registry reads
like protection, and the endpoint is unguarded. Only a test that looks at the
SHAPE of the code can catch it, so these read the source.

They are deliberately cheap and have no database: they are about what the
repository contains, not about what PostgreSQL does with it.
"""
from __future__ import annotations

import re
from fnmatch import fnmatch
from pathlib import Path

import pytest

from app.services.admission.policy import (
    UNUSED_REJECTION_REASONS,
    UNWIRED_BY_DESIGN,
    OperationClass,
    RejectionReason,
)

BACKEND = Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
WORKERS = BACKEND / "workers"

#: Publishing a task to the broker. Any of these in `app/` would be an enqueue
#: path a request can reach.
_PUBLISH = re.compile(r"\.delay\s*\(|\.apply_async\s*\(|send_task\s*\(")


def _python_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _sources(root: Path) -> dict[Path, str]:
    return {p: p.read_text() for p in _python_files(root)}


def _call_sites() -> dict[Path, str]:
    """`app/` minus the registry.

    `policy.py` names every `OperationClass` and every `RejectionReason` by
    definition, and its wiring ledger quotes `.delay()` in prose. Counting it as
    a call site would make every one of these tests pass vacuously.
    """
    return {p: s for p, s in _sources(APP).items() if p.name != "policy.py"}


def test_nothing_in_the_request_path_publishes_a_task():
    """§8–§9 — admission rejected implies zero new expensive task published.

    Asserted at its strongest available form: `app/` does not publish AT ALL.
    Every `.delay()` in the repository is worker-to-worker stage chaining inside
    `workers/tasks/`, reached only after an operator-triggered import has
    already passed IMPORT_RUN admission at the API boundary.

    So the invariant does not rest on each enqueue site remembering to check
    first — there are no enqueue sites in the request path to remember. If one
    is ever added, this test fails and the author has to put a guard in front of
    it, which is the moment to think about it rather than six months later.
    """
    offenders = [
        f"{path.relative_to(BACKEND)}:{lineno}: {line.strip()}"
        for path, source in _call_sites().items()
        for lineno, line in enumerate(source.splitlines(), 1)
        if _PUBLISH.search(line)
    ]
    assert not offenders, (
        "a module in the request path publishes a Celery task; it must take "
        "admission BEFORE the publish, and this test must then be taught about "
        "it deliberately:\n  " + "\n  ".join(offenders)
    )


def test_every_worker_task_has_an_explicit_queue_route():
    """§17 — routing is code-enforced; CAPACITY is not, and must not be claimed.

    What this repository controls is which QUEUE a task lands on, and that is
    asserted here: a task with no route falls to Celery's default queue, where
    it competes with everything else and the queue separation stops meaning
    anything.

    What it does NOT control is how many workers consume each queue. That is a
    deployment concern — a `-Q` flag and a concurrency setting — and there is no
    file in this repository that decides it. Saying "expensive work is isolated"
    would therefore be half true; the honest claim is that the ROUTING is
    guaranteed here and the CAPACITY is guaranteed by whoever runs the workers.
    """
    from workers.celery_app import celery_app

    routes = celery_app.conf.task_routes or {}
    declared = {
        name
        for source in _sources(WORKERS).values()
        for name in re.findall(r'@celery_app\.task\(\s*name="([^"]+)"', source)
    }
    assert declared, "no Celery tasks found; the test is looking in the wrong place"

    # Celery matches route keys as globs, so `workers.tasks.analysis.*` really
    # does route `workers.tasks.analysis.run_analysis`. Comparing exact strings
    # would report every glob-routed task as unrouted — which this test did on
    # its first run, and the config was right.
    unrouted = sorted(
        name for name in declared
        if not any(fnmatch(name, pattern) for pattern in routes)
    )
    assert not unrouted, (
        "these tasks have no explicit queue and would fall to the default "
        f"queue: {unrouted}"
    )


def test_every_operation_class_is_either_wired_or_explicitly_reserved():
    """§13, §16 — no class may be silently absent from both.

    A policy with no call site bounds nothing while looking in review exactly
    like one that does. Requiring an entry in `UNWIRED_BY_DESIGN` makes the
    absence a claim someone wrote down and a reviewer can disagree with.
    """
    guarded = "\n".join(_call_sites().values())

    wired = {
        member for member in OperationClass
        if f"OperationClass.{member.name}" in guarded
    }
    reserved = set(UNWIRED_BY_DESIGN)

    missing = sorted(set(OperationClass) - wired - reserved)
    assert not missing, (
        "these classes have no call site and no recorded reason:\n  "
        + "\n  ".join(str(m) for m in missing)
        + "\nWire them, or add an entry to UNWIRED_BY_DESIGN saying why not."
    )

    both = sorted(wired & reserved)
    assert not both, (
        "these classes are recorded as unwired but DO have a call site; the "
        f"record is stale: {both}"
    )


@pytest.mark.parametrize("reason", list(RejectionReason))
def test_every_rejection_reason_is_either_raised_or_explicitly_reserved(reason):
    """The same ledger discipline for the codes a caller can receive.

    A code that nothing raises is a documented behaviour that never happens.
    Callers write handling for it, dashboards leave a panel for it, and the
    panel stays at zero forever — which reads as "this never goes wrong" rather
    than "this cannot be reported".
    """
    raised = "\n".join(_call_sites().values())
    is_raised = f"RejectionReason.{reason.name}" in raised
    is_reserved = reason in UNUSED_REJECTION_REASONS

    assert is_raised or is_reserved, (
        f"{reason} is never raised and has no entry in "
        "UNUSED_REJECTION_REASONS explaining why it exists"
    )
    assert not (is_raised and is_reserved), (
        f"{reason} is recorded as unused but is raised in app/; the record is "
        "stale"
    )


def test_the_reserved_queue_capacity_premise_still_holds():
    """§16 — QUEUE_CAPACITY is reserved BECAUSE there is no user-reachable
    enqueue path. That premise is a fact about the code, so it is asserted
    rather than trusted: if an enqueue path appears, the reservation becomes
    false and this fails alongside the publish test above."""
    assert RejectionReason.QUEUE_CAPACITY in UNUSED_REJECTION_REASONS
    publishers = [
        str(path.relative_to(BACKEND))
        for path, source in _sources(WORKERS).items()
        if _PUBLISH.search(source)
    ]
    assert publishers == ["workers/tasks/tkms.py"], (
        "the set of task publishers changed; re-check whether a user-reachable "
        f"enqueue path now exists: {publishers}"
    )
