"""Structural assertions about the deletion cutoff (Entry 11B1 §30).

The cutoff is only worth as much as its coverage, and coverage is exactly the
property no functional test can see. A route added next year that opens its own
unit of work writes user data for a deleting account, passes every test it comes
with, and looks entirely normal in review — that is how the scenario routes came
to bypass it, and reading them one at a time is not a control.

So these tests look at the SHAPE of the application: which routes meet the
cutoff, and which task entry points check it before they write. They need no
database, because the question is what the repository contains rather than what
PostgreSQL does with it.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from app.api.v1.router import api_router

BACKEND = Path(__file__).resolve().parents[2]
WORKERS = BACKEND / "workers" / "tasks"

#: Dependencies that apply the cutoff, directly or by containing it.
_CUTOFF_DEPENDENCIES = frozenset({"db_authed", "assert_account_active"})

#: The only user-facing routes allowed to work for a deleting account, and why.
#: Requesting deletion must stay idempotent, and a user must be able to read the
#: status of the thing they asked for. Both are keyed on the caller's own token
#: and neither creates user data.
_EXEMPT: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/account/deletion"),
    ("GET", "/account/deletion"),
})

#: The operator plane. Separate credentials, separate tables, and an operator is
#: not an account that can be deleted — `db_admin` and `db_anon` are therefore
#: not gaps.
_NON_TENANT_DEPENDENCIES = frozenset({"db_admin", "db_anon"})


def _routes(router) -> list[APIRoute]:
    """Every APIRoute, through FastAPI's lazily-included routers."""
    found: list[APIRoute] = []
    for route in getattr(router, "routes", []):
        if isinstance(route, APIRoute):
            found.append(route)
        else:
            original = getattr(route, "original_router", None)
            if original is not None:
                found.extend(_routes(original))
    return found


def _dependency_names(dependant) -> set[str]:
    names: set[str] = set()
    for sub in dependant.dependencies:
        name = getattr(sub.call, "__name__", None)
        if name:
            names.add(name)
        names |= _dependency_names(sub)
    return names


def _tenant_routes() -> list[tuple[str, str, set[str]]]:
    out = []
    for route in _routes(api_router):
        names = _dependency_names(route.dependant)
        if names & _NON_TENANT_DEPENDENCIES:
            continue
        for method in sorted(route.methods or []):
            if method in {"HEAD", "OPTIONS"}:
                continue
            out.append((method, route.path, names))
    return out


def test_the_application_has_tenant_routes_to_check():
    """Guards the two tests below: a walker that silently found nothing would
    make both of them pass while asserting about an empty set."""
    assert len(_tenant_routes()) > 20


@pytest.mark.parametrize(
    "method,path,names",
    [pytest.param(m, p, n, id=f"{m} {p}") for m, p, n in _tenant_routes()],
)
def test_every_tenant_route_meets_the_deletion_cutoff(method, path, names):
    """A route serving an authenticated account either applies the cutoff or is
    on the exempt list with a stated reason.

    Taking `db_authed` is the usual way. A route that manages its own session
    takes `assert_account_active` instead — the point is that there is no third
    option where the check simply does not happen.
    """
    if (method, path) in _EXEMPT:
        assert not (names & _CUTOFF_DEPENDENCIES), (
            f"{method} {path} is on the exempt list but also applies the "
            "cutoff; requesting deletion twice would then be refused by the "
            "cutoff the first request installed"
        )
        return

    assert names & _CUTOFF_DEPENDENCIES, (
        f"{method} {path} serves an authenticated account without meeting the "
        f"deletion cutoff (dependencies: {sorted(names) or 'none'}). Take "
        "`db_authed`, or `assert_account_active` if the handler opens its own "
        "unit of work. Adding it to _EXEMPT requires a reason that survives "
        "'this route writes user data for an account that asked to be deleted'."
    )


# ---------------------------------------------------------------------------
# workers
# ---------------------------------------------------------------------------
_TASK_DECORATOR = re.compile(r"celery_app\.task")


def _task_modules() -> list[Path]:
    return sorted(p for p in WORKERS.glob("*.py") if p.name != "__init__.py")


