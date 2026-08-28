"""Slice 0B — every module that declares a routed task is reachable from a
clean Celery worker start.

THE DIRECTION THIS ADDS. `tests/unit/test_admission_wiring.py` proves
task → route: every declared task has an explicit queue. Nothing proved
module → registration: that the module declaring a routed task is actually
imported by `celery -A workers.celery_app worker`. The two can disagree
silently — `workers.tasks.documents.extract_document` was declared, routed to
the `documents` queue, consumed by `worker-app` (infra/modules/compute/
services.tf), and absent from `celery_app.include`, so a real worker received
messages for a task it had never registered (integration plan §4.2).

WHY THE EVIDENCE MUST COME FROM A SUBPROCESS. Registration is an import side
effect: the `@celery_app.task` decorator registers on module import, wherever
that import happens. A pytest process that has already imported a task module —
`test_document_extraction_worker.py` does so at module load — carries every
task in `celery_app.tasks` regardless of what `include` says, which is exactly
how the defect above stayed invisible. So the registration evidence here comes
from a fresh interpreter that imports only `workers.celery_app` and then runs
the worker's own default-module loading (`loader.init_worker()`), and the
probe refuses to report if any task module was imported by any other path
first.

WHY DISCOVERY IS STATIC. Declarations are read from the `workers/tasks/` tree
by AST — the same discovery direction admission wiring uses — never by
importing the declaring module in this process (that import would be the
vacuity), and never by inverting the route table: a route pattern is not
evidence that a task exists. `workers.tasks.ingestion.*` and
`workers.tasks.notify.*` have been routed since the queue table was written
and no module has ever declared under them. They are reserved names, recorded
in RESERVED_ROUTES below so an unclaimed route is a statement someone wrote
down rather than an absence nobody noticed.

Discovery is recursive and covers every declaration shape Celery accepts in
this tree — adversarial review of this file's first version built a package
module (`workers/tasks/notify/__init__.py`), a name-less `@celery_app.task`,
and an aliased app import that all escaped a flat, canonical-form-only scan
while their routes stayed reserved, which is precisely the silent world this
guard exists to forbid. The shapes are pinned by their own test below. A
declaration reached through indirection AST cannot resolve would still
escape on the not-included branch; the registered-side completeness check in
the main guard is the recorded limit of what static discovery can promise.

ISOLATION. Nothing here mutates this process's Celery app or any repository
source. The one deliberate mutation — dropping a module from `include` to
prove the guard notices — happens inside a throwaway subprocess and dies with
it; every other what-if runs the pure comparison function on copies.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping
from fnmatch import fnmatch
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
TASKS_DIR = BACKEND / "workers" / "tasks"

#: Route patterns that are ALLOWED to match no declaring module, each with the
#: reason someone is holding the name. An entry here is a reviewed decision:
#: adding a route without either a declaring module or an entry fails the
#: guard, and an entry whose route gains a declaring module fails it in the
#: other direction until the entry is removed.
RESERVED_ROUTES: dict[str, str] = {
    "workers.tasks.ingestion.*": (
        "reserved queue name held for planned ingestion work; routed since "
        "the queue table was written and no module has ever declared under "
        "it (integration plan §4.2)"
    ),
    "workers.tasks.notify.*": (
        "reserved queue name held for planned notification work; routed "
        "since the queue table was written and no module has ever declared "
        "under it (integration plan §4.2)"
    ),
}

#: Runs in a FRESH interpreter. Imports only `workers.celery_app`, then loads
#: default modules the way a real worker start does — `WorkController` calls
#: `app.loader.init_worker()`, which imports `conf.include` + `conf.imports`.
#: The optional exclude knob exists for the non-vacuity test below and mutates
#: only the subprocess's own copy of the configuration.
_PROBE_SOURCE = """\
import json
import os
import sys

assert "celery" not in sys.modules, "not a clean process: celery pre-imported"
assert not any(m == "workers" or m.startswith("workers.") for m in sys.modules), (
    "not a clean process: workers.* pre-imported"
)

from workers.celery_app import celery_app

exclude = os.environ.get("CELERY_REGISTRATION_PROBE_EXCLUDE")
if exclude:
    celery_app.conf.include = [m for m in celery_app.conf.include if m != exclude]

task_modules_before_init = sorted(
    m for m in sys.modules if m.startswith("workers.tasks")
)

celery_app.loader.init_worker()
celery_app.finalize()

print(json.dumps({
    "include": list(celery_app.conf.include),
    "routes": sorted(celery_app.conf.task_routes or {}),
    "task_modules_imported_before_init": task_modules_before_init,
    "registered": sorted(celery_app.tasks),
}))
"""


def clean_worker_start_evidence(exclude_module: str | None = None) -> dict:
    """What a clean `celery -A workers.celery_app worker` start registers.

    Not prefixed `test_` and importable: `test_document_extraction_worker.py`
    uses this for its registration assertion, so that file's own module-level
    task import can never satisfy it.
    """
    env = {**os.environ, "PYTHONPATH": str(BACKEND)}
    env.pop("CELERY_REGISTRATION_PROBE_EXCLUDE", None)
    if exclude_module is not None:
        env["CELERY_REGISTRATION_PROBE_EXCLUDE"] = exclude_module
    probe = subprocess.run(
        [sys.executable, "-c", _PROBE_SOURCE],
        cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120,
    )
    assert probe.returncode == 0, (
        f"the clean-start probe itself failed — a worker would not boot:\n{probe.stderr}"
    )
    evidence = json.loads(probe.stdout.strip().splitlines()[-1])
    assert evidence["task_modules_imported_before_init"] == [], (
        "task modules were imported before the worker's own default-module "
        "loading ran, so the registration evidence would not be the include "
        f"list's: {evidence['task_modules_imported_before_init']}"
    )
    return evidence


def _declared_tasks(root: Path = TASKS_DIR) -> dict[str, set[str]]:
    """module → the task names it declares, read from source by AST.

    Discovery, not enumeration: a task added next year is covered without
    anyone editing a list — in whatever shape it arrives. Recursive, so a
    package module (`workers/tasks/x/__init__.py` is `workers.tasks.x`)
    counts; aware of the app being imported under another name and of
    `shared_task`; and a decorator with no `name=` yields the name Celery
    would generate, `module.function`. AST rather than regex so a task name
    quoted in a docstring — this file's own included — is never mistaken for
    a declaration. `root` is a parameter only so the discovery-shapes test
    below can run on an isolated tree.
    """
    declared: dict[str, set[str]] = {}
    for source_file in sorted(root.rglob("*.py")):
        if "__pycache__" in source_file.parts:
            continue
        parts = ("workers", "tasks", *source_file.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        module = ".".join(parts)
        tree = ast.parse(source_file.read_text(), filename=str(source_file))

        # The local names a declaration can arrive through: the app itself,
        # possibly aliased, and celery's shared_task. An alias is still a
        # declaration — adversarial review proved the guard blind without this.
        app_names = {"celery_app"}
        shared_names = {"shared_task"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                if node.module == "workers.celery_app" and alias.name == "celery_app":
                    app_names.add(alias.asname or alias.name)
                if node.module == "celery" and alias.name == "shared_task":
                    shared_names.add(alias.asname or alias.name)

        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                is_app_task = (
                    isinstance(target, ast.Attribute)
                    and target.attr == "task"
                    and isinstance(target.value, ast.Name)
                    and target.value.id in app_names
                )
                is_shared = isinstance(target, ast.Name) and target.id in shared_names
                if not (is_app_task or is_shared):
                    continue
                name = f"{module}.{node.name}"  # Celery's generated default
                if isinstance(decorator, ast.Call):
                    for keyword in decorator.keywords:
                        if (
                            keyword.arg == "name"
                            and isinstance(keyword.value, ast.Constant)
                            and isinstance(keyword.value.value, str)
                        ):
                            name = keyword.value.value
                declared.setdefault(module, set()).add(name)
    return declared


def _registration_findings(
    declared: Mapping[str, set[str]],
    routes: Iterable[str],
    reserved: Mapping[str, str],
    registered: Iterable[str],
) -> list[str]:
    """Every way the declared/routed/reserved/registered quartet can disagree.

    Pure so the non-vacuity tests can feed it counterfactual worlds without
    touching any live Celery state. Route patterns are Celery globs, so they
    are matched with fnmatch exactly as `test_admission_wiring.py` learned to.
    """
    route_patterns = set(routes)
    registered_set = set(registered)
    declaring_module = {
        task: module for module, tasks in declared.items() for task in tasks
    }
    findings: list[str] = []

    # Module → registration, the direction that was missing: a declared task
    # with a route MUST be registered by a clean worker start.
    for task in sorted(declaring_module):
        if not any(fnmatch(task, pattern) for pattern in route_patterns):
            continue  # unrouted; test_admission_wiring owns that direction
        if task not in registered_set:
            findings.append(
                f"{task} (declared in {declaring_module[task]}) is routed but NOT "
                f"registered from a clean worker start — {declaring_module[task]} "
                "is missing from celery_app include, so its queue is consumed by "
                "workers that reject every message on it"
            )

    # A route no declared task matches must be a recorded reservation.
    for pattern in sorted(route_patterns):
        if any(fnmatch(task, pattern) for task in declaring_module):
            continue
        if pattern not in reserved:
            findings.append(
                f"route {pattern!r} matches no declared task and has no "
                "RESERVED_ROUTES entry — add the declaring module, or record "
                "the reservation with a reason"
            )

    # And a recorded reservation must still be true in both directions.
    for pattern in sorted(reserved):
        claimed = sorted(t for t in declaring_module if fnmatch(t, pattern))
        if claimed:
            findings.append(
                f"reserved route {pattern!r} now has declaring task(s) {claimed} "
                "— remove its RESERVED_ROUTES entry so the registration guard "
                "covers them"
            )
        if pattern not in route_patterns:
            findings.append(
                f"reserved route {pattern!r} is no longer in task_routes — the "
                "reservation record is stale"
            )
    return findings


def test_every_routed_declared_task_is_registered_from_a_clean_worker_start():
    """The guard itself, on the real repository.

    First seen FAILING at the Slice 0B baseline — `workers.tasks.documents.
    extract_document` declared, routed, and absent from a clean start — and
    green only since `workers.tasks.documents` joined `celery_app.include`.
    """
    declared = _declared_tasks()
    assert declared, "no task declarations found; the discovery is looking in the wrong place"

    evidence = clean_worker_start_evidence()

    # Discovery must cover reality before reality is judged against it: a task
    # registered from a clean start that AST discovery cannot see would mean
    # tasks are being declared outside workers/tasks/*.py, and every check
    # below would be blind to them.
    all_declared = {task for tasks in declared.values() for task in tasks}
    undiscovered = sorted(
        task for task in evidence["registered"]
        if task.startswith("workers.") and task not in all_declared
    )
    assert not undiscovered, (
        "registered from a clean start but not visible to static discovery "
        f"(declared outside workers/tasks/*.py?): {undiscovered}"
    )

    findings = _registration_findings(
        declared, evidence["routes"], RESERVED_ROUTES, evidence["registered"]
    )
    assert not findings, (
        "clean-start Celery registration disagrees with the declared/routed "
        "surface:\n  " + "\n  ".join(findings)
    )


def test_every_reserved_route_is_a_recorded_decision_not_an_accident():
    """The reserved set equals — exactly — the routes with no declaring module.

    One direction catches a new route nobody claimed; the other catches a
    reservation that has gone stale because the module now exists, or because
    the route itself was removed. The route table is read from this process's
    `workers.celery_app` — configuration, not registration, so it is immune to
    which task modules other tests have imported.
    """
    from workers.celery_app import celery_app

    declared = _declared_tasks()
    all_declared = {task for tasks in declared.values() for task in tasks}
    unclaimed = {
        pattern for pattern in (celery_app.conf.task_routes or {})
        if not any(fnmatch(task, pattern) for task in all_declared)
    }
    assert unclaimed == set(RESERVED_ROUTES), (
        f"routes with no declaring module: {sorted(unclaimed)}; recorded "
        f"reservations: {sorted(RESERVED_ROUTES)} — the two must agree exactly"
    )
    for pattern, reason in RESERVED_ROUTES.items():
        assert reason.strip(), f"reserved route {pattern!r} has no recorded reason"


def test_static_discovery_sees_every_declaration_shape_celery_accepts(tmp_path):
    """The discovery itself is guarded, on an isolated tree.

    Adversarial review of this file's first version produced three worlds in
    which a declared, routed task escaped a flat canonical-form-only scan and
    every guard here passed silently: a package module, a name-less
    decorator, and the app imported under another name. Each is pinned so
    discovery cannot narrow again without this test noticing — and the prose
    decoy pins the reason discovery is AST rather than regex.
    """
    (tmp_path / "flat.py").write_text(
        "from celery import shared_task\n"
        "from workers.celery_app import celery_app\n\n\n"
        '@celery_app.task(name="workers.tasks.flat.explicit", bind=True)\n'
        "def explicit(self):\n"
        "    ...\n\n\n"
        "@celery_app.task\n"
        "def bare():\n"
        "    ...\n\n\n"
        "@shared_task\n"
        "def shared():\n"
        "    ...\n"
    )
    package = tmp_path / "notify"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from workers.celery_app import celery_app as app\n\n\n"
        "@app.task\n"
        "def send_email():\n"
        "    ...\n"
    )
    (tmp_path / "helpers.py").write_text(
        '"""Prose that quotes @celery_app.task(name="workers.tasks.helpers.decoy")."""\n\n\n'
        "def plain():\n"
        "    ...\n"
    )

    assert _declared_tasks(tmp_path) == {
        "workers.tasks.flat": {
            "workers.tasks.flat.explicit",
            "workers.tasks.flat.bare",
            "workers.tasks.flat.shared",
        },
        "workers.tasks.notify": {"workers.tasks.notify.send_email"},
    }


# ---------------------------------------------------------------------------
# Non-vacuity: the guard has been SEEN to fail in each direction it claims to
# cover. Each counterfactual is isolated — a throwaway subprocess, or copies —
# so nothing here can leak into another test in any order.
# ---------------------------------------------------------------------------
def test_the_guard_fails_when_a_declaring_module_is_dropped_from_include():
    """The original defect, reproduced on demand through the real startup path.

    The subprocess drops `workers.tasks.analysis` from its own copy of
    `include` before default-module loading — exactly the shape the documents
    defect had — and the guard must name both the unregistered task and the
    module to re-include.
    """
    evidence = clean_worker_start_evidence(exclude_module="workers.tasks.analysis")
    assert "workers.tasks.analysis" not in evidence["include"], (
        "the probe's exclude knob did not take effect; this proves nothing"
    )
    assert "workers.tasks.analysis.run_analysis" not in evidence["registered"], (
        "the task registered anyway, so registration does not come from the "
        "include list and this guard is watching the wrong mechanism"
    )

    findings = _registration_findings(
        _declared_tasks(), evidence["routes"], RESERVED_ROUTES, evidence["registered"]
    )
    relevant = [f for f in findings if "workers.tasks.analysis.run_analysis" in f]
    assert relevant, (
        "a routed, declared task was absent from a clean start and the guard "
        f"said nothing; findings were: {findings}"
    )
    assert any("workers.tasks.analysis" in f and "include" in f for f in relevant), (
        f"the finding does not name the module to re-include: {relevant}"
    )


def test_the_guard_fails_on_a_route_nobody_declares_and_nobody_reserved():
    """A newly added unexplained route is a statement nobody wrote down.

    Registration evidence is irrelevant to this direction, so the world is
    synthetic and exact: every declared task registered, one route added. The
    single finding must be the unexplained route.
    """
    declared = _declared_tasks()
    all_declared = {task for tasks in declared.values() for task in tasks}
    from workers.celery_app import celery_app

    routes = set(celery_app.conf.task_routes or {}) | {"workers.tasks.mystery.*"}

    findings = _registration_findings(declared, routes, RESERVED_ROUTES, all_declared)
    assert len(findings) == 1 and "workers.tasks.mystery.*" in findings[0], (
        f"expected exactly one finding, for the unexplained route: {findings}"
    )
    assert "RESERVED_ROUTES" in findings[0], (
        f"the finding does not say how to resolve it: {findings[0]}"
    )


def test_the_guard_fails_while_a_reserved_route_has_a_declaring_task():
    """A reservation must be released the moment the module becomes real.

    In a world where `workers.tasks.ingestion` now declares a task but the
    reservation still stands, the guard must fail twice over: the record is
    stale, and — because a freshly declared module is not in `include` either —
    the task is routed and unregistered. Releasing the reservation and
    registering the task is what clears it, which is the review path a real
    ingestion module will have to walk.
    """
    declared = {
        module: set(tasks) for module, tasks in _declared_tasks().items()
    }
    declared["workers.tasks.ingestion"] = {"workers.tasks.ingestion.pull_provider_feed"}
    previously_declared = {
        task for module, tasks in declared.items()
        if module != "workers.tasks.ingestion" for task in tasks
    }
    from workers.celery_app import celery_app

    routes = set(celery_app.conf.task_routes or {})

    findings = _registration_findings(
        declared, routes, RESERVED_ROUTES, previously_declared
    )
    assert any(
        "reserved route 'workers.tasks.ingestion.*'" in f and "remove" in f
        for f in findings
    ), f"a claimed reservation was not reported stale: {findings}"
    assert any(
        "workers.tasks.ingestion.pull_provider_feed" in f and "include" in f
        for f in findings
    ), f"the unregistered new task was not reported: {findings}"

    # And the guard goes quiet only once the reservation is released AND the
    # task actually registers — the complete fix, not either half.
    released = {p: r for p, r in RESERVED_ROUTES.items() if "ingestion" not in p}
    assert not _registration_findings(
        declared, routes, released,
        previously_declared | {"workers.tasks.ingestion.pull_provider_feed"},
    ), "releasing the reservation and registering the task should satisfy the guard"