def _user_scoped_tasks() -> list[tuple[Path, ast.AST]]:
    """Celery tasks that take a `user_id`.

    That parameter — not the kind of unit of work the body happens to open — is
    what makes a task capable of producing user data. `run_optimization` opens a
    SYSTEM unit of work and still writes an optimization run for the account it
    was given, so a rule phrased in terms of `unit_of_work(user_id=...)` would
    have declared it out of scope while it wrote.

    The tasks without one are system work: freshness invalidation across
    tenants, the outbox relay, integrity sweeps, and the operator import
    pipeline. They are not "new user work" and later 11B phases, not this one,
    decide what they owe a deleted account.
    """
    found = []
    for module in _task_modules():
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
                continue
            decorated = any(_TASK_DECORATOR.search(ast.unparse(d))
                            for d in node.decorator_list)
            if decorated and "user_id" in {a.arg for a in node.args.args}:
                found.append((module, node))
    return found


@pytest.mark.parametrize(
    "module,node",
    _user_scoped_tasks(),
    ids=[f"{m.name}::{n.name}" for m, n in _user_scoped_tasks()],
)
def test_every_user_scoped_task_checks_the_cutoff_before_writing(module, node):
    """§10 — the queue race.

    A task published before a deletion request is executed after it. Celery
    `revoke` does not close that: a reserved task is already in a worker's
    hands, `acks_late` redelivers it after a restart, and a worker that was
    offline never sees the broadcast. The only reliable refusal is in the same
    database the request was recorded in, immediately before the work commits.
    """
    body = ast.unparse(node)
    assert "refuse_if_deleting" in body or "account_is_deleting" in body, (
        f"{module.relative_to(BACKEND)}::{node.name} takes a user_id and can "
        "write user data, but never checks the deletion cutoff. Call "
        "`refuse_if_deleting` before the work begins; see "
        "app/services/privacy/preflight.py."
    )


def test_the_worker_scan_finds_the_tasks_it_is_meant_to_cover():
    """The parametrized test above passes vacuously against an empty task
    list — which is exactly what a decorator rename would produce."""
    found = {f"{m.name}::{n.name}" for m, n in _user_scoped_tasks()}
    assert found == {"analysis.py::run_analysis", "ioe.py::run_optimization"}, found


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------
def test_login_and_refresh_consult_the_cutoff():
    """Sessions and tokens must stop working, which is a property of the two
    functions that issue them rather than of any route."""
    source = (BACKEND / "app" / "services" / "auth" / "service.py").read_text()
    tree = ast.parse(source)
    bodies = {
        node.name: ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    for name in ("authenticate", "refresh"):
        assert name in bodies, f"AuthService.{name} has been renamed"
        assert "_refuse_if_deleting" in bodies[name], (
            f"AuthService.{name} issues tokens without consulting the deletion "
            "cutoff, so a deleting account could still obtain a working session"
        )


def test_the_cutoff_is_read_through_the_privileged_function_not_the_table():
    """Login, admission and the worker preflight all run without `app.user_id`.

    RLS therefore hides the lifecycle row from them — correctly — so a direct
    `SELECT ... FROM identity.account_lifecycle` in any of these paths sees
    nothing and admits every deleting account. The bug is silent, which is why
    it is asserted structurally rather than left to a passing test.
    """
    anonymous_paths = [
        BACKEND / "app" / "services" / "auth" / "service.py",
        BACKEND / "app" / "services" / "admission" / "guard.py",
        BACKEND / "app" / "services" / "privacy" / "preflight.py",
    ]
    for path in anonymous_paths:
        source = path.read_text()
        assert "account_deletion_state" in source, (
            f"{path.relative_to(BACKEND)} does not read the cutoff through "
            "identity.account_deletion_state"
        )
        assert "FROM identity.account_lifecycle" not in source, (
            f"{path.relative_to(BACKEND)} reads the lifecycle table directly "
            "from a session that sets no app.user_id; RLS would hide the row "
            "and the check would silently pass for every deleting account"
        )
